"""Pydantic models for configuration validation."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class JiraProject(BaseModel):
    key: str
    name: str = ""

    @field_validator("key")
    @classmethod
    def upper_key(cls, v: str) -> str:
        return v.strip().upper()


class JiraConfig(BaseModel):
    url: str
    email: str
    api_token: str
    projects: list[JiraProject] = Field(default_factory=list)
    jql_filter: str = ""
    page_size: int = 100
    full_resync_lookback_days: int = 3650
    # Dev Panel providers to query via /rest/dev-status. Default is just
    # GitLab — probing all four (GitLab/stash/GitHub/bitbucket) on every
    # issue was costing ~75% of the sync time on sites that only use one.
    dev_providers: list[str] = Field(default_factory=lambda: ["GitLab"])


class EmbeddingsConfig(BaseModel):
    provider: str = "fastembed"
    fastembed_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimension: int = 384
    voyage_api_key: str = ""
    voyage_model: str = "voyage-3-lite"


class SupabaseConfig(BaseModel):
    database_url: str
    schema_name: str = Field(default="jira", alias="schema")

    model_config = {"populate_by_name": True}


class QdrantConfig(BaseModel):
    host: str = "localhost"
    port: int = 6333
    grpc_port: int = 6334


class IndexerConfig(BaseModel):
    batch_size: int = 32
    force_reindex: bool = False
    index_comments: bool = True
    index_merge_requests: bool = True
    # Per-issue HTTP fetches (comment / dev-status / remote-link) are
    # parallelised across this many issues at once. Network-bound, so 8-16 is
    # safe for Jira Cloud (rate limit ~50 req/s). Stays sequential for DB &
    # Qdrant writes.
    concurrency: int = 8


class SearchConfig(BaseModel):
    default_top_k: int = 5
    min_score: float = 0.35
    hydrate_parent_issue: bool = True


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8100


class SynthesisConfig(BaseModel):
    # Set via env: ANTHROPIC_API_KEY
    # Synthesis uses Sonnet (balanced quality/cost); query expansion uses Haiku.
    enabled: bool = False
    synthesis_model: str = "claude-sonnet-4-6"
    synthesis_max_tokens: int = 8192
    query_expansion_enabled: bool = True
    query_expansion_model: str = "claude-haiku-4-5"
    # How many alternative phrasings Haiku produces for multi-query retrieval.
    query_expansion_variants: int = 4
    # Retrieval budget for each expanded query before RRF merge.
    per_query_top_k: int = 20
    # How many hydrated issues reach the synthesis prompt.
    final_top_k: int = 12
    # Filter out boilerplate comments ("+1", "done", "approved" etc) before packing.
    min_comment_body_chars: int = 30
    # Cap the total context packed into the synthesis prompt (rough token budget).
    max_context_chars: int = 180_000


class WebhookConfig(BaseModel):
    # Enable the /webhook/jira/{secret} endpoint on the `serve` HTTP server.
    enabled: bool = False
    # Shared secret embedded in the Jira webhook URL path. Jira Cloud does not
    # sign webhook payloads, so URL-path-secret is the standard auth mechanism.
    secret: str = ""
    # If an event arrives for a project not listed in jira.projects, ignore it.
    # Prevents accidentally indexing unrelated projects shared on the same
    # Atlassian site.
    enforce_project_allowlist: bool = True


class AppConfig(BaseModel):
    jira: JiraConfig
    embeddings: EmbeddingsConfig = EmbeddingsConfig()
    supabase: SupabaseConfig
    qdrant: QdrantConfig = QdrantConfig()
    indexer: IndexerConfig = IndexerConfig()
    search: SearchConfig = SearchConfig()
    server: ServerConfig = ServerConfig()
    synthesis: SynthesisConfig = SynthesisConfig()
    webhook: WebhookConfig = WebhookConfig()
