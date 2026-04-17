"""PostgreSQL (Supabase) connection pool."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.extras
import psycopg2.pool

from jira_rag.config.schema import SupabaseConfig
from jira_rag.utils.logging import get_logger

logger = get_logger(__name__)


class DatabaseConnection:
    """Thread-safe Postgres connection pool with auto `search_path`."""

    def __init__(
        self,
        database_url: str,
        schema: str = "jira",
        minconn: int = 1,
        maxconn: int = 5,
    ) -> None:
        self._schema = schema
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=minconn,
            maxconn=maxconn,
            dsn=database_url,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        logger.info("db.pool.created", schema=schema)

    @contextmanager
    def cursor(self) -> Generator[psycopg2.extras.RealDictCursor, None, None]:
        conn = self._pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {self._schema}, public")
                    yield cur
        finally:
            self._pool.putconn(conn)

    def execute(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                return [dict(row) for row in cur.fetchall()]
            return []

    def execute_one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        rows = self.execute(sql, params)
        return rows[0] if rows else None

    def executemany(self, sql: str, seq: list[tuple | dict]) -> None:
        if not seq:
            return
        with self.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, seq, page_size=100)

    def close(self) -> None:
        self._pool.closeall()


def create_db_connection(config: SupabaseConfig) -> DatabaseConnection:
    if not config.database_url:
        raise ValueError("supabase.database_url is not set in config")
    return DatabaseConnection(config.database_url, schema=config.schema_name)


def jsonb(value: dict | list) -> psycopg2.extras.Json:
    return psycopg2.extras.Json(value, dumps=json.dumps)
