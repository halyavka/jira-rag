"""Qdrant collections for Jira RAG.

Three collections, all same embedding dimension:
  - jira_issues           — issue summary + description (primary search target)
  - jira_comments         — individual comments (discussion context)
  - jira_merge_requests   — MR titles + descriptions (implementation context)

Each point's payload carries `issue_key` so the caller can hydrate the full
record from Postgres.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

from qdrant_client import QdrantClient, models

from jira_rag.utils.logging import get_logger
from jira_rag.vectordb.embeddings import EmbeddingService

logger = get_logger(__name__)

ISSUES_COLLECTION = "jira_issues"
COMMENTS_COLLECTION = "jira_comments"
MERGE_REQUESTS_COLLECTION = "jira_merge_requests"

_COLLECTIONS: dict[str, list[str]] = {
    ISSUES_COLLECTION: [
        "issue_key",
        "project_key",
        "status",
        "status_category",
        "issue_type",
        "priority",
        "assignee",
    ],
    COMMENTS_COLLECTION: [
        "issue_key",
        "project_key",
        "author",
    ],
    MERGE_REQUESTS_COLLECTION: [
        "issue_key",
        "project_key",
        "provider",
        "state",
    ],
}


def stable_point_id(*parts: str) -> str:
    """Deterministic UUIDv5 from parts — same inputs produce same point id.

    We use Qdrant's UUID-as-point-id. With a stable id, upserts replace the
    prior vector instead of creating duplicates.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "::".join(parts)))


class VectorCollections:
    def __init__(self, client: QdrantClient, embeddings: EmbeddingService) -> None:
        self._client = client
        self._embeddings = embeddings

    # ── lifecycle ────────────────────────────────────────────────────────────
    def ensure_collections(self) -> None:
        dim = self._embeddings.dimension
        for name, indexed_fields in _COLLECTIONS.items():
            if not self._client.collection_exists(name):
                self._client.create_collection(
                    collection_name=name,
                    vectors_config=models.VectorParams(
                        size=dim, distance=models.Distance.COSINE
                    ),
                )
                for field in indexed_fields:
                    self._client.create_payload_index(
                        collection_name=name,
                        field_name=field,
                        field_schema=models.PayloadSchemaType.KEYWORD,
                    )
                logger.info("qdrant.collection.created", name=name, dim=dim)

    def reset(self) -> None:
        for name in _COLLECTIONS:
            if self._client.collection_exists(name):
                self._client.delete_collection(name)
                logger.info("qdrant.collection.dropped", name=name)
        self.ensure_collections()

    # ── upsert helpers ───────────────────────────────────────────────────────
    def upsert_issue(self, issue_key: str, text: str, payload: dict[str, Any]) -> str:
        point_id = stable_point_id("issue", issue_key)
        vector = self._embeddings.embed([text])[0]
        self._client.upsert(
            collection_name=ISSUES_COLLECTION,
            points=[models.PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        return point_id

    def upsert_issues_batch(self, records: list[dict[str, Any]]) -> list[str]:
        """records: [{'issue_key', 'text', 'payload'}, ...]. Returns point ids."""
        if not records:
            return []
        texts = [r["text"] for r in records]
        vectors = self._embeddings.embed(texts)
        points = []
        ids: list[str] = []
        for rec, vec in zip(records, vectors):
            pid = stable_point_id("issue", rec["issue_key"])
            ids.append(pid)
            points.append(models.PointStruct(id=pid, vector=vec, payload=rec["payload"]))
        self._client.upsert(collection_name=ISSUES_COLLECTION, points=points)
        return ids

    def upsert_comment(self, comment_id: str, text: str, payload: dict[str, Any]) -> str:
        point_id = stable_point_id("comment", comment_id)
        vector = self._embeddings.embed([text])[0]
        self._client.upsert(
            collection_name=COMMENTS_COLLECTION,
            points=[models.PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        return point_id

    def upsert_comments_batch(self, records: list[dict[str, Any]]) -> list[str]:
        if not records:
            return []
        texts = [r["text"] for r in records]
        vectors = self._embeddings.embed(texts)
        points = []
        ids: list[str] = []
        for rec, vec in zip(records, vectors):
            pid = stable_point_id("comment", rec["comment_id"])
            ids.append(pid)
            points.append(models.PointStruct(id=pid, vector=vec, payload=rec["payload"]))
        self._client.upsert(collection_name=COMMENTS_COLLECTION, points=points)
        return ids

    def upsert_merge_request(self, mr_id: str, text: str, payload: dict[str, Any]) -> str:
        point_id = stable_point_id("mr", mr_id)
        vector = self._embeddings.embed([text])[0]
        self._client.upsert(
            collection_name=MERGE_REQUESTS_COLLECTION,
            points=[models.PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        return point_id

    def upsert_merge_requests_batch(self, records: list[dict[str, Any]]) -> list[str]:
        if not records:
            return []
        texts = [r["text"] for r in records]
        vectors = self._embeddings.embed(texts)
        points = []
        ids: list[str] = []
        for rec, vec in zip(records, vectors):
            pid = stable_point_id("mr", rec["mr_id"])
            ids.append(pid)
            points.append(models.PointStruct(id=pid, vector=vec, payload=rec["payload"]))
        self._client.upsert(collection_name=MERGE_REQUESTS_COLLECTION, points=points)
        return ids

    # ── search ───────────────────────────────────────────────────────────────
    def search(
        self,
        collection: str,
        query_text: str,
        *,
        project_keys: list[str] | None = None,
        extra_filter: models.Filter | None = None,
        limit: int = 5,
        score_threshold: float = 0.0,
    ) -> list[dict]:
        vector = self._embeddings.embed_query(query_text)

        must: list[Any] = []
        if project_keys:
            must.append(
                models.FieldCondition(
                    key="project_key",
                    match=models.MatchAny(any=project_keys),
                )
            )
        if extra_filter and extra_filter.must:
            must.extend(extra_filter.must)

        query_filter = models.Filter(must=must) if must else None

        results = self._client.query_points(
            collection_name=collection,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            score_threshold=score_threshold,
            with_payload=True,
        )
        return [
            {**point.payload, "score": point.score, "point_id": str(point.id)}
            for point in results.points
        ]

    def delete_points(self, collection: str, point_ids: Iterable[str]) -> None:
        ids = list(point_ids)
        if not ids:
            return
        self._client.delete(
            collection_name=collection,
            points_selector=models.PointIdsList(points=ids),
        )
