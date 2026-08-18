"""
Download an ir.attachment to a local file — never inline into the chat.

Why this exists: reading `ir.attachment.datas` over the API returns the whole
file as base64 in the JSON response. Multi-MB blobs pulled inline are a
context-window hazard on every Claude surface (real-world logs show 9 MB+
single responses). This script decodes the payload straight to disk and
prints only the path and metadata.

Usage:
    python3 -m scripts.get_attachment <profile> --id 12245
    python3 -m scripts.get_attachment <profile> --id 12245 --out ./deck.pdf
    python3 -m scripts.get_attachment <profile> --id 12245 --json

The sha1 of the written file is checked against Odoo's stored checksum.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    from .odoo_client import OdooClient, OdooClientError
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from odoo_client import OdooClient, OdooClientError  # type: ignore


META_FIELDS = ["name", "mimetype", "file_size", "checksum", "type", "url",
               "res_model", "res_id", "create_date"]


def _safe_filename(name: str) -> str:
    """Attachment names are server data, ultimately controllable by whoever
    created the record (e.g. inbound email senders) — never let one become a
    dotfile or an empty/parent-dir name."""
    cleaned = re.sub(r"[^\w.\- ]+", "_", name).strip().lstrip(".").strip()
    if not cleaned:
        cleaned = "attachment"
    return cleaned[:150]


def fetch(profile: str, attachment_id: int, out: str | None,
          overwrite: bool = False) -> dict:
    client = OdooClient.from_profile(profile)
    rows = client.read("ir.attachment", [attachment_id], fields=META_FIELDS)
    if not rows:
        raise OdooClientError(f"No ir.attachment with id {attachment_id}.")
    meta = rows[0]

    if meta.get("type") == "url":
        return {
            "id": attachment_id,
            "name": meta.get("name"),
            "type": "url",
            "url": meta.get("url"),
            "note": "This attachment is a URL reference — nothing to download.",
        }

    # The one sanctioned inline read of `datas`: it goes straight to disk.
    data_rows = client.read("ir.attachment", [attachment_id], fields=["datas"])
    b64 = (data_rows[0].get("datas") or "") if data_rows else ""
    blob = base64.b64decode(b64) if b64 else b""

    out_path = Path(out) if out else Path.cwd() / _safe_filename(str(meta.get("name") or f"attachment_{attachment_id}"))
    if out_path.exists() and not overwrite:
        raise OdooClientError(
            f"{out_path} already exists — pass --out with a different path, or "
            "--overwrite to replace it."
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(blob)

    sha1 = hashlib.sha1(blob).hexdigest()
    stored = meta.get("checksum") or ""
    return {
        "id": attachment_id,
        "name": meta.get("name"),
        "path": str(out_path.resolve()),
        "bytes_written": len(blob),
        "file_size_reported": meta.get("file_size"),
        "mimetype": meta.get("mimetype"),
        "sha1": sha1,
        "checksum_match": (sha1 == stored) if stored else None,
        "res_model": meta.get("res_model") or None,
        "res_id": meta.get("res_id") or None,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="get_attachment", description=__doc__.split("\n\n")[0])
    p.add_argument("profile", help="Profile name (or 'default')")
    p.add_argument("--id", type=int, required=True, help="ir.attachment id")
    p.add_argument("--out", help="Output file path (default: attachment name in cwd)")
    p.add_argument("--overwrite", action="store_true",
                   help="Replace the output file if it already exists")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    args = p.parse_args(argv)

    try:
        result = fetch(args.profile, args.id, args.out, overwrite=args.overwrite)
    except OdooClientError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        if result.get("type") == "url":
            print(f"Attachment {result['id']} is a URL reference: {result['url']}")
        else:
            print(f"Wrote {result['bytes_written']:,} bytes to {result['path']}")
            print(f"  name:     {result['name']}")
            print(f"  mimetype: {result['mimetype']}")
            match = result["checksum_match"]
            print(f"  sha1:     {result['sha1']} "
                  f"({'matches Odoo checksum' if match else 'NO CHECKSUM MATCH' if match is False else 'no stored checksum'})")
            if result.get("res_model"):
                print(f"  attached to: {result['res_model']} #{result['res_id']}")
    if result.get("checksum_match") is False:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
