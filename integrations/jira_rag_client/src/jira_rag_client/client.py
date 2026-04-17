"""HTTP client + typed dataclasses for the jira-rag service.

Stdlib-only: no httpx / requests dependency so this package is safe to drop
into any agent environment.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

log = logging.getLogger("jira_rag_client")

_DEFAULT_URL = "http://localhost:8100"
_DEFAULT_TIMEOUT = 5.0


class JiraRagError(RuntimeError):
    """Raised by client methods when `raise_on_error=True` and a call fails.

    The module-level convenience functions never raise — they swallow errors
    and return empty values so the caller's agent flow isn't disrupted.
    """


# ── Typed response models ────────────────────────────────────────────────────
@dataclass
class Comment:
    id: str = ""
    issue_key: str = ""
    author: str = ""
    body_text: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Comment":
        return cls(
            id=str(d.get("id", "")),
            issue_key=d.get("issue_key", ""),
            author=d.get("author", ""),
            body_text=d.get("body_text", ""),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )


@dataclass
class MergeRequest:
    id: str = ""
    issue_key: str = ""
    provider: str = ""
    url: str = ""
    title: str = ""
    description: str = ""
    state: str = ""
    source_branch: str = ""
    target_branch: str = ""
    author: str = ""
    merged_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "MergeRequest":
        return cls(
            id=str(d.get("id", "")),
            issue_key=d.get("issue_key", ""),
            provider=d.get("provider", ""),
            url=d.get("url", ""),
            title=d.get("title", ""),
            description=d.get("description", ""),
            state=d.get("state", ""),
            source_branch=d.get("source_branch", ""),
            target_branch=d.get("target_branch", ""),
            author=d.get("author", ""),
            merged_at=d.get("merged_at"),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )


@dataclass
class IssueContext:
    """Full hydrated record for a Jira issue."""

    key: str = ""
    project_key: str = ""
    summary: str = ""
    description_text: str = ""
    issue_type: str = ""
    status: str = ""
    status_category: str = ""
    priority: str = ""
    resolution: str = ""
    assignee: str = ""
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    progress_percent: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    resolved_at: Optional[str] = None
    comments: list[Comment] = field(default_factory=list)
    merge_requests: list[MergeRequest] = field(default_factory=list)
    status_history: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "IssueContext":
        return cls(
            key=d.get("key", ""),
            project_key=d.get("project_key", ""),
            summary=d.get("summary", ""),
            description_text=d.get("description_text", ""),
            issue_type=d.get("issue_type", ""),
            status=d.get("status", ""),
            status_category=d.get("status_category", ""),
            priority=d.get("priority", ""),
            resolution=d.get("resolution", ""),
            assignee=d.get("assignee", ""),
            labels=list(d.get("labels") or []),
            components=list(d.get("components") or []),
            progress_percent=int(d.get("progress_percent") or 0),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            resolved_at=d.get("resolved_at"),
            comments=[Comment.from_dict(c) for c in (d.get("comments") or [])],
            merge_requests=[MergeRequest.from_dict(m) for m in (d.get("merge_requests") or [])],
            status_history=list(d.get("status_history") or []),
        )


@dataclass
class SearchHit:
    issue_key: str = ""
    score: float = 0.0
    summary: str = ""
    match_source: str = ""  # "issue" | "comment" | "merge_request"
    match_preview: str = ""
    context: Optional[IssueContext] = None

    @classmethod
    def from_dict(cls, d: dict) -> "SearchHit":
        ctx = d.get("context")
        return cls(
            issue_key=d.get("issue_key", ""),
            score=float(d.get("score") or 0.0),
            summary=d.get("summary", ""),
            match_source=d.get("match_source", ""),
            match_preview=d.get("match_preview", ""),
            context=IssueContext.from_dict(ctx) if ctx else None,
        )


# ── Client class ─────────────────────────────────────────────────────────────
class JiraRagClient:
    """HTTP client for a running jira-rag service.

    Configuration resolution order:
        1. Explicit args to __init__
        2. Env vars JIRA_RAG_URL / JIRA_RAG_TIMEOUT
        3. Defaults (http://localhost:8100, 5s)
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        *,
        raise_on_error: bool = False,
    ) -> None:
        self.base_url = (base_url or os.environ.get("JIRA_RAG_URL") or _DEFAULT_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else float(
            os.environ.get("JIRA_RAG_TIMEOUT", _DEFAULT_TIMEOUT)
        )
        self.raise_on_error = raise_on_error

    # ── public API ───────────────────────────────────────────────────────────
    def health_check(self) -> bool:
        """Ping /health. Returns False on any error (never raises)."""
        try:
            self._get("/health", raise_on_error=True)
            return True
        except Exception:
            return False

    def get_issue(self, issue_key: str) -> Optional[IssueContext]:
        """Fetch the full hydrated issue by key."""
        if not issue_key:
            return None
        data = self._get(f"/issues/{issue_key.upper()}")
        if not data or data.get("error"):
            return None
        return IssueContext.from_dict(data)

    def search(
        self,
        query: str,
        *,
        project_keys: Optional[Iterable[str]] = None,
        top_k: int = 5,
        min_score: float = 0.4,
        include_comments: bool = True,
        include_merge_requests: bool = False,
    ) -> list[SearchHit]:
        """Semantic search. Returns ranked hits with hydrated issue context."""
        if not query:
            return []
        params: dict[str, Any] = {
            "q": query,
            "top_k": top_k,
            "min_score": min_score,
            "include_comments": str(include_comments).lower(),
            "include_merge_requests": str(include_merge_requests).lower(),
        }
        if project_keys:
            params["project"] = [p.upper() for p in project_keys]

        data = self._get("/search", params=params) or {}
        return [SearchHit.from_dict(h) for h in (data.get("hits") or [])]

    # ── internals ────────────────────────────────────────────────────────────
    def _get(
        self,
        path: str,
        params: Optional[dict] = None,
        raise_on_error: Optional[bool] = None,
    ) -> Optional[dict]:
        should_raise = self.raise_on_error if raise_on_error is None else raise_on_error
        url = f"{self.base_url}{path}"
        if params:
            pairs = []
            for k, v in params.items():
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    pairs.extend((k, str(x)) for x in v)
                else:
                    pairs.append((k, str(v)))
            url = f"{url}?{urllib.parse.urlencode(pairs)}"

        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            msg = f"HTTP {e.code} on GET {path}"
            log.warning("jira_rag_client: %s", msg)
            if should_raise:
                raise JiraRagError(msg) from e
            return None
        except Exception as e:
            msg = f"GET {path} failed: {e}"
            log.warning("jira_rag_client: %s", msg)
            if should_raise:
                raise JiraRagError(msg) from e
            return None


# ── Module-level convenience API (dict-returning, legacy-compatible) ─────────
_default_client: Optional[JiraRagClient] = None


def _default() -> JiraRagClient:
    global _default_client
    if _default_client is None:
        _default_client = JiraRagClient()
    return _default_client


def health_check() -> bool:
    """Ping the default client's /health endpoint."""
    return _default().health_check()


def get_issue_context(issue_key: str) -> dict:
    """Return the full hydrated issue as a plain dict (empty on failure).

    Backward-compatible with the pre-package mirelia-agent helper.
    """
    issue = _default().get_issue(issue_key)
    if not issue:
        return {}
    return _issue_to_dict(issue)


def find_related_tasks(
    query: str,
    project_keys: Optional[list[str]] = None,
    top_k: int = 3,
    min_score: float = 0.45,
    include_comments: bool = True,
    include_merge_requests: bool = False,
) -> list[dict]:
    """Return ranked search hits as plain dicts (same shape as the server)."""
    hits = _default().search(
        query,
        project_keys=project_keys,
        top_k=top_k,
        min_score=min_score,
        include_comments=include_comments,
        include_merge_requests=include_merge_requests,
    )
    return [_hit_to_dict(h) for h in hits]


def _issue_to_dict(issue: IssueContext) -> dict:
    return {
        "key": issue.key,
        "project_key": issue.project_key,
        "summary": issue.summary,
        "description_text": issue.description_text,
        "issue_type": issue.issue_type,
        "status": issue.status,
        "status_category": issue.status_category,
        "priority": issue.priority,
        "resolution": issue.resolution,
        "assignee": issue.assignee,
        "labels": list(issue.labels),
        "components": list(issue.components),
        "progress_percent": issue.progress_percent,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "resolved_at": issue.resolved_at,
        "comments": [c.__dict__ for c in issue.comments],
        "merge_requests": [m.__dict__ for m in issue.merge_requests],
        "status_history": list(issue.status_history),
    }


def _hit_to_dict(hit: SearchHit) -> dict:
    return {
        "issue_key": hit.issue_key,
        "score": hit.score,
        "summary": hit.summary,
        "match_source": hit.match_source,
        "match_preview": hit.match_preview,
        "context": _issue_to_dict(hit.context) if hit.context else None,
    }
