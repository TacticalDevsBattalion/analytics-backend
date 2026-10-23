-- Which application session belongs to which K2 Auth login (back-channel logout).
CREATE TABLE federated_sessions (token_digest TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL, sid TEXT);
CREATE INDEX federated_sessions_sid ON federated_sessions(issuer, sid);
CREATE INDEX federated_sessions_subject ON federated_sessions(issuer, subject);
INSERT INTO account_schema_migrations VALUES ('002_federated_sessions', now()::text) ON CONFLICT DO NOTHING;
