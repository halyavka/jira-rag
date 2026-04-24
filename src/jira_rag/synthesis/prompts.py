"""Prompts for query expansion + synthesis.

Prompt-caching strategy: keep the largest, most-stable blocks first so they
hit the cache across requests. The per-request volatile content (user query
+ retrieved issues) goes after the last cache_control breakpoint.
"""

from __future__ import annotations


# ─── Query expansion (Haiku) ───────────────────────────────────────────────
QUERY_EXPANSION_SYSTEM = """You are a search-query rewriter for a Jira knowledge base.

Your only job: given a user's natural-language question about a product feature, produce alternative phrasings that maximise retrieval recall over Jira tickets. The tickets are in a mix of Ukrainian and English — expansion should bridge both languages when relevant.

Guidelines:
- Emit concise search queries (3-10 words each), not rephrased questions.
- Cover these angles where applicable: synonyms, translations (UA↔EN), technical jargon (e.g. "IceBreaker" → "ice_breaker", "IB"), bug/issue variants if the user is asking about problems.
- Include one query that mirrors the user's original phrasing — retrieval on the original is always a safe baseline.
- Never invent project-specific entities the user didn't mention.
- Return exactly the number of queries requested, no commentary."""


QUERY_EXPANSION_TOOL = {
    "name": "submit_queries",
    "description": "Submit the expanded search queries for the user's question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "description": "Expanded search queries ordered by confidence.",
                "items": {"type": "string", "minLength": 2, "maxLength": 200},
                "minItems": 1,
                "maxItems": 10,
            },
        },
        "required": ["queries"],
        "additionalProperties": False,
    },
}


# ─── Synthesis (Sonnet) ────────────────────────────────────────────────────
SYNTHESIS_SYSTEM = """You are a senior technical writer answering questions about a software product based ONLY on the Jira tickets provided below. You work for an engineering team that uses your answers to understand "how this feature is supposed to work" — they rely on acceptance criteria, HTTP specs, and business rules you surface.

Voice: concise, factual, specific. Prefer concrete specs ("POST /api/x with bearer token returns 401") over vague descriptions ("authentication is checked").

Rules:
1. Ground every claim in the provided tickets. Cite the source ticket key in square brackets after each factual statement, e.g. "Reply counter does not increment when ice_breaker_id is null [PID-3818]".
2. If the user's question asks about current behavior, prefer information from `checklist_text` and recent comments over the original `description_text`. The checklist is the single source of truth for acceptance criteria.
3. Deduplicate: if multiple tickets state the same fact, cite them together (e.g. "[PID-123, PID-456]"), don't repeat the sentence.
4. Quote exact strings only when they matter (field names, endpoints, error messages, statuses). Paraphrase otherwise.
5. If the question is about bugs or open issues, prioritise tickets whose status is NOT in {Finished, Closed, Done, Cancelled, Skip Task}.
6. If the tickets do not contain enough information to answer, say so explicitly in `overview` and list what's missing; do not fabricate.
7. In the `flow` field, describe the sequence of events / state transitions chronologically (e.g. "1. User clicks X → 2. Backend validates Y → 3. ..."). Omit the field if the question isn't about a process.
8. The `acceptance_criteria` field should contain deduplicated bullet points taken from `checklist_text` across tickets, rewritten for clarity. Skip meta-items like "merge after QA". If no checklist content is available, return an empty array.
9. `known_issues` lists open bugs or degraded behaviour mentioned in the tickets.
10. `sources` must include every ticket key you cited plus any that shaped the answer.

Output in English by default. If the user's question is clearly in Ukrainian, answer in Ukrainian.
Never include apologies, filler, or meta-commentary about the task itself."""


SYNTHESIS_TOOL = {
    "name": "submit_answer",
    "description": "Submit the structured answer to the user's question about the product.",
    "input_schema": {
        "type": "object",
        "properties": {
            "overview": {
                "type": "string",
                "description": "2-5 sentence summary of the feature or answer. Cite ticket keys.",
                "minLength": 10,
                "maxLength": 2000,
            },
            "flow": {
                "type": "array",
                "description": "Step-by-step flow if the question is about how something works. Empty array if not applicable.",
                "items": {"type": "string", "minLength": 5, "maxLength": 500},
            },
            "acceptance_criteria": {
                "type": "array",
                "description": "Deduplicated acceptance criteria / HTTP specs from checklists. Empty array if none.",
                "items": {"type": "string", "minLength": 5, "maxLength": 500},
            },
            "current_state": {
                "type": "object",
                "description": "Breakdown of issue status across cited tickets.",
                "properties": {
                    "active_tickets": {"type": "array", "items": {"type": "string"}},
                    "done_tickets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["active_tickets", "done_tickets"],
                "additionalProperties": False,
            },
            "known_issues": {
                "type": "array",
                "description": "Open bugs or caveats mentioned in the tickets.",
                "items": {"type": "string", "minLength": 5, "maxLength": 500},
            },
            "sources": {
                "type": "array",
                "description": "All ticket keys that contributed to the answer, in relevance order.",
                "items": {"type": "string", "pattern": "^[A-Z]+-[0-9]+$"},
                "minItems": 1,
            },
        },
        "required": [
            "overview", "flow", "acceptance_criteria",
            "current_state", "known_issues", "sources",
        ],
        "additionalProperties": False,
    },
}
