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


-- ============================================================================
-- v2 — normalisation layer (full history, multiple providers, Grafana)
--
-- Everything below is ADDITIVE. `metrics` and `workouts` stay exactly what they
-- were: the raw landing zone for Health Auto Export pushes. The canonical layer
-- sits on top, so the normalisation can be reworked by replaying local data
-- without re-exporting anything from the phone.
--
-- Provider model:
--   apple   — HAE pushes and the native export.xml backfill
--   samsung — one-off CSV archive covering an earlier, non-overlapping period
-- ============================================================================

-- Daily rollups must not split at UTC midnight. At Kyiv's +03 the readings that
-- get misfiled are the early-morning ones: a 01:30 local session is 22:30 UTC
-- the previous day, so a UTC `ts::date` files it under yesterday. Setting the
-- timezone on the database makes every existing `ts::date` correct at once.
-- Separate database from TeslaMate's, so this is contained. The `local_day`
-- columns below are still written explicitly, because a client (Grafana) can
-- override the session timezone.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET timezone = %L',
                   current_database(), 'Europe/Kyiv');
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'could not set database timezone — not the owner';
END $$;

-- ------------------------------------------------------------- raw archives --
-- raw_payloads keeps inlining HAE pushes: they are small and arrive over HTTP.
-- Big archives cannot go there — a jsonb value tops out around 255 MB and a
-- full export.xml is larger than that. Those stay on disk in /share/archive and
-- are registered here, so "raw is the source of truth" still holds: a replay
-- reads the file back off the disk.
CREATE TABLE IF NOT EXISTS raw_files (
    id          BIGSERIAL PRIMARY KEY,
    path        TEXT        NOT NULL,
    sha256      TEXT        NOT NULL UNIQUE,
    kind        TEXT        NOT NULL,          -- apple_xml | hae_json | samsung_zip
    bytes       BIGINT,
    covers_from TIMESTAMPTZ,
    covers_to   TIMESTAMPTZ,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_metrics   BIGINT DEFAULT 0,
    n_workouts  BIGINT DEFAULT 0,
    n_samples   BIGINT DEFAULT 0,
    note        TEXT
);

-- ------------------------------------------------------- provider columns --
ALTER TABLE metrics  ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'apple';
ALTER TABLE workouts ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'apple';

-- The id of an Apple workout is its HealthKit UUID. Other providers get
-- '<provider>:<their id>' so the primary key stays a single opaque string.
ALTER TABLE workouts ADD COLUMN IF NOT EXISTS external_id TEXT;
UPDATE workouts SET external_id = id WHERE external_id IS NULL;

-- Which rest-handling produced `swolf`. 'lap' means real per-length splits from
-- HKWorkoutEventTypeLap; 'estimated' means the top-quartile pace estimate, which
-- is an inference, not a measurement. Never average the two together.
ALTER TABLE workouts ADD COLUMN IF NOT EXISTS swolf_method TEXT;
UPDATE workouts SET swolf_method = 'estimated' WHERE swolf IS NOT NULL AND swolf_method IS NULL;

ALTER TABLE workouts ADD COLUMN IF NOT EXISTS local_day DATE;

CREATE UNIQUE INDEX IF NOT EXISTS workouts_provider_external_idx
    ON workouts (provider, external_id);
CREATE INDEX IF NOT EXISTS workouts_local_day_idx ON workouts (local_day DESC);

-- --------------------------------------------------------- metric metadata --
-- Mirrors app/canon.py so SQL can join against it. The dictionary lives in code
-- (it version-controls with the parser); this table is a materialised copy the
-- app upserts at boot.
--
-- kind drives how a day is rolled up:
--   cumulative    — summed over the day, ONE source only (see v_daily_metrics)
--   instantaneous — averaged, all sources fine (HRV, resting HR, weight)
CREATE TABLE IF NOT EXISTS metric_meta (
    metric TEXT PRIMARY KEY,
    kind   TEXT NOT NULL,
    unit   TEXT NOT NULL,
    label  TEXT
);

-- ------------------------------------------------------------ observations --
-- Canonical, unit-normalised, provider-tagged measurements. This is what the
-- dashboards read. `metrics` keeps whatever HAE happened to call things.
CREATE TABLE IF NOT EXISTS observations (
    provider  TEXT        NOT NULL,
    metric    TEXT        NOT NULL,       -- canonical name, see app/canon.py
    ts        TIMESTAMPTZ NOT NULL,
    local_day DATE        NOT NULL,
    value     DOUBLE PRECISION,
    unit      TEXT        NOT NULL,       -- canonical unit
    source    TEXT        NOT NULL DEFAULT '',
    PRIMARY KEY (provider, metric, ts, source)
);
CREATE INDEX IF NOT EXISTS observations_metric_day_idx ON observations (metric, local_day DESC);
CREATE INDEX IF NOT EXISTS observations_metric_ts_idx  ON observations (metric, ts DESC);

