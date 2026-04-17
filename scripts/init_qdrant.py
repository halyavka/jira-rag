#!/usr/bin/env python3
"""Initialize (or reset) Qdrant collections for the Jira RAG agent.

Usage:
    python scripts/init_qdrant.py
    python scripts/init_qdrant.py --reset
    python scripts/init_qdrant.py --status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from jira_rag.config import load_config  # noqa: E402
from jira_rag.vectordb import (  # noqa: E402
    COMMENTS_COLLECTION,
    ISSUES_COLLECTION,
    MERGE_REQUESTS_COLLECTION,
    VectorCollections,
    create_embedding_service,
    create_qdrant_client,
)

COLLECTIONS = [ISSUES_COLLECTION, COMMENTS_COLLECTION, MERGE_REQUESTS_COLLECTION]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", "-c", default="config.yaml")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate collections.")
    parser.add_argument("--status", action="store_true", help="Show status only.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    embeddings = create_embedding_service(cfg.embeddings)
    client = create_qdrant_client(cfg.qdrant)
    vectors = VectorCollections(client, embeddings)

    print(f"Qdrant @ {cfg.qdrant.host}:{cfg.qdrant.port}")
    print(f"Embeddings: {embeddings.provider} (dim={embeddings.dimension})")
    print()

    if args.status:
        for name in COLLECTIONS:
            if client.collection_exists(name):
                info = client.get_collection(name)
                count = client.count(name).count
                print(f"  ✓ {name:30s} points={count} dim={info.config.params.vectors.size}")
            else:
                print(f"  ✗ {name:30s} (missing)")
        return

    if args.reset:
        confirm = input("This drops all Jira RAG vectors. Type 'yes' to continue: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)
        vectors.reset()
        print("Collections reset.")
        return

    vectors.ensure_collections()
    print("Collections ready.")


if __name__ == "__main__":
    main()
