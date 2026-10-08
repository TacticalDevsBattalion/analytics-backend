CREATE TABLE IF NOT EXISTS analytics_cache_jobs (
    id TEXT PRIMARY KEY,
    requested_by TEXT,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','complete','failed')),
    completed_queries INTEGER NOT NULL DEFAULT 0,
    total_queries INTEGER NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
