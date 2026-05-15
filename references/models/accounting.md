# Accounting models

## account.move

A journal entry. This is the single model for invoices, bills, credit notes,
refunds, payments-as-misc-entries, and pure miscellaneous journal entries. The
`move_type` field discriminates.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | Reference (e.g. "INV/2025/0001") |
| `move_type` | selection | See types below |
| `state` | selection | `draft`, `posted`, `cancel` |
| `payment_state` | selection | `not_paid`, `in_payment`, `paid`, `partial`, `reversed`, `invoicing_legacy` |
| `partner_id` | many2one(res.partner) | Customer or vendor |
| `partner_bank_id` | many2one(res.partner.bank) | |
| `date` | date | Accounting date (drives reporting period) |
| `invoice_date` | date | Invoice issue date (customer/vendor docs) |
| `invoice_date_due` | date | Due date |
| `journal_id` | many2one(account.journal) | |
| `currency_id` | many2one(res.currency) | |
| `amount_untaxed` | monetary | |
| `amount_tax` | monetary | |
| `amount_total` | monetary | Grand total |
| `amount_residual` | monetary | Open balance (for AR/AP aging) |
| `amount_total_signed` | monetary | Signed by move_type (in company currency) |
| `amount_residual_signed` | monetary | Signed residual in company currency |
| `invoice_line_ids` | one2many(account.move.line) | Just the product lines |
| `line_ids` | one2many(account.move.line) | All lines including tax/receivable counter-entries |
| `ref` | char | Free-text reference |
| `narration` | text | Internal notes |
| `invoice_origin` | char | Source document (e.g. linked SO name) |
| `invoice_user_id` | many2one(res.users) | Salesperson (invoices) |
| `company_id` | many2one(res.company) | |

### move_type values

| Value | Meaning |
|---|---|
| `entry` | Pure miscellaneous journal entry |
| `out_invoice` | Customer invoice |
| `out_refund` | Customer credit note |
| `in_invoice` | Vendor bill |
| `in_refund` | Vendor refund/credit |
| `out_receipt` | Customer receipt (POS-style) |
| `in_receipt` | Purchase receipt |

For AR work, filter `move_type in ('out_invoice', 'out_refund')`. AP work:
`move_type in ('in_invoice', 'in_refund')`.

### Common queries

```python
# Open AR (unpaid customer invoices)
client.search_read("account.move",
    domain=[
        ["move_type", "=", "out_invoice"],
        ["state", "=", "posted"],
        ["payment_state", "in", ["not_paid", "partial"]],
    ],
    fields=["name","partner_id","invoice_date_due","amount_total","amount_residual"])

# Revenue by month (posted customer invoices, signed amount)
client.read_group("account.move",
    domain=[
        ["move_type", "in", ["out_invoice", "out_refund"]],
        ["state", "=", "posted"],
        ["date", ">=", "2025-01-01"],
    ],
    fields=["amount_untaxed_signed:sum"],
    groupby=["date:month"])
```

## account.move.line

Individual journal lines. **This is the analytics workhorse** for accounting —
P&L, balance sheet, GL by account, all start here.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `move_id` | many2one(account.move) | Parent move |
| `move_name` | char | Inherited from move for convenience |
| `account_id` | many2one(account.account) | Chart of accounts entry |
| `journal_id` | many2one(account.journal) | |
| `partner_id` | many2one(res.partner) | |
| `date` | date | Inherited from move |
| `date_maturity` | date | Due date for AR/AP lines |
| `name` | char | Line description |
| `ref` | char | |
| `debit` | monetary | Always ≥ 0 |
| `credit` | monetary | Always ≥ 0 |
| `balance` | monetary | `debit - credit`, signed |
| `amount_currency` | monetary | Line amount in `currency_id` |
| `currency_id` | many2one(res.currency) | |
| `product_id` | many2one(product.product) | On invoice product lines |
| `quantity` | float | |
| `price_unit` | float | |
| `tax_ids` | many2many(account.tax) | Taxes applied |
| `tax_line_id` | many2one(account.tax) | Set on the tax line itself |
| `analytic_distribution` | json | `{analytic_account_id: percentage}` |
| `parent_state` | selection | The move's state — handy in domains |
| `reconciled` | boolean | True when fully reconciled |
| `full_reconcile_id` | many2one(account.full.reconcile) | Reconciliation group |
| `matching_number` | char | Reconciliation marker |

### Why parent_state matters

Every move line has a `parent_state` field that inherits from `move_id.state`.
Use this in domain filters to limit to posted entries without joining:

```python
domain=[["parent_state", "=", "posted"]]
```

### Common queries

```python
# Trial balance for an account range, current year
client.read_group("account.move.line",
    domain=[
        ["parent_state", "=", "posted"],
        ["date", ">=", "2025-01-01"],
        ["date", "<=", "2025-12-31"],
        ["account_id.code", ">=", "4000"],
        ["account_id.code", "<", "5000"],
    ],
    fields=["debit:sum", "credit:sum", "balance:sum"],
    groupby=["account_id"])

# Customer ledger for one partner
client.search_read("account.move.line",
    domain=[
        ["parent_state", "=", "posted"],
        ["partner_id", "=", 42],
        ["account_id.account_type", "=", "asset_receivable"],
    ],
    fields=["date","move_name","name","debit","credit","reconciled","date_maturity"],
    order="date asc, id asc")
```

## account.account

Chart of accounts.

### Key fields

| Field | Type | Notes |
|---|---|---|
| `code` | char | Account code (e.g. "411000") |
| `name` | char | |
| `account_type` | selection | Categorization (see below) |
| `reconcile` | boolean | Whether this account is reconciled |
| `currency_id` | many2one(res.currency) | Secondary currency |
| `tag_ids` | many2many(account.account.tag) | Reporting tags |
| `company_id` | many2one(res.company) | |

### account_type values (common)

`asset_receivable`, `asset_cash`, `asset_current`, `asset_non_current`,
`asset_prepayments`, `asset_fixed`,
`liability_payable`, `liability_credit_card`, `liability_current`,
`liability_non_current`,
`equity`, `equity_unaffected`,
`income`, `income_other`,
`expense`, `expense_depreciation`, `expense_direct_cost`,
`off_balance`.

P&L = `income*` + `expense*`. Balance sheet = `asset*` + `liability*` + `equity*`.

## account.journal

A journal (Sales, Purchases, Bank, Cash, Misc).

### Key fields

| Field | Type | Notes |
|---|---|---|
| `name` | char | |
| `code` | char | |
| `type` | selection | `sale`, `purchase`, `bank`, `cash`, `general` |
| `bank_account_id` | many2one(res.partner.bank) | For bank journals |
| `default_account_id` | many2one(account.account) | |
| `currency_id` | many2one(res.currency) | |

## Gotchas

- **State**: filter `state='posted'` (on `account.move`) or `parent_state='posted'`
  (on `account.move.line`). Draft moves can have arbitrary, untrustworthy numbers.
- **Refunds and credit notes are positive amounts** on `out_refund`/`in_refund`.
  Use `amount_total_signed` or `balance` on move lines to get correctly signed values.
- **Tax lines** appear in `line_ids` but not `invoice_line_ids`. For revenue
  analysis from invoices, use `invoice_line_ids` or filter move lines where
  `tax_line_id` is False.
- **Reconciliation**: a line is "paid" when fully reconciled. `payment_state` on
  the move is the high-level summary, but for line-level analysis use
  `reconciled` / `full_reconcile_id` / `matching_number`.
- **Multi-currency**: `debit`/`credit`/`balance` are in **company currency**.
  `amount_currency` + `currency_id` is the original.
