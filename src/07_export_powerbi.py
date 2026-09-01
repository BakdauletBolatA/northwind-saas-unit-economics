#!/usr/bin/env python3
"""
Northwind Cloud — step 7: Power BI star-schema extracts.

Exports a clean star to outputs/powerbi/: four dimensions, five facts, one
bridge. Column names are stable and are the contract that docs/POWERBI_GUIDE.md
and its DAX measures are written against — renaming a column here breaks the
measures, so don't.

Why a snowflaked dim_customer is avoided: Power BI's engine handles a wide
dimension far better than a chain of joins, and every attribute an analyst
wants to slice by (segment, channel, plan, cohort, imputation flags) belongs on
the customer row.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "warehouse" / "northwind.db"
OUT = ROOT / "outputs" / "powerbi"

EXPORTS: dict[str, str] = {
    # ------------------------------- dimensions -----------------------------
    "dim_date": """
        SELECT month_key AS MonthKey, month AS MonthLabel,
               month_end_date AS MonthEndDate, month_index AS MonthIndex,
               year AS Year, quarter AS Quarter, fiscal_period AS FiscalPeriod,
               month_num AS MonthNumber,
               CASE WHEN is_actual=1 THEN 'Actual' ELSE 'Forecast' END AS PeriodType
        FROM dim_date ORDER BY month_index""",

    "dim_customer": """
        SELECT c.customer_key AS CustomerKey, c.customer_id AS CustomerId,
               c.company_name AS CompanyName, c.segment AS Segment,
               c.acquisition_channel AS Channel, ch.channel_label AS ChannelLabel,
               ch.channel_group AS ChannelGroup, c.plan_code AS Plan,
               c.country AS Country, c.industry AS Industry,
               c.signup_month AS SignupMonth, c.cohort_month AS CohortMonth,
               CASE WHEN c.is_pre_window=1 THEN 'Pre-window (no CAC)'
                    ELSE 'In-window' END AS AcquisitionWindow,
               c.is_pre_window AS IsPreWindow,
               c.segment_imputed AS SegmentImputed,
               c.channel_unattributed AS ChannelUnattributed,
               c.plan_imputed AS PlanImputed,
               c.is_churned AS IsChurned,
               c.churn_month_observed AS ChurnMonth,
               c.first_month_arr AS EntryAcv
        FROM dim_customer c
        LEFT JOIN dim_channel ch ON ch.channel_code = c.acquisition_channel
        ORDER BY c.customer_key""",

    "dim_channel": """
        SELECT channel_key AS ChannelKey, channel_code AS Channel,
               channel_label AS ChannelLabel, channel_group AS ChannelGroup,
               sales_touch_weight AS SalesTouchWeight,
               is_attributable AS IsAttributable
        FROM dim_channel ORDER BY channel_key""",

    "dim_segment": """
        SELECT segment_key AS SegmentKey, segment_code AS Segment,
               gross_margin AS GrossMargin,
               sales_effort_index AS SalesEffortIndex,
               benchmark_monthly_logo_churn AS BenchmarkMonthlyLogoChurn,
               sort_order AS SortOrder
        FROM dim_segment ORDER BY sort_order""",

    # --------------------------------- facts --------------------------------
    "fact_subscription_month": """
        SELECT f.customer_key AS CustomerKey, f.month_key AS MonthKey,
               f.mrr AS Mrr, f.prev_mrr AS PriorMrr, f.mrr*12 AS Arr,
               f.seats AS Seats, f.seat_price AS SeatPrice,
               f.movement_type AS MovementType,
               f.movement_amount AS MovementMrr,
               f.movement_amount*12 AS MovementArr,
               f.tenure_months AS TenureMonths,
               f.outlier_flag AS OutlierFlag,
               CASE WHEN f.mrr>0 THEN 1 ELSE 0 END AS IsActive
        FROM fact_subscription_month f ORDER BY f.month_key, f.customer_key""",

    "fact_marketing_spend": """
        SELECT f.month_key AS MonthKey, f.channel_key AS ChannelKey,
               f.program_spend AS ProgramSpend, f.cost_per_lead AS CostPerLead,
               f.leads AS Leads, f.sqls AS Sqls
        FROM fact_marketing_spend f ORDER BY f.month_key, f.channel_key""",

    "fact_opex": """
        SELECT month_key AS MonthKey, cost_category AS CostCategory,
               cost_center AS CostCenter, gl_account AS GlAccount,
               amount AS Amount
        FROM fact_opex ORDER BY month_key""",

    "fact_headcount": """
        SELECT month_key AS MonthKey, role AS Role, headcount AS Headcount,
               loaded_cost_per_head AS LoadedCostPerHead,
               total_cost AS TotalCost, cost_category AS CostCategory
        FROM fact_headcount ORDER BY month_key, role""",

    "fact_credit_memo": """
        SELECT customer_key AS CustomerKey, month_key AS MonthKey,
               credit_amount AS CreditAmount
        FROM fact_credit_memo ORDER BY month_key""",

    # ------------------------------ supporting ------------------------------
    "bridge_cohort_age": """
        SELECT f.customer_key AS CustomerKey, f.month_key AS MonthKey,
               d.month_index - dc.month_index AS MonthsSinceCohort
        FROM fact_subscription_month f
        JOIN dim_date d  ON d.month_key = f.month_key
        JOIN dim_customer c ON c.customer_key = f.customer_key
        JOIN dim_date dc ON dc.month = c.cohort_month""",

    "ref_events": """
        SELECT event_id AS EventId, event_name AS EventName,
               start_month AS StartMonth, scope_type AS ScopeType,
               scope_value AS ScopeValue, description AS Description
        FROM ref_events ORDER BY event_id""",

    "etl_audit": """
        SELECT step_no AS StepNo, rule_id AS RuleId, rule_name AS RuleName,
               table_name AS TableName, rows_in AS RowsIn, rows_out AS RowsOut,
               rows_affected AS RowsAffected, severity AS Severity,
               rule_description AS RuleDescription
        FROM etl_audit ORDER BY step_no""",
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    manifest = []
    for name, sql in EXPORTS.items():
        df = pd.read_sql(sql, con)
        path = OUT / f"{name}.csv"
        df.to_csv(path, index=False, lineterminator="\n")
        manifest.append(dict(table=name, rows=len(df), columns=len(df.columns),
                             column_names="|".join(df.columns)))
        print(f"{name:<28s} {len(df):>7,d} rows  {len(df.columns):>2d} cols")
    man = pd.DataFrame(manifest)
    man.to_csv(OUT / "_schema_manifest.csv", index=False)
    con.close()
    print(f"\n{len(manifest)} tables -> {OUT.relative_to(ROOT)}")
    print("Column names are the contract for docs/POWERBI_GUIDE.md — do not rename.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
