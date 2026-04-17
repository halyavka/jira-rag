"""Repository classes — one per table, thin CRUD on top of `DatabaseConnection`."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jira_rag.database.client import DatabaseConnection, jsonb


class ProjectsRepo:
    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    def upsert(self, key: str, name: str) -> None:
        self._db.execute(
            """
            INSERT INTO projects(key, name, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET
                name = EXCLUDED.name,
                updated_at = now()
            """,
            (key, name),
        )

    def list(self) -> list[dict]:
        return self._db.execute("SELECT key, name FROM projects ORDER BY key")


class IssuesRepo:
    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    def upsert(self, row: dict[str, Any]) -> None:
        self._db.execute(
            """
            INSERT INTO issues(
                key, project_key, summary, description_text, issue_type,
                status, status_category, priority, resolution,
                assignee, reporter, labels, components, fix_versions,
                parent_key, epic_key, progress_percent,
                created_at, updated_at, resolved_at, raw
            ) VALUES (
                %(key)s, %(project_key)s, %(summary)s, %(description_text)s, %(issue_type)s,
                %(status)s, %(status_category)s, %(priority)s, %(resolution)s,
                %(assignee)s, %(reporter)s, %(labels)s, %(components)s, %(fix_versions)s,
                %(parent_key)s, %(epic_key)s, %(progress_percent)s,
                %(created_at)s, %(updated_at)s, %(resolved_at)s, %(raw)s
            )
            ON CONFLICT (key) DO UPDATE SET
                project_key      = EXCLUDED.project_key,
                summary          = EXCLUDED.summary,
                description_text = EXCLUDED.description_text,
                issue_type       = EXCLUDED.issue_type,
                status           = EXCLUDED.status,
                status_category  = EXCLUDED.status_category,
                priority         = EXCLUDED.priority,
                resolution       = EXCLUDED.resolution,
                assignee         = EXCLUDED.assignee,
                reporter         = EXCLUDED.reporter,
                labels           = EXCLUDED.labels,
                components       = EXCLUDED.components,
                fix_versions     = EXCLUDED.fix_versions,
                parent_key       = EXCLUDED.parent_key,
                epic_key         = EXCLUDED.epic_key,
                progress_percent = EXCLUDED.progress_percent,
                created_at       = EXCLUDED.created_at,
                updated_at       = EXCLUDED.updated_at,
                resolved_at      = EXCLUDED.resolved_at,
                raw              = EXCLUDED.raw
            """,
            {**row, "raw": jsonb(row.get("raw") or {})},
        )

    def mark_embedded(self, key: str, embed_hash: str, qdrant_point_id: str) -> None:
        self._db.execute(
            """
            UPDATE issues
               SET embed_hash = %s,
                   qdrant_point_id = %s,
                   embedded_at = now()
             WHERE key = %s
            """,
            (embed_hash, qdrant_point_id, key),
        )

    def get(self, key: str) -> dict | None:
        return self._db.execute_one("SELECT * FROM issues WHERE key = %s", (key,))

    def get_many(self, keys: list[str]) -> list[dict]:
        if not keys:
            return []
        return self._db.execute(
            "SELECT * FROM issues WHERE key = ANY(%s)", (keys,)
        )

    def needs_reindex(self, key: str, embed_hash: str) -> bool:
        row = self._db.execute_one(
            "SELECT embed_hash FROM issues WHERE key = %s", (key,)
        )
        return not row or row["embed_hash"] != embed_hash


class CommentsRepo:
    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    def upsert(self, row: dict[str, Any]) -> None:
        self._db.execute(
            """
            INSERT INTO comments(
                id, issue_key, author, body_text, created_at, updated_at, raw
            ) VALUES (
                %(id)s, %(issue_key)s, %(author)s, %(body_text)s,
                %(created_at)s, %(updated_at)s, %(raw)s
            )
            ON CONFLICT (id) DO UPDATE SET
                author     = EXCLUDED.author,
                body_text  = EXCLUDED.body_text,
                updated_at = EXCLUDED.updated_at,
                raw        = EXCLUDED.raw
            """,
            {**row, "raw": jsonb(row.get("raw") or {})},
        )

    def mark_embedded(self, comment_id: str, embed_hash: str, qdrant_point_id: str) -> None:
        self._db.execute(
            """
            UPDATE comments
               SET embed_hash = %s,
                   qdrant_point_id = %s,
                   embedded_at = now()
             WHERE id = %s
            """,
            (embed_hash, qdrant_point_id, comment_id),
        )

    def list_for_issue(self, issue_key: str) -> list[dict]:
        return self._db.execute(
            "SELECT * FROM comments WHERE issue_key = %s ORDER BY created_at",
            (issue_key,),
        )

    def needs_reindex(self, comment_id: str, embed_hash: str) -> bool:
        row = self._db.execute_one(
            "SELECT embed_hash FROM comments WHERE id = %s", (comment_id,)
        )
        return not row or row["embed_hash"] != embed_hash


class MergeRequestsRepo:
    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    def upsert(self, row: dict[str, Any]) -> None:
        self._db.execute(
            """
            INSERT INTO merge_requests(
                id, issue_key, provider, url, title, description,
                source_branch, target_branch, state, author,
                merged_at, created_at, updated_at, raw
            ) VALUES (
                %(id)s, %(issue_key)s, %(provider)s, %(url)s, %(title)s, %(description)s,
                %(source_branch)s, %(target_branch)s, %(state)s, %(author)s,
                %(merged_at)s, %(created_at)s, %(updated_at)s, %(raw)s
            )
            ON CONFLICT (id) DO UPDATE SET
                issue_key     = EXCLUDED.issue_key,
                provider      = EXCLUDED.provider,
                url           = EXCLUDED.url,
                title         = EXCLUDED.title,
                description   = EXCLUDED.description,
                source_branch = EXCLUDED.source_branch,
                target_branch = EXCLUDED.target_branch,
                state         = EXCLUDED.state,
                author        = EXCLUDED.author,
                merged_at     = EXCLUDED.merged_at,
                updated_at    = EXCLUDED.updated_at,
                raw           = EXCLUDED.raw
            """,
            {**row, "raw": jsonb(row.get("raw") or {})},
        )

    def mark_embedded(self, mr_id: str, embed_hash: str, qdrant_point_id: str) -> None:
        self._db.execute(
            """
            UPDATE merge_requests
               SET embed_hash = %s,
                   qdrant_point_id = %s,
                   embedded_at = now()
             WHERE id = %s
            """,
            (embed_hash, qdrant_point_id, mr_id),
        )

    def list_for_issue(self, issue_key: str) -> list[dict]:
        return self._db.execute(
            "SELECT * FROM merge_requests WHERE issue_key = %s ORDER BY updated_at DESC",
            (issue_key,),
        )


class StatusHistoryRepo:
    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    def insert_many(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        self._db.executemany(
            """
            INSERT INTO status_history(issue_key, from_status, to_status, changed_by, changed_at)
            VALUES (%(issue_key)s, %(from_status)s, %(to_status)s, %(changed_by)s, %(changed_at)s)
            ON CONFLICT (issue_key, changed_at, from_status, to_status) DO NOTHING
            """,
            rows,
        )

    def list_for_issue(self, issue_key: str) -> list[dict]:
        return self._db.execute(
            "SELECT * FROM status_history WHERE issue_key = %s ORDER BY changed_at",
            (issue_key,),
        )


class SyncStateRepo:
    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    def get(self, project_key: str) -> dict | None:
        return self._db.execute_one(
            "SELECT * FROM sync_state WHERE project_key = %s", (project_key,)
        )

    def last_cursor(self, project_key: str) -> datetime | None:
        row = self.get(project_key)
        return row["last_issue_update"] if row else None

    def update(
        self,
        project_key: str,
        last_issue_update: datetime | None,
        issues_indexed: int,
        error: str = "",
    ) -> None:
        self._db.execute(
            """
            INSERT INTO sync_state(project_key, last_synced_at, last_issue_update, issues_indexed, last_error)
            VALUES (%s, now(), %s, %s, %s)
            ON CONFLICT (project_key) DO UPDATE SET
                last_synced_at    = now(),
                last_issue_update = COALESCE(EXCLUDED.last_issue_update, sync_state.last_issue_update),
                issues_indexed    = sync_state.issues_indexed + EXCLUDED.issues_indexed,
                last_error        = EXCLUDED.last_error
            """,
            (project_key, last_issue_update, issues_indexed, error),
        )
