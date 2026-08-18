# Caching strategy

The cache layer mirrors Odoo data into a local DuckDB or SQLite database. Use it
when the workload is too heavy for one-shot API calls or needs cross-model joins.

## When to cache (vs query live)

| Situation | Live | Cache |
|---|---|---|
| "What's the total of this month's sales?" | ✓ | |
| "Compare last 3 months revenue across 50 customers" | ✓ | |
| "Build a dashboard refreshed daily" | | ✓ |
| "Find products with high velocity AND low stock" (cross-model) | | ✓ |
| Repeated analysis on the same dataset in one session | | ✓ |
| One-shot exploratory question | ✓ | |
| > 5000 rows expected and you'll iterate on the query | | ✓ |

The cache is not a real-time mirror — it's a snapshot at sync time. For
financial close, period-end reporting, etc., that's exactly what you want.

## DuckDB vs SQLite

Both backends produce the same schema. Pick by use case:

- **DuckDB** — default for analytics. Columnar storage, vectorized aggregations,
  excellent pandas integration (`duckdb.query(...).to_df()`), supports
  window functions and complex joins fast. Best for dashboards, ad-hoc analysis,
  ML feature extraction.
- **SQLite** — pick for portability (e.g. ship the file somewhere, embed in an
  app), tiny footprints, or environments where DuckDB isn't installed. Slower
  on big aggregations but adequate for moderate datasets.

DuckDB requires `pip install duckdb`. SQLite is in the Python stdlib.

## Schema design

One table per Odoo model. Table name = model name **including dots**, double-
quoted in SQL (so `sale.order`, not `sale_order`).

Field types map as:
- `integer`, `many2one`, `boolean` (as 0/1) → `INTEGER`
- `float`, `monetary` → `REAL`
- `char`, `text`, `html`, `selection`, `date`, `datetime`, `reference` → `TEXT`
- `one2many`, `many2many` → `TEXT` containing a JSON array of related ids
- `binary` → skipped by default (use `--include-binary` to override; expect bloat)

`id` is the primary key. Many2one fields are stored as the bare integer id, not
the `[id, name]` tuple Odoo's API returns — display names are not cached. To
join across models, join on those ids.

## Incremental sync

The sync script tracks a per-model `last_write_date` in the `_sync_state` table.
With `--incremental`, only records with `write_date >= last_write_date` are
fetched (`>=`, not `>` — records modified later within the boundary second
would otherwise be missed; the upsert makes re-fetching boundary rows
idempotent). Every run — including one that finds no changes — refreshes the
model's `synced_at`, so `cache_status` reflects when freshness was last
verified, not just when data last changed.

This works because every Odoo record has automatic `create_date` and `write_date`
timestamps. Records are upserted by `id`.

Caveats:
- **Deletions are not propagated.** If a record is deleted in Odoo, it stays in
  the cache — and a full (non-incremental) re-run does NOT purge it either,
  because the sync only ever upserts. For models where this matters, drop the
  table (or the whole cache DB) and re-sync from scratch.
- **Archived records**: if the API user can't see archived records, they won't
  come through. The cache will retain the last-seen version.
- **Schema drift**: if Odoo adds fields between syncs, the script ALTERs the
  table to add the new columns. Removed fields are left in the table (NULLs).

## Cross-model joins

Because the cache stores raw ids for many2one fields, joins are straightforward:

```sql
-- Customer name on every sale order
SELECT so.name AS order_name, so.date_order, p.name AS customer
FROM "sale.order" so
LEFT JOIN "res.partner" p ON p.id = so.partner_id
WHERE so.state IN ('sale', 'done');
```

For one2many/many2many (stored as JSON arrays):

DuckDB:
```sql
-- Sale orders and the products on each line
SELECT so.id AS order_id, sol.product_id, sol.product_uom_qty
FROM "sale.order" so
JOIN "sale.order.line" sol ON sol.order_id = so.id;
```
Don't try to query the JSON-array columns directly — they're for round-tripping
the relation, not querying. Use the join model (sale.order.line) instead.

## Suggested sync sets

Typical model bundles for common workloads:

### Sales analytics
```
sale.order, sale.order.line, res.partner, product.product, product.template,
res.users
```

### AR / AP / accounting
```
account.move, account.move.line, account.account, account.journal, res.partner,
res.currency
```

### Inventory & manufacturing
```
stock.move, stock.quant, stock.location, stock.picking, product.product,
product.template, mrp.production, mrp.bom, mrp.bom.line
```

### CRM pipeline
```
crm.lead, crm.stage, crm.team, res.partner, res.users
```

### Purchasing
```
purchase.order, purchase.order.line, res.partner, product.product, account.move
```

## Refresh cadence

Daily incremental sync is usually enough for analytics. For near-real-time
dashboards, every 15–30 minutes is fine — Odoo's `write_date` filtering is cheap
and most models don't churn that fast. Avoid sub-minute polling; it'll burn rate
limit budget for marginal data freshness.

## Operating notes

- The local DB file is just a file — back it up, version it, or `.gitignore` it
  as appropriate.
- DuckDB allows multiple readers but only one writer at a time. Don't run two
  `cache_sync` processes against the same file.
- DuckDB also reads CSV/Parquet directly, so once data is cached you can do
  things like `COPY "sale.order" TO 'sales.parquet'` for long-term archival.
