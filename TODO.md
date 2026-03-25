# Banktivity MCP Server - TODO

## Phase 1: Data Exploration & Query Layer
- [x] Examine database structure and document schema
- [x] Create CLAUDE.md with database documentation
- [x] Run exploratory queries: balances, transaction summaries, category breakdowns
- [x] Reconcile balances (fixed: date-based ordering, ZPRUNNINGBALANCE tiebreaker)
- [x] Multi-currency conversion to GBP using stored exchange rates
- [x] Securities valuations for investment accounts

## Phase 2: MCP Server
- [x] Set up Python venv with mcp SDK
- [x] Implement MCP server with tools:
  - [x] `net_worth` — full balance sheet by type with securities + GBP conversion
  - [x] `account_balances` — balances filtered by account type
  - [x] `transactions` — filtered transaction list by account/date
  - [x] `spending_by_category` — aggregated spending with group-level option
  - [x] `monthly_spending` — monthly spending trends in GBP
  - [x] `search_transactions` — search by title/memo
  - [x] `investment_holdings` — detailed securities per account
- [ ] Configure for Claude Desktop / CLI integration

## Phase 3: Enhancements
- [ ] Credit card statement summaries
- [ ] Income vs expenses summary
- [ ] Year-over-year comparisons
