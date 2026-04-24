#!/usr/bin/env python3
"""Phase 3b — tag all PID issues against the controlled feature vocabulary.

Steps:
  1. Load taxonomy.yaml into jira.features (idempotent)
  2. For each untagged issue, call Haiku with summary+description excerpt
     and force tool-use to pick 1-3 categories from the controlled list
  3. Insert rows in jira.issue_features

Batching: 30 issues per Haiku call via structured output. Taxonomy lives in
system prompt → fully cached after first call (1024+ tokens of vocabulary).

Usage:
    python scripts/tag_features.py              # tag all untagged issues
    python scripts/tag_features.py --retag      # redo everything
    python scripts/tag_features.py --limit 50   # tiny dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import anthropic  # noqa: E402

from jira_rag.config import load_config  # noqa: E402
from jira_rag.database import create_db_connection  # noqa: E402


BATCH_SIZE = 25
MODEL = "claude-haiku-4-5"


TAG_SYSTEM_TEMPLATE = """You are an assistant that classifies Jira tickets into a FIXED, controlled feature taxonomy.

Output rules:
- Every ticket MUST be tagged with 1 to 3 categories from the list below.
- Use the category NAME exactly as written (including capitalisation and spaces).
- If a ticket genuinely fits nothing, tag it "Other" alone.
- Prefer the most specific category. If two categories partially apply, pick both.
- Infer from the summary, description excerpt, issue type, and labels together.
- Be consistent across similar tickets — identical patterns must receive identical tags.

The taxonomy below is the ONLY allowed set of category names:

