# Odoo Sidekick by SHIFTcollective

A Claude skill for querying Odoo V19 via the External JSON-2 API. Defaults to
read-only; supports opt-in writes with explicit per-request user confirmation.

## Install

1. Copy this folder into your skills directory (or package it into a `.skill` file).
2. Install the Python deps you need:
   - PyYAML (only if you write profiles in YAML; JSON works without it):
     `pip install pyyaml`
   - DuckDB (only if you want the DuckDB cache backend):
     `pip install duckdb`
   - SQLite is in the stdlib — no install.
3. Create your profile config at `~/.config/odoo-sidekick/profiles.yaml`
   based on `assets/profiles.example.yaml`. **All profiles default to
   read-only.** Set `mode: read-write` on a profile only when you genuinely
   need writes.
4. Set your API key env var(s):
   `export ODOO_MAIN_API_KEY=...`
5. (Optional) Check what environment you're in:
   `python -m scripts.detect_env`
   The skill recognizes Claude.ai (sandboxed/ephemeral), Cowork and Claude
   Code (local/persistent) and surfaces use-case guidance for each.
6. Test connectivity:
   `python -m scripts.introspect main --list-models --pattern sale`

## Which Claude surface should I use this from?

| Surface | Filesystem | Config persists? | Best for |
|---|---|---|---|
| **Claude.ai** (web/mobile) | Sandboxed | No — ephemeral per conversation | One-off queries, exploration, mobile, demos |
| **Cowork** | Your real filesystem | Yes | Recurring reports, local-file workflows (Excel/PDF), scheduled work |
| **Claude Code** | Your real filesystem | Yes | Skill iteration, batch ops, automation, git |

The skill itself travels across all three; profile configs do not. For
persistent setups, install on Cowork or Claude Code. Run `detect_env` any
time to see which surface you're in and what the implications are.

## Layout

```
odoo-sidekick/
├── SKILL.md                          # Entry point — read first
├── README.md                         # This file
├── LICENSE                           # Apache 2.0 + Commons Clause + Competitive Use Restriction
├── assets/
│   └── profiles.example.yaml         # Profile config template
├── VERSION                           # Single source of truth for the skill's version
├── scripts/
│   ├── odoo_client.py                # JSON-2 client with mode + confirm gates
│   ├── cache_sync.py                 # DuckDB/SQLite sync (always read-only)
│   ├── introspect.py                 # Schema & model discovery (always read-only)
│   ├── detect_env.py                 # Surface detection + use-case guidance
│   └── check_updates.py              # Compare local VERSION against upstream GitHub
└── references/
    ├── api_reference.md
    ├── domain_syntax.md
    ├── analytics_patterns.md
    ├── caching_strategy.md
    └── models/
        ├── sales.md
        ├── accounting.md
        ├── inventory.md
        ├── manufacturing.md
        ├── partners.md
        └── purchase.md
```

## Safety model

Three independent gates protect against unintended writes:

### 1. Profile mode (mechanical)
Every profile has `mode: read-only` (default) or `mode: read-write`. The
Python client checks this before any HTTP request. A read-only profile cannot
write under any circumstance — even with `confirm=True`, even with code that
tries to bypass the bound method shortcuts.

### 2. Per-call confirmation (script safety)
In read-write mode, all non-read methods require `confirm=True` (CLI: `--confirm`).
Without it, the client raises `WriteNotConfirmed` carrying a preview of what
would have been sent — a built-in dry-run.

### 3. Chat-level batched confirmation (per user request)
Every user request that implies writes is presented as a consolidated summary
in chat, with a poll-style prompt asking once. Authorization scope is the
current user message only — no rolling forward across turns. See `SKILL.md`
for the exact pattern.

### Defense in depth
Bind read-write profile API keys to Odoo users with the narrowest possible
group set. Process safety and chat safety stop most problems; Odoo's own ACLs
limit blast radius if all else fails.

## Quick reference: writes

```python
from scripts.odoo_client import OdooClient, WriteNotAllowed, WriteNotConfirmed

c = OdooClient.from_profile("main_rw")   # must be read-write mode

# Dry-run preview (raises WriteNotConfirmed with .preview attached)
try:
    c.create("res.partner", [{"name": "Test"}])
except WriteNotConfirmed as e:
    print(e.preview)   # shows model, method, impact, args

# Real call
new_ids = c.create("res.partner", [{"name": "Test"}], confirm=True)
c.write("res.partner", new_ids, {"phone": "+34 ..."}, confirm=True)
c.execute("sale.order", "action_confirm", ids=[123], confirm=True)
c.unlink("res.partner", new_ids, confirm=True)   # irreversible!
```

## Roadmap

- v1.0 — Read-only access + caching ✓
- v1.1 — Optional read-write mode with confirmation gates ✓
- v1.2 — Per-request batched confirmation, onboarding flow, license ✓
- v1.3 — Competitive Use Restriction, expanded onboarding (mode-first, two-path API key, welcome-back, failure recovery) ✓
- v1.4 — Renamed to "Odoo Sidekick by SHIFTcollective" ✓
- v1.5 — Environment auto-detection (Claude.ai / Cowork / Claude Code), surface-aware onboarding, strengthened chat-history warnings, rotation reminder ✓
- v1.6 — Update checker (compares local VERSION against upstream GitHub, with 24h cache, release notes, graceful network-failure handling) ✓ (current)
- v2.0 — Insight layers on the cache: manufacturing demand forecasting,
  accounting-trend detection (P&L deltas, AR aging shifts, vendor
  concentration), sales/CRM insights (cohort analysis, deal velocity).

## About

Built by **SHIFTcollective** — an AI-first consultancy bringing enterprise-grade
systems and processes within reach of small and mid-sized businesses. We build
in service of a simple idea: better tools make work more enjoyable, and people
who enjoy their work do it best.

Need help with this skill, or with rethinking how your business runs?
- Email: [info@shiftcollective.co](mailto:info@shiftcollective.co)
- Web: [shiftcollective.co](https://shiftcollective.co)

## License

Apache License 2.0 with Commons Clause and Competitive Use Restriction.
Copyright © 2026 SHIFTcollective.

**You may:**
- Use this skill for commercial purposes inside your own business
- Use it to analyze or operate your own Odoo (any industry)
- Modify it for your own needs
- Distribute it (keeping the license and attribution intact)

**You may not:**
- Sell this skill, or a product/service whose value derives substantially from
  its functionality, to third parties (no resale, no SaaS wrapping, no
  "Odoo-Analytics-as-a-Service" rebrand)
- Use it inside a competing consultancy to provide services to third parties.
  Specifically, if your business competes with SHIFTcollective — AI-driven
  consulting, ERP/Odoo implementation, business-systems integration, or
  automation services for SMBs — you can't use this skill to deliver those
  services to your clients.

The competitive-use restriction is about who you serve, not where you work.
A consultant using this skill on their own firm's books is fine; the same
consultant using it on a client engagement is the case the restriction
addresses.

If you want to embed this skill in something you sell, use it as the basis
of a paid service, or use it inside a competing consultancy, contact
[info@shiftcollective.co](mailto:info@shiftcollective.co) for commercial licensing.

See `LICENSE` for full terms.
