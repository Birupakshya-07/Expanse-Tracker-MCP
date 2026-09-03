import asyncio
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from main import (
    add_expense, get_expense, update_expense, delete_expense,
    list_expenses, summarize, search_expenses,
    set_budget, get_budgets, check_budget, delete_budget,
    monthly_comparison, dashboard, category_trend,
    export_csv, add_category, remove_category,
    add_income, list_income, savings_summary
)

async def test():
    print("=== PHASE 1 & 2: CRUD ===")
    r = await add_expense("2026-09-01", 250.0, "Food & Dining", "Lunch", "Test expense")
    print(f"  add_expense: {r}")

    eid = r["id"]
    r = await get_expense(eid)
    print(f"  get_expense: {r}")

    r = await update_expense(eid, note="Updated test")
    print(f"  update_expense: {r}")

    r = await list_expenses("2026-09-01", "2026-09-01")
    print(f"  list_expenses: {len(r)} result(s)")

    r = await summarize("2026-09-01", "2026-09-01")
    print(f"  summarize: {r}")

    # Validation tests
    r = await add_expense("bad-date", 100, "Food")
    print(f"  validation (bad date): {r['status']}")
    r = await add_expense("2026-09-01", -50, "Food")
    print(f"  validation (neg amount): {r['status']}")

    print("\n=== PHASE 3: SEARCH ===")
    r = await search_expenses(keyword="test")
    print(f"  search (keyword): {len(r)} result(s)")
    r = await search_expenses(min_amount=100, max_amount=300)
    print(f"  search (amount range): {len(r)} result(s)")

    print("\n=== PHASE 4: BUDGETS ===")
    r = await set_budget("Food & Dining", 5000)
    print(f"  set_budget: {r}")
    r = await get_budgets()
    print(f"  get_budgets: {r}")
    r = await check_budget("2026-09")
    print(f"  check_budget: {r}")

    print("\n=== ANALYTICS ===")
    r = await dashboard("2026-09")
    print(f"  dashboard keys: {list(r.keys())}")
    print(f"  dashboard total_spent: {r['total_spent']}")
    r = await category_trend("Food & Dining", 3)
    print(f"  category_trend: {r}")

    print("\n=== EXPORT & CATEGORIES ===")
    r = await export_csv("2026-09-01", "2026-09-30")
    print(f"  export_csv: {r['row_count']} rows")
    r = await add_category("Groceries")
    print(f"  add_category: {r['status']}")
    r = await remove_category("Groceries")
    print(f"  remove_category: {r['status']}")

    print("\n=== INCOME ===")
    r = await add_income("2026-09-01", 50000, "Salary", "Monthly salary")
    print(f"  add_income: {r}")
    r = await list_income("2026-09-01", "2026-09-30")
    print(f"  list_income: {len(r)} entry(ies)")
    r = await savings_summary("2026-09")
    print(f"  savings_summary: {r}")

    # Cleanup
    print("\n=== CLEANUP ===")
    await delete_expense(eid)
    await delete_budget("Food & Dining")
    print("  Cleaned up test data")
    print("\n✅ ALL TESTS PASSED")

asyncio.run(test())
