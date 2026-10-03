-- Runs once on first Postgres start (docker-entrypoint-initdb.d).

CREATE SEQUENCE IF NOT EXISTS url_id_seq START 1;

CREATE TABLE IF NOT EXISTS urls (
    id         BIGINT PRIMARY KEY,
    code       TEXT        NOT NULL UNIQUE,      -- unique index = the redirect lookup path
    long_url   TEXT        NOT NULL,
    is_custom  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS urls_expires_idx ON urls (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS clicks (
    event_id   UUID PRIMARY KEY,                 -- makes the worker idempotent (at-least-once delivery)
    code       TEXT        NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    ip         TEXT,
    user_agent TEXT,
    country    TEXT,
    referrer   TEXT,
    visitor_id TEXT                              -- sha256(ip|ua) prefix, used for "unique users"
);
CREATE INDEX IF NOT EXISTS clicks_code_ts_idx ON clicks (code, ts);
