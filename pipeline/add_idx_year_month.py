#!/usr/bin/env python3
"""
add_idx_year_month.py — Loupe Master Execution Plan P1.5 (W26)

Adds a composite index on assets(year, month) to metadata.db to eliminate the
TEMP B-TREE in the /api/overview month-drill-down GROUP BY.

    CREATE INDEX IF NOT EXISTS idx_year_month ON assets(year, month);

The ONE sanctioned metadata.db schema write in the P1 plan. Idempotent.
Default (no flag) = read-only report. --apply creates; --rollback drops.

Rollback: python3 add_idx_year_month.py --rollback   (DROP INDEX IF EXISTS idx_year_month)

Author: Worker1 (delta) · 2026-07-21
"""
import argparse, os, sqlite3, sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.db")
IDX = "idx_year_month"
PLAN_SQL = "EXPLAIN QUERY PLAN SELECT year,month,COUNT(*) FROM assets GROUP BY year,month;"

def show(con, label):
    n = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    print(f"[{label}] rowcount={n}")
    for r in con.execute(PLAN_SQL).fetchall():
        print(f"[{label}] plan: {tuple(r)}")

def main():
    ap = argparse.ArgumentParser(description="P1.5 idx_year_month migration")
    ap.add_argument("--apply", action="store_true",
                    help="CREATE INDEX IF NOT EXISTS idx_year_month ON assets(year, month)")
    ap.add_argument("--rollback", action="store_true",
                    help="DROP INDEX IF EXISTS idx_year_month")
    a = ap.parse_args()
    if a.apply and a.rollback:
        sys.exit("error: choose only one of --apply / --rollback")
    if not (a.apply or a.rollback):
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        show(con, "report"); con.close(); return
    con = sqlite3.connect(DB)
    show(con, "before")
    if a.apply:
        con.execute(f"CREATE INDEX IF NOT EXISTS {IDX} ON assets(year, month)")
        con.commit(); print(f"[apply] CREATE INDEX IF NOT EXISTS {IDX} ON assets(year, month) — done")
    else:
        con.execute(f"DROP INDEX IF EXISTS {IDX}")
        con.commit(); print(f"[rollback] DROP INDEX IF EXISTS {IDX} — done")
    show(con, "after"); con.close()

if __name__ == "__main__":
    main()
