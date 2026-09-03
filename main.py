from fastmcp import FastMCP
import os
import json
import csv
import io
import re
import base64
import psycopg
from psycopg_pool import AsyncConnectionPool
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for server-side rendering
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from mcp.types import ImageContent
from datetime import datetime, timedelta
from calendar import monthrange

# ─── Database Configuration ───────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

mcp = FastMCP("ExpenseTracker")

# Connection pool (only created if DATABASE_URL is available)
pool = None
if DATABASE_URL:
    pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=False)

@mcp.lifecycle()
async def on_startup():
    if pool:
        await pool.open()
    # Initialize tables on first startup
    init_db()

@mcp.lifecycle()
async def on_shutdown():
    if pool:
        await pool.close()



# ─── Validation Helpers ───────────────────────────────────────────────

def _validate_date(date_str: str) -> str:
    """Validate and return a YYYY-MM-DD date string."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise ValueError(f"Invalid date format: '{date_str}'. Expected YYYY-MM-DD.")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date: '{date_str}'. Not a real calendar date.")
    return date_str


def _validate_amount(amount: float) -> float:
    """Validate that amount is positive."""
    if amount <= 0:
        raise ValueError(f"Amount must be positive, got {amount}.")
    return amount


def _validate_month(month_str: str) -> str:
    """Validate YYYY-MM month format."""
    if not re.match(r"^\d{4}-\d{2}$", month_str):
        raise ValueError(f"Invalid month format: '{month_str}'. Expected YYYY-MM.")
    year, month = int(month_str[:4]), int(month_str[5:7])
    if month < 1 or month > 12:
        raise ValueError(f"Invalid month: {month}.")
    return month_str


def _month_date_range(month_str: str) -> tuple[str, str]:
    """Return (first_day, last_day) for a YYYY-MM month."""
    year, month = int(month_str[:4]), int(month_str[5:7])
    last_day = monthrange(year, month)[1]
    return f"{month_str}-01", f"{month_str}-{last_day:02d}"


VALID_PAYMENT_METHODS = [
    "Cash", "UPI", "Credit Card", "Debit Card", "Bank Transfer", "Wallet", "Other"
]


def _validate_payment_method(method: str) -> str:
    """Validate and return a payment method string."""
    method = method.strip()
    if method not in VALID_PAYMENT_METHODS:
        raise ValueError(
            f"Invalid payment method: '{method}'. "
            f"Valid options: {', '.join(VALID_PAYMENT_METHODS)}"
        )
    return method


# ─── Categories Helper ───────────────────────────────────────────────

def _load_categories() -> list[str]:
    """Load categories from JSON file or return defaults."""
    default = [
        "Food & Dining", "Transportation", "Shopping", "Entertainment",
        "Bills & Utilities", "Healthcare", "Travel", "Education",
        "Business", "Other"
    ]
    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("categories", default)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_categories(categories: list[str]) -> None:
    """Save categories to JSON file."""
    with open(CATEGORIES_PATH, "w", encoding="utf-8") as f:
        json.dump({"categories": sorted(set(categories))}, f, indent=2)


# ─── Database Initialization ─────────────────────────────────────────

def init_db():
    """Initialize all database tables synchronously at startup."""
    if not DATABASE_URL:
        return
    try:
        import psycopg
        with psycopg.connect(DATABASE_URL) as conn:
            c = conn.cursor()
            # Expenses table
            c.execute("""
                CREATE TABLE IF NOT EXISTS expenses(
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    payment_method TEXT DEFAULT 'Cash',
                    created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
                )
            """)

            # Add payment_method column if missing (for existing databases)
            try:
                c.execute("SELECT payment_method FROM expenses LIMIT 1")
            except psycopg.errors.UndefinedColumn:
                c.execute("ALTER TABLE expenses ADD COLUMN payment_method TEXT DEFAULT 'Cash'")

            # Budgets table
            c.execute("""
                CREATE TABLE IF NOT EXISTS budgets(
                    id SERIAL PRIMARY KEY,
                    category TEXT NOT NULL UNIQUE,
                    monthly_limit REAL NOT NULL,
                    created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
                )
            """)

            # Income table
            c.execute("""
                CREATE TABLE IF NOT EXISTS income(
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    source TEXT NOT NULL,
                    note TEXT DEFAULT '',
                    created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
                )
            """)

            # Expense tags table (many-to-many)
            c.execute("""
                CREATE TABLE IF NOT EXISTS expense_tags(
                    expense_id INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    created_at TEXT DEFAULT (CURRENT_TIMESTAMP),
                    PRIMARY KEY (expense_id, tag),
                    FOREIGN KEY (expense_id) REFERENCES expenses(id) ON DELETE CASCADE
                )
            """)

            # Global spending limits
            c.execute("""
                CREATE TABLE IF NOT EXISTS global_limits(
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    daily_limit REAL,
                    weekly_limit REAL,
                    monthly_limit REAL,
                    created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
                )
            """)

            print("Database initialized successfully (all tables ready)")
            conn.commit()
    except Exception as e:
        print(f"Database initialization error: {e}")



# ═══════════════════════════════════════════════════════════════════════
#  PHASE 1 & 2: CORE EXPENSE CRUD
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def add_expense(
    date: str, amount: float, category: str,
    subcategory: str = "", note: str = "", payment_method: str = "Cash"
) -> dict:
    """Add a new expense entry to the database.

    Args:
        date: Date in YYYY-MM-DD format
        amount: Expense amount (must be positive)
        category: Expense category (e.g. Food & Dining, Transportation)
        subcategory: Optional subcategory for finer classification
        note: Optional note or description
    """
    try:
        _validate_date(date)
        _validate_amount(amount)
        _validate_payment_method(payment_method)
        if not category.strip():
            return {"status": "error", "message": "Category cannot be empty."}

        async with pool.connection() as c:
            cur = await c.execute(
                "INSERT INTO expenses(date, amount, category, subcategory, note, payment_method) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (date, amount, category.strip(), subcategory.strip(), note.strip(), payment_method.strip())
            )
            row = await cur.fetchone()
            expense_id = row[0] if row else None
            await c.commit()

            # Check budget warning
            month = date[:7]
            budget_msg = await _check_single_budget(c, category.strip(), month)

            # Check global limits warning
            global_warnings = await _check_global_limits(c, date)

            result = {
                "status": "success", "id": expense_id,
                "message": f"Expense of ₹{amount:.2f} added to '{category}' on {date}"
            }
            if budget_msg:
                result["budget_warning"] = budget_msg
            if global_warnings:
                result["global_warnings"] = global_warnings
            return result
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        if "readonly" in str(e).lower():
            return {"status": "error", "message": "Database is in read-only mode."}
        return {"status": "error", "message": f"Database error: {str(e)}"}


@mcp.tool()
async def get_expense(id: int) -> dict:
    """Get a single expense by its ID.

    Args:
        id: The expense ID to look up
    """
    try:
        async with pool.connection() as c:
            cur = await c.execute(
                "SELECT id, date, amount, category, subcategory, note, payment_method FROM expenses WHERE id = %s",
                (id,)
            )
            row = await cur.fetchone()
            if not row:
                return {"status": "error", "message": f"Expense with id {id} not found."}
            cols = [d[0] for d in cur.description]
            return {"status": "success", "expense": dict(zip(cols, row))}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def update_expense(
    id: int, date: str = None, amount: float = None,
    category: str = None, subcategory: str = None, note: str = None,
    payment_method: str = None
) -> dict:
    """Update an existing expense. Only provided fields are updated.

    Args:
        id: The expense ID to update
        date: New date in YYYY-MM-DD format (optional)
        amount: New amount (optional, must be positive)
        category: New category (optional)
        subcategory: New subcategory (optional)
        note: New note (optional)
    """
    try:
        updates = []
        params = []
        if date is not None:
            _validate_date(date)
            updates.append("date = %s")
            params.append(date)
        if amount is not None:
            _validate_amount(amount)
            updates.append("amount = %s")
            params.append(amount)
        if category is not None:
            if not category.strip():
                return {"status": "error", "message": "Category cannot be empty."}
            updates.append("category = %s")
            params.append(category.strip())
        if subcategory is not None:
            updates.append("subcategory = %s")
            params.append(subcategory.strip())
        if note is not None:
            updates.append("note = %s")
            params.append(note.strip())
        if payment_method is not None:
            _validate_payment_method(payment_method)
            updates.append("payment_method = %s")
            params.append(payment_method.strip())

        if not updates:
            return {"status": "error", "message": "No fields to update."}

        params.append(id)
        async with pool.connection() as c:
            cur = await c.execute(
                f"UPDATE expenses SET {', '.join(updates)} WHERE id = %s", params
            )
            await c.commit()
            if cur.rowcount == 0:
                return {"status": "error", "message": f"Expense with id {id} not found."}
            return {"status": "success", "message": f"Expense {id} updated."}
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def delete_expense(id: int) -> dict:
    """Delete an expense by its ID.

    Args:
        id: The expense ID to delete
    """
    try:
        async with pool.connection() as c:
            cur = await c.execute("DELETE FROM expenses WHERE id = %s", (id,))
            await c.commit()
            if cur.rowcount == 0:
                return {"status": "error", "message": f"Expense with id {id} not found."}
            return {"status": "success", "message": f"Expense {id} deleted."}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def list_expenses(start_date: str, end_date: str) -> list | dict:
    """List expense entries within an inclusive date range.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
    """
    try:
        _validate_date(start_date)
        _validate_date(end_date)
        async with pool.connection() as c:
            cur = await c.execute(
                """SELECT id, date, amount, category, subcategory, note, payment_method
                   FROM expenses WHERE date BETWEEN %s AND %s
                   ORDER BY date DESC, id DESC""",
                (start_date, end_date)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def summarize(
    start_date: str, end_date: str, category: str = None
) -> list | dict:
    """Summarize expenses by category within an inclusive date range.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        category: Optional category to filter by
    """
    try:
        _validate_date(start_date)
        _validate_date(end_date)
        async with pool.connection() as c:
            query = """
                SELECT category, SUM(amount) AS total_amount, COUNT(*) as count
                FROM expenses WHERE date BETWEEN %s AND %s
            """
            params = [start_date, end_date]
            if category:
                query += " AND category = %s"
                params.append(category)
            query += " GROUP BY category ORDER BY total_amount DESC"

            cur = await c.execute(query, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def spending_by_payment_method(month: str) -> dict:
    """Breakdown of spending by payment method for a given month.

    Args:
        month: Month in YYYY-MM format
    """
    try:
        _validate_month(month)
        start, end = _month_date_range(month)

        async with pool.connection() as c:
            cur = await c.execute(
                """SELECT payment_method, SUM(amount) as total, COUNT(*) as count
                   FROM expenses WHERE date BETWEEN %s AND %s
                   GROUP BY payment_method ORDER BY total DESC""",
                (start, end)
            )
            rows = await cur.fetchall()

        if not rows:
            return {"month": month, "total": 0, "breakdown": []}

        total_spent = sum(r[1] for r in rows)
        breakdown = []
        for r in rows:
            method, amount, count = r
            breakdown.append({
                "method": method,
                "amount": round(amount, 2),
                "count": count,
                "percentage": round((amount / total_spent) * 100, 1) if total_spent > 0 else 0
            })

        return {
            "month": month,
            "total_spent": round(total_spent, 2),
            "breakdown": breakdown
        }
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


# ═══════════════════════════════════════════════════════════════════════
#  PHASE 3: SEARCH & FILTERING
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def search_expenses(
    keyword: str = None, category: str = None,
    min_amount: float = None, max_amount: float = None,
    start_date: str = None, end_date: str = None,
    payment_method: str = None, limit: int = 50
) -> list | dict:
    """Search expenses with flexible filters. All parameters are optional.

    Args:
        keyword: Search in note and subcategory fields
        category: Filter by category name
        min_amount: Minimum expense amount
        max_amount: Maximum expense amount
        start_date: Start of date range (YYYY-MM-DD)
        end_date: End of date range (YYYY-MM-DD)
        payment_method: Filter by payment method
        limit: Maximum results to return (default 50)
    """
    try:
        conditions = []
        params = []

        if keyword:
            conditions.append("(note LIKE %s OR subcategory LIKE %s)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if category:
            conditions.append("category = %s")
            params.append(category)
        if min_amount is not None:
            conditions.append("amount >= %s")
            params.append(min_amount)
        if max_amount is not None:
            conditions.append("amount <= %s")
            params.append(max_amount)
        if start_date:
            _validate_date(start_date)
            conditions.append("date >= %s")
            params.append(start_date)
        if end_date:
            _validate_date(end_date)
            conditions.append("date <= %s")
            params.append(end_date)
        if payment_method:
            _validate_payment_method(payment_method)
            conditions.append("payment_method = %s")
            params.append(payment_method.strip())

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(min(limit, 200))

        async with pool.connection() as c:
            cur = await c.execute(
                f"""SELECT id, date, amount, category, subcategory, note, payment_method
                    FROM expenses {where}
                    ORDER BY date DESC, id DESC LIMIT %s""",
                params
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


# ═══════════════════════════════════════════════════════════════════════
#  PHASE 9: TAGS / LABELS
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def tag_expense(expense_id: int, tag: str) -> dict:
    """Add a tag to an existing expense.

    Args:
        expense_id: The ID of the expense to tag
        tag: The tag to add (e.g. 'work-trip', 'vacation')
    """
    try:
        tag = tag.strip().lower()
        if not tag:
            return {"status": "error", "message": "Tag cannot be empty."}

        import sqlite3
        async with pool.connection() as c:
            # Check if expense exists
            cur = await c.execute("SELECT id FROM expenses WHERE id = %s", (expense_id,))
            if not await cur.fetchone():
                return {"status": "error", "message": f"Expense {expense_id} not found."}

            try:
                await c.execute(
                    "INSERT INTO expense_tags(expense_id, tag) VALUES (%s, %s)",
                    (expense_id, tag)
                )
                await c.commit()
                return {"status": "success", "message": f"Tag '{tag}' added to expense {expense_id}."}
            except sqlite3.IntegrityError:
                return {"status": "error", "message": f"Expense {expense_id} already has tag '{tag}'."}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def untag_expense(expense_id: int, tag: str) -> dict:
    """Remove a tag from an expense.

    Args:
        expense_id: The ID of the expense
        tag: The tag to remove
    """
    try:
        tag = tag.strip().lower()
        async with pool.connection() as c:
            cur = await c.execute(
                "DELETE FROM expense_tags WHERE expense_id = %s AND tag = %s",
                (expense_id, tag)
            )
            await c.commit()
            if cur.rowcount == 0:
                return {"status": "error", "message": f"Tag '{tag}' not found on expense {expense_id}."}
            return {"status": "success", "message": f"Tag '{tag}' removed from expense {expense_id}."}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def list_tags() -> dict:
    """List all unique tags and how many times they are used."""
    try:
        async with pool.connection() as c:
            cur = await c.execute(
                "SELECT tag, COUNT(expense_id) as count FROM expense_tags GROUP BY tag ORDER BY count DESC, tag ASC"
            )
            rows = await cur.fetchall()
            return {"status": "success", "tags": [{"tag": r[0], "count": r[1]} for r in rows]}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def search_by_tag(tag: str) -> list | dict:
    """Find all expenses that have a specific tag.

    Args:
        tag: The tag to search for
    """
    try:
        tag = tag.strip().lower()
        async with pool.connection() as c:
            cur = await c.execute(
                """SELECT e.id, e.date, e.amount, e.category, e.subcategory, e.note, e.payment_method
                   FROM expenses e
                   JOIN expense_tags t ON e.id = t.expense_id
                   WHERE t.tag = %s
                   ORDER BY e.date DESC, e.id DESC""",
                (tag,)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


# ═══════════════════════════════════════════════════════════════════════
#  PHASE 4: BUDGET TRACKING
# ═══════════════════════════════════════════════════════════════════════

async def _check_single_budget(c, category: str, month: str) -> str | None:
    """Internal: check budget for a category in a month. Returns warning or None."""
    cur = await c.execute(
        "SELECT monthly_limit FROM budgets WHERE category = %s", (category,)
    )
    row = await cur.fetchone()
    if not row:
        return None
    limit = row[0]
    start, end = _month_date_range(month)
    cur2 = await c.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE category = %s AND date BETWEEN %s AND %s",
        (category, start, end)
    )
    spent = (await cur2.fetchone())[0]
    pct = (spent / limit) * 100
    if pct > 100:
        return f"💥 OVER BUDGET: '{category}' spent ₹{spent:.2f} / ₹{limit:.2f} ({pct:.0f}%)"
    elif pct >= 90:
        return f"🚨 Danger zone: '{category}' spent ₹{spent:.2f} / ₹{limit:.2f} ({pct:.0f}%)"
    elif pct >= 75:
        return f"⚠️ Approaching limit: '{category}' spent ₹{spent:.2f} / ₹{limit:.2f} ({pct:.0f}%)"
    elif pct >= 50:
        return f"⚡ Halfway there: '{category}' spent ₹{spent:.2f} / ₹{limit:.2f} ({pct:.0f}%)"
    return None


@mcp.tool()
async def set_budget(category: str, monthly_limit: float) -> dict:
    """Set or update the monthly budget for a category.

    Args:
        category: The expense category to budget
        monthly_limit: Monthly spending limit (must be positive)
    """
    try:
        _validate_amount(monthly_limit)
        if not category.strip():
            return {"status": "error", "message": "Category cannot be empty."}
        async with pool.connection() as c:
            await c.execute(
                """INSERT INTO budgets(category, monthly_limit) VALUES (%s, %s)
                   ON CONFLICT(category) DO UPDATE SET monthly_limit = excluded.monthly_limit""",
                (category.strip(), monthly_limit)
            )
            await c.commit()
            return {"status": "success", "message": f"Budget set: '{category}' → ₹{monthly_limit:.2f}/month"}
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def get_budgets() -> list | dict:
    """List all category budgets."""
    try:
        async with pool.connection() as c:
            cur = await c.execute(
                "SELECT category, monthly_limit FROM budgets ORDER BY category"
            )
            return [{"category": r[0], "monthly_limit": r[1]} for r in await cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def check_budget(month: str) -> list | dict:
    """Check spending vs budget for all categories in a month.

    Args:
        month: Month in YYYY-MM format
    """
    try:
        _validate_month(month)
        start, end = _month_date_range(month)
        async with pool.connection() as c:
            cur = await c.execute("""
                SELECT b.category, b.monthly_limit,
                       COALESCE(SUM(e.amount), 0) as spent
                FROM budgets b
                LEFT JOIN expenses e ON e.category = b.category
                    AND e.date BETWEEN %s AND %s
                GROUP BY b.category
                ORDER BY b.category
            """, (start, end))
            results = []
            for row in await cur.fetchall():
                cat, limit, spent = row
                remaining = limit - spent
                pct = (spent / limit * 100) if limit > 0 else 0
                if pct > 100: status_icon = "💥"
                elif pct >= 90: status_icon = "🚨"
                elif pct >= 75: status_icon = "⚠️"
                elif pct >= 50: status_icon = "⚡"
                else: status_icon = "✅"

                results.append({
                    "category": cat, "monthly_limit": limit,
                    "spent": round(spent, 2), "remaining": round(remaining, 2),
                    "percentage": round(pct, 1),
                    "status": status_icon,
                    "over_budget": spent > limit
                })
            return results
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


async def _check_global_limits(c, date: str) -> list[str]:
    """Internal: check daily, weekly, and monthly global limits."""
    warnings = []
    cur = await c.execute("SELECT daily_limit, weekly_limit, monthly_limit FROM global_limits WHERE id = 1")
    row = await cur.fetchone()
    if not row:
        return warnings
    d_limit, w_limit, m_limit = row

    if d_limit:
        cur = await c.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date = %s", (date,))
        spent = (await cur.fetchone())[0]
        pct = (spent / d_limit) * 100
        if pct > 100: warnings.append(f"💥 DAILY LIMIT EXCEEDED: ₹{spent:.2f} / ₹{d_limit:.2f}")
        elif pct >= 90: warnings.append(f"🚨 Daily limit danger: ₹{spent:.2f} / ₹{d_limit:.2f}")
        elif pct >= 75: warnings.append(f"⚠️ Approaching daily limit: ₹{spent:.2f} / ₹{d_limit:.2f}")

    if w_limit:
        dt = datetime.strptime(date, "%Y-%m-%d")
        start = dt - timedelta(days=dt.weekday())
        end = start + timedelta(days=6)
        s_str, e_str = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        cur = await c.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN %s AND %s", (s_str, e_str))
        spent = (await cur.fetchone())[0]
        pct = (spent / w_limit) * 100
        if pct > 100: warnings.append(f"💥 WEEKLY LIMIT EXCEEDED: ₹{spent:.2f} / ₹{w_limit:.2f}")
        elif pct >= 90: warnings.append(f"🚨 Weekly limit danger: ₹{spent:.2f} / ₹{w_limit:.2f}")
        elif pct >= 75: warnings.append(f"⚠️ Approaching weekly limit: ₹{spent:.2f} / ₹{w_limit:.2f}")

    if m_limit:
        start_dt, end_dt = _month_date_range(date[:7])
        cur = await c.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN %s AND %s", (start_dt, end_dt))
        spent = (await cur.fetchone())[0]
        pct = (spent / m_limit) * 100
        if pct > 100: warnings.append(f"💥 MONTHLY LIMIT EXCEEDED: ₹{spent:.2f} / ₹{m_limit:.2f}")
        elif pct >= 90: warnings.append(f"🚨 Monthly limit danger: ₹{spent:.2f} / ₹{m_limit:.2f}")
        elif pct >= 75: warnings.append(f"⚠️ Approaching monthly limit: ₹{spent:.2f} / ₹{m_limit:.2f}")

    return warnings


@mcp.tool()
async def set_global_limits(daily: float = None, weekly: float = None, monthly: float = None) -> dict:
    """Set global spending limits (daily, weekly, monthly). Omitted values are unchanged.

    Args:
        daily: Daily spending limit
        weekly: Weekly spending limit
        monthly: Monthly spending limit
    """
    try:
        updates = []
        params = []
        if daily is not None:
            _validate_amount(daily)
            updates.append("daily_limit = %s")
            params.append(daily)
        if weekly is not None:
            _validate_amount(weekly)
            updates.append("weekly_limit = %s")
            params.append(weekly)
        if monthly is not None:
            _validate_amount(monthly)
            updates.append("monthly_limit = %s")
            params.append(monthly)

        if not updates:
            return {"status": "error", "message": "No limits provided to update."}

        async with pool.connection() as c:
            # ensure row exists
            await c.execute("INSERT INTO global_limits(id) VALUES (1) ON CONFLICT (id) DO NOTHING")
            await c.execute(f"UPDATE global_limits SET {', '.join(updates)} WHERE id = 1", params)
            await c.commit()
            return {"status": "success", "message": "Global limits updated."}
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def check_global_spending(date: str = None) -> dict:
    """Check total spending against global limits for a specific date.

    Args:
        date: Date in YYYY-MM-DD format (defaults to today)
    """
    try:
        target_date = date if date else datetime.now().strftime("%Y-%m-%d")
        _validate_date(target_date)

        async with pool.connection() as c:
            cur = await c.execute("SELECT daily_limit, weekly_limit, monthly_limit FROM global_limits WHERE id = 1")
            row = await cur.fetchone()
            if not row:
                return {"status": "success", "message": "No global limits are set."}
            d_limit, w_limit, m_limit = row

            results = {"date": target_date, "limits": {}}

            if d_limit:
                cur = await c.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date = %s", (target_date,))
                spent = (await cur.fetchone())[0]
                pct = (spent / d_limit * 100)
                results["limits"]["daily"] = {
                    "limit": d_limit, "spent": round(spent, 2), "remaining": round(d_limit - spent, 2),
                    "percentage": round(pct, 1), "over_limit": spent > d_limit
                }

            if w_limit:
                dt = datetime.strptime(target_date, "%Y-%m-%d")
                start = dt - timedelta(days=dt.weekday())
                end = start + timedelta(days=6)
                s_str, e_str = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
                cur = await c.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN %s AND %s", (s_str, e_str))
                spent = (await cur.fetchone())[0]
                pct = (spent / w_limit * 100)
                results["limits"]["weekly"] = {
                    "limit": w_limit, "spent": round(spent, 2), "remaining": round(w_limit - spent, 2),
                    "percentage": round(pct, 1), "over_limit": spent > w_limit
                }

            if m_limit:
                start_dt, end_dt = _month_date_range(target_date[:7])
                cur = await c.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN %s AND %s", (start_dt, end_dt))
                spent = (await cur.fetchone())[0]
                pct = (spent / m_limit * 100)
                results["limits"]["monthly"] = {
                    "limit": m_limit, "spent": round(spent, 2), "remaining": round(m_limit - spent, 2),
                    "percentage": round(pct, 1), "over_limit": spent > m_limit
                }

            return results
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def delete_budget(category: str) -> dict:
    """Remove the budget for a category.

    Args:
        category: The category whose budget to remove
    """
    try:
        async with pool.connection() as c:
            cur = await c.execute("DELETE FROM budgets WHERE category = %s", (category,))
            await c.commit()
            if cur.rowcount == 0:
                return {"status": "error", "message": f"No budget found for '{category}'."}
            return {"status": "success", "message": f"Budget for '{category}' removed."}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}




# ═══════════════════════════════════════════════════════════════════════
#  PHASE 6: ANALYTICS & DASHBOARD
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def monthly_comparison(month1: str, month2: str) -> dict:
    """Compare spending between two months.

    Args:
        month1: First month in YYYY-MM format
        month2: Second month in YYYY-MM format
    """
    try:
        _validate_month(month1)
        _validate_month(month2)

        async def _month_summary(c, month):
            start, end = _month_date_range(month)
            cur = await c.execute(
                """SELECT category, SUM(amount) as total FROM expenses
                   WHERE date BETWEEN %s AND %s GROUP BY category""",
                (start, end)
            )
            return {r[0]: r[1] for r in await cur.fetchall()}

        async with pool.connection() as c:
            s1 = await _month_summary(c, month1)
            s2 = await _month_summary(c, month2)

            all_cats = sorted(set(list(s1.keys()) + list(s2.keys())))
            comparison = []
            for cat in all_cats:
                v1 = s1.get(cat, 0)
                v2 = s2.get(cat, 0)
                diff = v2 - v1
                comparison.append({
                    "category": cat,
                    month1: round(v1, 2), month2: round(v2, 2),
                    "difference": round(diff, 2),
                    "change": f"{'+' if diff >= 0 else ''}{diff:.2f}"
                })

            total1 = sum(s1.values())
            total2 = sum(s2.values())
            return {
                "months": [month1, month2],
                "total_month1": round(total1, 2),
                "total_month2": round(total2, 2),
                "total_difference": round(total2 - total1, 2),
                "by_category": comparison
            }
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def dashboard(month: str) -> dict:
    """Get a complete financial snapshot for a month.

    Returns total spent, daily average, top categories, budget status,
    and recent expenses.

    Args:
        month: Month in YYYY-MM format
    """
    try:
        _validate_month(month)
        start, end = _month_date_range(month)
        year, mon = int(month[:4]), int(month[5:7])
        days_in_month = monthrange(year, mon)[1]

        async with pool.connection() as c:
            # Total & count
            cur = await c.execute(
                "SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM expenses WHERE date BETWEEN %s AND %s",
                (start, end)
            )
            total, count = await cur.fetchone()

            # Top categories
            cur = await c.execute(
                """SELECT category, SUM(amount) as total, COUNT(*) as cnt
                   FROM expenses WHERE date BETWEEN %s AND %s
                   GROUP BY category ORDER BY total DESC LIMIT 5""",
                (start, end)
            )
            top_cats = [{"category": r[0], "total": round(r[1], 2), "count": r[2]}
                        for r in await cur.fetchall()]

            # Budget status
            cur = await c.execute("""
                SELECT b.category, b.monthly_limit, COALESCE(SUM(e.amount), 0) as spent
                FROM budgets b
                LEFT JOIN expenses e ON e.category = b.category AND e.date BETWEEN %s AND %s
                GROUP BY b.category
            """, (start, end))
            budgets = []
            for r in await cur.fetchall():
                pct = (r[2] / r[1] * 100) if r[1] > 0 else 0
                budgets.append({
                    "category": r[0], "limit": r[1],
                    "spent": round(r[2], 2), "percentage": round(pct, 1),
                    "over": r[2] > r[1]
                })

            # Recent 5 expenses
            cur = await c.execute(
                """SELECT id, date, amount, category, note FROM expenses
                   WHERE date BETWEEN %s AND %s ORDER BY date DESC, id DESC LIMIT 5""",
                (start, end)
            )
            cols = [d[0] for d in cur.description]
            recent = [dict(zip(cols, r)) for r in await cur.fetchall()]

            # Income for the month
            cur = await c.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM income WHERE date BETWEEN %s AND %s",
                (start, end)
            )
            total_income = (await cur.fetchone())[0]

            return {
                "month": month,
                "total_spent": round(total, 2),
                "total_income": round(total_income, 2),
                "savings": round(total_income - total, 2),
                "expense_count": count,
                "daily_average": round(total / days_in_month, 2),
                "top_categories": top_cats,
                "budget_status": budgets,
                "recent_expenses": recent
            }
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def category_trend(category: str, months: int = 6) -> list | dict:
    """Show spending trend for a category over recent months.

    Args:
        category: The category to analyze
        months: Number of past months to include (default 6)
    """
    try:
        if months < 1 or months > 24:
            return {"status": "error", "message": "Months must be between 1 and 24."}

        today = datetime.now()
        results = []

        async with pool.connection() as c:
            for i in range(months - 1, -1, -1):
                # Calculate month offset
                target_month = today.month - i
                target_year = today.year
                while target_month <= 0:
                    target_month += 12
                    target_year -= 1
                month_str = f"{target_year:04d}-{target_month:02d}"
                start, end = _month_date_range(month_str)

                cur = await c.execute(
                    """SELECT COALESCE(SUM(amount), 0), COUNT(*)
                       FROM expenses WHERE category = %s AND date BETWEEN %s AND %s""",
                    (category, start, end)
                )
                total, count = await cur.fetchone()
                results.append({
                    "month": month_str, "total": round(total, 2), "count": count
                })

        return {"category": category, "trend": results}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


# ═══════════════════════════════════════════════════════════════════════
#  PHASE 7: EXPORT & CATEGORY MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def export_csv(start_date: str, end_date: str) -> dict:
    """Export expenses as CSV text for a date range.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
    """
    try:
        _validate_date(start_date)
        _validate_date(end_date)

        async with pool.connection() as c:
            cur = await c.execute(
                """SELECT id, date, amount, category, subcategory, note, payment_method
                   FROM expenses WHERE date BETWEEN %s AND %s
                   ORDER BY date ASC, id ASC""",
                (start_date, end_date)
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(cols)
        writer.writerows(rows)

        return {
            "status": "success",
            "row_count": len(rows),
            "csv_data": output.getvalue()
        }
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def add_category(name: str) -> dict:
    """Add a custom category to the categories list.

    Args:
        name: The category name to add
    """
    if not name.strip():
        return {"status": "error", "message": "Category name cannot be empty."}
    categories = _load_categories()
    name = name.strip()
    if name in categories:
        return {"status": "error", "message": f"Category '{name}' already exists."}
    categories.append(name)
    _save_categories(categories)
    return {"status": "success", "message": f"Category '{name}' added.", "categories": sorted(categories)}


@mcp.tool()
async def remove_category(name: str) -> dict:
    """Remove a category from the categories list.

    Args:
        name: The category name to remove
    """
    categories = _load_categories()
    if name not in categories:
        return {"status": "error", "message": f"Category '{name}' not found."}
    categories.remove(name)
    _save_categories(categories)
    return {"status": "success", "message": f"Category '{name}' removed.", "categories": sorted(categories)}


# ═══════════════════════════════════════════════════════════════════════
#  PHASE 8: INCOME TRACKING
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def add_income(
    date: str, amount: float, source: str, note: str = ""
) -> dict:
    """Log an income entry.

    Args:
        date: Date in YYYY-MM-DD format
        amount: Income amount (must be positive)
        source: Income source (e.g. Salary, Freelance, Investment)
        note: Optional note
    """
    try:
        _validate_date(date)
        _validate_amount(amount)
        if not source.strip():
            return {"status": "error", "message": "Source cannot be empty."}

        async with pool.connection() as c:
            cur = await c.execute(
                "INSERT INTO income(date, amount, source, note) VALUES (%s,%s,%s,%s) RETURNING id",
                (date, amount, source.strip(), note.strip())
            )
            row = await cur.fetchone()
            income_id = row[0] if row else None
            await c.commit()
            return {
                "status": "success", "id": income_id,
                "message": f"Income of ₹{amount:.2f} from '{source}' on {date}"
            }
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def list_income(start_date: str, end_date: str) -> list | dict:
    """List income entries within a date range.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
    """
    try:
        _validate_date(start_date)
        _validate_date(end_date)
        async with pool.connection() as c:
            cur = await c.execute(
                """SELECT id, date, amount, source, note
                   FROM income WHERE date BETWEEN %s AND %s
                   ORDER BY date DESC, id DESC""",
                (start_date, end_date)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def savings_summary(month: str) -> dict:
    """Calculate savings for a month (income minus expenses).

    Args:
        month: Month in YYYY-MM format
    """
    try:
        _validate_month(month)
        start, end = _month_date_range(month)

        async with pool.connection() as c:
            # Total income
            cur = await c.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM income WHERE date BETWEEN %s AND %s",
                (start, end)
            )
            total_income = (await cur.fetchone())[0]

            # Income by source
            cur = await c.execute(
                """SELECT source, SUM(amount) as total FROM income
                   WHERE date BETWEEN %s AND %s GROUP BY source ORDER BY total DESC""",
                (start, end)
            )
            income_breakdown = [{"source": r[0], "amount": round(r[1], 2)}
                                for r in await cur.fetchall()]

            # Total expenses
            cur = await c.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN %s AND %s",
                (start, end)
            )
            total_expenses = (await cur.fetchone())[0]

            savings = total_income - total_expenses
            rate = (savings / total_income * 100) if total_income > 0 else 0

            return {
                "month": month,
                "total_income": round(total_income, 2),
                "total_expenses": round(total_expenses, 2),
                "savings": round(savings, 2),
                "savings_rate": f"{rate:.1f}%",
                "income_breakdown": income_breakdown
            }
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def update_income(
    id: int, date: str = None, amount: float = None,
    source: str = None, note: str = None
) -> dict:
    """Update an existing income entry. Only provided fields are updated.

    Args:
        id: The income ID to update
        date: New date in YYYY-MM-DD format (optional)
        amount: New amount (optional, must be positive)
        source: New source (optional)
        note: New note (optional)
    """
    try:
        updates = []
        params = []
        if date is not None:
            _validate_date(date)
            updates.append("date = %s")
            params.append(date)
        if amount is not None:
            _validate_amount(amount)
            updates.append("amount = %s")
            params.append(amount)
        if source is not None:
            if not source.strip():
                return {"status": "error", "message": "Source cannot be empty."}
            updates.append("source = %s")
            params.append(source.strip())
        if note is not None:
            updates.append("note = %s")
            params.append(note.strip())

        if not updates:
            return {"status": "error", "message": "No fields to update."}

        params.append(id)
        async with pool.connection() as c:
            cur = await c.execute(
                f"UPDATE income SET {', '.join(updates)} WHERE id = %s", params
            )
            await c.commit()
            if cur.rowcount == 0:
                return {"status": "error", "message": f"Income with id {id} not found."}
            return {"status": "success", "message": f"Income {id} updated."}
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


@mcp.tool()
async def delete_income(id: int) -> dict:
    """Delete an income entry by its ID.

    Args:
        id: The income ID to delete
    """
    try:
        async with pool.connection() as c:
            cur = await c.execute("DELETE FROM income WHERE id = %s", (id,))
            await c.commit()
            if cur.rowcount == 0:
                return {"status": "error", "message": f"Income with id {id} not found."}
            return {"status": "success", "message": f"Income {id} deleted."}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


# ═══════════════════════════════════════════════════════════════════════
#  PHASE 10: BULK IMPORT
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def import_expenses_csv(csv_data: str, has_header: bool = True) -> dict:
    """Import expenses in bulk from a CSV string.
    
    Columns expected: date, amount, category, subcategory, note, payment_method
    If a column is missing, it will use defaults (Cash for payment_method).
    
    Args:
        csv_data: The CSV text data
        has_header: True if the first row is a header (will be skipped)
    """
    try:
        f = io.StringIO(csv_data.strip())
        reader = csv.reader(f)
        
        imported = 0
        skipped = 0
        errors = []
        total_amount = 0.0
        
        rows = list(reader)
        if has_header and rows:
            rows = rows[1:]
            
        async with pool.connection() as c:
            for i, row in enumerate(rows):
                line_num = i + (2 if has_header else 1)
                if not row or all(not x.strip() for x in row):
                    continue
                
                try:
                    while len(row) < 6:
                        row.append("")
                        
                    date = _validate_date(row[0].strip())
                    amount = _validate_amount(float(row[1].strip()))
                    category = row[2].strip()
                    if not category:
                        raise ValueError("Category cannot be empty")
                        
                    subcategory = row[3].strip()
                    note = row[4].strip()
                    
                    pm = row[5].strip()
                    if not pm:
                        pm = "Cash"
                    else:
                        pm = _validate_payment_method(pm)
                        
                    await c.execute(
                        "INSERT INTO expenses(date, amount, category, subcategory, note, payment_method) VALUES (%s,%s,%s,%s,%s,%s)",
                        (date, amount, category, subcategory, note, pm)
                    )
                    imported += 1
                    total_amount += amount
                except Exception as e:
                    skipped += 1
                    errors.append(f"Row {line_num}: {str(e)}")
                    
            await c.commit()
            
        return {
            "status": "success",
            "imported": imported,
            "skipped": skipped,
            "total_amount": round(total_amount, 2),
            "errors": errors[:10]
        }
    except Exception as e:
        return {"status": "error", "message": f"Error during import: {str(e)}"}


# ═══════════════════════════════════════════════════════════════════════
#  CHARTS — Server-side chart generation with matplotlib
# ═══════════════════════════════════════════════════════════════════════

# ─── Chart Theme ──────────────────────────────────────────────────────

CHART_COLORS = [
    "#6366f1", "#f43f5e", "#10b981", "#f59e0b", "#3b82f6",
    "#8b5cf6", "#ec4899", "#14b8a6", "#ef4444", "#06b6d4",
    "#84cc16", "#e879f9", "#22d3ee", "#fb923c", "#a3e635"
]

def _setup_chart_style():
    """Apply a premium dark theme to matplotlib charts."""
    plt.style.use("dark_background")
    plt.rcParams.update({
        "figure.facecolor": "#1a1a2e",
        "axes.facecolor": "#16213e",
        "axes.edgecolor": "#334155",
        "axes.labelcolor": "#e2e8f0",
        "axes.grid": True,
        "grid.color": "#334155",
        "grid.alpha": 0.4,
        "text.color": "#e2e8f0",
        "xtick.color": "#94a3b8",
        "ytick.color": "#94a3b8",
        "font.size": 11,
        "axes.titlesize": 14,
        "axes.titleweight": "bold",
        "figure.titlesize": 16,
        "figure.titleweight": "bold",
        "legend.facecolor": "#1e293b",
        "legend.edgecolor": "#475569",
        "legend.fontsize": 9,
    })


def _fig_to_image_content(fig) -> ImageContent:
    """Convert a matplotlib figure to MCP ImageContent (base64 PNG)."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("utf-8")
    return ImageContent(type="image", data=b64, mimeType="image/png")


