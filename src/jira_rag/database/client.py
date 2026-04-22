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
        """Yield a cursor with a validated live connection.

        Supabase's pgbouncer pooler (port 6543, transaction mode) closes idle
        server-side connections after a few minutes. When our client sits
        idle during a long HTTP batch (dev-status fetches, etc.) and then
        tries to `execute`, we hit `InterfaceError: connection already
        closed`. We guard against that here by pinging on checkout and
        replacing a stale conn before yielding.
        """
        conn = self._acquire_live_conn()
        closed = False
        try:
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(f"SET search_path TO {self._schema}, public")
                        yield cur
            except psycopg2.InterfaceError:
                # Connection died mid-transaction — discard it entirely.
                self._pool.putconn(conn, close=True)
                closed = True
                raise
        finally:
            if not closed:
                self._pool.putconn(conn)

    def _acquire_live_conn(self, max_retries: int = 2) -> psycopg2.extensions.connection:
        """Checkout a connection from the pool and verify it still works.

        If the conn is stale (server-side closed), we drop it and try again.
        """
        last_exc: Exception | None = None
        for _ in range(max_retries + 1):
            conn = self._pool.getconn()
            # `conn.closed != 0` on psycopg2 means the client thinks it's closed.
            # But pooler may have killed it server-side while client still thinks
            # it's open — so we actively ping with SELECT 1.
            try:
                if conn.closed:
                    raise psycopg2.InterfaceError("connection already closed")
                with conn.cursor() as probe:
                    probe.execute("SELECT 1")
                    probe.fetchone()
                return conn
            except (psycopg2.InterfaceError, psycopg2.OperationalError) as exc:
                last_exc = exc
                logger.warning("db.conn.stale_replaced", reason=str(exc)[:120])
                # Drop the bad one; the pool opens a replacement on next getconn.
                self._pool.putconn(conn, close=True)
        raise psycopg2.InterfaceError(
            f"Could not acquire a live DB connection after retries: {last_exc}"
        )

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
