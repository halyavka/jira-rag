"""RAG search API — the primary entry point for other agents.

Typical call:

    from jira_rag.search import create_searcher
    from jira_rag.config import load_config

    searcher = create_searcher(load_config("config.yaml"))
    hits = searcher.find_tasks_by_functionality(
        "user can reset password via SMS",
        project_keys=["PROJ"],
        top_k=5,
    )
    for hit in hits:
        print(hit.issue_key, hit.score, hit.summary)
        print(hit.context.description_text[:500])

The searcher combines Qdrant (semantic recall) with Supabase (ground truth
hydration) so callers receive a complete picture of each matching issue —
description, current status, comments, and linked MRs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jira_rag.config.schema import AppConfig
from jira_rag.database import (
    CommentsRepo,
    DatabaseConnection,
    IssuesRepo,
    MergeRequestsRepo,
    StatusHistoryRepo,
    create_db_connection,
)
from jira_rag.utils.logging import get_logger
from jira_rag.vectordb import (
    COMMENTS_COLLECTION,
    ISSUES_COLLECTION,
    MERGE_REQUESTS_COLLECTION,
    VectorCollections,
    create_embedding_service,
    create_qdrant_client,
)

logger = get_logger(__name__)


@dataclass
class IssueContext:
    """Full hydrated record for an issue — what other agents actually need."""

    key: str
    project_key: str
    summary: str
    description_text: str
    issue_type: str
    status: str
    status_category: str
    priority: str
    resolution: str
    assignee: str
    labels: list[str]
    components: list[str]
    progress_percent: int
    created_at: Any
    updated_at: Any
    resolved_at: Any
    comments: list[dict] = field(default_factory=list)
    merge_requests: list[dict] = field(default_factory=list)
    status_history: list[dict] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict) -> "IssueContext":
        return cls(
            key=row["key"],
            project_key=row["project_key"],
            summary=row["summary"],
            description_text=row["description_text"],
            issue_type=row["issue_type"],
            status=row["status"],
            status_category=row["status_category"],
            priority=row["priority"],
            resolution=row["resolution"],
            assignee=row["assignee"],
            labels=list(row.get("labels") or []),
            components=list(row.get("components") or []),
            progress_percent=row["progress_percent"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_at=row["resolved_at"],
        )

    def to_dict(self) -> dict:
        def iso(v):
            return v.isoformat() if hasattr(v, "isoformat") else v

        return {
            "key": self.key,
            "project_key": self.project_key,
            "summary": self.summary,
            "description_text": self.description_text,
            "issue_type": self.issue_type,
            "status": self.status,
            "status_category": self.status_category,
            "priority": self.priority,
            "resolution": self.resolution,
            "assignee": self.assignee,
            "labels": self.labels,
            "components": self.components,
            "progress_percent": self.progress_percent,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "resolved_at": iso(self.resolved_at),
            "comments": [
                {**c, "created_at": iso(c.get("created_at")), "updated_at": iso(c.get("updated_at"))}
                for c in self.comments
            ],
            "merge_requests": [
                {**m, "created_at": iso(m.get("created_at")), "updated_at": iso(m.get("updated_at")), "merged_at": iso(m.get("merged_at"))}
                for m in self.merge_requests
            ],
            "status_history": [
                {**h, "changed_at": iso(h.get("changed_at"))}
                for h in self.status_history
            ],
        }


@dataclass
class SearchHit:
    issue_key: str
    score: float
    summary: str
    match_source: str  # "issue" | "comment" | "merge_request"
    match_preview: str
    context: IssueContext | None = None

    def to_dict(self) -> dict:
        return {
            "issue_key": self.issue_key,
            "score": self.score,
            "summary": self.summary,
            "match_source": self.match_source,
            "match_preview": self.match_preview,
            "context": self.context.to_dict() if self.context else None,
        }


class Searcher:
    def __init__(
        self,
        config: AppConfig,
        db: DatabaseConnection,
        vectors: VectorCollections,
    ) -> None:
        self._config = config
        self._db = db
        self._vectors = vectors
        self._issues = IssuesRepo(db)
        self._comments = CommentsRepo(db)
        self._mrs = MergeRequestsRepo(db)
        self._history = StatusHistoryRepo(db)

    # ── primary API ──────────────────────────────────────────────────────────
    def find_tasks_by_functionality(
        self,
        query: str,
        *,
        project_keys: list[str] | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
        include_comments: bool = True,
        include_merge_requests: bool = False,
        must_issue_types: list[str] | None = None,
        must_status_categories: list[str] | None = None,
        must_issue_keys: set[str] | None = None,
    ) -> list[SearchHit]:
        """Semantic search primarily over issue summary+description.

        Structural filters (``must_issue_types`` / ``must_status_categories``)
        apply only to the ISSUES collection — comment/MR payloads don't carry
        parent-issue metadata, so we post-filter them in-memory against
        already-known issue keys further below.

        This is the call other agents should use to answer
        "which ticket describes feature X and how should it work?".
        Issue hits come first; comment/MR hits are merged in and deduped so
        the parent issue appears once with the best-scoring match.
        """
        top_k = top_k or self._config.search.default_top_k
        min_score = self._config.search.min_score if min_score is None else min_score

        # over-fetch each collection so merging doesn't under-fill top_k
        per_source_limit = max(top_k * 3, 10)

        from qdrant_client import models as qd  # lazy import to avoid cycles
        must_conditions: list[Any] = []
        if must_issue_types:
            must_conditions.append(
                qd.FieldCondition(
                    key="issue_type", match=qd.MatchAny(any=list(must_issue_types)),
                )
            )
        if must_status_categories:
            must_conditions.append(
                qd.FieldCondition(
                    key="status_category",
                    match=qd.MatchAny(any=list(must_status_categories)),
                )
            )
        issue_filter = qd.Filter(must=must_conditions) if must_conditions else None

        issue_hits = self._vectors.search(
            ISSUES_COLLECTION,
            query,
            project_keys=project_keys,
            extra_filter=issue_filter,
            limit=per_source_limit,
            score_threshold=min_score,
        )

        # When structural filter is active, build an allowed set from Supabase
        # so comment/MR hits on non-matching parent issues get post-filtered.
        # A caller-supplied `must_issue_keys` (e.g. from feature-tag classification)
        # intersects on top.
        allowed_keys: set[str] | None = None
        if must_conditions:
            allowed_keys = self._allowed_issue_keys(
                project_keys, must_issue_types, must_status_categories,
            )
        if must_issue_keys is not None:
            allowed_keys = (
                set(must_issue_keys) if allowed_keys is None
                else allowed_keys & set(must_issue_keys)
            )

        hits_by_issue: dict[str, SearchHit] = {}
        for h in issue_hits:
            key = h["issue_key"]
            if allowed_keys is not None and key not in allowed_keys:
                continue
            hits_by_issue[key] = SearchHit(
                issue_key=key,
                score=h["score"],
                summary=h.get("summary", "") or h.get("summary_preview", ""),
                match_source="issue",
                match_preview=h.get("summary_preview", "") or h.get("summary", ""),
            )

        if include_comments:
            for h in self._vectors.search(
                COMMENTS_COLLECTION,
                query,
                project_keys=project_keys,
                limit=per_source_limit,
                score_threshold=min_score,
            ):
                if allowed_keys is not None and h["issue_key"] not in allowed_keys:
                    continue
                key = h["issue_key"]
                existing = hits_by_issue.get(key)
                if existing is None or h["score"] > existing.score:
                    hits_by_issue[key] = SearchHit(
                        issue_key=key,
                        score=h["score"],
                        summary=existing.summary if existing else "",
                        match_source="comment" if existing is None else "comment",
                        match_preview=h.get("text_preview", ""),
                    )

        if include_merge_requests:
            for h in self._vectors.search(
                MERGE_REQUESTS_COLLECTION,
                query,
                project_keys=project_keys,
                limit=per_source_limit,
                score_threshold=min_score,
            ):
                if allowed_keys is not None and h["issue_key"] not in allowed_keys:
                    continue
                key = h["issue_key"]
                existing = hits_by_issue.get(key)
                if existing is None or h["score"] > existing.score:
                    hits_by_issue[key] = SearchHit(
                        issue_key=key,
                        score=h["score"],
                        summary=existing.summary if existing else h.get("title", ""),
                        match_source="merge_request",
                        match_preview=h.get("title", ""),
                    )

        ranked = sorted(hits_by_issue.values(), key=lambda h: h.score, reverse=True)[:top_k]
        self._hydrate(ranked)
        return ranked

    def get_issue(self, issue_key: str) -> IssueContext | None:
        row = self._issues.get(issue_key)
        if not row:
            return None
        ctx = IssueContext.from_row(row)
        ctx.comments = self._comments.list_for_issue(issue_key)
        ctx.merge_requests = self._mrs.list_for_issue(issue_key)
        ctx.status_history = self._history.list_for_issue(issue_key)
        return ctx

    # ── internal ─────────────────────────────────────────────────────────────
    def _allowed_issue_keys(
        self,
        project_keys: list[str] | None,
        must_issue_types: list[str] | None,
        must_status_categories: list[str] | None,
    ) -> set[str]:
        """Return the set of issue keys matching the structural filters.

        Used to post-filter comment/MR hits whose Qdrant payload doesn't
        carry parent-issue metadata.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if project_keys:
            conditions.append("project_key = ANY(%s)")
            params.append([p.upper() for p in project_keys])
        if must_issue_types:
            conditions.append("issue_type = ANY(%s)")
            params.append(list(must_issue_types))
        if must_status_categories:
            conditions.append("status_category = ANY(%s)")
            params.append(list(must_status_categories))
        where = " AND ".join(conditions) if conditions else "TRUE"
        rows = self._db.execute(f"SELECT key FROM issues WHERE {where}", tuple(params))
        return {r["key"] for r in rows}

    def _hydrate(self, hits: list[SearchHit]) -> None:
        if not hits:
            return
        keys = [h.issue_key for h in hits]
        rows = self._issues.get_many(keys)
        rows_by_key = {r["key"]: r for r in rows}
        for hit in hits:
            row = rows_by_key.get(hit.issue_key)
            if not row:
                continue
            ctx = IssueContext.from_row(row)
            ctx.comments = self._comments.list_for_issue(hit.issue_key)
            ctx.merge_requests = self._mrs.list_for_issue(hit.issue_key)
            if not hit.summary:
                hit.summary = ctx.summary
            hit.context = ctx


def create_searcher(config: AppConfig) -> Searcher:
    db = create_db_connection(config.supabase)
    embeddings = create_embedding_service(config.embeddings)
    qdrant = create_qdrant_client(config.qdrant)
    vectors = VectorCollections(qdrant, embeddings)
    return Searcher(config, db, vectors)
