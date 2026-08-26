# CLAUDE.md

Context for working on this repo. Read before changing the parser or schema —
several findings here were expensive to discover and are not obvious from the
code alone.

## What this is

A personal swimming coach pipeline. Apple Health → Postgres → MCP, so an agent
can analyse training history and eventually write training plans. Replaces a
paid service (athletedata.health, $69/yr) whose swim support was thin anyway.

The user swims in a **50m pool** (long course — hence the name). Roughly 2km
per session, 2-3 times a week, tracked on Apple Watch.

## Topology

Everything runs at home. Nothing is rented.

- **Home Assistant OS** on a Raspberry Pi 5. Because it's HAOS, a plain
  docker-compose next to it is not viable — Supervisor owns Docker. This runs
  as a **local add-on** in `/addons/longcourse/`.
- **Postgres 17** already ran there for TeslaMate (add-on container
  `app_db21ed7f_postgres_latest`, image `ghcr.io/alexbelgium/postgres_17-aarch64`).
  Reused, but with its **own database and role** (`longcourse`/`longcourse`) so
  a TeslaMate restore can never touch health data. No TimescaleDB — vanilla
  Postgres only. At this data volume (thousands of rows/year) hypertables
  bought nothing.
- **One container, one process.** FastAPI serves `/ingest` and `/health`;
  FastMCP is mounted at `/mcp` on the same port (8000 inside, 8787 on the host).
- **Cloudflare Tunnel** was already set up for `cutik.info` → Home Assistant.
  This added `lc.cutik.info` → `172.30.33.11:8000` on the same tunnel.

### Why not Tailscale

Originally planned, then dropped: the tunnel already existed. Note for future
reasoning — the reason a *plain* VPN (the user's existing OpenVPN on the
router) doesn't work is that iOS won't keep a normal app's tunnel up in the
background, so the hourly export would fire into a closed connection. That
constraint applies to OpenVPN, not to Cloudflare.

### Access control

Three Cloudflare Access applications on `lc.cutik.info`, default-deny:

| App | Path | Policy |
|---|---|---|
| `longcourse-ingest` | `ingest` | `svc-hae-iphone` — **Service Auth**, service token |
| `longcourse-mcp` | `mcp` | `me-only` — Allow, email |
| `longcourse-web` | (empty) | `me-only` — Allow, email |

`/ingest` uses Service Auth because the iPhone posts in the background and
cannot do an interactive login. Requests carry `CF-Access-Client-Id` and
`CF-Access-Client-Secret` **plus** the app's own `Authorization: Bearer`.

**Cloudflare's free plan caps request bodies at 100 MB.** A one-year export is
~122 MB and returns 413. Large backfills go in over the LAN
(`http://172.30.33.11:8000/ingest`, no CF headers) after dropping the file into
the HA `share` Samba folder, or get split into smaller periods.

## Data reality — read this before touching the parser

Health Auto Export's actual output differs from its documentation in ways that
silently produce wrong or empty results.

1. **Workout names and locations are localised.** The user's phone is
   Ukrainian, so swims arrive as `"Басейн Плавання"` / `"Відкрите плавання
   Плавання"`, location `"Басейн"`. An `ILIKE '%swim%'` filter returns zero
   rows. `classify()` derives sport from **structural signals** — the presence
   of `swimDistance`, `swimStroke`, `swimCadence`, `totalSwimmingStrokeCount`,
   `lapLength` — and pool vs open water from the boolean `isIndoor`. Never
   reintroduce string matching on user-visible fields.

2. **`lapLength` has a unit bug.** A 50m pool exports as
   `{"units":"m","qty":0.05}` — the value is kilometres, the label says metres.
   `pool_length_m()` repairs this: a sub-1 value tagged as metres is
   unambiguously km, since no pool is under a metre. `speed` is similarly
   mislabelled (`2.25 m/hr`) and is ignored entirely — pace is computed.

3. **There is no `swolfScore`.** Despite HAE's docs listing it. SWOLF is
   computed here: `seconds per length + strokes per length`.

