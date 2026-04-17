"""Text normalisation helpers."""

from __future__ import annotations

import re

_ADF_NODE_TYPES_WITH_TEXT = {"text"}


def adf_to_text(node: dict | list | str | None) -> str:
    """Flatten Atlassian Document Format (ADF) to plain text.

    Jira Cloud returns descriptions/comments as ADF JSON. This walks the tree
    and pulls out text content. Good enough for embedding — we don't need
    layout fidelity.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(adf_to_text(child) for child in node).strip()
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    text_parts: list[str] = []

    if node_type in _ADF_NODE_TYPES_WITH_TEXT and "text" in node:
        text_parts.append(node["text"])

    for child in node.get("content", []) or []:
        text_parts.append(adf_to_text(child))

    joined = "".join(text_parts)
    # Add paragraph / list breaks for readability.
    if node_type in {"paragraph", "heading", "listItem", "blockquote", "codeBlock"}:
        joined += "\n"
    return joined


def normalise_text(text: str, max_chars: int = 12000) -> str:
    """Collapse whitespace and cap length (embedding models have token limits)."""
    if not text:
        return ""
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text
