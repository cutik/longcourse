# Garmin Connect

Since 2026-10-25 the watch is a Garmin. The add-on pulls Garmin Connect
directly, every hour, in the same process that serves `/ingest` — no cron, no
second container. The Apple path (HAE pushes, `export.xml`, GPX) is untouched
and keeps running: the iPhone still feeds steps and gait through HAE.

Why direct and not through Apple Health: Garmin's pool-swim FIT file has one
message per length — active or idle, strokes, stroke style. Through HealthKit
all of that is lost. And the Garmin → Apple Health sync is unreliable anyway:
it delivered 2026-10-25..27 and then stopped.

## First-time setup

1. Deploy v0.5.0+ (bump is in `config.yaml`; *Check for updates → Update*).
2. Log in once, interactively. The password is used for this call only; what
   persists is a refreshable token in `/data/garmin` (owner-only, survives
   add-on updates):

   ```bash
   # on the Pi, in a shell that can reach Docker
   docker exec -it app_local_longcourse python -m importers.garmin --login
   ```

   Garmin may ask for an MFA code; the prompt waits for it.
3. Backfill from the day the watch arrived:

   ```bash
   docker exec app_local_longcourse python -m importers.garmin --since 2026-10-20
   ```
4. Add-on *Configuration*: `garmin_enabled: true`, restart. The log shows
   `garmin pull: {...}` within a minute, then once an hour.
5. `curl http://192.168.50.221:8787/health` — `primary` is `garmin`, and
   `providers.garmin.age_hours` is small.

The SSH add-on's shell has no Docker access (`permission denied` on the
socket). Use the HA host console, or the *Advanced SSH & Web Terminal* add-on
with protection mode off.

## What comes in

| Garmin | Lands in | Notes |
|---|---|---|
| activity summary + original FIT | `workouts`, id `garmin:<activityId>` | sport from `activityType.typeKey` (stable English key, not the localised name) |
| FIT `length`, `length_type=active` | `workout_laps`, with `stroke_style` | idle lengths are rests and are excluded → `swolf_method='lap'` |
| FIT `record` | `workout_samples` (`heartRateData`, per minute) | wrist HR reads under water |
| FIT GPS records | `route_points` / `workout_routes` | open water, runs, walks |
| `get_stats` | `observations`: steps, distance, floors, kcal, RHR, respiration, stress, Body Battery, intensity minutes | stamped at local midnight of the calendar day |
| `get_hrv_data` | `observations.hrv_rmssd` | **not** `hrv_sdnn` — see below |
| `get_sleep_data` | `sleep_segments` (`inbed`, `core`, `deep`, `rem`, `awake`) | Garmin "light" = `core` |
| `get_body_composition` | `observations.weight`, `body_fat` | only Garmin-scale/manual weigh-ins |

Every response is archived first: JSON in `raw_payloads` (`kind='garmin'`),
FIT files in `/share/archive/garmin/fit/<id>.fit`, registered in `raw_files`.
After a parser change, rebuild without touching the network:

```bash
docker exec app_local_longcourse python -m importers.garmin --replay
```

## Data reality (verified 2026-11-05)

- **`poolLength` in the activity summary is centimetres** (`5000.0`, with
  `unitOfPoolLength.factor: 100`). The FIT session's `pool_length` is metres.
  The importer takes the FIT's.
- **The profile timezone is wrong** (`Europe/Paris` on this account, while
  every reading carries Kyiv's +2). Workout timezones come from the watch's own
  `startTimeLocal − startTimeGMT` (`tz_source='device'`).
- **HRV is overnight RMSSD**, Apple's was SDNN of spot readings. Different
  statistics; the first Garmin nights read 15–26 ms. Separate metric, separate
  line on the Sleep dashboard.
- **SpO2 is empty** unless Pulse Ox is switched on on the watch.
- **A length is only as accurate as the lap button.** A rest without pressing
  it is folded into the next "active" length (seen: 210 s for 50 m); a missed
  turn splits one length in two (seen: 26 s). Garmin's SWOLF uses the same
  formula as ours — 97.7 vs Garmin's displayed 98 on 2026-11-02.
- **Strokes per length ~26**, about the same as Apple Watch and also below a
  hand count. Do not compare across watches anyway — see below.

## Two providers, one set of dashboards

- **Duplicates.** Garmin Connect writes into Apple Health, so a session can
  arrive twice: from the pull, and via HAE as `provider='apple'`, source
  `Connect`. `derive.mark_superseded` flags the lower-priority copy
  (`superseded_by`), and every view skips flagged rows. Nothing is deleted.
- **Daily metrics and sleep** come from one provider per day:
  the highest-ranked in `provider_priority` that has data that day (Garmin 1,
  Apple 2). Metrics only the iPhone records still come from Apple.
- **Never compare SWOLF or strokes across watches.** The Swim dashboard has a
  *Watch* variable (default `garmin`) on those panels. Pace and distance span
  both — both measure active time from per-length splits.

### Switching back to an Apple Watch

```sql
UPDATE provider_priority SET rank = CASE provider WHEN 'apple' THEN 1 ELSE 2 END;
```

then `garmin_enabled: false`, and `python -m derive --all` to re-point the
duplicate flags. The Watch variable on the Swim dashboard selects `apple`.

## When the pull breaks

`/health` → `providers.garmin_error.note` has the last exception. Typical:

- **Token expired / login flow changed.** Run `--login` again. If that fails
  too, check the `python-garminconnect` releases — Garmin changes its SSO
  without notice — bump the pin in `requirements.txt` and the add-on version.
- **429 Too Many Requests.** Leave it; the loop retries next round. Do not
  lower `garmin_poll_minutes` below 60.

The pull's lookback (`garmin_lookback_days`, default 3) re-fetches recent days
every round, so a few hours of failure lose nothing. For a longer outage, run
`--since <first missed day>` once it works again.
