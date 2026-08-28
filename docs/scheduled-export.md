# Automating the daily export (Health Auto Export)

The backfill loaded history up to the day it ran; from here on the pipeline
stays current through **Health Auto Export (HAE)** pushing on a schedule. Apple's
own "Export All Health Data" cannot be automated (no API, no Shortcut), so HAE is
the only automatic path — see `docs/backfill.md` for why the two coexist.

## Prerequisite: deploy the current add-on first

**Do this before turning on the automation.** The pre-0.4 add-on's ingest wrote
only the raw `metrics` table, not the canonical `observations` / `sleep_segments`
layer the dashboards read. If HAE pushes land on the old code, the raw data
arrives but the HRV / steps / sleep / weight dashboards stay flat.

```bash
# on the Pi
cd /addons/longcourse && git pull
# HA: Settings -> Add-ons -> Longcourse -> ... -> Check for updates -> Update -> Start
# log should show: schema applied … then Uvicorn running
```

Confirm the receiving end is healthy:

```bash
curl http://192.168.50.221:8787/health
# {"ok":true,"last_ingest":"…","age_hours":…}
```

## The HAE automation

On the iPhone, in Health Auto Export:

**Automations → add one → REST API**

| Setting | Value | Why |
|---|---|---|
| Format | **JSON** | the only format the ingest parses |
| Data | **all metrics + all workouts** | workouts carry the swim series; metrics feed the daily dashboards |
| Aggregation | **per day** (metrics) | keeps each push small; `v_daily_metrics` wants one point per day anyway. Workouts keep full per-minute detail regardless |
| Date range | **rolling, past ~7 days** | resends recent windows so samples the phone recorded while locked still arrive. Every write is an upsert, so the overlap is free |
| Frequency | **hourly** (or as often as offered) | the pipeline is eventually-consistent; more often just means fresher |
| URL | `https://lc.cutik.info/ingest` | the tunnel endpoint |

**Headers** (all three):

```
CF-Access-Client-Id:     <service token id>
CF-Access-Client-Secret: <service token secret>
Authorization:           Bearer <ingest_token>
```

The two `CF-Access-*` values are the Cloudflare Access **service token** for the
`longcourse-ingest` app; `ingest_token` is the add-on's own option. Over the LAN
(`http://192.168.50.221:8787/ingest`) the CF headers are not needed, but a phone
out of the house needs the tunnel, so configure the tunnel URL with all three.

## Troubleshooting: a 502 that looks like an auth failure

If the tunnel returns 502 (HAE shows a generic "export did not complete" with an
empty response body) while the LAN endpoint works, the Cloudflare tunnel is
pointing at a stale origin. The add-on's **Docker internal IP changes every time
it is rebuilt** (e.g. a version bump), and the cloudflared config hardcoded that
IP — so a deploy silently broke the tunnel. Point the tunnel's ingress for
`lc.cutik.info` at the **host port** instead, which follows the container:

```
http://192.168.50.221:8787
```

In the Cloudflared add-on that is Configuration → Additional Hosts →
`lc.cutik.info` → `http://192.168.50.221:8787`, then restart cloudflared (a full
HA restart works if the add-on refuses to restart on its own). Distinguish this
from a real auth problem by the add-on log: a 502 never reaches the app and
leaves no log line; a `401 bad token` does. Confirm the service token is valid
separately — Cloudflare returns 403, not 502, when it is wrong.

### iOS reality

Automations run **only while the phone is unlocked**, so gaps of a few hours are
normal and expected — the design is eventually-consistent, not real-time. The
rolling window is what closes those gaps: a push while unlocked resends the days
that were missed. If the phone was off or locked for a long stretch, the next
push still backfills as long as that period is inside the window. For anything
older than the window, a manual HAE **Quick Export** of the missing range,
posted the same way, fills it — same endpoint, same idempotent upsert.

## Watchdog

`homeassistant/longcourse_watchdog.yaml` alerts if the pipeline goes quiet. It
polls `/health` every 30 min and notifies when data has been stale for over 18 h
(sustained two hours, so a single missed push or a transient error does not
page). Install it **after** the first successful scheduled push, or it fires
immediately against the pre-automation gap.

Install: drop the file in `config/packages/`, ensure `configuration.yaml` has

```yaml
homeassistant:
  packages: !include_dir_named packages
```

then reload YAML. Adjust the `notify.notify` service if your companion-app
notifier is named differently.

## Verifying it works

After the automation's first run:

```sql
-- freshness should be minutes, not hours
SELECT at, n_metrics, n_workouts FROM ingest_log ORDER BY at DESC LIMIT 3;

-- the canonical layer must be advancing, not just raw metrics
SELECT metric, MAX(time) FROM v_daily_metrics
WHERE metric IN ('steps','hrv_sdnn','heart_rate') GROUP BY 1;
```

Then, after a week, compare session and step counts against the Health app to
confirm nothing is being silently dropped.

## Notes on growth

Every distinct push body is archived in `raw_payloads` (the replay source of
truth). With small daily-aggregated windows these are a few KB each; hourly for a
year is a few thousand small rows — not a concern. If it ever matters, old
`raw_payloads` can be pruned without touching the derived data, since the
canonical tables are already materialised.
