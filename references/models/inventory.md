# Inventory models

## product.template vs product.product

Two-tier product model. `product.template` is the "abstract" product;
`product.product` is the concrete variant. If a product has no variants, there's
still exactly one `product.product` row referencing the template.

Always read `product.product` for stock, sales, and purchase work. Read
`product.template` only for catalog-level questions.

### product.template — key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | Display name |
| `default_code` | char | Internal reference / SKU at template level |
| `barcode` | char | (Only useful when no variants) |
| `categ_id` | many2one(product.category) | Internal category |
| `type` | selection | `consu` (consumable), `service`, `combo` — in **Odoo 18+ `product` is replaced by `is_storable=True` on consu** |
| `is_storable` | boolean | True for tracked-stock products (replaces v17 `type='product'`) |
| `tracking` | selection | `none`, `serial`, `lot` |
| `list_price` | float | Sales price (template-level) |
| `standard_price` | float | Cost (read on variant for accurate value) |
| `uom_id` | many2one(uom.uom) | Stocking unit |
| `uom_po_id` | many2one(uom.uom) | Purchase unit |
| `active` | boolean | Archived products go False |
| `sale_ok` | boolean | Available for sale |
| `purchase_ok` | boolean | Available for purchase |

### product.product — key fields

Inherits everything from template via `product_tmpl_id`, plus:

| Field | Type | Notes |
|---|---|---|
| `product_tmpl_id` | many2one(product.template) | |
| `default_code` | char | Variant SKU (can differ from template) |
| `barcode` | char | |
| `product_template_attribute_value_ids` | many2many | The attribute values that define this variant |
| `qty_available` | float | Computed on-hand (real-time, expensive to compute in bulk) |
| `virtual_available` | float | Forecast (on-hand + incoming - outgoing) |
| `incoming_qty` | float | Confirmed incoming |
| `outgoing_qty` | float | Confirmed outgoing |
| `standard_price` | float | Cost |

**Performance note**: `qty_available` and `virtual_available` are non-stored
computed fields. Reading them on many products at once triggers a full
recomputation. For bulk inventory analysis, query `stock.quant` instead.

## stock.quant

Stock on hand. One row per (product, location, lot/serial, package, owner)
combination with non-zero quantity. **This is the source of truth for current
inventory.**

### Key fields

| Field | Type | Notes |
|---|---|---|
| `product_id` | many2one(product.product) | |
| `location_id` | many2one(stock.location) | Where it sits |
| `lot_id` | many2one(stock.lot) | If product is lot-tracked |
| `package_id` | many2one(stock.quant.package) | |
| `owner_id` | many2one(res.partner) | Consignment owner (if any) |
| `quantity` | float | Physical qty at this location |
| `reserved_quantity` | float | Reserved for outgoing pickings |
| `available_quantity` | float | `quantity - reserved_quantity` |
| `inventory_quantity` | float | Used during inventory counts |
| `value` | monetary | Inventory valuation (real-time-valued products only) |
| `in_date` | datetime | When stock arrived at this quant |
| `company_id` | many2one(res.company) | |

### Filtering to "real" stock

`stock.quant` includes all location types: internal, transit, customer/vendor
locations, scrap, production, etc. To get "stock we actually have", filter:

```python
domain=[["location_id.usage", "=", "internal"]]
```

### Common queries

```python
# Total on-hand by product (all internal locations)
client.read_group("stock.quant",
    domain=[["location_id.usage", "=", "internal"]],
    fields=["quantity:sum", "available_quantity:sum", "value:sum"],
    groupby=["product_id"],
    orderby="value desc")

# Stock by location
client.read_group("stock.quant",
    domain=[["product_id", "=", 1234], ["location_id.usage", "=", "internal"]],
    fields=["quantity:sum"],
    groupby=["location_id"])
```

## stock.move

A planned/executed inventory movement. The transaction log of stock flow.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | Description |
| `product_id` | many2one(product.product) | |
| `product_uom_qty` | float | Demand (initial planned qty) |
| `quantity` | float | Actual qty done (post Odoo 17) |
| `state` | selection | `draft`, `waiting`, `confirmed`, `assigned`, `done`, `cancel` |
| `location_id` | many2one(stock.location) | Source |
| `location_dest_id` | many2one(stock.location) | Destination |
| `date` | datetime | Scheduled or completed date |
| `date_deadline` | datetime | Hard deadline |
| `picking_id` | many2one(stock.picking) | Parent transfer (if any) |
| `origin` | char | Source document name |
| `reference` | char | |
| `partner_id` | many2one(res.partner) | |
| `company_id` | many2one(res.company) | |

### Common queries

```python
# Inbound vs outbound flow last 30 days (by product)
import datetime
since = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
client.read_group("stock.move",
    domain=[
        ["state", "=", "done"],
        ["date", ">=", since],
        ["location_id.usage", "!=", "internal"],
        ["location_dest_id.usage", "=", "internal"],  # incoming
    ],
    fields=["quantity:sum"],
    groupby=["product_id"])
```

## stock.picking

A transfer (receipt, delivery, internal). Groups one or more stock.move records.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | Reference (e.g. "WH/IN/00001") |
| `partner_id` | many2one(res.partner) | |
| `picking_type_id` | many2one(stock.picking.type) | Receipt/Delivery/Internal |
| `picking_type_code` | selection | `incoming`, `outgoing`, `internal` |
| `state` | selection | `draft`, `waiting`, `confirmed`, `assigned`, `done`, `cancel` |
| `scheduled_date` | datetime | |
| `date_done` | datetime | |
| `origin` | char | Source doc |
| `move_ids` | one2many(stock.move) | The moves in this transfer |
| `move_line_ids` | one2many(stock.move.line) | Actual operations (lot/serial/package detail) |
| `backorder_id` | many2one(stock.picking) | If this is a backorder |
| `priority` | selection | Urgency |
| `company_id` | many2one(res.company) | |

## stock.location

The warehouse-network topology.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | |
| `complete_name` | char | Full path ("WH/Stock/Shelf A") |
| `usage` | selection | `internal`, `customer`, `supplier`, `inventory`, `production`, `transit`, `view` |
| `location_id` | many2one(stock.location) | Parent (use `child_of` to recurse) |
| `warehouse_id` | many2one(stock.warehouse) | |
| `company_id` | many2one(res.company) | |

`usage` is the key filter:
- `internal` — physical stock you own
- `customer` / `supplier` — virtual locations representing partner-side balances
- `inventory` — adjustment-loss bucket
- `production` — for MRP
- `transit` — inter-company / inter-warehouse in-flight

## Gotchas

- **`type='product'` vs `is_storable`**: Odoo 18 removed the `product` value
  from `type`. Tracked-inventory items are now `type='consu'` with
  `is_storable=True`. If you're hitting a v19 instance, use `is_storable`.
- **Quants are negative in non-internal locations**: a `stock.quant` row for the
  customer location with quantity=-5 means "we've delivered 5 to customers".
  This is how Odoo balances the double-entry stock model. For "what's in our
  warehouse", always filter by `location_id.usage='internal'`.
- **Forecast vs on-hand**: `virtual_available` mixes confirmed-but-not-yet-done
  stock movements with on-hand. For a clean "what's physically on the shelf
  right now", use `stock.quant.quantity` summed.
- **Inventory valuation depends on costing method**: FIFO/AVCO products have
  `value` on quants; standard-cost products compute value as `quantity * cost`
  at read time and may not store it on quants.