def _rupee_formatter(x, pos):
    """Format axis values as ₹ amounts."""
    if x >= 1000:
        return f"₹{x/1000:.1f}k"
    return f"₹{x:.0f}"


# ─── Chart Tools ──────────────────────────────────────────────────────

@mcp.tool()
async def chart_spending_pie(month: str) -> ImageContent:
    """Generate a pie chart showing spending breakdown by category for a month.

    Args:
        month: Month in YYYY-MM format (e.g. 2026-09)
    """
    try:
        _validate_month(month)
        start, end = _month_date_range(month)

        async with pool.connection() as c:
            cur = await c.execute(
                """SELECT category, SUM(amount) as total
                   FROM expenses WHERE date BETWEEN %s AND %s
                   GROUP BY category ORDER BY total DESC""",
                (start, end)
            )
            rows = await cur.fetchall()

        if not rows:
            return ImageContent(type="image", data="", mimeType="text/plain")

        categories = [r[0] for r in rows]
        amounts = [r[1] for r in rows]
        colors = CHART_COLORS[:len(categories)]

        _setup_chart_style()
        fig, ax = plt.subplots(figsize=(8, 6))
        fig.patch.set_facecolor("#1a1a2e")

        wedges, texts, autotexts = ax.pie(
            amounts, labels=None, autopct="%1.1f%%",
            colors=colors, startangle=140,
            pctdistance=0.8, wedgeprops={"edgecolor": "#1a1a2e", "linewidth": 2}
        )
        for t in autotexts:
            t.set_fontsize(9)
            t.set_color("white")
            t.set_fontweight("bold")

        ax.legend(
            wedges, [f"{c}  ₹{a:,.0f}" for c, a in zip(categories, amounts)],
            loc="center left", bbox_to_anchor=(1.05, 0.5)
        )

        total = sum(amounts)
        ax.set_title(f"Spending Breakdown — {month}\nTotal: ₹{total:,.2f}", pad=20)

        return _fig_to_image_content(fig)
    except ValueError as e:
        return ImageContent(type="image", data="", mimeType="text/plain")


