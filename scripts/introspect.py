"""
Discover what's available on an Odoo instance.

Usage:
    # List all installed models on this database
    python3 -m scripts.introspect <profile> --list-models

    # Search for models matching a pattern (server-side ilike)
    python3 -m scripts.introspect <profile> --list-models --pattern stock

    # Fuzzy-find a model when you're not sure of its name — use this BEFORE
    # guessing a model name (guessed names 404).
    python3 -m scripts.introspect <profile> --find station

    # Show fields and types for a specific model
    python3 -m scripts.introspect <profile> --model sale.order

    # Show only certain attributes per field
    python3 -m scripts.introspect <profile> --model sale.order --attrs string,type,help,required

    # Machine-readable output for any of the above
    python3 -m scripts.introspect <profile> --find station --json

--find works against a locally cached copy of the instance's model list
(<state_dir>/model_cache/, 24h TTL) so repeated lookups don't hit the API.
Pass --refresh to force a re-fetch.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
import time
from pathlib import Path

try:
    from .odoo_client import OdooClient, OdooClientError, state_dir
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from odoo_client import OdooClient, OdooClientError, state_dir  # type: ignore


MODEL_CACHE_TTL_S = 24 * 3600


def list_models(client: OdooClient, pattern: str | None) -> list[dict]:
    domain: list = []
    if pattern:
        domain = ["|", ["model", "ilike", pattern], ["name", "ilike", pattern]]
    return client.search_read(
        "ir.model",
        domain=domain,
        fields=["model", "name", "transient"],
        order="model asc",
        limit=5000,
    )


def _model_cache_path(client: OdooClient) -> Path:
    # Key on url AND database — multi-DB hosts serve different model sets
    # per database behind one url.
    ident = f"{client.profile.url}|{client.profile.database or ''}"
    key = hashlib.sha1(ident.encode()).hexdigest()[:16]
    return state_dir() / "model_cache" / f"{key}.json"


def cached_models(client: OdooClient, refresh: bool = False) -> list[dict]:
    """The instance's full model list, cached locally with a 24h TTL."""
    cache = _model_cache_path(client)
    if not refresh and cache.exists():
        try:
            payload = json.loads(cache.read_text())
            if time.time() - payload.get("fetched_at", 0) < MODEL_CACHE_TTL_S:
                return payload["models"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    models = list_models(client, None)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"fetched_at": time.time(), "url": client.profile.url,
                                     "models": models}))
    except OSError:
        pass  # cache is a convenience, not a requirement
    return models


def find_models(client: OdooClient, query: str, refresh: bool = False,
                limit: int = 20) -> list[dict]:
    """Rank models against a fuzzy query: substring hits first (technical name
    beats display name), then difflib near-misses for typos."""
    models = cached_models(client, refresh=refresh)
    q = query.lower().strip()
    scored: list[tuple[float, dict]] = []
    for m in models:
        tech = (m.get("model") or "").lower()
        disp = (m.get("name") or "").lower()
        if q in tech:
            score = 100 - len(tech) * 0.1  # prefer shorter (more exact) names
        elif q in disp:
            score = 80 - len(disp) * 0.1
        else:
            ratio = max(
                difflib.SequenceMatcher(None, q, tech).ratio(),
                max((difflib.SequenceMatcher(None, q, part).ratio()
                     for part in tech.replace(".", " ").replace("_", " ").split()),
                    default=0.0),
            )
            if ratio < 0.6:
                continue
            score = ratio * 60
        scored.append((score, m))
    scored.sort(key=lambda t: (-t[0], t[1].get("model") or ""))
    return [m for _, m in scored[:limit]]


def describe_model(client: OdooClient, model: str, attrs: list[str]) -> dict:
    return client.fields_get(model, attributes=attrs)


def _print_model_table(rows: list[dict]) -> None:
    print(f"{'model':<40}  {'name':<50}  transient")
    print("-" * 110)
    for r in rows:
        t = "yes" if r.get("transient") else ""
        print(f"{r['model']:<40}  {(r.get('name') or ''):<50}  {t}")
    print(f"\n{len(rows)} model(s).", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="introspect")
    p.add_argument("profile", help="Profile name (or 'default')")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--list-models", action="store_true",
                     help="List all installed models on the database")
    grp.add_argument("--find", metavar="QUERY",
                     help="Fuzzy-search installed models (uses a local 24h cache)")
    grp.add_argument("--model", help="Describe fields for one model")
    p.add_argument("--pattern", help="With --list-models, filter by name pattern (ilike)")
    p.add_argument("--attrs", default="string,type,required,readonly,store,help,relation,selection",
                   help="Comma-separated field attributes to show with --model")
    p.add_argument("--refresh", action="store_true",
                   help="With --find, bypass the local model-list cache")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    args = p.parse_args(argv)

    try:
        client = OdooClient.from_profile(args.profile)
    except OdooClientError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    try:
        if args.list_models:
            rows = list_models(client, args.pattern)
            if args.json:
                json.dump(rows, sys.stdout, indent=2, default=str)
                sys.stdout.write("\n")
            else:
                _print_model_table(rows)
        elif args.find:
            rows = find_models(client, args.find, refresh=args.refresh)
            if args.json:
                json.dump(rows, sys.stdout, indent=2, default=str)
                sys.stdout.write("\n")
            elif rows:
                _print_model_table(rows)
            if not rows:
                # same exit code in both output modes — headless callers rely on it
                print(f"No model matches {args.find!r}. Try a shorter fragment, or "
                      "--list-models to browse everything.", file=sys.stderr)
                return 1
        else:
            attrs = [a.strip() for a in args.attrs.split(",") if a.strip()]
            fields = describe_model(client, args.model, attrs)
            json.dump(fields, sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
    except OdooClientError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
