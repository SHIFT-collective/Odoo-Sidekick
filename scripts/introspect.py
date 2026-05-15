"""
Discover what's available on an Odoo instance.

Usage:
    # List all installed models on this database
    python -m scripts.introspect <profile> --list-models

    # Search for models matching a pattern
    python -m scripts.introspect <profile> --list-models --pattern stock

    # Show fields and types for a specific model
    python -m scripts.introspect <profile> --model sale.order

    # Show only certain attributes per field
    python -m scripts.introspect <profile> --model sale.order --attrs string,type,help,required
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from .odoo_client import OdooClient, OdooClientError
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from odoo_client import OdooClient, OdooClientError  # type: ignore


def list_models(client: OdooClient, pattern: str | None) -> list[dict]:
    domain: list = []
    if pattern:
        domain = [["model", "ilike", pattern]]
    return client.search_read(
        "ir.model",
        domain=domain,
        fields=["model", "name", "transient"],
        order="model asc",
        limit=5000,
    )


def describe_model(client: OdooClient, model: str, attrs: list[str]) -> dict:
    return client.fields_get(model, attributes=attrs)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="introspect")
    p.add_argument("profile", help="Profile name (or 'default')")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--list-models", action="store_true",
                     help="List all installed models on the database")
    grp.add_argument("--model", help="Describe fields for one model")
    p.add_argument("--pattern", help="With --list-models, filter by name pattern (ilike)")
    p.add_argument("--attrs", default="string,type,required,readonly,store,help,relation,selection",
                   help="Comma-separated field attributes to show with --model")
    args = p.parse_args(argv)

    try:
        client = OdooClient.from_profile(args.profile)
    except OdooClientError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    try:
        if args.list_models:
            rows = list_models(client, args.pattern)
            print(f"{'model':<40}  {'name':<50}  transient")
            print("-" * 110)
            for r in rows:
                t = "yes" if r.get("transient") else ""
                print(f"{r['model']:<40}  {(r.get('name') or ''):<50}  {t}")
            print(f"\n{len(rows)} model(s).", file=sys.stderr)
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
