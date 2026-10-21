CREATE TABLE IF NOT EXISTS user_preferences (
    user_id TEXT NOT NULL,
    section TEXT NOT NULL,
    revision INTEGER NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, section)
);
