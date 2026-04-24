"""`jira-rag` CLI — init, sync, search, serve."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from jira_rag.config import load_config
from jira_rag.database import create_db_connection
from jira_rag.indexer import SyncService
from jira_rag.jira_client import create_jira_client
from jira_rag.search import create_searcher
from jira_rag.utils.logging import configure_logging, get_logger
from jira_rag.vectordb import (
    VectorCollections,
    create_embedding_service,
    create_qdrant_client,
)

logger = get_logger(__name__)


@click.group()
@click.option("--config", "-c", "config_path", default="config.yaml", show_default=True)
@click.option("--log-level", default="INFO", show_default=True)
@click.pass_context
def cli(ctx: click.Context, config_path: str, log_level: str) -> None:
    """Jira RAG indexer + search."""
    configure_logging(log_level)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["config"] = load_config(config_path)


@cli.command("init")
@click.option("--reset", is_flag=True, help="Drop and recreate Qdrant collections.")
@click.pass_context
def init_cmd(ctx: click.Context, reset: bool) -> None:
    """Create Qdrant collections (and verify Postgres reachability)."""
    cfg = ctx.obj["config"]
    db = create_db_connection(cfg.supabase)
    db.execute("SELECT 1")
    click.echo("✅ Postgres reachable")

    embeddings = create_embedding_service(cfg.embeddings)
    qdrant = create_qdrant_client(cfg.qdrant)
    vectors = VectorCollections(qdrant, embeddings)

    if reset:
        click.confirm("This drops all vectors. Continue?", abort=True)
        vectors.reset()
        click.echo("✅ Collections reset")
    else:
        vectors.ensure_collections()
        click.echo("✅ Collections ready")


@cli.command("sync")
@click.option("--project", "project_key", help="Sync a single project (default: all).")
@click.option("--full", is_flag=True, help="Ignore cursor and re-scan everything.")
@click.pass_context
def sync_cmd(ctx: click.Context, project_key: str | None, full: bool) -> None:
    """Fetch Jira issues/comments/MRs and index them."""
    cfg = ctx.obj["config"]
    db = create_db_connection(cfg.supabase)
    jira = create_jira_client(cfg.jira)
    embeddings = create_embedding_service(cfg.embeddings)
    qdrant = create_qdrant_client(cfg.qdrant)
    vectors = VectorCollections(qdrant, embeddings)
    vectors.ensure_collections()

    service = SyncService(cfg, jira, db, vectors)

    if project_key:
        from jira_rag.database import ProjectsRepo

        proj = next((p for p in cfg.jira.projects if p.key == project_key.upper()), None)
        if not proj:
            click.echo(f"Project {project_key} not in config", err=True)
            sys.exit(1)
        ProjectsRepo(db).upsert(proj.key, proj.name)
        results = [service.sync_project(proj.key, full=full)]
    else:
        results = service.sync_all(full=full)

    for r in results:
        click.echo(
            f"[{r.project_key}] fetched={r.issues_fetched} "
            f"issues_embedded={r.issues_embedded} "
            f"comments_embedded={r.comments_embedded} "
            f"mrs_embedded={r.mrs_embedded} "
            f"error={r.error or 'ok'}"
        )


@cli.command("search")
@click.argument("query")
@click.option("--project", "project_keys", multiple=True, help="Limit to project key(s).")
@click.option("--top-k", type=int, default=None)
@click.option("--min-score", type=float, default=None)
@click.option("--with-comments/--no-comments", default=True)
@click.option("--with-mrs/--no-mrs", default=False)
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON for scripting.")
@click.pass_context
def search_cmd(
    ctx: click.Context,
    query: str,
    project_keys: tuple[str, ...],
    top_k: int | None,
    min_score: float | None,
    with_comments: bool,
    with_mrs: bool,
    as_json: bool,
) -> None:
    """Semantic search over indexed Jira content."""
    cfg = ctx.obj["config"]
    searcher = create_searcher(cfg)
    hits = searcher.find_tasks_by_functionality(
        query,
        project_keys=[p.upper() for p in project_keys] or None,
        top_k=top_k,
        min_score=min_score,
        include_comments=with_comments,
        include_merge_requests=with_mrs,
    )

    if as_json:
        click.echo(json.dumps([h.to_dict() for h in hits], indent=2, default=str))
        return

    if not hits:
        click.echo("No matches.")
        return

    for i, hit in enumerate(hits, 1):
        click.echo(f"\n── {i}. {hit.issue_key}  score={hit.score:.3f}  via={hit.match_source}")
        click.echo(f"   {hit.summary}")
        if hit.context:
            click.echo(f"   status={hit.context.status} ({hit.context.progress_percent}%)  "
                       f"assignee={hit.context.assignee or '—'}")
            desc = hit.context.description_text.strip().replace("\n", " ")
            if desc:
                click.echo(f"   {desc[:300]}{'…' if len(desc) > 300 else ''}")
            if hit.context.merge_requests:
                click.echo(f"   MRs: {len(hit.context.merge_requests)}")


@cli.command("status")
@click.pass_context
def status_cmd(ctx: click.Context) -> None:
    """Print sync cursors and counts per project."""
    cfg = ctx.obj["config"]
    db = create_db_connection(cfg.supabase)
    rows = db.execute(
        """
        SELECT p.key, p.name, s.last_synced_at, s.last_issue_update,
               s.issues_indexed, s.last_error,
               (SELECT count(*) FROM jira.issues WHERE project_key = p.key) AS issue_count
          FROM projects p
          LEFT JOIN sync_state s ON s.project_key = p.key
         ORDER BY p.key
        """
    )
    for r in rows:
        click.echo(
            f"[{r['key']}] name={r['name']!r} issues={r['issue_count']} "
            f"cursor={r['last_issue_update']} synced={r['last_synced_at']} "
            f"err={r['last_error'] or '—'}"
        )


@cli.command("serve")
@click.pass_context
def serve_cmd(ctx: click.Context) -> None:
    """Expose HTTP /search for agents and POST /webhook/jira/{secret} for Jira."""
    import uvicorn
    from fastapi import FastAPI, Query

    cfg = ctx.obj["config"]
    searcher = create_searcher(cfg)
    app = FastAPI(title="Jira RAG")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/search")
    def search(
        q: str = Query(..., description="Natural-language functionality query"),
        project: list[str] | None = Query(None),
        top_k: int = Query(cfg.search.default_top_k),
        min_score: float = Query(cfg.search.min_score),
        include_comments: bool = Query(True),
        include_merge_requests: bool = Query(False),
    ) -> dict:
        hits = searcher.find_tasks_by_functionality(
            q,
            project_keys=[p.upper() for p in project] if project else None,
            top_k=top_k,
            min_score=min_score,
            include_comments=include_comments,
            include_merge_requests=include_merge_requests,
        )
        return {"query": q, "hits": [h.to_dict() for h in hits]}

    @app.get("/issues/{key}")
    def get_issue(key: str) -> dict:
        ctx_obj = searcher.get_issue(key.upper())
        if not ctx_obj:
            return {"error": "not_found"}
        return ctx_obj.to_dict()

    # ── /ask: synthesised answer via Claude (Phase 1) ─────────────────────
    if cfg.synthesis.enabled:
        from fastapi import Body
        from jira_rag.synthesis import create_synthesis_service

        synth = create_synthesis_service(cfg, searcher)
        logger.info(
            "synthesis.enabled",
            synthesis_model=cfg.synthesis.synthesis_model,
            expansion_model=cfg.synthesis.query_expansion_model,
        )

        @app.post("/ask")
        def ask(payload: dict = Body(...)) -> dict:
            q = (payload.get("q") or "").strip()
            if not q:
                return {"error": "missing 'q'"}
            project = payload.get("project") or []
            project_keys = [p.upper() for p in project] if project else None
            result = synth.ask(q, project_keys=project_keys)
            return result.to_dict()
    else:
        logger.info("synthesis.disabled", hint="set synthesis.enabled=true in config + ANTHROPIC_API_KEY env")

    if cfg.webhook.enabled:
        from jira_rag.webhook import build_webhook_router

        db = create_db_connection(cfg.supabase)
        jira = create_jira_client(cfg.jira)
        embeddings = create_embedding_service(cfg.embeddings)
        qdrant = create_qdrant_client(cfg.qdrant)
        vectors = VectorCollections(qdrant, embeddings)
        vectors.ensure_collections()
        sync_service = SyncService(cfg, jira, db, vectors)
        app.include_router(build_webhook_router(cfg.webhook, sync_service))
        logger.info("webhook.enabled", path="/webhook/jira/{secret}")

    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)


if __name__ == "__main__":
    cli()
