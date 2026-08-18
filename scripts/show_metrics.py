"""
Summarize Odoo API call metrics from the per-call log.

The log is at <state_dir>/call_log.jsonl (default ~/.config/odoo-sidekick/,
override with ODOO_SIDEKICK_STATE_DIR), one JSON record per HTTP call, plus
datestamped call_log-*.jsonl archives from rotation. This script aggregates
current log + archives into useful summaries. Records may carry `caller`
(from ODOO_SIDEKICK_CALLER) and `auth_ref` — surfaced here for auditing
multi-agent hosts.

Usage:
    python3 -m scripts.show_metrics                    # last 100 calls
    python3 -m scripts.show_metrics --last 20          # last N calls
    python3 -m scripts.show_metrics --since '5m'       # calls in last 5 minutes
    python3 -m scripts.show_metrics --since '1h'       # last hour
    python3 -m scripts.show_metrics --since '1d'       # last 24h
    python3 -m scripts.show_metrics --since 2026-05-15T10:00:00
    python3 -m scripts.show_metrics --by-method        # group by method
    python3 -m scripts.show_metrics --by-model         # group by model
    python3 -m scripts.show_metrics --by-caller        # group by caller (audit)
    python3 -m scripts.show_metrics --json             # machine-readable
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .odoo_client import state_dir, READ_METHODS
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from odoo_client import state_dir, READ_METHODS  # type: ignore


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
    # ISO timestamp — assume UTC when no offset is given (log records are aware)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_records() -> list[dict]:
    """Read archives (chronological by datestamped name) then the live log."""
    log_dir = state_dir()
    paths = sorted(log_dir.glob("call_log-*.jsonl")) + [log_dir / "call_log.jsonl"]
    records = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Tolerate hand-edited or future-schema lines: anything without a
            # parseable ts can't be filtered or spanned — skip, don't crash.
            if isinstance(rec, dict) and isinstance(rec.get("ts"), str):
                records.append(rec)
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
    by_method = Counter(r.get("method", "unknown") for r in records)
    by_status = Counter(r.get("status", "unknown") for r in records)
    by_profile = Counter(r.get("profile", "unknown") for r in records)
    by_model = Counter(r.get("model", "unknown") for r in records)
    total_bytes = sum(r.get("bytes", 0) for r in records)
    total_ms = sum(r.get("ms", 0) for r in records)
    errors = sum(1 for r in records
                 if not str(r.get("status") or "unknown").startswith("ok"))
    first = _record_ts(records[0])
    last = _record_ts(records[-1])
    span_s = (last - first).total_seconds()
    by_caller = Counter(r["caller"] for r in records if r.get("caller"))
    writes = [r for r in records if r.get("method", "") not in READ_METHODS]
    out = {
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
    if by_caller:
        out["by_caller"] = dict(by_caller.most_common())
    if writes:
        out["write_calls"] = len(writes)
        out["writes_with_auth_ref"] = sum(1 for r in writes if r.get("auth_ref"))
        out["writes_without_auth_ref"] = sum(1 for r in writes if not r.get("auth_ref"))
    return out


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
    if group == "caller":
        if summary.get("by_caller"):
            print("By caller:")
            for c, n in summary["by_caller"].items():
                print(f"  {c:<30} {n}")
        else:
            print("No caller identities recorded (set ODOO_SIDEKICK_CALLER "
                  "per agent to enable).")
    if group is None:
        if summary["errors"]:
            print()
            print("By status:")
            for s, n in summary["by_status"].items():
                print(f"  {s:<20} {n}")
        print()
        print(f"By profile:    {', '.join(f'{p}={n}' for p, n in summary['by_profile'].items())}")
        if summary.get("by_caller"):
            print(f"By caller:     {', '.join(f'{c}={n}' for c, n in summary['by_caller'].items())}")
        if summary.get("write_calls"):
            print(f"Writes:        {summary['write_calls']} "
                  f"({summary['writes_with_auth_ref']} with auth_ref, "
                  f"{summary['writes_without_auth_ref']} without)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="show_metrics", description=__doc__.split("\n\n")[0])
    p.add_argument("--since", help="Relative ('5m','1h','2d') or ISO timestamp")
    p.add_argument("--last", type=int, default=None, help="Last N calls")
    group_flags = p.add_mutually_exclusive_group()
    group_flags.add_argument("--by-method", action="store_true")
    group_flags.add_argument("--by-model", action="store_true")
    group_flags.add_argument("--by-caller", action="store_true",
                             help="Group by caller identity (ODOO_SIDEKICK_CALLER)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    args = p.parse_args(argv)

    records = _read_records()
    try:
        since = _parse_since(args.since) if args.since else None
    except (ValueError, OverflowError):
        # OverflowError: an absurdly large relative value (e.g. '10...0d')
        # overflows timedelta — still a bad argument, not a crash.
        p.error(f"--since {args.since!r} is neither a usable relative value "
                "('5m','1h','2d') nor an ISO timestamp")
    last = args.last if args.last is not None else (None if args.since else 100)
    records = _filter(records, since=since, last=last)
    summary = summarize(records)

    if args.json:
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        group = ("method" if args.by_method
                 else "model" if args.by_model
                 else "caller" if args.by_caller
                 else None)
        print_human(summary, group=group)
    return 0


if __name__ == "__main__":
    sys.exit(main())
