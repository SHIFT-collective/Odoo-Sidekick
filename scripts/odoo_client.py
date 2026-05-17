"""
Odoo V19 JSON-2 API client with two modes:

  - read-only (default): only methods in READ_METHODS may be called.
    Any write attempt raises WriteNotAllowed before the HTTP call is made.

  - read-write: read methods work normally; write methods (create/write/
    unlink/copy and any non-read method like action_*/button_*) require
    confirm=True per call. Without confirm=True, WriteNotConfirmed is raised
    with a description of what would have been sent — a built-in dry-run.

The CLI mirrors the library: pass --confirm for writes; without it, you get
a dry-run preview.

Profile config:
    Default path: ~/.config/odoo-sidekick/profiles.yaml
    Override with env var ODOO_PROFILES_PATH.
    See assets/profiles.example.yaml for format.

Usage as a library (reads):
    from scripts.odoo_client import OdooClient
    c = OdooClient.from_profile("main")
    rows = c.search_read("sale.order",
        domain=[["state","in",["sale","done"]]],
        fields=["name","partner_id","amount_total"], limit=10)

Usage as a library (writes — requires read-write mode AND confirm=True):
    new_ids = c.create("res.partner",
        [{"name": "Acme Inc.", "is_company": True}], confirm=True)
    c.write("res.partner", [new_ids[0]], {"phone": "+34 ..."}, confirm=True)
    c.unlink("res.partner", [new_ids[0]], confirm=True)
    c.execute("sale.order", "action_confirm", ids=[5], confirm=True)

Usage as a CLI:
    # read
    python -m scripts.odoo_client <profile> sale.order search_read \\
        --json '{"domain": [["state","=","sale"]], "fields": ["name"], "limit": 5}'

    # write — dry-run (no --confirm): prints the preview, sends nothing
    python -m scripts.odoo_client <profile> res.partner create \\
        --json '{"vals_list": [{"name": "X"}]}'

    # write — real (with --confirm)
    python -m scripts.odoo_client <profile> res.partner create \\
        --json '{"vals_list": [{"name": "X"}]}' --confirm
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import urllib.request
import urllib.error


# -- Modes ---------------------------------------------------------------------

MODE_READ_ONLY = "read-only"
MODE_READ_WRITE = "read-write"
VALID_MODES = (MODE_READ_ONLY, MODE_READ_WRITE)


# -- Method classification -----------------------------------------------------
#
# READ_METHODS are pure reads — safe in either mode, never need confirm.
# Anything else is treated as a write in read-write mode (requires confirm),
# or refused in read-only mode.

READ_METHODS: frozenset[str] = frozenset({
    "search",
    "search_count",
    "search_read",
    "read",
    "read_group",
    "fields_get",
    "name_search",
    "name_get",
    "web_search_read",
    "get_views",
    "default_get",
})

# Common write verbs we recognize by name. This is for clearer error messages
# and impact descriptions; in read-write mode any non-read method is gated on
# confirm even if it's not in this set (because Odoo has thousands of action_*
# and button_* methods we can't enumerate).
KNOWN_WRITE_METHODS: frozenset[str] = frozenset({
    "create",
    "write",
    "unlink",
    "copy",
    "copy_data",
    "web_save",
    "message_post",
    "message_subscribe",
    "message_unsubscribe",
})


# -- Exceptions ----------------------------------------------------------------

class OdooClientError(Exception):
    """Base for all client errors."""


class WriteNotAllowed(OdooClientError):
    """Refused: the profile is in read-only mode and a write was attempted."""


class WriteNotConfirmed(OdooClientError):
    """Refused: write method called without confirm=True.

    Carries `.preview` with what would have been sent so callers (and the CLI's
    dry-run mode) can show the user.
    """

    def __init__(self, message: str, preview: dict[str, Any]):
        super().__init__(message)
        self.preview = preview


class ProfileError(OdooClientError):
    """Profile config missing or malformed."""


class OdooAPIError(OdooClientError):
    """The Odoo server returned a 4xx/5xx with an error body."""

    def __init__(self, status: int, payload: dict[str, Any] | str, url: str):
        self.status = status
        self.payload = payload
        self.url = url
        if isinstance(payload, dict):
            name = payload.get("name", "UnknownError")
            msg = payload.get("message") or (payload.get("arguments") or ["<no message>"])[0]
            super().__init__(f"HTTP {status} from {url}: {name}: {msg}")
        else:
            super().__init__(f"HTTP {status} from {url}: {payload[:500]}")


# -- Profile loading -----------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _substitute_env(value: Any) -> Any:
    """Recursively replace ${ENV_VAR} placeholders in strings."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            var = m.group(1)
            v = os.environ.get(var)
            if v is None:
                raise ProfileError(f"Profile references env var ${{{var}}} which is not set")
            return v
        return _ENV_VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


