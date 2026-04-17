#!/usr/bin/env python3
"""Apply SQL migrations in ./migrations to the Supabase database.

Usage:
    python scripts/migrate.py [--config config.yaml]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from jira_rag.config import load_config  # noqa: E402
from jira_rag.database import create_db_connection  # noqa: E402

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", "-c", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    db = create_db_connection(cfg.supabase)

    # Track applied migrations in a dedicated table.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    applied = {
        row["version"]
        for row in db.execute("SELECT version FROM schema_migrations")
    }

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"No migrations found in {MIGRATIONS_DIR}")
        return

    for path in files:
        version = path.stem
        if version in applied:
            print(f"↷ {version} (already applied)")
            continue
        print(f"▶ applying {version}")
        sql = path.read_text()
        with db.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations(version) VALUES (%s)", (version,)
            )
        print(f"✓ {version}")

    print("All migrations applied.")


if __name__ == "__main__":
    main()