@mcp.tool()
async def chart_monthly_trend(months: int = 6) -> ImageContent:
    """Generate a line chart showing total spending trend over recent months.

    Args:
        months: Number of past months to plot (default 6, max 24)
    """
    try:
        months = min(max(months, 2), 24)
        today = datetime.now()
        labels = []
        totals = []

        async with pool.connection() as c:
            for i in range(months - 1, -1, -1):
                target_month = today.month - i
                target_year = today.year
                while target_month <= 0:
                    target_month += 12
                    target_year -= 1
                month_str = f"{target_year:04d}-{target_month:02d}"
                start, end = _month_date_range(month_str)
                cur = await c.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN %s AND %s",
                    (start, end)
                )
                total = (await cur.fetchone())[0]
                labels.append(month_str)
                totals.append(total)

        _setup_chart_style()
        fig, ax = plt.subplots(figsize=(10, 5))

        ax.plot(labels, totals, color="#6366f1", linewidth=2.5, marker="o",
                markersize=8, markerfacecolor="#818cf8", markeredgecolor="white",
                markeredgewidth=1.5, zorder=5)
        ax.fill_between(labels, totals, alpha=0.15, color="#6366f1")

        # Annotate each point
        for i, (label, val) in enumerate(zip(labels, totals)):
            ax.annotate(f"₹{val:,.0f}", (label, val),
                        textcoords="offset points", xytext=(0, 14),
                        ha="center", fontsize=9, color="#c7d2fe", fontweight="bold")

        ax.set_xlabel("Month")
        ax.set_ylabel("Total Spending")
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(_rupee_formatter))
        ax.set_title(f"Monthly Spending Trend — Last {months} Months", pad=15)
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()

        return _fig_to_image_content(fig)
    except Exception as e:
        return ImageContent(type="image", data="", mimeType="text/plain")


