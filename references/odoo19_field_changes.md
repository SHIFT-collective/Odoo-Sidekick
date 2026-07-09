# Odoo 19 field renames & JSON-2 API quirks

Agents coming from older Odoo versions (or older tutorials) keep hitting the
same schema drift and protocol traps. Check this list BEFORE retrying a failed
call — the client's error hints point here for a reason. Items marked ✓ are
verified against a live Odoo 19.0 enterprise instance.

## Argument-name traps (the #1 source of 500/422s)

| You wrote | Correct for Odoo 19 JSON-2 | Notes |
|---|---|---|
| `orderby` on `search`/`search_read` | `order` | ✓ Only `read_group` takes `orderby`. The client normalizes this automatically, but fix the habit. |
| `vals` on `create` | `vals_list` (a **list** of dicts) | ✓ `{"vals": {...}}` → 422 "missing a required argument: 'vals_list'". The client normalizes `vals` → `vals_list` and wraps bare dicts. |
| positional args | named args only | JSON-2 has no positional arguments — every body key is a named argument. |

## Field renames / removals that keep burning agents

| Old name | Odoo 19 | Model(s) |
|---|---|---|
| `groups_id` | `group_ids` | `res.users` |
| `kanban_state` | *(removed)* | `project.task` — use `state` |
| `is_close` | `fold` | `helpdesk.stage` |
| `name_get()` | `display_name` field | read `display_name` instead of calling `name_get` |
| `read_group` | still works, but `formatted_read_group` is the Odoo 19 native form | any model — ✓ both callable over JSON-2 |

When any field guess fails: `python -m scripts.introspect <profile> --model <model>`
lists the real fields; `--find <word>` fuzzy-searches model names.

## Chatter: message_post double-escapes HTML by default

✓ Verified: without `body_is_html`, HTML in `body` is escaped into literal
`&lt;p&gt;` text that a human has to delete by hand.

```json
{"ids": [42], "body": "<p>Approved per KIN-7902</p>", "body_is_html": true}
```

Always pass `"body_is_html": true` when the body contains markup.

## Methods you cannot call over JSON-2

- **`_`-prefixed (private) methods** — ✓ server refuses with 403
  "Private methods cannot be called remotely". Look for a public wrapper.
- **Methods without a parameter map** (some `@api.model` internals) — refused
  or 404. If a method 404s but the model exists, this is usually why.
- `base.automation.create` is known to 500 ("unhashable type: 'list'") over
  JSON-2 on some builds while the same payload works on other models. If you
  hit it, create the automation by hand in the UI or via the legacy
  `/jsonrpc` endpoint — and pin a note in your deployment.

## Authentication quirks (Odoo SaaS)

- `common.authenticate()` may return `uid=False` on SaaS even with valid
  credentials. You don't need the login dance at all: JSON-2 authenticates
  per-request via `Authorization: bearer <api_key>`.
- API keys can only be minted by a human in the Odoo UI (Preferences →
  Account Security → New API Key). Plan key rotation around that.
- **SaaS staging rebuilds regenerate the database name's hash suffix** (e.g.
  `yourco-staging-12345678`). Anything that hardcodes the DB name silently
  breaks after a rebuild. Set `database: auto` in the profile — ✓ the client
  resolves the name at runtime via `/web/database/list`.

## Restricted keys and field-level ACLs

With a restricted API user, a `read`/`search_read` that includes a field the
user cannot read fails the **whole call** with an AccessError naming the field
(e.g. `credit_limit`) — Odoo does not skip unreadable fields. Drop the named
field from `fields` and retry. This is another reason to always pass an
explicit `fields` list.

## Attachments

`ir.attachment.datas` is the full file as base64 — a multi-MB context bomb
over the API (and `read` without `fields` includes it). `/web/content/<id>`
does NOT accept bearer API keys (✓ returns 404), so there is no streaming
shortcut. Use `python -m scripts.get_attachment <profile> --id <id> --out <file>`,
which decodes to disk and prints only path + metadata.
