# Manufacturing models

## mrp.production

A manufacturing order (MO). Produces one product (with optional by-products).

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | Reference ("MO/00001") |
| `product_id` | many2one(product.product) | Product being manufactured |
| `product_qty` | float | Quantity to produce |
| `qty_produced` | float | Actually produced |
| `product_uom_id` | many2one(uom.uom) | |
| `bom_id` | many2one(mrp.bom) | The BoM used |
| `state` | selection | See states below |
| `date_start` | datetime | Planned start |
| `date_finished` | datetime | Planned finish |
| `date_deadline` | datetime | Customer/sales deadline |
| `picking_type_id` | many2one(stock.picking.type) | Manufacturing operation type |
| `location_src_id` | many2one(stock.location) | Component source |
| `location_dest_id` | many2one(stock.location) | Finished-goods destination |
| `move_raw_ids` | one2many(stock.move) | Component consumption |
| `move_finished_ids` | one2many(stock.move) | Finished-goods production |
| `workorder_ids` | one2many(mrp.workorder) | Operations |
| `origin` | char | Source doc (SO, replenishment, etc.) |
| `priority` | selection | |
| `company_id` | many2one(res.company) | |

### States

- `draft` — Not confirmed
- `confirmed` — Confirmed, components reserved as available
- `progress` — Work has started
- `to_close` — Components fully consumed, awaiting finish
- `done` — Completed
- `cancel` — Cancelled

For "open demand", filter `state in ('confirmed', 'progress', 'to_close')`.

### Common queries

```python
# Open manufacturing demand by product
client.read_group("mrp.production",
    domain=[["state", "in", ["confirmed", "progress", "to_close"]]],
    fields=["product_qty:sum"],
    groupby=["product_id"],
    orderby="product_qty desc")

# MOs scheduled this week
import datetime
today = datetime.date.today()
week_end = (today + datetime.timedelta(days=7)).isoformat()
client.search_read("mrp.production",
    domain=[
        ["state", "in", ["confirmed", "progress"]],
        ["date_start", ">=", today.isoformat()],
        ["date_start", "<", week_end],
    ],
    fields=["name","product_id","product_qty","date_start","date_deadline"])
```

## mrp.bom

A bill of materials. Defines what components produce what finished product.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `code` | char | Reference |
| `product_tmpl_id` | many2one(product.template) | Finished product template |
| `product_id` | many2one(product.product) | Specific variant (optional) |
| `product_qty` | float | Output quantity per BoM run |
| `product_uom_id` | many2one(uom.uom) | |
| `type` | selection | `normal` (manufacture), `phantom` (kit), `subcontract` |
| `bom_line_ids` | one2many(mrp.bom.line) | Components |
| `operation_ids` | one2many(mrp.routing.workcenter) | Operations |
| `byproduct_ids` | one2many(mrp.bom.byproduct) | |
| `version` | integer | (Odoo 17+) |
| `active` | boolean | |
| `company_id` | many2one(res.company) | |

## mrp.bom.line

A single component on a BoM.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `bom_id` | many2one(mrp.bom) | Parent BoM |
| `product_id` | many2one(product.product) | Component |
| `product_qty` | float | Qty needed per BoM `product_qty` units of output |
| `product_uom_id` | many2one(uom.uom) | |
| `operation_id` | many2one(mrp.routing.workcenter) | Consumed at this operation |
| `bom_product_template_attribute_value_ids` | many2many | Variant-specific component |

### "How many of component X do we need per finished unit Y"

```python
bom = client.search_read("mrp.bom",
    [["product_tmpl_id", "=", finished_template_id], ["active", "=", True]],
    fields=["id", "product_qty"], limit=1)
lines = client.search_read("mrp.bom.line",
    [["bom_id", "=", bom[0]["id"]]],
    fields=["product_id", "product_qty"])
# qty_per_unit = line.product_qty / bom.product_qty
```

## mrp.workorder

Operations within a manufacturing order. Only present if the BoM defines routing.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | Operation name |
| `production_id` | many2one(mrp.production) | Parent MO |
| `workcenter_id` | many2one(mrp.workcenter) | |
| `operation_id` | many2one(mrp.routing.workcenter) | The routing operation |
| `state` | selection | `pending`, `waiting`, `ready`, `progress`, `done`, `cancel` |
| `date_start` | datetime | Actual start |
| `date_finished` | datetime | Actual finish |
| `duration_expected` | float | Planned minutes |
| `duration` | float | Actual minutes |
| `qty_produced` | float | |
| `qty_remaining` | float | |

### Workcenter load (capacity planning)

```python
client.read_group("mrp.workorder",
    domain=[
        ["state", "in", ["pending", "waiting", "ready", "progress"]],
        ["date_start", ">=", "2025-04-01"],
    ],
    fields=["duration_expected:sum"],
    groupby=["workcenter_id", "date_start:week"],
    lazy=False)
```

## stock.warehouse.orderpoint

Reordering rules (min/max stock levels). Useful for "what do we need to buy/make
to avoid stockout".

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | |
| `product_id` | many2one(product.product) | |
| `location_id` | many2one(stock.location) | |
| `product_min_qty` | float | Trigger threshold |
| `product_max_qty` | float | Target after replenishment |
| `qty_multiple` | float | Round-up multiple |
| `qty_to_order` | float | Computed: what should be ordered now |
| `route_id` | many2one(stock.route) | How to replenish (buy/manufacture/etc.) |
| `trigger` | selection | `auto`, `manual` |
| `company_id` | many2one(res.company) | |

## Common analytics

### "What do I need to manufacture in the next month to meet demand?"

There's no single field. The pattern is:

1. Aggregate open `mrp.production` demand by product (this gives planned MOs).
2. Aggregate confirmed `sale.order.line` quantities (forecast demand).
3. Compare to on-hand from `stock.quant`.

This is one of the cases where the local cache pays off — three reads, then a SQL
join. See `analytics_patterns.md` for the SQL pattern.

### Component shortage forecast

For each open MO, the components it needs come from `move_raw_ids` (which are
`stock.move` records). If any of those moves are in state `waiting` or
`confirmed` (not yet `assigned`), the components aren't available. So:

```python
short = client.search_read("stock.move",
    domain=[
        ["raw_material_production_id", "!=", False],  # belongs to an MO
        ["state", "in", ["waiting", "confirmed"]],
        ["raw_material_production_id.state", "in", ["confirmed", "progress"]],
    ],
    fields=["product_id", "product_uom_qty", "raw_material_production_id"])
```

## Gotchas

- **MO state `done` does not mean "shipped"** — it means the finished product
  moved to the stock location. Whether it then ships depends on a separate
  `stock.picking`.
- **Multi-level BoMs**: components can themselves be manufactured products. To
  compute total raw-material demand for a finished unit, recurse through BoMs.
- **Phantom BoMs (kits)**: don't create MOs — they're exploded on the sales/picking
  side. Analytics on "what we manufacture" should usually exclude `type='phantom'`.
- **`qty_produced` vs `product_qty`**: production may overshoot or undershoot
  planned qty. For "actual output" use `qty_produced`; for "planned" use `product_qty`.
