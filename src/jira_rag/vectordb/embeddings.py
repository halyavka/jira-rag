"""Embedding service — FastEmbed (local) or Voyage AI (cloud)."""

from __future__ import annotations

from jira_rag.config.schema import EmbeddingsConfig
from jira_rag.utils.logging import get_logger

logger = get_logger(__name__)


class EmbeddingService:
    def __init__(self, config: EmbeddingsConfig) -> None:
        self._config = config
        self._provider = config.provider.lower()
        self._dimension = config.embedding_dimension
        self._client = None
        self._init_client()

    def _init_client(self) -> None:
        if self._provider == "fastembed":
            from fastembed import TextEmbedding

            logger.info("embeddings.fastembed.loading", model=self._config.fastembed_model)
            self._client = TextEmbedding(model_name=self._config.fastembed_model)
        elif self._provider == "voyage":
            import voyageai

            if not self._config.voyage_api_key:
                raise ValueError("embeddings.voyage_api_key is required when provider=voyage")
            self._client = voyageai.Client(api_key=self._config.voyage_api_key)
        else:
            raise ValueError(
                f"Unknown embeddings provider: '{self._provider}'. Use 'fastembed' or 'voyage'."
            )

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def provider(self) -> str:
        return self._provider

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents."""
        if not texts:
            return []
        if self._provider == "fastembed":
            return [e.tolist() for e in self._client.embed(texts)]
        # voyage
        result = self._client.embed(
            texts, model=self._config.voyage_model, input_type="document"
        )
        return result.embeddings

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query (asymmetric where supported)."""
        if self._provider == "fastembed":
            if hasattr(self._client, "query_embed"):
                vectors = list(self._client.query_embed([text]))
            else:
                vectors = list(self._client.embed([text]))
            return vectors[0].tolist()
        result = self._client.embed(
            [text], model=self._config.voyage_model, input_type="query"
        )
        return result.embeddings[0]


def create_embedding_service(config: EmbeddingsConfig) -> EmbeddingService:
    return EmbeddingService(config)
