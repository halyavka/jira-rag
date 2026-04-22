-- =============================================================================
-- Migration 002: Smart Checklist fields
-- =============================================================================
-- Stores the plaintext Smart Checklist (customfield_10720) and its progress
-- string (customfield_10289 — e.g. "43/43 - Done"). The checklist markdown is
-- also appended to the issue embed text so agents can semantically match on
-- acceptance-criteria-level content.

ALTER TABLE jira.issues
    ADD COLUMN IF NOT EXISTS checklist_text     TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS checklist_progress TEXT NOT NULL DEFAULT '';