4. **`swimStroke`, `swimDistance`, `heartRateData`, `activeEnergy` are
   per-minute time series**, not scalars. They land in `workout_samples`.
   In HAE output there are **no per-length splits** — the finest structure is
   per-minute buckets, and they must not be presented as 50m splits.

   **Corrected 2026-08-25 — this held for HAE only.** The conclusion came from
   HAE's output, which is not the same thing as HealthKit's. Scanning the real
   native archive found **16,867 `HKWorkoutEventTypeLap` events across 386 of
   441 swims**. Per-length splits do exist; HAE simply drops them.

   So for those sessions lengths and active time are *counted*, and SWOLF is a
   measurement. `swolf_method` records which path produced a figure — `lap` or
   `estimated` — and the two must never be averaged or compared. The
   top-quartile estimator still covers the ~55 swims with no lap events, and
   remains the only option for anything arriving through HAE.

   Still absent: `HKMetadataKeySwimmingStrokeStyle` is not present anywhere in
   the archive (0 entries), so there is no per-stroke breakdown.

   **Metadata key names differ from the documentation.** The real `export.xml`
   writes `HKLapLength`, `HKIndoorWorkout`, `HKSwimmingLocationType`,
   `HKExternalUUID` — the short forms — not the `HKMetadataKey*` constants that
   HealthKit's API and Apple's docs use, and that HAE echoes. Reading only the
   long form nulled pool length and indoor/outdoor across the whole history.
   The importer now reads both; the test fixture uses the short forms so a
   regression is caught. Pool length is also recoverable from lap splits
   (`distance / lap count`) when a workout carries neither key.

5. **Energy is in kJ** despite field names implying kcal. `kcal()` converts.

6. **`heartRateData` buckets use capitalised `Min`/`Avg`/`Max`** instead of
   `qty`, unlike every other series.

### Active time and why SWOLF is an estimate

`duration_s` includes rest at the wall. Raw SWOLF over elapsed time measures
"time spent in the building", not technique.

First attempt filtered buckets below a 5m threshold. Measured on real data it
removed **7%** of elapsed time — useless, because at per-minute resolution even
30 seconds of rest leaves 20+ metres in the bucket.

Current approach inverts it: take the **top quartile of buckets** as
representative of true swimming speed, then `moving_s = distance / that speed`.
Bucket boundaries stop mattering. On real data this gives `ratio ≈ 0.72`,
pace ~113 s/100m (vs 148 gross), SWOLF ~81 (vs 106 gross).

**This is an estimate, not a measurement.** A session containing one all-out
interval will pull the estimate high and understate active time. Both `swolf`
and `swolf_gross` are stored so the difference stays visible.

### Stroke counts are systematically low

Apple Watch reports ~22-27 strokes per 50m length; the user counts 30-40 by
hand. Most likely the watch misses strokes during push-off and glide. The bias
appears consistent, so the metric is fine for tracking change over time, but
**absolute values should not be compared against hand counts or figures from
elsewhere**. Reported `swimCadence` (~19/min) is likewise computed over elapsed
time and is not a real stroke rate.

### Never compare SWOLF across pool lengths

`pool_length_m` is a first-class column for this reason. A 25m course halves
seconds-per-length at identical technique. There is exactly one 25m session in
the history (genuine, not a watch misconfiguration) — it must stay out of
trend comparisons. `swim_sessions` takes a `pool_length_m` filter; default to 50.

## Schema

Two layers. The raw layer keeps whatever the provider called things; the
canonical layer is what dashboards read. Raw input is always the source of
truth — this is what made three parser rewrites cheap.

**Raw landing**

- `raw_payloads` — full HAE request bodies, archived before parsing
- `raw_files` — big archives are *not* inlined here: a `jsonb` value tops out
  near 255 MB and a full `export.xml` is larger. The file stays on `/share` and
  is registered by digest, so a replay reads it back off disk
- `metrics` — non-workout data under HAE's own names, PK `(name, ts, source)`
- `workout_samples` — per-workout time series `(workout_id, metric, ts)`. One
  table on purpose, so new export fields need no migration
