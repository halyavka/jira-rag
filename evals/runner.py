#!/usr/bin/env python3
"""Eval runner for jira-rag retrieval and answer quality.

Usage:
    python evals/runner.py --endpoint search --out evals/results/baseline.json
    python evals/runner.py --endpoint search --top-k 10
    python evals/runner.py --compare evals/results/baseline.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

QUESTIONS_PATH = Path(__file__).parent / "reference_questions.yaml"
DEFAULT_SERVER = "http://localhost:8100"


@dataclass
class QuestionResult:
    id: str
    kind: str
    question: str
    expected_must: list[str]
    expected_should: list[str]
    retrieved: list[dict]   # [{key, score, source, rank}]
    recall_at_5: float
    recall_at_10: float
    precision_at_5: float
    mrr: float
    latency_ms: float
    first_match_rank: int | None  # 1-based; None if no must-have appeared


@dataclass
class SuiteResult:
    endpoint: str
    server: str
    top_k: int
    min_score: float
    total_questions: int
    avg_recall_at_5: float
    avg_recall_at_10: float
    avg_precision_at_5: float
    avg_mrr: float
    avg_latency_ms: float
    by_kind: dict[str, dict[str, float]]
    per_question: list[QuestionResult]


# ── retrieval adapters ───────────────────────────────────────────────────────
def retrieve_search(question: str, project: str, server: str, top_k: int, min_score: float) -> list[dict]:
    params = {
        "q": question,
        "project": project,
        "top_k": top_k,
        "min_score": min_score,
        "include_comments": "true",
        "include_merge_requests": "false",
    }
    url = f"{server}/search?{urllib.parse.urlencode(params, doseq=True)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    return [
        {
            "key": h["issue_key"],
            "score": float(h.get("score") or 0.0),
            "source": h.get("match_source", "?"),
            "summary": h.get("summary", "")[:80],
        }
        for h in (data.get("hits") or [])
    ]


def retrieve_ask(question: str, project: str, server: str, top_k: int, min_score: float) -> list[dict]:
    """Call /ask and measure recall against `retrieved_keys` (what RAG sent
    into synthesis). `sources` (what Claude cited) is a tighter metric but
    also depends on model citation behaviour, which we track separately
    below when `--include-sources` is set."""
    url = f"{server}/ask"
    body = json.dumps({"q": question, "project": [project]}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=420) as resp:
        data = json.loads(resp.read())
    if "retrieved_keys" not in data:
        raise RuntimeError(f"/ask response missing retrieved_keys: {list(data.keys())}")
    return [
        {"key": k, "score": 1.0 - i * 0.01, "source": "ask", "summary": ""}
        for i, k in enumerate(data["retrieved_keys"])
    ]


RETRIEVERS = {"search": retrieve_search, "ask": retrieve_ask}


# ── metrics ──────────────────────────────────────────────────────────────────
def compute_metrics(retrieved: list[dict], must: set[str], should: set[str]) -> dict[str, float]:
    keys_in_order = [r["key"] for r in retrieved]

    top5 = set(keys_in_order[:5])
    top10 = set(keys_in_order[:10])

    recall_at_5 = len(top5 & must) / len(must) if must else 0.0
    recall_at_10 = len(top10 & must) / len(must) if must else 0.0

    relevant = must | should
    precision_at_5 = len(top5 & relevant) / 5 if top5 else 0.0

    mrr = 0.0
    first_rank: int | None = None
    for i, k in enumerate(keys_in_order, 1):
        if k in must:
            mrr = 1.0 / i
            first_rank = i
            break

    return {
        "recall_at_5": round(recall_at_5, 3),
        "recall_at_10": round(recall_at_10, 3),
        "precision_at_5": round(precision_at_5, 3),
        "mrr": round(mrr, 3),
        "first_match_rank": first_rank,
    }


# ── suite runner ─────────────────────────────────────────────────────────────
def run_suite(
    endpoint: str,
    server: str,
    top_k: int,
    min_score: float,
) -> SuiteResult:
    questions = yaml.safe_load(QUESTIONS_PATH.read_text())["questions"]
    retriever = RETRIEVERS[endpoint]

    per_q: list[QuestionResult] = []
    for q in questions:
        must = set(q["must_have"])
        should = set(q.get("should_have") or [])

        t0 = time.monotonic()
        retrieved = retriever(q["question"], q["project"], server, top_k, min_score)
        latency_ms = (time.monotonic() - t0) * 1000.0

        retrieved_with_rank = [
            {**r, "rank": i + 1} for i, r in enumerate(retrieved)
        ]
        m = compute_metrics(retrieved, must, should)

        per_q.append(QuestionResult(
            id=q["id"],
            kind=q["kind"],
            question=q["question"],
            expected_must=sorted(must),
            expected_should=sorted(should),
            retrieved=retrieved_with_rank[:top_k],
            recall_at_5=m["recall_at_5"],
            recall_at_10=m["recall_at_10"],
            precision_at_5=m["precision_at_5"],
            mrr=m["mrr"],
            latency_ms=round(latency_ms, 1),
            first_match_rank=m["first_match_rank"],
        ))

    # aggregates
    def avg(attr: str) -> float:
        vals = [getattr(r, attr) for r in per_q]
        return round(sum(vals) / len(vals), 3) if vals else 0.0

    by_kind: dict[str, dict[str, float]] = {}
    kinds = sorted({r.kind for r in per_q})
    for k in kinds:
        subset = [r for r in per_q if r.kind == k]
        by_kind[k] = {
            "n": len(subset),
            "recall_at_5": round(sum(r.recall_at_5 for r in subset) / len(subset), 3),
            "recall_at_10": round(sum(r.recall_at_10 for r in subset) / len(subset), 3),
            "precision_at_5": round(sum(r.precision_at_5 for r in subset) / len(subset), 3),
            "mrr": round(sum(r.mrr for r in subset) / len(subset), 3),
        }

    return SuiteResult(
        endpoint=endpoint,
        server=server,
        top_k=top_k,
        min_score=min_score,
        total_questions=len(per_q),
        avg_recall_at_5=avg("recall_at_5"),
        avg_recall_at_10=avg("recall_at_10"),
        avg_precision_at_5=avg("precision_at_5"),
        avg_mrr=avg("mrr"),
        avg_latency_ms=avg("latency_ms"),
        by_kind=by_kind,
        per_question=per_q,
    )


# ── pretty print ─────────────────────────────────────────────────────────────
def print_suite(suite: SuiteResult) -> None:
    print(f"\n{'='*66}")
    print(f"eval: endpoint={suite.endpoint} top_k={suite.top_k} min_score={suite.min_score}")
    print(f"{'='*66}")
    print(f"  avg recall@5   : {suite.avg_recall_at_5:.3f}")
    print(f"  avg recall@10  : {suite.avg_recall_at_10:.3f}")
    print(f"  avg precision@5: {suite.avg_precision_at_5:.3f}")
    print(f"  avg MRR        : {suite.avg_mrr:.3f}")
    print(f"  avg latency    : {suite.avg_latency_ms:.0f} ms")
    print(f"\n  by kind:")
    for kind, stats in suite.by_kind.items():
        print(f"    {kind:10} n={stats['n']:2}  "
              f"R@5={stats['recall_at_5']:.2f}  R@10={stats['recall_at_10']:.2f}  "
              f"P@5={stats['precision_at_5']:.2f}  MRR={stats['mrr']:.2f}")

    print(f"\n  per question:")
    for r in suite.per_question:
        marker = "✓" if r.recall_at_5 >= 1.0 else ("~" if r.recall_at_5 > 0 else "✗")
        rank_str = f"rank={r.first_match_rank}" if r.first_match_rank else "no-match"
        print(f"    {marker} [{r.kind:7}] {r.id:30} "
              f"R@5={r.recall_at_5:.2f} R@10={r.recall_at_10:.2f} "
              f"MRR={r.mrr:.2f}  {rank_str}")


def save_suite(suite: SuiteResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialisable = asdict(suite)
    path.write_text(json.dumps(serialisable, ensure_ascii=False, indent=2))
    print(f"\n  → saved to {path}")


# ── compare two runs ─────────────────────────────────────────────────────────
def compare(baseline_path: Path, current: SuiteResult) -> None:
    baseline = json.loads(baseline_path.read_text())
    print(f"\n{'─'*66}")
    print(f"delta vs baseline ({baseline_path.name})")
    print(f"{'─'*66}")
    metrics = ["avg_recall_at_5", "avg_recall_at_10", "avg_precision_at_5", "avg_mrr"]
    for m in metrics:
        b = baseline[m]
        c = getattr(current, m)
        delta = c - b
        arrow = "↑" if delta > 0.005 else ("↓" if delta < -0.005 else "=")
        print(f"  {m:22} {b:.3f} → {c:.3f}  {arrow} {delta:+.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", choices=list(RETRIEVERS), default="search")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--min-score", type=float, default=0.3)
    ap.add_argument("--out", type=Path, help="Save full result JSON here")
    ap.add_argument("--compare", type=Path, help="Diff against a saved baseline")
    args = ap.parse_args()

    suite = run_suite(args.endpoint, args.server, args.top_k, args.min_score)
    print_suite(suite)

    if args.out:
        save_suite(suite, args.out)
    if args.compare:
        compare(args.compare, suite)


if __name__ == "__main__":
    main()
