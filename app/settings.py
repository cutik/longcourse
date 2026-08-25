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
