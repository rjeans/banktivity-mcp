"""Banktivity MCP Server - read-only access to Banktivity 8 financial data.

Supports multiple Banktivity files via BANKTIVITY_DATABASES env var (JSON object mapping
names to .bank8 paths or core.sql paths). The first entry is the default.

Example:
  BANKTIVITY_DATABASES='{"mine": "/path/to/Banking.bank8", "mum": "/path/to/Mums.bank8"}'

If not set, falls back to BANKTIVITY_DB (single path) or the local Banking.bank8.
"""

import json
import sqlite3
import os
from datetime import datetime, date
from mcp.server.fastmcp import FastMCP

# Core Data epoch offset (seconds between 2001-01-01 and 1970-01-01)
CD_EPOCH_OFFSET = 978307200

REPORTING_CCY = os.environ.get("BANKTIVITY_REPORTING_CCY", "GBP")

mcp = FastMCP("banktivity")


# --- Database registry ---

def _resolve_path(path: str) -> str:
    """Resolve a .bank8 bundle or direct .sql path to the core.sql file."""
    path = os.path.expanduser(path)
    if path.endswith(".bank8"):
        return os.path.join(path, "StoreContent", "core.sql")
    return path


def _load_databases() -> dict[str, str]:
    """Load named database paths from environment."""
    env_multi = os.environ.get("BANKTIVITY_DATABASES")
    if env_multi:
        raw = json.loads(env_multi)
        return {name: _resolve_path(p) for name, p in raw.items()}

    env_single = os.environ.get("BANKTIVITY_DB")
    if env_single:
        return {"default": _resolve_path(env_single)}

    return {"default": _resolve_path(
        os.path.join(os.path.dirname(__file__), "Banking.bank8")
    )}


DATABASES = _load_databases()
DEFAULT_DB = next(iter(DATABASES))


