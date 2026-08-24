# longcourse

Apple Health → Postgres → MCP, as a Home Assistant OS local add-on.
Named for the 50m course this is built around.

## 1. Prepare the database

Reuse the Postgres instance TeslaMate already runs on, but in its **own
database with its own user** — so restoring a TeslaMate backup can never
touch your health data.

Find the Postgres add-on hostname (Supervisor exposes add-ons to each other
under their slug, e.g. `77b2833f-postgres`):

```bash
ha addons list | grep -i postgres     # note the slug
```

Then connect (phpPgAdmin, or `psql` from any machine on the LAN) and run:

```sql
CREATE USER longcourse WITH PASSWORD 'pick-something-long';
CREATE DATABASE longcourse OWNER longcourse;
```

Apply the schema against the new database:

```bash
psql -h <pg-host> -U longcourse -d longcourse -f db/schema.sql
```

No extensions needed — this is vanilla PostgreSQL 17.

## 2. Install the add-on

Copy this directory to `/addons/longcourse/` on the HAOS host (Samba share,
or the Advanced SSH & Web Terminal add-on). Then:

Settings → Add-ons → Add-on Store → ⋮ → **Check for updates**.
"Longcourse" appears under *Local add-ons*. Install — the first build takes a
few minutes on a Pi.

In Configuration, fill in the Postgres host/user/password and generate a token:

```bash
openssl rand -hex 32
```

Start it, then check the log for `Uvicorn running`.

## 3. Verify

```bash
curl http://<ha-ip>:8787/health
# {"ok":true,"last_ingest":null,"age_hours":999}
```

## 4. Reach it from the iPhone

Install the **Tailscale** add-on from the HA add-on store and log in; put
Tailscale on the iPhone with the same account. The add-on's port 8787 is then
reachable at the tailnet address, with nothing exposed to the internet.

Your existing OpenVPN stays as it is — different interface, no conflict. It is
not used here because iOS will not keep a normal app's VPN tunnel up in the
background, so the hourly export would fire into a closed connection. Tailscale
runs as a Network Extension and stays up on its own.

## 5. Backfill history

Health Auto Export → Quick Export → JSON, format version 2, workouts enabled,
last 3 months. Drop the file into the HA `share` folder (mapped into the
add-on), then POST it:

```bash
curl -X POST http://<ha-ip>:8787/ingest \
  -H "Authorization: Bearer <ingest_token>" \
  -H "Content-Type: application/json" \
  --data-binary @HealthAutoExport.json
```

**The `lengths` count in the response is the answer to the open question.**
Above zero means HAE ships per-length splits and `session_detail` gives real
set-by-set analysis. Zero means session-level aggregates only, and we decide
then whether a free swim app's CSV is worth mixing in.

Re-posting the same file is safe — every write upserts.

## 6. Scheduled export

Only after the backfill works. HAE → Automations → REST API:

- URL `http://<tailnet-ip>:8787/ingest`, POST, JSON, format version 2
- Header `Authorization: Bearer <ingest_token>`
- Schedule hourly, all metrics + workouts

## 7. Connect the MCP

```bash
# Claude Code, on any machine in the tailnet
claude mcp add --transport http longcourse http://<ha-ip>:8787/mcp
```

For claude.ai on web/mobile you need a public HTTPS endpoint — Anthropic
connects from its own network and cannot enter your tailnet. That means a
Cloudflare Tunnel plus real auth on the MCP route. Worth doing later, and
worth doing deliberately; skip it while the pipeline is still being shaken out.

Also install `ha-mcp` (HACS → custom repository `homeassistant-ai/ha-mcp`) to
give the same conversation your bedroom climate, presence and calendar.

## 8. Watchdog

```yaml
# configuration.yaml
rest:
  - resource: http://127.0.0.1:8787/health
    scan_interval: 1800
    sensor:
      - name: Longcourse sync age
        value_template: "{{ value_json.age_hours | default(999) }}"
        unit_of_measurement: h
```

Add the alert automation *after* the first successful sync, or it fires
immediately. iOS blocks health reads while the phone is locked and throttles
background refresh, so ingest is eventually-consistent by design — the sensor
catches a dead pipeline, not a slow one.

## Design notes

- `raw_payloads` is the source of truth; the other tables are projections.
  Wrong parse → fix `main.py`, replay, nothing lost.
- Everything upserts. HAE resends overlapping windows constantly.
- Distances normalise to metres at the edge. `pool_length_m` is a real column
  because SWOLF only compares within one course length.
- The MCP exposes questions, not tables. Resist adding a generic `run_sql`
  tool — it moves aggregation into the context window, where it costs most.

## Not built yet

The weekly coach loop: a schedule that runs `claude -p` against this MCP plus
a plan file in git, writes the next microcycle and notifies through HA. Worth
building after two or three weeks of data have actually landed.
