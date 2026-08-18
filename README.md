# Odoo Sidekick by SHIFTcollective

A Claude skill for querying Odoo V19 via the External JSON-2 API. Defaults to
read-only; supports opt-in writes with explicit per-request user confirmation.

**Just want to use it day to day?** See [`quick-start-guide.md`](quick-start-guide.md)
(also [`quick-start-guide.pdf`](quick-start-guide.pdf)) — a 6-page, non-technical
guide to what to ask and how to get the most out of it, aimed at everyday Odoo users.

**Setting it up, or need the full reference?** See [`technical-manual.md`](technical-manual.md)
(also [`technical-manual.pdf`](technical-manual.pdf)) for the in-depth walkthrough:
setup, the safety model, everyday reads and writes, caching, headless/automation
use, and a command reference. This README stays focused on install and architecture.

## Install

1. Copy this folder into your skills directory (or package it into a `.skill` file).
2. Install the Python deps you need:
   - PyYAML (only if you write profiles in YAML; JSON works without it):
     `pip install pyyaml`
   - DuckDB (only if you want the DuckDB cache backend):
     `pip install duckdb`
   - SQLite is in the stdlib — no install.
3. Run the self-test — it verifies the install is complete before anything
   else has a chance to fail confusingly:
   `python3 -m scripts.selftest`
4. Create your profile config at `~/.config/odoo-sidekick/profiles.yaml` —
   either based on `assets/profiles.example.yaml`, or with the CLI:
   `python3 -m scripts.profiles add main --url https://yourco.odoo.com --api-key-env ODOO_MAIN_API_KEY`
   **All profiles default to read-only.** Set `mode: read-write` on a profile
   only when you genuinely need writes.
5. Set your API key env var(s):
   `export ODOO_MAIN_API_KEY=...`
6. (Optional) Check what environment you're in:
   `python3 -m scripts.detect_env`
   The skill recognizes Claude.ai (sandboxed/ephemeral), Cowork and Claude
   Code (local/persistent), plus a declared headless mode for autonomous
   agents, and surfaces use-case guidance for each.
7. Test connectivity and credential alignment:
   `python3 -m scripts.selftest --profile main`
   `python3 -m scripts.verify_profile main`
   The second command asks Odoo what your API key can actually do — a
   read-only profile holding a write-capable key gets flagged loudly,
   because the profile mode alone is client-side, not a security boundary.
8. (Optional) Tell the skill who you are so it tailors itself — in chat
   ("tailor to me") or pre-seeded for headless agents:
   `python3 -m scripts.user_profile seed --file assets/user_profile.example.yaml`
   Then `python3 -m scripts.user_profile guidance` shows what changes.

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
├── quick-start-guide.md               # 6-page, non-technical guide for everyday Odoo users
├── quick-start-guide.pdf
├── technical-manual.md                # Full technical manual: setup, safety model, reference
├── technical-manual.pdf
├── LICENSE                           # Apache 2.0 + Commons Clause + Competitive Use Restriction
├── assets/
│   ├── profiles.example.yaml         # Profile config template
│   └── user_profile.example.yaml     # User-context seed template (headless pre-seed)
├── VERSION                           # Single source of truth for the skill's version
├── scripts/
│   ├── odoo_client.py                # JSON-2 client: mode/policy/confirm gates, retries, error hints, audit log
│   ├── profiles.py                   # Safe profile editing: validated, locked, atomic
│   ├── user_profile.py               # User context & role profiling: capture, seed, derive guidance
│   ├── verify_profile.py             # Prove what a credential can do server-side
│   ├── selftest.py                   # Install/config/env sanity check — run first
│   ├── introspect.py                 # Schema & model discovery incl. fuzzy --find
│   ├── get_attachment.py             # Download attachments to disk (never inline)
│   ├── cache_sync.py                 # DuckDB/SQLite sync (always read-only)
│   ├── cache_status.py               # Report cache freshness per model
│   ├── detect_env.py                 # Surface detection (incl. headless) + guidance
│   ├── check_updates.py              # Compare local VERSION against upstream GitHub
│   └── show_metrics.py               # Aggregate the per-call metrics/audit log
└── references/
    ├── api_reference.md
    ├── odoo19_field_changes.md       # Odoo 19 renames + JSON-2 traps (check before retrying!)
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

Layered gates protect against unintended writes:

### 0. The credential (the only real security boundary)
Everything below is enforced client-side, which binds only code that goes
through this client. The API key keeps whatever rights its Odoo user has.
Bind read-only profiles to genuinely restricted Odoo users, and prove the
alignment with `python3 -m scripts.verify_profile <name>` — it asks Odoo
directly (`has_access`, never mutates) and exits 2 with remediation steps
when a "read-only" profile holds a write-capable key.

