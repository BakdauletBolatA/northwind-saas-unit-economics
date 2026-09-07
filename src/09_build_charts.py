#!/usr/bin/env python3
"""
Northwind Cloud — step 9: render the README charts from the warehouse.

Five figures, each rendered twice (light and dark surface) so the README can
serve the right one via <picture media="(prefers-color-scheme: dark)">. The dark
variants use their own steps from the same ramps — not an automatic inversion.

Every number plotted is read from the warehouse or from outputs/tables at render
time, so the charts cannot drift from the analysis.

Palette: the validated reference instance. The three categorical slots used here
(blue / orange / aqua) pass the all-pairs CVD, normal-vision and lightness gates
in both modes; aqua is sub-3:1 on the light surface, so every chart that uses it
carries visible direct labels (the documented relief).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                                    # noqa: E402
import numpy as np                                                 # noqa: E402
import pandas as pd                                                # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm  # noqa: E402
from matplotlib.lines import Line2D                                # noqa: E402
from matplotlib.patches import PathPatch                           # noqa: E402
from matplotlib.path import Path as MPath                          # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "warehouse" / "northwind.db"
TAB = ROOT / "outputs" / "tables"
IMG = ROOT / "docs" / "img"

THEME = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", muted="#898781",
                  grid="#e1e0d9", axis="#c3c2b7",
                  s1="#2a78d6", s2="#eb6834", s3="#1baf7a",
                  critical="#d03b3b", deemph="#c3c2b7",
                  div_lo="#8f2020", div_mid="#f0efec", div_hi="#0d366b",
                  ramp=["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]),
    "dark": dict(surface="#1a1a19", ink="#ffffff", ink2="#c3c2b7", muted="#898781",
                 grid="#2c2c2a", axis="#383835",
                 s1="#3987e5", s2="#d95926", s3="#199e70",
                 critical="#d03b3b", deemph="#52514e",
                 div_lo="#a83030", div_mid="#383835", div_hi="#86b6ef",
                 ramp=["#0d366b", "#1c5cab", "#3987e5", "#86b6ef", "#cde2fb"]),
}

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "axes.linewidth": 0.8,
    "xtick.major.width": 0.8,
    "ytick.major.width": 0.8,
})


# --------------------------------------------------------------------------
# chrome helpers
# --------------------------------------------------------------------------
def frame(t, figsize):
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(t["surface"])
    ax.set_facecolor(t["surface"])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=t["muted"], labelsize=9, length=0)
    return fig, ax


def titles(fig, t, title, subtitle, caption=None):
    fig.text(0.035, 0.955, title, color=t["ink"], fontsize=14.5, fontweight="bold",
             va="top")
    fig.text(0.035, 0.902, subtitle, color=t["ink2"], fontsize=10, va="top")
    if caption:
        fig.text(0.035, 0.028, caption, color=t["muted"], fontsize=8.2, va="bottom")


def rounded(ax, t, x, y, w, h, side, color, alpha=1.0, z=3):
    """Rectangle with a 4px rounded data-end, square at the baseline."""
    inv = ax.transData.inverted()
    p0, p1 = inv.transform((0, 0)), inv.transform((4, 4))
    rx, ry = abs(p1[0] - p0[0]), abs(p1[1] - p0[1])
    rx, ry = min(rx, abs(w) / 2 or rx), min(ry, abs(h) / 2 or ry)
    x1, y1 = x + w, y + h
    if side == "right":
        v = [(x, y), (x1 - rx, y), (x1, y), (x1, y + ry), (x1, y1 - ry),
             (x1, y1), (x1 - rx, y1), (x, y1), (x, y)]
    elif side == "top":
        v = [(x, y), (x1, y), (x1, y1 - ry), (x1, y1), (x1 - rx, y1),
             (x + rx, y1), (x, y1), (x, y1 - ry), (x, y)]
    elif side == "bottom":
        v = [(x, y1), (x1, y1), (x1, y + ry), (x1, y), (x1 - rx, y),
             (x + rx, y), (x, y), (x, y + ry), (x, y1)]
    else:
        v = [(x, y), (x1, y), (x1, y1), (x, y1), (x, y)]
    codes = {
        "right": [1, 2, 3, 3, 2, 3, 3, 2, 79],
        "top":   [1, 2, 2, 3, 3, 2, 3, 3, 79],
        "bottom": [1, 2, 2, 3, 3, 2, 3, 3, 79],
        "none":  [1, 2, 2, 2, 79],
    }[side]
    ax.add_patch(PathPatch(MPath(v, codes), facecolor=color, edgecolor="none",
                           alpha=alpha, zorder=z))


def money(v, unit="m"):
    sign = "-" if v < 0 else ""
    return (f"{sign}${abs(v)/1e6:.2f}m" if unit == "m"
            else f"{sign}${abs(v):,.0f}")


# --------------------------------------------------------------------------
# 1. cash by scenario
# --------------------------------------------------------------------------
def chart_cash(t, mode):
    cp = pd.read_csv(TAB / "scenario_cash_paths.csv")
    ss = pd.read_csv(TAB / "scenario_summary.csv").set_index("scenario")
    fig, ax = frame(t, (9.2, 5.4))
    fig.subplots_adjust(left=0.085, right=0.80, top=0.79, bottom=0.225)

    x = np.arange(1, len(cp) + 1)
    series = [("base", "Base — hold the line", t["s1"]),
              ("sales_proposal", "Sales proposal  +$180k/mo", t["s2"]),
              ("selective", "Selective  +$88.5k/mo", t["s3"])]

    ax.axhline(0, color=t["axis"], lw=1.0, zorder=1)
    ax.grid(axis="y", color=t["grid"], lw=0.8, zorder=0)
    ax.set_axisbelow(True)

    ends = []
    for key, label, col in series:
        y = cp[key].to_numpy()
        ax.plot(x, y, color=col, lw=2.0, solid_capstyle="round",
                solid_joinstyle="round", zorder=4, label=label)
        ends.append((y[-1], col, label, ss.loc[key, "runway_months"]))

    # the crossing is the whole point of the chart
    sp = cp["sales_proposal"].to_numpy()
    cross = int(np.argmax(sp < 0))
    if sp[cross] < 0:
        ax.plot([x[cross]], [sp[cross]], "o", ms=9, color=t["s2"],
                mec=t["surface"], mew=2, zorder=6)
        ax.annotate(f"cash out\n{cp.month.iloc[cross]}",
                    xy=(x[cross], sp[cross]), xytext=(x[cross] - 3.6, sp[cross] - 1.35e6),
                    color=t["ink"], fontsize=9.5, fontweight="bold", ha="center",
                    arrowprops=dict(arrowstyle="-", color=t["muted"], lw=0.9,
                                    shrinkA=0, shrinkB=6))

    # direct labels at the right edge, de-collided with leader lines
    ends.sort(key=lambda r: r[0])
    span = ax.get_ylim()
    # two-line labels need ~30px of clearance; leader lines keep each one
    # attached to its own series where base and selective converge at the edge
    gap = (span[1] - span[0]) * 0.115
    placed = []
    for val, col, label, runway in ends:
        pos = val
        for p in placed:
            if abs(pos - p) < gap:
                pos = p + gap
        placed.append(pos)
        ax.annotate(f"{money(val)}\n{runway} mo runway",
                    xy=(x[-1], val), xytext=(x[-1] + 0.9, pos),
                    color=t["ink"], fontsize=9.2, va="center", fontweight="bold",
                    annotation_clip=False,
                    arrowprops=dict(arrowstyle="-", color=t["muted"], lw=0.8,
                                    shrinkA=2, shrinkB=0))

    ax.set_xlim(0.5, len(cp) + 0.6)
    ax.set_xticks(x[::3])
    ax.set_xticklabels(cp.month[::3], fontsize=9)
    ax.set_yticks(np.arange(-2e6, 7e6, 1e6))
    ax.set_yticklabels([("-" if v < 0 else "") + f"${abs(v):.0f}m"
                        for v in np.arange(-2, 7, 1)])
    ax.set_ylim(-2.4e6, 6.6e6)

    leg = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.155), ncol=3,
                    frameon=False, fontsize=9.3, handlelength=1.6,
                    columnspacing=2.0, handletextpad=0.7)
    for txt in leg.get_texts():
        txt.set_color(t["ink2"])

    titles(fig, t,
           "The $180k ask runs the company out of cash before the board's horizon",
           "Projected cash balance, 18 months from Aug 2026. $6.1m opening cash.",
           "Source: outputs/tables/scenario_cash_paths.csv · Runway beyond month 18 "
           "extrapolated at the exit burn rate.")
    return fig


# --------------------------------------------------------------------------
# 2. CAC payback by channel
# --------------------------------------------------------------------------
def chart_payback(t, mode):
    q7 = pd.read_csv(TAB / "Q07_cac_ltv_payback_by_channel.csv")
    q7 = q7.sort_values("cac_payback_months")
    labels = {"partner": "Partner / Reseller", "inbound_content": "Inbound / Content",
              "paid_search": "Paid Search", "outbound_sdr": "Outbound SDR",
              "events": "Events / Field", "paid_social": "Paid Social"}
    fig, ax = frame(t, (9.2, 5.0))
    fig.subplots_adjust(left=0.235, right=0.955, top=0.775, bottom=0.235)

    y = np.arange(len(q7))[::-1]
    ax.set_xlim(0, 78)
    ax.set_ylim(-0.9, len(q7) - 0.02)
    ax.grid(axis="x", color=t["grid"], lw=0.8, zorder=0)
    ax.set_axisbelow(True)

    for yi, (_, r) in zip(y, q7.iterrows()):
        fail = r.cac_payback_months > 18
        col = t["critical"] if fail else t["s1"]
        rounded(ax, t, 0, yi - 0.15, r.cac_payback_months, 0.30, "right", col)
        ax.text(r.cac_payback_months + 1.4, yi, f"{r.cac_payback_months:.1f} mo",
                va="center", ha="left", color=t["ink"], fontsize=9.4,
                fontweight="bold")
        ax.text(-1.8, yi, labels.get(r.channel, r.channel), va="center", ha="right",
                color=t["ink"], fontsize=9.8)
        ax.text(-1.8, yi - 0.30, f"n={int(r.n_wins)}  ·  CAC ${r.cac:,.0f}",
                va="center", ha="right", color=t["muted"], fontsize=8.1)

    ax.axvline(18, color=t["ink2"], lw=1.2, zorder=5)
    ax.text(18.6, len(q7) - 0.10, "18-month hurdle — the cash we have",
            color=t["ink2"], fontsize=9, va="bottom", fontweight="bold")

    ax.set_yticks([])
    ax.set_xticks([0, 18, 30, 45, 60, 75])
    ax.set_xlabel("CAC payback, months of customer gross profit", color=t["ink2"],
                  fontsize=9.5, labelpad=8)

    handles = [Line2D([], [], marker="s", ls="", ms=9, color=t["s1"],
                      label="Pays back inside the runway"),
               Line2D([], [], marker="s", ls="", ms=9, color=t["critical"],
                      label="Does not")]
    leg = ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.155),
                    ncol=2, frameon=False, fontsize=9.2, handletextpad=0.6,
                    columnspacing=2.4)
    for txt in leg.get_texts():
        txt.set_color(t["ink2"])

    titles(fig, t, "Only one channel pays back inside the runway",
           "Trailing 12 months. Cost includes programme spend, SDR payroll, "
           "allocated AE/SE/marketing and commission.",
           "Source: sql/Q07 · Excludes 452 pre-window customers, who carry no "
           "acquisition cost, and 4 logos with no channel attribution.")
    return fig


# --------------------------------------------------------------------------
# 3. payback by channel x segment
# --------------------------------------------------------------------------
def chart_channel_segment(t, mode):
    q8 = pd.read_csv(TAB / "Q08_channel_economics_cuts.csv")
    d = q8[(q8.cut_type == "by_segment") & (q8.n_wins >= 8)].copy()
    labels = {"partner": "Partner / Reseller", "inbound_content": "Inbound / Content",
              "paid_search": "Paid Search", "outbound_sdr": "Outbound SDR",
              "paid_social": "Paid Social"}
    seg_col = {"Enterprise": t["s1"], "MidMarket": t["s2"], "SMB": t["s3"]}
    order = (d.groupby("channel").payback_months.min().sort_values().index.tolist())

    fig, ax = frame(t, (9.2, 4.9))
    fig.subplots_adjust(left=0.215, right=0.955, top=0.775, bottom=0.245)
    ax.set_xlim(0, 95)
    ax.set_ylim(-0.85, len(order) - 0.02)
    ax.grid(axis="x", color=t["grid"], lw=0.8, zorder=0)
    ax.set_axisbelow(True)

    for i, ch in enumerate(order):
        yi = len(order) - 1 - i
        g = d[d.channel == ch].sort_values("payback_months")
        if len(g) > 1:
            ax.plot([g.payback_months.min(), g.payback_months.max()], [yi, yi],
                    color=t["deemph"], lw=2.0, zorder=2, solid_capstyle="round")
        for _, r in g.iterrows():
            ax.plot([r.payback_months], [yi], "o", ms=11, color=seg_col[r.segment],
                    mec=t["surface"], mew=2, zorder=5)
            ax.text(r.payback_months, yi + 0.30, f"{r.payback_months:.0f}",
                    ha="center", va="bottom", color=t["ink"], fontsize=8.8,
                    fontweight="bold")
        ax.text(-2.2, yi, labels.get(ch, ch), va="center", ha="right",
                color=t["ink"], fontsize=9.8)

    ax.axvline(18, color=t["ink2"], lw=1.2, zorder=6)
    ax.text(18.7, len(order) - 0.10, "18-month hurdle", color=t["ink2"], fontsize=9,
            va="bottom", fontweight="bold")

    # No in-plot callout here: any label wide enough to say it collided with
    # the neighbouring row's value. The subtitle carries the message and the
    # dumbbell length carries the spread.

    ax.set_yticks([])
    ax.set_xticks([0, 18, 40, 60, 80])
    ax.set_xlabel("CAC payback, months", color=t["ink2"], fontsize=9.5, labelpad=8)

    handles = [Line2D([], [], marker="o", ls="", ms=9, color=seg_col[s],
                      mec=t["surface"], mew=1.5, label=s)
               for s in ["Enterprise", "MidMarket", "SMB"]]
    leg = ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.165),
                    ncol=3, frameon=False, fontsize=9.3, handletextpad=0.5,
                    columnspacing=2.4)
    for txt in leg.get_texts():
        txt.set_color(t["ink2"])

    titles(fig, t, "The blended number hides the decision",
           "Outbound pays back in 12 months on Enterprise and 40 on Mid-Market — "
           "and sells mostly Mid-Market.",
           "Source: sql/Q08 · Cells with fewer than 8 wins excluded as too thin to "
           "carry a rate. Cost per deal is constant within a channel.")
    return fig


# --------------------------------------------------------------------------
# 4. quarterly cohort retention
# --------------------------------------------------------------------------
def chart_cohorts(t, mode):
    tri = pd.read_csv(TAB / "cohort_triangle_quarterly_revenue_retention.csv",
                      index_col=0)
    size = tri["cohort_size"]
    m = tri.drop(columns=["cohort_size"])
    m.columns = [int(c) for c in m.columns]
    m = m[[c for c in m.columns if c <= 12]]
    data = np.ma.masked_invalid(m.to_numpy(dtype=float))

    cmap = LinearSegmentedColormap.from_list(
        "div", [t["div_lo"], t["div_mid"], t["div_hi"]], N=256)
    cmap.set_bad(t["surface"])
    norm = TwoSlopeNorm(vmin=75, vcenter=100, vmax=125)

    fig, ax = frame(t, (9.2, 5.2))
    fig.subplots_adjust(left=0.145, right=0.905, top=0.775, bottom=0.165)
    ax.pcolormesh(np.arange(len(m.columns) + 1), np.arange(len(m) + 1),
                  data[::-1], cmap=cmap, norm=norm,
                  edgecolors=t["surface"], linewidth=1.6)

    ax.set_xticks(np.arange(len(m.columns)) + 0.5)
    ax.set_xticklabels(m.columns, fontsize=8.8)
    ax.set_yticks(np.arange(len(m)) + 0.5)
    ax.set_yticklabels([f"{c}   n={int(size[c])}" for c in m.index[::-1]], fontsize=8.8)
    ax.set_xlabel("Months since acquisition", color=t["ink2"], fontsize=9.5, labelpad=7)
    ax.tick_params(axis="y", colors=t["ink2"])

    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    cb = fig.colorbar(sm, ax=ax, pad=0.02, fraction=0.033, ticks=[75, 100, 125])
    cb.ax.set_yticklabels(["75%", "100%\nheld", "125%"], fontsize=8.6,
                          color=t["ink2"])
    cb.outline.set_visible(False)
    cb.ax.tick_params(length=0)

    titles(fig, t, "Cohorts hold, and the recent ones expand",
           "Revenue retention by acquisition quarter: cohort MRR as a share of its "
           "own first month. Blue is above 100%.",
           "Source: outputs/tables/cohort_triangle_quarterly_revenue_retention.csv\n"
           "Quarterly, not monthly: monthly cohorts here are 1-9 logos, too small to "
           "carry a rate. Blank cells are not yet observed.")
    return fig


# --------------------------------------------------------------------------
# 5. ARR bridge waterfall
# --------------------------------------------------------------------------
def chart_bridge(t, mode):
    # Components must be read at FULL precision. Building this from the Q12 CSV
    # instead used values rounded to whole dollars, which left a $0.56 residual
    # printed on a chart whose entire point is that the bridge ties exactly.
    con = sqlite3.connect(DB)
    pnl = pd.read_sql("SELECT month, arr FROM v_pnl_month ORDER BY month_index", con)
    tot = pd.read_sql("""
        SELECT SUM(CASE WHEN movement_type='new'          THEN movement_amount ELSE 0 END)*12 AS new_logo_arr,
               SUM(CASE WHEN movement_type='expansion'    THEN movement_amount ELSE 0 END)*12 AS expansion_arr,
               SUM(CASE WHEN movement_type='reactivation' THEN movement_amount ELSE 0 END)*12 AS reactivation_arr,
               SUM(CASE WHEN movement_type='contraction'  THEN movement_amount ELSE 0 END)*12 AS contraction_arr,
               SUM(CASE WHEN movement_type='churn'        THEN movement_amount ELSE 0 END)*12 AS churn_arr
        FROM v_customer_month WHERE month BETWEEN '2025-09' AND '2026-08'""",
                      con).iloc[0]
    con.close()
    opening = float(pnl[pnl.month == "2025-08"].arr.iloc[0])
    closing = float(pnl[pnl.month == "2026-08"].arr.iloc[0])

    steps = [("Opening\nAug 2025", opening, "total"),
             ("New\nlogos", float(tot.new_logo_arr), "up"),
             ("Expansion", float(tot.expansion_arr), "up"),
             ("Reactivation", float(tot.reactivation_arr), "up"),
             ("Contraction", float(tot.contraction_arr), "down"),
             ("Churn", float(tot.churn_arr), "down"),
             ("Closing\nAug 2026", closing, "total")]

    fig, ax = frame(t, (9.2, 5.2))
    fig.subplots_adjust(left=0.095, right=0.965, top=0.775, bottom=0.135)
    ax.grid(axis="y", color=t["grid"], lw=0.8, zorder=0)
    ax.set_axisbelow(True)

    running, bw = 0.0, 0.46
    for i, (lab, val, kind) in enumerate(steps):
        if kind == "total":
            rounded(ax, t, i - bw / 2, 0, bw, val, "top", t["ink2"], alpha=0.85)
            top, base = val, 0.0
            running = val
        else:
            base = running
            top = running + val
            col = t["div_hi"] if kind == "up" else t["critical"]
            side = "top" if val >= 0 else "bottom"
            rounded(ax, t, i - bw / 2, base, bw, val, side, col)
            if i < len(steps) - 1:
                ax.plot([i + bw / 2, i + 1 - bw / 2], [top, top], color=t["axis"],
                        lw=1.0, zorder=2)
            running = top
        y = max(top, base)
        ax.text(i, y + 2.3e5, f"{'+' if kind == 'up' else ''}"
                              f"{val/1e6:.2f}m" if kind != "total"
                              else f"${val/1e6:.2f}m",
                ha="center", va="bottom", color=t["ink"], fontsize=9.3,
                fontweight="bold")

    ax.plot([0 + bw / 2, 1 - bw / 2], [opening, opening], color=t["axis"], lw=1.0,
            zorder=2)
    ax.set_xticks(range(len(steps)))
    ax.set_xticklabels([s[0] for s in steps], fontsize=9, color=t["ink2"])
    ax.set_yticks(np.arange(0, 1.5e7, 2.5e6))
    ax.set_yticklabels([f"${v:.1f}m" for v in np.arange(0, 15, 2.5)])
    ax.set_ylim(0, 1.60e7)
    ax.set_xlim(-0.7, len(steps) - 0.3)
    ax.axhline(0, color=t["axis"], lw=1.0)

    resid = opening + sum(v for _, v, k in steps[1:-1]) - closing
    ax.text(len(steps) - 1, 1.50e7, f"residual  ${resid:,.2f}", ha="right",
            color=t["ink2"], fontsize=9.5, fontweight="bold")

    handles = [Line2D([], [], marker="s", ls="", ms=9, color=t["div_hi"], label="Adds ARR"),
               Line2D([], [], marker="s", ls="", ms=9, color=t["critical"], label="Removes ARR"),
               Line2D([], [], marker="s", ls="", ms=9, color=t["ink2"], label="Balance")]
    leg = ax.legend(handles=handles, loc="upper left", frameon=False, fontsize=9.2,
                    handletextpad=0.6, ncol=1)
    for txt in leg.get_texts():
        txt.set_color(t["ink2"])

    titles(fig, t, "The ARR bridge ties to the cent",
           "Twelve months to Aug 2026. Every movement is derived from "
           "month-over-month MRR, never copied from a source system.",
           "Source: sql/Q02 and sql/Q12 · The residual is checked in all 36 months "
           "by gate G2 in src/06_validate.py.")
    return fig


# --------------------------------------------------------------------------
CHARTS = [("cash-runway-by-scenario", chart_cash),
          ("cac-payback-by-channel", chart_payback),
          ("payback-by-channel-segment", chart_channel_segment),
          ("cohort-retention-quarterly", chart_cohorts),
          ("arr-bridge-waterfall", chart_bridge)]


def main() -> int:
    IMG.mkdir(parents=True, exist_ok=True)
    for name, fn in CHARTS:
        for mode, t in THEME.items():
            fig = fn(t, mode)
            out = IMG / f"{name}-{mode}.png"
            fig.savefig(out, facecolor=t["surface"], edgecolor="none")
            plt.close(fig)
        print(f"  {name:<32s} light + dark")
    print(f"\n{len(CHARTS)} charts x 2 modes -> {IMG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
