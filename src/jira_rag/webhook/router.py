"""FastAPI router for Jira webhook events.

Jira Cloud does not sign webhook payloads, so authentication is done via a
shared secret embedded in the URL path — the standard pattern for Jira
integrations. Configure the webhook URL as:

    https://<public-host>/webhook/jira/<secret>

Events handled (subscribe to these in Jira → System → Webhooks):
    - jira:issue_created
    - jira:issue_updated
    - jira:issue_deleted
    - comment_created
    - comment_updated
    - comment_deleted
    - jira:worklog_updated

For each event we either re-sync the affected issue by key (fetches fresh
fields + comments + MRs, embeds, upserts) or delete it. The handler is
fire-and-forget: we return 202 Accepted immediately and process in the
background so Jira's 10-second webhook timeout is never the bottleneck.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from jira_rag.config.schema import WebhookConfig
from jira_rag.indexer import SyncService
from jira_rag.utils.logging import get_logger

logger = get_logger(__name__)

_DELETE_EVENTS = {"jira:issue_deleted"}
_ISSUE_EVENTS = {
    "jira:issue_created",
    "jira:issue_updated",
    "jira:worklog_updated",
    "comment_created",
    "comment_updated",
    "comment_deleted",
}


def _extract_issue_key(payload: dict[str, Any]) -> str | None:
    issue = payload.get("issue") or {}
    key = issue.get("key")
    if key:
        return key
    # Some events (e.g. comment_*) nest the issue under `comment.parent`.
    comment = payload.get("comment") or {}
    parent = comment.get("parent") or {}
    return parent.get("key")


def _handle_event(sync_service: SyncService, payload: dict[str, Any]) -> None:
    event = payload.get("webhookEvent") or ""
    issue_key = _extract_issue_key(payload)
    if not issue_key:
        logger.warning("webhook.no_issue_key", event=event)
        return

    try:
        if event in _DELETE_EVENTS:
            sync_service.delete_issue(issue_key)
        elif event in _ISSUE_EVENTS:
            sync_service.sync_single_issue(issue_key)
        else:
            logger.info("webhook.event.ignored", event=event, issue_key=issue_key)
    except Exception:
        # Never raise — Jira retries aggressively on 5xx.
        logger.exception("webhook.handler.failed", event=event, issue_key=issue_key)


def build_webhook_router(
    config: WebhookConfig,
    sync_service: SyncService,
) -> APIRouter:
    router = APIRouter()
    expected_secret = config.secret

    @router.post("/webhook/jira/{secret}", status_code=202)
    async def jira_webhook(
        secret: str,
        request: Request,
        background: BackgroundTasks,
    ) -> dict:
        if not expected_secret:
            raise HTTPException(503, "webhook secret not configured")
        if not hmac.compare_digest(secret, expected_secret):
            raise HTTPException(401, "invalid webhook secret")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON payload")

        event = payload.get("webhookEvent") or ""
        issue_key = _extract_issue_key(payload) or ""
        logger.info("webhook.received", event=event, issue_key=issue_key)

        background.add_task(_handle_event, sync_service, payload)
        return {"accepted": True, "event": event, "issue_key": issue_key}

    return router
