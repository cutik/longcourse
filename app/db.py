"""
Batched upserts.

The original ingest issued one `await conn.execute` per data point. That is fine
for an hourly HAE push of a few hundred readings and hopeless for a full history
backfill: a multi-year Apple export runs to millions of rows, and a round-trip
each turns minutes of work into hours.

The pattern here is COPY into a temporary staging table, then a single
INSERT ... SELECT ... ON CONFLICT DO UPDATE. COPY cannot do conflict resolution
itself, which is why the staging hop exists. The staging table is built with
`CREATE TEMP TABLE ... AS SELECT <cols> FROM <target> WITH NO DATA` so its column
types are copied from the real table rather than restated here and drifting.

Batches are committed as they fill. A backfill that dies two thirds of the way
through leaves two thirds of the data behind and can be re-run — every write is
an upsert, so re-running is free.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

BATCH = 5000


class BatchWriter:
    """Accumulate rows for one table and flush them in batches.

    Not thread-safe and not re-entrant; one instance per table per import run.
    """

    def __init__(self, conn, table: str, columns: Sequence[str],
                 conflict: Sequence[str], update: Sequence[str] | None = None,
                 batch: int = BATCH):
        self.conn = conn
        self.table = table
        self.columns = list(columns)
        self.conflict = list(conflict)
        # Columns not part of the key are refreshed on conflict by default:
        # providers resend overlapping windows and the later copy is the better
        # one (a workout can be edited after the fact).
        self.update = list(update) if update is not None else [
            c for c in self.columns if c not in self.conflict
        ]
        self.batch = batch
        self.stage = f"stage_{table}"
        self._rows: list[tuple] = []
        self._ready = False
        self.written = 0

    async def _prepare(self) -> None:
        cols = ", ".join(self.columns)
        await self.conn.execute(
            f"CREATE TEMP TABLE IF NOT EXISTS {self.stage} AS "
            f"SELECT {cols} FROM {self.table} WITH NO DATA"
        )
        self._ready = True

    async def add(self, row: tuple) -> None:
        self._rows.append(row)
        if len(self._rows) >= self.batch:
            await self.flush()

    async def extend(self, rows: Iterable[tuple]) -> None:
        for r in rows:
            await self.add(r)

    async def flush(self) -> int:
        if not self._rows:
            return 0
        if not self._ready:
            await self._prepare()

        cols = ", ".join(self.columns)
        async with self.conn.cursor() as cur:
            await cur.execute(f"TRUNCATE {self.stage}")
            async with cur.copy(f"COPY {self.stage} ({cols}) FROM STDIN") as cp:
                for row in self._rows:
                    await cp.write_row(row)

            if self.update:
                setters = ", ".join(f"{c}=EXCLUDED.{c}" for c in self.update)
                action = f"DO UPDATE SET {setters}"
            else:
                action = "DO NOTHING"

            # DISTINCT ON guards against a duplicate key *within* one batch:
            # ON CONFLICT cannot fix a row that collides with another row in the
            # same statement, and Apple exports do contain exact repeats.
            key = ", ".join(self.conflict)
            await cur.execute(
                f"INSERT INTO {self.table} ({cols}) "
                f"SELECT DISTINCT ON ({key}) {cols} FROM {self.stage} "
                f"ORDER BY {key} "
                f"ON CONFLICT ({key}) {action}"
            )

        n = len(self._rows)
        self.written += n
        self._rows.clear()
        return n


async def upsert_metric_meta(conn, canon: dict[str, tuple[str, str, str]]) -> None:
    """Materialise app/canon.py's vocabulary into `metric_meta` for SQL joins.

    Rewritten from the dictionary on every boot, so removing a metric from the
    code removes it here too and the views stop referencing something that no
    longer has a definition.
    """
    rows = [(m, kind, unit, label) for m, (kind, unit, label) in canon.items()]
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO metric_meta (metric, kind, unit, label) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (metric) DO UPDATE SET "
            " kind=EXCLUDED.kind, unit=EXCLUDED.unit, label=EXCLUDED.label",
            rows,
        )
        await cur.execute(
            "DELETE FROM metric_meta WHERE metric <> ALL(%s)",
            ([m for m in canon],),
        )


async def register_raw_file(conn, path: str, sha: str, kind: str, size: int,
                            counts: dict[str, Any] | None = None,
                            covers: tuple[Any, Any] | None = None,
                            note: str | None = None) -> None:
    """Record that an on-disk archive was imported.

    Large archives are not inlined into raw_payloads: a jsonb value cannot
    exceed roughly 255 MB and a full export.xml is bigger. Keeping the file on
    the mapped share and recording its digest preserves the invariant that raw
    input is replayable — the replay just reads from disk.
    """
    c = counts or {}
    await conn.execute(
        "INSERT INTO raw_files (path, sha256, kind, bytes, covers_from, covers_to,"
        " n_metrics, n_workouts, n_samples, note)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (sha256) DO UPDATE SET"
        " path=EXCLUDED.path, imported_at=now(), bytes=EXCLUDED.bytes,"
        " covers_from=EXCLUDED.covers_from, covers_to=EXCLUDED.covers_to,"
        " n_metrics=EXCLUDED.n_metrics, n_workouts=EXCLUDED.n_workouts,"
        " n_samples=EXCLUDED.n_samples, note=EXCLUDED.note",
        (path, sha, kind, size,
         covers[0] if covers else None, covers[1] if covers else None,
         c.get("metrics", 0), c.get("workouts", 0), c.get("samples", 0), note),
    )
