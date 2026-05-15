# Partners

## res.partner

The single contact model — customers, vendors, employees-as-contacts, internal
addresses, leads-as-companies, all live here. Discriminated by flags rather than
separate tables.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | |
| `display_name` | char | Computed; includes company prefix if a contact |
| `is_company` | boolean | True for organizations, False for individuals |
| `company_type` | selection | `person` or `company` (UI-friendly version of `is_company`) |
| `parent_id` | many2one(res.partner) | Company this contact belongs to |
| `child_ids` | one2many(res.partner) | Children (contacts/addresses of a company) |
| `type` | selection | `contact`, `invoice`, `delivery`, `private`, `other` |
| `email` | char | |
| `email_normalized` | char | Lowercased + trimmed (use for dedup) |
| `phone` | char | |
| `mobile` | char | |
| `website` | char | |
| `vat` | char | VAT/tax ID |
| `street` | char | |
| `street2` | char | |
| `city` | char | |
| `state_id` | many2one(res.country.state) | |
| `zip` | char | |
| `country_id` | many2one(res.country) | |
| `lang` | selection | Preferred language |
| `tz` | selection | Timezone |
| `category_id` | many2many(res.partner.category) | Tags |
| `customer_rank` | integer | >0 means "has been a customer" (computed) |
| `supplier_rank` | integer | >0 means "has been a vendor" (computed) |
| `active` | boolean | Archived = False |
| `company_id` | many2one(res.company) | Multi-company; can be False = shared |
| `user_id` | many2one(res.users) | Account manager (optional) |
| `team_id` | many2one(crm.team) | |
| `industry_id` | many2one(res.partner.industry) | |
| `property_payment_term_id` | many2one(account.payment.term) | Default payment term (companies) |
| `property_supplier_payment_term_id` | many2one | Default vendor payment term |
| `credit` | monetary | Computed: outstanding AR |
| `debit` | monetary | Computed: outstanding AP |

### `type` values

- `contact` — Main contact (person or company)
- `invoice` — Invoicing address
- `delivery` — Shipping address
- `private` — Private address (HR)
- `other` — Other address

### customer_rank / supplier_rank

These are computed indicators, not toggleable flags:
- `customer_rank > 0` → confirmed/done sales order exists, OR posted customer invoice
- `supplier_rank > 0` → confirmed PO exists, OR posted vendor bill

Use them as soft signals; don't expect them to be perfectly accurate after manual
data manipulation.

### Common queries

```python
# All active customer companies
client.search_read("res.partner",
    domain=[
        ["is_company", "=", True],
        ["customer_rank", ">", 0],
        ["active", "=", True],
    ],
    fields=["name","vat","country_id","email","phone","credit"])

# Contacts at a specific company
client.search_read("res.partner",
    domain=[["parent_id", "=", 42]],
    fields=["name","email","phone","function"])

# Customers by country (revenue)
client.read_group("sale.order",
    domain=[["state", "in", ["sale", "done"]],
            ["date_order", ">=", "2025-01-01"]],
    fields=["amount_total:sum"],
    groupby=["partner_id.country_id"])
```

## Gotchas

- **Company vs contact**: a contact has `parent_id` set to its company. To get
  "all companies plus contacts for a specific company", filter by `is_company`
  or by `parent_id`.
- **Multi-company partners**: a partner with `company_id=False` is shared across
  all companies. With a specific `company_id`, it's scoped.
- **Deduplication**: `email_normalized` is the safest dedup key for individuals.
  For companies, dedup on `vat` if available, otherwise `name + country_id`.
- **`type='contact'` does not exclude addresses**: a child partner with
  `type='delivery'` is a delivery address; `type='contact'` is the main child
  contact. Filter explicitly when needed.
- **Inactive partners**: archived contacts default to hidden. Pass
  `context={"active_test": False}` to include them.