@mcp.tool()
async def chart_category_bars(month: str) -> ImageContent:
    """Generate a horizontal bar chart of spending by category for a month.

    Args:
        month: Month in YYYY-MM format (e.g. 2026-09)
    """
    try:
        _validate_month(month)
        start, end = _month_date_range(month)

        async with pool.connection() as c:
            cur = await c.execute(
                """SELECT category, SUM(amount) as total
                   FROM expenses WHERE date BETWEEN %s AND %s
                   GROUP BY category ORDER BY total ASC""",
                (start, end)
            )
            rows = await cur.fetchall()

        if not rows:
            return ImageContent(type="image", data="", mimeType="text/plain")

        categories = [r[0] for r in rows]
        amounts = [r[1] for r in rows]
        colors = CHART_COLORS[:len(categories)]
        colors.reverse()

        _setup_chart_style()
        fig, ax = plt.subplots(figsize=(10, max(4, len(categories) * 0.6)))

        bars = ax.barh(categories, amounts, color=colors, edgecolor="#1a1a2e",
                       linewidth=1, height=0.6)

        # Add value labels on bars
        for bar, amt in zip(bars, amounts):
            ax.text(bar.get_width() + max(amounts) * 0.02, bar.get_y() + bar.get_height() / 2,
                    f"₹{amt:,.0f}", va="center", fontsize=10, color="#e2e8f0", fontweight="bold")

        ax.set_xlabel("Amount Spent")
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(_rupee_formatter))
        ax.set_title(f"Spending by Category — {month}", pad=15)
        plt.tight_layout()

        return _fig_to_image_content(fig)
    except ValueError as e:
        return ImageContent(type="image", data="", mimeType="text/plain")


