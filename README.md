# Northwind Cloud — SaaS unit economics and runway analysis

> **The question, from the CEO:** *"We're burning $340k a month with $6.1m in the
> bank. Sales wants to double the SDR team — another $180k a month. I don't
> understand whether the current sales machine pays back at all. I need an answer
> before the board meets in two weeks."*

**The answer: hire selectively.** Reject the $180k request, approve an $88.5k
Enterprise-focused pod, and fund it by switching off Paid Social.

| | Base | Sales proposal (+$180k/mo) | **Selective (recommended)** |
|---|---:|---:|---:|
| Incremental S&M | $0 | $180,000/mo | **$88,500/mo** |
| Incremental CAC | — | $79,788 | **$45,671** |
| CAC payback | — | **28.0 months** | **11.2 months** |
| ARR at month 18 | $19.72m | $20.96m | **$21.04m** |
| Cash at month 18 | $806k | **−$1.70m** | **$1.07m** |
| Runway | 21 months | **14 months** | **23 months** |

The company has 18.2 months of cash. It cannot fund a 28-month payback. The
selective option delivers *more* ARR than the full proposal for half the money,
because it sells Enterprise deals that are 3.5x larger at the same cost to win.

**→ [Read the full memo](docs/CEO_MEMO.md)**

---

## What the analysis found

**1. Outbound isn't one channel, it's two businesses.** Blended, it pays back in
26.1 months — mediocre and un-actionable. Split by segment, the answer is obvious:

| Outbound SDR, trailing 12m | Logos | Avg ACV | CAC | Payback |
|---|---:|---:|---:|---:|
| Enterprise | 13 | $93,486 | $70,164 | **12.3 months** |
| Mid-Market | 29 | $26,764 | $70,164 | **40.3 months** |
| SMB | 4 | $8,556 | $70,164 | 120 months |

An outbound deal costs about the same to win whatever its size. 29 of 46 wins
went into the segment where that cost takes forty months to return.

**2. Paid Social returns 29 cents on the dollar.** It is the largest programme
line at $75,500/month. Over twelve months it consumed $1.11m of fully allocated
cost to buy $241,428 of new ARR at $6,706 a deal, churning at 6.34% per month —
a 68.5-month payback and LTV/CAC of **0.29**. Cohort retention at six months fell
from 91.4% to 64.8% after the agency switched to broad-reach bidding in November
2024; cost per lead fell, which is why the channel looked like it was working.

**3. Blended NRR of 104.7% hides a leaking segment.** Enterprise 121.1%,
Mid-Market 104.4%, SMB **82.0%**. SMB is 14.6% of ARR, 48% of new logos, and
pays back in 62 months.

**4. SDR #10 through #18 are not worth what SDR #3 was.** When the team went 4 →
9, meetings per fully-ramped rep fell from **12.50 to 9.49**. Extending that
decay to eighteen reps gives 5.50 incremental meetings per added rep; clearing an
18-month payback needs 8.57. The proposal is 36% short.

---

## Method

```
config/assumptions.yml ─→ 01 generate ─→ 02 warehouse ─→ 03 SQL library ─┐
                                                                         ├─→ 06 validate
                                          04 cash model ─→ 05 Excel ─────┘
                                                        ─→ 07 Power BI  ─→ 08 docs
```

| Step | Script | What it does |
|---|---|---|
| 1 | `01_generate_data.py` | Driver-based simulator: spend → leads → SQLs → wins → subscription lifecycle. Seeded and byte-reproducible. Grain: **subscription × month**, 36 months. |
| 2 | `02_build_warehouse.py` | SQLite star schema. 13 numbered cleaning rules, full `etl_audit` trail, `dq_quarantine` for everything removed. |
| 3 | `03_run_sql_library.py` | 12 analytical queries + 1 audit query. Two integrity gates abort the pipeline on failure. |
| 4 | `04_cashflow_model.py` | Forecast bake-off, then an 18-month, 3-scenario cash and runway model with break-even sensitivities. |
| 5 | `05_build_excel_model.py` | Formula-driven workbook with a live scenario switch. |
| 6 | `06_validate.py` | Seven gates: determinism, bridge, cohorts, audit, Excel recalculation, Excel-vs-SQL, scenario switch. |
| 7 | `07_export_powerbi.py` | Star-schema extracts + [DAX guide](docs/POWERBI_GUIDE.md). |
| 8 | `08_generate_docs.py` | Regenerates the [data dictionary](docs/DATA_DICTIONARY.md) and [cleaning rules](docs/CLEANING_RULES.md) from the warehouse. |

### Choices worth arguing with

**The forecast model was chosen by backtest, not by preference.** Five methods,
rolling-origin evaluation, two window policies, 93 out-of-sample points each:

