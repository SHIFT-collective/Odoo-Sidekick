# Sales models

## sale.order

The sales order header. One row per quotation/order.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | Order number ("S00001"). Sequenced. |
| `partner_id` | many2one(res.partner) | Customer |
| `partner_invoice_id` | many2one | Invoice address |
| `partner_shipping_id` | many2one | Delivery address |
| `user_id` | many2one(res.users) | Salesperson |
| `team_id` | many2one(crm.team) | Sales team |
| `date_order` | datetime | Order date (or quote date) |
| `validity_date` | date | Quotation expiry |
| `state` | selection | See states below |
| `invoice_status` | selection | `upselling`, `invoiced`, `to invoice`, `no` |
| `amount_untaxed` | monetary | Subtotal |
| `amount_tax` | monetary | Tax total |
| `amount_total` | monetary | Grand total |
| `currency_id` | many2one(res.currency) | |
| `pricelist_id` | many2one(product.pricelist) | |
| `payment_term_id` | many2one(account.payment.term) | |
| `order_line` | one2many(sale.order.line) | Lines |
| `client_order_ref` | char | Customer's PO ref |
| `origin` | char | Source document |
| `company_id` | many2one(res.company) | Multi-company filter |
| `create_date` | datetime | When record was created |
| `write_date` | datetime | Last modification (use for incremental sync) |

### States

- `draft` — Quotation (not yet sent)
- `sent` — Quotation sent
- `sale` — Sales order confirmed
- `done` — Locked (post-confirmation, no edits)
- `cancel` — Cancelled

For "real" revenue queries, filter `state in ('sale', 'done')`. Including `draft`/`sent`
gives the pipeline view.

### Common queries

```python
# This year's confirmed revenue
client.search_read("sale.order",
    domain=[["state","in",["sale","done"]], ["date_order",">=","2025-01-01"]],
    fields=["name","partner_id","amount_total","date_order","user_id"])

# Quotations in the pipeline (not yet won/lost)
client.search_read("sale.order",
    domain=[["state","in",["draft","sent"]]],
    fields=["name","partner_id","amount_total","validity_date"])
```

## sale.order.line

Individual line items. Most useful for product-level analytics.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `order_id` | many2one(sale.order) | Parent order |
| `product_id` | many2one(product.product) | Product (variant) |
| `product_template_id` | many2one(product.template) | Variant's template |
| `name` | text | Line description |
| `product_uom_qty` | float | Ordered quantity |
| `qty_delivered` | float | Delivered |
| `qty_invoiced` | float | Invoiced |
| `qty_to_invoice` | float | Pending invoice |
| `price_unit` | monetary | Unit price |
| `discount` | float | Discount % |
| `price_subtotal` | monetary | Line subtotal (untaxed) |
| `price_total` | monetary | Line total (with tax) |
| `tax_id` | many2many(account.tax) | |
| `display_type` | selection | `''` (real line), `line_section`, `line_note` — filter these out for analytics |

### Common queries

```python
# Top products by quantity sold this year
client.read_group("sale.order.line",
    domain=[
        ["order_id.state","in",["sale","done"]],
        ["order_id.date_order",">=","2025-01-01"],
        ["display_type","=",False],   # exclude section/note lines
    ],
    fields=["product_uom_qty:sum","price_subtotal:sum"],
    groupby=["product_id"],
    orderby="product_uom_qty desc", limit=50)
```

## crm.lead

Leads and opportunities. Same model; `type` distinguishes them.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | Title |
| `type` | selection | `lead` or `opportunity` |
| `partner_id` | many2one(res.partner) | Linked contact (optional for leads) |
| `partner_name` | char | Free-form company name (when no partner) |
| `contact_name` | char | Free-form contact name |
| `email_from` | char | |
| `phone` | char | |
| `stage_id` | many2one(crm.stage) | Pipeline stage |
| `team_id` | many2one(crm.team) | Sales team |
| `user_id` | many2one(res.users) | Salesperson |
| `expected_revenue` | monetary | Forecast |
| `probability` | float | Win probability % |
| `recurring_revenue` | monetary | MRR/ARR if applicable |
| `date_deadline` | date | Expected close |
| `date_closed` | datetime | When won/lost (filter to find recent wins) |
| `lost_reason_id` | many2one(crm.lost.reason) | Set when lost |
| `active` | boolean | Lost opportunities go `active=False` |
| `won_status` | selection | `won`, `lost`, `pending` (Odoo 17+) |
| `tag_ids` | many2many(crm.tag) | |
| `source_id` | many2one(utm.source) | Marketing source |
| `medium_id` | many2one(utm.medium) | |
| `campaign_id` | many2one(utm.campaign) | |

### Common queries

```python
# Active pipeline value by stage
client.read_group("crm.lead",
    domain=[["type","=","opportunity"], ["active","=",True]],
    fields=["expected_revenue:sum","id:count"],
    groupby=["stage_id"])

# Recent wins
client.search_read("crm.lead",
    domain=[["type","=","opportunity"], ["won_status","=","won"],
            ["date_closed",">=","2025-01-01"]],
    fields=["name","partner_id","expected_revenue","date_closed","user_id"])
```

## Gotchas

- A quotation in `state=sale` is the confirmed sales order. `state=done` only
  happens when the user explicitly locks it; many Odoo deployments never lock.
  For "all confirmed sales", use `state in ('sale', 'done')`.
- `amount_total` includes tax. Use `amount_untaxed` for revenue analysis if you
  want apples-to-apples across tax regimes.
- `date_order` is a datetime, not a date. Group with `:day` or `:month`.
- Cancelled orders still exist in the DB. Default analytics queries should
  exclude `state=cancel`.
- Lost opportunities are archived (`active=False`). Default queries hide them
  unless you opt in via `context={"active_test": False}` or filter on `active`.
