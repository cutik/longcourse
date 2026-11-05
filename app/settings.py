"""
Add-on options in one place.

Split out of main.py so the CLI importers can read the same configuration
without importing the FastAPI app (which would build the MCP server and open a
connection pool just to parse a file).

Supervisor writes /data/options.json from the add-on's Configuration tab. The
`options:` block in config.yaml holds defaults only and is committed to git —
a real password leaked that way once, so those fields stay empty there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

OPTS: dict[str, Any] = {}
_opts_file = Path("/data/options.json")
if _opts_file.exists():
    OPTS = json.loads(_opts_file.read_text())


def opt(key: str, default: Any = None) -> Any:
    return OPTS.get(key) or os.getenv(key.upper(), default)


DSN = (
    f"postgresql://{opt('db_user')}:{opt('db_password')}"
    f"@{opt('db_host')}:{opt('db_port', 5432)}/{opt('db_name')}"
)
TOKEN = opt("ingest_token")
TZ = opt("tz", "Europe/Kyiv")

# Where large archives are dropped over Samba for backfill. Mapped share:rw.
ARCHIVE_DIR = Path(opt("archive_dir", "/share/archive"))

# Garmin Connect. Only refreshable tokens live here — the password is typed
# once into `python -m importers.garmin --login` and never stored. /data is the
# add-on's persistent volume; on a laptop the library's own default is used.
GARMIN_TOKEN_DIR = opt("garmin_token_dir",
                       "/data/garmin" if Path("/data").is_dir() else "~/.garminconnect")


def flag(key: str, default: bool) -> bool:
    """A boolean option. opt() cannot carry False — it treats falsy as unset."""
    if key in OPTS:
        return bool(OPTS[key])
    env = os.getenv(key.upper())
    return default if env is None else env.strip().lower() in ("1", "true", "yes", "on")


# Each provider can be switched off without touching the other. Apple stays on
# by default (the iPhone still feeds steps and gait through HAE); Garmin is
# opt-in because it needs a one-off interactive login first.
HAE_ENABLED = flag("hae_enabled", True)
GARMIN_ENABLED = flag("garmin_enabled", False)
GARMIN_POLL_MINUTES = int(opt("garmin_poll_minutes", 60))
GARMIN_LOOKBACK_DAYS = int(opt("garmin_lookback_days", 3))
