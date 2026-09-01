#!/usr/bin/env python3
"""
Northwind Cloud — step 2: raw CSV -> SQLite star schema.

Every cleaning rule is numbered (R01..R13), applied in order, and logged to
etl_audit with rows in / rows out / rows affected. Anything discarded lands in
dq_quarantine with the reason, so nothing disappears silently.

Design notes worth arguing about, stated up front:

  * Credit memos are NOT netted into MRR. MRR is a recurring-revenue metric;
    a one-off back-dated credit is not a change in the recurring contract.
    They are held in fact_credit_memo and netted into *recognised revenue*,
    which is what the cash model uses. Conflating the two is the most common
    way a SaaS ARR bridge stops tying.
  * Missing acquisition_channel is never imputed. Attribution cannot be
    invented from revenue. Those customers go to an explicit 'unattributed'
    bucket and are excluded from every CAC denominator.
  * Missing segment IS imputed, from first-month ARR bands, and flagged. The
    band rule is stated in docs/CLEANING_RULES.md and is reversible.
  * Outliers are flagged, not removed, unless there is a documented mechanical
    cause. The only documented cause here is the billing migration (R02).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "warehouse" / "northwind.db"

SEGMENT_BANDS = [(0, 15_000, "SMB"), (15_000, 60_000, "MidMarket"),
                 (60_000, float("inf"), "Enterprise")]


class Audit:
    """Collects one row per cleaning rule, plus quarantined records."""

    def __init__(self):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.rows: list[dict] = []
        self.quarantine: list[dict] = []
        self._step = 0

    def log(self, rule_id, name, description, table, rows_in, rows_out,
            rows_affected=None, severity="info"):
        self._step += 1
        self.rows.append(dict(
            run_id=self.run_id, step_no=self._step, rule_id=rule_id,
            rule_name=name, rule_description=" ".join(description.split()),
            table_name=table, rows_in=int(rows_in), rows_out=int(rows_out),
            rows_affected=int(rows_affected if rows_affected is not None
                              else rows_in - rows_out),
            severity=severity))
        print(f"  {rule_id}  {name:<42s} in={rows_in:>7,d} out={rows_out:>7,d} "
              f"affected={self.rows[-1]['rows_affected']:>6,d}")

    def quarantine_rows(self, rule_id, table, df: pd.DataFrame, reason: str):
        for rec in df.to_dict("records"):
            self.quarantine.append(dict(
                run_id=self.run_id, rule_id=rule_id, source_table=table,
                reason=reason,
                record=json.dumps({k: (None if pd.isna(v) else v)
                                   for k, v in rec.items()}, default=str)))


def month_add(ms: str, n: int) -> str:
    y, m = (int(x) for x in ms.split("-"))
    t = y * 12 + (m - 1) + n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def build():
    cfg = yaml.safe_load(open(ROOT / "config" / "assumptions.yml"))
    meta = cfg["meta"]
    start, T, F = meta["history_start"], int(meta["history_months"]), int(meta["forecast_months"])
    months = [month_add(start, i) for i in range(T)]
    all_months = [month_add(start, i) for i in range(T + F)]
    a = Audit()

    print("ETL run", a.run_id)
    billing = pd.read_csv(RAW / "billing_raw.csv")
    customers = pd.read_csv(RAW / "customers.csv")
    spend = pd.read_csv(RAW / "marketing_spend.csv")
    opex = pd.read_csv(RAW / "opex_ledger.csv")
    headcount = pd.read_csv(RAW / "headcount.csv")
    events = pd.read_csv(RAW / "events_calendar.csv")

    n0 = len(billing)
    a.log("R01", "Load raw billing extract", "Source of record for revenue.",
          "billing_raw", n0, n0, 0)

    # ---- R02 orphan billing rows -----------------------------------------
    known = set(customers.customer_id)
    orphans = billing[~billing.customer_id.isin(known)]
    a.quarantine_rows("R02", "billing_raw", orphans,
                      "customer_id not present in the CRM customer master")
    billing = billing[billing.customer_id.isin(known)].copy()
    a.log("R02", "Quarantine orphan billing rows",
          """Billing lines whose customer_id has no row in the CRM export. They
          cannot be attributed to a segment or channel, so they are removed
          from the facts and held in dq_quarantine.""",
          "billing_raw", n0, len(billing), severity="warn")

    # ---- R03 split credit memos ------------------------------------------
    n_in = len(billing)
    memos = billing[billing.record_type == "credit_memo"].copy()
    billing = billing[billing.record_type == "recurring"].copy()
    a.log("R03", "Separate credit memos from recurring",
          """Credit memos are one-off adjustments, not changes to the recurring
          contract. They are excluded from MRR and held in fact_credit_memo,
          where they reduce recognised revenue only.""",
          "billing_raw", n_in, len(billing), len(memos))

    # ---- R04 negative / zero seat corruption -----------------------------
    n_in = len(billing)
    bad = billing[(billing.seats <= 0) | (billing.mrr_amount <= 0)]
    a.quarantine_rows("R04", "billing_raw", bad,
                      "recurring line with non-positive seats or MRR")
    billing = billing[(billing.seats > 0) & (billing.mrr_amount > 0)].copy()
    a.log("R04", "Quarantine non-positive recurring lines",
          """A recurring subscription line cannot have negative seats. These are
          data-entry corruption, not contraction — contraction is a *reduction*
          between two positive months and is derived at R09.""",
          "billing_raw", n_in, len(billing), severity="warn")

    # ---- R05 de-duplicate the billing-migration double post ---------------
    n_in = len(billing)
    billing = billing.sort_values(["subscription_id", "billing_month",
                                   "created_at", "billing_id"])
    dup_mask = billing.duplicated(subset=["subscription_id", "billing_month"], keep="first")
    a.quarantine_rows("R05", "billing_raw", billing[dup_mask],
                      "duplicate subscription-month line (billing engine migration)")
    dup_by_month = (billing[dup_mask].groupby("billing_month").size().to_dict())
    billing = billing[~dup_mask].copy()
    a.log("R05", "De-duplicate subscription-month lines",
          """One row per subscription per month, keeping the earliest created_at.
          This is the only outlier treatment applied anywhere in the pipeline
          with a documented mechanical cause: the February 2026 cut-over to the
          new billing engine double-posted invoices. Concentration by month:
          """ + json.dumps(dup_by_month),
          "billing_raw", n_in, len(billing), severity="warn")

    # ---- R06 customer attribute repair -----------------------------------
    n_in = len(customers)
    first_mrr = (billing.sort_values(["customer_id", "billing_month"])
                 .groupby("customer_id").mrr_amount.first())
    customers["first_month_arr"] = customers.customer_id.map(first_mrr) * 12.0

    miss_seg = customers.segment.isna() | (customers.segment.astype(str).str.strip() == "")
    def band(arr):
        if pd.isna(arr):
            return "SMB"
        for lo, hi, name in SEGMENT_BANDS:
            if lo <= arr < hi:
                return name
        return "Enterprise"
    customers["segment_imputed"] = miss_seg.astype(int)
    customers.loc[miss_seg, "segment"] = customers.loc[miss_seg, "first_month_arr"].map(band)
    a.log("R06", "Impute missing segment from first-month ARR",
          """Bands: <$15k SMB, $15k-$60k Mid-Market, >=$60k Enterprise. Flagged
          in dim_customer.segment_imputed so any analysis can exclude them.""",
          "customers", n_in, n_in, int(miss_seg.sum()))

    miss_ch = (customers.acquisition_channel.isna()
               | (customers.acquisition_channel.astype(str).str.strip() == ""))
    customers["channel_unattributed"] = miss_ch.astype(int)
    customers.loc[miss_ch, "acquisition_channel"] = "unattributed"
    a.log("R07", "Bucket missing channel as 'unattributed'",
          """Attribution is NOT imputed. Guessing a channel from revenue would
          feed a fabricated denominator straight into CAC. These customers are
          excluded from every channel CAC and LTV/CAC figure and the exclusion
          is reported alongside the result.""",
          "customers", n_in, n_in, int(miss_ch.sum()), severity="warn")

    miss_plan = (customers.plan_code.isna()
                 | (customers.plan_code.astype(str).str.strip() == ""))
    price_to_plan = {float(v["seat_price"]): k for k, v in cfg["plans"].items()}
    customers["plan_imputed"] = miss_plan.astype(int)
    customers.loc[miss_plan, "plan_code"] = (
        customers.loc[miss_plan, "seat_price"].map(
            lambda p: price_to_plan.get(round(float(p), 2), "Growth")))
    a.log("R08", "Impute missing plan from contracted seat price",
          "Seat price uniquely identifies the plan in the current rate card.",
          "customers", n_in, n_in, int(miss_plan.sum()))

    # ---- R09 build the dense subscription-month spine + movements ---------
    n_in = len(billing)
    b = billing[["customer_id", "billing_month", "seats", "seat_price",
                 "mrr_amount", "plan_code"]].copy()
    b["mi"] = b.billing_month.map(lambda m: months.index(m))
    b = b.sort_values(["customer_id", "mi"])

    frames = []
    for cid, g in b.groupby("customer_id", sort=True):
        lo, hi = int(g.mi.min()), int(g.mi.max())
        # extend one month past the last active month so churn is observable
        span = range(lo, min(hi + 2, T))
        s = g.set_index("mi").reindex(span)
        s["customer_id"] = cid
        s["mi"] = list(span)
        frames.append(s.reset_index(drop=True))
    fact = pd.concat(frames, ignore_index=True)
    fact["billing_month"] = fact.mi.map(lambda i: months[i])
    fact["mrr"] = fact.mrr_amount.fillna(0.0).round(2)
    fact["prev_mrr"] = fact.groupby("customer_id").mrr.shift().fillna(0.0).round(2)
    fact["is_first"] = fact.groupby("customer_id").cumcount() == 0

    pre = dict(zip(customers.customer_id, customers.is_pre_window))
    fact["is_pre_window"] = fact.customer_id.map(pre).astype(int)

    def classify(r):
        if r.is_first and r.mrr > 0:
            return "opening_balance" if r.is_pre_window == 1 else "new"
        if r.prev_mrr == 0 and r.mrr > 0:
            return "reactivation"
        if r.prev_mrr > 0 and r.mrr == 0:
            return "churn"
        if r.mrr > r.prev_mrr:
            return "expansion"
        if r.mrr < r.prev_mrr:
            return "contraction"
        return "flat"

    fact["movement_type"] = fact.apply(classify, axis=1)
    fact["movement_amount"] = (fact.mrr - fact.prev_mrr).round(2)
    fact.loc[fact.movement_type.isin(["new", "opening_balance"]), "movement_amount"] = \
        fact.loc[fact.movement_type.isin(["new", "opening_balance"]), "mrr"]
    fact = fact[~((fact.movement_type == "flat") & (fact.mrr == 0))]
    a.log("R09", "Derive dense subscription-month spine and movements",
          """Each customer gets a contiguous month spine from first to last
          active month, plus one trailing month so churn is an observable event
          rather than an absence. movement_type is one of opening_balance /
          new / expansion / contraction / churn / reactivation / flat and is
          derived purely from the month-over-month MRR level — never copied
          from the source system. This is what makes the ARR bridge an actual
          reconciliation.""",
          "fact_subscription_month", n_in, len(fact), len(fact) - n_in)

    # ---- R10 outlier scan (flag only) ------------------------------------
    moves = fact[fact.movement_type.isin(["expansion", "contraction"])]
    sd = moves.movement_amount.std()
    thresh = 5.0 * sd
    fact["outlier_flag"] = ((fact.movement_type.isin(["expansion", "contraction"]))
                            & (fact.movement_amount.abs() > thresh)).astype(int)
    a.log("R10", "Flag >5-sigma MRR movements (no removal)",
          f"""Threshold |delta MRR| > {thresh:,.0f} (5 x sd of expansion and
          contraction deltas). These are FLAGGED, not normalised: the only
          outlier with a documented mechanical cause is the billing migration,
          already handled at R05. Removing the rest would be curve-fitting, so
          they stay in the history and carry outlier_flag = 1.""",
          "fact_subscription_month", len(fact), len(fact),
          int(fact.outlier_flag.sum()), severity="warn")

    # ---- R11 spend ledger cross-foot -------------------------------------
    prog = (opex[opex.gl_account == "Marketing programmes"]
            .groupby(["month", "cost_center"]).amount.sum().rename("opex_amt"))
    mk = spend.set_index(["month", "channel"]).program_spend.rename("mkt_amt")
    j = pd.concat([prog, mk], axis=1).fillna(0.0)
    variance = float((j.opex_amt - j.mkt_amt).abs().sum())
    a.log("R11", "Cross-foot marketing spend: GL vs platform export",
          f"""Marketing-programme lines in opex_ledger must agree with
          marketing_spend by month x channel. Absolute variance =
          ${variance:,.2f}. A non-zero variance here means CAC is being built
          on a different spend number than the P&L.""",
          "fact_marketing_spend", len(j), len(j), 0,
          severity="info" if variance < 0.01 else "error")

    # ---- R12 pre-window attribution flag ---------------------------------
    n_pre = int((customers.is_pre_window == 1).sum())
    a.log("R12", "Flag pre-window customers as unattributable",
          f"""{n_pre} customers were acquired before the S&M ledger begins.
          They carry no acquisition cost and are excluded from every CAC, LTV
          and payback figure. Including them is the standard way CAC gets
          flattered — the denominator grows, the numerator does not.""",
          "dim_customer", len(customers), len(customers), n_pre, severity="warn")

    # ---- R13 revenue recognition -----------------------------------------
    memo_m = memos.groupby("billing_month").mrr_amount.sum()
    a.log("R13", "Recognised revenue = MRR less credit memos",
          f"""Total credit memos across the window: ${memo_m.sum():,.2f} over
          {len(memos)} lines. Recognised revenue feeds the cash model; MRR
          feeds the ARR bridge and retention metrics. They are different
          numbers on purpose.""",
          "fact_credit_memo", len(memos), len(memos), 0)

    # ======================= write the star schema ========================
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)

    # dim_date
    dim_date = pd.DataFrame({"month": all_months})
    dim_date["month_key"] = dim_date.month.str.replace("-", "").astype(int)
    dim_date["month_index"] = range(1, len(dim_date) + 1)
    dim_date["year"] = dim_date.month.str[:4].astype(int)
    dim_date["month_num"] = dim_date.month.str[5:].astype(int)
    dim_date["quarter"] = "Q" + ((dim_date.month_num - 1) // 3 + 1).astype(str)
    dim_date["fiscal_period"] = dim_date.year.astype(str) + "-" + dim_date.quarter
    dim_date["is_actual"] = (dim_date.month_index <= T).astype(int)
    dim_date["month_end_date"] = pd.to_datetime(
        dim_date.month + "-01").dt.to_period("M").dt.end_time.dt.date.astype(str)
    dim_date = dim_date[["month_key", "month", "month_index", "year", "quarter",
                         "month_num", "fiscal_period", "month_end_date", "is_actual"]]
    dim_date.to_sql("dim_date", con, index=False)

    # dim_channel
    ch_rows = []
    for k, v in cfg["channels"].items():
        ch_rows.append(dict(channel_code=k, channel_label=v["label"],
                            sales_touch_weight=float(v["sales_touch"]),
                            channel_group="Outbound" if k == "outbound_sdr"
                            else ("Partner" if k == "partner"
                                  else ("Paid" if k.startswith("paid") else "Inbound")),
                            is_attributable=1))
    ch_rows.append(dict(channel_code="unattributed", channel_label="Unattributed",
                        sales_touch_weight=0.0, channel_group="Unknown",
                        is_attributable=0))
    dim_channel = pd.DataFrame(ch_rows)
    dim_channel.insert(0, "channel_key", range(1, len(dim_channel) + 1))
    dim_channel.to_sql("dim_channel", con, index=False)

    # dim_segment
    effort = cfg["segment_sales_effort_index"]
    seg_rows = [dict(segment_code=k, gross_margin=float(v["gross_margin"]),
                     benchmark_monthly_logo_churn=float(v["base_monthly_logo_churn"]),
                     sales_effort_index=float(effort[k]), sort_order=i)
                for i, (k, v) in enumerate(cfg["segments"].items())]
    dim_segment = pd.DataFrame(seg_rows)
    dim_segment.insert(0, "segment_key", range(1, len(dim_segment) + 1))
    dim_segment.to_sql("dim_segment", con, index=False)

    # dim_plan
    dim_plan = pd.DataFrame([dict(plan_code=k, list_seat_price=float(v["seat_price"]),
                                  min_seats=int(v["min_seats"]),
                                  target_segment=v["segment"])
                             for k, v in cfg["plans"].items()])
    dim_plan.insert(0, "plan_key", range(1, len(dim_plan) + 1))
    dim_plan.to_sql("dim_plan", con, index=False)

    # dim_customer
    last_active = fact[fact.mrr > 0].groupby("customer_id").billing_month.max()
    first_active = fact[fact.mrr > 0].groupby("customer_id").billing_month.min()
    churn_obs = (fact[fact.movement_type == "churn"]
                 .groupby("customer_id").billing_month.min())
    dim_customer = customers.copy()
    dim_customer["first_active_month"] = dim_customer.customer_id.map(first_active)
    dim_customer["last_active_month"] = dim_customer.customer_id.map(last_active)
    dim_customer["churn_month_observed"] = dim_customer.customer_id.map(churn_obs)
    dim_customer["is_churned"] = dim_customer.churn_month_observed.notna().astype(int)
    dim_customer["cohort_month"] = dim_customer.first_active_month
    dim_customer["acq_cohort_month"] = dim_customer.signup_month
    dim_customer = dim_customer[dim_customer.customer_id.isin(fact.customer_id)]
    dim_customer = dim_customer[[
        "customer_id", "company_name", "segment", "segment_imputed",
        "acquisition_channel", "channel_unattributed", "plan_code", "plan_imputed",
        "seat_price", "country", "industry", "is_pre_window", "signup_month",
        "acq_cohort_month", "cohort_month", "first_active_month",
        "last_active_month", "churn_month_observed", "is_churned",
        "first_month_arr"]].sort_values("customer_id").reset_index(drop=True)
    dim_customer.insert(0, "customer_key", range(1, len(dim_customer) + 1))
    dim_customer.to_sql("dim_customer", con, index=False)

    ckey = dict(zip(dim_customer.customer_id, dim_customer.customer_key))
    mkey = dict(zip(dim_date.month, dim_date.month_key))
    chkey = dict(zip(dim_channel.channel_code, dim_channel.channel_key))

    # fact_subscription_month
    f = fact.copy()
    f["customer_key"] = f.customer_id.map(ckey)
    f["month_key"] = f.billing_month.map(mkey)
    f["seats"] = f.seats.fillna(0).astype(int)
    f["seat_price"] = f.seat_price.fillna(0.0).round(2)
    f["tenure_months"] = f.groupby("customer_id").cumcount() + 1
    f = f[["customer_key", "month_key", "seats", "seat_price", "mrr", "prev_mrr",
           "movement_type", "movement_amount", "tenure_months", "outlier_flag"]]
    f.to_sql("fact_subscription_month", con, index=False)

    # fact_credit_memo
    cm = memos.copy()
    cm["customer_key"] = cm.customer_id.map(ckey)
    cm["month_key"] = cm.billing_month.map(mkey)
    cm = cm[cm.customer_key.notna()]
    cm["customer_key"] = cm.customer_key.astype(int)
    cm[["customer_key", "month_key", "mrr_amount"]].rename(
        columns={"mrr_amount": "credit_amount"}).to_sql(
        "fact_credit_memo", con, index=False)

    # fact_marketing_spend
    ms = spend.copy()
    ms["month_key"] = ms.month.map(mkey)
    ms["channel_key"] = ms.channel.map(chkey)
    ms[["month_key", "channel_key", "program_spend", "cost_per_lead",
        "leads", "sqls"]].to_sql("fact_marketing_spend", con, index=False)

    # fact_opex
    ox = opex.copy()
    ox["month_key"] = ox.month.map(mkey)
    ox[["month_key", "cost_category", "cost_center", "gl_account", "amount",
        "note"]].to_sql("fact_opex", con, index=False)

    # fact_headcount
    hc = headcount.copy()
    hc["month_key"] = hc.month.map(mkey)
    hc[["month_key", "role", "headcount", "loaded_cost_per_head", "total_cost",
        "cost_category"]].to_sql("fact_headcount", con, index=False)

    # reference tables
    events.to_sql("ref_events", con, index=False)
    pd.DataFrame([dict(key=k, value=json.dumps(v))
                  for k, v in {**cfg["meta"], **cfg["treasury"]}.items()]
                 ).to_sql("ref_assumptions", con, index=False)
    pd.DataFrame(a.rows).to_sql("etl_audit", con, index=False)
    pd.DataFrame(a.quarantine).to_sql("dq_quarantine", con, index=False)

    # ---------------- analysis views --------------------------------------
    # Three thin views keep the SQL library readable. They contain joins and
    # sign conventions only — no business logic that would hide an assumption.
    views = [
        """
        CREATE VIEW v_customer_month AS
        SELECT d.month, d.month_index, d.month_key, d.year, d.quarter,
               c.customer_key, c.customer_id, c.segment, c.segment_imputed,
               c.acquisition_channel AS channel, c.channel_unattributed,
               c.plan_code, c.is_pre_window, c.signup_month, c.cohort_month,
               c.first_month_arr,
               f.seats, f.seat_price, f.mrr, f.prev_mrr, f.movement_type,
               f.movement_amount, f.tenure_months, f.outlier_flag,
               s.gross_margin
        FROM fact_subscription_month f
        JOIN dim_date     d ON d.month_key   = f.month_key
        JOIN dim_customer c ON c.customer_key = f.customer_key
        JOIN dim_segment  s ON s.segment_code = c.segment
        """,
        """
        CREATE VIEW v_pnl_month AS
        WITH rev AS (SELECT month_key, SUM(mrr) AS mrr
                     FROM fact_subscription_month GROUP BY month_key),
             cm  AS (SELECT month_key, SUM(credit_amount) AS credits
                     FROM fact_credit_memo GROUP BY month_key),
             ox  AS (SELECT month_key,
                       SUM(CASE WHEN cost_category='COGS' THEN amount ELSE 0 END) AS cogs,
                       SUM(CASE WHEN cost_category='SM'   THEN amount ELSE 0 END) AS sm,
                       SUM(CASE WHEN cost_category='RD'   THEN amount ELSE 0 END) AS rd,
                       SUM(CASE WHEN cost_category='GA'   THEN amount ELSE 0 END) AS ga
                     FROM fact_opex GROUP BY month_key)
        SELECT d.month, d.month_index, d.month_key,
               ROUND(COALESCE(rev.mrr,0), 2)                       AS mrr,
               ROUND(COALESCE(rev.mrr,0)*12, 2)                    AS arr,
               ROUND(COALESCE(cm.credits,0), 2)                    AS credit_memos,
               ROUND(COALESCE(rev.mrr,0)+COALESCE(cm.credits,0),2) AS recognised_revenue,
               ROUND(ox.cogs,2) AS cogs, ROUND(ox.sm,2) AS sm,
               ROUND(ox.rd,2)   AS rd,   ROUND(ox.ga,2) AS ga,
               ROUND(COALESCE(rev.mrr,0)+COALESCE(cm.credits,0)-ox.cogs, 2)
                                                                   AS gross_profit,
               ROUND(ox.cogs+ox.sm+ox.rd+ox.ga, 2)                 AS total_cost,
               ROUND(COALESCE(rev.mrr,0)+COALESCE(cm.credits,0)
                     -(ox.cogs+ox.sm+ox.rd+ox.ga), 2)              AS operating_income,
               ROUND((ox.cogs+ox.sm+ox.rd+ox.ga)
                     -(COALESCE(rev.mrr,0)+COALESCE(cm.credits,0)), 2) AS net_burn
        FROM dim_date d
        LEFT JOIN rev ON rev.month_key = d.month_key
        LEFT JOIN cm  ON cm.month_key  = d.month_key
        LEFT JOIN ox  ON ox.month_key  = d.month_key
        WHERE d.is_actual = 1
        """,
        """
        CREATE VIEW v_new_business AS
        SELECT c.customer_key, c.customer_id, c.signup_month,
               c.acquisition_channel AS channel, c.channel_unattributed,
               c.segment, c.segment_imputed, c.first_month_arr AS new_acv,
               c.first_month_arr/12.0 AS new_mrr, s.gross_margin,
               c.is_churned, c.churn_month_observed
        FROM dim_customer c
        JOIN dim_segment s ON s.segment_code = c.segment
        WHERE c.is_pre_window = 0
        """,
    ]
    for v in views:
        con.execute(v)

    for ddl in [
        "CREATE INDEX ix_fsm_month ON fact_subscription_month(month_key)",
        "CREATE INDEX ix_fsm_cust ON fact_subscription_month(customer_key)",
        "CREATE INDEX ix_fsm_move ON fact_subscription_month(movement_type)",
        "CREATE INDEX ix_dc_channel ON dim_customer(acquisition_channel)",
        "CREATE INDEX ix_dc_segment ON dim_customer(segment)",
        "CREATE INDEX ix_opex_month ON fact_opex(month_key)",
    ]:
        con.execute(ddl)
    con.commit()

    # -------- immediate integrity gate ------------------------------------
    q = """
    SELECT d.month,
           SUM(CASE WHEN f.movement_type IN ('new','opening_balance','expansion',
                                             'reactivation') THEN f.movement_amount
                    WHEN f.movement_type IN ('contraction','churn') THEN f.movement_amount
                    ELSE 0 END) AS net_move,
           SUM(f.mrr) AS closing
    FROM fact_subscription_month f JOIN dim_date d ON d.month_key = f.month_key
    GROUP BY d.month ORDER BY d.month"""
    br = pd.read_sql(q, con)
    br["opening"] = br.closing.shift().fillna(0.0)
    br["resid"] = (br.opening + br.net_move - br.closing).round(2)
    worst = float(br.resid.abs().max())
    print(f"\n  ARR bridge residual (max abs, $): {worst:.2f}")
    if worst > 0.005:
        print(br[br.resid.abs() > 0.005].to_string())
        con.close()
        raise SystemExit("ARR bridge does not tie — aborting build")

    tables = pd.read_sql(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name", con)
    print("\n  tables:", ", ".join(tables.name))
    print(f"  dim_customer={len(dim_customer):,}  fact_subscription_month={len(f):,}  "
          f"quarantined={len(a.quarantine):,}")
    con.close()


if __name__ == "__main__":
    sys.exit(build())
