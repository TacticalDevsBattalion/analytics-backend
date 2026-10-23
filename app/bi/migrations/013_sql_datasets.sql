-- SQL datasets run on ClickHouse (like Superset datasets), change history and usage.
CREATE TABLE IF NOT EXISTS sql_datasets (
    dataset_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    published_version INTEGER,
    archived INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_by TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sql_dataset_versions (
    dataset_key TEXT NOT NULL REFERENCES sql_datasets(dataset_key) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    sql_text TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (dataset_key, version)
);
CREATE TABLE IF NOT EXISTS analytics_history (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    revision INTEGER,
    actor_id TEXT,
    changed_at TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT
);
CREATE INDEX IF NOT EXISTS analytics_history_entity ON analytics_history(entity_type, entity_id, changed_at);
CREATE TABLE IF NOT EXISTS analytics_usage (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    day TEXT NOT NULL,
    runs INTEGER NOT NULL DEFAULT 0,
    cache_hits INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    total_ms REAL NOT NULL DEFAULT 0,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id, day)
)
