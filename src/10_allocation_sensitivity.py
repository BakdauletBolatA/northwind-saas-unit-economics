#!/usr/bin/env python3
"""
Northwind Cloud — step 10: how much of the recommendation is the allocation rule?

Q07 charges every deal in a channel the same CAC. For comparing channels that is
fine. For comparing segments *inside* one channel it is not: an Enterprise deal
consumes several times the AE and Sales Engineer effort of an SMB deal, so a
flat channel CAC quietly moves cost off the big deals and onto the small ones.

The memo's recommendation — fund an Enterprise outbound pod — is exactly a
comparison of segments inside one channel, so it is exposed to that choice.
This script prices every channel x segment both ways:

  equal split     channel cost pool / wins            (the Q07 basis)
  effort weighted pool allocated on wins x segment effort index
                  (SMB 1.0, Mid-Market 2.2, Enterprise 4.0 — a stated
                  assumption in config/assumptions.yml, not a measurement)

and then sweeps the Enterprise effort index to find where the answer flips
against the board's 18-month guardrail.

The channel cost pool is taken from Q07's own output (cac x wins) rather than
rebuilt here, so the two cannot drift apart.

Outputs:
  outputs/tables/allocation_sensitivity.csv    channel x segment, both bases
  outputs/tables/allocation_breakeven.csv      effort-index sweep for outbound
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "warehouse" / "northwind.db"
TAB = ROOT / "outputs" / "tables"
CONFIG = ROOT / "config" / "assumptions.yml"

WINDOW = ("2025-09", "2026-08")
MIN_WINS = 3                      # ниже трёх сделок средний чек ничего не значит
SWEEP = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]


def segment_wins(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT n.channel, n.segment,
               COUNT(*)            AS wins,
               SUM(n.new_acv)      AS new_acv,
               SUM(n.new_acv * n.gross_margin) / NULLIF(SUM(n.new_acv), 0) AS gross_margin
        FROM v_new_business n
        WHERE n.signup_month BETWEEN ? AND ?
          AND n.channel_unattributed = 0
        GROUP BY n.channel, n.segment
        """,
        con, params=WINDOW,
    )


def price(wins: pd.DataFrame, channel_cac: pd.Series, effort: dict[str, float]) -> pd.DataFrame:
    """CAC и окупаемость на обеих базах при заданном индексе трудоёмкости."""
    df = wins.copy()
    df["avg_new_acv"] = df["new_acv"] / df["wins"]
    df["monthly_gp_per_deal"] = df["avg_new_acv"] * df["gross_margin"] / 12.0
    df["cac_equal_split"] = df["channel"].map(channel_cac)

    df["effort_units"] = df["wins"] * df["segment"].map(effort)
    pool = df.groupby("channel").apply(
        lambda g: g["cac_equal_split"].iloc[0] * g["wins"].sum(), include_groups=False
    )
    total_effort = df.groupby("channel")["effort_units"].sum()
    df["cac_effort_wtd"] = (
        df["channel"].map(pool) * df["effort_units"] / df["channel"].map(total_effort) / df["wins"]
    )

    for basis in ("equal_split", "effort_wtd"):
        df[f"payback_{basis}"] = df[f"cac_{basis}"] / df["monthly_gp_per_deal"]
    return df


def verdict(row: pd.Series, hurdle: float) -> str:
    equal, effort = row["payback_equal_split"], row["payback_effort_wtd"]
    if equal <= hurdle and effort <= hurdle:
        return "clears on both bases"
    if equal <= hurdle:
        return "clears only on equal split"
    return "fails on both bases"


def main() -> int:
    if not DB.exists():
        print(f"Нет витрины {DB} — сначала ./run_all.sh", file=sys.stderr)
        return 2

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    effort = dict(cfg["segment_sales_effort_index"])
    hurdle = float(cfg["treasury"]["board_min_runway_months"])

    q07 = pd.read_csv(TAB / "Q07_cac_ltv_payback_by_channel.csv").set_index("channel")["cac"]
    con = sqlite3.connect(DB)
    wins = segment_wins(con)
    wins = wins[wins["wins"] >= MIN_WINS]

    priced = price(wins, q07, effort)
    priced["verdict"] = priced.apply(verdict, axis=1, hurdle=hurdle)
    out = priced[[
        "channel", "segment", "wins", "avg_new_acv", "monthly_gp_per_deal",
        "cac_equal_split", "cac_effort_wtd", "payback_equal_split",
        "payback_effort_wtd", "verdict",
    ]].round(1).sort_values(["channel", "segment"])
    TAB.mkdir(parents=True, exist_ok=True)
    out.to_csv(TAB / "allocation_sensitivity.csv", index=False)

    # Свип: при какой трудоёмкости Enterprise ответ переворачивается
    rows = []
    for index in SWEEP:
        swept = price(wins, q07, {**effort, "Enterprise": index})
        ent = swept[(swept["channel"] == "outbound_sdr") & (swept["segment"] == "Enterprise")]
        mid = swept[(swept["channel"] == "outbound_sdr") & (swept["segment"] == "MidMarket")]
        rows.append({
            "enterprise_effort_index": index,
            "outbound_enterprise_payback": round(float(ent["payback_effort_wtd"].iloc[0]), 1),
            "outbound_midmarket_payback": round(float(mid["payback_effort_wtd"].iloc[0]), 1),
            "clears_hurdle": bool(ent["payback_effort_wtd"].iloc[0] <= hurdle),
        })
    sweep = pd.DataFrame(rows)
    sweep.to_csv(TAB / "allocation_breakeven.csv", index=False)

    below = sweep[sweep["clears_hurdle"]]["enterprise_effort_index"].max()
    above = sweep[~sweep["clears_hurdle"]]["enterprise_effort_index"].min()

    print(out.to_string(index=False))
    print()
    print(sweep.to_string(index=False))
    print()
    ent = out[(out["channel"] == "outbound_sdr") & (out["segment"] == "Enterprise")].iloc[0]
    print(f"Outbound Enterprise: {ent['payback_equal_split']} мес при равном распределении, "
          f"{ent['payback_effort_wtd']} мес при распределении по трудоёмкости "
          f"(порог правления {hurdle:.0f} мес)")
    print(f"Ответ переворачивается между индексом {below} и {above}; "
          f"в assumptions.yml стоит {effort['Enterprise']}")
    print("\n  outputs/tables/allocation_sensitivity.csv")
    print("  outputs/tables/allocation_breakeven.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
