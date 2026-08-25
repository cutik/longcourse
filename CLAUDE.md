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

   **Caveat added later:** that conclusion came from HAE's output, which is not
   the same thing as HealthKit's. The native `export.xml` emits
   `<WorkoutEvent type="HKWorkoutEventTypeLap">`, and `--scan` reports whether
   this archive actually contains them. If it does, `workout_laps` fills and
   `swolf_method='lap'` sessions are measured rather than estimated. Unverified
   until the first real archive is scanned — do not assume either way.

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
select sport, is_indoor, pool_length_m, count(*),
       round(avg(swolf)::numeric,1) swolf,
       round(avg(moving_s/nullif(duration_s,0))::numeric,2) ratio,
       round(avg(pace_s_per_100m)::numeric) pace
from workouts group by 1,2,3 order by 4 desc;
```

Expected on current data: `swim | t | 50 | 94 | ~81 | ~0.72 | ~113`.

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

Working: ingest end-to-end (LAN and tunnel), Access policies, schema
auto-apply, all six MCP tools.

Built but **not yet run against real data or a real database** — everything
below was developed offline, and the SQL in particular has never been executed:

- `app/importers/apple_xml.py` — streaming `export.xml` importer, `--scan` and
  `--import`. Verified against a synthetic fixture only
- `app/derive.py` — in-place recompute of derived columns
- `app/db.py` — COPY-into-staging batched upserts, replacing the per-row
  round-trips that made a backfill take hours
- `observations` / `sleep_segments` / `metric_meta` / `raw_files` /
  `workout_laps` tables, and six Grafana views
- `grafana/dashboards/*.json` — four dashboards, generated by
  `grafana/build_dashboards.py`
- `tests/test_parsers.py` — 45 checks, the project's first tests

First real run should follow `docs/backfill.md`.

### Not done yet

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

6. **GPX routes.** Exports include per-workout `.gpx` files for open-water
   swims, walks and runs (pool swims have none). `workout_routes` exists but is
   empty. **The matching problem:** GPX filenames carry a localised activity
   name and a timestamp, no workout UUID — so linking must be done by start
   time and sport. Expect timestamp skew between filename and workout `start`.

### Open questions

- Does HAE's scheduled export backfill windows missed while the phone was
  locked, or are they lost? Determines whether monthly manual exports stay
  necessary.
- Is the top-quartile pace estimate stable on interval sessions, or does it
  need a different estimator once structured sets appear in the data?

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
`v_training_load`, `v_sessions`, `v_source_rank`) all expose a `time` column so
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
