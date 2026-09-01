#!/usr/bin/env python3
"""
Northwind Cloud — step 4: forecast bake-off, then an 18-month cash and runway
model under three scenarios.

Part 1 — model selection by evidence.
    Five candidate forecasters are run through rolling-origin backtests under
    two window policies (expanding and rolling-24). Winner is chosen on
    out-of-sample error across horizons 1-6, not on preference. Whatever wins,
    wins; the result is printed and written to outputs/tables/.

Part 2 — scenario cash model.
    Base, the sales proposal as tabled (+$180k/mo), and a selective
    alternative. Incremental bookings from added SDR capacity are derived from
    the SAME power-law saturation curve the historical data exhibits, put
    through the OBSERVED outbound funnel conversion rates and the OBSERVED
    outbound ACV by segment, then delayed by hire lag, ramp and sales cycle.

Part 3 — sensitivity.
    The break-even marginal productivity for each hiring scenario: how good
    the new reps must actually be for the decision to flip.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "warehouse" / "northwind.db"
OUT = ROOT / "outputs" / "tables"


# ===========================================================================
# Part 1 — candidate forecasters
# ===========================================================================
def f_naive(h: pd.DataFrame, n: int) -> np.ndarray:
    """Random walk. The benchmark every other method has to beat."""
    return np.repeat(h.mrr.iloc[-1], n)


def f_drift(h: pd.DataFrame, n: int) -> np.ndarray:
    """Random walk with drift = mean monthly change over the whole history."""
    y = h.mrr.to_numpy()
    d = float(np.mean(np.diff(y))) if len(y) > 1 else 0.0
    return y[-1] + d * np.arange(1, n + 1)


def f_loglinear(h: pd.DataFrame, n: int) -> np.ndarray:
    """OLS on log(MRR) — i.e. a constant-CAGR extrapolation."""
    y = np.log(h.mrr.to_numpy())
    t = np.arange(len(y), dtype=float)
    b, a = np.polyfit(t, y, 1)
    return np.exp(a + b * (len(y) - 1 + np.arange(1, n + 1)))


def fit_holt(y: np.ndarray) -> tuple[float, float]:
    """Grid-search alpha/beta on in-sample one-step SSE. Exposed separately so
    src/05 can hand the same two parameters to the Excel workbook, which then
    re-runs the whole recursion in cell formulas."""
    best, best_sse = (0.5, 0.1), math.inf
    for alpha in np.arange(0.1, 1.01, 0.1):
        for beta in np.arange(0.05, 0.61, 0.05):
            lvl, tr, sse = y[0], y[1] - y[0], 0.0
            for i in range(1, len(y)):
                sse += (y[i] - (lvl + tr)) ** 2
                new_lvl = alpha * y[i] + (1 - alpha) * (lvl + tr)
                tr = beta * (new_lvl - lvl) + (1 - beta) * tr
                lvl = new_lvl
            if sse < best_sse:
                best_sse, best = sse, (float(alpha), float(beta))
    return best


def f_holt(h: pd.DataFrame, n: int) -> np.ndarray:
    """Holt's linear exponential smoothing; alpha/beta grid-searched on the
    in-sample one-step SSE, so no parameter is chosen by hand."""
    y = h.mrr.to_numpy()
    alpha, beta = fit_holt(y)
    lvl, tr = y[0], y[1] - y[0]
    for i in range(1, len(y)):
        new_lvl = alpha * y[i] + (1 - alpha) * (lvl + tr)
        tr = beta * (new_lvl - lvl) + (1 - beta) * tr
        lvl = new_lvl
    return lvl + tr * np.arange(1, n + 1)


def f_driver(h: pd.DataFrame, n: int) -> np.ndarray:
    """Bottom-up SaaS identity:  MRR(t+1) = MRR(t) * NRR + new MRR.

    NRR and new MRR are each the trailing 6-month mean. This is the only
    candidate that knows the series is a subscription book rather than an
    arbitrary time series.
    """
    k = min(6, len(h))
    nrr = float(h.nrr_m.tail(k).mean())
    new = float(h.new_mrr.tail(k).mean())
    out, cur = [], float(h.mrr.iloc[-1])
    for _ in range(n):
        cur = cur * nrr + new
        out.append(cur)
    return np.array(out)


METHODS = {"naive": f_naive, "drift": f_drift, "log_linear": f_loglinear,
           "holt": f_holt, "driver_nrr": f_driver}


def backtest(hist: pd.DataFrame, min_train=18, max_h=6) -> pd.DataFrame:
    """Rolling-origin evaluation under two window policies."""
    rows = []
    T = len(hist)
    for policy, win in (("expanding", None), ("rolling_24", 24)):
        for origin in range(min_train, T):
            train = hist.iloc[:origin] if win is None else hist.iloc[max(0, origin - win):origin]
            if len(train) < 12:
                continue
            n = min(max_h, T - origin)
            for name, fn in METHODS.items():
                try:
                    pred = fn(train, n)
                except Exception:                                  # noqa: BLE001
                    continue
                act = hist.mrr.to_numpy()[origin:origin + n]
                for i in range(n):
                    rows.append(dict(policy=policy, origin=hist.month.iloc[origin - 1],
                                     method=name, horizon=i + 1,
                                     actual=act[i], pred=float(pred[i]),
                                     err=float(pred[i]) - act[i],
                                     ape=abs(float(pred[i]) - act[i]) / act[i]))
    return pd.DataFrame(rows)


def score(bt: pd.DataFrame) -> pd.DataFrame:
    g = bt.groupby(["policy", "method"]).agg(
        n=("err", "size"),
        mae=("err", lambda s: float(np.mean(np.abs(s)))),
        rmse=("err", lambda s: float(np.sqrt(np.mean(s ** 2)))),
        mape_pct=("ape", lambda s: 100 * float(np.mean(s))),
        bias=("err", "mean")).reset_index()
    return g.sort_values(["policy", "mape_pct"])


# ===========================================================================
# Part 2 — scenario cash model
# ===========================================================================
def sdr_per_rep(sdr_cfg: dict, team: float) -> float:
    n0 = float(sdr_cfg["saturation_team_size"])
    base = float(sdr_cfg["meetings_per_rep_small_team"])
    if team <= n0:
        return base
    return base * (n0 / float(team)) ** float(sdr_cfg["saturation_exponent"])


def incremental_meetings(sdr_cfg, existing: float, added: float, overlap: float) -> float:
    """Extra monthly meetings from `added` fully-ramped reps, net of the drag
    they put on the existing team by working an overlapping account list."""
    if added <= 0:
        return 0.0
    base = existing * sdr_per_rep(sdr_cfg, existing)
    exist_eff = existing + added * overlap
    new_eff = existing * overlap + added
    total = existing * sdr_per_rep(sdr_cfg, exist_eff) + added * sdr_per_rep(sdr_cfg, new_eff)
    return total - base


def observed_params(con) -> dict:
    """Everything the forward model needs, measured from the warehouse rather
    than read back out of the generator's assumptions."""
    p = {}
    q = """
    SELECT SUM(f.leads) AS meetings, SUM(f.sqls) AS sqls
    FROM fact_marketing_spend f
    JOIN dim_channel dc ON dc.channel_key = f.channel_key
    JOIN dim_date    d  ON d.month_key    = f.month_key
    WHERE dc.channel_code='outbound_sdr' AND d.month BETWEEN '2025-09' AND '2026-08'"""
    r = pd.read_sql(q, con).iloc[0]
    wins = pd.read_sql("""
        SELECT COUNT(*) AS wins FROM v_new_business
        WHERE channel='outbound_sdr' AND signup_month BETWEEN '2025-09' AND '2026-08'
    """, con).wins.iloc[0]
    p["meetings_12m"] = float(r.meetings)
    p["lead_to_sql"] = float(r.sqls) / float(r.meetings)
    p["sql_to_win"] = float(wins) / float(r.sqls)
    p["meeting_to_win"] = float(wins) / float(r.meetings)

    p["outbound_acv"] = pd.read_sql("""
        SELECT segment, AVG(new_acv) AS acv FROM v_new_business
        WHERE channel='outbound_sdr' AND signup_month BETWEEN '2025-09' AND '2026-08'
        GROUP BY segment""", con).set_index("segment").acv.to_dict()

    p["seg_nrr"] = pd.read_sql("""
        SELECT segment,
               SUM(CASE WHEN prev_mrr>0 THEN mrr ELSE 0 END)
               / SUM(CASE WHEN prev_mrr>0 THEN prev_mrr ELSE 0 END) AS nrr
        FROM v_customer_month GROUP BY segment""", con).set_index("segment").nrr.to_dict()

    p["seg_gm"] = pd.read_sql(
        "SELECT segment_code, gross_margin FROM dim_segment", con
    ).set_index("segment_code").gross_margin.to_dict()

    # Both sides of the Paid Social switch-off are measured on the SAME
    # trailing-3-month run rate. Using 12-month averages for the spend saved
    # and the run rate for the bookings lost (or the reverse) would bias the
    # comparison in whichever direction the analyst preferred.
    ps = pd.read_sql("""
        SELECT COUNT(*) AS n, SUM(new_acv) AS acv FROM v_new_business
        WHERE channel='paid_social' AND signup_month BETWEEN '2026-06' AND '2026-08'
    """, con).iloc[0]
    p["paid_social_new_mrr_per_month"] = float(ps.acv) / 3.0 / 12.0
    p["paid_social_logos_3m"] = int(ps.n)
    p["paid_social_spend_per_month"] = float(pd.read_sql("""
        SELECT SUM(f.program_spend)/3.0 AS s FROM fact_marketing_spend f
        JOIN dim_channel dc ON dc.channel_key=f.channel_key
        JOIN dim_date d ON d.month_key=f.month_key
        WHERE dc.channel_code='paid_social' AND d.month BETWEEN '2026-06' AND '2026-08'
    """, con).s.iloc[0])
    p["paid_social_seg_mix"] = pd.read_sql("""
        SELECT segment, SUM(new_acv) AS acv FROM v_new_business
        WHERE channel='paid_social' AND signup_month BETWEEN '2025-09' AND '2026-08'
        GROUP BY segment""", con).set_index("segment").acv.pipe(lambda s: s / s.sum()).to_dict()
    p["paid_social_payback_months"] = None  # filled by the caller from Q07

    p["sdr_heads_now"] = int(pd.read_sql("""
        SELECT headcount FROM fact_headcount h JOIN dim_date d ON d.month_key=h.month_key
        WHERE h.role='sdr' ORDER BY d.month_index DESC LIMIT 1""", con).headcount.iloc[0])
    return p


