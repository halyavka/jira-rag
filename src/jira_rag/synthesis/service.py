"""Anthropic-powered /ask pipeline: query expansion → RRF retrieval → synthesis.

Prompt-caching policy
---------------------
Both calls (expansion + synthesis) put the large static system prompt first
with `cache_control: ephemeral`. Per-request volatile content (the user's
question and the retrieved tickets) goes after the breakpoint. On a warm
cache we pay ~0.1× for the system prefix and full price only on the tail.

Structured output
-----------------
We use tool-use (with `tool_choice = {type: "tool", name: ...}`) rather than
JSON mode — it enforces the schema more reliably for this model family.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import anthropic

from jira_rag.config.schema import AppConfig, SynthesisConfig
from jira_rag.database import FeatureTagsRepo, create_db_connection
from jira_rag.search import Searcher
from jira_rag.synthesis.context_packing import (
    PackedHit,
    format_prompt_body,
    pack_hits,
)
from jira_rag.synthesis.prompts import (
    QUERY_EXPANSION_SYSTEM,
    QUERY_EXPANSION_TOOL,
    SYNTHESIS_SYSTEM,
    SYNTHESIS_TOOL,
)
from jira_rag.synthesis.feature_classifier import classify_query_features
from jira_rag.synthesis.query_intent import QueryIntent, classify
from jira_rag.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class AnswerResult:
    question: str
    project_keys: list[str]
    expanded_queries: list[str]
    intent: dict[str, bool]   # {bug_intent, open_intent}
    feature_tags: list[str]   # controlled-vocabulary tags detected in the query
    # Ranked issue keys after retrieval merge — what we sent into synthesis.
    retrieved_keys: list[str]
    # Output fields from the synthesis tool.
    overview: str
    flow: list[str]
    acceptance_criteria: list[str]
    current_state: dict[str, list[str]]
    known_issues: list[str]
    sources: list[str]
    # Diagnostics.
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "project_keys": self.project_keys,
            "expanded_queries": self.expanded_queries,
            "intent": self.intent,
            "feature_tags": self.feature_tags,
            "retrieved_keys": self.retrieved_keys,
            "answer": {
                "overview": self.overview,
                "flow": self.flow,
                "acceptance_criteria": self.acceptance_criteria,
                "current_state": self.current_state,
                "known_issues": self.known_issues,
                "sources": self.sources,
            },
            "diagnostics": {
                "cache_read_input_tokens": self.cache_read_input_tokens,
                "cache_creation_input_tokens": self.cache_creation_input_tokens,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "latency_ms": self.latency_ms,
                "warnings": self.warnings,
            },
        }


class SynthesisService:
    def __init__(
        self,
        config: SynthesisConfig,
        searcher: Searcher,
        client: anthropic.Anthropic,
        features_repo: FeatureTagsRepo | None = None,
    ) -> None:
        self._config = config
        self._searcher = searcher
        self._client = client
        self._features_repo = features_repo
        # Lazy-loaded: taxonomy is stable, so we load once per process and cache.
        self._taxonomy: list[dict] | None = None
        self._taxonomy_names: set[str] = set()

    def _taxonomy_cache(self) -> tuple[list[dict], set[str]]:
        if self._taxonomy is None and self._features_repo is not None:
            try:
                self._taxonomy = self._features_repo.all_features()
                self._taxonomy_names = {f["name"] for f in self._taxonomy}
            except Exception as exc:
                logger.warning("feature_taxonomy.load_failed", error=str(exc)[:200])
                self._taxonomy = []
                self._taxonomy_names = set()
        return self._taxonomy or [], self._taxonomy_names

    # ── public entry point ─────────────────────────────────────────────────
    def ask(
        self,
        question: str,
        project_keys: list[str] | None = None,
    ) -> AnswerResult:
        import time
        t0 = time.monotonic()
        warnings: list[str] = []

        # 1. Classify intent (heuristic) — enables structural retrieval filter
        intent = classify(question)

        # 2. Classify feature tags (LLM) — narrows retrieval to tagged issues
        feature_tags: list[str] = []
        feature_key_filter: set[str] | None = None
        taxonomy, valid_names = self._taxonomy_cache()
        if taxonomy and self._features_repo is not None:
            try:
                feature_tags = classify_query_features(
                    self._client, question, taxonomy, valid_names,
                )
                if feature_tags:
                    feature_key_filter = self._features_repo.keys_with_features(
                        feature_tags, project_keys=project_keys,
                    )
                    # Safety: if the feature-filter set is empty (tag exists
                    # but zero issues tagged with it), drop the filter — better
                    # to fall back to semantic than to return nothing.
                    if not feature_key_filter:
                        feature_key_filter = None
                        warnings.append("feature_filter_empty")
            except Exception as exc:
                logger.warning("feature_classify.failed", error=str(exc)[:200])
                warnings.append(f"feature_classify_failed: {exc}")

        # 3. Query expansion (optional)
        expanded = [question]
        if self._config.query_expansion_enabled:
            try:
                extras = self._expand_query(question)
                for q in extras:
                    if q and q not in expanded:
                        expanded.append(q)
            except Exception as exc:
                logger.warning("synthesis.expand.failed", error=str(exc)[:200])
                warnings.append(f"query_expansion_failed: {exc}")

        # 4. Retrieve + RRF merge across expanded queries
        ranked_hits = self._multi_query_retrieve(
            expanded, project_keys, intent, feature_key_filter,
        )

        # 3. Pack context for synthesis
        packed = pack_hits(
            ranked_hits,
            min_comment_body_chars=self._config.min_comment_body_chars,
            max_context_chars=self._config.max_context_chars,
        )
        retrieved_keys = [h.issue_key for h in packed]

        # 4. Synthesis with Sonnet
        synth_result = self._synthesise(question, packed)

        latency_ms = (time.monotonic() - t0) * 1000.0
        usage = synth_result.get("usage") or {}
        return AnswerResult(
            question=question,
            project_keys=project_keys or [],
            expanded_queries=expanded,
            intent={
                "bug_intent": intent.bug_intent,
                "open_intent": intent.open_intent,
            },
            feature_tags=feature_tags,
            retrieved_keys=retrieved_keys,
            overview=synth_result.get("overview", ""),
            flow=synth_result.get("flow", []),
            acceptance_criteria=synth_result.get("acceptance_criteria", []),
            current_state=synth_result.get("current_state", {"active_tickets": [], "done_tickets": []}),
            known_issues=synth_result.get("known_issues", []),
            sources=synth_result.get("sources", []),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            latency_ms=round(latency_ms, 1),
            warnings=warnings,
        )

    # ── internals ──────────────────────────────────────────────────────────
    def _expand_query(self, question: str) -> list[str]:
        n = self._config.query_expansion_variants
        user_msg = (
            f"Produce {n} search queries for this user question. "
            "Include one query mirroring the original; the rest should be synonyms / "
            "translations / technical term variants.\n\n"
            f"Question: {question}"
        )
        resp = self._client.messages.create(
            model=self._config.query_expansion_model,
            max_tokens=800,
            temperature=0,   # deterministic — eval needs stable queries
            system=[
                {
                    "type": "text",
                    "text": QUERY_EXPANSION_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[QUERY_EXPANSION_TOOL],
            tool_choice={"type": "tool", "name": QUERY_EXPANSION_TOOL["name"]},
            messages=[{"role": "user", "content": user_msg}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == QUERY_EXPANSION_TOOL["name"]:
                queries = (block.input or {}).get("queries") or []
                return [q.strip() for q in queries if isinstance(q, str) and q.strip()]
        return []

    def _multi_query_retrieve(
        self,
        queries: list[str],
        project_keys: list[str] | None,
        intent: QueryIntent,
        feature_key_filter: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Run each expanded query through the Searcher, merge via RRF.

        When a feature filter is active, we run TWO retrieval passes per query
        (filtered + unfiltered) and merge both streams via Reciprocal Rank
        Fusion. Rationale:
          - Filtered pass raises recall when the classifier picked the right tag
            (bug queries, wide-scope concept queries).
          - Unfiltered pass protects top-1 precision when the classifier
            over-narrowed (lookup queries, short direct-match queries).
          Taking the union and re-ranking via RRF captures both.
        """
        per_query_top_k = self._config.per_query_top_k
        final_top_k = self._config.final_top_k

        # Reciprocal Rank Fusion (constant k=60 is the convention from the original paper).
        RRF_K = 60
        scores: dict[str, float] = {}
        best_hits: dict[str, dict[str, Any]] = {}

        def ingest(hits):
            for rank, hit in enumerate(hits, 1):
                key = hit.issue_key
                scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
                prev = best_hits.get(key)
                if prev is None or hit.score > prev["score"]:
                    best_hits[key] = {
                        "issue_key": hit.issue_key,
                        "score": hit.score,
                        "match_source": hit.match_source,
                        "summary": hit.summary,
                        "context": hit.context.to_dict() if hit.context else None,
                    }

        for q in queries:
            # Pass 1 — filter-aware retrieval (structural + feature filter)
            hits = self._searcher.find_tasks_by_functionality(
                q,
                project_keys=project_keys,
                top_k=per_query_top_k,
                min_score=0.2,   # permissive — let RRF do the ranking
                include_comments=True,
                include_merge_requests=False,
                must_issue_types=intent.must_issue_types,
                must_status_categories=intent.must_status_categories,
                must_issue_keys=feature_key_filter,
            )
            ingest(hits)

            # Pass 2 — unfiltered fallback, ONLY when the filter came from the
            # LLM feature-classifier (which can over-narrow on lookup queries).
            # Structural filters (bug_intent / open_intent) are high-confidence
            # keyword-based — we trust them and don't dilute with unfiltered
            # candidates that would otherwise push non-bug tickets into the
            # RRF-ranked top when the user asked specifically about bugs.
            if feature_key_filter is not None and intent.must_issue_types is None:
                hits_unfiltered = self._searcher.find_tasks_by_functionality(
                    q,
                    project_keys=project_keys,
                    top_k=per_query_top_k,
                    min_score=0.2,
                    include_comments=True,
                    include_merge_requests=False,
                    # no structural / feature filter
                )
                ingest(hits_unfiltered)

        ranked_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)[:final_top_k]
        return [best_hits[k] for k in ranked_keys if k in best_hits]

    def _synthesise(self, question: str, packed: list[PackedHit]) -> dict[str, Any]:
        user_prompt = format_prompt_body(question, packed)
        resp = self._client.messages.create(
            model=self._config.synthesis_model,
            max_tokens=self._config.synthesis_max_tokens,
            temperature=0,   # deterministic — makes eval reproducible
            system=[
                {
                    "type": "text",
                    "text": SYNTHESIS_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[SYNTHESIS_TOOL],
            tool_choice={"type": "tool", "name": SYNTHESIS_TOOL["name"]},
            messages=[{"role": "user", "content": user_prompt}],
        )
        out: dict[str, Any] = {
            "usage": {
                "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            }
        }
        for block in resp.content:
            if block.type == "tool_use" and block.name == SYNTHESIS_TOOL["name"]:
                out.update(block.input or {})
                break
        else:
            logger.warning("synthesis.no_tool_call", stop_reason=resp.stop_reason)
        return out


def create_synthesis_service(config: AppConfig, searcher: Searcher) -> SynthesisService:
    # Silence anthropic's verbose INFO logs in eval runs.
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required when synthesis is enabled. "
            "Set it in .env or export it in the shell."
        )
    client = anthropic.Anthropic(api_key=api_key)
    # Features repo reuses the existing DB connection pool held by searcher.
    features_repo = FeatureTagsRepo(searcher._db) if getattr(searcher, "_db", None) else None
    return SynthesisService(config.synthesis, searcher, client, features_repo)
