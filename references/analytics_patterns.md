# Analytics patterns

Recipes for common questions. All examples use `read_group` because it pushes
aggregation to the server — orders of magnitude faster than `search_read` + Python sums.

## Revenue by month

```python
client.read_group(
    "sale.order",
    domain=[["state", "in", ["sale", "done"]],
            ["date_order", ">=", "2025-01-01"]],
    fields=["amount_total:sum", "id:count"],
    groupby=["date_order:month"],
    orderby="date_order asc",
)
```
Returns rows like `{"date_order:month": "January 2025", "amount_total": 48210.50, "id_count": 17, "__domain": [...]}`.

## Top N customers by revenue (this year)

```python
client.read_group(
    "sale.order",
    domain=[["state", "in", ["sale", "done"]],
            ["date_order", ">=", "2025-01-01"]],
    fields=["amount_total:sum", "id:count"],
    groupby=["partner_id"],
    orderby="amount_total desc",
    limit=10,
)
```

## Revenue by salesperson × month

Two-level groupby. Use `lazy: false` to get a flat result with all combinations:

```python
client.read_group(
    "sale.order",
    domain=[["state", "in", ["sale", "done"]]],
    fields=["amount_total:sum"],
    groupby=["user_id", "date_order:month"],
    lazy=False,
)
```

## AR aging (open customer invoices bucketed by days overdue)

There's no built-in "aging bucket" field — compute buckets client-side after
pulling open invoices. The cheapest fetch:

```python
import datetime
today = datetime.date.today().isoformat()
rows = client.search_read(
    "account.move",
    domain=[
        ["move_type", "=", "out_invoice"],         # customer invoices
        ["state", "=", "posted"],
        ["payment_state", "in", ["not_paid", "partial"]],
    ],
    fields=["name", "partner_id", "invoice_date_due",
            "amount_total", "amount_residual", "currency_id"],
)
# Bucket in Python: 0–30, 31–60, 61–90, 90+
```

For totals only, use `read_group` on `account.move.line` filtered by the
receivable account, grouped by `partner_id`.

## Inventory on hand by product

```python
client.read_group(
    "stock.quant",
    domain=[["location_id.usage", "=", "internal"]],
    fields=["quantity:sum"],
    groupby=["product_id"],
    orderby="quantity desc",
    limit=200,
)
```
For value, also pull `value:sum` (Odoo stores the inventory valuation here for
real-time-valued products).

## Sales velocity per product (units sold last 90 days)

```python
import datetime
since = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
client.read_group(
    "sale.order.line",
    domain=[
        ["order_id.state", "in", ["sale", "done"]],
        ["order_id.date_order", ">=", since],
    ],
    fields=["product_uom_qty:sum", "price_subtotal:sum"],
    groupby=["product_id"],
    orderby="product_uom_qty desc",
    limit=100,
)
```

## Manufacturing demand (open MO quantities by product)

```python
client.read_group(
    "mrp.production",
    domain=[["state", "in", ["confirmed", "progress", "to_close"]]],
    fields=["product_qty:sum"],
    groupby=["product_id"],
    orderby="product_qty desc",
)
```

To also compare against on-hand stock, pull `stock.quant` aggregated by product
in the same way and join in pandas/SQL.

## Purchase spend by vendor (last 12 months)

```python
import datetime
since = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
client.read_group(
    "purchase.order",
    domain=[["state", "in", ["purchase", "done"]],
            ["date_order", ">=", since]],
    fields=["amount_total:sum", "id:count"],
    groupby=["partner_id"],
    orderby="amount_total desc",
    limit=50,
)
```

## CRM pipeline by stage

```python
client.read_group(
    "crm.lead",
    domain=[["type", "=", "opportunity"], ["active", "=", True]],
    fields=["expected_revenue:sum", "id:count"],
    groupby=["stage_id"],
    orderby="stage_id asc",
)
```

## P&L-style snapshot from account.move.line

Pull journal lines grouped by account, filtered to a date range and posted state.
Income accounts will have credit balances, expense accounts debit balances.

```python
client.read_group(
    "account.move.line",
    domain=[
        ["parent_state", "=", "posted"],
        ["date", ">=", "2025-01-01"],
        ["date", "<=", "2025-03-31"],
        ["account_id.account_type", "in",
            ["income", "income_other", "expense", "expense_depreciation", "expense_direct_cost"]],
    ],
    fields=["balance:sum", "debit:sum", "credit:sum"],
    groupby=["account_id", "account_id.account_type"],
    lazy=False,
)
```

## Patterns to avoid

**Search-in-loop.** Don't do this:
```python
# BAD: one API call per partner
for partner_id in partner_ids:
    orders = client.search_read("sale.order", [["partner_id","=",partner_id]], ...)
```
Replace with a single `read_group` grouped by `partner_id`, or one `search_read`
with `["partner_id", "in", partner_ids]`.

**Pulling all records to count.** Use `search_count`, not `len(search(...))`.

**Reading every field.** Always pass `fields=[...]` with what you actually need.
The default is "all fields the user can read", which on rich models like
`sale.order` can be 100+ columns including expensive computed ones.

## When to drop to the local cache

If you're doing more than two queries against the same dataset, or need a join
that `read_group` can't express (e.g. "products with sales_velocity > 10 AND
stock_on_hand < 5"), it's faster to sync those models into DuckDB and use SQL:

```bash
python -m scripts.cache_sync <profile> \
    --models sale.order.line,stock.quant,product.product \
    --backend duckdb --db ./cache.duckdb
```

Then:
```sql
WITH velocity AS (
  SELECT product_id, SUM(product_uom_qty) AS qty_90d
  FROM "sale.order.line" sol
  JOIN "sale.order" so ON so.id = sol.order_id
  WHERE so.state IN ('sale', 'done')
    AND so.date_order >= CURRENT_DATE - INTERVAL '90 days'
  GROUP BY product_id
),
on_hand AS (
  SELECT product_id, SUM(quantity) AS qty
  FROM "stock.quant" q
  WHERE q.location_id IN (SELECT id FROM "stock.location" WHERE usage='internal')
  GROUP BY product_id
)
SELECT p.id, p.name, v.qty_90d, h.qty
FROM "product.product" p
JOIN velocity v ON v.product_id = p.id
LEFT JOIN on_hand h ON h.product_id = p.id
WHERE v.qty_90d > 10 AND COALESCE(h.qty, 0) < 5
ORDER BY v.qty_90d DESC;
```