def ramp_factor(month_offset: int, hire_lag: int, curve: list[float]) -> float:
    """Share of full productivity in forecast month `month_offset` (1-based)."""
    k = month_offset - hire_lag
    if k <= 0:
        return 0.0
    return float(curve[min(k, len(curve)) - 1])


def build_scenario(name, scfg, cfg, obs, base_mrr, base_cost, months, cash0):
    """Return a monthly DataFrame for one scenario."""
    sdr_cfg = cfg["sdr"]
    lag = cfg["sales_cycle_lag_months"]
    H = len(months)
    added = int(scfg.get("sdr_heads_added", 0))
    overlap = float(scfg.get("list_overlap", 1.0))
    curve = scfg.get("ramp_curve", [0.35, 0.70, 1.00])
    hire_lag = int(scfg.get("hire_lag_months", 0))
    seg_mix = scfg.get("seg_mix", {})
    build = scfg.get("build_up", [])
    monthly_program_cost = (sum(b["heads"] * b["cost_each"] for b in build)
                            + float(scfg.get("tooling_data", 0)))
    ps_cut = float(scfg.get("paid_social_cut_pct", 0.0))

    # ---- incremental new MRR booked in each forecast month ----------------
    booked = np.zeros(H)                       # new MRR signed, by month
    booked_seg = {s: np.zeros(H) for s in obs["seg_gm"]}
    for t in range(1, H + 1):
        rf = ramp_factor(t, hire_lag, curve)
        eff_added = added * rf
        meets = incremental_meetings(sdr_cfg, obs["sdr_heads_now"], eff_added, overlap)
        wins = meets * obs["lead_to_sql"] * obs["sql_to_win"]
        for seg, share in seg_mix.items():
            acv = obs["outbound_acv"].get(seg, 0.0)
            booked_seg[seg][t - 1] += wins * share * acv / 12.0
        booked[t - 1] = sum(wins * share * obs["outbound_acv"].get(s, 0.0) / 12.0
                            for s, share in seg_mix.items())

    # ---- paid social switch-off removes bookings as well as spend ---------
    for seg, share in obs["paid_social_seg_mix"].items():
        booked_seg[seg] -= ps_cut * obs["paid_social_new_mrr_per_month"] * share

    # ---- compound each booked cohort forward at its segment's monthly NRR --
    delta_mrr = np.zeros(H)
    for seg, series in booked_seg.items():
        nrr = obs["seg_nrr"].get(seg, 1.0)
        for t in range(H):
            if abs(series[t]) < 1e-12:
                continue
            start = t + int(lag.get(seg, 2))     # signature -> first billed month
            for u in range(start, H):
                delta_mrr[u] += series[t] * (nrr ** (u - start))

    df = pd.DataFrame({"month": months})
    df["base_mrr"] = base_mrr
    df["delta_mrr"] = delta_mrr.round(2)
    df["mrr"] = (df.base_mrr + df.delta_mrr).round(2)

    # Payroll starts when the seat is filled, not when the req is opened.
    df["incremental_sm"] = [monthly_program_cost if t > hire_lag else 0.0
                            for t in range(1, H + 1)]
    df["paid_social_saving"] = -ps_cut * obs["paid_social_spend_per_month"]
    # commission follows the incremental bookings, in both directions
    df["incremental_commission"] = (np.array([sum(booked_seg[s][t] for s in booked_seg)
                                              for t in range(H)]) * 12.0
                                    * float(cfg["opex"]["commission_new_pct"])).round(2)
    infl = (1.0 + float(cfg["forecast_policy"]["opex_inflation_annual"])) ** (1.0 / 12.0)
    mth = np.arange(1, H + 1)
    df["cogs"] = (df.mrr * base_cost["cogs_pct"]).round(2)
    # S&M holds its current share of revenue on the base book, plus the
    # scenario's own incremental spend and commission.
    df["sm"] = (df.base_mrr * base_cost["sm_pct"] + df.incremental_sm
                + df.paid_social_saving + df.incremental_commission).round(2)
    df["rd"] = (base_cost["rd"] * infl ** mth).round(2)
    df["ga"] = (base_cost["ga"] * infl ** mth).round(2)
    df["total_cost"] = (df.cogs + df.sm + df.rd + df.ga).round(2)
    df["net_burn"] = (df.total_cost - df.mrr).round(2)
    df["cash"] = (cash0 - df.net_burn.cumsum()).round(2)
    df["arr"] = (df.mrr * 12).round(2)
    df["scenario"] = name
    return df, monthly_program_cost, booked_seg


