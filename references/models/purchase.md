# Purchase models

## purchase.order

A purchase order header. Mirrors `sale.order` on the buy side.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | PO number ("PO00001") |
| `partner_id` | many2one(res.partner) | Vendor |
| `partner_ref` | char | Vendor's reference |
| `user_id` | many2one(res.users) | Purchase agent |
| `date_order` | datetime | Confirmation/quote date |
| `date_approve` | datetime | Approval date |
| `date_planned` | datetime | Earliest expected receipt across lines |
| `state` | selection | See states below |
| `invoice_status` | selection | `no`, `to invoice`, `invoiced` |
| `amount_untaxed` | monetary | |
| `amount_tax` | monetary | |
| `amount_total` | monetary | |
| `currency_id` | many2one(res.currency) | |
| `payment_term_id` | many2one(account.payment.term) | |
| `order_line` | one2many(purchase.order.line) | Lines |
| `picking_ids` | one2many(stock.picking) | Receipts generated |
| `origin` | char | Source document |
| `company_id` | many2one(res.company) | |

### States

- `draft` — RFQ (not sent)
- `sent` — RFQ sent to vendor
- `to approve` — Awaiting approval (if approval workflow enabled)
- `purchase` — Confirmed purchase order
- `done` — Locked
- `cancel` — Cancelled

For confirmed spend, filter `state in ('purchase', 'done')`.

### Common queries

```python
# Open PO commitment (confirmed but not fully received/invoiced)
client.search_read("purchase.order",
    domain=[
        ["state", "in", ["purchase", "done"]],
        ["invoice_status", "!=", "invoiced"],
    ],
    fields=["name","partner_id","amount_total","date_planned","invoice_status"])

# Vendor spend YTD
client.read_group("purchase.order",
    domain=[
        ["state", "in", ["purchase", "done"]],
        ["date_order", ">=", "2025-01-01"],
    ],
    fields=["amount_total:sum", "id:count"],
    groupby=["partner_id"],
    orderby="amount_total desc", limit=50)
```

## purchase.order.line

PO line items. For product-level purchasing analytics.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `order_id` | many2one(purchase.order) | Parent |
| `product_id` | many2one(product.product) | |
| `name` | text | Description |
| `product_qty` | float | Ordered qty |
| `qty_received` | float | Received |
| `qty_invoiced` | float | Invoiced |
| `qty_to_invoice` | float | Pending invoicing |
| `price_unit` | monetary | Unit cost |
| `price_subtotal` | monetary | Line untaxed |
| `price_total` | monetary | Line with tax |
| `discount` | float | (Odoo 17+) |
| `taxes_id` | many2many(account.tax) | |
| `date_planned` | datetime | Line-level expected receipt |
| `product_uom` | many2one(uom.uom) | |
| `account_analytic_id` | many2one(account.analytic.account) | (legacy single account) |
| `analytic_distribution` | json | `{account_id: %}` (current) |
| `display_type` | selection | Filter `=False` for real product lines |

### Common queries

```python
# Top purchased products this year (by spend)
client.read_group("purchase.order.line",
    domain=[
        ["order_id.state", "in", ["purchase", "done"]],
        ["order_id.date_order", ">=", "2025-01-01"],
        ["display_type", "=", False],
    ],
    fields=["product_qty:sum","price_subtotal:sum"],
    groupby=["product_id"],
    orderby="price_subtotal desc", limit=50)
```

## product.supplierinfo (vendor pricelist)

The vendor catalog: who sells what, at what price, with what lead time.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `partner_id` | many2one(res.partner) | Vendor |
| `product_tmpl_id` | many2one(product.template) | Product |
| `product_id` | many2one(product.product) | Specific variant (optional) |
| `product_code` | char | Vendor's SKU |
| `product_name` | char | Vendor's product name |
| `price` | float | Cost from this vendor |
| `currency_id` | many2one(res.currency) | |
| `min_qty` | float | Minimum order qty |
| `delay` | integer | Lead time in days |
| `date_start` | date | Valid from |
| `date_end` | date | Valid to |
| `sequence` | integer | Vendor priority |
| `company_id` | many2one(res.company) | |

### Common queries

```python
# All vendors and prices for one product
client.search_read("product.supplierinfo",
    domain=[["product_tmpl_id", "=", template_id]],
    fields=["partner_id","price","currency_id","min_qty","delay","sequence"],
    order="sequence asc")
```

## Gotchas

- **Vendor bills vs POs**: A PO is a commitment; the related `account.move` records
  (`move_type='in_invoice'`) are the actual recorded liability. For AP analytics,
  use accounting models. For procurement/committed-spend analytics, use purchase models.
- **3-way match status**: `invoice_status` on the PO summarizes the 3-way match
  (PO → receipt → invoice). It's the easiest signal for "is this PO complete".
- **Multi-currency**: `price_unit` on a line is in the PO's `currency_id`. For
  cross-currency analytics, convert to company currency (`amount_total_signed`
  fields on the related `account.move` are pre-converted).
- **Receipt timing**: `qty_received` on the line updates only when the related
  receipt picking moves to `done`. Partial receipts are reflected.
