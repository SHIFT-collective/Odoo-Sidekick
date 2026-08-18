"""
Odoo V19 JSON-2 API client with two modes:

  - read-only (default): only methods in READ_METHODS may be called.
    Any write attempt raises WriteNotAllowed before the HTTP call is made.

  - read-write: read methods work normally; write methods (create/write/
    unlink/copy and any non-read method like action_*/button_*) require
    confirm=True per call. Without confirm=True, WriteNotConfirmed is raised
    with a description of what would have been sent — a built-in dry-run.

IMPORTANT — what the mode gate is and is not:
    `mode: read-only` is enforced by THIS CLIENT, before dispatch. It is a
    process-safety gate, not a security boundary. The profile's API key is
    whatever Odoo says it is — any code path that bypasses this client (curl,
    another wrapper, a buggy script) can do whatever the key's Odoo user can
    do. For a real boundary, bind read-only profiles to a restricted Odoo
    user and prove it with `python3 -m scripts.verify_profile <profile>`.

Optional per-profile hardening (see assets/profiles.example.yaml):
    write_policy:       narrow a read-write profile to specific model/method
                        combinations (allow list) and hard-deny models.
    require_auth_ref:   writes must carry an authorization reference
                        (auth_ref=..., --auth-ref, or ODOO_SIDEKICK_AUTH_REF)
                        which is recorded in the call log.
    confirm_cmd:        external command run before every write; receives the
                        write preview as JSON on stdin and must exit 0 for the
                        write to proceed. This is the headless replacement for
                        chat-level confirmation.

Environment variables:
    ODOO_PROFILES_PATH       — override the profiles file location (wins over
                               the state-dir default below).
    ODOO_SIDEKICK_STATE_DIR  — override the state dir (call log, caches, and
                               the DEFAULT profiles.yaml location).
                               Default: ~/.config/odoo-sidekick
    ODOO_SIDEKICK_CALLER     — identity string recorded on every call-log
                               record (agent id / run id). For multi-agent
                               hosts sharing one state dir.
    ODOO_SIDEKICK_AUTH_REF   — default auth_ref attached to writes.
    ODOO_SIDEKICK_MAX_RETRIES — max retry count for transient read failures
                               (default 2, i.e. up to 3 attempts).

Retry behaviour: transient failures (HTTP 429/502/503/504, network errors,
and 500s whose exception is not a deterministic builtins./odoo.exceptions./
werkzeug. error) are retried with exponential backoff + jitter — but only
for read methods. Writes are never auto-retried: a 502/504 can mask a
committed transaction, and blind re-issue risks double-writes.

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
    python3 -m scripts.odoo_client <profile> sale.order search_read \\
        --json '{"domain": [["state","=","sale"]], "fields": ["name"], "limit": 5}'

    # write — dry-run with before→after diff (reads current values, sends no write)
    python3 -m scripts.odoo_client <profile> res.partner write \\
        --json '{"ids": [7], "vals": {"phone": "+1 210 555 0100"}}' --dry-run

    # write — real (with --confirm; --auth-ref ties it to an approval)
    python3 -m scripts.odoo_client <profile> res.partner create \\
        --json '{"vals_list": [{"name": "X"}]}' --confirm --auth-ref TICKET-123
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import http.client
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
    "formatted_read_group",   # Odoo 19's replacement for read_group
    "fields_get",
    "name_search",
    "name_get",
    "web_search_read",
    "web_read",
    "web_read_group",
    "get_views",
    "default_get",
    "has_access",             # permission probe — does not mutate
    "check_access_rights",    # legacy permission probe — does not mutate
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


class WritePolicyViolation(WriteNotAllowed):
    """Refused: the profile's write_policy does not allow this model/method."""


class AuthRefRequired(OdooClientError):
    """Refused: the profile requires an auth_ref on writes and none was given."""


class WriteNotConfirmed(OdooClientError):
    """Refused: write method called without confirm=True.

    Carries `.preview` with what would have been sent so callers (and the CLI's
    dry-run mode) can show the user.
    """

    def __init__(self, message: str, preview: dict[str, Any]):
        super().__init__(message)
        self.preview = preview


class ConfirmCmdRejected(WriteNotConfirmed):
    """Refused: the profile's confirm_cmd hook rejected (or timed out on) the write."""


class ProfileError(OdooClientError):
    """Profile config missing or malformed."""