def _default_profiles_path() -> Path:
    override = os.environ.get("ODOO_PROFILES_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "odoo-sidekick" / "profiles.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Tiny YAML loader, preferring PyYAML if present, otherwise JSON as a fallback.
    The parser is chosen by file content, so .yaml or .json both work.
    """
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise ProfileError(
            f"Profile file at {path} is not valid JSON, and PyYAML is not installed. "
            "Install with `pip install pyyaml` or rewrite the file as JSON."
        ) from e
    return yaml.safe_load(text)


@dataclass
class Profile:
    name: str
    url: str
    api_key: str
    database: str | None = None
    mode: str = MODE_READ_ONLY
    default_context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, name: str | None = None, profiles_path: Path | None = None) -> "Profile":
        path = profiles_path or _default_profiles_path()
        if not path.exists():
            raise ProfileError(
                f"No profiles file at {path}. Create one based on "
                "assets/profiles.example.yaml in the skill folder."
            )
        raw = _load_yaml(path)
        profiles = raw.get("profiles") or {}
        if not profiles:
            raise ProfileError(f"No profiles defined in {path}")
        if name is None:
            name = raw.get("default")
            if not name:
                raise ProfileError(
                    f"No profile name given and no 'default' set in {path}. "
                    f"Available: {sorted(profiles.keys())}"
                )
        if name not in profiles:
            raise ProfileError(
                f"Unknown profile {name!r}. Available: {sorted(profiles.keys())}"
            )
        p = _substitute_env(profiles[name])
        mode = p.get("mode", MODE_READ_ONLY)
        if mode not in VALID_MODES:
            raise ProfileError(
                f"Profile {name!r} has invalid mode {mode!r}. "
                f"Must be one of: {VALID_MODES}"
            )
        return cls(
            name=name,
            url=p["url"].rstrip("/"),
            api_key=p["api_key"],
            database=p.get("database"),
            mode=mode,
            default_context=p.get("default_context") or {},
        )


# -- Preview / dry-run helpers -------------------------------------------------

def _redact(args: dict[str, Any]) -> dict[str, Any]:
    """Redact common secret-shaped keys before showing a dry-run preview."""
    SENSITIVE = {"password", "api_key", "token", "secret", "private_key"}
    redacted = {}
    for k, v in args.items():
        if any(s in k.lower() for s in SENSITIVE):
            redacted[k] = "<redacted>"
        elif isinstance(v, dict):
            redacted[k] = _redact(v)
        else:
            redacted[k] = v
    return redacted


def _describe_impact(method: str, args: dict[str, Any]) -> str:
    """Best-effort impact description for known write methods."""
    if method == "create":
        vals_list = args.get("vals_list") or args.get("vals") or []
        if isinstance(vals_list, dict):
            return "Creates 1 record"
        return f"Creates {len(vals_list)} record(s)"
    if method == "write":
        ids = args.get("ids") or []
        vals = args.get("vals") or {}
        field_list = ", ".join(vals.keys()) if isinstance(vals, dict) else "<unknown>"
        return f"Updates field(s) [{field_list}] on {len(ids)} record(s)"
    if method == "unlink":
        ids = args.get("ids") or []
        return f"DELETES {len(ids)} record(s) — IRREVERSIBLE"
    if method == "copy":
        return "Duplicates 1 record (returns new id)"
    if method.startswith("action_") or method.startswith("button_"):
        ids = args.get("ids") or []
        return f"Calls {method} on {len(ids)} record(s) — state-changing, treat as write"
    return f"Calls {method} — non-read method, treated as write"


# -- Call log (append-only JSONL for per-request metrics) ---------------------
#
# Each Odoo API call appends one line to ~/.config/odoo-sidekick/call_log.jsonl.
# Format: {"ts": ISO-8601, "profile": str, "model": str, "method": str,
#          "ms": int, "bytes": int, "status": "ok"|"ok_empty"|"http_<code>"|"network_error"}
#
# Failure to write is silently ignored — metrics are nice-to-have, not load-bearing.
# The log is read by scripts/show_metrics.py.

_CALL_LOG_PATH = Path.home() / ".config" / "odoo-sidekick" / "call_log.jsonl"
_CALL_LOG_MAX_LINES = 5000  # rotate when exceeded


def _log_call(profile: str, model: str, method: str, *,
              ms: int, bytes_received: int, status: str) -> None:
    """Append one metric record to the call log. Silent on any failure."""
    try:
        from datetime import datetime, timezone
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "profile": profile,
            "model": model,
            "method": method,
            "ms": ms,
            "bytes": bytes_received,
            "status": status,
        }
        _CALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Rotate if needed (cheap line count, only when file gets large)
        if _CALL_LOG_PATH.exists() and _CALL_LOG_PATH.stat().st_size > 2_000_000:
            try:
                lines = _CALL_LOG_PATH.read_text().splitlines()
                if len(lines) > _CALL_LOG_MAX_LINES:
                    keep = lines[-(_CALL_LOG_MAX_LINES // 2):]
                    _CALL_LOG_PATH.write_text("\n".join(keep) + "\n")
            except OSError:
                pass
        with _CALL_LOG_PATH.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 — never let logging break a real call
        pass


# -- The client ----------------------------------------------------------------

@dataclass
class OdooClient:
    profile: Profile
    timeout: float = 60.0

    @classmethod
    def from_profile(cls, name: str | None = None, **kwargs: Any) -> "OdooClient":
        return cls(profile=Profile.load(name), **kwargs)

    @property
    def mode(self) -> str:
        return self.profile.mode

    @property
    def is_read_only(self) -> bool:
        return self.profile.mode == MODE_READ_ONLY

    # ---- bound read shortcuts ------------------------------------------------

    def search(self, model: str, domain: list, **kw: Any) -> list[int]:
        return self.call(model, "search", {"domain": domain, **kw})

    def search_count(self, model: str, domain: list, **kw: Any) -> int:
        return self.call(model, "search_count", {"domain": domain, **kw})

    def search_read(
        self,
        model: str,
        domain: list,
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str | None = None,
        **kw: Any,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"domain": domain, "offset": offset}
        if fields is not None:
            args["fields"] = fields
        if limit is not None:
            args["limit"] = limit
        if order is not None:
            args["order"] = order
        args.update(kw)
        return self.call(model, "search_read", args)

    def read(self, model: str, ids: list[int], fields: list[str] | None = None, **kw: Any) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"ids": ids}
        if fields is not None:
            args["fields"] = fields
        args.update(kw)
        return self.call(model, "read", args)

    def read_group(
        self,
        model: str,
        domain: list,
        fields: list[str],
        groupby: list[str] | str,
        limit: int | None = None,
        offset: int = 0,
        orderby: str | None = None,
        lazy: bool = True,
        **kw: Any,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {
            "domain": domain,
            "fields": fields,
            "groupby": groupby if isinstance(groupby, list) else [groupby],
            "offset": offset,
            "lazy": lazy,
        }
        if limit is not None:
            args["limit"] = limit
        if orderby is not None:
            args["orderby"] = orderby
        args.update(kw)
        return self.call(model, "read_group", args)

    def fields_get(
        self,
        model: str,
        fields: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        args: dict[str, Any] = {}
        if fields is not None:
            args["allfields"] = fields
        if attributes is not None:
            args["attributes"] = attributes
        return self.call(model, "fields_get", args)

    # ---- bound write shortcuts ----------------------------------------------
    #
    # All require confirm=True. All raise WriteNotAllowed if mode is read-only.
    # All raise WriteNotConfirmed (with preview) if confirm=False.

    def create(
        self,
        model: str,
        vals_list: list[dict[str, Any]] | dict[str, Any],
        *,
        confirm: bool = False,
        **kw: Any,
    ) -> Any:
        # Normalize: Odoo accepts either a list of dicts (modern) or one dict.
        args = {"vals_list": vals_list if isinstance(vals_list, list) else [vals_list]}
        args.update(kw)
        return self.call(model, "create", args, confirm=confirm)

    def write(
        self,
        model: str,
        ids: list[int],
        vals: dict[str, Any],
        *,
        confirm: bool = False,
        **kw: Any,
    ) -> Any:
        args: dict[str, Any] = {"ids": ids, "vals": vals}
        args.update(kw)
        return self.call(model, "write", args, confirm=confirm)

    def unlink(
        self,
        model: str,
        ids: list[int],
        *,
        confirm: bool = False,
        **kw: Any,
    ) -> Any:
        args: dict[str, Any] = {"ids": ids}
        args.update(kw)
        return self.call(model, "unlink", args, confirm=confirm)

    def copy(
        self,
        model: str,
        record_id: int,
        default: dict[str, Any] | None = None,
        *,
        confirm: bool = False,
        **kw: Any,
    ) -> Any:
        args: dict[str, Any] = {"ids": [record_id]}
        if default is not None:
            args["default"] = default
        args.update(kw)
        return self.call(model, "copy", args, confirm=confirm)

    def execute(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        kwargs: dict[str, Any] | None = None,
        *,
        confirm: bool = False,
    ) -> Any:
        """Call an arbitrary model method (e.g. action_confirm, button_post).
        Treated as a write — requires confirm=True in read-write mode.
        """
        args: dict[str, Any] = {}
        if ids is not None:
            args["ids"] = ids
        if kwargs:
            args.update(kwargs)
        return self.call(model, method, args, confirm=confirm)

    # ---- the one place that actually does I/O --------------------------------

    def call(
        self,
        model: str,
        method: str,
        args: dict[str, Any] | None = None,
        *,
        confirm: bool = False,
    ) -> Any:
        args = dict(args or {})
        is_read = method in READ_METHODS

        # Mode gate ------------------------------------------------------------
        if not is_read and self.is_read_only:
            raise WriteNotAllowed(
                f"Refusing to call {model}.{method}: profile {self.profile.name!r} is in "
                f"{MODE_READ_ONLY} mode. To enable writes, set mode: read-write on the "
                "profile (and bind it to an Odoo user with appropriate write access)."
            )

        # Confirmation gate (dry-run) ------------------------------------------
        if not is_read and not confirm:
            preview = {
                "profile": self.profile.name,
                "url": f"{self.profile.url}/json/2/{model}/{method}",
                "model": model,
                "method": method,
                "impact": _describe_impact(method, args),
                "args": _redact(args),
            }
            raise WriteNotConfirmed(
                f"DRY RUN: write to {model}.{method} not executed. "
                f"{preview['impact']}. Pass confirm=True (or --confirm) to proceed.",
                preview=preview,
            )

        # Merge default context -------------------------------------------------
        ctx = dict(self.profile.default_context)
        ctx.update(args.get("context") or {})
        if ctx:
            args["context"] = ctx

        # Send -----------------------------------------------------------------
        url = f"{self.profile.url}/json/2/{model}/{method}"
        body = json.dumps(args).encode("utf-8")
        headers = {
            "Authorization": f"bearer {self.profile.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "odoo-sidekick/1.7 (claude-skill)",
        }
        if self.profile.database:
            headers["X-Odoo-Database"] = self.profile.database
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        import time as _time  # local import to keep top-level imports tidy
        start = _time.monotonic()
        bytes_received = 0
        status = "ok"
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                bytes_received = len(raw)
                if not raw:
                    _log_call(self.profile.name, model, method,
                              ms=int((_time.monotonic() - start) * 1000),
                              bytes_received=0, status="ok_empty")
                    return None
                result = json.loads(raw)
                _log_call(self.profile.name, model, method,
                          ms=int((_time.monotonic() - start) * 1000),
                          bytes_received=bytes_received, status="ok")
                return result
        except urllib.error.HTTPError as e:
            err_body: dict[str, Any] | str
            try:
                err_body = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                err_body = e.reason or "<no body>"
            _log_call(self.profile.name, model, method,
                      ms=int((_time.monotonic() - start) * 1000),
                      bytes_received=bytes_received, status=f"http_{e.code}")
            raise OdooAPIError(e.code, err_body, url) from e
        except urllib.error.URLError as e:
            _log_call(self.profile.name, model, method,
                      ms=int((_time.monotonic() - start) * 1000),
                      bytes_received=0, status="network_error")
            raise OdooClientError(f"Network error calling {url}: {e.reason}") from e

    # ---- convenience: pagination --------------------------------------------

    def search_read_all(
        self,
        model: str,
        domain: list,
        fields: list[str] | None = None,
        page_size: int = 1000,
        order: str = "id asc",
        **kw: Any,
    ) -> Iterable[dict[str, Any]]:
        """Yield all matching records in pages. Always read-only."""
        offset = 0
        while True:
            batch = self.search_read(
                model, domain, fields=fields, limit=page_size, offset=offset, order=order, **kw
            )
            if not batch:
                return
            for row in batch:
                yield row
            if len(batch) < page_size:
                return
            offset += page_size


# -- CLI -----------------------------------------------------------------------

def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="odoo_client",
        description="Odoo V19 JSON-2 API client with read-only / read-write modes.",
    )
    p.add_argument("profile", help="Profile name from profiles.yaml (or 'default')")
    p.add_argument("model", help="Odoo model, e.g. sale.order")
    p.add_argument("method", help="Method name (any non-read method requires --confirm)")
    p.add_argument("--json", dest="json_args", default="{}",
                   help="JSON object of method arguments. Default: '{}'")
    p.add_argument("--confirm", action="store_true",
                   help="Required for any non-read method. Without it, you get a dry-run preview.")
    p.add_argument("--pretty", action="store_true", help="Pretty-print the JSON result")
    args = p.parse_args(argv)
    try:
        method_args = json.loads(args.json_args)
    except json.JSONDecodeError as e:
        print(f"--json must be a valid JSON object: {e}", file=sys.stderr)
        return 2
    if not isinstance(method_args, dict):
        print("--json must be an object (got a list/scalar)", file=sys.stderr)
        return 2

    try:
        client = OdooClient.from_profile(args.profile)
        result = client.call(args.model, args.method, method_args, confirm=args.confirm)
    except WriteNotAllowed as e:
        print(f"REFUSED (read-only profile): {e}", file=sys.stderr)
        return 3
    except WriteNotConfirmed as e:
        # Dry-run: print the preview so the human (or Claude) can review.
        print("DRY RUN — nothing was sent. Re-run with --confirm to execute.", file=sys.stderr)
        json.dump(e.preview, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 5
    except OdooAPIError as e:
        print(f"API ERROR: {e}", file=sys.stderr)
        return 4
    except OdooClientError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, indent=2 if args.pretty else None, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