| Method | MAPE (expanding) | MAPE (rolling-24) |
|---|---:|---:|
| **Holt linear** | **3.46%** | **3.42%** |
| Driver (NRR + new MRR) | 3.78% | 3.78% |
| Drift | 7.33% | 6.96% |
| Log-linear | 10.80% | 9.11% |
| Naive | 13.15% | 13.15% |

I expected the bottom-up driver model to win — it is the only candidate that
knows the series is a subscription book. It lost. Holt is used.

**CAC excludes 452 of 826 customers.** They were acquired before the S&M ledger
begins and carry no cost. Leaving them in the denominator roughly halves CAC.
This is the most common way SaaS CAC gets flattered, and the exclusion is
enforced in `v_new_business` rather than left to the analyst to remember.

**Missing attribution is never imputed.** Segment is imputed from ARR bands and
flagged. Channel is not — attribution cannot be reconstructed from revenue, so
those customers go to an `unattributed` bucket and out of every CAC denominator.

**LTV is shown three ways** because the simple version flatters everything:
perpetuity on logo churn, a 60-month horizon on observed net revenue retention,
and that horizon discounted at 1%/month. The recommendation uses the second. On
the perpetuity method Outbound looks like a 6.4x return; on the finite-horizon
method it is 3.0x. The perpetuity method assumes a constant hazard forever and
ignores contraction entirely.

**Outliers are flagged, not removed.** The only outlier treatment applied
anywhere is de-duplicating the February 2026 billing-migration double-post, which
has a documented mechanical cause. 18 extreme MRR movements carry
`outlier_flag = 1` and stay in the history. Removing unexplained outliers is
curve-fitting.

### The weakest assumption

The recommendation turns on the pod holding an Enterprise-weighted win mix. It
fails the 18-month test below roughly **19% Enterprise share**; the current
outbound team runs 28%. That is why it is a monitored condition of approval, not
a footnote. Territory overlap — the assumption I expected to be fragile — turned
out not to matter: even at 100% overlap payback is 16.4 months and still clears.

---

## Verification

Nothing here is asserted without a check. `python src/06_validate.py`:

| Gate | Result |
|---|---|
| G1 Generator determinism (SHA-256 on re-run) | PASS — 6/6 files byte-identical |
| G2 ARR bridge ties to the cent | PASS — max residual **$0.0000** across 36 months |
| G3 Cohort triangle foots to total MRR | PASS — 36/36 months |
| G4 ETL audit accounts for every source row | PASS — 17,310 in, 176 quarantined with a reason |
| G5 Excel recalculates with zero formula errors | PASS — LibreOffice headless recalc |
| G6 Excel reconciles to SQL | PASS — **24/24** checks |
| G7 Scenario switch is live | PASS — 3 distinct cash paths, max Δ vs Python **$0.41** |

Tolerances are set to exactly half the last displayed unit of whatever produced
the reference value, so a PASS means "identical apart from display rounding" —
not "close enough". A green recalculation only proves nothing is broken, which is
why G6 and G7 exist separately.

---

## Reproduce

```bash
git clone https://github.com/BakdauletBolatA/northwind-saas-unit-economics
cd northwind-saas-unit-economics
pip install -r requirements.txt
apt-get install -y libreoffice-calc     # G5-G7 recalculate the workbook headlessly
./run_all.sh                            # ~3 minutes end to end
```

Everything is driven by `config/assumptions.yml`. Change a number there — a
channel's spend, a segment's churn, the SDR saturation exponent — re-run
`./run_all.sh`, and the whole chain moves, Excel workbook included. The seed is
fixed, so two runs of the generator produce byte-identical files.

### Output

| Path | What |
|---|---|
| `docs/CEO_MEMO.md` | The answer |
| `outputs/excel/northwind_unit_economics.xlsx` | Scenario model, live switch |
| `outputs/tables/*.csv` | SQL results, backtest, scenarios, reconciliation |
| `outputs/powerbi/*.csv` | Star-schema extracts |
| `data/warehouse/northwind.db` | SQLite warehouse |

---

## The scenario

Northwind Cloud sells cloud warehouse-management software to B2B distributors on
subscription. It grew 2021–2023 on a low-touch SMB motion and has since moved
upmarket, which is why the installed base is SMB-heavy while new bookings are
not.

Six business events are planted in the generated data for the analysis to
recover, and the analysis recovers them from the data rather than from the
config: a Paid Social lead-quality collapse (Nov 2024), an Enterprise expansion
wave following a product launch (Jun 2025), an SMB price increase and the churn
spike behind it (Feb 2025), the SDR scale-up from 4 to 9 (Sep 2025), a partner
co-sell programme (Mar 2025), and a billing-engine migration that double-posted a
month of invoices (Feb 2026). The data also carries realistic defects — duplicate
billing lines, back-dated credit memos, orphan rows, negative-seat corruption and
missing attributes — which the ETL detects, handles and logs rather than being
handed clean input.

*Northwind Cloud is fictional and the data is synthetic, generated by this
repository's own driver-based simulator; the method, the reconciliations and the
reasoning are real.*