{taxonomy_block}"""


TAG_TOOL = {
    "name": "submit_tags",
    "description": "Submit feature tags for a batch of Jira tickets.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tagged": {
                "type": "array",
                "description": "One entry per input ticket, in the same order as the input list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "pattern": "^[A-Z]+-[0-9]+$"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 3,
                        },
                    },
                    "required": ["key", "tags"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["tagged"],
        "additionalProperties": False,
    },
}


def load_taxonomy() -> list[dict]:
    path = Path(__file__).parent.parent / "taxonomy.yaml"
    return yaml.safe_load(path.read_text())["taxonomy"]


def format_taxonomy_for_prompt(tax: list[dict]) -> str:
    lines: list[str] = []
    for cat in tax:
        kws = ", ".join(cat.get("keywords", [])[:8])
        lines.append(f"• {cat['name']} — {cat['description']}  (keywords: {kws})")
    return "\n".join(lines)


def seed_features_table(db, tax: list[dict]) -> None:
    for cat in tax:
        db.execute(
            """
            INSERT INTO features(name, description, keywords)
            VALUES (%s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                keywords    = EXCLUDED.keywords
            """,
            (cat["name"], cat["description"], cat.get("keywords", [])),
        )


def fetch_untagged_issues(db, limit: int | None, retag: bool) -> list[dict]:
    where_retag = "" if retag else """
        AND NOT EXISTS (
            SELECT 1 FROM issue_features f WHERE f.issue_key = i.key
        )"""
    q = f"""
        SELECT key, summary, description_text, checklist_text,
               issue_type, status, labels, components
        FROM issues i
        WHERE project_key = 'PID'
          AND summary <> ''
          {where_retag}
        ORDER BY updated_at DESC
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    return db.execute(q)


def format_issue_for_prompt(issue: dict) -> str:
    summary = (issue.get("summary") or "").strip()[:180]
    desc = (issue.get("description_text") or "").strip()
    desc_short = desc[:400] + ("…" if len(desc) > 400 else "")
    checklist_head = (issue.get("checklist_text") or "").strip().split("\n", 2)
    checklist_preview = " / ".join(x.strip() for x in checklist_head[:2])[:200]
    meta_bits = [
        f"type={issue.get('issue_type','')}",
        f"status={issue.get('status','')}",
    ]
    if issue.get("labels"):
        meta_bits.append("labels=" + ",".join(list(issue["labels"])[:6]))
    if issue.get("components"):
        meta_bits.append("components=" + ",".join(list(issue["components"])[:3]))
    parts = [
        f"[{issue['key']}]  {summary}",
        "  " + "  ".join(meta_bits),
    ]
    if desc_short:
        parts.append(f"  desc: {desc_short}")
    if checklist_preview:
        parts.append(f"  checklist: {checklist_preview}")
    return "\n".join(parts)


def tag_batch(
    client: anthropic.Anthropic,
    system_prompt: str,
    batch: list[dict],
    valid_categories: set[str],
) -> list[dict]:
    user_msg = (
        f"Tag the {len(batch)} tickets below. Return the same number of entries in "
        "the `tagged` array, preserving order.\n\n"
        + "\n\n".join(format_issue_for_prompt(i) for i in batch)
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        temperature=0,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[TAG_TOOL],
        tool_choice={"type": "tool", "name": TAG_TOOL["name"]},
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == TAG_TOOL["name"]:
            raw = block.input.get("tagged") or []
            # validate
            out: list[dict] = []
            for entry in raw:
                key = entry.get("key")
                tags = [t for t in (entry.get("tags") or []) if t in valid_categories]
                if not tags:
                    tags = ["Other"]
                out.append({"key": key, "tags": tags})
            return out, resp.usage
    return [], resp.usage


def insert_tags(db, tagged: list[dict]) -> int:
    n = 0
    for entry in tagged:
        key = entry["key"]
        for t in entry["tags"]:
            try:
                db.execute(
                    """
                    INSERT INTO issue_features(issue_key, feature, source, confidence)
                    VALUES (%s, %s, 'llm', 1.0)
                    ON CONFLICT (issue_key, feature) DO NOTHING
                    """,
                    (key, t),
                )
                n += 1
            except Exception as exc:
                print(f"  ! insert failed {key} / {t}: {exc}", file=sys.stderr)
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--retag", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY missing", file=sys.stderr); sys.exit(2)

    cfg = load_config("config.yaml")
    db = create_db_connection(cfg.supabase)
    tax = load_taxonomy()
    valid_names = {c["name"] for c in tax}
    seed_features_table(db, tax)
    print(f"Taxonomy: {len(tax)} categories seeded", file=sys.stderr)

    if args.retag:
        db.execute("DELETE FROM issue_features WHERE source = 'llm'")

    issues = fetch_untagged_issues(db, args.limit, args.retag)
    print(f"Issues to tag: {len(issues)}", file=sys.stderr)
    if not issues:
        print("Nothing to do.", file=sys.stderr); return

    system_prompt = TAG_SYSTEM_TEMPLATE.format(
        taxonomy_block=format_taxonomy_for_prompt(tax)
    )
    client = anthropic.Anthropic(api_key=api_key)

    t0 = time.monotonic()
    total_tagged, total_input, total_output, total_cache_read = 0, 0, 0, 0
    batch_count = 0
    for i in range(0, len(issues), BATCH_SIZE):
        batch = issues[i:i + BATCH_SIZE]
        try:
            tagged, usage = tag_batch(client, system_prompt, batch, valid_names)
        except anthropic.BadRequestError as e:
            print(f"  ! batch {i // BATCH_SIZE}: {e}", file=sys.stderr); continue
        inserted = insert_tags(db, tagged)
        total_tagged += inserted
        total_input += usage.input_tokens
        total_output += usage.output_tokens
        total_cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        batch_count += 1
        if batch_count % 5 == 0 or i + BATCH_SIZE >= len(issues):
            elapsed = time.monotonic() - t0
            done = min(i + BATCH_SIZE, len(issues))
            rate = done / elapsed if elapsed else 0
            eta = (len(issues) - done) / rate if rate else 0
            print(
                f"  progress {done}/{len(issues)}  ({rate:.1f} issues/s)  "
                f"eta={eta:.0f}s  tags_written={total_tagged}  "
                f"cache_read={total_cache_read}t",
                file=sys.stderr,
            )

    elapsed = time.monotonic() - t0
    print(file=sys.stderr)
    print(f"Done in {elapsed:.1f}s", file=sys.stderr)
    print(f"  batches            : {batch_count}", file=sys.stderr)
    print(f"  tags inserted      : {total_tagged}", file=sys.stderr)
    print(f"  input tokens total : {total_input}", file=sys.stderr)
    print(f"  cache read tokens  : {total_cache_read}", file=sys.stderr)
    print(f"  output tokens total: {total_output}", file=sys.stderr)

    # Quick coverage report
    counts = db.execute("""
        SELECT feature, count(*) AS n
        FROM issue_features
        GROUP BY feature
        ORDER BY n DESC
    """)
    print(file=sys.stderr)
    print("Coverage per feature (top 15):", file=sys.stderr)
    for r in counts[:15]:
        print(f"  {r['n']:5}  {r['feature']}", file=sys.stderr)


if __name__ == "__main__":
    main()