class OdooAPIError(OdooClientError):
    """The Odoo server returned a 4xx/5xx with an error body.

    Attributes:
        status         — HTTP status code (int)
        error_name     — server exception class, e.g. 'odoo.exceptions.ValidationError'
        error_message  — server exception message (the actionable part)
        debug          — server-side traceback string, if provided
        hint           — client-generated remediation hint, if we recognize the error
        url, model, method — what was being called
    """

    def __init__(self, status: int, payload: dict[str, Any] | str, url: str,
                 model: str = "", method: str = ""):
        self.status = status
        self.payload = payload
        self.url = url
        self.model = model
        self.method = method
        if isinstance(payload, dict):
            self.error_name = payload.get("name", "UnknownError")
            self.error_message = str(
                payload.get("message")
                or (payload.get("arguments") or ["<no message>"])[0]
            )
            self.debug = payload.get("debug") or ""
        else:
            self.error_name = "UnknownError"
            self.error_message = str(payload)[:500]
            self.debug = ""
        self.hint = _hint_for(status, self.error_name, self.error_message, model, method)
        msg = f"HTTP {status} from {url}: {self.error_name}: {self.error_message}"
        if self.hint:
            msg += f"\nHINT: {self.hint}"
        super().__init__(msg)

    def as_dict(self) -> dict[str, Any]:
        """Machine-readable error shape (used by --json-errors)."""
        return {
            "ok": False,
            "status": self.status,
            "error_name": self.error_name,
            "error_message": self.error_message,
            "hint": self.hint,
            "model": self.model,
            "method": self.method,
            "url": self.url,
            "traceback_tail": _traceback_tail(self.debug),
        }


def _traceback_tail(debug: str, frames: int = 3) -> list[str]:
    """Last few meaningful lines of the server traceback — where it actually broke."""
    if not debug:
        return []
    lines = [ln.rstrip() for ln in debug.splitlines() if ln.strip()]
    return lines[-frames:]


_INVALID_FIELD_RE = re.compile(r"[Ii]nvalid field '?([\w.]+)'?")
_UNEXPECTED_KWARG_RE = re.compile(r"unexpected keyword argument '(\w+)'")
_MISSING_ARG_RE = re.compile(r"missing a required argument: '(\w+)'")


def _hint_for(status: int, name: str, message: str, model: str, method: str) -> str:
    """Turn a recognized server error into an actionable next step.

    This exists because agents (and humans) otherwise re-issue the failing
    call verbatim. Every hint names the concrete fix or the tool that finds it.
    """
    msg = message or ""

    if status == 401:
        return ("Authentication failed. Check the profile's API key — if it uses "
                "${ENV_VAR} substitution, confirm the variable is set in THIS shell "
                "(echo $VAR), then retry.")

    if status == 404 and "does not exist" in msg and "model" in msg.lower():
        stem = (model.split(".")[0] or model) if model else ""
        return (f"The model name is wrong. Fuzzy-search installed models with: "
                f"python3 -m scripts.introspect <profile> --find {stem or '<word>'}")

    if "Private methods" in msg:
        return ("Methods starting with '_' are not callable over JSON-2. "
                "Look for a public wrapper method (fields_get / introspect can help).")

    m = _INVALID_FIELD_RE.search(msg)
    if m:
        return (f"Unknown field {m.group(1)!r} on {model or 'this model'}. List real fields with: "
                f"python3 -m scripts.introspect <profile> --model {model or '<model>'} — "
                "note Odoo 19 renamed several common fields "
                "(see references/odoo19_field_changes.md).")

    m = _UNEXPECTED_KWARG_RE.search(msg)
    if m:
        arg = m.group(1)
        if arg == "orderby":
            return ("search/search_read take `order`; only read_group takes `orderby`. "
                    "Rename the key and retry.")
        return (f"{model}.{method} does not accept an argument named {arg!r}. JSON-2 passes "
                "every body key as a named argument — check the method signature "
                "(introspect, or GET <url>/doc).")

    m = _MISSING_ARG_RE.search(msg)
    if m:
        arg = m.group(1)
        if arg == "vals_list":
            return ("create takes `vals_list` — a LIST of dicts — not `vals`. "
                    "Wrap your dict: {\"vals_list\": [{...}]}.")
        return (f"The method requires a named argument {arg!r} in the JSON body.")

    if status == 403 and name.endswith("AccessError"):
        return ("The API key's Odoo user lacks access. If the message names a specific "
                "field, a restricted user lacks field-level access — drop that field from "
                "`fields` and retry. Otherwise fix group membership in Odoo "
                "(Settings → Users).")

    if status == 422 or name.endswith("ValidationError"):
        return ("Odoo rejected the values against a business rule — the message above "
                "usually names the field or constraint. Fix the payload; do NOT retry "
                "unchanged.")

    if status in (502, 503, 504):
        return ("Gateway/proxy error — usually transient. Reads are retried "
                "automatically; if this persists, the Odoo host may be down.")

    if status == 429:
        return "Rate limited. Slow down, batch reads (id lists / read_group), or cache."

    return ""