- `workout_laps` — per-length splits, if `export.xml` turns out to carry
  `HKWorkoutEventTypeLap` (see "Per-length splits" below)
- `workout_routes` / `route_points` — GPX geometry: one summary row per route,
  one row per GPS fix. Filled by `app/importers/routes.py`
- `ingest_log` — feeds `/health` and the HA watchdog

**Canonical**

- `observations` — unit-normalised, provider-tagged readings under the names in
  `app/canon.py`, with `local_day` precomputed
- `metric_meta` — materialised copy of `app/canon.py`, so SQL can join on
  whether a metric is cumulative or instantaneous
- `sleep_segments` — sleep does not fit `observations` (HAE sends a structure,
  not a quantity), so stages get their own table
- `workouts` — shared by both layers, with `provider` and `external_id`

Everything upserts. HAE resends overlapping windows on every run.

### Normalisation rules worth knowing

- **Never sum a cumulative metric across sources.** iPhone and Watch both record
  steps and energy for the same day; adding every row roughly doubles the real
  figure. `v_daily_metrics` picks one source per day for cumulative metrics and
  averages across sources for instantaneous ones. The source is chosen by
  coverage, not by name — device names are user-set and localised.
- **Days are cut in local time, not UTC.** At Kyiv's +03 it is the early-morning
  readings that UTC misfiles: 01:30 local is 22:30 UTC the previous day. Hence
  `local_day` columns and the database-level `timezone` setting.
- **Sleep is attributed to the morning you wake up on**, with the cut at 18:00
  local so afternoon naps stay on their own day.
- **`swolf_method` says how a SWOLF was produced** — `lap` (counted) or
  `estimated` (top-quartile model). Never average across the two.

## Development workflow

**Supervisor caches images by version tag.** Editing code and hitting Rebuild
often runs the *old* image, which cost an hour of debugging a bug that was
already fixed. Always bump `version:` in `config.yaml`, then Check for updates
→ Update.

```bash
# on the Pi
cd /addons/longcourse && git pull
# HA: Apps → ⋮ → Check for updates → Longcourse → Update → Start
# expect "schema applied from /app/schema.sql" then "Uvicorn running"
```

Schema is applied automatically at boot from the copied `schema.sql`. A missing
file is **fatal on purpose** — an earlier version skipped it silently and
produced a server that started cleanly then 500'd on every query.

### Re-deriving after a parser change

Derived columns are still computed at ingest, but changing a formula no longer
means truncating and re-POSTing a 122 MB file — which stopped being viable once
the full history was loaded. Recompute in place from data already in Postgres:

```bash
docker exec addon_local_longcourse python -m derive --all
# or just the recent end
docker exec addon_local_longcourse python -m derive --since 2026-01-01
```

It is idempotent and reports how many sessions used real lap splits versus the
estimate. The old truncate-and-replay route still works if the *parser*, rather
than the derivation, changed — the archived payloads are all in `raw_payloads`.

Run the tests first; they cover the conversions that fail quietly:

```bash
python3 tests/test_parsers.py
```

### Sanity check after any parser change

```sql
select sport, is_indoor, pool_length_m, swolf_method, count(*),
       round(avg(swolf)::numeric,1) swolf,
       round(avg(moving_s/nullif(duration_s,0))::numeric,2) ratio,
       round(avg(pace_s_per_100m)::numeric) pace
from workouts group by 1,2,3,4 order by 5 desc;
```

Expected after the full backfill (measured from lap splits, 2026-08-25):
`swim | t | 50 | lap | 385 | swolf ~88 | ratio ~0.82 | pace ~129`, plus one
genuine 25m session and ~50 open-water swims with no pool length.

These differ from the pre-backfill figures (`~81 / ~0.72 / ~113` over 94 HAE
sessions) because those were the top-quartile *estimate*; the lap-measured
active time is higher, so the ratio rose and the pace is honestly slower. That
is the estimate understating active time, exactly as warned above — not a
regression. Group by `swolf_method`; never compare a `lap` figure to an
`estimated` one.

