# Banktivity MCP Server

## Project Overview

MCP server providing read-only access to a Banktivity 8 personal finance database, enabling LLM-powered summaries of transactions and balances.

## Database

- **File**: `Banking.bank8/StoreContent/core.sql` (SQLite, ~32MB)
- **Format**: Core Data-backed SQLite (tables prefixed with `Z`, columns with `ZP`)
- **Access**: Read-only (`?mode=ro`). Never write to this database.

### Date Encoding

Core Data epoch: seconds since 2001-01-01. Convert to/from Unix epoch by adding 978307200.

```sql
datetime(ZPDATE + 978307200, 'unixepoch')  -- Core Data → human-readable
```

### Key Tables

| Table | Purpose |
|-------|---------|
| `ZTRANSACTION` | Transaction headers: date, title, type, cleared status |
| `ZLINEITEM` | Double-entry line items: amount, running balance, memo |
| `ZACCOUNT` | Accounts and expense/income categories |
| `ZTRANSACTIONTYPE` | Deposit, Withdrawal, Transfer, Buy, Sell, Dividend, etc. |
| `ZSECURITY` | Investment securities (ETFs, crypto, funds) |
| `ZCURRENCY` | 8 currencies: GBP, EUR, DKK, USD, NOK, HUF, SGD, SEK |
| `ZEXCHANGERATE` | Currency exchange rates |
| `ZSECURITYLINEITEM` | Investment transaction details |

### Relationships

- `ZLINEITEM.ZPTRANSACTION` → `ZTRANSACTION.Z_PK`
- `ZLINEITEM.ZPACCOUNT` → `ZACCOUNT.Z_PK`
- `ZTRANSACTION.ZPTRANSACTIONTYPE` → `ZTRANSACTIONTYPE.Z_PK`
- `ZACCOUNT.ZCURRENCY` → `ZCURRENCY.Z_PK`
- `ZSECURITY.ZPCURRENCY` → `ZCURRENCY.Z_PK`

### Account Classes

| Class | Meaning |
|-------|---------|
| 1, 2 | Assets (property) |
| 1000 | Cash/bank |
| 1002 | Savings |
| 1006 | Current/checking accounts |
| 2000 | Investment accounts |
| 4001 | Income |
| 5001 | Credit cards |
| 6000 | Income categories |
| 7000 | Expense categories |

### Double-Entry Bookkeeping

Each transaction typically has 2 line items: one debits a bank/credit account, the other credits an expense/income category. Running balances are maintained on line items.

### Determining Current Balance

To get the current balance for an account, find the most recent line item by:

```sql
ORDER BY t.ZPDATE DESC, li.ZPCREATIONTIME DESC
```

**Critical**: Do NOT use `MAX(Z_PK)`, `ZPINTRADAYSORTINDEX`, or `ZPRUNNINGBALANCE` as tiebreakers — they all fail in edge cases (back-dated imports, same-date multi-entry transactions, cross-currency exchanges). `ZPCREATIONTIME` (Core Data microsecond timestamp) is the only reliable ordering when multiple line items share the same transaction date.

### Cross-Currency Transactions

`ZLINEITEM.ZPTRANSACTIONAMOUNT` is in the *transaction's* currency, not the account's currency. The running balance is in the account's currency. `ZPEXCHANGERATE` on the line item provides the conversion factor. This means `SUM(ZPTRANSACTIONAMOUNT)` does NOT equal the account balance for multi-currency accounts.

### Account Folder Hierarchy (ZGROUP)

Groups form a tree via `ZPPARENT`. Account membership is stored in `ZPITEMS` as NSKeyedArchiver binary plists containing UUIDs that reference either `IGGCAccountingGroup` (subgroup) or `IGGCAccountingPrimaryAccount` (account via `ZACCOUNT.ZPUNIQUEID`).

The hierarchy is nested up to 4 levels deep (e.g. Accounts > Pensions > Owner > Region > Fund). Top-level folders include Credit Cards, Banking, Savings, Liabilities, Investments, Assets, Closed, and Pensions. Not needed for MCP queries — account class provides sufficient grouping.

### Expense Category Hierarchy

Expense categories (class 7000) use `ZPFULLNAME` with a prefix-based hierarchy:
- `11-Regular Payments - DK:` / `11-Regular Payments - UK:` (rent, energy, subscriptions)
- `12-Living Expenses:` (groceries, travel, clothing, medical)
- `13-Discretionary Spending:` (dining, entertainment, misc)
- `14-Other Spending:` (flights, holiday, gifts, home improvement)
- `16-Extraordinary:` (moving, holding for kids)

## Development Guidelines

- Always open the database read-only
- Use Python with the `mcp` SDK for the server
- Handle multi-currency amounts where relevant
