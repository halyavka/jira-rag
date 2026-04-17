"""Sync pipeline: Jira → Supabase → Qdrant.

Flow per project:
  1. Determine cursor (sync_state.last_issue_update minus 5 min safety window).
  2. JQL-paginate issues updated >= cursor.
  3. For each issue:
       a. Upsert issue row in Postgres.
       b. Upsert status history from expanded changelog.
       c. Fetch + upsert all comments.
       d. Fetch + upsert linked merge requests (dev-panel + remote links).
  4. Embed & upsert:
       - issues batch
       - comments batch (only changed bodies)
       - merge requests batch (only changed descriptions)
  5. Update sync_state cursor.

Idempotency: each embeddable record stores `embed_hash` in Postgres. If the
hash of the embed text hasn't changed, we skip re-embedding (saves API calls
on re-runs). Point ids in Qdrant are deterministic UUIDv5 of the natural key,
so upserts overwrite.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from jira_rag.config.schema import AppConfig
from jira_rag.database import (
    CommentsRepo,
    DatabaseConnection,
    IssuesRepo,
    MergeRequestsRepo,
    ProjectsRepo,
    StatusHistoryRepo,
    SyncStateRepo,
)
from jira_rag.jira_client import (
    JiraClient,
    comment_to_row,
    dev_info_to_mr_rows,
    extract_status_history,
    issue_to_row,
    remote_link_to_mr_row,
)
from jira_rag.utils.logging import get_logger
from jira_rag.vectordb import VectorCollections

logger = get_logger(__name__)

_CURSOR_SAFETY = timedelta(minutes=5)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class SyncResult:
    project_key: str
    issues_fetched: int = 0
    issues_embedded: int = 0
    comments_embedded: int = 0
    mrs_embedded: int = 0
    error: str = ""
    last_issue_update: datetime | None = None
    warnings: list[str] = field(default_factory=list)


class SyncService:
    def __init__(
        self,
        config: AppConfig,
        jira: JiraClient,
        db: DatabaseConnection,
        vectors: VectorCollections,
    ) -> None:
        self._config = config
        self._jira = jira
        self._db = db
        self._vectors = vectors
        self._projects = ProjectsRepo(db)
        self._issues = IssuesRepo(db)
        self._comments = CommentsRepo(db)
        self._mrs = MergeRequestsRepo(db)
        self._history = StatusHistoryRepo(db)
        self._sync = SyncStateRepo(db)

    # ── public API ───────────────────────────────────────────────────────────
    def sync_all(self, *, full: bool = False) -> list[SyncResult]:
        results: list[SyncResult] = []
        for project in self._config.jira.projects:
            self._projects.upsert(project.key, project.name)
            try:
                results.append(self.sync_project(project.key, full=full))
            except Exception as exc:  # keep going on per-project errors
                logger.exception("sync.project.failed", project=project.key)
                self._sync.update(project.key, None, 0, error=str(exc))
                results.append(SyncResult(project_key=project.key, error=str(exc)))
        return results

    def sync_project(self, project_key: str, *, full: bool = False) -> SyncResult:
        cursor = self._compute_cursor(project_key, full=full)
        logger.info("sync.project.start", project=project_key, cursor=cursor)

        result = SyncResult(project_key=project_key)

        issue_batch: list[dict[str, Any]] = []
        comment_batch: list[dict[str, Any]] = []
        mr_batch: list[dict[str, Any]] = []

        for raw_issue in self._jira.iter_project_issues(project_key, updated_since=cursor):
            self._process_issue(raw_issue, project_key, result, issue_batch, comment_batch, mr_batch)

            if len(issue_batch) >= self._config.indexer.batch_size:
                self._flush_issues(issue_batch, result)
            if len(comment_batch) >= self._config.indexer.batch_size:
                self._flush_comments(comment_batch, result)
            if len(mr_batch) >= self._config.indexer.batch_size:
                self._flush_merge_requests(mr_batch, result)

        self._flush_issues(issue_batch, result)
        self._flush_comments(comment_batch, result)
        self._flush_merge_requests(mr_batch, result)

        self._sync.update(
            project_key,
            last_issue_update=result.last_issue_update,
            issues_indexed=result.issues_embedded,
        )
        logger.info(
            "sync.project.done",
            project=project_key,
            issues=result.issues_fetched,
            issues_embedded=result.issues_embedded,
            comments_embedded=result.comments_embedded,
            mrs_embedded=result.mrs_embedded,
        )
        return result

    def sync_single_issue(self, issue_key: str) -> SyncResult | None:
        """Fetch one issue by key and re-index it. Used by the webhook path.

        Returns None if the issue no longer exists or belongs to a project
        not in the configured allowlist.
        """
        raw_issue = self._jira.get_issue(issue_key)
        if raw_issue is None:
            logger.info("sync.issue.missing", issue_key=issue_key)
            return None

        project_key = (raw_issue.get("fields", {}).get("project") or {}).get("key") \
            or issue_key.split("-", 1)[0]
        if not self._project_is_allowed(project_key):
            logger.info("sync.issue.skipped.project", issue_key=issue_key, project=project_key)
            return None

        self._projects.upsert(project_key, "")

        result = SyncResult(project_key=project_key)
        issue_batch: list[dict[str, Any]] = []
        comment_batch: list[dict[str, Any]] = []
        mr_batch: list[dict[str, Any]] = []

        self._process_issue(raw_issue, project_key, result, issue_batch, comment_batch, mr_batch)

        self._flush_issues(issue_batch, result)
        self._flush_comments(comment_batch, result)
        self._flush_merge_requests(mr_batch, result)

        # Advance the cursor so the cron safety-net doesn't re-scan this issue.
        self._sync.update(
            project_key,
            last_issue_update=result.last_issue_update,
            issues_indexed=result.issues_embedded,
        )
        logger.info(
            "sync.issue.done",
            issue_key=issue_key,
            embedded=result.issues_embedded,
            comments=result.comments_embedded,
            mrs=result.mrs_embedded,
        )
        return result

    def delete_issue(self, issue_key: str) -> bool:
        """Remove an issue and all dependent rows from Postgres + Qdrant.

        `ON DELETE CASCADE` handles comments / MRs / status_history in Postgres.
        Qdrant point IDs are deterministic (UUIDv5 of natural key), so we can
        compute + delete them without looking up.
        """
        from jira_rag.vectordb.collections import (
            COMMENTS_COLLECTION,
            ISSUES_COLLECTION,
            MERGE_REQUESTS_COLLECTION,
            stable_point_id,
        )

        comments = self._comments.list_for_issue(issue_key)
        mrs = self._mrs.list_for_issue(issue_key)

        self._vectors.delete_points(ISSUES_COLLECTION, [stable_point_id("issue", issue_key)])
        self._vectors.delete_points(
            COMMENTS_COLLECTION,
            [stable_point_id("comment", c["id"]) for c in comments],
        )
        self._vectors.delete_points(
            MERGE_REQUESTS_COLLECTION,
            [stable_point_id("mr", m["id"]) for m in mrs],
        )

        deleted = self._db.execute(
            "DELETE FROM issues WHERE key = %s RETURNING key", (issue_key,)
        )
        logger.info("sync.issue.deleted", issue_key=issue_key, removed=bool(deleted))
        return bool(deleted)

    def _process_issue(
        self,
        raw_issue: dict,
        project_key: str,
        result: SyncResult,
        issue_batch: list[dict[str, Any]],
        comment_batch: list[dict[str, Any]],
        mr_batch: list[dict[str, Any]],
    ) -> None:
        row = issue_to_row(raw_issue, project_key)
        if not row.get("key"):
            return

        self._issues.upsert(row)
        self._history.insert_many(extract_status_history(raw_issue))
        result.issues_fetched += 1

        if row["updated_at"] and (
            result.last_issue_update is None or row["updated_at"] > result.last_issue_update
        ):
            result.last_issue_update = row["updated_at"]

        self._ingest_comments(row["key"], project_key, comment_batch)
        if self._config.indexer.index_merge_requests:
            self._ingest_merge_requests(raw_issue, row["key"], project_key, mr_batch, result)

        issue_embed = self._prepare_issue_for_embedding(row)
        if issue_embed is not None:
            issue_batch.append(issue_embed)

    def _project_is_allowed(self, project_key: str) -> bool:
        configured = {p.key for p in self._config.jira.projects}
        return project_key.upper() in configured

    # ── internal helpers ─────────────────────────────────────────────────────
    def _compute_cursor(self, project_key: str, *, full: bool) -> datetime | None:
        if full or self._config.indexer.force_reindex:
            return None
        last = self._sync.last_cursor(project_key)
        if last is None:
            return None
        return (last - _CURSOR_SAFETY).astimezone(timezone.utc)

    def _ingest_comments(
        self,
        issue_key: str,
        project_key: str,
        batch: list[dict[str, Any]],
    ) -> None:
        if not self._config.indexer.index_comments:
            return
        for raw_comment in self._jira.iter_comments(issue_key):
            row = comment_to_row(raw_comment, issue_key)
            self._comments.upsert(row)

            text = row["body_text"]
            if not text:
                continue
            h = _sha(text)
            if not self._config.indexer.force_reindex and not self._comments.needs_reindex(row["id"], h):
                continue
            batch.append(
                {
                    "comment_id": row["id"],
                    "text": text,
                    "embed_hash": h,
                    "payload": {
                        "comment_id": row["id"],
                        "issue_key": issue_key,
                        "project_key": project_key,
                        "author": row["author"],
                        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                        "text_preview": text[:500],
                    },
                }
            )

    def _ingest_merge_requests(
        self,
        raw_issue: dict,
        issue_key: str,
        project_key: str,
        batch: list[dict[str, Any]],
        result: SyncResult,
    ) -> None:
        mr_rows: list[dict[str, Any]] = []

        # Dev panel (preferred — has branches, state, author).
        try:
            dev_info = self._jira.get_dev_info(raw_issue.get("id"))
            mr_rows.extend(dev_info_to_mr_rows(dev_info, issue_key))
        except Exception as exc:
            result.warnings.append(f"dev_info:{issue_key}:{exc}")

        # Remote links fallback / augmentation.
        try:
            for raw_link in self._jira.get_remote_links(issue_key):
                row = remote_link_to_mr_row(raw_link, issue_key)
                if row:
                    mr_rows.append(row)
        except Exception as exc:
            result.warnings.append(f"remote_links:{issue_key}:{exc}")

        # Dedup on id (dev-panel wins over remote-link with same id).
        seen: dict[str, dict[str, Any]] = {}
        for row in mr_rows:
            seen[row["id"]] = row

        for row in seen.values():
            self._mrs.upsert(row)
            embed_text = "\n".join(
                filter(None, [row.get("title"), row.get("description")])
            ).strip()
            if not embed_text:
                continue
            h = _sha(embed_text)
            batch.append(
                {
                    "mr_id": row["id"],
                    "text": embed_text,
                    "embed_hash": h,
                    "payload": {
                        "mr_id": row["id"],
                        "issue_key": issue_key,
                        "project_key": project_key,
                        "provider": row.get("provider", ""),
                        "state": row.get("state", ""),
                        "url": row.get("url", ""),
                        "title": row.get("title", ""),
                    },
                }
            )

    def _prepare_issue_for_embedding(self, row: dict[str, Any]) -> dict[str, Any] | None:
        text = self._issue_embed_text(row)
        if not text:
            return None
        h = _sha(text)
        if not self._config.indexer.force_reindex and not self._issues.needs_reindex(row["key"], h):
            return None
        payload = {
            "issue_key": row["key"],
            "project_key": row["project_key"],
            "summary": row["summary"],
            "issue_type": row["issue_type"],
            "status": row["status"],
            "status_category": row["status_category"],
            "priority": row["priority"],
            "assignee": row["assignee"],
            "labels": row["labels"],
            "components": row["components"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "summary_preview": row["summary"][:200],
        }
        return {
            "issue_key": row["key"],
            "text": text,
            "embed_hash": h,
            "payload": payload,
        }

    @staticmethod
    def _issue_embed_text(row: dict[str, Any]) -> str:
        """Combine the fields that describe *what the issue is about*.

        The primary search use-case is "find the task that describes feature X",
        so we prioritise summary + description and add light metadata that helps
        the embedding disambiguate.
        """
        parts: list[str] = [
            f"[{row['issue_type']}] {row['summary']}",
            row["description_text"] or "",
        ]
        if row.get("labels"):
            parts.append("labels: " + ", ".join(row["labels"]))
        if row.get("components"):
            parts.append("components: " + ", ".join(row["components"]))
        return "\n\n".join(p for p in parts if p).strip()

    # ── batch flushers ───────────────────────────────────────────────────────
    def _flush_issues(self, batch: list[dict[str, Any]], result: SyncResult) -> None:
        if not batch:
            return
        ids = self._vectors.upsert_issues_batch(
            [{"issue_key": r["issue_key"], "text": r["text"], "payload": r["payload"]} for r in batch]
        )
        for rec, pid in zip(batch, ids):
            self._issues.mark_embedded(rec["issue_key"], rec["embed_hash"], pid)
        result.issues_embedded += len(batch)
        batch.clear()

    def _flush_comments(self, batch: list[dict[str, Any]], result: SyncResult) -> None:
        if not batch:
            return
        ids = self._vectors.upsert_comments_batch(
            [{"comment_id": r["comment_id"], "text": r["text"], "payload": r["payload"]} for r in batch]
        )
        for rec, pid in zip(batch, ids):
            self._comments.mark_embedded(rec["comment_id"], rec["embed_hash"], pid)
        result.comments_embedded += len(batch)
        batch.clear()

    def _flush_merge_requests(self, batch: list[dict[str, Any]], result: SyncResult) -> None:
        if not batch:
            return
        ids = self._vectors.upsert_merge_requests_batch(
            [{"mr_id": r["mr_id"], "text": r["text"], "payload": r["payload"]} for r in batch]
        )
        for rec, pid in zip(batch, ids):
            self._mrs.mark_embedded(rec["mr_id"], rec["embed_hash"], pid)
        result.mrs_embedded += len(batch)
        batch.clear()
