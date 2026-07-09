"""
Verify the skill installation is complete and runnable — run this FIRST.

Why this exists: a broken install fails confusingly at first real use (a
platform import that only shipped SKILL.md, a missing optional dep, an env
var set in one shell but not another). This script surfaces all of that in
seconds, offline by default.

Usage:
    python -m scripts.selftest                      # offline checks only
    python -m scripts.selftest --profile main       # + connectivity check
    python -m scripts.selftest --json               # machine-readable

Exit codes: 0 — everything required passed; 1 — something is broken.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path


REQUIRED_FILES = [
    "SKILL.md",
    "VERSION",
    "assets/profiles.example.yaml",
    "references/api_reference.md",
    "references/domain_syntax.md",
    "references/odoo19_field_changes.md",
    "references/models/sales.md",
]
SCRIPT_MODULES = [
    "odoo_client", "introspect", "profiles", "verify_profile", "get_attachment",
    "cache_sync", "cache_status", "detect_env", "check_updates", "show_metrics",
]
_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def run_checks(profile: str | None) -> dict:
    checks: list[dict] = []

    def check(name: str, ok: bool | None, detail: str, required: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "required": required})

    # Python version — the scripts use stdlib-only features from 3.9+.
    py_ok = sys.version_info >= (3, 9)
    check("python", py_ok, f"{sys.version.split()[0]}"
          + ("" if py_ok else " — need Python 3.9+"))

    # Skill files present (catches partial installs that only shipped SKILL.md).
    root = _skill_root()
    missing = [f for f in REQUIRED_FILES if not (root / f).exists()]
    missing += [f"scripts/{m}.py" for m in SCRIPT_MODULES
                if not (root / "scripts" / f"{m}.py").exists()]
    check("skill_files", not missing,
          "all present" if not missing else f"MISSING: {', '.join(missing)} — "
          "the install is incomplete; re-download the full skill folder")

    # Every script imports cleanly.
    sys.path.insert(0, str(root / "scripts"))
    bad_imports = []
    for mod in SCRIPT_MODULES:
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001
            bad_imports.append(f"{mod}: {type(e).__name__}: {e}")
    check("imports", not bad_imports,
          "all scripts import" if not bad_imports else "; ".join(bad_imports))

    # Optional dependencies — absence is informational, not a failure.
    for dep, why in (("yaml", "YAML-authored profiles (JSON works without it)"),
                     ("duckdb", "the DuckDB cache backend (sqlite works without it)")):
        try:
            importlib.import_module(dep)
            check(f"optional_{dep}", True, "installed", required=False)
        except ImportError:
            check(f"optional_{dep}", None, f"not installed — only needed for {why}",
                  required=False)

    # Everything below needs odoo_client itself; if THAT import failed, report
    # what we have instead of dying with a traceback — a broken install is
    # exactly the scenario this script exists to diagnose.
    try:
        from odoo_client import _default_profiles_path, _load_config, ProfileError, state_dir  # type: ignore
    except Exception as e:  # noqa: BLE001
        check("core_module", False,
              f"odoo_client failed to import ({type(e).__name__}: {e}) — "
              "profile/state/connectivity checks skipped")
        required_failed = [c for c in checks if c["required"] and c["ok"] is False]
        return {"ok": not required_failed, "skill_root": str(root), "checks": checks}

    # Profiles file: existence, parseability, and env-var readiness.
    ppath = _default_profiles_path()
    if not ppath.exists():
        check("profiles_file", None,
              f"none at {ppath} — first-run onboarding will create it", required=False)
    else:
        try:
            doc = _load_config(ppath)
            profiles = doc.get("profiles") or {}
            check("profiles_file", bool(profiles),
                  f"{ppath} parses ({'JSON' if _is_json(ppath) else 'YAML'}), "
                  f"{len(profiles)} profile(s): {', '.join(sorted(profiles))}")
            referenced = sorted(set(_ENV_VAR_RE.findall(json.dumps(doc))))
            unset = [v for v in referenced if os.environ.get(v) is None]
            check("profile_env_vars", not unset,
                  "all referenced env vars are set in this shell" if not unset
                  else f"NOT SET in this shell: {', '.join(unset)} — profiles "
                       "referencing them will fail to load here")
        except ProfileError as e:
            check("profiles_file", False, str(e))

    # State dir writable (call log, caches).
    sdir = state_dir()
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=sdir):
            pass
        check("state_dir", True, f"{sdir} is writable")
    except OSError as e:
        check("state_dir", False, f"{sdir} not writable: {e}")

    # Env overrides in effect — so headless/multi-agent hosts can see their wiring.
    overrides = {v: os.environ.get(v) for v in (
        "ODOO_PROFILES_PATH", "ODOO_SIDEKICK_STATE_DIR", "ODOO_SIDEKICK_CALLER",
        "ODOO_SIDEKICK_AUTH_REF", "ODOO_SIDEKICK_HEADLESS", "ODOO_SIDEKICK_MAX_RETRIES",
    ) if os.environ.get(v)}
    check("env_overrides", None,
          ", ".join(f"{k}={v}" for k, v in overrides.items()) or "none set",
          required=False)

    # Optional connectivity check.
    if profile:
        try:
            from odoo_client import OdooClient, OdooClientError  # type: ignore
            import time
            client = OdooClient.from_profile(profile)
            t0 = time.monotonic()
            n = client.search_count("res.partner", [])
            ms = int((time.monotonic() - t0) * 1000)
            check("connectivity", True,
                  f"profile {profile!r} reached {client.profile.url} "
                  f"({n} partners, {ms} ms)")
        except OdooClientError as e:
            check("connectivity", False, f"{e}")

    required_failed = [c for c in checks if c["required"] and c["ok"] is False]
    return {
        "ok": not required_failed,
        "skill_root": str(root),
        "checks": checks,
    }


def _is_json(path: Path) -> bool:
    try:
        json.loads(path.read_text())
        return True
    except (json.JSONDecodeError, OSError):
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="selftest", description=__doc__.split("\n\n")[0])
    p.add_argument("--profile", help="Also test connectivity using this profile")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    args = p.parse_args(argv)

    result = run_checks(args.profile)
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        symbols = {True: "ok  ", False: "FAIL", None: "--  "}
        for c in result["checks"]:
            print(f"[{symbols[c['ok']]}] {c['name']:<18} {c['detail']}")
        print(f"\n{'All required checks passed.' if result['ok'] else 'PROBLEMS FOUND — see FAIL lines above.'}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
