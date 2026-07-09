# JSON-2 API reference (Odoo 19)

The new External JSON-2 API replaces the deprecated XML-RPC and JSON-RPC endpoints
(both scheduled for removal in Odoo 22). It's a plain JSON-over-HTTPS protocol
with proper HTTP status codes and named arguments.

## Endpoint shape

```
POST /json/2/<model>/<method>
```

The model and method are positional in the URL. All method arguments go in the
JSON body as named keys.

## Headers

| Header | Required | Notes |
|---|---|---|
| `Authorization: bearer <api_key>` | yes | API key from a user's profile in Odoo |
| `Content-Type: application/json; charset=utf-8` | yes | |
| `X-Odoo-Database: <db_name>` | sometimes | Required only on multi-database hosts |
| `User-Agent: ...` | recommended | Helps with debugging on the server side |

API keys are obtained from the user's profile in Odoo (Settings → My Profile →
Account Security → New API Key). They're tied to one user and inherit that user's
access rights, so the recommended pattern is a dedicated read-only user.

## Request body

A single JSON object containing the method's named arguments, plus optionally
`context` and `ids`:

```json
{
  "context": {"lang": "en_US", "tz": "Europe/Madrid"},
  "domain": [["state", "=", "sale"]],
  "fields": ["name", "amount_total"],
  "limit": 100,
  "offset": 0,
  "order": "date_order desc"
}
```

There is no way to pass positional arguments. All arguments must be named.

## Response

### Success
HTTP 200 with the JSON-serialized return value of the called method in the body.
For `search_read` this is a list of dicts; for `read_group` a list of dicts with
aggregates; for `search_count` an integer; etc.

### Error
HTTP 4xx or 5xx with a JSON error object:

```json
{
  "name": "odoo.exceptions.AccessError",
  "message": "You are not allowed to access 'Sales Order' (sale.order) records.",
  "arguments": ["You are not allowed..."],
  "context": {"lang": "en_US"},
  "debug": "Traceback (most recent call last):..."
}
```

Common statuses:
- `401` — bad/missing API key
- `403` — user lacks access (check group membership); also private (`_`-prefixed) methods
- `404` — wrong URL: typo in model name, method not exposed, or `/json/2` not enabled on this host
- `400` — malformed domain, bad argument, etc.
- `422` — argument-shape errors (e.g. `vals` instead of `vals_list`) and validation errors
- `500` — server-side exception (look at `name` and `debug`)

The `message` field almost always names the exact problem (unknown field,
bad argument name, violated constraint). **Read it before retrying — a call
that failed with a 4xx or a deterministic 500 (builtins.*, odoo.exceptions.*)
will fail identically if re-issued unchanged.** The client surfaces `name`,
`message`, a remediation hint, and the traceback tail on every error; see
`references/odoo19_field_changes.md` for the recurring traps.

### Retry semantics (what the client does for you)

The client auto-retries **read methods only**, with exponential backoff +
jitter, on transient statuses: 429/502/503/504, network errors, and 500s whose
exception is not deterministic (e.g. psycopg2 serialization failures under
load). Writes are never auto-retried — a gateway timeout can mask a committed
transaction, so blind re-issue risks double-writes. Deterministic errors
(bad field, validation) are never retried at all.

## Discovery

Two useful endpoints beyond `/json/2`:

- `GET /web/version` — returns server version. Use to verify connectivity:
  ```json
  {"version_info": [19, 0, 0, "final", 0, ""], "version": "19.0"}
  ```
- `GET /doc` — Odoo 19's auto-generated documentation page for the current
  database, listing every model, field, and method with signatures and help
  text. Browse this in a browser when constructing complex queries.

## Method reference (the read-only ones)

### `search`
```json
{"domain": [...], "limit": 100, "offset": 0, "order": "id asc"}
```
Returns: `[id, id, ...]`

### `search_count`
```json
{"domain": [...]}
```
Returns: integer

