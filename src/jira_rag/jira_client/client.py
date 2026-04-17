"""Jira Cloud REST API wrapper (atlassian-python-api + direct /rest/dev-status for MRs)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

import httpx
from atlassian import Jira
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
        self._client = Jira(
            url=config.url,
            username=config.email,
            password=config.api_token,
            cloud=True,
        )
        # Separate httpx client for the internal /rest/dev-status endpoint
        # (not exposed by atlassian-python-api).
        self._http = httpx.Client(
            base_url=config.url,
            auth=(config.email, config.api_token),
            headers={"Accept": "application/json"},
            timeout=30.0,
        )

    # ── issues (JQL, paginated) ──────────────────────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def _jql_page(self, jql: str, start_at: int, max_results: int) -> dict:
        return self._client.jql(
            jql,
            start=start_at,
            limit=max_results,
            fields=",".join(_ISSUE_FIELDS),
            expand="changelog",
        )

    def iter_project_issues(
        self,
        project_key: str,
        updated_since: datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield raw issue dicts from Jira, newest updates first.

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

        start_at = 0
        page_size = self._config.page_size
        while True:
            page = self._jql_page(jql, start_at, page_size)
            issues = page.get("issues", []) or []
            if not issues:
                break
            for issue in issues:
                yield issue
            total = page.get("total", 0)
            start_at += len(issues)
            if start_at >= total:
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
