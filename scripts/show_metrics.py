"""
Summarize Odoo API call metrics from the per-call log.

The log is at ~/.config/odoo-sidekick/call_log.jsonl, one JSON record per
HTTP call. This script aggregates it into useful summaries.

Usage:
    python -m scripts.show_metrics                    # last 100 calls
    python -m scripts.show_metrics --last 20          # last N calls
    python -m scripts.show_metrics --since '5m'       # calls in last 5 minutes
    python -m scripts.show_metrics --since '1h'       # last hour
    python -m scripts.show_metrics --since '1d'       # last 24h
    python -m scripts.show_metrics --since 2026-05-15T10:00:00
    python -m scripts.show_metrics --by-method        # group by method
    python -m scripts.show_metrics --by-model         # group by model
    python -m scripts.show_metrics --json             # machine-readable
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


LOG_PATH = Path.home() / ".config" / "odoo-sidekick" / "call_log.jsonl"


def _parse_since(s: str) -> datetime:
    """Parse a relative ('5m', '1h', '2d') or ISO timestamp into a datetime."""
    m = re.fullmatch(r"(\d+)([smhd])", s.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        return datetime.now(timezone.utc) - delta
    # ISO timestamp
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _read_records() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    records = []
    for line in LOG_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _filter(records: list[dict], since: datetime | None, last: int | None) -> list[dict]:
    if since is not None:
        records = [r for r in records if _record_ts(r) >= since]
    if last is not None:
        records = records[-last:]
    return records


def _record_ts(rec: dict) -> datetime:
    return datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))


def summarize(records: list[dict]) -> dict:
    if not records:
        return {"total_calls": 0}
    by_method = Counter(r["method"] for r in records)
    by_status = Counter(r["status"] for r in records)
    by_profile = Counter(r["profile"] for r in records)
    by_model = Counter(r["model"] for r in records)
    total_bytes = sum(r.get("bytes", 0) for r in records)
    total_ms = sum(r.get("ms", 0) for r in records)
    errors = sum(1 for r in records if not r["status"].startswith("ok"))
    first = _record_ts(records[0])
    last = _record_ts(records[-1])
    span_s = (last - first).total_seconds()
    return {
        "total_calls": len(records),
        "total_bytes": total_bytes,
        "total_ms": total_ms,
        "errors": errors,
        "span_seconds": span_s,
        "first_call": records[0]["ts"],
        "last_call": records[-1]["ts"],
        "by_method": dict(by_method.most_common()),
        "by_status": dict(by_status.most_common()),
        "by_profile": dict(by_profile.most_common()),
        "by_model": dict(by_model.most_common(20)),  # cap at 20 for readability
    }


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_ms(ms: int) -> str:
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms/1000:.2f} s"


def print_human(summary: dict, group: str | None = None) -> None:
    if summary["total_calls"] == 0:
        print("No calls logged.")
        return
    print(f"Total calls:    {summary['total_calls']}")
    print(f"Total bytes:    {_fmt_bytes(summary['total_bytes'])}")
    print(f"Total time:     {_fmt_ms(summary['total_ms'])}")
    print(f"Errors:         {summary['errors']}")
    print(f"First call:     {summary['first_call']}")
    print(f"Last call:      {summary['last_call']}")
    print()
    if group == "method" or group is None:
        print("By method:")
        for m, n in summary["by_method"].items():
            print(f"  {m:<20} {n}")
    if group == "model":
        print("By model:")
        for m, n in summary["by_model"].items():
            print(f"  {m:<30} {n}")
    if group is None:
        if summary["errors"]:
            print()
            print("By status:")
            for s, n in summary["by_status"].items():
                print(f"  {s:<20} {n}")
        print()
        print(f"By profile:    {', '.join(f'{p}={n}' for p, n in summary['by_profile'].items())}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="show_metrics", description=__doc__.split("\n\n")[0])
    p.add_argument("--since", help="Relative ('5m','1h','2d') or ISO timestamp")
    p.add_argument("--last", type=int, default=None, help="Last N calls")
    p.add_argument("--by-method", action="store_true")
    p.add_argument("--by-model", action="store_true")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    args = p.parse_args(argv)

    records = _read_records()
    since = _parse_since(args.since) if args.since else None
    last = args.last if args.last is not None else (None if args.since else 100)
    records = _filter(records, since=since, last=last)
    summary = summarize(records)

    if args.json:
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        group = "method" if args.by_method else ("model" if args.by_model else None)
        print_human(summary, group=group)
    return 0


if __name__ == "__main__":
    sys.exit(main())