## MCP design

The server exposes **questions, not tables**. Six tools: `sync_status`,
`swim_sessions`, `session_detail`, `recovery_window`, `compare_blocks`,
`css_inputs`. Each returns small, pre-aggregated payloads.

**Do not add a generic `run_sql` tool.** It looks flexible but moves
aggregation into the context window, where it is most expensive, and lets the
agent pull thousands of raw sample rows.

Tool docstrings carry the interpretation caveats (rest handling, pool-length
comparability, "these are whole sessions not timed sets") because that's where
the agent actually reads them.

```bash
claude mcp add --transport http longcourse http://<ha-lan-ip>:8787/mcp
```

claude.ai on web/mobile can reach `https://lc.cutik.info/mcp`, but that path is
behind an email-login Access policy — fine for a browser, not for a headless
client. Revisit if remote agent access is wanted.

## Status

Working end to end. Full-history backfill and normalisation done 2026-08-25;
Grafana dashboards imported and live 2026-08-26.

- **Ingest**: HAE push path (LAN + tunnel), Access policies, schema auto-apply,
  all six MCP tools. `store()` now writes both the raw landing tables and the
  canonical layer, batched.
- **Backfill**: the native `export.xml` (1.51 GB) was imported straight from a
  laptop into the Pi's Postgres — the DB is reachable on the LAN
  (`192.168.50.221:5432`), so the archive never had to be copied to the Pi. Ran
  in ~4 min. Result: **1274 workouts** (441 swims, 611 walks, 166 runs, plus
  dive/ski/cardio), **3.17M observations**, **40,754 sleep segments**, **16,867
  lap splits**, covering **2022-10-18 → 2026-08-25**. Import and derive are
  idempotent — re-running the same file does not grow the counts.
- **Swim derivation**: 385 indoor 50m swims measured from lap splits
  (`swolf_method='lap'`), 1 genuine 25m, ~50 open-water (no pool, no SWOLF, by
  design), 3 pool swims without laps or a usable series.
- **Grafana**: five dashboards (Swim, Open Water, Training load, Sleep, Body)
  imported into the existing TeslaMate instance via
  **Dashboards → Import** (the classic JSON in `grafana/dashboards/*.json` — the
  instance is new enough that pasting into a new dashboard's *JSON model* editor
  fails on the v2 `dashboard.grafana.app` schema; Import migrates it). Datasource
  is the read-only `longcourse_ro` role (`db/grafana_role.sql`), added through
  the UI so a TeslaMate add-on update cannot overwrite it.
- **Tests**: `tests/test_parsers.py`, ~50 checks, the project's first. Run
  before any parser or derivation change: `python3 tests/test_parsers.py`.

Two bugs were found during the first real import and are now fixed + tested:
the streaming parser emptied `<Workout>` children before use (dropped laps and
HR stats), and the real archive uses **short** metadata keys (`HKLapLength`,
`HKIndoorWorkout`) not the documented `HKMetadataKey*` forms (nulled pool length
and indoor/outdoor). See "Data reality" for the key-name detail.

Backfill runbook: `docs/backfill.md`. DB connection and the `longcourse_ro`
password are not in the repo — they live in the operator's notes.

### Deferred by decision (2026-08-25)

The MCP / LLM-coach direction is **on hold**. The six MCP tools still work, but
the focus is a trustworthy, visible data foundation first — full history,
normalisation, Grafana — not the agentic coach the repo was originally aimed at.
Do not build out MCP tools, the weekly `claude -p` loop, or `ha-mcp` unless
asked. A **Samsung Health** archive is expected (earlier, non-overlapping
period → appends as `provider='samsung'`, no dedup); `app/importers/samsung.py`
and its `canon.py` aliases are the remaining work there.

### Not done yet

Items 3 (`ha-mcp`) and 5 (weekly coach loop) fall under the deferral above —
listed for completeness, not queued. 1, 2 and 6 are live infrastructure.

