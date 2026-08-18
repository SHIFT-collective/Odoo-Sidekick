"""
Capture and serve user context — who is using this skill and what they care
about — so queries, phrasing, and (from v2.0) routines can be tailored.

Why this exists: "show me sales" means something different to a CFO than to a
production manager, and a €10k invoice is headline news in a 5-person shop but
noise in a 500-person one. This module stores a small, explicit user profile
at <state_dir>/user_profile.yaml and turns it into concrete defaults via the
`guidance` command: which reports to lead with, how deep to go, what counts as
a notable amount, and which comparison window matches the user's cadence.

The schema is persona-agnostic: `role: ops-agent` is as valid as `role: CFO`,
because in agent deployments the "user" is an agent with a role. Onboarding is
skippable (`skip` records the choice so the user is never nagged twice) and
pre-seedable from a file (`seed --file`), so headless deployments never fight
the interactive flow.

Storage: <state_dir>/user_profile.yaml (state dir honors
ODOO_SIDEKICK_STATE_DIR; default ~/.config/odoo-sidekick). Canonical on-disk
format is JSON — valid YAML, loads without PyYAML — same convention as
profiles.yaml. Edited under the same advisory lock + atomic write as the
profiles CLI. This file holds preferences, never credentials.

Usage:
    python3 -m scripts.user_profile show [--json]
    python3 -m scripts.user_profile guidance [--json]     # profile -> concrete defaults
    python3 -m scripts.user_profile set <key> <value>
    python3 -m scripts.user_profile set goals --json-value '["cash-flow","cost-control"]'
    python3 -m scripts.user_profile unset <key>
    python3 -m scripts.user_profile seed --file <path> [--force]   # headless pre-seed
    python3 -m scripts.user_profile skip                  # record "asked, declined"
    python3 -m scripts.user_profile clear

Keys:
    role              free-form string (CFO, Operations Director, ops-agent, ...)
    company_size      bucket (solo, 2-10, 11-50, 51-200, 201-500, 500+) or a
                      plain employee count, which is normalized to a bucket
    goals             list of strings; known vocabulary (revenue-growth,
                      cost-control, operational-efficiency, cash-flow,
                      customer-retention, throughput) unlocks focus hints,
                      free text is kept as-is
    pain_points       list of strings, free text
    decision_cadence  daily | weekly | monthly | quarterly | ad-hoc
    detail_preference executive | balanced | operational (optional — derived
                      from the role when unset)
    language          optional preferred output language (e.g. "es", "Deutsch")

Exit codes: 0 ok (including `show`/`guidance` when no profile exists — the
JSON carries "exists": false so machine callers can branch) · 1 error · 2 bad
arguments.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .odoo_client import ProfileError, _load_config, state_dir
    from .profiles import _Lock, _atomic_write
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from odoo_client import ProfileError, _load_config, state_dir  # type: ignore
    from profiles import _Lock, _atomic_write  # type: ignore


SCHEMA_VERSION = 1

CADENCES = ("daily", "weekly", "monthly", "quarterly", "ad-hoc")
DEPTHS = ("executive", "balanced", "operational")
SIZE_BUCKETS = ("solo", "2-10", "11-50", "51-200", "201-500", "500+")
ONBOARDING_STATES = ("completed", "skipped", "seeded")

LIST_KEYS = {"goals", "pain_points"}
SCALAR_KEYS = {"role", "company_size", "decision_cadence", "detail_preference",
               "language"}
USER_KEYS = SCALAR_KEYS | LIST_KEYS
# Written by this tool, not user-settable directly.
MANAGED_KEYS = {"schema_version", "onboarding", "created", "updated"}


def user_profile_path() -> Path:
    return state_dir() / "user_profile.yaml"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- Normalization & validation -------------------------------------------------

def _normalize_company_size(value: Any) -> str:
    """Accept a bucket name or an employee count; return a bucket."""
    if isinstance(value, bool):
        raise ProfileError("company_size must be a size bucket or employee count.")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ProfileError("company_size must be a finite employee count.")
        n = int(value)
    else:
        text = str(value).strip().lower()
        aliases = {
            "solo": "solo", "1": "solo", "just me": "solo", "one": "solo",
            "500+": "500+", ">500": "500+", "500 plus": "500+",
        }
        if text in aliases:
            return aliases[text]
        if text in SIZE_BUCKETS:
            return text
        if re.fullmatch(r"\d+", text):
            n = int(text)
        else:
            raise ProfileError(
                f"company_size must be one of {list(SIZE_BUCKETS)} or a plain "
                f"employee count, got {value!r}."
            )
    if n <= 0:
        raise ProfileError(f"company_size count must be positive, got {n}.")
    if n == 1:
        return "solo"
    if n <= 10:
        return "2-10"
    if n <= 50:
        return "11-50"
    if n <= 200:
        return "51-200"
    if n <= 500:
        return "201-500"
    return "500+"


def _validate_str_list(key: str, value: Any) -> list[str]:
    if isinstance(value, str):
        # Convenience: plain `set goals "cash-flow, cost-control"` splits on commas.
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        raise ProfileError(f"{key} must be a list of non-empty strings.")
    if len(value) > 12:
        raise ProfileError(f"{key} holds at most 12 entries, got {len(value)}.")
    cleaned = [v.strip() for v in value]
    for v in cleaned:
        if len(v) > 200:
            raise ProfileError(f"{key} entries must be ≤200 chars: {v[:60]!r}…")
    return cleaned


def _validate_and_normalize(key: str, value: Any) -> Any:
    if key == "role":
        if not isinstance(value, str) or not value.strip():
            raise ProfileError("role must be a non-empty string.")
        if len(value.strip()) > 120:
            raise ProfileError("role must be ≤120 chars.")
        return value.strip()
    if key == "company_size":
        return _normalize_company_size(value)
    if key == "decision_cadence":
        low = str(value).strip().lower().replace("ad hoc", "ad-hoc").replace("adhoc", "ad-hoc")
        if low not in CADENCES:
            raise ProfileError(f"decision_cadence must be one of {list(CADENCES)}, got {value!r}.")
        return low
    if key == "detail_preference":
        low = str(value).strip().lower()
        if low not in DEPTHS:
            raise ProfileError(f"detail_preference must be one of {list(DEPTHS)}, got {value!r}.")
        return low
    if key == "language":
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 32:
            raise ProfileError("language must be a non-empty string ≤32 chars.")
        return value.strip()
    if key in LIST_KEYS:
        return _validate_str_list(key, value)
    raise ProfileError(
        f"Unknown user-profile key {key!r}. Settable: {sorted(USER_KEYS)}."
    )


def _validate_document(doc: dict[str, Any]) -> None:
    if not isinstance(doc, dict):
        raise ProfileError("User profile root must be a mapping.")
    unknown = set(doc) - USER_KEYS - MANAGED_KEYS
    if unknown:
        raise ProfileError(
            f"Unknown user-profile key(s): {sorted(unknown)}. "
            f"Valid: {sorted(USER_KEYS)} (plus managed {sorted(MANAGED_KEYS)})."
        )
    for key in USER_KEYS:
        if key in doc:
            doc[key] = _validate_and_normalize(key, doc[key])
    if doc.get("onboarding") is not None and doc["onboarding"] not in ONBOARDING_STATES:
        raise ProfileError(
            f"onboarding must be one of {list(ONBOARDING_STATES)}, got {doc['onboarding']!r}."
        )


# -- Role playbooks (derived guidance) -------------------------------------------

# Single-word keywords match whole words of the role; phrases match as
# substrings. First family whose keywords hit wins, so order places the more
# specific families (finance before executive: "Chief Financial Officer"
# contains "chief") ahead of the catch-alls.
ROLE_FAMILIES: list[tuple[str, tuple[str, ...]]] = [
    ("finance", ("cfo", "finance", "financial", "treasurer", "controller",
                 "chief financial")),
    ("accounting", ("accountant", "accounting", "bookkeeper", "bookkeeping",
                    "accounts receivable", "accounts payable")),
    ("production", ("production", "manufacturing", "plant", "mrp", "shop floor",
                    "workshop", "factory")),
    ("purchasing", ("purchasing", "procurement", "buyer", "sourcing")),
    ("operations", ("coo", "operations", "ops", "logistics", "supply chain",
                    "warehouse", "fulfillment", "fulfilment")),
    ("sales", ("sales", "crm", "revenue", "account manager",
               "business development", "bd", "commercial")),
    ("marketing", ("marketing", "cmo", "growth", "brand", "campaigns")),
    ("executive", ("ceo", "founder", "owner", "president", "director general",
                   "managing director", "general manager", "chief executive",
                   "gm", "md")),
]

AGENT_MARKERS = ("agent", "bot", "automation", "pipeline", "scheduler",
                 "service", "daemon", "headless")

PLAYBOOKS: dict[str, dict[str, Any]] = {
    "executive": {
        "depth": "executive",
        "default_reports": [
            "Cash position (bank + outstanding AR/AP)",
            "Yesterday's / this week's sales vs prior period",
            "Open opportunities needing attention",
            "Exceptions: late deliveries, overdue invoices, stalled MOs",
        ],
        "default_models": ["sale.order", "account.move", "crm.lead", "mrp.production"],
        "suggested_routines": ["daily-standup-ceo", "weekly-review"],
    },
    "finance": {
        "depth": "executive",
        "default_reports": [
            "Cash position and short-term cash schedule",
            "AR aging (delta vs yesterday, movement between buckets)",
            "AP coming due this week",
            "Posted-but-unreconciled flags",
        ],
        "default_models": ["account.move", "account.move.line", "account.payment"],
        "suggested_routines": ["morning-checkin-cfo", "weekly-review"],
    },
    "accounting": {
        "depth": "operational",
        "default_reports": [
            "Unreconciled payments and bank statement lines",
            "Draft invoices awaiting posting",
            "Overdue AR at record level (who, what, how old)",
            "Vendor bills awaiting validation",
        ],
        "default_models": ["account.move", "account.move.line", "account.payment"],
        "suggested_routines": ["weekly-review"],
    },
    "operations": {
        "depth": "operational",
        "default_reports": [
            "Open MOs and today's schedule",
            "Stock-out risks (reordering rules crossed)",
            "Late shipments and receipts",
            "Production efficiency vs yesterday",
        ],
        "default_models": ["mrp.production", "stock.move", "stock.quant",
                          "stock.warehouse.orderpoint"],
        "suggested_routines": ["operations-standup", "weekly-review"],
    },
    "sales": {
        "depth": "balanced",
        "default_reports": [
            "Pipeline value by stage",
            "Deals advancing vs stalling (>14 days idle)",
            "New leads this week and their sources",
            "Win rate trend",
        ],
        "default_models": ["crm.lead", "sale.order", "res.partner"],
        "suggested_routines": ["sales-pipeline-review", "weekly-review"],
    },
    "production": {
        "depth": "operational",
        "default_reports": [
            "Open MOs and work orders by workcenter",
            "Actual vs estimated times (who's beating estimates)",
            "Component availability for upcoming MOs",
            "Scrap and rework flags",
        ],
        "default_models": ["mrp.production", "mrp.workorder", "mrp.bom"],
        "suggested_routines": ["operations-standup", "weekly-review"],
    },
    "purchasing": {
        "depth": "operational",
        "default_reports": [
            "POs awaiting confirmation or approval",
            "Late vendor deliveries",
            "Price variance vs last purchase",
            "Reordering suggestions",
        ],
        "default_models": ["purchase.order", "purchase.order.line", "res.partner"],
        "suggested_routines": ["weekly-review"],
    },
    "marketing": {
        "depth": "balanced",
        "default_reports": [
            "Lead volume and conversion by source/campaign",
            "New vs returning customer revenue",
            "Top products by growth",
        ],
        "default_models": ["crm.lead", "sale.order", "utm.campaign"],
        "suggested_routines": ["weekly-review"],
    },
    "general": {
        "depth": "balanced",
        "default_reports": [
            "Revenue this month vs last",
            "Cash position",
            "Open orders and anything overdue",
        ],
        "default_models": ["sale.order", "account.move", "res.partner"],
        "suggested_routines": ["weekly-review"],
    },
}

# Known goal vocabulary -> extra focus. Free-text goals are kept verbatim and
# simply don't unlock extras.
GOAL_FOCUS: dict[str, dict[str, Any]] = {
    "revenue-growth": {
        "report": "Pipeline value and win-rate trend",
        "models": ["crm.lead", "sale.order"],
    },
    "cost-control": {
        "report": "Expense and vendor-bill trend vs prior period",
        "models": ["account.move", "purchase.order"],
    },
    "operational-efficiency": {
        "report": "Production efficiency: actual vs estimated work-order times",
        "models": ["mrp.workorder"],
    },
    "cash-flow": {
        "report": "Cash position, AR/AP schedule, DSO trend",
        "models": ["account.payment", "account.move"],
    },
    "customer-retention": {
        "report": "Churn signals: customers whose revenue is dropping",
        "models": ["sale.order", "res.partner"],
    },
    "throughput": {
        "report": "MO cycle times and WIP levels",
        "models": ["mrp.production"],
    },
}

# Starting calibration for "a notable amount", in the instance's currency.
# A hint to scale conversation defaults, not a business rule.
MATERIALITY_BY_SIZE = {
    "solo": 500, "2-10": 1_000, "11-50": 5_000, "51-200": 20_000,
    "201-500": 50_000, "500+": 100_000,
}

COMPARISON_BY_CADENCE = {
    "daily": "yesterday vs the day before, plus today's schedule",
    "weekly": "last 7 days vs the prior 7",
    "monthly": "month-to-date vs the same span of last month",
    "quarterly": "quarter-to-date vs the prior quarter",
    "ad-hoc": "the most recent complete period relevant to the question",
}

DEPTH_PHRASING = {
    "executive": [
        "Lead with the headline number and its delta; keep tables to ~5 rows.",
        "Name the one thing that needs a decision; keep drill-down for follow-ups.",
    ],
    "balanced": [
        "Give the summary first, then one level of supporting detail.",
        "Highlight trends and outliers; leave record-level dumps for follow-ups.",
    ],
    "operational": [
        "Include record-level detail: references, ids, quantities, dates.",
        "Sort by actionability (what needs doing first), not by size.",
    ],
}


def _normalize_goal(goal: str) -> str:
    return re.sub(r"[\s_]+", "-", goal.strip().lower())


def classify_role(role: str) -> tuple[str, bool]:
    """Return (role_family, is_agent) for a free-form role string."""
    low = role.strip().lower()
    words = set(re.split(r"[^a-z0-9]+", low)) - {""}
    is_agent = any(
        marker in words if " " not in marker else marker in low
        for marker in AGENT_MARKERS
    )
    for family, keywords in ROLE_FAMILIES:
        for kw in keywords:
            if (" " in kw and kw in low) or (" " not in kw and kw in words):
                return family, is_agent
    return "general", is_agent


def build_guidance(doc: dict[str, Any]) -> dict[str, Any]:
    """Turn a stored profile into concrete, actionable defaults."""
    role = doc.get("role")
    family, is_agent = classify_role(role) if role else ("general", False)
    play = PLAYBOOKS[family]
    depth = doc.get("detail_preference") or play["depth"]

    reports = list(play["default_reports"])
    models = list(play["default_models"])
    goals = doc.get("goals") or []
    matched_goals, extra_goals = [], []
    for goal in goals:
        focus = GOAL_FOCUS.get(_normalize_goal(goal))
        if focus:
            matched_goals.append(goal)
            if focus["report"] not in reports:
                reports.append(focus["report"])
            for m in focus["models"]:
                if m not in models:
                    models.append(m)
        else:
            extra_goals.append(goal)

    size = doc.get("company_size")
    cadence = doc.get("decision_cadence")
    phrasing = list(DEPTH_PHRASING[depth])
    if is_agent:
        phrasing.append(
            "Consumer is an agent: prefer --json outputs, skip conversational "
            "chrome, keep results structured."
        )
    if doc.get("language"):
        phrasing.append(f"Preferred output language: {doc['language']}.")
    if doc.get("pain_points"):
        phrasing.append(
            "Stated pain points (watch for chances to address them): "
            + "; ".join(doc["pain_points"])
        )

    return {
        "exists": True,
        "onboarding": doc.get("onboarding"),
        "role": role,
        "role_family": family,
        "is_agent": is_agent,
        "depth": depth,
        "company_size": size,
        "materiality_hint": MATERIALITY_BY_SIZE.get(size) if size else None,
        "decision_cadence": cadence,
        "comparison_window": COMPARISON_BY_CADENCE.get(cadence) if cadence else None,
        "goals": goals,
        "goals_recognized": matched_goals,
        "goals_freeform": extra_goals,
        "pain_points": doc.get("pain_points") or [],
        "default_reports": reports,
        "default_models": models,
        "phrasing": phrasing,
        "suggested_routines": play["suggested_routines"],
        "notes": [
            "materiality_hint is a starting calibration in the instance's "
            "currency — adjust when the user signals otherwise.",
            "suggested_routines are v2.0 templates; until v2.0 ships they are "
            "names to pre-populate, not runnable definitions.",
        ],
    }


# -- Storage --------------------------------------------------------------------

def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    doc = _load_config(path)
    _validate_document(doc)
    return doc


def _save(path: Path, doc: dict[str, Any], *, new: bool) -> None:
    doc["schema_version"] = SCHEMA_VERSION
    now = _now()
    if new or "created" not in doc:
        doc["created"] = doc.get("created") or now
    doc["updated"] = now
    _validate_document(doc)
    _atomic_write(path, doc)


# -- Commands -------------------------------------------------------------------

def cmd_show(path: Path, as_json: bool) -> int:
    doc = _load(path)
    if as_json:
        json.dump({"path": str(path), "exists": doc is not None,
                   "profile": doc}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if doc is None:
        print(f"No user profile at {path}.")
        print("Create one with `python3 -m scripts.user_profile set role <role>` "
              "or seed it with `... seed --file <path>`.")
        return 0
    print(f"User profile: {path}")
    for key in sorted(USER_KEYS):
        if key in doc:
            value = doc[key]
            print(f"  {key:<18} {', '.join(value) if isinstance(value, list) else value}")
    for key in ("onboarding", "created", "updated"):
        if doc.get(key):
            print(f"  {key:<18} {doc[key]}")
    return 0


def cmd_guidance(path: Path, as_json: bool) -> int:
    doc = _load(path)
    if doc is None or not any(k in doc for k in USER_KEYS):
        onboarding = (doc or {}).get("onboarding")
        if onboarding == "skipped":
            hint = ("Tailoring was offered and declined — use generic defaults "
                    "and do not re-offer unless the user asks.")
        else:
            hint = ("No user context stored. Offer the (skippable) tailoring "
                    "questions once, or continue with generic defaults.")
        payload = {"exists": False, "onboarding": onboarding, "hint": hint}
        if as_json:
            json.dump(payload, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            print(hint)
        return 0
    guidance = build_guidance(doc)
    if as_json:
        json.dump(guidance, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    print(f"Role:        {guidance['role']}  (family: {guidance['role_family']}"
          + (", agent consumer" if guidance["is_agent"] else "") + ")")
    print(f"Depth:       {guidance['depth']}")
    if guidance["company_size"]:
        print(f"Size:        {guidance['company_size']}  "
              f"(notable amount ≈ {guidance['materiality_hint']:,} in instance currency)")
    if guidance["decision_cadence"]:
        print(f"Cadence:     {guidance['decision_cadence']}  "
              f"(compare {guidance['comparison_window']})")
    if guidance["goals"]:
        print(f"Goals:       {', '.join(guidance['goals'])}")
    if guidance["pain_points"]:
        print(f"Pain points: {'; '.join(guidance['pain_points'])}")
    print("\nLead with:")
    for r in guidance["default_reports"]:
        print(f"  - {r}")
    print("\nLikely models: " + ", ".join(guidance["default_models"]))
    print("\nPhrasing:")
    for ph in guidance["phrasing"]:
        print(f"  - {ph}")
    return 0


def cmd_set(path: Path, key: str, value: str | None, json_value: str | None) -> int:
    if key not in USER_KEYS:
        raise ProfileError(
            f"Unknown user-profile key {key!r}. Settable: {sorted(USER_KEYS)}."
        )
    if json_value is not None:
        try:
            raw: Any = json.loads(json_value)
        except json.JSONDecodeError as e:
            raise ProfileError(f"--json-value is not valid JSON: {e}") from e
    elif value is not None:
        raw = value
    else:
        raise ProfileError(f"`set {key}` needs a value (or --json-value).")
    with _Lock(path):
        doc = _load(path) or {}
        new = not path.exists()
        doc[key] = _validate_and_normalize(key, raw)
        if doc.get("onboarding") != "seeded":
            doc["onboarding"] = "completed"
        _save(path, doc, new=new)
    print(f"Set {key} in {path}")
    return 0


def cmd_unset(path: Path, key: str) -> int:
    if key not in USER_KEYS:
        raise ProfileError(
            f"Unknown user-profile key {key!r}. Settable: {sorted(USER_KEYS)}."
        )
    with _Lock(path):
        doc = _load(path)
        if doc is None or key not in doc:
            print(f"{key} was not set — nothing to do.")
            return 0
        del doc[key]
        _save(path, doc, new=False)
    print(f"Removed {key} from {path}")
    return 0


def cmd_seed(path: Path, file: str, force: bool) -> int:
    seed_path = Path(file).expanduser()
    if not seed_path.exists():
        raise ProfileError(f"Seed file not found: {seed_path}")
    try:
        doc = _load_config(seed_path)
    except ProfileError as e:
        if "PyYAML" in str(e):
            raise ProfileError(
                f"Seed file {seed_path} is not valid JSON and PyYAML is not "
                "installed. Either `pip install pyyaml` or write the seed "
                "file as JSON (JSON is valid YAML)."
            ) from e
        raise
    if not isinstance(doc, dict):
        raise ProfileError(f"Seed file {seed_path} must contain a mapping.")
    doc = {k: v for k, v in doc.items() if k not in MANAGED_KEYS}
    unknown = set(doc) - USER_KEYS
    if unknown:
        raise ProfileError(
            f"Seed file has unknown key(s): {sorted(unknown)}. "
            f"Valid: {sorted(USER_KEYS)}."
        )
    if not doc:
        raise ProfileError(f"Seed file {seed_path} sets none of {sorted(USER_KEYS)}.")
    with _Lock(path):
        if path.exists() and not force:
            raise ProfileError(
                f"A user profile already exists at {path}. Re-run with --force "
                "to replace it."
            )
        doc["onboarding"] = "seeded"
        _save(path, doc, new=True)
    print(f"Seeded user profile at {path} from {seed_path}")
    return 0


def cmd_skip(path: Path) -> int:
    with _Lock(path):
        doc = _load(path) or {}
        new = not path.exists()
        doc["onboarding"] = "skipped"
        _save(path, doc, new=new)
    print(f"Recorded that tailoring was offered and declined ({path}). "
          "It will not be offered again unless the user asks.")
    return 0


def cmd_clear(path: Path) -> int:
    if not path.exists():
        print(f"No user profile at {path} — nothing to do.")
        return 0
    path.unlink()
    print(f"Removed {path}")
    return 0


# -- CLI ------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="user_profile", description=__doc__.split("\n\n")[0])
    p.add_argument("--file", default=None,
                   help="User profile file (default: <state_dir>/user_profile.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("show", help="Show the stored user profile")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("guidance", help="Derive concrete defaults from the profile")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("set", help="Set one key (validated, atomic)")
    sp.add_argument("key")
    sp.add_argument("value", nargs="?")
    sp.add_argument("--json-value", help="JSON for list keys (goals, pain_points)")

    sp = sub.add_parser("unset", help="Remove one key")
    sp.add_argument("key")

    sp = sub.add_parser("seed", help="Pre-seed the profile from a YAML/JSON file")
    sp.add_argument("--file", required=True, dest="seed_file")
    sp.add_argument("--force", action="store_true",
                    help="Replace an existing profile")

    sub.add_parser("skip", help="Record that tailoring was offered and declined")
    sub.add_parser("clear", help="Delete the stored user profile")

    args = p.parse_args(argv)
    path = Path(args.file).expanduser() if args.file else user_profile_path()

    try:
        if args.cmd == "show":
            return cmd_show(path, args.json)
        if args.cmd == "guidance":
            return cmd_guidance(path, args.json)
        if args.cmd == "set":
            return cmd_set(path, args.key, args.value, args.json_value)
        if args.cmd == "unset":
            return cmd_unset(path, args.key)
        if args.cmd == "seed":
            return cmd_seed(path, args.seed_file, args.force)
        if args.cmd == "skip":
            return cmd_skip(path)
        if args.cmd == "clear":
            return cmd_clear(path)
    except ProfileError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
