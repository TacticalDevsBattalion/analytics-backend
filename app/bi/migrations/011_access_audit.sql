CREATE TABLE IF NOT EXISTS access_audit (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    actor_id TEXT,
    changed_at TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS access_audit_user ON access_audit(user_id, changed_at);