def get_db(database: str | None = None) -> sqlite3.Connection:
    """Open read-only connection to a named Banktivity database."""
    name = database or DEFAULT_DB
    if name not in DATABASES:
        raise ValueError(f"Unknown database '{name}'. Available: {', '.join(DATABASES.keys())}")
    conn = sqlite3.connect(f"file:{DATABASES[name]}?mode=ro&immutable=1", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def get_reporting_ccy_uuid(db: sqlite3.Connection) -> str:
    """Look up the UUID for the reporting currency in this database."""
    row = db.execute(
        "SELECT ZPUNIQUEID FROM ZCURRENCY WHERE ZPCODE = ?", (REPORTING_CCY,)
    ).fetchone()
    if not row:
        raise ValueError(f"Reporting currency '{REPORTING_CCY}' not found in database")
    return row["ZPUNIQUEID"]


# --- Date helpers ---

def date_to_cd(d: str) -> float:
    """Convert YYYY-MM-DD string to Core Data timestamp."""
    dt = datetime.strptime(d, "%Y-%m-%d")
    return dt.timestamp() - CD_EPOCH_OFFSET


def cd_to_date(ts: float) -> str:
    """Convert Core Data timestamp to YYYY-MM-DD string."""
    return datetime.utcfromtimestamp(ts + CD_EPOCH_OFFSET).strftime("%Y-%m-%d")


# --- SQL building blocks ---

ACCOUNT_TYPE_MAP = """
CASE a.ZPACCOUNTCLASS
  WHEN 1 THEN 'Asset' WHEN 2 THEN 'Asset'
  WHEN 1000 THEN 'Cash/Bank' WHEN 1002 THEN 'Savings'
  WHEN 1006 THEN 'Current' WHEN 2000 THEN 'Investment'
  WHEN 5001 THEN 'Credit Card'
  WHEN 6000 THEN 'Income Category' WHEN 7000 THEN 'Expense Category'
END
"""

ACCOUNT_TYPE_SORT = """
CASE a.ZPACCOUNTCLASS
  WHEN 1 THEN 1 WHEN 2 THEN 1
  WHEN 1000 THEN 2 WHEN 1002 THEN 3
  WHEN 1006 THEN 4 WHEN 2000 THEN 5 WHEN 5001 THEN 6
END
"""


def reporting_rates_cte(ccy_uuid: str) -> str:
    """CTE to get latest exchange rates to the reporting currency."""
    return f"""
latest_rates AS (
  SELECT ZPSOURCECURRENCYID AS from_uuid,
         ZPEXCHANGERATE AS rate,
         ROW_NUMBER() OVER (PARTITION BY ZPSOURCECURRENCYID ORDER BY ZPEFFECTIVEDATE DESC) AS rn
  FROM ZEXCHANGERATE
  WHERE ZPDESTINATIONCURRENCYID = '{ccy_uuid}'
),
rep_rates AS (
  SELECT c.Z_PK AS ccy_pk, c.ZPCODE AS ccy, lr.rate
  FROM latest_rates lr
  JOIN ZCURRENCY c ON c.ZPUNIQUEID = lr.from_uuid
  WHERE lr.rn = 1
  UNION ALL
  SELECT Z_PK, ZPCODE, 1.0 FROM ZCURRENCY WHERE ZPUNIQUEID = '{ccy_uuid}'
)
"""


LATEST_BALANCES_CTE = """
latest_balances AS (
  SELECT li.ZPACCOUNT, li.ZPRUNNINGBALANCE AS balance,
    ROW_NUMBER() OVER (
      PARTITION BY li.ZPACCOUNT
      ORDER BY t.ZPDATE DESC, li.ZPCREATIONTIME DESC
    ) AS rn
  FROM ZLINEITEM li
  JOIN ZTRANSACTION t ON li.ZPTRANSACTION = t.Z_PK
)
"""

SEC_TOTALS_CTE = """
holdings AS (
  SELECT li.ZPACCOUNT, sli.ZPSECURITY, SUM(sli.ZPSHARES) AS shares
  FROM ZSECURITYLINEITEM sli
  JOIN ZLINEITEM li ON sli.ZPLINEITEM = li.Z_PK
  GROUP BY li.ZPACCOUNT, sli.ZPSECURITY
  HAVING ABS(SUM(sli.ZPSHARES)) > 0.0001
),
latest_prices AS (
  SELECT spi.ZPSECURITYID, sp.ZPCLOSEPRICE AS price,
    ROW_NUMBER() OVER (PARTITION BY spi.ZPSECURITYID ORDER BY sp.ZPDATE DESC) AS rn
  FROM ZSECURITYPRICE sp
  JOIN ZSECURITYPRICEITEM spi ON sp.ZPSECURITYPRICEITEM = spi.Z_PK
),
sec_totals AS (
  SELECT h.ZPACCOUNT,
    SUM(h.shares * lp.price * r.rate) AS rep_securities
  FROM holdings h
  JOIN ZSECURITY s ON h.ZPSECURITY = s.Z_PK
  JOIN latest_prices lp ON lp.ZPSECURITYID = s.ZPUNIQUEID AND lp.rn = 1
  JOIN rep_rates r ON r.ccy_pk = s.ZPCURRENCY
  GROUP BY h.ZPACCOUNT
)
"""


def _db_label(database: str | None) -> str:
    """Return a label prefix for multi-database output."""
    name = database or DEFAULT_DB
    if len(DATABASES) <= 1:
        return ""
    return f"[{name}] "


# --- Tools ---

@mcp.tool()
def list_databases() -> str:
    """List all available Banktivity databases."""
    lines = [f"{'Name':<15} {'Path':<80} {'Default'}"]
    lines.append("-" * 100)
    for name, path in DATABASES.items():
        is_default = "(default)" if name == DEFAULT_DB else ""
        lines.append(f"{name:<15} {path:<80} {is_default}")
    lines.append(f"\nReporting currency: {REPORTING_CCY}")
    return "\n".join(lines)


@mcp.tool()
def net_worth(include_zero: bool = False, database: str | None = None) -> str:
    """Get net worth summary: all accounts grouped by type with balances converted to reporting currency.
    Investment accounts include securities valuations at latest prices plus cash.
    Zero-balance accounts are hidden by default to keep output concise.

    Args:
        include_zero: If true, include accounts with zero balance. Default false.
        database: Database name to query. Omit for default.
    """
    db = get_db(database)
    ccy_uuid = get_reporting_ccy_uuid(db)
    rates_cte = reporting_rates_cte(ccy_uuid)

    rows = db.execute(f"""
        WITH {rates_cte},
        {LATEST_BALANCES_CTE},
        {SEC_TOTALS_CTE}
        SELECT
          {ACCOUNT_TYPE_MAP} AS type,
          a.ZPNAME AS account,
          r.ccy AS ccy,
          lb.balance AS local_balance,
          lb.balance * r.rate AS rep_cash,
          COALESCE(st.rep_securities, 0) AS rep_securities,
          lb.balance * r.rate + COALESCE(st.rep_securities, 0) AS rep_total,
          {ACCOUNT_TYPE_SORT} AS sort_order
        FROM ZACCOUNT a
        JOIN latest_balances lb ON lb.ZPACCOUNT = a.Z_PK AND lb.rn = 1
        JOIN rep_rates r ON r.ccy_pk = a.ZCURRENCY
        LEFT JOIN sec_totals st ON st.ZPACCOUNT = a.Z_PK
        WHERE a.ZPACCOUNTCLASS NOT IN (6000, 7000, 4001)
          AND a.ZPHIDDEN = 0
        ORDER BY sort_order, rep_total DESC
    """).fetchall()
    db.close()

    label = _db_label(database)
    rc = REPORTING_CCY
    lines = [f"{label}Net Worth (all amounts in {rc})"]
    lines.append(f"{'Account':<30} {'CCY':<5} {'Local Bal':>14} {rc+' Cash':>14} {rc+' Secs':>14} {rc+' Total':>14}")
    lines.append("-" * 95)

    current_type = None
    type_total_cash = type_total_secs = type_total = 0
    grand_cash = grand_secs = grand_total = 0
    type_skipped = 0

    for row in rows:
        if row["type"] != current_type:
            if current_type is not None:
                if type_skipped > 0:
                    lines.append(f"    ({type_skipped} zero-balance account{'s' if type_skipped != 1 else ''} hidden)")
                lines.append(f"  {'SUBTOTAL':<28} {'':<5} {'':<14} {type_total_cash:>14,.2f} {type_total_secs:>14,.2f} {type_total:>14,.2f}")
                lines.append("")
            current_type = row["type"]
            type_total_cash = type_total_secs = type_total = 0
            type_skipped = 0
            lines.append(f"  {current_type}")

        type_total_cash += row["rep_cash"]
        type_total_secs += row["rep_securities"]
        type_total += row["rep_total"]
        grand_cash += row["rep_cash"]
        grand_secs += row["rep_securities"]
        grand_total += row["rep_total"]

        if not include_zero and abs(row["rep_total"]) < 0.01:
            type_skipped += 1
            continue

        secs_str = f"{row['rep_securities']:>14,.2f}" if row["rep_securities"] else ""
        lines.append(
            f"    {row['account']:<28} {row['ccy']:<5} {row['local_balance']:>14,.2f} {row['rep_cash']:>14,.2f} {secs_str:>14} {row['rep_total']:>14,.2f}"
        )

    if type_skipped > 0:
        lines.append(f"    ({type_skipped} zero-balance account{'s' if type_skipped != 1 else ''} hidden)")
    lines.append(f"  {'SUBTOTAL':<28} {'':<5} {'':<14} {type_total_cash:>14,.2f} {type_total_secs:>14,.2f} {type_total:>14,.2f}")
    lines.append("")
    lines.append("=" * 95)
    lines.append(f"  {'NET WORTH':<28} {'':<5} {'':<14} {grand_cash:>14,.2f} {grand_secs:>14,.2f} {grand_total:>14,.2f}")

    return "\n".join(lines)


@mcp.tool()
def account_balances(account_type: str | None = None, include_zero: bool = False, database: str | None = None) -> str:
    """Get current balances for accounts, optionally filtered by type.
    Zero-balance accounts are hidden by default.

    Args:
        account_type: Filter by type: Asset, Cash/Bank, Savings, Current, Investment, Credit Card. None for all.
        include_zero: If true, include accounts with zero balance. Default false.
        database: Database name to query. Omit for default.
    """
    db = get_db(database)
    ccy_uuid = get_reporting_ccy_uuid(db)
    rates_cte = reporting_rates_cte(ccy_uuid)

    where_clause = ""
    if account_type:
        where_clause = f"AND {ACCOUNT_TYPE_MAP} = ?"

    rows = db.execute(f"""
        WITH {rates_cte},
        {LATEST_BALANCES_CTE},
        {SEC_TOTALS_CTE}
        SELECT
          {ACCOUNT_TYPE_MAP} AS type,
          a.ZPNAME AS account,
          r.ccy AS ccy,
          lb.balance AS local_balance,
          lb.balance * r.rate AS rep_cash,
          COALESCE(st.rep_securities, 0) AS rep_securities,
          lb.balance * r.rate + COALESCE(st.rep_securities, 0) AS rep_total
        FROM ZACCOUNT a
        JOIN latest_balances lb ON lb.ZPACCOUNT = a.Z_PK AND lb.rn = 1
        JOIN rep_rates r ON r.ccy_pk = a.ZCURRENCY
        LEFT JOIN sec_totals st ON st.ZPACCOUNT = a.Z_PK
        WHERE a.ZPACCOUNTCLASS NOT IN (6000, 7000, 4001)
          AND a.ZPHIDDEN = 0
          {where_clause}
        ORDER BY {ACCOUNT_TYPE_SORT}, rep_total DESC
    """, (account_type,) if account_type else ()).fetchall()
    db.close()

    label = _db_label(database)
    rc = REPORTING_CCY
    lines = [f"{label}Account Balances (in {rc})"]
    lines.append(f"{'Type':<14} {'Account':<28} {'CCY':<5} {'Local Bal':>14} {rc+' Total':>14}")
    lines.append("-" * 79)
    skipped = 0
    for row in rows:
        if not include_zero and abs(row["rep_total"]) < 0.01:
            skipped += 1
            continue
        lines.append(f"{row['type']:<14} {row['account']:<28} {row['ccy']:<5} {row['local_balance']:>14,.2f} {row['rep_total']:>14,.2f}")
    if skipped > 0:
        lines.append(f"({skipped} zero-balance accounts hidden)")

    return "\n".join(lines)


@mcp.tool()
def transactions(
    account: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
    database: str | None = None,
) -> str:
    """Get transactions with amounts and categories.

    Args:
        account: Filter by account name (exact match). None for all.
        start_date: Start date YYYY-MM-DD. None for no lower bound.
        end_date: End date YYYY-MM-DD. None for no upper bound.
        limit: Max rows to return (default 50).
        database: Database name to query. Omit for default.
    """
    db = get_db(database)
    conditions = []
    params = []

    if account:
        conditions.append("a.ZPNAME = ?")
        params.append(account)
    if start_date:
        conditions.append("t.ZPDATE >= ?")
        params.append(date_to_cd(start_date))
    if end_date:
        conditions.append("t.ZPDATE <= ?")
        params.append(date_to_cd(end_date))

    where = "AND " + " AND ".join(conditions) if conditions else ""

    rows = db.execute(f"""
        SELECT
          datetime(t.ZPDATE + {CD_EPOCH_OFFSET}, 'unixepoch') AS date,
          a.ZPNAME AS account,
          t.ZPTITLE AS title,
          li.ZPTRANSACTIONAMOUNT AS amount,
          li.ZPRUNNINGBALANCE AS balance,
          c.ZPCODE AS ccy,
          tt.ZPNAME AS txn_type,
          li.ZPMEMO AS memo
        FROM ZLINEITEM li
        JOIN ZACCOUNT a ON li.ZPACCOUNT = a.Z_PK
        JOIN ZTRANSACTION t ON li.ZPTRANSACTION = t.Z_PK
        LEFT JOIN ZTRANSACTIONTYPE tt ON t.ZPTRANSACTIONTYPE = tt.Z_PK
        LEFT JOIN ZCURRENCY c ON a.ZCURRENCY = c.Z_PK
        WHERE a.ZPACCOUNTCLASS NOT IN (6000, 7000)
          {where}
        ORDER BY t.ZPDATE DESC, li.Z_PK DESC
        LIMIT ?
    """, params + [limit]).fetchall()
    db.close()

    label = _db_label(database)
    lines = [f"{label}Transactions"]
    lines.append(f"{'Date':<12} {'Account':<22} {'Type':<12} {'Amount':>12} {'Balance':>12} {'CCY':<4} {'Title'}")
    lines.append("-" * 110)
    for row in rows:
        d = row["date"][:10] if row["date"] else ""
        lines.append(
            f"{d:<12} {(row['account'] or ''):<22} {(row['txn_type'] or ''):<12} {row['amount']:>12,.2f} {row['balance']:>12,.2f} {(row['ccy'] or ''):<4} {row['title'] or ''}"
        )

    return "\n".join(lines)


@mcp.tool()
def spending_by_category(
    start_date: str | None = None,
    end_date: str | None = None,
    group_level: bool = True,
    database: str | None = None,
) -> str:
    """Get spending aggregated by expense category, converted to reporting currency.

    Args:
        start_date: Start date YYYY-MM-DD. None for no lower bound.
        end_date: End date YYYY-MM-DD. None for no upper bound.
        group_level: If true, group by top-level category (e.g. '12-Living Expenses'). If false, show individual categories.
        database: Database name to query. Omit for default.
    """
    db = get_db(database)
    ccy_uuid = get_reporting_ccy_uuid(db)
    rates_cte = reporting_rates_cte(ccy_uuid)

    conditions = []
    params = []
    if start_date:
        conditions.append("t.ZPDATE >= ?")
        params.append(date_to_cd(start_date))
    if end_date:
        conditions.append("t.ZPDATE <= ?")
        params.append(date_to_cd(end_date))

    where = "AND " + " AND ".join(conditions) if conditions else ""

    if group_level:
        group_expr = """
            CASE
              WHEN a.ZPFULLNAME LIKE '%:%' THEN substr(a.ZPFULLNAME, 1, instr(a.ZPFULLNAME, ':')-1)
              ELSE a.ZPFULLNAME
            END
        """
    else:
        group_expr = "a.ZPNAME"

    rows = db.execute(f"""
        WITH {rates_cte}
        SELECT
          {group_expr} AS category,
          SUM(li.ZPTRANSACTIONAMOUNT * r.rate) AS rep_total,
          COUNT(li.Z_PK) AS txn_count
        FROM ZLINEITEM li
        JOIN ZACCOUNT a ON li.ZPACCOUNT = a.Z_PK
        JOIN ZTRANSACTION t ON li.ZPTRANSACTION = t.Z_PK
        JOIN rep_rates r ON r.ccy_pk = t.ZPCURRENCY
        WHERE a.ZPACCOUNTCLASS = 7000
          {where}
        GROUP BY category
        ORDER BY rep_total DESC
    """, params).fetchall()
    db.close()

    label = _db_label(database)
    rc = REPORTING_CCY
    lines = [f"{label}Spending by Category (in {rc})"]
    lines.append(f"{'Category':<40} {rc+' Total':>14} {'Txns':>6}")
    lines.append("-" * 62)
    grand = 0
    for row in rows:
        lines.append(f"{row['category']:<40} {row['rep_total']:>14,.2f} {row['txn_count']:>6}")
        grand += row["rep_total"]
    lines.append("-" * 62)
    lines.append(f"{'TOTAL':<40} {grand:>14,.2f}")

    return "\n".join(lines)


@mcp.tool()
def monthly_spending(
    start_date: str | None = None,
    end_date: str | None = None,
    database: str | None = None,
) -> str:
    """Get total spending by month converted to reporting currency.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 12 months ago.
        end_date: End date YYYY-MM-DD. None for no upper bound.
        database: Database name to query. Omit for default.
    """
    db = get_db(database)
    ccy_uuid = get_reporting_ccy_uuid(db)
    rates_cte = reporting_rates_cte(ccy_uuid)

    if not start_date:
        start_date = f"{date.today().year - 1}-{date.today().month:02d}-01"

    conditions = ["t.ZPDATE >= ?"]
    params = [date_to_cd(start_date)]
    if end_date:
        conditions.append("t.ZPDATE <= ?")
        params.append(date_to_cd(end_date))

    where = "AND " + " AND ".join(conditions)

    rows = db.execute(f"""
        WITH {rates_cte}
        SELECT
          strftime('%Y-%m', datetime(t.ZPDATE + {CD_EPOCH_OFFSET}, 'unixepoch')) AS month,
          SUM(li.ZPTRANSACTIONAMOUNT * r.rate) AS rep_spending,
          COUNT(DISTINCT t.Z_PK) AS txn_count
        FROM ZLINEITEM li
        JOIN ZACCOUNT a ON li.ZPACCOUNT = a.Z_PK
        JOIN ZTRANSACTION t ON li.ZPTRANSACTION = t.Z_PK
        JOIN rep_rates r ON r.ccy_pk = t.ZPCURRENCY
        WHERE a.ZPACCOUNTCLASS = 7000
          {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()
    db.close()

    label = _db_label(database)
    rc = REPORTING_CCY
    lines = [f"{label}Monthly Spending (in {rc})"]
    lines.append(f"{'Month':<10} {rc+' Spending':>14} {'Txns':>6}")
    lines.append("-" * 32)
    for row in rows:
        lines.append(f"{row['month']:<10} {row['rep_spending']:>14,.2f} {row['txn_count']:>6}")

    return "\n".join(lines)


@mcp.tool()
def search_transactions(
    query: str,
    limit: int = 30,
    database: str | None = None,
) -> str:
    """Search transactions by title or memo.

    Args:
        query: Search term (case-insensitive, matches anywhere in title or memo).
        limit: Max rows to return (default 30).
        database: Database name to query. Omit for default.
    """
    db = get_db(database)
    pattern = f"%{query}%"
    rows = db.execute(f"""
        SELECT
          datetime(t.ZPDATE + {CD_EPOCH_OFFSET}, 'unixepoch') AS date,
          a.ZPNAME AS account,
          t.ZPTITLE AS title,
          li.ZPTRANSACTIONAMOUNT AS amount,
          c.ZPCODE AS ccy,
          tt.ZPNAME AS txn_type,
          li.ZPMEMO AS memo
        FROM ZLINEITEM li
        JOIN ZACCOUNT a ON li.ZPACCOUNT = a.Z_PK
        JOIN ZTRANSACTION t ON li.ZPTRANSACTION = t.Z_PK
        LEFT JOIN ZTRANSACTIONTYPE tt ON t.ZPTRANSACTIONTYPE = tt.Z_PK
        LEFT JOIN ZCURRENCY c ON a.ZCURRENCY = c.Z_PK
        WHERE a.ZPACCOUNTCLASS NOT IN (6000, 7000)
          AND (t.ZPTITLE LIKE ? OR li.ZPMEMO LIKE ?)
        ORDER BY t.ZPDATE DESC
        LIMIT ?
    """, (pattern, pattern, limit)).fetchall()
    db.close()

    label = _db_label(database)
    lines = [f"{label}Search: '{query}'"]
    lines.append(f"{'Date':<12} {'Account':<22} {'Amount':>12} {'CCY':<4} {'Title'}")
    lines.append("-" * 80)
    for row in rows:
        d = row["date"][:10] if row["date"] else ""
        lines.append(
            f"{d:<12} {(row['account'] or ''):<22} {row['amount']:>12,.2f} {(row['ccy'] or ''):<4} {row['title'] or ''}"
        )

    return "\n".join(lines)


@mcp.tool()
def investment_holdings(database: str | None = None) -> str:
    """Get detailed investment holdings: shares, latest price, and value per security per account,
    all converted to reporting currency.

    Args:
        database: Database name to query. Omit for default.
    """
    db = get_db(database)
    ccy_uuid = get_reporting_ccy_uuid(db)
    rates_cte = reporting_rates_cte(ccy_uuid)

    rows = db.execute(f"""
        WITH {rates_cte},
        {LATEST_BALANCES_CTE},
        holdings AS (
          SELECT li.ZPACCOUNT, sli.ZPSECURITY, SUM(sli.ZPSHARES) AS shares
          FROM ZSECURITYLINEITEM sli
          JOIN ZLINEITEM li ON sli.ZPLINEITEM = li.Z_PK
          GROUP BY li.ZPACCOUNT, sli.ZPSECURITY
          HAVING ABS(SUM(sli.ZPSHARES)) > 0.0001
        ),
        latest_prices AS (
          SELECT spi.ZPSECURITYID, sp.ZPCLOSEPRICE AS price,
            ROW_NUMBER() OVER (PARTITION BY spi.ZPSECURITYID ORDER BY sp.ZPDATE DESC) AS rn
          FROM ZSECURITYPRICE sp
          JOIN ZSECURITYPRICEITEM spi ON sp.ZPSECURITYPRICEITEM = spi.Z_PK
        )
        SELECT
          a.ZPNAME AS account,
          s.ZPNAME AS security,
          sc.ZPCODE AS sec_ccy,
          h.shares,
          lp.price,
          h.shares * lp.price AS local_value,
          h.shares * lp.price * r.rate AS rep_value
        FROM holdings h
        JOIN ZACCOUNT a ON h.ZPACCOUNT = a.Z_PK
        JOIN ZSECURITY s ON h.ZPSECURITY = s.Z_PK
        JOIN latest_prices lp ON lp.ZPSECURITYID = s.ZPUNIQUEID AND lp.rn = 1
        JOIN ZCURRENCY sc ON sc.Z_PK = s.ZPCURRENCY
        JOIN rep_rates r ON r.ccy_pk = s.ZPCURRENCY
        WHERE a.ZPHIDDEN = 0
        ORDER BY a.ZPNAME, rep_value DESC
    """).fetchall()
    db.close()

    label = _db_label(database)
    rc = REPORTING_CCY
    lines = [f"{label}Investment Holdings (in {rc})"]
    lines.append(f"{'Account':<20} {'Security':<40} {'CCY':<4} {'Shares':>10} {'Price':>10} {rc+' Value':>14}")
    lines.append("-" * 100)
    current_account = None
    account_total = 0
    grand_total = 0

    for row in rows:
        if row["account"] != current_account:
            if current_account is not None:
                lines.append(f"{'':>84} {account_total:>14,.2f}")
                lines.append("")
            current_account = row["account"]
            account_total = 0

        lines.append(
            f"  {row['account']:<18} {row['security']:<40} {row['sec_ccy']:<4} {row['shares']:>10,.2f} {row['price']:>10,.2f} {row['rep_value']:>14,.2f}"
        )
        account_total += row["rep_value"]
        grand_total += row["rep_value"]

    if current_account is not None:
        lines.append(f"{'':>84} {account_total:>14,.2f}")
    lines.append("")
    lines.append("-" * 100)
    lines.append(f"{'TOTAL SECURITIES':>84} {grand_total:>14,.2f}")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
