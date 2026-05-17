"""
Report freshness of the local Odoo cache: which models are cached, when they
were last synced, how stale they are, and how many rows each has.

The cache_sync script maintains a _sync_state table with one row per model:
    model, last_write_date, synced_at

This script reads that table from a DuckDB or SQLite cache file and presents
the data in a usable form. Use it before running queries against the cache
to decide whether a refresh is needed first.

Usage:
    python -m scripts.cache_status --backend duckdb --db ./cache.duckdb
    python -m scripts.cache_status --backend sqlite --db ./cache.sqlite
    python -m scripts.cache_status --backend duckdb --db ./cache.duckdb --table sale.order
    python -m scripts.cache_status --backend duckdb --db ./cache.duckdb --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _open_backend(backend: str, db_path: str):
    backend = backend.lower()
    if backend == "duckdb":
        try:
            import duckdb  # type: ignore
        except ImportError:
            raise SystemExit("duckdb not installed. `pip install duckdb` or use --backend sqlite.")
        return duckdb.connect(db_path, read_only=True), "duckdb"
    if backend == "sqlite":
        import sqlite3
        return sqlite3.connect(db_path), "sqlite"
    raise SystemExit(f"Unknown backend {backend!r}")


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        # Odoo timestamps are usually 'YYYY-MM-DD HH:MM:SS' without tz
        if "T" not in s and " " in s and "+" not in s:
            s = s.replace(" ", "T") + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _humanize_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d {h}h"


def _table_exists(conn, backend_kind: str, name: str) -> bool:
    if backend_kind == "sqlite":
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        )
        return cur.fetchone() is not None
    # duckdb
    try:
        conn.execute(f'SELECT 1 FROM "{name}" LIMIT 0')
        return True
    except Exception:  # noqa: BLE001
        return False


def status(db_path: str, backend: str, table_filter: str | None = None) -> dict:
    """Return cache freshness for all (or one) cached model."""
    conn, kind = _open_backend(backend, db_path)
    out: dict[str, Any] = {
        "db_path": db_path,
        "backend": backend,
        "now": datetime.now(timezone.utc).isoformat(),
        "tables": [],
        "note": None,
    }

    if not _table_exists(conn, kind, "_sync_state"):
        out["note"] = (
            "No _sync_state table found. This database hasn't been populated by "
            "cache_sync, or the sync was never completed. Run cache_sync first."
        )
        return out

    rows = conn.execute(
        "SELECT model, last_write_date, synced_at FROM _sync_state ORDER BY model"
    ).fetchall()

    now = datetime.now(timezone.utc)
    for model, last_write_date, synced_at in rows:
        if table_filter and model != table_filter:
            continue
        # Count rows in the actual model table
        row_count = None
        if _table_exists(conn, kind, model):
            try:
                cur = conn.execute(f'SELECT COUNT(*) FROM "{model}"')
                row_count = cur.fetchone()[0]
            except Exception:  # noqa: BLE001
                pass
        synced_dt = _parse_iso(synced_at)
        last_dt = _parse_iso(last_write_date)
        age_s = None
        if synced_dt:
            age_s = (now - synced_dt).total_seconds()
        out["tables"].append({
            "model": model,
            "rows": row_count,
            "synced_at": synced_at,
            "last_write_date": last_write_date,
            "age_seconds": age_s,
            "age_human": _humanize_age(age_s) if age_s is not None else None,
        })
    return out


def print_human(info: dict) -> None:
    print(f"Cache:          {info['db_path']} ({info['backend']})")
    if info.get("note"):
        print(f"Note:           {info['note']}")
        return
    if not info["tables"]:
        print("No cached models.")
        return
    # Column-aligned report
    print(f"\n{'Model':<28} {'Rows':>10}  {'Age':<12}  {'Last synced (UTC)'}")
    print("-" * 80)
    for t in info["tables"]:
        rows = "—" if t["rows"] is None else f"{t['rows']:,}"
        age = t["age_human"] or "?"
        synced = t["synced_at"] or "—"
        print(f"{t['model']:<28} {rows:>10}  {age:<12}  {synced}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cache_status", description=__doc__.split("\n\n")[0])
    p.add_argument("--db", required=True, help="Path to the cache DB file")
    p.add_argument("--backend", choices=["duckdb", "sqlite"], default="duckdb")
    p.add_argument("--table", help="Only report on this Odoo model")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    args = p.parse_args(argv)

    info = status(args.db, args.backend, table_filter=args.table)
    if args.json:
        json.dump(info, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        print_human(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
