CREATE TABLE IF NOT EXISTS dashboards (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dashboard_widgets (
    id TEXT PRIMARY KEY, dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS widget_dashboard ON dashboard_widgets(dashboard_id);
CREATE TABLE IF NOT EXISTS dashboard_assignments (
    dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    assignment_type TEXT NOT NULL, assignment_id TEXT NOT NULL,
    PRIMARY KEY(dashboard_id,assignment_type,assignment_id)
);
CREATE TABLE IF NOT EXISTS dashboard_user_overrides (
    id TEXT PRIMARY KEY, dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    widget_id TEXT NOT NULL REFERENCES dashboard_widgets(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL, revision INTEGER NOT NULL, definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(dashboard_id,widget_id,user_id)
);
CREATE TABLE IF NOT EXISTS metric_definitions (
    id TEXT PRIMARY KEY, metric_key TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kpi_definitions (
    id TEXT PRIMARY KEY, metric_key TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS category_weights (
    id TEXT PRIMARY KEY, category TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY, role_key TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_access (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bi_state (
    id INTEGER PRIMARY KEY CHECK(id=1), seeded INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO bi_state VALUES (1,0);
