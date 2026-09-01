#!/usr/bin/env python3
"""
Northwind Cloud — step 3: run the SQL library against the warehouse.

Each sql/Q*.sql answers exactly one question the CEO asked. Results are written
to outputs/tables/<name>.csv and the headline ones are printed. Two integrity
gates run here and abort the pipeline on failure:

  * Q02 ARR bridge residual must be 0.00 in every month.
  * Q04 cohort triangle must foot to total MRR in every month.

SQLite is not compiled with the math extension in every distribution, so
power()/ln()/exp() are registered from Python. They behave identically to the
SQL standard functions; registering them here keeps the .sql files portable.
"""
from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "warehouse" / "northwind.db"
SQLDIR = ROOT / "sql"
OUT = ROOT / "outputs" / "tables"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.create_function("power", 2, lambda a, b: None if a is None or b is None
                        else float(a) ** float(b))
    con.create_function("ln", 1, lambda a: None if a is None or a <= 0 else math.log(a))
    con.create_function("exp", 1, lambda a: None if a is None else math.exp(a))
    con.create_function("sqrt", 1, lambda a: None if a is None or a < 0 else math.sqrt(a))
    return con


def show(df: pd.DataFrame, cols=None, n=None, title=""):
    if title:
        print(f"\n--- {title} " + "-" * max(0, 74 - len(title)))
    d = df[cols] if cols else df
    if n:
        d = d.tail(n) if n > 0 else d.head(-n)
    print(d.to_string(index=False))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    con = connect()
    results: dict[str, pd.DataFrame] = {}
    failures: list[str] = []

    for f in sorted(SQLDIR.glob("Q*.sql")):
        sql = f.read_text()
        try:
            df = pd.read_sql(sql, con)
        except Exception as exc:                                # noqa: BLE001
            print(f"!! {f.name} FAILED: {exc}")
            failures.append(f.name)
            continue
        name = f.stem
        results[name] = df
        df.to_csv(OUT / f"{name}.csv", index=False)
        print(f"{f.name:<42s} -> {len(df):>5,d} rows")

    if failures:
        con.close()
        print("\nqueries failed:", failures)
        return 1

    # ---- integrity gate 1: ARR bridge -----------------------------------
    br = results["Q02_arr_bridge"]
    worst = br.residual_arr_must_be_zero.abs().max()
    print(f"\nGATE 1  ARR bridge max |residual| = ${worst:,.4f}  "
          f"-> {'PASS' if worst < 0.005 else 'FAIL'}")
    if worst >= 0.005:
        print(br[br.residual_arr_must_be_zero.abs() >= 0.005].to_string(index=False))
        failures.append("ARR bridge")

    # ---- integrity gate 2: cohort reconciliation -------------------------
    rec = results["Q04_cohort_reconciliation"]
    bad = rec[rec.status != "PASS"]
    print(f"GATE 2  Cohort-to-total reconciliation: "
          f"{len(rec) - len(bad)}/{len(rec)} months PASS "
          f"-> {'PASS' if bad.empty else 'FAIL'}")
    if not bad.empty:
        print(bad.to_string(index=False))
        failures.append("cohort reconciliation")

    # ---- headline output --------------------------------------------------
    show(results["Q01_executive_summary"], n=6, title="Q01 Executive summary (last 6 months)")
    show(br, n=4, title="Q02 ARR bridge (last 4 months, $ ARR)")
    show(results["Q05_nrr_grr_by_month"], n=4, title="Q05 NRR / GRR, trailing 12m")

    q6 = results["Q06_nrr_grr_by_segment"]
    show(q6[q6.as_of_month == q6.as_of_month.max()],
         title="Q06 NRR / GRR by segment, as at the latest month")

    show(results["Q07_cac_ltv_payback_by_channel"],
         cols=["channel", "n_wins", "avg_acv", "cac", "cac_payback_months",
               "monthly_logo_churn_pct", "monthly_nrr_pct", "ltv_cac_a",
               "ltv_cac_b", "ltv_cac_c"],
         title="Q07 Channel unit economics, trailing 12m")

    q8 = results["Q08_channel_economics_cuts"]
    show(q8[q8.cut_type == "by_segment"],
         cols=["channel", "segment", "n_wins", "caveat", "avg_acv", "cac_equal_split",
               "payback_months", "payback_months_effort_wtd"],
         title="Q08 Channel x segment payback, trailing 12m")

    show(results["Q09_unit_economics_by_segment"],
         cols=["segment", "live_logos", "book_arr", "pct_of_arr", "new_logos_12m",
               "avg_new_acv", "cac_equal_split", "cac_effort_weighted",
               "payback_months_entry", "payback_months_effort_wtd",
               "monthly_nrr_pct", "ltv_cac"],
         title="Q09 Unit economics by segment")

    q10 = results["Q10_channel_cohort_quality"]
    show(q10, title="Q10 Six-month revenue retention by channel and cohort half")

    q11 = results["Q11_sdr_productivity"]
    show(q11[q11.heads_added.fillna(0) != 0],
         cols=["month", "sdr_heads", "meetings", "meetings_per_rep", "heads_added",
               "marginal_meetings_per_added_rep"],
         title="Q11 SDR productivity at each headcount step")

    show(results["Q12_growth_decomposition"], title="Q12 Growth decomposition, trailing 12m")

    con.close()
    if failures:
        print("\nFAILURES:", failures)
        return 1
    print("\nAll queries ran; both integrity gates passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
