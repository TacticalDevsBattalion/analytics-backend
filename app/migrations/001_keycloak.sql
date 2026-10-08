CREATE TABLE IF NOT EXISTS federated_identities (
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    user_id TEXT NOT NULL UNIQUE REFERENCES users(id),
    PRIMARY KEY (issuer, subject)
);
CREATE TABLE IF NOT EXISTS oidc_login_states (
    state_hash TEXT PRIMARY KEY,
    browser_hash TEXT NOT NULL,
    verifier TEXT NOT NULL,
    nonce TEXT NOT NULL,
    expires_at REAL NOT NULL
);
