"""
Check whether a newer version of Odoo Sidekick is available upstream.

How it works:
  - Reads the local VERSION file at the skill root.
  - Fetches the upstream VERSION from:
      https://raw.githubusercontent.com/SHIFT-collective/Odoo-Sidekick/main/VERSION
  - Compares using tuple-based version parsing (so 1.10 > 1.9, 1.5.1 > 1.5).
  - If an update is available, fetches the latest GitHub Release for the
    changelog/release notes.
  - Caches the result for 24 hours in ~/.config/odoo-sidekick/update_check.json
    to avoid hammering GitHub on every skill invocation.

Failure modes are intentional and graceful:
  - Network unreachable / GitHub down / firewall: returns ok=False with a
    descriptive error. Never raises.
  - Local VERSION missing: returns local_version="unknown".
  - Cache file corrupt: ignored, fresh check done.
  - Rate limited or 404 on releases endpoint: VERSION comparison still works,
    release notes just won't be included.

Usage:
    python -m scripts.check_updates              # use cache if fresh
    python -m scripts.check_updates --force      # bypass cache
    python -m scripts.check_updates --json       # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# -- Configuration ------------------------------------------------------------

GITHUB_OWNER = "SHIFT-collective"
GITHUB_REPO = "Odoo-Sidekick"
GITHUB_BRANCH = "main"

UPSTREAM_VERSION_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/"
    f"{GITHUB_BRANCH}/VERSION"
)
RELEASES_LATEST_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
)
RELEASES_HTML_URL = (
    f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
)

DEFAULT_CACHE_TTL_HOURS = 24
NETWORK_TIMEOUT_S = 10.0


# -- Paths --------------------------------------------------------------------

def _skill_root() -> Path:
    """Return the skill root (this file's parent's parent)."""
    return Path(__file__).resolve().parent.parent


def _cache_path() -> Path:
    override = os.environ.get("ODOO_SIDEKICK_STATE_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".config" / "odoo-sidekick"
    return base / "update_check.json"


# -- Local version ------------------------------------------------------------

def get_local_version() -> str:
    """Read the local VERSION file. Returns 'unknown' if missing/unreadable."""
    f = _skill_root() / "VERSION"
    try:
        return f.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _is_git_install() -> bool:
    """True if the skill folder is a git checkout (suggests git pull is the right update path)."""
    return (_skill_root() / ".git").is_dir()


# -- Version parsing ----------------------------------------------------------

def _parse_version(v: str) -> tuple:
    """Parse 'x.y.z' or 'vx.y.z' into a tuple for comparison.

    Examples:
        '1.5'        -> (1, 5)
        '1.10'       -> (1, 10)
        '1.5.1'      -> (1, 5, 1)
        '1.6-beta'   -> (1, 6, 'beta')
        'v2.0'       -> (2, 0)
    """
    v = v.strip().lstrip("vV")
    parts = re.split(r"[.\-+]", v)
    out: list[Any] = []
    for p in parts:
        if not p:
            continue
        try:
            out.append(int(p))
        except ValueError:
            out.append(p)
    return tuple(out)


def _is_newer(remote: str, local: str) -> bool:
    """Is `remote` strictly newer than `local`? Returns False on parse errors."""
    if local == "unknown":
        return True  # can't compare; assume update may help
    try:
        return _parse_version(remote) > _parse_version(local)
    except Exception:  # noqa: BLE001 — be defensive on malformed versions
        return remote != local


# -- Network ------------------------------------------------------------------

def _fetch(url: str, timeout: float = NETWORK_TIMEOUT_S) -> str | None:
    """Fetch a URL, return body as string. Returns None on any error."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "odoo-sidekick-update-check/1.0",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


# -- Cache --------------------------------------------------------------------

def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(data: dict) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))
    except OSError:
        pass  # non-fatal — running without persistent cache is fine


# -- Main check ---------------------------------------------------------------

def check(
    force: bool = False,
    cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS,
) -> dict:
    """Run an update check. Uses cache if fresh unless force=True."""
    local = get_local_version()
    now = datetime.now(timezone.utc)

    # Try cache first
    if not force:
        cached = _read_cache()
        if cached:
            try:
                last_str = cached.get("checked_at", "")
                last_dt = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
                if now - last_dt < timedelta(hours=cache_ttl_hours):
                    cached["from_cache"] = True
                    cached["local_version"] = local  # refresh local in case file changed
                    return cached
            except (ValueError, AttributeError, TypeError):
                pass

    # Fresh check
    upstream_raw = _fetch(UPSTREAM_VERSION_URL)
    if upstream_raw is None:
        return {
            "ok": False,
            "from_cache": False,
            "local_version": local,
            "latest_version": None,
            "update_available": None,
            "error": "Could not reach the upstream VERSION file (network, firewall, or repo issue).",
            "checked_at": now.isoformat(),
        }

    latest = upstream_raw.strip()
    update_available = _is_newer(latest, local)

    result: dict = {
        "ok": True,
        "from_cache": False,
        "local_version": local,
        "latest_version": latest,
        "update_available": update_available,
        "is_git_install": _is_git_install(),
        "releases_url": RELEASES_HTML_URL,
        "checked_at": now.isoformat(),
    }

    # If update is available, try to also fetch release notes
    if update_available:
        release_body = _fetch(RELEASES_LATEST_URL, timeout=5.0)
        if release_body:
            try:
                release = json.loads(release_body)
                result["release_name"] = release.get("name") or release.get("tag_name")
                result["release_notes"] = (release.get("body") or "").strip()
                result["release_url"] = release.get("html_url") or RELEASES_HTML_URL
            except json.JSONDecodeError:
                pass

    _write_cache(result)
    return result


# -- Output -------------------------------------------------------------------

def print_human(info: dict) -> None:
    if not info.get("ok"):
        print(f"Update check failed: {info.get('error', 'unknown error')}")
        print(f"Local version: {info.get('local_version')}")
        return

    print(f"Local version:    {info['local_version']}")
    print(f"Latest version:   {info['latest_version']}")
    print(f"Update available: {'YES' if info['update_available'] else 'no'}")
    if info.get("from_cache"):
        print(f"(cached at {info.get('checked_at')}; pass --force to re-check)")

    if info["update_available"]:
        print()
        if info.get("release_name"):
            print(f"Release: {info['release_name']}")
        if info.get("release_notes"):
            print("\nRelease notes:")
            lines = info["release_notes"].splitlines()
            for line in lines[:20]:
                print(f"  {line}")
            if len(lines) > 20:
                print(f"  ... ({len(lines) - 20} more lines — see {info.get('release_url', info['releases_url'])})")
        print()
        if info.get("is_git_install"):
            print("To update: `cd <skill-folder> && git pull origin main`")
        else:
            print(f"To update: download the latest from {info.get('release_url') or info['releases_url']}")
        print("Your profile config in ~/.config/odoo-sidekick/ will be preserved across updates.")


# -- CLI ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="check_updates",
        description="Check whether a newer Odoo Sidekick is available upstream.",
    )
    p.add_argument("--force", action="store_true", help="Ignore cache; do a fresh check.")
    p.add_argument("--json", action="store_true", help="Output JSON instead of human-readable text.")
    args = p.parse_args(argv)
    info = check(force=args.force)
    if args.json:
        json.dump(info, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print_human(info)
    return 0 if info.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
