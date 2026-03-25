# Banktivity MCP Server

An MCP (Model Context Protocol) server that provides read-only access to [Banktivity 8](https://www.iggsoftware.com/banktivity/) personal finance databases, enabling LLM-powered summaries of transactions, balances, and spending.

## Features

- **Net worth summary** with multi-currency conversion to a reporting currency
- **Account balances** grouped by type (Asset, Cash/Bank, Savings, Current, Investment, Credit Card)
- **Investment holdings** with securities valuations at latest prices
- **Spending analysis** by category and month
- **Transaction search** by title or memo
- **Multi-database support** — query multiple Banktivity files from a single server

All queries are **read-only** and the database is opened in immutable mode.

## Requirements

- Python 3.12+
- A Banktivity 8 `.bank8` file

## Installation

```bash
git clone <repo-url>
cd banktivity
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Add the following to your Claude Desktop config file:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

### Single database

```json
{
  "mcpServers": {
    "banktivity": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/server.py"],
      "env": {
        "BANKTIVITY_DB": "/path/to/YourFile.bank8"
      }
    }
  }
}
```

### Multiple databases

```json
{
  "mcpServers": {
    "banktivity": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/server.py"],
      "env": {
        "BANKTIVITY_DATABASES": "{\"mine\": \"/path/to/Banking.bank8\", \"family\": \"/path/to/Family.bank8\"}"
      }
    }
  }
}
```

The first database in the JSON object is the default. Pass `database="family"` to any tool to query a non-default database.

### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BANKTIVITY_DATABASES` | JSON object mapping names to `.bank8` paths | — |
| `BANKTIVITY_DB` | Single `.bank8` path (fallback if `BANKTIVITY_DATABASES` not set) | `./Banking.bank8` |
| `BANKTIVITY_REPORTING_CCY` | Currency code for converted amounts | `GBP` |

## Available Tools

| Tool | Description |
|------|-------------|
| `list_databases` | List all configured Banktivity databases |
| `net_worth` | Full balance sheet by account type with securities + currency conversion |
| `account_balances` | Account list, optionally filtered by type |
| `transactions` | Transaction list filtered by account and/or date range |
| `spending_by_category` | Expense totals by category or category group |
| `monthly_spending` | Monthly spending trend in reporting currency |
| `search_transactions` | Search transactions by title or memo |
| `investment_holdings` | Securities detail: shares, prices, values per account |

## How It Works

Banktivity 8 stores data in a Core Data-backed SQLite database inside the `.bank8` bundle at `StoreContent/core.sql`. This server queries it directly using read-only SQLite connections.

Key implementation details:
- **Double-entry bookkeeping**: Each transaction has 2+ line items (debit + credit)
- **Running balances**: Determined using `ZPDATE DESC, ZPCREATIONTIME DESC` ordering
- **Multi-currency**: Exchange rates from the database convert all amounts to the reporting currency
- **Securities**: Investment accounts show cash + holdings valued at latest prices

## License

MIT
