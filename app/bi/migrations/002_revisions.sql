ALTER TABLE bi_state ADD COLUMN configuration_revision INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS dashboard_override_revisions (
    dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY(dashboard_id,user_id)
);
