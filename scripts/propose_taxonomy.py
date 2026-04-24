#!/usr/bin/env python3
"""Phase 3b.1 — Use Claude to propose a controlled feature taxonomy.

Samples Jira ticket summaries + top epic names and asks Claude to cluster
them into 50-80 stable feature categories. Outputs YAML for human review.

Usage:
    python scripts/propose_taxonomy.py > taxonomy.proposal.yaml
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import anthropic  # noqa: E402

from jira_rag.config import load_config  # noqa: E402
from jira_rag.database import create_db_connection  # noqa: E402


SAMPLE_SIZE = 300   # random ticket summaries to cluster
TOP_EPICS = 30      # plus top-N epic names as strong signals


SYSTEM = """You are a senior product taxonomist. Given a list of Jira ticket titles from one product,
build a controlled vocabulary of FEATURE-level categories that can be used to classify every ticket
into 1-3 tags.

Rules:
1. Categories must be FEATURE-level (what the user / system does), not process-level.
   YES: "Email unsubscribe", "Icebreakers", "Payments", "Onboarding", "Push notifications".
   NO:  "Bug", "QA", "Backend work", "Analytics request" — those are processes/roles, not features.
2. 40-80 categories total. Fewer is better if it still covers ≥85% of tickets distinctly.
3. Each category name must be 2-4 words, title-case English, no acronyms unless universally known (MR, API).
4. Avoid overlap. "Chat messages" and "Chat UI" → collapse to "Chat". Broad umbrella beats fine-grained.
5. Include exactly ONE "Other" category as the fallback for tickets that don't fit anywhere else.

Output is consumed by a deterministic tagger downstream — be concise and stable."""


TOOL = {
    "name": "submit_taxonomy",
    "description": "Submit the controlled feature taxonomy proposal.",
    "input_schema": {
        "type": "object",
        "properties": {
            "categories": {
                "type": "array",
                "description": "Ordered list of feature categories.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short title-case English name (2-4 words).",
                            "minLength": 2,
                            "maxLength": 40,
                        },
                        "description": {
                            "type": "string",
                            "description": "One-sentence definition to guide taggers.",
                            "minLength": 10,
                            "maxLength": 200,
                        },
                        "keywords": {
                            "type": "array",
                            "description": "5-10 representative keywords/phrases (UA+EN mix).",
                            "items": {"type": "string", "minLength": 2, "maxLength": 40},
                            "minItems": 3,
                            "maxItems": 15,
                        },
                        "example_keys": {
                            "type": "array",
                            "description": "2-4 ticket keys from the input that clearly fit this category.",
                            "items": {"type": "string", "pattern": "^[A-Z]+-[0-9]+$"},
                            "maxItems": 5,
                        },
                    },
                    "required": ["name", "description", "keywords"],
                    "additionalProperties": False,
                },
                "minItems": 20,
                "maxItems": 100,
            },
        },
        "required": ["categories"],
        "additionalProperties": False,
    },
}


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY missing", file=sys.stderr)
        sys.exit(2)

    cfg = load_config("config.yaml")
    db = create_db_connection(cfg.supabase)

    # Sample titles — stratified by status_category so we capture both active and historic tickets.
    rows = db.execute("""
        SELECT key, summary, status_category, epic_key
        FROM issues
        WHERE project_key = 'PID'
          AND summary <> ''
          AND key NOT IN (
              SELECT epic_key FROM issues WHERE epic_key IS NOT NULL AND epic_key <> ''
              GROUP BY epic_key HAVING count(*) >= 5
          )  -- skip meta-epics like 'Bugs archive'
    """)
    random.seed(42)
    sample = random.sample(rows, min(SAMPLE_SIZE, len(rows)))

    # Top epics by ticket count (excluding process-level ones by keyword).
    epics = db.execute("""
        SELECT e.key AS epic_key, e.summary
        FROM (
            SELECT epic_key, count(*) AS n
            FROM issues WHERE project_key='PID' AND epic_key IS NOT NULL AND epic_key <> ''
            GROUP BY epic_key
            ORDER BY n DESC LIMIT %s
        ) c
        JOIN issues e ON e.key = c.epic_key
        WHERE lower(coalesce(e.summary,'')) NOT LIKE '%%bug%%archive%%'
          AND lower(coalesce(e.summary,'')) NOT LIKE '%%bugs storage%%'
          AND lower(coalesce(e.summary,'')) NOT LIKE '%%tracking%%'
    """, (TOP_EPICS,))

    # Build prompt payload
    sample_text = "\n".join(f"{r['key']}: {r['summary'][:140]}" for r in sample)
    epic_text = "\n".join(f"{r['epic_key']}: {r['summary'][:140]}" for r in epics if r.get("summary"))

    user_msg = (
        f"Below are {len(sample)} randomly sampled Jira ticket titles from the 'PID' product "
        f"plus {len(epics)} top-level epic names (feature-level groupings already in Jira). "
        "Cluster them into a feature taxonomy per the rules in the system prompt.\n\n"
        f"=== Random ticket sample ({len(sample)}) ===\n{sample_text}\n\n"
        f"=== Top epics ({len(epics)}) ===\n{epic_text}\n\n"
        "Call submit_taxonomy with your proposal."
    )

    client = anthropic.Anthropic(api_key=api_key)
    # NB: adaptive thinking cannot be combined with tool_choice-forced tool
    # use on Opus 4.7 (returns 400). Keep the forced tool schema, drop thinking.
    resp = client.messages.create(
        model="claude-opus-4-7",   # taxonomy is one-off, worth Opus quality
        max_tokens=16000,
        output_config={"effort": "high"},
        system=[{
            "type": "text", "text": SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[TOOL],
        tool_choice={"type": "tool", "name": TOOL["name"]},
        messages=[{"role": "user", "content": user_msg}],
    )

    print(f"# Generated by propose_taxonomy.py", file=sys.stderr)
    print(f"# Sample size: {len(sample)} tickets + {len(epics)} epics", file=sys.stderr)
    print(f"# Model: claude-opus-4-7 (adaptive thinking, effort=high)", file=sys.stderr)
    print(f"# Tokens: input={resp.usage.input_tokens} output={resp.usage.output_tokens}",
          file=sys.stderr)

    for block in resp.content:
        if block.type == "tool_use" and block.name == TOOL["name"]:
            categories = block.input.get("categories", [])
            # Output as YAML for manual review
            print(f"# {len(categories)} categories proposed")
            print("taxonomy:")
            for cat in categories:
                print(f"  - name: {json.dumps(cat['name'])}")
                print(f"    description: {json.dumps(cat['description'])}")
                kws = ", ".join(json.dumps(k) for k in cat.get("keywords", []))
                print(f"    keywords: [{kws}]")
                if cat.get("example_keys"):
                    print(f"    examples: {json.dumps(cat['example_keys'])}")
            break


if __name__ == "__main__":
    main()
