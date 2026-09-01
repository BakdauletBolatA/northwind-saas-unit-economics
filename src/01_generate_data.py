#!/usr/bin/env python3
"""
Northwind Cloud — step 1: driver-based synthetic data generator.

Grain of the fact output is subscription x month. Nothing here is drawn from a
"nice" distribution and pasted in: every customer is produced by a funnel
(spend -> leads -> SQLs -> wins) and then lives a month-by-month life with
expansion, contraction and a churn hazard. Revenue movements are therefore an
*emergent* property of the simulation, which is what makes the ARR bridge a
real reconciliation rather than a copy of the generator's own bookkeeping.

Determinism: one seeded numpy Generator, consumed in a fixed order. Re-running
this script byte-for-byte reproduces every output file (verified in step 6 by
SHA-256 comparison).

Outputs -> data/raw/
    customers.csv          customer master (CRM export, with real-world gaps)
    billing_raw.csv        subscription x month billing lines, uncleaned
    marketing_spend.csv    channel x month spend / leads / SQLs
    opex_ledger.csv        full operating expense register
    headcount.csv          role x month headcount and loaded cost
    events_calendar.csv    the named business events, for annotation only
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
LOGS = ROOT / "outputs" / "logs"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def month_str(start: str, offset: int) -> str:
    """month_str('2023-09', 0) -> '2023-09'; offset may be negative."""
    y, m = (int(x) for x in start.split("-"))
    total = (y * 12 + (m - 1)) + offset
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_index(start: str, target: str) -> int:
    """1-based index of `target` relative to `start` (start itself == 1)."""
    ys, ms = (int(x) for x in start.split("-"))
    yt, mt = (int(x) for x in target.split("-"))
    return (yt * 12 + mt) - (ys * 12 + ms) + 1


def month_first_day(ms: str) -> str:
    return f"{ms}-01"


def interp_schedule(sched: dict, t: int) -> float:
    """Linear interpolation between {month_index: value} anchors, flat outside."""
    keys = sorted(int(k) for k in sched)
    if t <= keys[0]:
        return float(sched[keys[0]])
    if t >= keys[-1]:
        return float(sched[keys[-1]])
    for a, b in zip(keys, keys[1:]):
        if a <= t <= b:
            w = (t - a) / (b - a)
            return float(sched[a]) + w * (float(sched[b]) - float(sched[a]))
    raise AssertionError("unreachable")


def sdr_per_rep(sdr_cfg: dict, team_size: int) -> float:
    """Meetings per fully-ramped SDR at a given team size (power-law saturation).

    Shared by the generator and the forward cash model so that the marginal
    productivity used to price the hiring proposal is the SAME curve the
    historical data exhibits, not a separate hand-set number.
    """
    n0 = float(sdr_cfg["saturation_team_size"])
    base = float(sdr_cfg["meetings_per_rep_small_team"])
    if team_size <= n0:
        return base
    return base * (n0 / float(team_size)) ** float(sdr_cfg["saturation_exponent"])


def lognormal_mean(rng, mean: float, sigma: float, size=None):
    """Log-normal draw whose *arithmetic* mean is `mean`."""
    mu = math.log(mean) - 0.5 * sigma**2
    return rng.lognormal(mu, sigma, size)


NAME_A = ["Ridge", "Harbor", "Cedar", "Vantage", "Copper", "Northgate", "Lakeshore",
          "Pioneer", "Granite", "Beacon", "Sterling", "Bright", "Orchard", "Falcon",
          "Summit", "Iron", "Prairie", "Anchor", "Kestrel", "Meridian", "Bluff",
          "Willow", "Foundry", "Trident", "Halcyon", "Ember", "Quarry", "Dune"]
NAME_B = ["Logistics", "Distribution", "Supply", "Fulfilment", "Industrial", "Freight",
          "Wholesale", "Components", "Foods", "Materials", "Packaging", "Automotive",
          "Medical Supply", "Beverage", "Apparel", "Hardware", "Chemicals", "Paper"]
NAME_C = ["Group", "Co", "Partners", "Holdings", "Inc", "LLC", "Ltd", "Works"]
COUNTRIES = ["US", "US", "US", "US", "CA", "GB", "DE", "NL", "AU", "SE"]
INDUSTRIES = ["3PL", "Food & Beverage", "Industrial Distribution", "Retail",
              "Automotive Parts", "Medical Devices", "Chemicals", "Apparel"]


# --------------------------------------------------------------------------
class Generator:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.meta = cfg["meta"]
        self.start = self.meta["history_start"]
        self.T = int(self.meta["history_months"])
        self.rng = np.random.default_rng(int(self.meta["seed"]))
        self.months = [month_str(self.start, i) for i in range(self.T)]  # index t-1

        # event start indices (may be <1 or >T if the config is edited)
        self.ev = {e["id"]: e for e in cfg["events_log"]}
        self.ev_t = {k: month_index(self.start, v["start_month"]) for k, v in self.ev.items()}

        self.customers: list[dict] = []
        self.billing: list[dict] = []      # clean, pre-defect rows
        self.marketing: list[dict] = []
        self.diag: dict = {}

    # ---------------- headcount ------------------------------------------
    def build_headcount(self):
        hc = self.cfg["headcount"]
        self.hc = {}
        for role, sched in hc["plan"].items():
            self.hc[role] = [int(round(interp_schedule(sched, t))) for t in range(1, self.T + 1)]
        return self.hc

    def sdr_effective_reps(self, t: int) -> float:
        """Ramp-adjusted SDR capacity in month t (1-based)."""
        ramp = self.cfg["sdr"]["ramp_curve"]
        team = self.hc["sdr"]
        cur = team[t - 1]
        deficit = 0.0
        for k, factor in enumerate(ramp):          # k = months since hire
            idx = t - 1 - k
            prev = team[idx - 1] if idx - 1 >= 0 else team[0]
            hires = max(0, team[idx] - prev) if idx >= 0 else 0
            deficit += hires * (1.0 - factor)
        return max(0.0, cur - deficit)

    def sdr_meetings(self, t: int) -> float:
        s = self.cfg["sdr"]
        team = self.hc["sdr"][t - 1]
        return self.sdr_effective_reps(t) * sdr_per_rep(s, team)

    # ---------------- event multipliers -----------------------------------
    def e1_factor(self, t: int) -> float:
        """Phase-in weight 0..1 of the Paid Social quality collapse for a cohort
        acquired in month t."""
        t0 = self.ev_t["E1"]
        p = self.ev["E1"]["params"]
        if t < t0:
            return 0.0
        return min(1.0, (t - t0 + 1) / float(p["ramp_months"]))

    def e3_churn_mult(self, t: int) -> float:
        """SMB churn multiplier in calendar month t after the price rise."""
        t0 = self.ev_t["E3"]
        p = self.ev["E3"]["params"]
        if t < t0:
            return 1.0
        n = int(p["spike_months"])
        if t < t0 + n:
            w = (t - t0) / float(n)
            return p["churn_multiplier_peak"] + w * (p["churn_multiplier_plateau"]
                                                     - p["churn_multiplier_peak"])
        return float(p["churn_multiplier_plateau"])

    # ---------------- plan / seat helpers ---------------------------------
    def pick_plan(self, segment: str, acv: float):
        plans = self.cfg["plans"]
        if segment == "SMB":
            code = "Starter" if acv < 4_200 else "Growth"
        elif segment == "MidMarket":
            code = "Scale"
        else:
            code = "Enterprise"
        price = float(plans[code]["seat_price"])
        seats = max(int(plans[code]["min_seats"]), int(round(acv / 12.0 / price)))
        return code, price, seats

    # ---------------- acquisition ----------------------------------------
    def generate_acquisitions(self):
        cfg = self.cfg
        seg_names = list(cfg["segments"].keys())
        cid = 1

        # ---- legacy installed base (pre-window; no S&M attribution) -------
        lb = cfg["legacy_base"]
        n_leg = int(lb["n_customers"])
        seg_p = np.array([lb["segment_mix"][s] for s in seg_names], dtype=float)
        seg_p = seg_p / seg_p.sum()
        ch_names = list(lb["channel_mix"].keys())
        ch_p = np.array([lb["channel_mix"][c] for c in ch_names], dtype=float)
        ch_p = ch_p / ch_p.sum()

        leg_seg = self.rng.choice(seg_names, size=n_leg, p=seg_p)
        leg_ch = self.rng.choice(ch_names, size=n_leg, p=ch_p)
        leg_tenure = self.rng.integers(1, int(lb["max_tenure_months"]) + 1, size=n_leg)
        leg_acv = np.array([
            float(lognormal_mean(self.rng, cfg["segments"][s]["acv_mean"],
                                 cfg["segments"][s]["acv_sigma"]))
            for s in leg_seg
        ])
        # Back-solve one scale factor so the legacy book ties to the opening ARR
        # the board has been quoted. Legacy ARPA lands ~50% of current new
        # business, consistent with the old low-touch self-serve motion.
        scale = float(lb["target_arr_at_window_start"]) / leg_acv.sum()
        leg_acv = leg_acv * scale
        self.diag["legacy_acv_scale"] = round(scale, 4)

        for i in range(n_leg):
            seg = str(leg_seg[i])
            code, price, seats = self.pick_plan(seg, float(leg_acv[i]))
            t_sign = 1 - int(leg_tenure[i])          # negative / zero month index
            self.customers.append(dict(
                customer_id=f"CUST-{cid:05d}",
                company_name=self._name(),
                signup_month=month_str(self.start, t_sign - 1),
                signup_index=t_sign,
                acquisition_channel=str(leg_ch[i]),
                segment=seg,
                plan_code=code,
                seat_price=price,
                initial_seats=seats,
                country=str(self.rng.choice(COUNTRIES)),
                industry=str(self.rng.choice(INDUSTRIES)),
                is_pre_window=1,
            ))
            cid += 1

        # ---- in-window acquisition through the funnel ---------------------
        for t in range(1, self.T + 1):
            for ch, c in cfg["channels"].items():
                spend = interp_schedule(c["program_spend"], t)
                if c["cpl"] and c["cpl"] > 0:
                    cpl = float(c["cpl"]) * (1.0 + float(c["cpl_drift"])) ** (t - 1)
                    leads = spend / cpl
                else:                                   # outbound: capacity-driven
                    cpl = 0.0
                    leads = self.sdr_meetings(t)

                l2s = float(c["lead_to_sql"])
                s2w = float(c["sql_to_win"])
                acv_mult = 1.0
                if ch == "paid_social":
                    f = self.e1_factor(t)
                    p = self.ev["E1"]["params"]
                    # broad-reach traffic converts worse *and* signs smaller
                    l2s *= 1.0 - 0.52 * f
                    acv_mult *= 1.0 + (float(p["acv_multiplier"]) - 1.0) * f
                if ch == "partner" and t >= self.ev_t["E5"]:
                    s2w *= float(self.ev["E5"]["params"]["sql_to_win_multiplier"])

                sqls = leads * l2s
                exp_wins = sqls * s2w
                wins = int(self.rng.poisson(exp_wins))

                self.marketing.append(dict(
                    month=self.months[t - 1], channel=ch,
                    program_spend=round(spend, 2),
                    cost_per_lead=round(cpl, 2),
                    leads=round(leads, 1),
                    sqls=round(sqls, 1),
                ))

                seg_mix = c["seg_mix"]
                sp = np.array([seg_mix.get(s, 0.0) for s in seg_names], dtype=float)
                sp = sp / sp.sum()
                for _ in range(wins):
                    seg = str(self.rng.choice(seg_names, p=sp))
                    acv = float(lognormal_mean(self.rng, cfg["segments"][seg]["acv_mean"],
                                               cfg["segments"][seg]["acv_sigma"])) * acv_mult
                    code, price, seats = self.pick_plan(seg, acv)
                    self.customers.append(dict(
                        customer_id=f"CUST-{cid:05d}",
                        company_name=self._name(),
                        signup_month=self.months[t - 1],
                        signup_index=t,
                        acquisition_channel=ch,
                        segment=seg,
                        plan_code=code,
                        seat_price=price,
                        initial_seats=seats,
                        country=str(self.rng.choice(COUNTRIES)),
                        industry=str(self.rng.choice(INDUSTRIES)),
                        is_pre_window=0,
                    ))
                    cid += 1

    def _name(self) -> str:
        a = str(self.rng.choice(NAME_A)); b = str(self.rng.choice(NAME_B))
        c = str(self.rng.choice(NAME_C))
        return f"{a} {b} {c}"

    # ---------------- subscription lifecycle ------------------------------
    def simulate_lifecycles(self):
        cfg = self.cfg
        tenure_sched = cfg["tenure_churn_multiplier"]
        e1p = self.ev["E1"]["params"]
        e2p = self.ev["E2"]["params"]
        e3p = self.ev["E3"]["params"]
        t_e1, t_e2, t_e3 = self.ev_t["E1"], self.ev_t["E2"], self.ev_t["E3"]

        bid = 1
        for cust in self.customers:
            seg = cust["segment"]
            s = cfg["segments"][seg]
            seats = int(cust["initial_seats"])
            price = float(cust["seat_price"])
            t_start = max(1, int(cust["signup_index"]))
            price_raised = False

            # cohort-level Paid Social quality penalty (E1)
            churn_cohort_mult = 1.0
            if cust["acquisition_channel"] == "paid_social":
                f = self.e1_factor(int(cust["signup_index"]))
                churn_cohort_mult = 1.0 + (float(e1p["churn_multiplier"]) - 1.0) * f

            for t in range(t_start, self.T + 1):
                tenure = t - int(cust["signup_index"]) + 1

                if t > t_start:
                    # ---- churn hazard --------------------------------------
                    haz = float(s["base_monthly_logo_churn"])
                    haz *= interp_schedule(tenure_sched, tenure)
                    haz *= churn_cohort_mult
                    if seg == "SMB":
                        haz *= self.e3_churn_mult(t)
                    if self.rng.random() < haz:
                        cust["churn_index"] = t
                        cust["churn_month"] = self.months[t - 1]
                        break

                    # ---- expansion -----------------------------------------
                    exp_rate = float(s["expansion_rate"])
                    exp_size = float(s["expansion_size"])
                    if seg == "Enterprise" and t >= t_e2:
                        exp_rate *= float(e2p["expansion_rate_multiplier"])
                        exp_size *= float(e2p["expansion_size_multiplier"])
                    if self.rng.random() < exp_rate:
                        add = max(1, int(round(seats * exp_size *
                                               float(lognormal_mean(self.rng, 1.0, 0.30)))))
                        seats += add

                    # ---- contraction ---------------------------------------
                    con_rate = float(s["contraction_rate"])
                    if seg == "SMB" and t >= t_e3:
                        con_rate *= float(e3p["contraction_rate_multiplier"])
                    if self.rng.random() < con_rate:
                        cut = max(1, int(round(seats * float(s["contraction_size"]) *
                                               float(lognormal_mean(self.rng, 1.0, 0.30)))))
                        seats = max(1, seats - cut)

                    # ---- SMB price rise at renewal anniversary (E3) ---------
                    if (seg == "SMB" and not price_raised and t >= t_e3
                            and (t - int(cust["signup_index"])) % 12 == 0):
                        price = round(price * (1.0 + float(e3p["price_uplift"])), 2)
                        price_raised = True

                mrr = round(seats * price, 2)
                self.billing.append(dict(
                    billing_id=bid,
                    customer_id=cust["customer_id"],
                    subscription_id="SUB-" + cust["customer_id"].split("-")[1],
                    billing_month=self.months[t - 1],
                    record_type="recurring",
                    seats=seats,
                    seat_price=round(price, 2),
                    mrr_amount=mrr,
                    plan_code=cust["plan_code"],
                    currency="USD",
                    source_system="billing_v1",
                    created_at=month_first_day(self.months[t - 1]),
                ))
                bid += 1
            self.next_billing_id = bid

    # ---------------- deliberate data defects ------------------------------
    def inject_defects(self):
        dq = self.cfg["data_quality"]
        bid = self.next_billing_id
        log = {}

        # 1. billing-migration double post (E6)
        dup_month = dq["duplicate_month"]
        idx = [i for i, r in enumerate(self.billing) if r["billing_month"] == dup_month]
        n_dup = int(round(len(idx) * float(dq["duplicate_share"])))
        pick = self.rng.choice(np.array(idx), size=n_dup, replace=False)
        dups = []
        for i in sorted(pick.tolist()):
            r = dict(self.billing[i])
            r["billing_id"] = bid; bid += 1
            r["source_system"] = "billing_v2_migration"
            r["created_at"] = month_first_day(month_str(dup_month, 1))
            dups.append(r)
        self.billing.extend(dups)
        log["duplicate_rows_injected"] = n_dup

        # 2. back-dated credit memos (legitimate negative adjustments)
        n_cm = int(round(len(self.billing) * float(dq["credit_memo_share"])))
        pick = self.rng.choice(len(self.billing), size=n_cm, replace=False)
        memos = []
        for i in sorted(pick.tolist()):
            r = dict(self.billing[i])
            if r["record_type"] != "recurring":
                continue
            amt = -round(abs(r["mrr_amount"]) * float(self.rng.uniform(0.15, 0.60)), 2)
            memos.append(dict(
                billing_id=bid, customer_id=r["customer_id"],
                subscription_id=r["subscription_id"],
                billing_month=r["billing_month"], record_type="credit_memo",
                seats=0, seat_price=0.0, mrr_amount=amt, plan_code=r["plan_code"],
                currency="USD", source_system="billing_v1",
                created_at=month_first_day(month_str(r["billing_month"], 2)),
            ))
            bid += 1
        self.billing.extend(memos)
        log["credit_memo_rows_injected"] = len(memos)

        # 3. orphan billing rows (customer never made it into the CRM export)
        orph = []
        for k in range(int(dq["orphan_billing_rows"])):
            m = self.months[int(self.rng.integers(0, self.T))]
            orph.append(dict(
                billing_id=bid, customer_id=f"CUST-9{k:04d}",
                subscription_id=f"SUB-9{k:04d}", billing_month=m,
                record_type="recurring", seats=int(self.rng.integers(5, 40)),
                seat_price=62.0, mrr_amount=round(float(self.rng.uniform(400, 3000)), 2),
                plan_code="Growth", currency="USD", source_system="billing_v1",
                created_at=month_first_day(m),
            ))
            bid += 1
        self.billing.extend(orph)
        log["orphan_rows_injected"] = len(orph)

        # 4. negative-seat rows (data-entry corruption on the recurring line)
        rec_idx = [i for i, r in enumerate(self.billing) if r["record_type"] == "recurring"]
        pick = self.rng.choice(np.array(rec_idx), size=int(dq["negative_seat_rows"]),
                               replace=False)
        for i in sorted(pick.tolist()):
            self.billing[i]["seats"] = -abs(self.billing[i]["seats"])
            self.billing[i]["mrr_amount"] = -abs(self.billing[i]["mrr_amount"])
        log["negative_seat_rows_injected"] = int(dq["negative_seat_rows"])

        # 5. missing customer attributes
        n = len(self.customers)
        for field, share in (("acquisition_channel", dq["missing_channel_share"]),
                             ("segment", dq["missing_segment_share"]),
                             ("plan_code", dq["missing_plan_share"])):
            k = int(round(n * float(share)))
            pick = self.rng.choice(n, size=k, replace=False)
            for i in sorted(pick.tolist()):
                self.customers[i][field] = ""
            log[f"missing_{field}"] = k

        self.diag["defects"] = log
        self.next_billing_id = bid

    # ---------------- spend / opex ledgers ---------------------------------
    def build_ledgers(self, clean_mrr_by_month: dict, new_acv: dict, exp_acv: dict):
        cfg = self.cfg
        hc_cost = cfg["headcount"]["loaded_cost"]
        hc_cat = cfg["headcount"]["cost_category"]
        op = cfg["opex"]
        seg = cfg["segments"]

        headcount_rows, opex_rows = [], []
        for t in range(1, self.T + 1):
            m = self.months[t - 1]
            rev = clean_mrr_by_month.get(m, 0.0)

            for role in cfg["headcount"]["plan"]:
                n = self.hc[role][t - 1]
                cost = n * float(hc_cost[role])
                headcount_rows.append(dict(month=m, role=role, headcount=n,
                                           loaded_cost_per_head=float(hc_cost[role]),
                                           total_cost=round(cost, 2),
                                           cost_category=hc_cat[role]))
                opex_rows.append(dict(month=m, cost_category=hc_cat[role],
                                      cost_center=role, gl_account="Personnel",
                                      amount=round(cost, 2),
                                      note=f"{n} x {role} fully loaded"))

            # COGS
            opex_rows.append(dict(month=m, cost_category="COGS", cost_center="infrastructure",
                                  gl_account="Hosting",
                                  amount=round(rev * float(op["hosting_pct_of_revenue"]), 2),
                                  note="cloud infrastructure, % of MRR"))
            opex_rows.append(dict(month=m, cost_category="COGS", cost_center="infrastructure",
                                  gl_account="Third-party licences",
                                  amount=round(rev * float(op["other_cogs_pct_of_revenue"]), 2),
                                  note="carrier / EDI / mapping licences, % of MRR"))
            # S&M programme spend, by channel
            for row in self.marketing:
                if row["month"] == m:
                    opex_rows.append(dict(month=m, cost_category="SM",
                                          cost_center=row["channel"],
                                          gl_account="Marketing programmes",
                                          amount=row["program_spend"],
                                          note="ties to marketing_spend.csv"))
            opex_rows.append(dict(month=m, cost_category="SM", cost_center="marketing",
                                  gl_account="Marketing operations",
                                  amount=round(float(op["marketing_ops_base"]) *
                                               (1 + float(op["marketing_ops_growth"])) ** (t - 1), 2),
                                  note="martech stack, agency retainers"))
            opex_rows.append(dict(month=m, cost_category="SM", cost_center="sales",
                                  gl_account="Sales commission",
                                  amount=round(new_acv.get(m, 0.0) * float(op["commission_new_pct"])
                                               + exp_acv.get(m, 0.0) * float(op["commission_expansion_pct"]), 2),
                                  note="10% of new ACV + 5% of expansion ACV"))
            # R&D / G&A non-personnel
            opex_rows.append(dict(month=m, cost_category="RD", cost_center="engineering",
                                  gl_account="R&D tooling",
                                  amount=round(float(op["rd_tooling_base"]) *
                                               (1 + float(op["rd_tooling_growth"])) ** (t - 1), 2),
                                  note="CI, observability, dev environments"))
            opex_rows.append(dict(month=m, cost_category="GA", cost_center="corporate",
                                  gl_account="Facilities, insurance, professional fees",
                                  amount=round(float(op["ga_fixed_base"]) *
                                               (1 + float(op["ga_fixed_growth"])) ** (t - 1), 2),
                                  note="fixed corporate overhead"))
        return headcount_rows, opex_rows

    # ---------------- orchestration ----------------------------------------
    def run(self):
        self.build_headcount()
        self.generate_acquisitions()
        self.simulate_lifecycles()

        # ground-truth movements BEFORE defects, used only for commission accrual
        clean_mrr, new_acv, exp_acv = self._movements()

        self.inject_defects()
        hc_rows, opex_rows = self.build_ledgers(clean_mrr, new_acv, exp_acv)
        self.diag["clean_mrr_by_month"] = {k: round(v, 2) for k, v in clean_mrr.items()}
        return hc_rows, opex_rows

    def _movements(self):
        """MRR level per month plus new / expansion ACV, from the clean sim.

        Commission accrues on genuinely new logos only. The legacy book first
        *appears* in month 1 of the window, but it was not sold in month 1 —
        crediting it would put a ~$400k phantom commission into the opening
        month and make every efficiency ratio in the first year meaningless.
        """
        signup = {c["customer_id"]: c["signup_month"] for c in self.customers}
        by_cust = {}
        for r in self.billing:
            by_cust.setdefault(r["customer_id"], {})[r["billing_month"]] = r["mrr_amount"]
        mrr, new_acv, exp_acv = {}, {}, {}
        for m in self.months:
            mrr[m] = 0.0; new_acv[m] = 0.0; exp_acv[m] = 0.0
        for cid, series in by_cust.items():
            for i, m in enumerate(self.months):
                cur = series.get(m)
                if cur is None:
                    continue
                mrr[m] += cur
                prev = series.get(self.months[i - 1]) if i > 0 else None
                if prev is None:
                    if signup.get(cid) == m:            # sold this month
                        new_acv[m] += cur * 12.0
                elif cur > prev:
                    exp_acv[m] += (cur - prev) * 12.0
        return mrr, new_acv, exp_acv


# --------------------------------------------------------------------------
def write_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    cfg = yaml.safe_load(open(ROOT / "config" / "assumptions.yml"))
    g = Generator(cfg)
    hc_rows, opex_rows = g.run()

    # ---- customers -------------------------------------------------------
    cust_fields = ["customer_id", "company_name", "signup_month", "acquisition_channel",
                   "segment", "plan_code", "seat_price", "initial_seats", "country",
                   "industry", "is_pre_window", "churn_month"]
    customers = sorted(g.customers, key=lambda r: r["customer_id"])
    write_csv(RAW / "customers.csv", customers, cust_fields)

    # ---- billing ---------------------------------------------------------
    bill_fields = ["billing_id", "customer_id", "subscription_id", "billing_month",
                   "record_type", "seats", "seat_price", "mrr_amount", "plan_code",
                   "currency", "source_system", "created_at"]
    billing = sorted(g.billing, key=lambda r: (r["billing_month"], r["customer_id"],
                                               r["record_type"], r["billing_id"]))
    write_csv(RAW / "billing_raw.csv", billing, bill_fields)

    # ---- marketing -------------------------------------------------------
    mk_fields = ["month", "channel", "program_spend", "cost_per_lead", "leads", "sqls"]
    write_csv(RAW / "marketing_spend.csv",
              sorted(g.marketing, key=lambda r: (r["month"], r["channel"])), mk_fields)

    # ---- headcount / opex ------------------------------------------------
    hc_fields = ["month", "role", "headcount", "loaded_cost_per_head", "total_cost",
                 "cost_category"]
    write_csv(RAW / "headcount.csv",
              sorted(hc_rows, key=lambda r: (r["month"], r["role"])), hc_fields)

    op_fields = ["month", "cost_category", "cost_center", "gl_account", "amount", "note"]
    write_csv(RAW / "opex_ledger.csv",
              sorted(opex_rows, key=lambda r: (r["month"], r["cost_category"],
                                               r["cost_center"], r["gl_account"])), op_fields)

    # ---- events ----------------------------------------------------------
    ev_fields = ["event_id", "event_name", "start_month", "scope_type", "scope_value",
                 "description"]
    ev_rows = [dict(event_id=e["id"], event_name=e["name"], start_month=e["start_month"],
                    scope_type=e["scope_type"], scope_value=e["scope_value"],
                    description=" ".join(e["description"].split()))
               for e in cfg["events_log"]]
    write_csv(RAW / "events_calendar.csv", ev_rows, ev_fields)

    # ---- manifest --------------------------------------------------------
    LOGS.mkdir(parents=True, exist_ok=True)
    files = ["customers.csv", "billing_raw.csv", "marketing_spend.csv",
             "headcount.csv", "opex_ledger.csv", "events_calendar.csv"]
    manifest = {"seed": cfg["meta"]["seed"],
                "generated_rows": {f: sum(1 for _ in open(RAW / f)) - 1 for f in files},
                "sha256": {f: sha256(RAW / f) for f in files},
                "diagnostics": {k: v for k, v in g.diag.items()
                                if k != "clean_mrr_by_month"}}
    (LOGS / "generation_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    # ---- console diagnostics --------------------------------------------
    mrr = g.diag["clean_mrr_by_month"]
    m1, mT = g.months[0], g.months[-1]
    print(f"customers          : {len(customers):,}  "
          f"(pre-window {sum(1 for c in customers if c['is_pre_window'] == 1):,})")
    print(f"billing rows       : {len(billing):,}")
    print(f"ARR {m1}       : ${mrr[m1]*12:,.0f}")
    print(f"ARR {mT}       : ${mrr[mT]*12:,.0f}")
    opex_T = sum(r["amount"] for r in opex_rows if r["month"] == mT)
    print(f"MRR {mT}       : ${mrr[mT]:,.0f}")
    print(f"opex+cogs {mT} : ${opex_T:,.0f}")
    print(f"net burn {mT}  : ${mrr[mT]-opex_T:,.0f}")
    print(f"legacy acv scale   : {g.diag['legacy_acv_scale']}")
    print(f"defects            : {g.diag['defects']}")


if __name__ == "__main__":
    sys.exit(main())
