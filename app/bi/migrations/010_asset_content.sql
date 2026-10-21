CREATE TABLE IF NOT EXISTS dashboard_asset_content (
    id TEXT PRIMARY KEY REFERENCES dashboard_assets(id) ON DELETE CASCADE,
    content BLOB NOT NULL
);