# -- Profile loading -----------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _substitute_env(value: Any, source: Path | None = None) -> Any:
    """Recursively replace ${ENV_VAR} placeholders in strings."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            var = m.group(1)
            v = os.environ.get(var)
            if v is None:
                where = f" (referenced from profiles file {source})" if source else ""
                raise ProfileError(
                    f"Profile references env var ${{{var}}} which is not set{where}. "
                    f"Set it in this shell, or point ODOO_PROFILES_PATH at a profiles "
                    f"file whose keys are available here."
                )
            return v
        return _ENV_VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v, source) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v, source) for v in value]
    return value


def _skill_version() -> str:
    """The skill version from the VERSION file (single source of truth)."""
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


_SKILL_VERSION = _skill_version()


def state_dir() -> Path:
    """Directory for skill state: call log, caches. Overridable for multi-agent
    hosts where each agent needs (or a fleet shares) its own state."""
    override = os.environ.get("ODOO_SIDEKICK_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "odoo-sidekick"


def _default_profiles_path() -> Path:
    override = os.environ.get("ODOO_PROFILES_PATH")
    if override:
        return Path(override).expanduser()
    return state_dir() / "profiles.yaml"


def _load_config(path: Path) -> dict[str, Any]:
    """Load the profiles file.

    Canonical format is JSON (which is also valid YAML, so any YAML tooling can
    still read it, and it loads without PyYAML). YAML-authored files are
    accepted when PyYAML is installed. Do not edit this file with string
    surgery (sed etc.) — use `python3 -m scripts.profiles set ...`, which
    validates and writes atomically.
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
            "Install with `pip install pyyaml` (`profiles migrate` can then convert "
            "the file to JSON so PyYAML is no longer needed), or re-create the "
            "config with `python3 -m scripts.profiles add`, which writes JSON."
        ) from e
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ProfileError(f"Profile file at {path} did not parse to a mapping.")
    return data


def _validate_write_policy(policy: Any, profile_name: str) -> dict[str, Any]:
    """Validate the write_policy block shape. Returns the normalized policy."""
    if not isinstance(policy, dict):
        raise ProfileError(f"Profile {profile_name!r}: write_policy must be a mapping.")
    allow = policy.get("allow", [])
    deny = policy.get("deny_models", [])
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise ProfileError(f"Profile {profile_name!r}: write_policy.deny_models must be a list of model names.")
    if not isinstance(allow, list):
        raise ProfileError(f"Profile {profile_name!r}: write_policy.allow must be a list.")
    for entry in allow:
        if (not isinstance(entry, dict) or not isinstance(entry.get("model"), str)
                or not isinstance(entry.get("methods"), list)):
            raise ProfileError(
                f"Profile {profile_name!r}: each write_policy.allow entry needs "
                "{model: <name>, methods: [<method>, ...]} (use ['*'] for any method)."
            )
    unknown = set(policy) - {"allow", "deny_models"}
    if unknown:
        raise ProfileError(f"Profile {profile_name!r}: unknown write_policy key(s): {sorted(unknown)}")
    return {"allow": allow, "deny_models": deny}


@dataclass
class Profile:
    name: str
    url: str
    api_key: str
    database: str | None = None
    mode: str = MODE_READ_ONLY
    default_context: dict[str, Any] = field(default_factory=dict)
    write_policy: dict[str, Any] | None = None
    confirm_cmd: str | None = None
    require_auth_ref: bool = False

    @classmethod
    def load(cls, name: str | None = None, profiles_path: Path | None = None) -> "Profile":
        path = profiles_path or _default_profiles_path()
        if not path.exists():
            raise ProfileError(
                f"No profiles file at {path}. Create one based on "
                "assets/profiles.example.yaml in the skill folder, or run "
                "`python3 -m scripts.profiles add <name> --url ... --api-key ...`."
            )
        raw = _load_config(path)
        profiles = raw.get("profiles") or {}
        if not profiles:
            raise ProfileError(f"No profiles defined in {path}")
        # The literal name "default" resolves the config's `default` key
        # (unless a profile is actually named that) — the CLIs advertise it.
        if name is None or (name == "default" and "default" not in profiles):
            name = raw.get("default")
            if not name:
                raise ProfileError(
                    f"No profile name given and no 'default' set in {path}. "
                    f"Available: {sorted(profiles.keys())}"
                )
        if name not in profiles:
            raise ProfileError(
                f"Unknown profile {name!r} in {path}. Available: {sorted(profiles.keys())}"
            )
        p = _substitute_env(profiles[name], source=path)
        mode = p.get("mode", MODE_READ_ONLY)
        if mode not in VALID_MODES:
            raise ProfileError(
                f"Profile {name!r} has invalid mode {mode!r}. "
                f"Must be one of: {VALID_MODES}"
            )
        policy = p.get("write_policy")
        if policy is not None:
            policy = _validate_write_policy(policy, name)
        return cls(
            name=name,
            url=p["url"].rstrip("/"),
            api_key=p["api_key"],
            database=p.get("database"),
            mode=mode,
            default_context=p.get("default_context") or {},
            write_policy=policy,
            confirm_cmd=p.get("confirm_cmd"),
            require_auth_ref=bool(p.get("require_auth_ref", False)),
        )


