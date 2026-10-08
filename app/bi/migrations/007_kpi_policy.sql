CREATE TABLE IF NOT EXISTS kpi_policy_versions (
    rule_set_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    PRIMARY KEY(rule_set_id,version)
);
CREATE INDEX IF NOT EXISTS kpi_policy_validity ON kpi_policy_versions(valid_from,valid_to);
