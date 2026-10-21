-- Account, configuration and BI stores in PostgreSQL. Columns keep the SQLite
-- order: the stores insert positional VALUES. INTEGER is BIGINT and REAL is
-- DOUBLE PRECISION so that values match SQLite's 64-bit storage.

-- Accounts and sessions
CREATE TABLE account_state (id BIGINT PRIMARY KEY CHECK(id=1), ever_enabled BIGINT NOT NULL CHECK(ever_enabled IN (0,1)));
INSERT INTO account_state VALUES (1, 0);
CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('administrator','viewer')), active BIGINT NOT NULL CHECK(active IN (0,1)), created_at TEXT NOT NULL, password_hash TEXT NOT NULL, auth_version BIGINT NOT NULL DEFAULT 0);
CREATE TABLE sessions (token_digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), auth_version BIGINT NOT NULL, created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL);
CREATE INDEX sessions_user ON sessions(user_id);
CREATE INDEX sessions_expiry ON sessions(expires_at);
CREATE TABLE dashboard_preferences (user_id TEXT PRIMARY KEY REFERENCES users(id), revision BIGINT NOT NULL DEFAULT 0, preferences_json TEXT);
CREATE TABLE login_attempts (scope TEXT PRIMARY KEY, window_started DOUBLE PRECISION NOT NULL, attempts BIGINT NOT NULL);
CREATE TABLE account_schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
INSERT INTO account_schema_migrations VALUES ('001_keycloak', now()::text);
CREATE TABLE federated_identities (issuer TEXT NOT NULL, subject TEXT NOT NULL, user_id TEXT NOT NULL UNIQUE REFERENCES users(id), PRIMARY KEY (issuer, subject));
CREATE TABLE oidc_login_states (state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, verifier TEXT NOT NULL, nonce TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL);

-- Application configuration (published versions and the draft)
CREATE TABLE configuration_versions (revision BIGINT PRIMARY KEY, config_json TEXT NOT NULL, created_at TEXT NOT NULL, action TEXT NOT NULL);
CREATE TABLE configuration_state (id BIGINT PRIMARY KEY CHECK(id = 1), active_revision BIGINT NOT NULL, draft_version BIGINT NOT NULL DEFAULT 0);
CREATE TABLE configuration_draft (id BIGINT PRIMARY KEY CHECK(id = 1), config_json TEXT NOT NULL, base_revision BIGINT NOT NULL);

-- BI definitions, access and personal dashboards
CREATE TABLE bi_schema_migrations (version BIGINT PRIMARY KEY, applied_at TEXT NOT NULL);
INSERT INTO bi_schema_migrations SELECT version, now()::text FROM generate_series(1, 10) AS version;
CREATE TABLE dashboards (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE dashboard_widgets (id TEXT PRIMARY KEY, dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX widget_dashboard ON dashboard_widgets(dashboard_id);
CREATE TABLE dashboard_assignments (dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE, assignment_type TEXT NOT NULL, assignment_id TEXT NOT NULL, PRIMARY KEY(dashboard_id,assignment_type,assignment_id));
CREATE TABLE metric_definitions (id TEXT PRIMARY KEY, metric_key TEXT NOT NULL UNIQUE, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE kpi_definitions (id TEXT PRIMARY KEY, metric_key TEXT NOT NULL UNIQUE, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE roles (id TEXT PRIMARY KEY, role_key TEXT NOT NULL UNIQUE, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE user_access (id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE bi_state (id BIGINT PRIMARY KEY CHECK(id=1), seeded BIGINT NOT NULL DEFAULT 0, configuration_revision BIGINT NOT NULL DEFAULT 0);
INSERT INTO bi_state VALUES (1, 0, 0);
CREATE TABLE dashboard_override_revisions (dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE, user_id TEXT NOT NULL, revision BIGINT NOT NULL, PRIMARY KEY(dashboard_id,user_id));
CREATE TABLE dashboard_personal_widgets (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX personal_widget_owner ON dashboard_personal_widgets(dashboard_id,user_id);
CREATE TABLE dashboard_user_overrides (id TEXT PRIMARY KEY, dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE, widget_id TEXT NOT NULL, user_id TEXT NOT NULL, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(dashboard_id,widget_id,user_id));
CREATE TABLE bi_bootstrap_versions (version BIGINT PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE analytics_cache_jobs (id TEXT PRIMARY KEY, requested_by TEXT, request_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','running','complete','failed')), completed_queries BIGINT NOT NULL DEFAULT 0, total_queries BIGINT NOT NULL, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, lease_owner TEXT, lease_expires_at DOUBLE PRECISION, deadline_at DOUBLE PRECISION);
CREATE INDEX cache_jobs_active ON analytics_cache_jobs(status,lease_expires_at);
CREATE TABLE kpi_policy_versions (rule_set_id TEXT NOT NULL, version BIGINT NOT NULL, definition_json TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT, created_at TEXT NOT NULL, created_by TEXT, PRIMARY KEY(rule_set_id,version));
CREATE INDEX kpi_policy_validity ON kpi_policy_versions(valid_from,valid_to);
CREATE TABLE weight_sets (id TEXT PRIMARY KEY, weight_set_key TEXT NOT NULL UNIQUE, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO weight_sets VALUES
('target_equivalent','target_equivalent',1,'{"id":"target_equivalent","key":"target_equivalent","title":"Еквівалент цілей","revision":1}',to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"')),
('complexity','complexity',1,'{"id":"complexity","key":"complexity","title":"Складність","revision":1}',to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"')),
('priority','priority',1,'{"id":"priority","key":"priority","title":"Пріоритет","revision":1}',to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"')),
('cost_equivalent','cost_equivalent',1,'{"id":"cost_equivalent","key":"cost_equivalent","title":"Еквівалент витрат","revision":1}',to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'));
CREATE TABLE category_weights (id TEXT PRIMARY KEY, weight_set_id TEXT NOT NULL REFERENCES weight_sets(id) ON DELETE RESTRICT, category TEXT NOT NULL, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(weight_set_id, category));
CREATE TABLE personal_dashboard_preferences (user_id TEXT PRIMARY KEY, revision BIGINT NOT NULL, definition_json TEXT NOT NULL, migrated BIGINT NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE dashboard_assets (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, content_type TEXT NOT NULL, byte_size BIGINT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX dashboard_asset_owner ON dashboard_assets(user_id);
CREATE TABLE dashboard_asset_content (id TEXT PRIMARY KEY REFERENCES dashboard_assets(id) ON DELETE CASCADE, content BYTEA NOT NULL);
