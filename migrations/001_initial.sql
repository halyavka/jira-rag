-- =============================================================================
-- Migration 001: initial Jira RAG schema
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS jira;

-- ── projects ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jira.projects (
    key          TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── issues ───────────────────────────────────────────────────────────────────
-- One row per Jira issue. `description_text` stores the ADF-flattened text
-- that we actually embed; `raw` keeps the full API payload for debugging.
CREATE TABLE IF NOT EXISTS jira.issues (
    key              TEXT PRIMARY KEY,
    project_key      TEXT NOT NULL REFERENCES jira.projects(key) ON DELETE CASCADE,
    summary          TEXT NOT NULL DEFAULT '',
    description_text TEXT NOT NULL DEFAULT '',
    issue_type       TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT '',
    status_category  TEXT NOT NULL DEFAULT '',
    priority         TEXT NOT NULL DEFAULT '',
    resolution       TEXT NOT NULL DEFAULT '',
    assignee         TEXT NOT NULL DEFAULT '',
    reporter         TEXT NOT NULL DEFAULT '',
    labels           TEXT[] NOT NULL DEFAULT '{}',
    components       TEXT[] NOT NULL DEFAULT '{}',
    fix_versions     TEXT[] NOT NULL DEFAULT '{}',
    parent_key       TEXT,
    epic_key         TEXT,
    progress_percent INTEGER NOT NULL DEFAULT 0,   -- derived from resolution/status
    created_at       TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ,
    resolved_at      TIMESTAMPTZ,
    raw              JSONB NOT NULL DEFAULT '{}',
    embedded_at      TIMESTAMPTZ,
    embed_hash       TEXT NOT NULL DEFAULT '',     -- sha256 of embed text; skip re-embed if unchanged
    qdrant_point_id  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_issues_project        ON jira.issues(project_key);
CREATE INDEX IF NOT EXISTS idx_issues_status         ON jira.issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_updated_at     ON jira.issues(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_issues_labels_gin     ON jira.issues USING gin(labels);
CREATE INDEX IF NOT EXISTS idx_issues_components_gin ON jira.issues USING gin(components);

-- ── comments ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jira.comments (
    id              TEXT PRIMARY KEY,
    issue_key       TEXT NOT NULL REFERENCES jira.issues(key) ON DELETE CASCADE,
    author          TEXT NOT NULL DEFAULT '',
    body_text       TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ,
    raw             JSONB NOT NULL DEFAULT '{}',
    embedded_at     TIMESTAMPTZ,
    embed_hash      TEXT NOT NULL DEFAULT '',
    qdrant_point_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_comments_issue      ON jira.comments(issue_key);
CREATE INDEX IF NOT EXISTS idx_comments_updated_at ON jira.comments(updated_at DESC);

-- ── merge requests (dev-panel remote links + development API) ────────────────
CREATE TABLE IF NOT EXISTS jira.merge_requests (
    id              TEXT PRIMARY KEY,          -- "<provider>:<external_id>"
    issue_key       TEXT NOT NULL REFERENCES jira.issues(key) ON DELETE CASCADE,
    provider        TEXT NOT NULL DEFAULT '',  -- gitlab / github / bitbucket / stash
    url             TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    source_branch   TEXT NOT NULL DEFAULT '',
    target_branch   TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT '',  -- open / merged / declined / closed
    author          TEXT NOT NULL DEFAULT '',
    merged_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ,
    raw             JSONB NOT NULL DEFAULT '{}',
    embedded_at     TIMESTAMPTZ,
    embed_hash      TEXT NOT NULL DEFAULT '',
    qdrant_point_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_mr_issue ON jira.merge_requests(issue_key);
CREATE INDEX IF NOT EXISTS idx_mr_state ON jira.merge_requests(state);

-- ── status history ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jira.status_history (
    id          BIGSERIAL PRIMARY KEY,
    issue_key   TEXT NOT NULL REFERENCES jira.issues(key) ON DELETE CASCADE,
    from_status TEXT NOT NULL DEFAULT '',
    to_status   TEXT NOT NULL DEFAULT '',
    changed_by  TEXT NOT NULL DEFAULT '',
    changed_at  TIMESTAMPTZ NOT NULL,
    UNIQUE(issue_key, changed_at, from_status, to_status)
);

CREATE INDEX IF NOT EXISTS idx_status_history_issue ON jira.status_history(issue_key);

-- ── sync state ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jira.sync_state (
    project_key      TEXT PRIMARY KEY REFERENCES jira.projects(key) ON DELETE CASCADE,
    last_synced_at   TIMESTAMPTZ,          -- wall-clock of last successful sync
    last_issue_update TIMESTAMPTZ,         -- max(updated_at) seen from Jira; next sync cursor
    issues_indexed   INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT NOT NULL DEFAULT ''
);

-- ── RLS (Supabase pattern — permissive for service role) ─────────────────────
ALTER TABLE jira.projects        ENABLE ROW LEVEL SECURITY;
ALTER TABLE jira.issues          ENABLE ROW LEVEL SECURITY;
ALTER TABLE jira.comments        ENABLE ROW LEVEL SECURITY;
ALTER TABLE jira.merge_requests  ENABLE ROW LEVEL SECURITY;
ALTER TABLE jira.status_history  ENABLE ROW LEVEL SECURITY;
ALTER TABLE jira.sync_state      ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all" ON jira.projects;
DROP POLICY IF EXISTS "service_role_all" ON jira.issues;
DROP POLICY IF EXISTS "service_role_all" ON jira.comments;
DROP POLICY IF EXISTS "service_role_all" ON jira.merge_requests;
DROP POLICY IF EXISTS "service_role_all" ON jira.status_history;
DROP POLICY IF EXISTS "service_role_all" ON jira.sync_state;

CREATE POLICY "service_role_all" ON jira.projects       FOR ALL USING (true);
CREATE POLICY "service_role_all" ON jira.issues         FOR ALL USING (true);
CREATE POLICY "service_role_all" ON jira.comments       FOR ALL USING (true);
CREATE POLICY "service_role_all" ON jira.merge_requests FOR ALL USING (true);
CREATE POLICY "service_role_all" ON jira.status_history FOR ALL USING (true);
CREATE POLICY "service_role_all" ON jira.sync_state     FOR ALL USING (true);

GRANT USAGE ON SCHEMA jira TO postgres;
GRANT ALL ON ALL TABLES IN SCHEMA jira TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA jira TO postgres;