# -- Preview / dry-run helpers -------------------------------------------------

_SENSITIVE_KEY_PARTS = ("password", "api_key", "token", "secret", "private_key",
                        "smtp_pass", "passwd")


def _is_sensitive_key(key: str) -> bool:
    low = key.lower()
    return any(s in low for s in _SENSITIVE_KEY_PARTS)


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    """Redact common secret-shaped keys before showing a dry-run preview.
    Recurses into dicts AND lists — create's vals_list is a list of dicts."""
    redacted = {}
    for k, v in args.items():
        if _is_sensitive_key(k):
            redacted[k] = "<redacted>"
        else:
            redacted[k] = _redact_value(v)
    return redacted


def _redact_value(v: Any) -> Any:
    if isinstance(v, dict):
        return _redact(v)
    if isinstance(v, list):
        return [_redact_value(item) for item in v]
    return v


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
    if method in KNOWN_WRITE_METHODS:
        ids = args.get("ids") or []
        target = f" on {len(ids)} record(s)" if ids else ""
        return f"Calls {method}{target} — recognized write method"
    return f"Calls {method} — non-read method, treated as write"


# -- Call log (append-only JSONL for per-request metrics & audit) ---------------
#
# Each Odoo API call appends one line to <state_dir>/call_log.jsonl.
# Core fields: {"ts": ISO-8601, "profile": str, "model": str, "method": str,
#               "ms": int, "bytes": int,
#               "status": "ok"|"ok_empty"|"http_<code>"|"network_error"}
# Optional fields (present only when applicable):
#   "caller"   — from ODOO_SIDEKICK_CALLER (agent/run identity on shared hosts)
#   "auth_ref" — authorization reference attached to a write
#   "attempt"  — retry attempt number, when > 1
#
# Failure to write is silently ignored — metrics are nice-to-have, not
# load-bearing. The log is read by scripts/show_metrics.py.

_CALL_LOG_ROTATE_BYTES = 2_000_000
_CALL_LOG_KEEP_ARCHIVES = 5


def _call_log_path() -> Path:
    return state_dir() / "call_log.jsonl"


def _rotate_call_log(path: Path) -> None:
    """Archive (never truncate) the log when it grows past the threshold.

    History is exactly what you want when investigating an incident, so
    rotation renames to a datestamped archive and prunes only the oldest.
    """
    try:
        if not path.exists() or path.stat().st_size <= _CALL_LOG_ROTATE_BYTES:
            return
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = path.with_name(f"call_log-{stamp}.jsonl")
        os.replace(path, archive)  # atomic; loses the race harmlessly if another process rotated first
        archives = sorted(path.parent.glob("call_log-*.jsonl"))
        for old in archives[:-_CALL_LOG_KEEP_ARCHIVES]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _log_call(profile: str, model: str, method: str, *,
              ms: int, bytes_received: int, status: str,
              auth_ref: str | None = None, attempt: int = 1) -> None:
    """Append one metric record to the call log. Silent on any failure."""
    try:
        from datetime import datetime, timezone
        rec: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "profile": profile,
            "model": model,
            "method": method,
            "ms": ms,
            "bytes": bytes_received,
            "status": status,
        }
        caller = os.environ.get("ODOO_SIDEKICK_CALLER")
        if caller:
            rec["caller"] = caller
        if auth_ref:
            rec["auth_ref"] = auth_ref
        if attempt > 1:
            rec["attempt"] = attempt
        path = _call_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_call_log(path)
        with path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 — never let logging break a real call
        pass


# -- Retry classification --------------------------------------------------------

_RETRYABLE_HTTP = {429, 502, 503, 504}
# 500s carrying deterministic application errors (bad field, validation, ...)
# are NOT transient; retrying them verbatim is pure waste.
_DETERMINISTIC_500_PREFIXES = ("builtins.", "odoo.exceptions.", "werkzeug.")


def _is_transient(status: int, error_name: str) -> bool:
    if status in _RETRYABLE_HTTP:
        return True
    if status == 500 and error_name and not error_name.startswith(_DETERMINISTIC_500_PREFIXES):
        return True  # e.g. psycopg2 serialization/operational errors under load
    return False


def _default_max_retries() -> int:
    try:
        return max(0, int(os.environ.get("ODOO_SIDEKICK_MAX_RETRIES", "2")))
    except ValueError:
        return 2


# -- The client ----------------------------------------------------------------

