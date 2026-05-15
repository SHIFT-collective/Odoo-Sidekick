# Odoo domain syntax

A "domain" is Odoo's filter language. It's a Python/JSON list of clauses combined
with logical operators in **prefix notation**. Mastering it is the difference
between a query that returns the right data on the first try and one that doesn't.

## The basic clause

A clause is a 3-element list: `[field_name, operator, value]`.

```python
["state", "=", "sale"]                              # exact match
["amount_total", ">", 1000]                          # numeric comparison
["name", "ilike", "acme"]                            # case-insensitive substring
["date_order", ">=", "2025-01-01"]                   # ISO date string
["partner_id", "in", [42, 43, 44]]                   # one of these ids
["partner_id.country_id.code", "=", "ES"]            # dot-walk through many2one
```

## Operators

| Operator | Meaning |
|---|---|
| `=` `!=` | exact match / not |
| `>` `<` `>=` `<=` | numeric / lexicographic / date ordering |
| `in` `not in` | value is one of a list |
| `like` `not like` | SQL LIKE, case-sensitive, `%` wildcards |
| `ilike` `not ilike` | case-insensitive substring (most common for text search) |
| `=like` `=ilike` | LIKE without auto-wrapping in `%...%` (you control wildcards) |
| `child_of` | descendants in a hierarchy (works on parent_id-style trees) |
| `parent_of` | ancestors in a hierarchy |
| `=?` | falsy-or-equal (matches False/None or the given value) |

## Logical operators (prefix!)

Place operators **before** their operands, and remember that AND is implicit when
clauses are simply listed.

```python
# Implicit AND (most common)
[["state", "=", "sale"], ["amount_total", ">", 1000]]
# == "state = sale AND amount_total > 1000"

# Explicit OR
["|", ["state", "=", "sale"], ["state", "=", "done"]]
# == "state = sale OR state = done"  (or use ["state", "in", ["sale", "done"]])

# NOT
["!", ["state", "=", "cancel"]]

# Combining: AND of (status filter) and (OR of two date ranges)
[
  ["state", "in", ["sale", "done"]],
  "|",
    ["date_order", ">=", "2025-01-01"],
    ["date_order", "<=", "2024-01-01"],
]
```

The trick with prefix operators: `|` and `&` consume the **next two** clauses.
For nested logic you stack them:

```python
# (a AND b) OR (c AND d)
[
  "|",
    "&", ["a","=",1], ["b","=",2],
    "&", ["c","=",3], ["d","=",4],
]
```

Empty domain `[]` matches everything.

## Dates and datetimes

Use ISO strings. Datetimes are interpreted as **UTC unless the user's context
sets tz**. The default_context in your profile (e.g. `tz: Europe/Madrid`) helps
keep this consistent.

```python
# All sales orders in Q1 2025, Europe/Madrid time
["date_order", ">=", "2025-01-01 00:00:00"],
["date_order", "<",  "2025-04-01 00:00:00"],
```

For "this month / last quarter" relative ranges, compute the boundaries in
Python and pass concrete ISO strings — domains don't support relative dates
natively. Odoo's UI uses `context_today()`/`relativedelta` server-side, which
isn't reachable from JSON-2.

## Dot-walking through relations

Many2one fields can be traversed with dots:

```python
["partner_id.country_id.code", "=", "ES"]      # SOs for Spanish customers
["product_id.categ_id.name", "ilike", "Service"]
["user_id.partner_id.email", "=", "alice@…"]
```

For one2many/many2many, you generally filter on the **child model directly**
rather than the parent. E.g., to find orders containing a specific product,
either query `sale.order.line` and aggregate, or use:

```python
["order_line.product_id", "=", 1234]
```

This works because Odoo translates it to a JOIN behind the scenes.

## Common pitfalls

- **`False` vs missing**: Odoo serializes "no value" as `false` (not `null`).
  Filter for "has a value" with `["field", "!=", False]`.
- **Many2one in `=`**: pass the id, not the display name. `["partner_id", "=", 42]`.
  Use `ilike` on `partner_id.name` for name matching.
- **Booleans**: use real booleans, not strings. `["active", "=", false]`.
- **Selection fields**: use the technical value, not the label. Check `selection`
  on `fields_get` to see the valid values.
- **Multi-company**: queries inherit the API user's company. Records belonging
  to other companies are filtered out by record rules. If results look short,
  the API user might be company-scoped.
- **Archived records**: by default Odoo filters out `active=False` records via
  the `active` field. To include them, add `["active", "in", [true, false]]` or
  pass `context: {"active_test": false}`.
- **String comparison on numerics**: `["amount", ">", "100"]` may work but
  `["amount", ">", 100]` is safer — Odoo casts the column type from the schema.

## Quick reference for read_group date granularity

In `groupby`, append `:day`, `:week`, `:month`, `:quarter`, or `:year` to a
date or datetime field:

```python
{
  "groupby": ["date_order:month"],
  "fields": ["amount_total:sum"],
  "domain": [["state", "in", ["sale", "done"]]]
}
```

Each row's groupby value comes back as a human-readable label like `"March 2025"`
(localized to the context's `lang`).
