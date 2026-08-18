"""
Sync Odoo models into a local DuckDB or SQLite database for offline analysis.

Why cache:
  - Repeated queries against the same data without hammering the API.
  - Cross-model joins via SQL.
  - Pandas/polars/DuckDB analytics workflows.
  - Snapshotting a point-in-time view for reporting.

How it works:
  - For each model, call fields_get() to learn the schema.
  - Map Odoo field types to local SQL types (TEXT/INTEGER/REAL/BOOLEAN).
  - search_read() in pages and upsert into a table named exactly like the model
    (with dots — e.g. "sale.order" — quoted in SQL).
  - Track per-model sync state in _sync_state(model TEXT PRIMARY KEY, last_write_date TEXT, synced_at TEXT).
  - --incremental uses write_date > last_write_date to fetch only changes.

Many2one fields are stored as the related record's id (integer). One2many and
many2many fields are stored as JSON arrays of ids. Binary fields are skipped by
default to avoid bloating the cache (use --include-binary to override).

Usage:
    # Initial full sync
    python3 -m scripts.cache_sync <profile> \\
        --models sale.order,sale.order.line,res.partner \\
        --backend duckdb --db ./cache.duckdb

    # Incremental — only records changed since last sync
    python3 -m scripts.cache_sync <profile> \\
        --models sale.order,sale.order.line \\
        --backend duckdb --db ./cache.duckdb --incremental
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as both module and script
try:
    from .odoo_client import OdooClient, OdooClientError
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from odoo_client import OdooClient, OdooClientError  # type: ignore


# Field types we'll skip unless --include-binary is passed
BINARY_TYPES = {"binary"}

# Fields that are reverse references; we store them as JSON arrays of ids
LIST_TYPES = {"one2many", "many2many"}

# Field-type -> local SQL type
TYPE_MAP = {
    "integer":   "INTEGER",
    "float":     "REAL",
    "monetary":  "REAL",
    "boolean":   "INTEGER",  # 0/1 in both backends for portability
    "date":      "TEXT",      # ISO YYYY-MM-DD
    "datetime":  "TEXT",      # ISO YYYY-MM-DD HH:MM:SS (UTC)
    "many2one":  "INTEGER",
    "char":      "TEXT",
    "text":      "TEXT",
    "html":      "TEXT",
    "selection": "TEXT",
    "reference": "TEXT",
    "json":      "TEXT",  # stored as serialized JSON
    "properties": "TEXT",
    "properties_definition": "TEXT",
}


@dataclass
class Backend:
    name: str          # "duckdb" or "sqlite"
    conn: Any          # connection object
    placeholder: str   # "?" for sqlite, "?" for duckdb (both use ?)

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, params_list: list[tuple]) -> Any:
        return self.conn.executemany(sql, params_list)

    def commit(self) -> None:
        # DuckDB autocommits by default, sqlite needs explicit commit
        if self.name == "sqlite":
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def open_backend(name: str, path: str) -> Backend:
    name = name.lower()
    if name == "duckdb":
        try:
            import duckdb  # type: ignore
        except ImportError as e:
            raise SystemExit(
                "duckdb is not installed. `pip install duckdb` or use --backend sqlite."
            ) from e
        conn = duckdb.connect(path)
        return Backend("duckdb", conn, "?")
    if name == "sqlite":
        import sqlite3
        conn = sqlite3.connect(path)
        return Backend("sqlite", conn, "?")
    raise SystemExit(f"Unknown backend {name!r}. Use 'duckdb' or 'sqlite'.")


def quote_table(name: str) -> str:
    """Odoo model names contain dots, so we double-quote them in SQL."""
    # Defensive: forbid quotes/backticks in the model name itself
    if '"' in name or "`" in name or ";" in name:
        raise ValueError(f"Refusing dangerous model name: {name!r}")
    return f'"{name}"'


def quote_col(name: str) -> str:
    if '"' in name or "`" in name or ";" in name:
        raise ValueError(f"Refusing dangerous column name: {name!r}")
    return f'"{name}"'


def ensure_sync_state(backend: Backend) -> None:
    backend.execute("""
        CREATE TABLE IF NOT EXISTS _sync_state (
            model TEXT PRIMARY KEY,
            last_write_date TEXT,
            synced_at TEXT
        )
    """)
    backend.commit()


def get_last_write_date(backend: Backend, model: str) -> str | None:
    cur = backend.execute(
        "SELECT last_write_date FROM _sync_state WHERE model = ?", (model,)
    )
    row = cur.fetchone()
    return row[0] if row else None


def set_last_write_date(backend: Backend, model: str, last_write_date: str | None) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if backend.name == "sqlite":
        backend.execute(
            "INSERT INTO _sync_state(model, last_write_date, synced_at) VALUES (?, ?, ?) "
            "ON CONFLICT(model) DO UPDATE SET last_write_date=excluded.last_write_date, "
            "synced_at=excluded.synced_at",
            (model, last_write_date, now),
        )
    else:  # duckdb
        backend.execute("DELETE FROM _sync_state WHERE model = ?", (model,))
        backend.execute(
            "INSERT INTO _sync_state VALUES (?, ?, ?)",
            (model, last_write_date, now),
        )
    backend.commit()


def schema_for_model(
    client: OdooClient,
    model: str,
    include_binary: bool,
) -> tuple[dict[str, str], list[str]]:
    """Return (column_name -> sql_type, list of fields to fetch). 'id' is always included."""
    meta = client.fields_get(model, attributes=["type", "string", "store"])
    columns: dict[str, str] = {"id": "INTEGER"}
    fetch: list[str] = ["id"]
    for fname, info in sorted(meta.items()):
        ftype = info.get("type", "char")
        if fname == "id":
            continue
        if ftype in BINARY_TYPES and not include_binary:
            continue
        # Non-stored computed fields can be expensive; skip unless they're useful.
        # Keep them by default — Odoo will compute them on read. Users with huge
        # datasets can post-filter via the API user's ACLs.
        if ftype in LIST_TYPES:
            columns[fname] = "TEXT"  # JSON array of ids
        else:
            columns[fname] = TYPE_MAP.get(ftype, "TEXT")
        fetch.append(fname)
    return columns, fetch


def ensure_table(backend: Backend, model: str, columns: dict[str, str]) -> None:
    table = quote_table(model)
    cols_sql = ", ".join(f"{quote_col(c)} {t}" for c, t in columns.items())
    backend.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols_sql}, PRIMARY KEY (id))")
    # Add any new columns that appeared since last sync
    cur = backend.execute(f"SELECT * FROM {table} LIMIT 0")
    existing = {d[0] for d in cur.description}
    for c, t in columns.items():
        if c not in existing:
            backend.execute(f"ALTER TABLE {table} ADD COLUMN {quote_col(c)} {t}")
    backend.commit()


def coerce_value(field_type: str, value: Any) -> Any:
    """Convert Odoo's JSON shapes into something SQL-friendly."""
    if value is False or value is None:
        # Odoo uses `false` for null/empty in many cases. Distinguish boolean from null:
        # for boolean fields, keep 0/1; for everything else, store NULL.
        if field_type == "boolean":
            return 0
        return None
    if field_type == "boolean":
        return 1 if value else 0
    if field_type == "many2one":
        # API returns [id, display_name] or false
        if isinstance(value, list) and len(value) >= 1:
            return int(value[0])
        if isinstance(value, int):
            return value
        return None
    if field_type in LIST_TYPES:
        # store as JSON array of ids
        if isinstance(value, list):
            return json.dumps(value)
        return json.dumps([])
    if field_type in ("json", "properties", "properties_definition"):
        # Odoo returns real Python structures for these; DB drivers can't bind them
        return value if isinstance(value, str) else json.dumps(value)
    # everything else: pass through, but never hand a raw container to the driver
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def upsert_rows(
    backend: Backend,
    model: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    field_types: dict[str, str],
) -> None:
    if not rows:
        return
    table = quote_table(model)
    cols_q = [quote_col(c) for c in columns]
    placeholders = ", ".join(["?"] * len(columns))
    values: list[tuple] = []
    for r in rows:
        values.append(tuple(coerce_value(field_types.get(c, "char"), r.get(c)) for c in columns))

    if backend.name == "sqlite":
        update_set = ", ".join(f"{c}=excluded.{c}" for c in cols_q if c != '"id"')
        sql = (
            f"INSERT INTO {table} ({', '.join(cols_q)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {update_set}"
        )
        backend.executemany(sql, values)
    else:  # duckdb: emulate upsert with delete-then-insert
        ids = [r["id"] for r in rows]
        # DuckDB IN-list with placeholders
        in_clause = ", ".join(["?"] * len(ids))
        backend.execute(f"DELETE FROM {table} WHERE id IN ({in_clause})", tuple(ids))
        sql = f"INSERT INTO {table} ({', '.join(cols_q)}) VALUES ({placeholders})"
        backend.executemany(sql, values)
    backend.commit()