### 1. Profile mode (mechanical, client-side)
Every profile has `mode: read-only` (default) or `mode: read-write`. The
Python client checks this before any HTTP request and refuses writes on
read-only profiles — even with `confirm=True`. Read-write profiles can be
narrowed further with a `write_policy` (model/method allowlist plus hard-denied
models) and `require_auth_ref` (every write must carry a ticket/approval
reference, which is recorded in the call log).

### 2. Per-call confirmation (script safety)
In read-write mode, all non-read methods require `confirm=True` (CLI: `--confirm`).
Without it, the client raises `WriteNotConfirmed` carrying a preview of what
would have been sent. For a richer artifact, `--dry-run` fetches current values
and prints a before→after diff per record without sending anything.

### 3. Batched confirmation (per user request)
Every user request that implies writes is presented as a consolidated summary
in chat, with a poll-style prompt asking once. Authorization scope is the
current user message only — no rolling forward across turns. In headless
deployments (no human at call time) this layer is replaced by the profile's
`confirm_cmd` approval hook. See `SKILL.md` for both patterns.

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

# Real call — auth_ref ties the write to an approval in the audit log
new_ids = c.create("res.partner", [{"name": "Test"}], confirm=True, auth_ref="TICKET-42")
c.write("res.partner", new_ids, {"phone": "+34 ..."}, confirm=True)
c.execute("sale.order", "action_confirm", ids=[123], confirm=True)
c.unlink("res.partner", new_ids, confirm=True)   # irreversible!