@mcp.tool()
async def chart_comparison_bars(month1: str, month2: str) -> ImageContent:
    """Generate a grouped (multi) bar chart comparing spending across two months.

    Args:
        month1: First month in YYYY-MM format
        month2: Second month in YYYY-MM format
    """
    try:
        _validate_month(month1)
        _validate_month(month2)

        async def _get_cat_totals(c, month):
            start, end = _month_date_range(month)
            cur = await c.execute(
                """SELECT category, SUM(amount) as total
                   FROM expenses WHERE date BETWEEN %s AND %s
                   GROUP BY category""",
                (start, end)
            )
            return {r[0]: r[1] for r in await cur.fetchall()}

        async with pool.connection() as c:
            d1 = await _get_cat_totals(c, month1)
            d2 = await _get_cat_totals(c, month2)

        all_cats = sorted(set(list(d1.keys()) + list(d2.keys())))
        if not all_cats:
            return ImageContent(type="image", data="", mimeType="text/plain")

        v1 = [d1.get(cat, 0) for cat in all_cats]
        v2 = [d2.get(cat, 0) for cat in all_cats]

        import numpy as np
        x = np.arange(len(all_cats))
        width = 0.35

        _setup_chart_style()
        fig, ax = plt.subplots(figsize=(max(8, len(all_cats) * 1.2), 6))

        bars1 = ax.bar(x - width / 2, v1, width, label=month1, color="#6366f1",
                       edgecolor="#1a1a2e", linewidth=1)
        bars2 = ax.bar(x + width / 2, v2, width, label=month2, color="#f43f5e",
                       edgecolor="#1a1a2e", linewidth=1)

        # Add value labels
        for bar in bars1:
            if bar.get_height() > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                        f"₹{bar.get_height():,.0f}", ha="center", va="bottom",
                        fontsize=8, color="#a5b4fc")
        for bar in bars2:
            if bar.get_height() > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                        f"₹{bar.get_height():,.0f}", ha="center", va="bottom",
                        fontsize=8, color="#fda4af")

        ax.set_xticks(x)
        ax.set_xticklabels(all_cats, rotation=45, ha="right")
        ax.set_ylabel("Amount Spent")
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(_rupee_formatter))
        ax.set_title(f"Month Comparison — {month1} vs {month2}", pad=15)
        ax.legend()
        plt.tight_layout()

        return _fig_to_image_content(fig)
    except ValueError as e:
        return ImageContent(type="image", data="", mimeType="text/plain")


