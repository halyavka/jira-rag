FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build deps for psycopg2. libpq for runtime. gcc can be removed after install
# but keeping it cheap; we're not shipping this image everywhere.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       gcc \
       libpq-dev \
       curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/

RUN pip install -e .

# Pre-warm the FastEmbed model cache so the first search isn't cold.
# Skips gracefully if the chosen provider is voyage (remote).
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" || true

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/health', timeout=3)" || exit 1

CMD ["jira-rag", "serve"]
