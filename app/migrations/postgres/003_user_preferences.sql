-- Filters and display settings of each user, the same on every device (BI SQLite migration 012).
CREATE TABLE user_preferences (user_id TEXT NOT NULL, section TEXT NOT NULL, revision BIGINT NOT NULL, value_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (user_id, section));
INSERT INTO bi_schema_migrations VALUES (12, now()::text) ON CONFLICT DO NOTHING;
