"""
Prove what a profile's credential can actually do on the server.

Why this exists: `mode: read-only` is enforced by this skill's client only.
The API key itself is as powerful as its Odoo user — any code path around
the client (curl, another wrapper, a buggy script) gets the key's full
rights. This script asks Odoo directly (`has_access`) what the credential
can do, and compares that against what the profile claims.

The one configuration this skill considers SAFE for production analytics:
    mode: read-only  +  a credential that genuinely cannot write.

Run it at setup time and any time an API key changes:
    python3 -m scripts.verify_profile <profile>
    python3 -m scripts.verify_profile <profile> --models sale.order,account.move
    python3 -m scripts.verify_profile <profile> --json

Exit codes:
    0 — profile mode and credential capability are aligned
    1 — connection/config error, could not verify
    2 — MISALIGNED: read-only profile whose credential CAN write
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

try:
    from .odoo_client import OdooClient, OdooAPIError, OdooClientError, MODE_READ_ONLY
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from odoo_client import OdooClient, OdooAPIError, OdooClientError, MODE_READ_ONLY  # type: ignore


# Representative spread: contacts, sales, accounting, inventory, and the
# schema itself. Write access on ANY of these means the key is not read-only.
DEFAULT_PROBE_MODELS = [
    "res.partner",
    "sale.order",
    "account.move",
    "product.product",
    "stock.picking",
    "ir.model",
]

WRITE_OPERATIONS = ("write", "create", "unlink")


def check_server(client: OdooClient) -> dict:
    """Connectivity + server version via /web/version (no auth required)."""
    req = urllib.request.Request(
        f"{client.profile.url}/web/version",
        headers={"User-Agent": "odoo-sidekick-verify/1.0"},
    )
    with urllib.request.urlopen(req, timeout=client.timeout) as resp:
        return json.loads(resp.read())


def probe_model(client: OdooClient, model: str) -> dict:
    """Ask Odoo what this credential can do on `model`. Never mutates."""
    result: dict = {"model": model}
    for op in ("read",) + WRITE_OPERATIONS:
        try:
            result[op] = bool(client.call(model, "has_access", {"operation": op}))
        except OdooAPIError as e:
            if e.status == 401:
                raise  # bad credential — the whole verification is moot
            # 404 → model not installed on this DB; anything else → unknown
            result[op] = None
            result.setdefault("errors", []).append(f"{op}: {e.error_name}: {e.error_message}")
            if e.status == 404:
                result["not_installed"] = True
                break
    return result


def verify(profile_name: str, models: list[str]) -> dict:
    client = OdooClient.from_profile(profile_name)
    out: dict = {
        "profile": profile_name,
        "url": client.profile.url,
        "mode": client.profile.mode,
        "server_version": None,
        "auth_ok": None,
        "probes": [],
        "credential_can_write": None,
        "aligned": None,
        "verdict": "",
    }

    try:
        version = check_server(client)
        out["server_version"] = version.get("version")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        out["verdict"] = f"Cannot reach {client.profile.url}: {e}"
        return out

    # If the write_policy names models, include them in the probe set —
    # capability there is exactly what the policy is trying to bound.
    policy = client.profile.write_policy or {}
    for entry in policy.get("allow", []):
        if entry["model"] not in models:
            models.append(entry["model"])

    try:
        probes = [probe_model(client, m) for m in models]
        out["auth_ok"] = True
    except OdooAPIError as e:
        if e.status == 401:
            out["auth_ok"] = False
            out["verdict"] = "Authentication failed — check the API key / env var."
            return out
        raise
    out["probes"] = probes

    writable = [
        p["model"] for p in probes
        if any(p.get(op) for op in WRITE_OPERATIONS)
    ]
    unknown = [
        p["model"] for p in probes
        if not p.get("not_installed") and any(p.get(op) is None for op in WRITE_OPERATIONS)
    ]
    # A model that answered every write-op probe with a definite boolean.
    answered = [
        p["model"] for p in probes
        if not p.get("not_installed") and all(p.get(op) is not None for op in WRITE_OPERATIONS)
    ]
    out["credential_can_write"] = (bool(writable) if writable
                                   else False if answered and not unknown
                                   else None)
    out["writable_models"] = writable

    if client.profile.mode == MODE_READ_ONLY:
        if writable:
            out["aligned"] = False
            out["verdict"] = (
                f"MISALIGNED: profile {profile_name!r} says read-only, but the credential "
                f"CAN write to: {', '.join(writable)}. The client-side gate protects only "
                "calls made through this skill. To make read-only real: in Odoo, create a "
                "dedicated user whose groups grant read access only, have a human mint an "
                "API key for that user (Preferences → Account Security), point this "
                "profile's api_key at it, and re-run this check."
            )
        elif out["credential_can_write"] is False:
            out["aligned"] = True
            out["verdict"] = (
                "Aligned: the credential cannot write to any probed model — read-only is "
                "enforced server-side, not just by this client."
            )
        else:
            # Zero answered probes (all not-installed or errored) prove NOTHING —
            # never issue a positive verdict from an empty sample.
            missing = unknown or [p["model"] for p in probes]
            out["verdict"] = (
                f"Could not verify (no probe answered for: {', '.join(missing)}). "
                "Treat the credential as write-capable until proven otherwise — "
                "re-run with --models naming models that exist on this database."
            )
    else:  # read-write profile
        out["aligned"] = True
        if writable:
            out["verdict"] = (
                f"Profile is read-write; credential can write to: {', '.join(writable)}. "
                "Keep the Odoo user's groups as narrow as the work allows, and consider "
                "a write_policy block to bound what this skill will attempt."
            )
        else:
            out["verdict"] = (
                "Profile is read-write but the credential could not write to any probed "
                "model — writes will fail server-side. Check the Odoo user's groups if "
                "writes are actually needed."
            )
    return out


def print_human(out: dict) -> None:
    print(f"Profile:         {out['profile']} (mode: {out['mode']})")
    print(f"URL:             {out['url']}")
    print(f"Server version:  {out['server_version'] or 'unreachable'}")
    if out["auth_ok"] is not None:
        print(f"Auth:            {'ok' if out['auth_ok'] else 'FAILED'}")
    if out["probes"]:
        print(f"\n{'model':<22} {'read':<6} {'write':<6} {'create':<7} {'unlink':<7}")
        print("-" * 52)
        for pr in out["probes"]:
            if pr.get("not_installed"):
                print(f"{pr['model']:<22} (not installed on this database)")
                continue
            def fmt(v):  # noqa: E306
                return {True: "YES", False: "no", None: "?"}[v]
            print(f"{pr['model']:<22} {fmt(pr.get('read')):<6} {fmt(pr.get('write')):<6} "
                  f"{fmt(pr.get('create')):<7} {fmt(pr.get('unlink')):<7}")
    print(f"\n{out['verdict']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="verify_profile", description=__doc__.split("\n\n")[0])
    p.add_argument("profile", help="Profile name (or 'default')")
    p.add_argument("--models", default=None,
                   help="Comma-separated models to probe (default: a representative spread)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    args = p.parse_args(argv)

    models = ([m.strip() for m in args.models.split(",") if m.strip()]
              if args.models else list(DEFAULT_PROBE_MODELS))
    try:
        out = verify(args.profile, models)
    except OdooClientError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(out, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        print_human(out)

    if out.get("aligned") is False:
        return 2
    if out.get("aligned") is not True:
        return 1  # unreachable, auth failure, or nothing was actually verified
    return 0


if __name__ == "__main__":
    sys.exit(main())
