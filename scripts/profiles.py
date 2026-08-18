"""
Manage the profiles config file safely — the supported way to edit profiles.

Why this exists: editing the profiles file with string surgery (sed, ad-hoc
scripts) has corrupted real deployments — a YAML-shaped edit silently no-ops
against a JSON-content file, and concurrent editors race each other. This CLI
validates every change against the profile schema, takes an advisory lock,
and writes atomically (temp file + rename), so a half-written or invalid
config can never land on disk.

Canonical format: JSON. JSON is a subset of YAML, so a JSON-content
profiles.yaml stays readable by YAML tooling AND loads without PyYAML
installed. Files authored as YAML are still accepted everywhere; run
`migrate` to convert one to canonical JSON.

Env var placeholders (${VAR}) are preserved verbatim — this tool never
resolves or stores secrets.

Usage:
    python3 -m scripts.profiles list [--json]
    python3 -m scripts.profiles show <name> [--json]
    python3 -m scripts.profiles add <name> --url URL --api-key-env VAR [--mode MODE] [--database DB]
    python3 -m scripts.profiles set <name> <key> <value>       # scalar keys
    python3 -m scripts.profiles set <name> write_policy --json-value '{"allow": [...], "deny_models": [...]}'
    python3 -m scripts.profiles unset <name> <key>
    python3 -m scripts.profiles remove <name>
    python3 -m scripts.profiles set-default <name>
    python3 -m scripts.profiles migrate                        # rewrite as canonical JSON

Examples:
    python3 -m scripts.profiles set kingdom_prod mode read-write
    python3 -m scripts.profiles set kingdom_prod require_auth_ref true
    python3 -m scripts.profiles unset kingdom_prod confirm_cmd
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from .odoo_client import (
        ProfileError, VALID_MODES, _default_profiles_path, _load_config,
        _validate_write_policy,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from odoo_client import (  # type: ignore
        ProfileError, VALID_MODES, _default_profiles_path, _load_config,
        _validate_write_policy,
    )


# Keys editable with plain `set <name> <key> <value>` and how to coerce them.
SCALAR_KEYS = {
    "url": str,
    "api_key": str,
    "database": str,
    "mode": str,
    "confirm_cmd": str,
    "require_auth_ref": "bool",
}
# Structured keys — must be set with --json-value.
JSON_KEYS = {"write_policy", "default_context"}


def _coerce(key: str, value: str) -> Any:
    kind = SCALAR_KEYS[key]
    if kind == "bool":
        low = value.strip().lower()
        if low in ("true", "yes", "1", "on"):
            return True
        if low in ("false", "no", "0", "off"):
            return False
        raise ProfileError(f"{key} must be a boolean (true/false), got {value!r}")
    return value


def _validate_document(doc: dict[str, Any]) -> None:
    """Validate the whole config document before it's allowed onto disk."""
    if not isinstance(doc, dict):
        raise ProfileError("Config root must be a mapping.")
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ProfileError("Config needs a non-empty 'profiles' mapping.")
    default = doc.get("default")
    if default is not None and default not in profiles:
        raise ProfileError(f"'default' points at unknown profile {default!r}.")
    for name, p in profiles.items():
        if not isinstance(p, dict):
            raise ProfileError(f"Profile {name!r} must be a mapping.")
        url = p.get("url")
        if not isinstance(url, str) or not (
            url.startswith(("http://", "https://")) or "${" in url
        ):
            raise ProfileError(f"Profile {name!r} needs a url starting with http(s):// "
                               "(or an ${ENV_VAR} placeholder).")
        if not isinstance(p.get("api_key"), str) or not p["api_key"]:
            raise ProfileError(f"Profile {name!r} needs an api_key (a value or ${{ENV_VAR}}).")
        mode = p.get("mode", "read-only")
        if mode not in VALID_MODES:
            raise ProfileError(f"Profile {name!r}: mode must be one of {VALID_MODES}, got {mode!r}.")
        if "write_policy" in p and p["write_policy"] is not None:
            _validate_write_policy(p["write_policy"], name)
        if "require_auth_ref" in p and not isinstance(p["require_auth_ref"], bool):
            raise ProfileError(f"Profile {name!r}: require_auth_ref must be a boolean.")
        if "default_context" in p and not isinstance(p["default_context"], dict):
            raise ProfileError(f"Profile {name!r}: default_context must be a mapping.")