# Rich preview without sending anything: current values + before→after diff
print(c.dry_run("res.partner", "write", {"ids": new_ids, "vals": {"phone": "+1 ..."}}))
```

## Roadmap

### Released

- **v1.0** — Read-only access + caching
- **v1.1** — Optional read-write mode with confirmation gates
- **v1.2** — Per-request batched confirmation, onboarding flow, license
- **v1.3** — Competitive Use Restriction, expanded onboarding (mode-first, two-path API key, welcome-back, failure recovery)
- **v1.4** — Renamed to "Odoo Sidekick by SHIFTcollective"
- **v1.5** — Environment auto-detection (Claude.ai / Cowork / Claude Code), surface-aware onboarding, strengthened chat-history warnings, rotation reminder
- **v1.6** — Update checker (compares local VERSION against upstream GitHub, with 24h cache, release notes, graceful network-failure handling)
- **v1.7** — Per-call metrics logging (`show_metrics`), cache freshness reporting (`cache_status`), conversational staleness prompts before querying cached data
- **v1.8** — Reliability, audit & headless hardening (shaped by production feedback from a 24-day / 8,600-call multi-agent deployment)
  - Server error bodies surfaced with remediation hints on every failure; `--json-errors` for machine callers; bounded retry with backoff+jitter for transient read failures (never writes, never deterministic errors)
  - Audit trail: `caller` (ODOO_SIDEKICK_CALLER) and `auth_ref` (`--auth-ref`) recorded per call; `require_auth_ref` profiles; `show_metrics --by-caller`; log rotation archives instead of truncating
  - `verify_profile.py` proves credential capability server-side; `write_policy` model/method allowlists; `confirm_cmd` headless approval hook
  - `profiles.py` CLI (validated, locked, atomic config edits; canonical JSON); `ODOO_SIDEKICK_STATE_DIR` for multi-agent hosts; `database: auto` (SaaS rebuild immunity)
  - Rich `--dry-run` with per-record before→after diffs — the write preview IS the approval artifact
  - `get_attachment.py` (downloads to disk — no more multi-MB base64 in context); `introspect --find` fuzzy model discovery; `selftest.py`; headless surface in `detect_env`; `references/odoo19_field_changes.md`

- **v1.9** — User context & role profiling + plumbing hardening ← **current**
  - `scripts/user_profile.py`: capture **role / company size / goals / pain points / decision cadence** at `<state_dir>/user_profile.yaml` (canonical-JSON-in-`.yaml`, locked, atomic — same conventions as profiles). Persona-agnostic: `role: ops-agent` is as valid as `role: CFO`.
  - `guidance` derives concrete defaults: role-family playbooks (lead reports + likely models), phrasing depth (executive / balanced / operational), a materiality hint scaled by company size, a comparison window from cadence, and agent-consumer detection (`is_agent` switches phrasing to structured/JSON) — the hooks v2.0 routines and v2.1 insights build on.
  - Capture is **skippable** (≤3 quick questions; a skip is recorded and never re-asked) and **pre-seedable** for headless (`seed --file`, template at `assets/user_profile.example.yaml`). `selftest` and `detect_env` surface the profile's presence.
  - Plumbing hardening from a full pass over the scripts plus production feedback: update checker no longer reports a stale `update_available` after upgrading; incremental cache sync records `synced_at` on no-change runs (kills spurious staleness prompts) and uses a `>=` boundary (no lost boundary-second records); `cache_status` no longer creates the DB file / crashes on a missing one, and names the missing model when `--table` isn't cached; `show_metrics` survives malformed log lines and rejects bad `--since` cleanly; `odoo_client --json-errors` now also covers refusal exits 3/6; `profiles show` honors `--json` vs human output; selftest checks the SKILL.md frontmatter length; clearer PyYAML remediation hints.
  - Hardening driven by a large multi-agent production deployment: **all documented commands use `python3 -m`** (bare `python` is missing from PATH on many modern hosts — the old docs made even `selftest` fail to launch); `profiles.py` **writes through a symlink** instead of clobbering it (deployments that symlink `profiles.yaml` at a canonical store kept breaking); and `selftest` gained **`--only NAME[,…]`** and **`--list`** so a deploy pipeline can gate on one specific check (exit 2 when a named check never ran) rather than an aggregate exit code that stops discriminating once any unrelated check goes red.

### Planned

#### v2.0 — Scheduled routines & check-ins

Goal: a registry of routines the user can opt into, role-aware, executable on schedule (Cowork/Code) or on demand (Claude.ai).

Built-in routine templates, selected based on the v1.9 role:
- **Daily standup (CEO/Founder)**: cash position, top 3 sales of yesterday, open MOs starting today, urgent leads, anything new requiring attention.
- **Morning check-in (CFO)**: cash, AR aging delta vs yesterday, AP coming due this week, posted-but-unreconciled flags.
- **Operations standup (Ops Director)**: open MOs, stock-out risks (orderpoints crossed), late shipments, production efficiency vs yesterday.
- **Sales pipeline review (Sales Lead)**: pipeline value by stage, deals advancing, deals stalling >14 days, new leads this week, win rate trend.
- **Weekly review (any role)**: cadence-appropriate summary of the last 7 days vs prior 7.

Routines as YAML at `routines/<name>.yaml` — schedule, required role, queries, output template, optional alert thresholds. A `routine_runner.py` script executes them.

Design commitment: routines are **plain CLI invocations + YAML definitions that any external scheduler can drive** — cron, CI, launchd, Task Scheduler, or an orchestration platform with its own scheduler. No built-in daemon. The valuable parts are the definitions (queries + thresholds + output template); alert thresholds ("tell me only when AR aging crosses X") are what let both humans and autonomous agents stop burning attention re-checking dashboards. Runners honor headless mode (`--json` output, exit codes signal whether thresholds fired).

On Cowork/Code: integrate with cron/launchd/Task Scheduler for unattended runs that produce an email or markdown summary. On Claude.ai: quick-launch buttons to run "today's standup".

#### v2.1 — Insight layers with suggested actions

Builds on the cache (v1.0), the v1.8 reliability layer (retries + trustworthy error surfacing are prerequisites for trustworthy baselines), user context (v1.9), and routines (v2.0). Each insight is paired with a concrete suggested action.

- **Manufacturing demand forecasting** — compares open MO demand + sales forecast against on-hand stock and lead times. Surface: "Component X is short by 240 units against the next 30 days. Suggested action: raise PO with vendor Y (lead time 7 d, last unit price €4.20)."
- **Accounting trend detection** — P&L deltas vs prior period, AR aging shifts (movement between buckets), AP concentration, gross margin drift by product line. Surface: "AR > 60 days grew €18k this week, concentrated in two customers. Suggested action: payment reminder to Acme (€11k) and Beta (€7k)."
- **Sales/CRM insights** — cohort analysis (new vs returning revenue), deal velocity by stage, win rate by source/segment, churn signals (customer revenue dropping). Suggested actions tied to specific accounts or deals.
- **Anomaly detection** — alerts when metrics deviate from rolling baselines (significantly slow week, unusually large invoice, vendor charge that doesn't match a PO, inventory count discrepancy).
- **Role-aware suggested-actions queue** — when the user opens the skill, a short list of "things worth your attention right now," filtered to their role. Respects headless mode: emits JSON, not a briefing paragraph.

The v2.1 surface is meant to feel like a sidekick who's been watching the business overnight and has 3 things they think you should know about — not a dashboard that requires the user to go looking.

### Beyond v2.1 (loose ideas)

- Two-way write actions tied to insights ("send those payment reminders" → drafts emails or creates Odoo activities). These route through the same write-gate machinery as everything else — mode, write_policy, auth_ref, confirm_cmd — because an insight layer that can act is an agent, and inherits every safety question above.
- Multi-tenant aggregation (compare metrics across multiple client Odoos for consultancies, where allowed by license).
- Integration with non-Odoo sources (bank feeds, payment processors, analytics tools).
- A web-based skill admin UI for managing profiles, routines, and viewing metrics.

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