@mcp.tool()
async def chart_daily_histogram(month: str) -> ImageContent:
    """Generate a histogram/bar chart of daily spending for a month.

    Shows spending amount for each day, highlighting high-spend days.

    Args:
        month: Month in YYYY-MM format (e.g. 2026-09)
    """
    try:
        _validate_month(month)
        start, end = _month_date_range(month)
        year, mon = int(month[:4]), int(month[5:7])
        days_in_month = monthrange(year, mon)[1]

        async with pool.connection() as c:
            cur = await c.execute(
                """SELECT date, SUM(amount) as total
                   FROM expenses WHERE date BETWEEN %s AND %s
                   GROUP BY date ORDER BY date""",
                (start, end)
            )
            rows = await cur.fetchall()

        # Build full day-by-day data
        daily = {}
        for r in rows:
            day = int(r[0].split("-")[2])
            daily[day] = r[1]

        days = list(range(1, days_in_month + 1))
        amounts = [daily.get(d, 0) for d in days]

        if not any(amounts):
            return ImageContent(type="image", data="", mimeType="text/plain")

        avg = sum(amounts) / days_in_month
        colors = ["#f43f5e" if a > avg * 1.5 else "#6366f1" for a in amounts]

        _setup_chart_style()
        fig, ax = plt.subplots(figsize=(max(10, days_in_month * 0.4), 5))

        ax.bar(days, amounts, color=colors, edgecolor="#1a1a2e", linewidth=0.5, width=0.8)
        ax.axhline(y=avg, color="#f59e0b", linestyle="--", linewidth=1.5,
                   label=f"Daily Avg: ₹{avg:,.0f}", alpha=0.8)

        ax.set_xlabel("Day of Month")
        ax.set_ylabel("Amount Spent")
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(_rupee_formatter))
        ax.set_xticks(days)
        ax.set_title(f"Daily Spending — {month}", pad=15)
        ax.legend(loc="upper right")
        plt.tight_layout()

        return _fig_to_image_content(fig)
    except ValueError as e:
        return ImageContent(type="image", data="", mimeType="text/plain")


