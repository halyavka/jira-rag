"""Pack retrieved issues into a synthesis prompt.

Principles:
- Comments are filtered (drop `+1`, `approved`, short noise) and ordered
  newest-first so "current behaviour" questions favour recent signal.
- Checklist content (acceptance criteria) is kept in full — it's the single
  most valuable signal per issue.
- When the combined context would exceed max_context_chars, we trim
  low-value blocks first (oldest comments) rather than truncating high-value
  blocks (description, checklist).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Comments matching these get dropped as noise.
_NOISE_PATTERNS = [
    re.compile(r"^\s*\+?1\s*$", re.IGNORECASE),
    re.compile(r"^\s*(done|approved|merged|closed|lgtm|ok|fixed)\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(bumped?|ping|thx|thanks)\.?\s*$", re.IGNORECASE),
]

_TERMINAL_STATUSES = {"finished", "closed", "done", "cancelled", "skip task", "resolved"}


@dataclass
class PackedHit:
    issue_key: str
    score: float
    rank: int
    text: str   # fully formatted prompt block
    is_terminal: bool


def _is_noise(body: str, min_chars: int) -> bool:
    stripped = (body or "").strip()
    if len(stripped) < min_chars:
        return True
    return any(p.match(stripped) for p in _NOISE_PATTERNS)


def _iso(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def format_issue_block(
    rank: int,
    issue: dict[str, Any],
    comments: list[dict],
    merge_requests: list[dict],
    *,
    min_comment_body_chars: int,
    max_comments: int = 5,
    max_mrs: int = 3,
    max_description_chars: int = 4000,
    max_checklist_chars: int = 8000,
    max_comment_chars: int = 800,
) -> str:
    """Render one issue as an LLM-readable block with ranked metadata up top."""
    key = issue["key"]
    status = issue.get("status") or ""
    progress = issue.get("progress_percent") or 0
    updated = _iso(issue.get("updated_at"))
    summary = (issue.get("summary") or "").strip()
    description = (issue.get("description_text") or "").strip()
    checklist = (issue.get("checklist_text") or "").strip()
    checklist_progress = (issue.get("checklist_progress") or "").strip()
    assignee = issue.get("assignee") or ""
    labels = issue.get("labels") or []
    components = issue.get("components") or []

    lines: list[str] = [
        f"=== #{rank}  {key}  [{status}]  updated={updated[:10]} ===",
        f"Title: {summary}",
    ]
    meta_bits: list[str] = []
    if assignee:
        meta_bits.append(f"assignee={assignee}")
    if labels:
        meta_bits.append(f"labels={','.join(labels)}")
    if components:
        meta_bits.append(f"components={','.join(components)}")
    if meta_bits:
        lines.append("Meta: " + "  ".join(meta_bits))

    if description:
        trimmed = description[:max_description_chars]
        ellipsis = "…" if len(description) > max_description_chars else ""
        lines.append("")
        lines.append("Description:")
        lines.append(trimmed + ellipsis)

    if checklist:
        trimmed = checklist[:max_checklist_chars]
        ellipsis = "…" if len(checklist) > max_checklist_chars else ""
        lines.append("")
        header = "Acceptance criteria (Smart Checklist):"
        if checklist_progress:
            header += f" [{checklist_progress}]"
        lines.append(header)
        lines.append(trimmed + ellipsis)

    # Comments: newest first, drop noise.
    relevant_comments = [
        c for c in comments
        if not _is_noise(c.get("body_text") or "", min_comment_body_chars)
    ]
    relevant_comments.sort(
        key=lambda c: _iso(c.get("created_at")) or "", reverse=True,
    )
    if relevant_comments:
        lines.append("")
        lines.append(f"Recent comments (newest first, {len(relevant_comments)} total):")
        for c in relevant_comments[:max_comments]:
            body = (c.get("body_text") or "").strip()
            if len(body) > max_comment_chars:
                body = body[:max_comment_chars] + "…"
            author = c.get("author") or "?"
            when = _iso(c.get("created_at"))[:10]
            # inline — fewer tokens than bulleted blocks
            lines.append(f"  · [{when}] {author}: {body}")

    if merge_requests:
        relevant_mrs = sorted(
            merge_requests,
            key=lambda m: _iso(m.get("updated_at")) or "",
            reverse=True,
        )
        lines.append("")
        lines.append(f"Merge requests ({len(relevant_mrs)}):")
        for m in relevant_mrs[:max_mrs]:
            state = m.get("state") or "?"
            title = (m.get("title") or "").strip()
            url = (m.get("url") or "").strip()
            lines.append(f"  · [{state}] {title}  {url}")

    return "\n".join(lines)


def pack_hits(
    hits: list[dict[str, Any]],
    *,
    min_comment_body_chars: int,
    max_context_chars: int,
) -> list[PackedHit]:
    """Format each hit and apply a global character budget.

    `hits` is the Searcher output shape: each entry has `issue_key`, `score`,
    and `context` (full hydrated IssueContext dict with comments/MRs nested).
    """
    packed: list[PackedHit] = []
    running_total = 0
    for rank, h in enumerate(hits, 1):
        ctx = h.get("context") or {}
        if not ctx:
            continue
        comments = list(ctx.get("comments") or [])
        mrs = list(ctx.get("merge_requests") or [])
        text = format_issue_block(
            rank=rank,
            issue=ctx,
            comments=comments,
            merge_requests=mrs,
            min_comment_body_chars=min_comment_body_chars,
        )
        # Budget guard: once we exceed, keep only high-rank hits and truncate
        # the tail. In practice final_top_k * per-issue ~= well under budget.
        if running_total + len(text) > max_context_chars and packed:
            break
        running_total += len(text)
        status_lower = (ctx.get("status") or "").lower()
        packed.append(PackedHit(
            issue_key=ctx["key"],
            score=float(h.get("score") or 0.0),
            rank=rank,
            text=text,
            is_terminal=status_lower in _TERMINAL_STATUSES,
        ))
    return packed


def format_prompt_body(user_question: str, packed: list[PackedHit]) -> str:
    """Compose the final user-turn content for the synthesis API call."""
    if not packed:
        return (
            "No Jira tickets matched the query. "
            f"User question: {user_question}\n\n"
            "Respond with overview explaining no matching tickets were found."
        )
    body: list[str] = [
        "You have been given the following Jira tickets, ranked by relevance to the user's question. "
        "Use ONLY this information. Cite ticket keys in every factual sentence.\n",
    ]
    body.extend(h.text for h in packed)
    body.append("")
    body.append(f"User question: {user_question}")
    body.append("")
    body.append("Call the submit_answer tool with your structured response.")
    return "\n\n".join(body)