-- ------------------------------------------------------------------ sleep --
-- sleep_analysis arrives as a structure in `extra`, not a qty, so it does not
-- fit `observations`. Segments are stored as they come; nights are derived.
CREATE TABLE IF NOT EXISTS sleep_segments (
    provider   TEXT        NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at   TIMESTAMPTZ NOT NULL,
    stage      TEXT        NOT NULL,      -- inbed | awake | asleep | core | deep | rem
    source     TEXT        NOT NULL DEFAULT '',
    -- A night is attributed to the day you WAKE UP on: sleep starting 23:40
    -- Monday counts as Tuesday's night, which is how every sleep app reports it.
    local_day  DATE        NOT NULL,
    PRIMARY KEY (provider, started_at, stage, source)
);
CREATE INDEX IF NOT EXISTS sleep_segments_day_idx ON sleep_segments (local_day DESC);

-- ------------------------------------------------------------------- laps --
-- Per-length splits, if the native export turns out to carry them.
--
-- CLAUDE.md records that Apple Watch exports no per-length splits. That was
-- concluded from Health Auto Export's output, which is not the same thing as
-- HealthKit's: export.xml emits <WorkoutEvent type="HKWorkoutEventTypeLap"/>.
-- If those events are present this table fills and SWOLF becomes a measurement
-- instead of the top-quartile estimate; if they are not, it stays empty and
-- nothing else changes. Either way the assumption is now visible in the schema
-- rather than buried in a parser.
CREATE TABLE IF NOT EXISTS workout_laps (
    workout_id TEXT        NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
    idx        INTEGER     NOT NULL,      -- 1-based lap number within the session
    started_at TIMESTAMPTZ NOT NULL,
    duration_s DOUBLE PRECISION,
    distance_m DOUBLE PRECISION,
    stroke_style TEXT,
    PRIMARY KEY (workout_id, idx)
);
CREATE INDEX IF NOT EXISTS workout_laps_start_idx ON workout_laps (started_at);

-- ============================================================================
-- Views for Grafana.
--
-- Every view exposes a `time` column so $__timeFilter(time) works without the
-- panel author knowing the underlying column names. Views, not materialised
-- views: at a few thousand sessions and a few million observations this is
-- fast enough, and a materialised view would need a refresh job that could
-- silently go stale — a dashboard quietly showing last week's numbers is worse
-- than one that takes an extra 200 ms.
-- ============================================================================

-- Which source to trust for a metric, without matching on device names.
-- Device names are user-set and localised, so 'Apple Watch' is not something to
-- grep for. Ranking by how much a source has actually reported picks the
-- primary device on evidence instead of on a string.
CREATE OR REPLACE VIEW v_source_rank AS
SELECT provider, metric, source,
       ROW_NUMBER() OVER (PARTITION BY provider, metric
                          ORDER BY COUNT(*) DESC, source) AS rank
FROM observations
GROUP BY provider, metric, source;

-- Daily rollup. The important part is the cumulative branch: iPhone and Watch
-- both record steps and energy for the same day, so summing every row double
-- counts. Cumulative metrics take the single best-covered source; instantaneous
-- ones (HRV, resting HR, weight) average across sources, which is correct
-- because they are independent measurements of the same thing rather than
-- overlapping tallies of it.
CREATE OR REPLACE VIEW v_daily_metrics AS
WITH per_source AS (
    SELECT provider, metric, local_day, source,
           SUM(value) AS total, AVG(value) AS mean,
           MIN(value) AS lo, MAX(value) AS hi, COUNT(*) AS n
    FROM observations
    GROUP BY 1, 2, 3, 4
),
cumulative AS (
    SELECT DISTINCT ON (p.provider, p.metric, p.local_day)
           p.provider, p.metric, p.local_day,
           p.total AS value, p.lo, p.hi, p.n, p.source
    FROM per_source p
    JOIN metric_meta m ON m.metric = p.metric AND m.kind = 'cumulative'
    JOIN v_source_rank r ON r.provider = p.provider
                        AND r.metric   = p.metric
                        AND r.source   = p.source
    ORDER BY p.provider, p.metric, p.local_day, r.rank
),
instantaneous AS (
    SELECT p.provider, p.metric, p.local_day,
           SUM(p.mean * p.n) / NULLIF(SUM(p.n), 0) AS value,
           MIN(p.lo) AS lo, MAX(p.hi) AS hi, SUM(p.n) AS n,
           '(all sources)'::text AS source
    FROM per_source p
    JOIN metric_meta m ON m.metric = p.metric AND m.kind = 'instantaneous'
    GROUP BY 1, 2, 3
)
SELECT u.local_day AS time, u.provider, u.metric,
       mm.label, mm.unit, mm.kind,
       u.value, u.lo AS min, u.hi AS max, u.n AS samples, u.source