@mcp.tool()
async def chart_category_stacked(months: int = 6) -> ImageContent:
    """Generate a stacked bar chart showing category breakdown over recent months.

    Each bar represents a month, with colored segments for each category.

    Args:
        months: Number of past months to plot (default 6, max 12)
    """
    try:
        months = min(max(months, 2), 12)
        today = datetime.now()
        month_labels = []
        all_data = {}  # {category: [amounts per month]}

        async with pool.connection() as c:
            for i in range(months - 1, -1, -1):
                target_month = today.month - i
                target_year = today.year
                while target_month <= 0:
                    target_month += 12
                    target_year -= 1
                month_str = f"{target_year:04d}-{target_month:02d}"
                month_labels.append(month_str)
                start, end = _month_date_range(month_str)

                cur = await c.execute(
                    """SELECT category, SUM(amount) as total
                       FROM expenses WHERE date BETWEEN %s AND %s
                       GROUP BY category""",
                    (start, end)
                )
                month_data = {r[0]: r[1] for r in await cur.fetchall()}
                for cat, total in month_data.items():
                    if cat not in all_data:
                        all_data[cat] = [0] * months
                    idx = months - 1 - i
                    all_data[cat][idx] = total

        if not all_data:
            return ImageContent(type="image", data="", mimeType="text/plain")

        # Sort categories by total spend (descending)
        sorted_cats = sorted(all_data.keys(), key=lambda c: sum(all_data[c]), reverse=True)

        import numpy as np
        x = np.arange(len(month_labels))

        _setup_chart_style()
        fig, ax = plt.subplots(figsize=(max(8, months * 1.5), 6))

        bottom = np.zeros(len(month_labels))
        for i, cat in enumerate(sorted_cats):
            vals = all_data[cat]
            color = CHART_COLORS[i % len(CHART_COLORS)]
            ax.bar(x, vals, bottom=bottom, label=cat, color=color,
                   edgecolor="#1a1a2e", linewidth=0.5, width=0.65)
            bottom += np.array(vals)

        ax.set_xticks(x)
        ax.set_xticklabels(month_labels, rotation=45, ha="right")
        ax.set_ylabel("Amount Spent")
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(_rupee_formatter))
        ax.set_title(f"Category Breakdown — Last {months} Months", pad=15)
        ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), borderaxespad=0)
        plt.tight_layout()

        return _fig_to_image_content(fig)
    except Exception as e:
        return ImageContent(type="image", data="", mimeType="text/plain")


