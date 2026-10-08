ALTER TABLE analytics_cache_jobs ADD COLUMN lease_owner TEXT;
ALTER TABLE analytics_cache_jobs ADD COLUMN lease_expires_at REAL;
ALTER TABLE analytics_cache_jobs ADD COLUMN deadline_at REAL;
CREATE INDEX IF NOT EXISTS cache_jobs_active ON analytics_cache_jobs(status,lease_expires_at);