### `search_read`
```json
{
  "domain": [...],
  "fields": ["name", "partner_id", "amount_total"],
  "limit": 100,
  "offset": 0,
  "order": "date_order desc"
}
```
Returns: `[{...}, {...}, ...]`. Many2one fields come back as `[id, "Display Name"]`
tuples; one2many/many2many as `[id, id, ...]` arrays.

### `read`
```json
{"ids": [1, 2, 3], "fields": ["name"]}
```
Returns: `[{...}, ...]`. Like `search_read` but with explicit ids.

**Batch your reads.** `ids` takes a list — one call for 100 records, not 100
calls for one record each (N+1 loops are the most common self-inflicted
rate-limit). If you're about to loop `read` per id, pass the whole id list
instead; if the ids come from a `search`, use `search_read` and skip the
second round-trip entirely. Always pass an explicit `fields` list — omitting
it fetches every field, including base64 binaries on models that have them
(see the attachments note below).

### `read_group` — the key method for analytics
```json
{
  "domain": [["state", "in", ["sale", "done"]]],
  "fields": ["amount_total:sum", "id:count"],
  "groupby": ["partner_id", "date_order:month"],
  "limit": 100,
  "orderby": "amount_total desc",
  "lazy": true
}
```
Returns aggregated rows. Aggregation syntax: `field:aggregate`. Supported
aggregates: `sum`, `avg`, `min`, `max`, `count`, `count_distinct`. Date/datetime
fields support granularity suffixes in `groupby`: `:day`, `:week`, `:month`,
`:quarter`, `:year`.

When `lazy: true` and you pass multiple `groupby` fields, only the first level
is expanded — you'll get a `__domain` in each row to drill down. Use `lazy: false`
for a flat full multi-level grouping.

Note the argument-name asymmetry: `read_group` sorts with `orderby`, while
`search`/`search_read` sort with `order`. Odoo 19 also exposes
`formatted_read_group` (`{"domain": [...], "aggregates": ["amount_total:sum"],
"groupby": ["state"]}`) — the native Odoo 19 aggregation entry point; both work
over JSON-2.

### `fields_get`
```json
{"allfields": ["name", "state"], "attributes": ["string", "type", "help"]}
```
Returns: `{"field_name": {"type": "char", "string": "Name", ...}, ...}`. Omit
`allfields` to get all fields. Useful attributes: `type`, `string`, `help`,
`required`, `readonly`, `store`, `relation` (for relational fields), `selection`
(for selection fields).

### `name_search`
```json
{"name": "Acme", "limit": 10, "operator": "ilike"}
```
Returns: `[[id, "Display Name"], ...]`. Best for autocomplete-style lookups.

## Attachments (context-window hazard)

`ir.attachment` stores file bodies in `datas` as base64. A `read` that
includes `datas` — or omits `fields` entirely — returns the whole file inline
in the JSON response (observed: 9 MB+ single responses). Never pull that into
an agent context. Use:

```
python -m scripts.get_attachment <profile> --id <id> --out <path>
```

which decodes to disk and prints only path + metadata. The CLI refuses
inline-binary reads of ir.attachment unless `--allow-inline-binary` is passed.

## Rate limits

Rate limiting depends on the Odoo host and any CDN/proxy in front of it. Burst
behaviour (hundreds of calls per second) will likely be throttled. Mitigations:

1. **Use `read_group` instead of read-and-aggregate-in-Python.** A single call
   often replaces dozens.
2. **Page large reads** with `limit` + `offset` (the cache_sync script does this
   automatically; default page size 1000).
3. **Cache** repeated analysis workloads — see `caching_strategy.md`.

## Transactions

Each JSON-2 call runs in its own PostgreSQL transaction on the server. This
matters more for writes than reads, but be aware: a read at time T may see data
that didn't exist when an earlier read at time T-1 ran. For point-in-time
consistency across multiple models, snapshot to the local cache.

## Pricing tier note

External API access (including JSON-2) is available on Odoo Custom plans only.
One App Free and Standard plans do not expose the API. If you get a generic
404 on `/json/2/...` paths, check the plan first.
