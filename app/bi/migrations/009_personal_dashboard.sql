CREATE TABLE personal_dashboard_preferences (
    user_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    migrated INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE dashboard_assets (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX dashboard_asset_owner ON dashboard_assets(user_id);
