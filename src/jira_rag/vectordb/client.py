"""Qdrant client factory."""

from __future__ import annotations

from qdrant_client import QdrantClient

from jira_rag.config.schema import QdrantConfig


def create_qdrant_client(config: QdrantConfig) -> QdrantClient:
    return QdrantClient(host=config.host, port=config.port)
