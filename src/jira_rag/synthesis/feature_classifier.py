"""Classify a user query against the controlled feature vocabulary.

Haiku picks 0-3 feature tags from the list in jira.features. Output is used
to build a pre-filter: the retrieval candidate set is restricted to issues
tagged with any of these features BEFORE semantic search runs.

If the classifier returns empty list → no feature filter applied (semantic
retrieval runs over all issues as before).
"""

from __future__ import annotations

from typing import Iterable

import anthropic

from jira_rag.utils.logging import get_logger

logger = get_logger(__name__)

_MODEL = "claude-haiku-4-5"


_SYSTEM_TEMPLATE = """You are a query classifier for a Jira knowledge base.

Your job: given a user's natural-language question about a product feature, pick 0-3 feature tags from the CONTROLLED TAXONOMY below that name the feature(s) the question is about.

Rules:
- Use category names EXACTLY as listed — don't paraphrase, don't lowercase, don't translate.
- Pick the MOST SPECIFIC category that fits. If the question is about a concrete flow ("email unsubscribe"), prefer a narrow feature name over a broad one.
- Pick MULTIPLE tags ONLY when the question genuinely spans several features.
- If the question is ambiguous, too general ("how do things work"), or names no feature at all, return an empty list.
- Never invent category names. Never output "Other".

Controlled taxonomy:
{taxonomy_block}"""


_TOOL = {
    "name": "submit_features",
    "description": "Submit the feature tags that match the user's question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "features": {
                "type": "array",
                "description": "Zero to three feature names from the controlled list. Empty if none fit.",
                "items": {"type": "string", "minLength": 2, "maxLength": 40},
                "maxItems": 3,
            },
        },
        "required": ["features"],
        "additionalProperties": False,
    },
}


def build_system_prompt(features: Iterable[dict]) -> str:
    lines = [f"• {f['name']} — {f.get('description', '')}" for f in features]
    return _SYSTEM_TEMPLATE.format(taxonomy_block="\n".join(lines))


def classify_query_features(
    client: anthropic.Anthropic,
    question: str,
    features: list[dict],
    valid_names: set[str],
) -> list[str]:
    """Return the subset of feature names that match the user's question.

    Safe fallback: on any error or schema violation → return [] so the
    caller falls back to unfiltered semantic retrieval.
    """
    if not question or not features:
        return []

    system_prompt = build_system_prompt(features)
    try:
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=200,
            temperature=0,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": _TOOL["name"]},
            messages=[{"role": "user", "content": f"Classify this question: {question}"}],
        )
    except Exception as exc:
        logger.warning("feature_classify.failed", error=str(exc)[:200])
        return []

    for block in resp.content:
        if block.type == "tool_use" and block.name == _TOOL["name"]:
            raw = (block.input or {}).get("features") or []
            # Validate against controlled list — drop any hallucinations.
            return [f for f in raw if f in valid_names]
    return []
