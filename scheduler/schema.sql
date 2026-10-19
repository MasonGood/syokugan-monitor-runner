CREATE TABLE IF NOT EXISTS dispatch_slots (
    slot_id TEXT PRIMARY KEY,
    cron TEXT NOT NULL,
    scheduled_minute INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT NOT NULL,
    lease_until INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    error_code TEXT,
    github_run_id TEXT
);

CREATE INDEX IF NOT EXISTS dispatch_recovery
ON dispatch_slots(status, lease_until, created_at);