1. **Scheduled export.** HAE → Automations → REST API, hourly, all metrics +
   workouts, to `https://lc.cutik.info/ingest` with the three headers. Deferred
   until a fresh swim exists to verify against. Note iOS only runs these while
   the phone is unlocked — the pipeline is eventually-consistent by design, and
   HAE may not resend windows that failed. Compare session counts against the
   Health app after a week; a monthly manual Quick Export covers any gaps.

2. **Watchdog sensor.** REST sensor in HA against `/health`, alert when
   `age_hours > 12`. Add the automation *after* the first successful scheduled
   sync or it fires immediately.

   ```yaml
   rest:
     - resource: http://172.30.33.11:8000/health
       scan_interval: 1800
       sensor:
         - name: Longcourse sync age
           value_template: "{{ value_json.age_hours | default(999) }}"
           unit_of_measurement: h
   ```

3. **`ha-mcp`** (HACS → `homeassistant-ai/ha-mcp`) so the same conversation can
   see bedroom climate, presence, smart-scale weight and calendar — context
   the paid service can't have.

4. **CSS test.** No maximal 400m/200m time trial exists yet, so training zones
   are guesswork. `css_inputs` currently returns whole sessions, which are not
   time trials. Do this on a fresh session, not the first one after a pipeline
   change.

5. **Weekly coach loop.** Cron on the Pi → `claude -p` headless, reading this
   MCP plus a plan file in git, writing the next microcycle and notifying
   through HA. Worth building only after a few weeks of automated data.

6. **GPX routes — done (2026-08-26).** `app/importers/routes.py` imported all
   388 tracks (763,565 points): 45 open-water swims, 341 walks, 2 runs. The
   archive links each route to its workout directly via
   `<WorkoutRoute><FileReference path>`, so there was no filename matching to do
   — the HAE-era worry did not apply. Geometry is in `route_points`; summary and
   bounding box in `workout_routes`. The **Open Water** dashboard maps them.

### Open questions

- Does HAE's scheduled export backfill windows missed while the phone was
  locked, or are they lost? Determines whether monthly manual exports stay
  necessary.
- Is the top-quartile pace estimate stable on interval sessions, or does it
  need a different estimator once structured sets appear in the data? **Largely
  moot now:** 386 of 441 swims carry real lap splits, so SWOLF is measured for
  them and the estimator only covers the ~3 pool swims with no laps plus any
  future HAE-only session. Keep it honest there, but it is no longer load-bearing.

## Grafana

Dashboards live in the Grafana that already ships with TeslaMate on the same Pi.
Nothing new is deployed for them.

- `db/grafana_role.sql` creates `longcourse_ro` with SELECT only. Use it for the
  datasource — a panel's `rawSql` is editable by anyone with dashboard edit
  rights, so the connection must not be able to write.
- Add the datasource **through the Grafana UI**, not by dropping a provisioning
  file. TeslaMate provisions its own datasource from a file, and an add-on
  update can overwrite that directory; a UI-created datasource lives in
  `grafana.db` and survives.
- Import `grafana/dashboards/*.json`. Each declares a `ds` datasource variable,
  so no UID editing is needed.
- Edit dashboards through `grafana/build_dashboards.py` and regenerate, rather
  than hand-editing JSON — the panels share query patterns that drift otherwise.

The views (`v_swim_sessions`, `v_daily_metrics`, `v_sleep_nights`,
`v_training_load`, `v_sessions`, `v_source_rank`, plus `v_route_points` and
`v_open_water_swims` for the map) expose a `time` column so
`$__timeFilter(time)` works. They are plain views, not materialised: at this
data volume the cost is negligible and a stale materialised view showing last
week's numbers would be worse than a slower query.

**The Swim dashboard defaults `pool_length_m` to 50 and this must stay.** There
is one genuine 25m session in the history and it will wreck any trend it enters.

## Secrets

`.env`-style values live in the add-on's Configuration tab (Supervisor writes
`/data/options.json`); the `options:` block in `config.yaml` holds **defaults
only** and is committed. A real password leaked into git this way once — keep
those fields empty. `.gitignore` includes `*.json` so HAE exports can sit in
the working tree without being committed.