class _Lock:
    """Advisory lock on <profiles>.lock so concurrent editors serialize.
    POSIX-only (fcntl); on platforms without fcntl it degrades to no lock —
    the atomic rename still prevents torn writes."""

    def __init__(self, target: Path, timeout: float = 10.0):
        self.path = target.with_name(target.name + ".lock")
        self.timeout = timeout
        self._fh = None

    def __enter__(self) -> "_Lock":
        try:
            import fcntl
        except ImportError:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    self._fh.close()
                    self._fh = None
                    raise ProfileError(
                        f"Could not acquire lock {self.path} within {self.timeout}s — "
                        "another process is editing the profiles file."
                    ) from None
                time.sleep(0.1)

    def __exit__(self, *exc: Any) -> None:
        if self._fh is not None:
            self._fh.close()  # closing releases the flock


def _atomic_write(path: Path, doc: dict[str, Any]) -> None:
    """Serialize as canonical JSON and rename into place. 0600 — the file may
    hold inline API keys.

    If `path` is a symlink, write through it to the real target rather than
    replacing the link with a regular file: some deployments symlink
    profiles.yaml at a canonical store (e.g. profiles.json), and a bare
    os.replace here would silently break that link and desync tools that read
    the target directly. Following the link keeps the write atomic on the
    target's own filesystem.
    """
    if path.is_symlink():
        path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _redact_key(value: str) -> str:
    if "${" in value:
        return value  # env placeholder, not a secret
    if len(value) <= 8:
        return "<redacted>"
    return value[:4] + "…<redacted>"


def _redacted_profile(p: dict[str, Any]) -> dict[str, Any]:
    out = dict(p)
    if isinstance(out.get("api_key"), str):
        out["api_key"] = _redact_key(out["api_key"])
    return out