FROM (SELECT * FROM cumulative UNION ALL SELECT * FROM instantaneous) u
JOIN metric_meta mm ON mm.metric = u.metric;

-- All training, every sport, one row per session.
CREATE OR REPLACE VIEW v_sessions AS
SELECT started_at AS time, id, provider, sport, is_indoor,
       COALESCE(local_day, (started_at AT TIME ZONE 'Europe/Kyiv')::date) AS day,
       duration_s, moving_s, distance_m,
       distance_m / NULLIF(duration_s, 0) * 3.6 AS avg_kmh,
       active_kcal, total_kcal, avg_hr, max_hr, hr_recovery
FROM workouts;

-- Swim sessions. pool_length_m is exposed deliberately and every dashboard
-- built on this must filter on it: SWOLF sums seconds and strokes per length,
-- so a 25m course produces structurally smaller numbers than a 50m one at
-- identical technique. There is exactly one genuine 25m session in the history
-- and it will wreck any trend line it is allowed into.
--
-- swolf_method matters just as much. 'lap' is counted from real per-length
-- splits; 'estimated' is inferred from a top-quartile pace model. Comparing or
-- averaging across the two produces a number that means nothing.
CREATE OR REPLACE VIEW v_swim_sessions AS
SELECT started_at AS time, id, provider, is_indoor, pool_length_m,
       COALESCE(local_day, (started_at AT TIME ZONE 'Europe/Kyiv')::date) AS day,
       distance_m, duration_s, moving_s,
       moving_s / NULLIF(duration_s, 0) AS active_ratio,
       lengths, stroke_count,
       stroke_count / NULLIF(lengths, 0) AS strokes_per_length,
       swolf, swolf_gross, swolf_method, pace_s_per_100m,
       avg_hr, max_hr, hr_recovery
FROM workouts
WHERE sport = 'swim';

-- One row per night, attributed to the morning you woke up on.
--
-- asleep_s prefers the staged breakdown and falls back to the unspecified
-- bucket. Older watchOS wrote a single 'asleep' record; newer versions write
-- core/deep/rem. Adding both together on a night that has both would report
-- roughly twice the sleep actually had.
CREATE OR REPLACE VIEW v_sleep_nights AS
WITH seg AS (
    SELECT provider, local_day, stage,
           SUM(EXTRACT(epoch FROM (ended_at - started_at))) AS secs
    FROM sleep_segments
    WHERE ended_at > started_at
    GROUP BY 1, 2, 3
),
night AS (
    SELECT provider, local_day,
           COALESCE(SUM(secs) FILTER (WHERE stage = 'inbed'), 0)  AS inbed_s,
           COALESCE(SUM(secs) FILTER (WHERE stage = 'awake'), 0)  AS awake_s,
           COALESCE(SUM(secs) FILTER (WHERE stage = 'core'), 0)   AS core_s,
           COALESCE(SUM(secs) FILTER (WHERE stage = 'deep'), 0)   AS deep_s,
           COALESCE(SUM(secs) FILTER (WHERE stage = 'rem'), 0)    AS rem_s,
           COALESCE(SUM(secs) FILTER (WHERE stage = 'asleep'), 0) AS unspecified_s
    FROM seg GROUP BY 1, 2
)
SELECT local_day AS time, provider,
       inbed_s, awake_s, core_s, deep_s, rem_s,
       CASE WHEN (core_s + deep_s + rem_s) > 0
            THEN core_s + deep_s + rem_s ELSE unspecified_s END AS asleep_s,
       CASE WHEN (core_s + deep_s + rem_s) > 0 THEN 'staged' ELSE 'unspecified' END
            AS detail,
       CASE WHEN inbed_s > 0
            THEN (CASE WHEN (core_s + deep_s + rem_s) > 0
                       THEN core_s + deep_s + rem_s ELSE unspecified_s END) / inbed_s
       END AS efficiency
FROM night;

-- Weekly volume per sport, for the training-load view.
CREATE OR REPLACE VIEW v_training_load AS
SELECT date_trunc('week', COALESCE(local_day,
           (started_at AT TIME ZONE 'Europe/Kyiv')::date))::date AS time,
       sport,
       COUNT(*)                              AS sessions,
       SUM(distance_m)                       AS distance_m,
       SUM(COALESCE(moving_s, duration_s))   AS active_s,
       SUM(duration_s)                       AS elapsed_s,
       SUM(active_kcal)                      AS kcal,
       AVG(avg_hr)                           AS avg_hr
FROM workouts
GROUP BY 1, 2;