def main() -> int:
    cfg = yaml.safe_load(open(ROOT / "config" / "assumptions.yml"))
    con = sqlite3.connect(DB)
    OUT.mkdir(parents=True, exist_ok=True)

    hist = pd.read_sql("""
        WITH mv AS (
          SELECT d.month, d.month_index,
                 SUM(CASE WHEN f.movement_type IN ('new','opening_balance')
                          THEN f.movement_amount ELSE 0 END) AS new_mrr,
                 SUM(CASE WHEN f.prev_mrr > 0 THEN f.mrr      ELSE 0 END) AS ret_num,
                 SUM(CASE WHEN f.prev_mrr > 0 THEN f.prev_mrr ELSE 0 END) AS ret_den
          FROM fact_subscription_month f JOIN dim_date d ON d.month_key=f.month_key
          GROUP BY d.month, d.month_index)
        SELECT p.month, p.month_index, p.mrr, p.recognised_revenue, p.cogs, p.sm,
               p.rd, p.ga, p.total_cost, p.net_burn,
               mv.new_mrr,
               CASE WHEN mv.ret_den > 0 THEN mv.ret_num/mv.ret_den ELSE 1.0 END AS nrr_m
        FROM v_pnl_month p JOIN mv ON mv.month_index = p.month_index
        ORDER BY p.month_index""", con)

    # ---------------- Part 1: bake-off ------------------------------------
    print("=" * 78)
    print("PART 1  Forecast model selection — rolling-origin backtest")
    print("=" * 78)
    bt = backtest(hist)
    sc = score(bt)
    bt.to_csv(OUT / "backtest_raw.csv", index=False)
    sc.to_csv(OUT / "backtest_scores.csv", index=False)
    print(sc.to_string(index=False))

    byh = (bt.groupby(["method", "horizon"]).ape.mean().mul(100).unstack().round(2))
    byh.to_csv(OUT / "backtest_mape_by_horizon.csv")
    print("\nMAPE % by horizon (both policies pooled):")
    print(byh.to_string())

    overall = (bt.groupby("method").ape.mean().mul(100).sort_values())
    winner = overall.index[0]
    print(f"\nWINNER: {winner}  (pooled out-of-sample MAPE {overall.iloc[0]:.2f}%)")
    print("Ranking:", ", ".join(f"{m} {v:.2f}%" for m, v in overall.items()))

    # ---------------- Part 2: scenario cash model -------------------------
    print("\n" + "=" * 78)
    print("PART 2  18-month cash and runway model")
    print("=" * 78)
    obs = observed_params(con)
    print("Observed forward parameters (all measured from the warehouse):")
    print(f"  outbound meeting->SQL         {obs['lead_to_sql']:.3f}")
    print(f"  outbound SQL->win             {obs['sql_to_win']:.3f}")
    print(f"  outbound meeting->win         {obs['meeting_to_win']:.4f}")
    print(f"  outbound ACV by segment       "
          f"{ {k: round(v) for k, v in obs['outbound_acv'].items()} }")
    print(f"  monthly net rev retention     "
          f"{ {k: round(v, 4) for k, v in obs['seg_nrr'].items()} }")
    print(f"  current SDR heads             {obs['sdr_heads_now']}")
    print(f"  paid social spend / month     ${obs['paid_social_spend_per_month']:,.0f}")
    print(f"  paid social new MRR / month   ${obs['paid_social_new_mrr_per_month']:,.0f}"
          f"  ({obs['paid_social_logos_3m']} logos in the last 3 months)")

    H = int(cfg["meta"]["forecast_months"])
    fmonths = pd.read_sql(
        f"SELECT month FROM dim_date WHERE is_actual=0 ORDER BY month_index LIMIT {H}",
        con).month.tolist()
    base_mrr = METHODS[winner](hist, H)

    last3 = hist.tail(3)
    base_cost = dict(
        cogs_pct=float((last3.cogs / last3.mrr).mean()),
        sm_pct=float((last3.sm / last3.mrr).mean()),
        sm=float(last3.sm.mean()), rd=float(last3.rd.mean()), ga=float(last3.ga.mean()))
    cash0 = float(json.loads(pd.read_sql(
        "SELECT value FROM ref_assumptions WHERE key='cash_on_hand'", con).value.iloc[0]))
    print("\nForward cost policy (trailing-3-month basis):")
    print(f"  COGS {base_cost['cogs_pct']*100:.1f}% of revenue (variable)")
    print(f"  S&M  {base_cost['sm_pct']*100:.1f}% of revenue — the current spending policy")
    print(f"       held constant, i.e. ${base_cost['sm']:,.0f}/mo today growing with revenue")
    print(f"  R&D  ${base_cost['rd']:,.0f}/mo and G&A ${base_cost['ga']:,.0f}/mo, headcount")
    print(f"       flat, inflated at {cfg['forecast_policy']['opex_inflation_annual']:.1%}/yr")

    frames, summary = [], []
    base_frame = None
    for name, scfg in cfg["scenarios"].items():
        df, monthly_cost, booked = build_scenario(
            name, scfg, cfg, obs, base_mrr, base_cost, fmonths, cash0)
        if base_frame is None:
            base_frame = df
        frames.append(df)
        df.to_csv(OUT / f"cashflow_{name}.csv", index=False)

        neg = df[df.cash < 0]
        if not neg.empty:
            runway = int(neg.index[0]) + 1
        else:
            # not exhausted inside the horizon: extend at the exit burn rate
            exit_burn = float(df.net_burn.tail(3).mean())
            runway = (round(H + float(df.cash.iloc[-1]) / exit_burn, 1)
                      if exit_burn > 0 else "cash-flow positive")
        inc_gp_m18 = float(df.delta_mrr.iloc[-1]) * (1 - base_cost["cogs_pct"])
        summary.append(dict(
            scenario=name, label=scfg["label"],
            incremental_sm_per_month=round(monthly_cost, 0),
            # month 6: hiring lag and ramp are done, revenue benefit is not
            net_incremental_burn_month6=round(
                float(df.net_burn.iloc[5] - base_frame.net_burn.iloc[5]), 0),
            arr_month18=round(float(df.arr.iloc[-1]), 0),
            delta_arr_month18=round(float(df.delta_mrr.iloc[-1] * 12), 0),
            burn_month18=round(float(df.net_burn.iloc[-1]), 0),
            cash_month18=round(float(df.cash.iloc[-1]), 0),
            runway_months=runway if runway else f">{H}",
            incremental_gp_month18=round(inc_gp_m18, 0)))

    allf = pd.concat(frames, ignore_index=True)
    allf.to_csv(OUT / "cashflow_all_scenarios.csv", index=False)
    summ = pd.DataFrame(summary)
    summ.to_csv(OUT / "scenario_summary.csv", index=False)
    print("\nScenario summary:")
    print(summ.drop(columns=["label"]).to_string(index=False))

    piv = allf.pivot(index="month", columns="scenario", values="cash").round(0)
    piv.to_csv(OUT / "scenario_cash_paths.csv")
    print("\nCash balance by month ($):")
    print(piv.to_string())

    # ---------------- Part 3: sensitivity ---------------------------------
    print("\n" + "=" * 78)
    print("PART 3  Sensitivity — how good would the new reps have to be?")
    print("=" * 78)
    rows = []
    for name in ("sales_proposal", "selective"):
        scfg = cfg["scenarios"][name]
        added = scfg["sdr_heads_added"]
        overlap = scfg["list_overlap"]
        cost = (sum(b["heads"] * b["cost_each"] for b in scfg["build_up"])
                + scfg["tooling_data"])
        meets = incremental_meetings(cfg["sdr"], obs["sdr_heads_now"], added, overlap)
        wins = meets * obs["meeting_to_win"]
        blended_acv = sum(scfg["seg_mix"].get(s, 0) * obs["outbound_acv"].get(s, 0)
                          for s in obs["outbound_acv"])
        blended_gm = sum(scfg["seg_mix"].get(s, 0) * obs["seg_gm"][s] for s in obs["seg_gm"])
        gp_per_cust_month = blended_acv / 12.0 * blended_gm
        cac = cost / wins if wins > 0 else float("nan")
        payback = cac / gp_per_cust_month if gp_per_cust_month else float("nan")
        # break-even: wins/month needed for an 18-month CAC payback
        need_wins = cost / (18.0 * gp_per_cust_month)
        need_meets = need_wins / obs["meeting_to_win"]
        rows.append(dict(
            scenario=name, added_heads=added, list_overlap=overlap,
            monthly_cost=round(cost),
            incremental_meetings_per_month=round(meets, 1),
            meetings_per_added_rep=round(meets / added, 2),
            wins_per_month=round(wins, 2),
            blended_acv=round(blended_acv),
            incremental_cac=round(cac),
            cac_payback_months=round(payback, 1),
            breakeven_meetings_per_added_rep_for_18m=round(need_meets / added, 2),
            productivity_headroom_pct=round(100 * (meets / added) /
                                            (need_meets / added) - 100, 1)))
    sens = pd.DataFrame(rows)
    sens.to_csv(OUT / "hiring_sensitivity.csv", index=False)
    print(sens.to_string(index=False))
    print("\nRead the last column as: how far the new reps' productivity could")
    print("fall short of assumption before the 18-month payback test fails.")

    # ---- stress the two assumptions the recommendation actually rests on --
    print("\nStress test A — territory overlap of the selective pod.")
    print("list_overlap = 1.00 is the worst case: the new reps add nothing but")
    print("more people working the SAME account list the current team works.")
    scfg = cfg["scenarios"]["selective"]
    cost = (sum(b["heads"] * b["cost_each"] for b in scfg["build_up"]) + scfg["tooling_data"])
    blended_acv = sum(scfg["seg_mix"].get(s_, 0) * obs["outbound_acv"].get(s_, 0)
                      for s_ in obs["outbound_acv"])
    blended_gm = sum(scfg["seg_mix"].get(s_, 0) * obs["seg_gm"][s_] for s_ in obs["seg_gm"])
    rows = []
    for ov in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
        mt = incremental_meetings(cfg["sdr"], obs["sdr_heads_now"],
                                  scfg["sdr_heads_added"], ov)
        wins = mt * obs["meeting_to_win"]
        cac = cost / wins
        pb = cac / (blended_acv / 12.0 * blended_gm)
        rows.append(dict(list_overlap=ov, meetings_per_added_rep=round(mt / 5, 2),
                         wins_per_month=round(wins, 2), incremental_cac=round(cac),
                         cac_payback_months=round(pb, 1),
                         verdict="PASS" if pb <= 18 else "FAIL"))
    ov_df = pd.DataFrame(rows)
    ov_df.to_csv(OUT / "sensitivity_territory_overlap.csv", index=False)
    print(ov_df.to_string(index=False))

    print("\nStress test B — Enterprise share of the pod's won mix.")
    print("The pod is priced on selling Enterprise. If it drifts back to the")
    print("Mid-Market mix the current team sells, does it still clear 18 months?")
    rows = []
    for ent in [0.10, 0.20, 0.28, 0.40, 0.58, 0.70]:
        mix = {"Enterprise": ent, "MidMarket": max(0.0, 0.98 - ent), "SMB": 0.02}
        acv = sum(mix[s_] * obs["outbound_acv"][s_] for s_ in mix)
        gm = sum(mix[s_] * obs["seg_gm"][s_] for s_ in mix)
        mt = incremental_meetings(cfg["sdr"], obs["sdr_heads_now"],
                                  scfg["sdr_heads_added"], scfg["list_overlap"])
        wins = mt * obs["meeting_to_win"]
        pb = (cost / wins) / (acv / 12.0 * gm)
        rows.append(dict(enterprise_share=ent, blended_acv=round(acv),
                         incremental_cac=round(cost / wins),
                         cac_payback_months=round(pb, 1),
                         verdict="PASS" if pb <= 18 else "FAIL"))
    mix_df = pd.DataFrame(rows)
    mix_df.to_csv(OUT / "sensitivity_segment_mix.csv", index=False)
    print(mix_df.to_string(index=False))

    alpha, beta = fit_holt(hist.mrr.to_numpy())
    json.dump({
        "observed": {k: (v if not isinstance(v, dict)
                         else {kk: float(vv) for kk, vv in v.items()})
                     for k, v in obs.items()},
        "forecast": {"winning_method": winner, "holt_alpha": alpha, "holt_beta": beta,
                     "pooled_mape_pct": {m: float(v) for m, v in overall.items()}},
        "cost_policy": base_cost,
        "cash_on_hand": cash0,
        "forecast_months": H,
        "base_mrr_path": [float(x) for x in base_mrr],
    }, open(OUT / "forward_model_parameters.json", "w"), indent=2, default=float)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
