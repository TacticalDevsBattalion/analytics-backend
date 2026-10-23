-- Which application session belongs to which K2 Auth login (back-channel logout).
CREATE TABLE IF NOT EXISTS federated_sessions (
    token_digest TEXT PRIMARY KEY,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    sid TEXT
);
CREATE INDEX IF NOT EXISTS federated_sessions_sid ON federated_sessions(issuer, sid);
CREATE INDEX IF NOT EXISTS federated_sessions_subject ON federated_sessions(issuer, subject)