@mcp.tool()
async def chart_income_vs_expense(months: int = 6) -> ImageContent:
    """Generate a grouped bar chart comparing Income vs Expenses over recent months.

    Args:
        months: Number of months to show (default 6, max 24)
    """
    try:
        months = max(1, min(months, 24))
        today = datetime.now()
        month_labels = []
        income_vals = []
        expense_vals = []

        async with pool.connection() as c:
            for i in range(months - 1, -1, -1):
                target_month = today.month - i
                target_year = today.year
                while target_month <= 0:
                    target_month += 12
                    target_year -= 1
                month_str = f"{target_year:04d}-{target_month:02d}"
                month_labels.append(datetime(target_year, target_month, 1).strftime("%b %y"))
                
                start, end = _month_date_range(month_str)

                # Income
                cur = await c.execute("SELECT COALESCE(SUM(amount), 0) FROM income WHERE date BETWEEN %s AND %s", (start, end))
                income_vals.append((await cur.fetchone())[0])

                # Expenses
                cur = await c.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN %s AND %s", (start, end))
                expense_vals.append((await cur.fetchone())[0])

        _setup_chart_style()
        fig, ax = plt.subplots(figsize=(max(8, months * 1.2), 5))

        x = np.arange(len(month_labels))
        width = 0.35

        ax.bar(x - width/2, income_vals, width, label='Income', color='#10b981', edgecolor='#1a1a2e')
        ax.bar(x + width/2, expense_vals, width, label='Expenses', color='#ef4444', edgecolor='#1a1a2e')

        ax.set_xticks(x)
        ax.set_xticklabels(month_labels, rotation=45, ha="right")
        ax.set_ylabel("Amount")
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(_rupee_formatter))
        ax.set_title(f"Income vs Expenses — Last {months} Months", pad=15)
        ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), borderaxespad=0)
        plt.tight_layout()

        return _fig_to_image_content(fig)
    except Exception as e:
        return ImageContent(type="image", data="", mimeType="text/plain")


# ═══════════════════════════════════════════════════════════════════════
#  INFO TOOL
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def tracker_info() -> dict:
    """Get information about available tools and capabilities in the expense tracker."""
    return {
        "status": "success",
        "message": "Expense Tracker is fully operational.",
        "capabilities": [
            "Expense & Income CRUD (Create, Read, Update, Delete)",
            "Budget Tracking with Global Limits (Daily/Weekly/Monthly) & Multi-tier Alerts",
            "Payment Method Classification (Cash, UPI, Cards, etc.)",
            "Tagging System (many-to-many labels)",
            "Bulk Import via CSV",
            "Rich Interactive Charts (Pie, Bar, Trend, Income vs Expense)"
        ],
        "usage_tip": "Ask me to 'chart income vs expense for 6 months' or 'import these expenses from csv'."
    }


# ═══════════════════════════════════════════════════════════════════════
#  RESOURCES
# ═══════════════════════════════════════════════════════════════════════

@mcp.resource("expense:///categories", mime_type="application/json")
def categories_resource():
    """Serve the list of expense categories as JSON."""
    categories = _load_categories()
    return json.dumps({"categories": categories}, indent=2)


# ═══════════════════════════════════════════════════════════════════════
#  SERVER ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
    # mcp.run()  # Uncomment for STDIO transport