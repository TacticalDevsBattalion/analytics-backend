CREATE TABLE dashboard_personal_widgets (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX personal_widget_owner ON dashboard_personal_widgets(dashboard_id,user_id);
ALTER TABLE dashboard_user_overrides RENAME TO dashboard_user_overrides_v2;
CREATE TABLE dashboard_user_overrides (
    id TEXT PRIMARY KEY,
    dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    widget_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dashboard_id,widget_id,user_id)
);
INSERT INTO dashboard_user_overrides SELECT * FROM dashboard_user_overrides_v2;
DROP TABLE dashboard_user_overrides_v2;
