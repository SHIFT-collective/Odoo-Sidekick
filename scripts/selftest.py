"""
Verify the skill installation is complete and runnable — run this FIRST.

Why this exists: a broken install fails confusingly at first real use (a
platform import that only shipped SKILL.md, a missing optional dep, an env
var set in one shell but not another). This script surfaces all of that in
seconds, offline by default.

Usage:
    python3 -m scripts.selftest                      # offline checks only
    python3 -m scripts.selftest --profile main       # + connectivity check
    python3 -m scripts.selftest --json               # machine-readable

Usage (deploy-pipeline / acceptance-criterion form):
    python3 -m scripts.selftest --list                       # names of every check
    python3 -m scripts.selftest --only imports,state_dir     # gate on specific checks
    python3 -m scripts.selftest --only connectivity --profile main

Exit codes: 0 — everything required passed (or, with --only, every named check
was asserted as passing, i.e. ok is True); 1 — a selected/required check failed
(ok is False); 2 — with --only, a named check could not be asserted as passing:
it was never emitted (typo, or skipped because an earlier stage bailed out) OR
it is informational with no pass/fail answer (ok is None — e.g. optional_yaml
when PyYAML isn't installed, profiles_file before onboarding, env_overrides).

Why --only exists: the aggregate exit code stops discriminating the moment any
unrelated check goes red, so it can't answer "did *this* check pass?" in a
pipeline. --only narrows the pass/fail verdict (and the exit code) to exactly
the checks named. A gate must be *asserted*, so an informational (--) check
never greens a --only gate — it is exit 2, the same "can't assert" signal as a
missing check.
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
    "assets/user_profile.example.yaml",
    "references/api_reference.md",
    "references/domain_syntax.md",
    "references/odoo19_field_changes.md",
    "references/models/sales.md",
]
SCRIPT_MODULES = [
    "odoo_client", "introspect", "profiles", "verify_profile", "get_attachment",
    "cache_sync", "cache_status", "detect_env", "check_updates", "show_metrics",
    "user_profile",
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

    # SKILL.md frontmatter description must stay under the platform's
    # 1024-char limit — an oversized description breaks skill triggering.
    skill_md = root / "SKILL.md"
    if skill_md.exists():
        m = re.search(r"^description: (.*)$", skill_md.read_text(), re.M)
        if m:
            n = len(m.group(1))
            check("frontmatter_description", n <= 1024,
                  f"{n}/1024 chars" + ("" if n <= 1024 else " — OVER LIMIT, trim it"))

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
    # Emit core_module on the success path too — otherwise it is a failure-only
    # sentinel and `--only core_module` could never return success.
    check("core_module", True, "odoo_client imports")

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

    # User profile (v1.9 tailoring context) — optional, but a corrupt file
    # should be visible here rather than erroring at session start.
    try:
        from user_profile import user_profile_path, _load as _load_user_profile  # type: ignore
        upath = user_profile_path()
        if not upath.exists():
            check("user_profile", None,
                  f"none at {upath} — tailoring not captured (optional; "
                  "seed with scripts.user_profile)", required=False)
        else:
            updoc = _load_user_profile(upath)
            role = (updoc or {}).get("role")
            state = (updoc or {}).get("onboarding")
            check("user_profile", True,
                  f"{upath} parses"
                  + (f", role: {role}" if role else "")
                  + (f" ({state})" if state else ""), required=False)
    except ProfileError as e:
        check("user_profile", False, f"{e} — fix or `python3 -m "
              "scripts.user_profile clear` to reset", required=False)
    except Exception as e:  # noqa: BLE001
        check("user_profile", False, f"{type(e).__name__}: {e}", required=False)

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
        except Exception as e:  # noqa: BLE001 — selftest must never crash on
            # the exact failure it exists to diagnose
            check("connectivity", False, f"unexpected {type(e).__name__}: {e}")

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


# Every check name run_checks can emit (offline set; `connectivity` is added
# only with --profile). Kept as a list so `--list` works without running checks
# and `--only` can validate names up front.
CHECK_NAMES = [
    "python", "skill_files", "frontmatter_description", "imports",
    "optional_yaml", "optional_duckdb", "core_module", "profiles_file",
    "profile_env_vars", "state_dir", "user_profile", "env_overrides",
    "connectivity",
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="selftest", description=__doc__.split("\n\n")[0])
    p.add_argument("--profile", help="Also test connectivity using this profile")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p.add_argument("--list", action="store_true",
                   help="List every check name and exit (no checks run)")
    p.add_argument("--only", metavar="NAME[,NAME...]",
                   help="Restrict the pass/fail verdict and exit code to these "
                        "checks; exit 2 if a named check was not emitted")
    args = p.parse_args(argv)

    if args.list:
        if args.json:
            json.dump({"checks": CHECK_NAMES}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            for name in CHECK_NAMES:
                print(name)
        return 0

    only: set[str] | None = None
    if args.only:
        only = {n.strip() for n in args.only.split(",") if n.strip()}
        unknown = only - set(CHECK_NAMES)
        if unknown:
            p.error(f"--only names unknown check(s): {', '.join(sorted(unknown))}. "
                    f"Valid: {', '.join(CHECK_NAMES)}")

    result = run_checks(args.profile)

    # --only: narrow the verdict to the named checks. A gate must be *asserted*
    # as passing, so only `ok is True` counts as pass. A hard failure (ok False)
    # is exit 1; a check that never ran (missing) or that is informational with
    # no pass/fail answer (ok None — e.g. optional_yaml not installed,
    # profiles_file absent on first run) cannot be asserted as passing and is
    # exit 2, never a silent exit 0.
    exit_code = 0 if result["ok"] else 1
    if only is not None:
        emitted = {c["name"] for c in result["checks"]}
        missing = only - emitted
        selected = [c for c in result["checks"] if c["name"] in only]
        failed = [c for c in selected if c["ok"] is False]
        unassertable = [c["name"] for c in selected if c["ok"] is None]
        result = {**result, "checks": selected, "only": sorted(only),
                  "missing": sorted(missing), "unassertable": sorted(unassertable),
                  "ok": bool(selected) and not failed and not missing
                        and not unassertable}
        if failed:
            exit_code = 1
        elif missing or unassertable:
            exit_code = 2
        else:
            exit_code = 0

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        symbols = {True: "ok  ", False: "FAIL", None: "--  "}
        for c in result["checks"]:
            print(f"[{symbols[c['ok']]}] {c['name']:<18} {c['detail']}")
        for name in result.get("missing", []):
            print(f"[MISS] {name:<18} check was not emitted (skipped upstream?)")
        if exit_code == 0:
            print("\nAll required checks passed." if only is None
                  else f"\nAll --only checks passed: {', '.join(sorted(only))}.")
        elif exit_code == 2:
            bits = []
            if result.get("missing"):
                bits.append(f"not emitted: {', '.join(result['missing'])}")
            if result.get("unassertable"):
                bits.append("informational, no pass/fail answer: "
                            f"{', '.join(result['unassertable'])}")
            print("\nPROBLEM — a --only check could not be asserted as passing "
                  f"({'; '.join(bits)}).")
        else:
            print("\nPROBLEMS FOUND — see FAIL lines above.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