def _load_for_edit(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ProfileError(
            f"No profiles file at {path}. Create one with "
            "`python3 -m scripts.profiles add <name> --url ... --api-key-env VAR`."
        )
    return _load_config(path)


def _detect_format(path: Path) -> str:
    try:
        json.loads(path.read_text())
        return "json"
    except (json.JSONDecodeError, OSError):
        return "yaml"


def _require_profile(doc: dict[str, Any], name: str) -> dict[str, Any]:
    profiles = doc.get("profiles") or {}
    if name not in profiles:
        raise ProfileError(f"Unknown profile {name!r}. Available: {sorted(profiles.keys())}")
    return profiles[name]


# -- Commands -------------------------------------------------------------------

def cmd_list(path: Path, as_json: bool) -> int:
    doc = _load_for_edit(path)
    profiles = doc.get("profiles") or {}
    rows = []
    for name, p in sorted(profiles.items()):
        rows.append({
            "name": name,
            "mode": p.get("mode", "read-only"),
            "url": p.get("url"),
            "is_default": name == doc.get("default"),
            "write_policy": bool(p.get("write_policy")),
            "confirm_cmd": bool(p.get("confirm_cmd")),
            "require_auth_ref": bool(p.get("require_auth_ref")),
        })
    if as_json:
        json.dump({"path": str(path), "format": _detect_format(path),
                   "default": doc.get("default"), "profiles": rows},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    print(f"Profiles file: {path} (format: {_detect_format(path)})")
    print(f"{'name':<20} {'mode':<12} {'flags':<28} url")
    print("-" * 90)
    for r in rows:
        flags = []
        if r["is_default"]:
            flags.append("default")
        if r["write_policy"]:
            flags.append("write_policy")
        if r["confirm_cmd"]:
            flags.append("confirm_cmd")
        if r["require_auth_ref"]:
            flags.append("require_auth_ref")
        print(f"{r['name']:<20} {r['mode']:<12} {','.join(flags) or '-':<28} {r['url']}")
    return 0


def cmd_show(path: Path, name: str, as_json: bool) -> int:
    doc = _load_for_edit(path)
    p = _redacted_profile(_require_profile(doc, name))
    if as_json:
        json.dump(p, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    print(f"Profile {name!r} in {path}"
          + (" (default)" if doc.get("default") == name else "") + ":")
    for key, value in p.items():
        rendered = json.dumps(value) if isinstance(value, (dict, list)) else value
        print(f"  {key:<18} {rendered}")
    return 0


def cmd_add(path: Path, name: str, url: str, api_key: str | None,
            api_key_env: str | None, mode: str, database: str | None) -> int:
    if bool(api_key) == bool(api_key_env):
        raise ProfileError("Pass exactly one of --api-key or --api-key-env.")
    if api_key:
        print("WARNING: --api-key puts the secret on the command line (shell history, "
              "process lists, session transcripts). Prefer --api-key-env.", file=sys.stderr)
    key = api_key if api_key else "${%s}" % api_key_env
    with _Lock(path):
        doc = _load_for_edit(path) if path.exists() else {"profiles": {}}
        profiles = doc.setdefault("profiles", {})
        if name in profiles:
            raise ProfileError(f"Profile {name!r} already exists. Use `set` to modify it.")
        profile: dict[str, Any] = {"url": url, "api_key": key, "mode": mode}
        if database:
            profile["database"] = database
        profiles[name] = profile
        if not doc.get("default"):
            doc["default"] = name
        _validate_document(doc)
        _atomic_write(path, doc)
    print(f"Added profile {name!r} (mode: {mode}) to {path}")
    if api_key_env:
        print(f"Remember to set the env var: export {api_key_env}=<your key>")
    return 0


def cmd_set(path: Path, name: str, key: str, value: str | None, json_value: str | None) -> int:
    with _Lock(path):
        doc = _load_for_edit(path)
        profile = _require_profile(doc, name)
        if json_value is not None:
            if key not in JSON_KEYS and key not in SCALAR_KEYS:
                raise ProfileError(f"Unknown profile key {key!r}.")
            try:
                profile[key] = json.loads(json_value)
            except json.JSONDecodeError as e:
                raise ProfileError(f"--json-value is not valid JSON: {e}") from e
        elif key in SCALAR_KEYS:
            if value is None:
                raise ProfileError(f"`set {name} {key}` needs a value.")
            if key == "api_key" and "${" not in value:
                print("WARNING: setting api_key inline puts the secret on the command "
                      "line (shell history, process lists, session transcripts). "
                      "Prefer a ${ENV_VAR} placeholder.", file=sys.stderr)
            profile[key] = _coerce(key, value)
        elif key in JSON_KEYS:
            raise ProfileError(
                f"{key} is structured — pass it with --json-value '<json>'."
            )
        else:
            raise ProfileError(
                f"Unknown profile key {key!r}. Editable: "
                f"{sorted(SCALAR_KEYS)} and (via --json-value) {sorted(JSON_KEYS)}."
            )
        _validate_document(doc)
        _atomic_write(path, doc)
        result = _redacted_profile(profile)
    print(f"Updated {name}.{key} in {path}:")
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_unset(path: Path, name: str, key: str) -> int:
    if key in ("url", "api_key"):
        raise ProfileError(f"{key} is required and cannot be unset.")
    with _Lock(path):
        doc = _load_for_edit(path)
        profile = _require_profile(doc, name)
        if key not in profile:
            print(f"{name}.{key} was not set — nothing to do.")
            return 0
        del profile[key]
        _validate_document(doc)
        _atomic_write(path, doc)
    print(f"Removed {name}.{key} in {path}")
    return 0


def cmd_remove(path: Path, name: str) -> int:
    with _Lock(path):
        doc = _load_for_edit(path)
        _require_profile(doc, name)
        # Refuse before mutating the in-memory doc — keeps the invariant local
        # instead of depending on nothing being written before the raise.
        if len(doc["profiles"]) == 1:
            raise ProfileError(
                f"Refusing to remove the last profile. Delete {path} manually if "
                "you really want an empty config."
            )
        del doc["profiles"][name]
        if doc.get("default") == name:
            doc["default"] = sorted(doc["profiles"].keys())[0]
        _validate_document(doc)
        _atomic_write(path, doc)
    print(f"Removed profile {name!r} from {path}"
          + (f" (default is now {doc['default']!r})" if doc.get("default") else ""))
    return 0


def cmd_set_default(path: Path, name: str) -> int:
    with _Lock(path):
        doc = _load_for_edit(path)
        _require_profile(doc, name)
        doc["default"] = name
        _validate_document(doc)
        _atomic_write(path, doc)
    print(f"Default profile is now {name!r} in {path}")
    return 0


def cmd_migrate(path: Path) -> int:
    with _Lock(path):
        fmt = _detect_format(path)  # inside the lock: check-then-act must serialize
        doc = _load_for_edit(path)
        _validate_document(doc)
        if fmt == "json":
            print(f"{path} is already canonical JSON — nothing to do.")
            return 0
        backup = path.with_name(path.name + ".yaml-backup")
        # 0600 like the live file — the backup holds the same inline keys
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(path.read_text())
        os.chmod(backup, 0o600)  # in case the file pre-existed with wider perms
        _atomic_write(path, doc)
    print(f"Rewrote {path} as canonical JSON (comments are not preserved).")
    print(f"Original YAML kept at {backup}")
    return 0


# -- CLI ------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="profiles", description=__doc__.split("\n\n")[0])
    p.add_argument("--file", default=None,
                   help="Profiles file (default: ODOO_PROFILES_PATH or "
                        "~/.config/odoo-sidekick/profiles.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="List profiles (never prints keys)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("show", help="Show one profile (api_key redacted)")
    sp.add_argument("name")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("add", help="Add a new profile")
    sp.add_argument("name")
    sp.add_argument("--url", required=True)
    sp.add_argument("--api-key", help="Inline API key (prefer --api-key-env)")
    sp.add_argument("--api-key-env", help="Env var name; stored as ${VAR}")
    sp.add_argument("--mode", choices=list(VALID_MODES), default="read-only")
    sp.add_argument("--database", help="DB name, or 'auto' to resolve at runtime")

    sp = sub.add_parser("set", help="Set one key on a profile (validated, atomic)")
    sp.add_argument("name")
    sp.add_argument("key")
    sp.add_argument("value", nargs="?")
    sp.add_argument("--json-value", help="JSON for structured keys (write_policy, default_context)")

    sp = sub.add_parser("unset", help="Remove an optional key from a profile")
    sp.add_argument("name")
    sp.add_argument("key")

    sp = sub.add_parser("remove", help="Remove a profile")
    sp.add_argument("name")

    sp = sub.add_parser("set-default", help="Set the default profile")
    sp.add_argument("name")

    sub.add_parser("migrate", help="Rewrite the file as canonical JSON")

    args = p.parse_args(argv)
    path = Path(args.file).expanduser() if args.file else _default_profiles_path()

    try:
        if args.cmd == "list":
            return cmd_list(path, args.json)
        if args.cmd == "show":
            return cmd_show(path, args.name, args.json)
        if args.cmd == "add":
            return cmd_add(path, args.name, args.url, args.api_key,
                           args.api_key_env, args.mode, args.database)
        if args.cmd == "set":
            return cmd_set(path, args.name, args.key, args.value, args.json_value)
        if args.cmd == "unset":
            return cmd_unset(path, args.name, args.key)
        if args.cmd == "remove":
            return cmd_remove(path, args.name)
        if args.cmd == "set-default":
            return cmd_set_default(path, args.name)
        if args.cmd == "migrate":
            return cmd_migrate(path)
    except ProfileError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
