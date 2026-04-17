"""Map raw Jira API payloads → DB row dicts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jira_rag.utils.text import adf_to_text, normalise_text


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # Jira returns ISO 8601 with offset, e.g. "2026-04-17T10:23:45.123+0000"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # Fallback: drop sub-second portion if non-standard
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
        except ValueError:
            return None


def _name(field: dict | None) -> str:
    if not field:
        return ""
    return field.get("displayName") or field.get("name") or field.get("value") or ""


def _progress_percent(status_category: str, resolution: str) -> int:
    """Rough numeric progress for downstream filtering.

    We don't have a reliable per-issue progress in Jira, so derive from status:
      - Done / resolved → 100
      - In Progress     → 50
      - To Do / Open    → 0
    """
    if resolution or status_category == "done":
        return 100
    if status_category == "indeterminate":
        return 50
    return 0


def issue_to_row(raw: dict[str, Any], project_key: str) -> dict[str, Any]:
    fields = raw.get("fields") or {}
    status = fields.get("status") or {}
    status_category = (status.get("statusCategory") or {}).get("key") or ""

    description_text = normalise_text(adf_to_text(fields.get("description")))
    summary = fields.get("summary") or ""

    parent = fields.get("parent") or {}
    resolution = _name(fields.get("resolution"))

    return {
        "key": raw.get("key"),
        "project_key": project_key,
        "summary": summary,
        "description_text": description_text,
        "issue_type": _name(fields.get("issuetype")),
        "status": _name(status),
        "status_category": status_category,
        "priority": _name(fields.get("priority")),
        "resolution": resolution,
        "assignee": _name(fields.get("assignee")),
        "reporter": _name(fields.get("reporter")),
        "labels": list(fields.get("labels") or []),
        "components": [c.get("name", "") for c in (fields.get("components") or [])],
        "fix_versions": [v.get("name", "") for v in (fields.get("fixVersions") or [])],
        "parent_key": parent.get("key") if parent else None,
        "epic_key": fields.get("customfield_10014") or None,
        "progress_percent": _progress_percent(status_category, resolution),
        "created_at": _parse_dt(fields.get("created")),
        "updated_at": _parse_dt(fields.get("updated")),
        "resolved_at": _parse_dt(fields.get("resolutiondate")),
        "raw": raw,
    }


def comment_to_row(raw: dict[str, Any], issue_key: str) -> dict[str, Any]:
    body_text = normalise_text(adf_to_text(raw.get("body")))
    return {
        "id": str(raw.get("id")),
        "issue_key": issue_key,
        "author": _name(raw.get("author") or raw.get("updateAuthor")),
        "body_text": body_text,
        "created_at": _parse_dt(raw.get("created")),
        "updated_at": _parse_dt(raw.get("updated")),
        "raw": raw,
    }


def extract_status_history(raw_issue: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull `status` transitions from the expanded changelog."""
    issue_key = raw_issue.get("key")
    rows: list[dict[str, Any]] = []
    for history in (raw_issue.get("changelog") or {}).get("histories", []) or []:
        when = _parse_dt(history.get("created"))
        author = _name(history.get("author"))
        for item in history.get("items", []) or []:
            if item.get("field") != "status":
                continue
            rows.append(
                {
                    "issue_key": issue_key,
                    "from_status": item.get("fromString") or "",
                    "to_status": item.get("toString") or "",
                    "changed_by": author,
                    "changed_at": when,
                }
            )
    return rows


def remote_link_to_mr_row(raw: dict[str, Any], issue_key: str) -> dict[str, Any] | None:
    """Best-effort mapping of a Jira Remote Link to a merge-request row.

    Returns None if the link doesn't look like a pull/merge request.
    """
    obj = raw.get("object") or {}
    url = obj.get("url") or ""
    title = obj.get("title") or ""
    lowered = (url + " " + title).lower()
    if not any(tag in lowered for tag in ("merge_requests", "pull/", "pull-request", "/pr/")):
        return None

    provider = "unknown"
    if "gitlab" in lowered:
        provider = "gitlab"
    elif "github" in lowered:
        provider = "github"
    elif "bitbucket" in lowered:
        provider = "bitbucket"

    external_id = str(raw.get("id") or url)
    return {
        "id": f"{provider}:{external_id}",
        "issue_key": issue_key,
        "provider": provider,
        "url": url,
        "title": title,
        "description": obj.get("summary") or "",
        "source_branch": "",
        "target_branch": "",
        "state": (obj.get("status") or {}).get("name", "") if isinstance(obj.get("status"), dict) else "",
        "author": "",
        "merged_at": None,
        "created_at": None,
        "updated_at": None,
        "raw": raw,
    }


def dev_info_to_mr_rows(
    dev_info: dict[str, list[dict[str, Any]]],
    issue_key: str,
) -> list[dict[str, Any]]:
    """Convert /rest/dev-status payloads into MR rows."""
    rows: list[dict[str, Any]] = []
    for application_type, pulls in dev_info.items():
        provider = application_type.lower()
        for pr in pulls:
            external_id = str(pr.get("id") or pr.get("url") or "")
            if not external_id:
                continue
            source = pr.get("source") or {}
            dest = pr.get("destination") or {}
            rows.append(
                {
                    "id": f"{provider}:{external_id}",
                    "issue_key": issue_key,
                    "provider": provider,
                    "url": pr.get("url", ""),
                    "title": pr.get("name") or pr.get("title") or "",
                    "description": pr.get("description") or "",
                    "source_branch": source.get("branch", "") if isinstance(source, dict) else "",
                    "target_branch": dest.get("branch", "") if isinstance(dest, dict) else "",
                    "state": (pr.get("status") or "").lower(),
                    "author": (pr.get("author") or {}).get("name", "") if isinstance(pr.get("author"), dict) else "",
                    "merged_at": _parse_dt(pr.get("lastUpdate")) if (pr.get("status") or "").lower() == "merged" else None,
                    "created_at": None,
                    "updated_at": _parse_dt(pr.get("lastUpdate")),
                    "raw": pr,
                }
            )
    return rows
