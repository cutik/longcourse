-- Longcourse schema. Vanilla PostgreSQL 17 — no extensions required, so this
-- runs on the same instance TeslaMate uses without touching its setup.
--
-- Design rule: raw_payloads is the source of truth. Everything else is a
-- derived projection that can be dropped and rebuilt by replaying raw rows.
-- That is what lets us commit to a schema before we know Health Auto Export's
-- exact swim shape.
--
-- Run as the longcourse user, against the longcourse database:
--   psql -h <pg-host> -U longcourse -d longcourse -f schema.sql

-- ---------------------------------------------------------------- raw archive
CREATE TABLE IF NOT EXISTS raw_payloads (
    id          BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    body_sha256 TEXT        NOT NULL UNIQUE,   -- dedupe identical retries
    body        JSONB       NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_payloads_received_idx ON raw_payloads (received_at DESC);

-- ------------------------------------------------------------------- metrics
-- One row per (metric, timestamp, source). The primary key gives idempotent
-- re-ingest: HAE resends overlapping windows on every run.
CREATE TABLE IF NOT EXISTS metrics (
    name   TEXT        NOT NULL,
    ts     TIMESTAMPTZ NOT NULL,
    source TEXT        NOT NULL DEFAULT '',
    qty    DOUBLE PRECISION,
    units  TEXT,
    extra  JSONB,                              -- sleep phases, bp pairs, etc.
    PRIMARY KEY (name, ts, source)
);
CREATE INDEX IF NOT EXISTS metrics_name_ts_idx ON metrics (name, ts DESC);

-- ------------------------------------------------------------------ workouts
CREATE TABLE IF NOT EXISTS workouts (
    id              TEXT PRIMARY KEY,          -- HAE workout UUID
    name            TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    duration_s      DOUBLE PRECISION,
    distance_m      DOUBLE PRECISION,
    active_kcal     DOUBLE PRECISION,
    avg_hr          DOUBLE PRECISION,
    max_hr          DOUBLE PRECISION,

    -- pool_length_m is a first-class column on purpose: SWOLF is only
    -- comparable within one course length.
    pool_length_m   DOUBLE PRECISION,
    stroke_style    TEXT,
    swolf           DOUBLE PRECISION,
    stroke_count    DOUBLE PRECISION,
    swim_cadence    DOUBLE PRECISION,
    salinity        TEXT,

    raw             JSONB       NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workouts_name_start_idx ON workouts (name, started_at DESC);

-- ------------------------------------------------------- per-length (if any)
-- Populated only if HAE turns out to expose lap/length granularity.
-- An empty table is a valid answer and costs nothing.
CREATE TABLE IF NOT EXISTS swim_lengths (
    workout_id   TEXT NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
    idx          INT  NOT NULL,
    started_at   TIMESTAMPTZ,
    duration_s   DOUBLE PRECISION,
    distance_m   DOUBLE PRECISION,
    stroke_style TEXT,
    stroke_count DOUBLE PRECISION,
    swolf        DOUBLE PRECISION,
    avg_hr       DOUBLE PRECISION,
    raw          JSONB,
    PRIMARY KEY (workout_id, idx)
);

-- -------------------------------------------------------------- health check
CREATE TABLE IF NOT EXISTS ingest_log (
    id         BIGSERIAL PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_metrics  INT NOT NULL DEFAULT 0,
    n_workouts INT NOT NULL DEFAULT 0,
    n_lengths  INT NOT NULL DEFAULT 0,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS ingest_log_at_idx ON ingest_log (at DESC);
