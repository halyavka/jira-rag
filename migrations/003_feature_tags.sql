-- =============================================================================
-- Migration 003: feature taxonomy + issue tags
-- =============================================================================
-- Phase 3b — LLM-assigned feature tags enable precise filtering for concept
-- and bug queries where pure semantic retrieval misses (e.g. "subscription
-- types" semantically distant from "split72 PWA subscription 2.0").
--
-- The `features` table is a controlled vocabulary; tags in `issue_features`
-- REFERENCE this table so we can't introduce free-form drift.

CREATE TABLE IF NOT EXISTS jira.features (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    keywords    TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jira.issue_features (
    issue_key   TEXT NOT NULL REFERENCES jira.issues(key) ON DELETE CASCADE,
    feature     TEXT NOT NULL REFERENCES jira.features(name) ON DELETE CASCADE,
    source      TEXT NOT NULL,        -- 'llm' | 'manual' | 'heuristic'
    confidence  REAL NOT NULL DEFAULT 1.0,
    tagged_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (issue_key, feature)
);

CREATE INDEX IF NOT EXISTS idx_issue_features_feature ON jira.issue_features(feature);
CREATE INDEX IF NOT EXISTS idx_issue_features_issue   ON jira.issue_features(issue_key);

ALTER TABLE jira.features        ENABLE ROW LEVEL SECURITY;
ALTER TABLE jira.issue_features  ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all" ON jira.features;
DROP POLICY IF EXISTS "service_role_all" ON jira.issue_features;

CREATE POLICY "service_role_all" ON jira.features        FOR ALL USING (true);
CREATE POLICY "service_role_all" ON jira.issue_features  FOR ALL USING (true);
