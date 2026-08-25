-- Longcourse schema. Vanilla PostgreSQL 17 — no extensions required, so this
-- runs on the same instance TeslaMate uses without touching its setup.
--
-- Design rule: raw_payloads is the source of truth. Everything else is a
-- derived projection that can be dropped and rebuilt by replaying raw rows.
--
-- Shaped against real Health Auto Export v2 output, which differs from the
-- documented shape in ways worth stating:
--   * workout name/location are LOCALISED. Never match on them. Sport is
--     classified from structural signals (presence of swim fields, isIndoor).
--   * lapLength arrives as {"units":"m","qty":0.05} for a 50m pool — the value
--     is kilometres mislabelled as metres. Ingest repairs this.
--   * there is no swolfScore. SWOLF is computed here from distance, duration
--     and stroke count.
--   * swimStroke / swimDistance / heartRateData are per-minute TIME SERIES,
--     not scalars. They land in workout_samples.

-- ---------------------------------------------------------------- raw archive
CREATE TABLE IF NOT EXISTS raw_payloads (
    id          BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    body_sha256 TEXT        NOT NULL UNIQUE,
    body        JSONB       NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_payloads_received_idx ON raw_payloads (received_at DESC);

-- ------------------------------------------------------------------- metrics
CREATE TABLE IF NOT EXISTS metrics (
    name   TEXT        NOT NULL,
    ts     TIMESTAMPTZ NOT NULL,
    source TEXT        NOT NULL DEFAULT '',
    qty    DOUBLE PRECISION,
    units  TEXT,
    extra  JSONB,
    PRIMARY KEY (name, ts, source)
);
CREATE INDEX IF NOT EXISTS metrics_name_ts_idx ON metrics (name, ts DESC);

-- ------------------------------------------------------------------ workouts
CREATE TABLE IF NOT EXISTS workouts (
    id              TEXT PRIMARY KEY,

    -- sport is derived structurally, never from the localised name.
    sport           TEXT        NOT NULL,       -- swim | walk | run | other
    is_indoor       BOOLEAN,                    -- true = pool, for swims
    name_raw        TEXT,                       -- localised, kept for reference
    location_raw    TEXT,

    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    duration_s      DOUBLE PRECISION,
    distance_m      DOUBLE PRECISION,
    active_kcal     DOUBLE PRECISION,
    total_kcal      DOUBLE PRECISION,
    avg_hr          DOUBLE PRECISION,
    min_hr          DOUBLE PRECISION,
    max_hr          DOUBLE PRECISION,
    hr_recovery     DOUBLE PRECISION,           -- 1-min post-workout HR drop

    -- swim specifics
    pool_length_m   DOUBLE PRECISION,
    stroke_count    DOUBLE PRECISION,
    swim_cadence    DOUBLE PRECISION,

    -- computed at ingest, so queries stay cheap and consistent
    lengths         INTEGER,                    -- distance / pool_length
    moving_s        DOUBLE PRECISION,           -- active time, rest excluded
    swolf           DOUBLE PRECISION,           -- from moving_s (comparable)
    swolf_gross     DOUBLE PRECISION,           -- from duration_s (incl. rest)
    pace_s_per_100m DOUBLE PRECISION,           -- from moving_s

    raw             JSONB       NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workouts_sport_start_idx ON workouts (sport, started_at DESC);

-- ----------------------------------------------------------- sample series
-- Generic per-workout time series: swimStroke, swimDistance, heartRateData,
-- activeEnergy, and whatever future exports add. One table instead of one per
-- metric keeps GPX-era additions from needing a migration.
CREATE TABLE IF NOT EXISTS workout_samples (
    workout_id TEXT        NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
    metric     TEXT        NOT NULL,            -- swimStroke | swimDistance | ...
    ts         TIMESTAMPTZ NOT NULL,
    qty        DOUBLE PRECISION,
    units      TEXT,
    extra      JSONB,                           -- Min/Avg/Max for HR buckets
    PRIMARY KEY (workout_id, metric, ts)
);
CREATE INDEX IF NOT EXISTS workout_samples_metric_idx ON workout_samples (metric, ts DESC);

-- --------------------------------------------------------------- route data
-- Not populated yet. GPX files arrive as a separate export alongside the JSON;
-- they will be matched to workouts by start time and sport, since the filenames
-- carry a localised activity name and a timestamp but no workout UUID.
CREATE TABLE IF NOT EXISTS workout_routes (
    workout_id  TEXT PRIMARY KEY REFERENCES workouts(id) ON DELETE CASCADE,
    source_file TEXT,
    point_count INTEGER,
    started_at  TIMESTAMPTZ,
    points      JSONB,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -------------------------------------------------------------- health check
CREATE TABLE IF NOT EXISTS ingest_log (
    id         BIGSERIAL PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_metrics  INT NOT NULL DEFAULT 0,
    n_workouts INT NOT NULL DEFAULT 0,
    n_samples  INT NOT NULL DEFAULT 0,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS ingest_log_at_idx ON ingest_log (at DESC);