def sync_model(
    client: OdooClient,
    backend: Backend,
    model: str,
    incremental: bool,
    page_size: int,
    include_binary: bool,
) -> int:
    print(f"[{model}] introspecting schema...", file=sys.stderr)
    columns, fetch = schema_for_model(client, model, include_binary)
    ensure_table(backend, model, columns)

    # Build field-type map for value coercion
    meta = client.fields_get(model, attributes=["type"])
    field_types = {f: m.get("type", "char") for f, m in meta.items()}
    field_types["id"] = "integer"

    domain: list = []
    if incremental:
        last = get_last_write_date(backend, model)
        if last:
            # ">=" not ">": records modified later within the boundary second
            # would otherwise be skipped forever. The upsert makes re-fetching
            # the boundary rows idempotent.
            domain = [["write_date", ">=", last]]
            print(f"[{model}] incremental from write_date >= {last}", file=sys.stderr)
        else:
            print(f"[{model}] no prior sync state; doing full sync", file=sys.stderr)

    total = 0
    offset = 0
    max_write_date: str | None = None
    while True:
        batch = client.search_read(
            model, domain, fields=fetch,
            limit=page_size, offset=offset, order="id asc",
        )
        if not batch:
            break
        upsert_rows(backend, model, list(columns.keys()), batch, field_types)
        for r in batch:
            wd = r.get("write_date")
            if wd and (max_write_date is None or wd > max_write_date):
                max_write_date = wd
        total += len(batch)
        print(f"[{model}] synced {total} rows...", file=sys.stderr)
        if len(batch) < page_size:
            break
        offset += page_size

    # Always record the sync moment — a no-change incremental run still
    # verified freshness, and leaving synced_at stale makes cache_status
    # report (and staleness prompts re-trigger on) data that was checked
    # seconds ago. Carry the previous boundary forward when nothing new
    # was seen.
    if max_write_date is None and incremental:
        max_write_date = get_last_write_date(backend, model)
    set_last_write_date(backend, model, max_write_date)
    print(f"[{model}] done. {total} rows.", file=sys.stderr)
    return total


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cache_sync", description=__doc__.split("\n\n")[0])
    p.add_argument("profile", help="Profile name (or 'default')")
    p.add_argument("--models", required=True,
                   help="Comma-separated list of Odoo model names")
    p.add_argument("--backend", choices=["duckdb", "sqlite"], default="duckdb")
    p.add_argument("--db", required=True, help="Path to the local DB file")
    p.add_argument("--incremental", action="store_true",
                   help="Only fetch records with write_date > last sync")
    p.add_argument("--page-size", type=int, default=1000)
    p.add_argument("--include-binary", action="store_true",
                   help="Include binary fields (default: skip — they're huge)")
    args = p.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    try:
        client = OdooClient.from_profile(args.profile)
    except OdooClientError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    backend = open_backend(args.backend, args.db)
    failed: list[str] = []
    try:
        ensure_sync_state(backend)
        for model in models:
            try:
                sync_model(client, backend, model,
                           incremental=args.incremental,
                           page_size=args.page_size,
                           include_binary=args.include_binary)
            except Exception as e:  # noqa: BLE001 — API *or* backend errors:
                # continue with the next model rather than aborting the whole run
                print(f"[{model}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
                failed.append(model)
    finally:
        backend.close()
    if failed:
        # headless/CI callers rely on the exit code — a partial sync is a failure
        print(f"{len(failed)}/{len(models)} model(s) failed: {', '.join(failed)}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
