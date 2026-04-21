"""Jira Cloud REST API wrapper (httpx + /rest/api/3/search/jql + /rest/dev-status for MRs)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from jira_rag.config.schema import JiraConfig
from jira_rag.utils.logging import get_logger

logger = get_logger(__name__)


_ISSUE_FIELDS = [
    "summary",
    "description",
    "issuetype",
    "status",
    "priority",
    "resolution",
    "assignee",
    "reporter",
    "labels",
    "components",
    "fixVersions",
    "parent",
    "customfield_10014",  # Epic Link (default id on many Jira Cloud sites)
    "created",
    "updated",
    "resolutiondate",
    "comment",
]


class JiraClient:
    def __init__(self, config: JiraConfig) -> None:
        self._config = config
        self._http = httpx.Client(
            base_url=config.url,
            auth=(config.email, config.api_token),
            headers={"Accept": "application/json"},
            timeout=30.0,
        )

    # ── issues (JQL via /rest/api/3/search/jql — token-paginated) ────────────
    # Atlassian retired /rest/api/3/search (the old `startAt`/`total` endpoint)
    # in 2025. Its replacement exposes cursor-based pagination via
    # `nextPageToken` and no longer returns `total`.
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def _jql_page(
        self,
        jql: str,
        next_page_token: Optional[str],
        max_results: int,
    ) -> dict:
        params: dict[str, Any] = {
            "jql": jql,
            "fields": ",".join(_ISSUE_FIELDS),
            "expand": "changelog",
            "maxResults": max_results,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        resp = self._http.get("/rest/api/3/search/jql", params=params)
        resp.raise_for_status()
        return resp.json()

    def iter_project_issues(
        self,
        project_key: str,
        updated_since: datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield raw issue dicts from Jira, ordered by updated ASC.

        `updated_since` is used as a cursor: we ask for issues with
        `updated >= <cursor>`. Callers should subtract a small safety window
        and rely on upsert idempotency.
        """
        jql_parts = [f'project = "{project_key}"']
        if self._config.jql_filter:
            jql_parts.append(f"({self._config.jql_filter})")
        if updated_since is not None:
            ts = updated_since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
            jql_parts.append(f'updated >= "{ts}"')
        jql = " AND ".join(jql_parts) + " ORDER BY updated ASC"

        logger.info("jira.jql", jql=jql)

        next_page_token: Optional[str] = None
        page_size = self._config.page_size
        while True:
            page = self._jql_page(jql, next_page_token, page_size)
            issues = page.get("issues", []) or []
            if not issues:
                break
            for issue in issues:
                yield issue
            next_page_token = page.get("nextPageToken")
            if not next_page_token:
                break

    # ── single issue (used by webhook path) ──────────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_issue(self, issue_key: str) -> dict[str, Any] | None:
        """Fetch one issue by key with the same fields + expand as `iter_project_issues`.

        Returns None if Jira reports 404 (issue deleted or no permission).
        """
        resp = self._http.get(
            f"/rest/api/3/issue/{issue_key}",
            params={
                "fields": ",".join(_ISSUE_FIELDS),
                "expand": "changelog",
            },
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ── changelog extracted from issue (already expanded) ────────────────────
    @staticmethod
    def changelog(issue: dict) -> list[dict]:
        return (issue.get("changelog") or {}).get("histories", []) or []

    # ── comments (paginated — `comment` field caps at ~50 on large issues) ───
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def iter_comments(self, issue_key: str) -> Iterator[dict[str, Any]]:
        start_at = 0
        page_size = 100
        while True:
            resp = self._http.get(
                f"/rest/api/3/issue/{issue_key}/comment",
                params={"startAt": start_at, "maxResults": page_size},
            )
            resp.raise_for_status()
            data = resp.json()
            comments = data.get("comments", []) or []
            if not comments:
                break
            for c in comments:
                yield c
            total = data.get("total", 0)
            start_at += len(comments)
            if start_at >= total:
                break

    # ── merge requests via Jira Dev Panel ────────────────────────────────────
    # Uses the internal but stable /rest/dev-status/1.0/issue/detail endpoint
    # that the Dev Panel itself calls. Requires the issue's numeric id.
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_dev_info(self, issue_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for application_type in ("GitLab", "stash", "GitHub", "bitbucket"):
            resp = self._http.get(
                "/rest/dev-status/1.0/issue/detail",
                params={
                    "issueId": issue_id,
                    "applicationType": application_type,
                    "dataType": "pullrequest",
                },
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            payload = resp.json()
            details = payload.get("detail") or []
            if not details:
                continue
            pulls = details[0].get("pullRequests") or []
            if pulls:
                result[application_type] = pulls
        return result

    # ── remote links (fallback when dev-panel isn't available) ───────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_remote_links(self, issue_key: str) -> list[dict[str, Any]]:
        resp = self._http.get(f"/rest/api/3/issue/{issue_key}/remotelink")
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json() or []

    def close(self) -> None:
        self._http.close()


def create_jira_client(config: JiraConfig) -> JiraClient:
    return JiraClient(config)
