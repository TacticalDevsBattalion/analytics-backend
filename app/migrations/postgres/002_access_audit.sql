-- Who changed whose access and how (matches BI SQLite migration 011).
CREATE TABLE access_audit (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, actor_id TEXT, changed_at TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL);
CREATE INDEX access_audit_user ON access_audit(user_id, changed_at);
INSERT INTO bi_schema_migrations VALUES (11, now()::text) ON CONFLICT DO NOTHING;