@dataclass
class OdooClient:
    profile: Profile
    timeout: float = 60.0
    max_retries: int = field(default_factory=_default_max_retries)

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

    def has_access(self, model: str, operation: str) -> bool:
        """Does the API key's user have `operation` ('read'/'write'/'create'/
        'unlink') access on `model`? Pure permission probe — never mutates."""
        return bool(self.call(model, "has_access", {"operation": operation}))

    # ---- bound write shortcuts ----------------------------------------------
    #
    # All require confirm=True. All raise WriteNotAllowed if mode is read-only.
    # All raise WriteNotConfirmed (with preview) if confirm=False.
    # auth_ref (or ODOO_SIDEKICK_AUTH_REF) is recorded in the call log.

    def create(
        self,
        model: str,
        vals_list: list[dict[str, Any]] | dict[str, Any],
        *,
        confirm: bool = False,
        auth_ref: str | None = None,
        **kw: Any,
    ) -> Any:
        # Normalize: JSON-2 create requires vals_list as a list of dicts.
        args = {"vals_list": vals_list if isinstance(vals_list, list) else [vals_list]}
        args.update(kw)
        return self.call(model, "create", args, confirm=confirm, auth_ref=auth_ref)

    def write(
        self,
        model: str,
        ids: list[int],
        vals: dict[str, Any],
        *,
        confirm: bool = False,
        auth_ref: str | None = None,
        **kw: Any,
    ) -> Any:
        args: dict[str, Any] = {"ids": ids, "vals": vals}
        args.update(kw)
        return self.call(model, "write", args, confirm=confirm, auth_ref=auth_ref)

    def unlink(
        self,
        model: str,
        ids: list[int],
        *,
        confirm: bool = False,
        auth_ref: str | None = None,
        **kw: Any,
    ) -> Any:
        args: dict[str, Any] = {"ids": ids}
        args.update(kw)
        return self.call(model, "unlink", args, confirm=confirm, auth_ref=auth_ref)

    def copy(
        self,
        model: str,
        record_id: int,
        default: dict[str, Any] | None = None,
        *,
        confirm: bool = False,
        auth_ref: str | None = None,
        **kw: Any,
    ) -> Any:
        args: dict[str, Any] = {"ids": [record_id]}
        if default is not None:
            args["default"] = default
        args.update(kw)
        return self.call(model, "copy", args, confirm=confirm, auth_ref=auth_ref)

    def execute(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        kwargs: dict[str, Any] | None = None,
        *,
        confirm: bool = False,
        auth_ref: str | None = None,
    ) -> Any:
        """Call an arbitrary model method (e.g. action_confirm, button_post).
        Treated as a write — requires confirm=True in read-write mode.
        """
        args: dict[str, Any] = {}
        if ids is not None:
            args["ids"] = ids
        if kwargs:
            args.update(kwargs)
        return self.call(model, method, args, confirm=confirm, auth_ref=auth_ref)

    # ---- write policy ----------------------------------------------------------

    def check_write_policy(self, model: str, method: str) -> str | None:
        """Return a refusal reason if the profile's write_policy blocks this
        model/method, else None. Reads are never policy-gated."""
        policy = self.profile.write_policy
        if not policy:
            return None
        if model in policy.get("deny_models", []):
            return (f"model {model!r} is in write_policy.deny_models for "
                    f"profile {self.profile.name!r}")
        allow = policy.get("allow", [])
        if allow:
            for entry in allow:
                if entry["model"] == model and (
                    "*" in entry["methods"] or method in entry["methods"]
                ):
                    return None
            return (f"{model}.{method} is not in write_policy.allow for "
                    f"profile {self.profile.name!r}")
        return None

    # ---- dry-run with before→after diff ----------------------------------------

    def dry_run(self, model: str, method: str, args: dict[str, Any] | None = None,
                max_records: int = 50, auth_ref: str | None = None) -> dict[str, Any]:
        """Preview a write without sending it: fetches current values for the
        target records and reports what would change. Works on any profile
        (only performs reads) and also reports whether the write WOULD be
        allowed by the profile's mode and write_policy. A confirm_cmd hook is
        deliberately NOT run here (it may page an approver); the preview
        carries a note when one is configured. The output is designed to be
        the artifact a human (or approval process) signs off on.
        """
        args = _normalize_args(method, dict(args or {}))
        preview: dict[str, Any] = {
            "dry_run": True,
            "profile": self.profile.name,
            "model": model,
            "method": method,
            "impact": _describe_impact(method, args),
            "args": _redact(args),
        }

        refusal: str | None = None
        if method in READ_METHODS:
            refusal = None
            preview["impact"] = f"{method} is a read method — nothing to preview"
        elif self.is_read_only:
            refusal = f"profile {self.profile.name!r} is read-only"
        else:
            refusal = self.check_write_policy(model, method)
            if refusal is None and self.profile.require_auth_ref and not (
                auth_ref or os.environ.get("ODOO_SIDEKICK_AUTH_REF")
            ):
                preview["note"] = ("profile requires auth_ref on writes — pass "
                                   "--auth-ref / auth_ref=... when executing")
            if refusal is None and self.profile.confirm_cmd:
                preview["confirm_cmd_note"] = (
                    "profile has a confirm_cmd hook — final approval happens at "
                    "execution time and may still refuse this write"
                )
        preview["would_be_allowed"] = refusal is None
        if refusal:
            preview["refusal_reason"] = refusal

        ids = args.get("ids") or []
        try:
            if method == "write" and ids:
                vals = args.get("vals") or {}
                fetch = [f for f in vals.keys()]
                current = self.read(model, ids[:max_records],
                                    fields=fetch + ["display_name"] if fetch else ["display_name"])
                changes = []
                for rec in current:
                    diff = {}
                    for f, new in vals.items():
                        old = rec.get(f)
                        # many2one reads come back as [id, name]; compare on id
                        old_cmp = old[0] if isinstance(old, list) and old else old
                        changed = old_cmp != new and old != new
                        if _is_sensitive_key(f):
                            # never echo current or new secret values
                            old = new = "<redacted>"
                        diff[f] = {"from": old, "to": new, "changed": changed}
                    changes.append({"id": rec["id"],
                                    "display_name": rec.get("display_name"),
                                    "fields": diff})
                preview["before_after"] = changes
                if len(ids) > max_records:
                    preview["truncated"] = f"showing {max_records} of {len(ids)} records"
            elif method == "unlink" and ids:
                recs = self.read(model, ids[:max_records], fields=["display_name"])
                preview["records_to_delete"] = recs
                if len(ids) > max_records:
                    preview["truncated"] = f"showing {max_records} of {len(ids)} records"
            elif method == "create":
                preview["records_to_create"] = _redact_value(args.get("vals_list") or [])
            elif ids:
                recs = self.read(model, ids[:max_records], fields=["display_name"])
                preview["target_records"] = recs
        except OdooAPIError as e:
            preview["enrichment_error"] = (
                f"could not fetch current values: {e.error_name}: {e.error_message}"
            )
        return preview

    # ---- the one place that actually does I/O --------------------------------

    def call(
        self,
        model: str,
        method: str,
        args: dict[str, Any] | None = None,
        *,
        confirm: bool = False,
        auth_ref: str | None = None,
    ) -> Any:
        args = _normalize_args(method, dict(args or {}))
        is_read = method in READ_METHODS
        auth_ref = auth_ref or os.environ.get("ODOO_SIDEKICK_AUTH_REF") or None

        # Mode gate ------------------------------------------------------------
        if not is_read and self.is_read_only:
            raise WriteNotAllowed(
                f"Refusing to call {model}.{method}: profile {self.profile.name!r} is in "
                f"{MODE_READ_ONLY} mode. To enable writes, set mode: read-write on the "
                "profile (and bind it to an Odoo user with appropriate write access). "
                "Note: this gate is client-side only — see scripts/verify_profile.py "
                "to check what the credential itself can do."
            )

        # Write-policy gate ------------------------------------------------------
        if not is_read:
            refusal = self.check_write_policy(model, method)
            if refusal:
                raise WritePolicyViolation(f"Refusing to call {model}.{method}: {refusal}.")

        # Merge default context BEFORE the confirmation gates so the preview
        # and the confirm_cmd hook see the exact body that will be posted —
        # context (allowed_company_ids, tracking flags, ...) can change a
        # write's scope and side effects.
        ctx = dict(self.profile.default_context)
        ctx.update(args.get("context") or {})
        if ctx:
            args["context"] = ctx

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

        # Auth-ref gate (after the confirm gate so previews stay available) ------
        if not is_read and self.profile.require_auth_ref and not auth_ref:
            raise AuthRefRequired(
                f"Profile {self.profile.name!r} requires an authorization reference on "
                f"writes. Pass auth_ref=... (CLI: --auth-ref, env: ODOO_SIDEKICK_AUTH_REF) "
                "naming the ticket/approval this write executes."
            )

        # Headless confirmation hook ---------------------------------------------
        if not is_read and self.profile.confirm_cmd:
            self._run_confirm_cmd(model, method, args, auth_ref)

        # Send (with bounded retry for transient read failures) -------------------
        url = f"{self.profile.url}/json/2/{model}/{method}"
        body = json.dumps(args).encode("utf-8")
        headers = {
            "Authorization": f"bearer {self.profile.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"odoo-sidekick/{_SKILL_VERSION} (claude-skill)",
        }
        database = self._resolved_database()
        if database:
            headers["X-Odoo-Database"] = database

        max_attempts = 1 + (self.max_retries if is_read else 0)
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            start = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    ms = int((time.monotonic() - start) * 1000)
                    if not raw:
                        _log_call(self.profile.name, model, method, ms=ms,
                                  bytes_received=0, status="ok_empty",
                                  auth_ref=auth_ref, attempt=attempt)
                        return None
                    result = json.loads(raw)
                    _log_call(self.profile.name, model, method, ms=ms,
                              bytes_received=len(raw), status="ok",
                              auth_ref=auth_ref, attempt=attempt)
                    return result
            except urllib.error.HTTPError as e:
                err_body: dict[str, Any] | str
                try:
                    err_body = json.loads(e.read().decode("utf-8"))
                except Exception:  # noqa: BLE001
                    err_body = e.reason or "<no body>"
                _log_call(self.profile.name, model, method,
                          ms=int((time.monotonic() - start) * 1000),
                          bytes_received=0, status=f"http_{e.code}",
                          auth_ref=auth_ref, attempt=attempt)
                api_err = OdooAPIError(e.code, err_body, url, model=model, method=method)
                if attempt < max_attempts and _is_transient(e.code, api_err.error_name):
                    self._backoff(attempt)
                    continue
                raise api_err from e
            except (OSError, http.client.HTTPException, json.JSONDecodeError) as e:
                # urllib wraps only connect-phase failures in URLError; a timeout
                # or connection reset while READING the response arrives raw
                # (TimeoutError / ConnectionResetError), and a 200 with a
                # non-JSON body (proxy/captive-portal page) raises
                # JSONDecodeError. All are network-class: log, retry, wrap.
                _log_call(self.profile.name, model, method,
                          ms=int((time.monotonic() - start) * 1000),
                          bytes_received=0, status="network_error",
                          auth_ref=auth_ref, attempt=attempt)
                if attempt < max_attempts:
                    self._backoff(attempt)
                    continue
                reason = getattr(e, "reason", None) or f"{type(e).__name__}: {e}"
                raise OdooClientError(f"Network error calling {url}: {reason}") from e

    @staticmethod
    def _backoff(attempt: int) -> None:
        """Exponential backoff with jitter: ~0.5s, ~1s, ~2s ..."""
        delay = 0.5 * (2 ** (attempt - 1))
        time.sleep(delay + random.uniform(0, delay / 2))

    def _run_confirm_cmd(self, model: str, method: str,
                         args: dict[str, Any], auth_ref: str | None) -> None:
        """Run the profile's confirm_cmd with the write preview on stdin.
        Exit 0 → proceed; anything else → the write is refused."""
        import subprocess
        preview = {
            "profile": self.profile.name,
            "model": model,
            "method": method,
            "impact": _describe_impact(method, args),
            "args": _redact(args),
            "auth_ref": auth_ref,
        }
        try:
            proc = subprocess.run(
                self.profile.confirm_cmd, shell=True,
                input=json.dumps(preview).encode("utf-8"),
                capture_output=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise ConfirmCmdRejected(
                f"confirm_cmd for profile {self.profile.name!r} timed out — write refused.",
                preview=preview,
            ) from None
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[:500]
            raise ConfirmCmdRejected(
                f"confirm_cmd for profile {self.profile.name!r} rejected the write "
                f"(exit {proc.returncode}){': ' + detail if detail else ''}.",
                preview=preview,
            )

    def _resolved_database(self) -> str | None:
        """Return the database header value; 'auto' resolves via
        /web/database/list at first use (SaaS rebuilds regenerate the DB-name
        hash suffix, so hardcoding it silently breaks after a rebuild)."""
        db = self.profile.database
        if db != "auto":
            return db
        payload = json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}}).encode()
        req = urllib.request.Request(
            f"{self.profile.url}/web/database/list", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                names = json.loads(resp.read()).get("result") or []
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            raise ProfileError(
                f"database: auto — could not list databases at {self.profile.url}: {e}"
            ) from e
        if len(names) != 1:
            raise ProfileError(
                f"database: auto needs exactly one database at {self.profile.url}, "
                f"found {names!r}. Set `database:` explicitly."
            )
        self.profile.database = names[0]  # cache for the rest of this process
        return names[0]

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


def _normalize_args(method: str, args: dict[str, Any]) -> dict[str, Any]:
    """Fix the most common argument-shape traps before they hit the server.

    - create takes `vals_list` (a list of dicts). A bare dict, or the key
      `vals`, is a known trap that yields an opaque 422.
    - search/search_read take `order`; `orderby` belongs to read_group only.
    - `ids` must be a list; a bare int is an equally common slip.
    """
    if method == "create":
        if "vals_list" not in args and "vals" in args:
            args["vals_list"] = args.pop("vals")
        vl = args.get("vals_list")
        if isinstance(vl, dict):
            args["vals_list"] = [vl]
    elif method in ("search", "search_read", "web_search_read") and "orderby" in args and "order" not in args:
        args["order"] = args.pop("orderby")
    if isinstance(args.get("ids"), int):
        args["ids"] = [args["ids"]]
    return args


# -- CLI -----------------------------------------------------------------------

# Fields whose values are base64 blobs — pulling them inline floods the
# context window. The CLI steers these to scripts/get_attachment.py.
_BINARY_FIELDS = {"datas", "raw", "db_datas"}


def _inline_binary_risk(model: str, method: str, args: dict[str, Any]) -> str | None:
    if model != "ir.attachment" or method not in ("read", "search_read"):
        return None
    fields = args.get("fields")
    if not fields:  # None AND [] both mean "all fields" to Odoo
        return ("reading ir.attachment without `fields` returns the full base64 file "
                "body inline")
    hit = [f for f in fields if f in _BINARY_FIELDS]
    if hit:
        return f"field(s) {hit} on ir.attachment return the full base64 file body inline"
    return None


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
    p.add_argument("--dry-run", action="store_true",
                   help="For writes: fetch current values and print a before→after "
                        "diff without sending anything. Exit 0.")
    p.add_argument("--auth-ref", default=None,
                   help="Authorization reference (ticket/approval id) recorded in the "
                        "call log with this write.")
    p.add_argument("--retries", type=int, default=None,
                   help="Max retries for transient read failures (default: env "
                        "ODOO_SIDEKICK_MAX_RETRIES or 2). Writes never auto-retry.")
    p.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds (default 60)")
    p.add_argument("--json-errors", action="store_true",
                   help="On API errors, print a machine-readable JSON error object to "
                        "stdout (for headless callers).")
    p.add_argument("--allow-inline-binary", action="store_true",
                   help="Allow reading base64 file bodies (ir.attachment datas/raw) inline. "
                        "Prefer scripts/get_attachment.py, which writes to a file.")
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

    risk = _inline_binary_risk(args.model, args.method, method_args)
    if risk and not args.allow_inline_binary:
        print(f"REFUSED: {risk}.\n"
              f"Use `python3 -m scripts.get_attachment {args.profile} --id <id> --out <file>` "
              "to download to disk, or pass --allow-inline-binary if you really "
              "want the blob on stdout.", file=sys.stderr)
        return 7

    try:
        client_kw: dict[str, Any] = {}
        if args.retries is not None:
            client_kw["max_retries"] = max(0, args.retries)
        if args.timeout is not None:
            client_kw["timeout"] = args.timeout
        client = OdooClient.from_profile(args.profile, **client_kw)
        if args.dry_run:
            result = client.dry_run(args.model, args.method, method_args,
                                    auth_ref=args.auth_ref)
        else:
            result = client.call(args.model, args.method, method_args,
                                 confirm=args.confirm, auth_ref=args.auth_ref)
    except WriteNotAllowed as e:  # includes WritePolicyViolation
        label = "write policy" if isinstance(e, WritePolicyViolation) else "read-only profile"
        print(f"REFUSED ({label}): {e}", file=sys.stderr)
        if args.json_errors:
            json.dump({"ok": False, "refused": label.replace(" ", "_"),
                       "error_name": type(e).__name__, "error_message": str(e)},
                      sys.stdout, indent=2)
            sys.stdout.write("\n")
        return 3
    except AuthRefRequired as e:
        print(f"REFUSED (auth_ref required): {e}", file=sys.stderr)
        if args.json_errors:
            json.dump({"ok": False, "refused": "auth_ref_required",
                       "error_name": type(e).__name__, "error_message": str(e)},
                      sys.stdout, indent=2)
            sys.stdout.write("\n")
        return 6
    except ConfirmCmdRejected as e:
        print(f"REFUSED (confirm_cmd hook): {e}", file=sys.stderr)
        json.dump(e.preview, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 8
    except WriteNotConfirmed as e:
        # Dry-run: print the preview so the human (or Claude) can review.
        print("DRY RUN — nothing was sent. Re-run with --confirm to execute.", file=sys.stderr)
        json.dump(e.preview, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 5
    except OdooAPIError as e:
        print(f"API ERROR: {e}", file=sys.stderr)
        tail = _traceback_tail(e.debug)
        if tail:
            print("Server traceback (tail):", file=sys.stderr)
            for line in tail:
                print(f"  {line}", file=sys.stderr)
        if args.json_errors:
            json.dump(e.as_dict(), sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
        return 4
    except OdooClientError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        if args.json_errors:
            json.dump({"ok": False, "error_name": type(e).__name__,
                       "error_message": str(e)}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        return 1
    json.dump(result, sys.stdout, indent=2 if args.pretty or args.dry_run else None, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
