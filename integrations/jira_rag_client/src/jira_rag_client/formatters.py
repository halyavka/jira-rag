"""Markdown formatters for injecting jira-rag responses into LLM prompts.

Accepts either the typed dataclasses (IssueContext / SearchHit) or plain
dicts with the same keys — so callers using the module-level dict API don't
have to convert.
"""

from __future__ import annotations

from typing import Any, Union

from jira_rag_client.client import IssueContext, SearchHit

IssueLike = Union[IssueContext, dict]
HitLike = Union[SearchHit, dict]


def _get(obj: Any, attr: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def format_issue_for_prompt(
    issue: IssueLike,
    *,
    max_description_chars: int = 2500,
    max_comments: int = 3,
    max_comment_chars: int = 600,
    max_merge_requests: int = 5,
) -> str:
    """Render an issue as a Markdown section ready to paste into an LLM prompt.

    Empty input → empty string (so callers can inject unconditionally).
    """
    if not issue:
        return ""

    key = _get(issue, "key")
    if not key:
        return ""

    status = _get(issue, "status")
    progress = _get(issue, "progress_percent")
    progress_suffix = f" / {progress}%" if progress else ""

    lines: list[str] = [
        f"## Jira task context: {key} [{status}{progress_suffix}]",
        f"**{_get(issue, 'summary')}**",
    ]

    description = _get(issue, "description_text")
    if description:
        lines.append("")
        lines.append(description[:max_description_chars])

    comments = _get(issue, "comments") or []
    if comments:
        take = min(max_comments, len(comments))
        lines.append("")
        lines.append(f"**Last {take} comment(s):**")
        for c in list(comments)[-max_comments:]:
            body = (_get(c, "body_text") or "").strip()
            if not body:
                continue
            author = _get(c, "author", "?") or "?"
            when = _get(c, "created_at", "") or ""
            lines.append(f"> _{author}_ ({when})")
            lines.append(f"> {body[:max_comment_chars]}")

    mrs = _get(issue, "merge_requests") or []
    if mrs:
        lines.append("")
        lines.append(f"**Merge requests ({len(mrs)}):**")
        for m in list(mrs)[:max_merge_requests]:
            state = _get(m, "state", "?") or "?"
            title = _get(m, "title", "") or ""
            url = _get(m, "url", "") or ""
            lines.append(f"- [{state}] {title} — {url}")

    return "\n".join(lines)


def format_related_tasks_for_prompt(
    hits: list[HitLike],
    *,
    max_per_hit_chars: int = 500,
) -> str:
    """Compact Markdown rendering of a list of search hits."""
    if not hits:
        return ""

    lines: list[str] = [
        "## Related Jira tasks (semantic match — may be relevant to this failure)"
    ]
    for h in hits:
        ctx = _get(h, "context") or {}
        summary = _get(ctx, "summary") or _get(h, "summary", "")
        status = _get(ctx, "status", "")
        score = _get(h, "score", 0.0)
        source = _get(h, "match_source", "")
        key = _get(h, "issue_key", "?")

        lines.append("")
        lines.append(
            f"### {key} [{status}] score={float(score):.2f} via={source}"
        )
        lines.append(f"**{summary}**")

        desc = (_get(ctx, "description_text") or _get(h, "match_preview") or "").strip()
        if desc:
            lines.append(desc[:max_per_hit_chars])

    return "\n".join(lines)
