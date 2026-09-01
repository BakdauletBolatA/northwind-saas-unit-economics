# Power BI build guide — Northwind Cloud unit economics

Everything below is written against the exact column names in
`outputs/powerbi/*.csv`. They are the contract; `outputs/powerbi/_schema_manifest.csv`
lists every table, row count and column. If you rename a column, the DAX breaks.

Run `python src/07_export_powerbi.py` to regenerate the extracts.

---

## 1. Load

**Get Data → Text/CSV**, point at `outputs/powerbi/`, load these twelve tables:

| Table | Rows | Role |
|---|---:|---|
| `dim_date` | 54 | Date dimension (36 actual + 18 forecast months) |
| `dim_customer` | 826 | Customer dimension, wide |
| `dim_channel` | 7 | Acquisition channel |
| `dim_segment` | 3 | SMB / Mid-Market / Enterprise |
| `fact_subscription_month` | 17,181 | **Grain: customer × month.** The core fact |
| `fact_marketing_spend` | 216 | Channel × month programme spend and funnel |
| `fact_opex` | 720 | Operating expense register |
| `fact_headcount` | 288 | Role × month headcount and loaded cost |
| `fact_credit_memo` | 239 | One-off credits, deliberately outside MRR |
| `bridge_cohort_age` | 17,181 | Customer × month → months since cohort |
| `ref_events` | 6 | The named business events, for annotations |
| `etl_audit` | 13 | Cleaning-rule audit trail |

In Power Query set these types explicitly (CSV type inference gets `MonthLabel`
wrong and turns `2026-08` into a date):

- `dim_date[MonthKey]`, all `*Key` columns → **Whole Number**
- `dim_date[MonthLabel]`, `dim_customer[SignupMonth]`, `[CohortMonth]`,
  `[ChurnMonth]` → **Text**
- `dim_date[MonthEndDate]` → **Date**
- all money and rate columns → **Decimal Number**

---

## 2. Model

Mark **`dim_date` as the date table** on `MonthEndDate`, then build these
relationships. All are single-direction, many-to-one, from fact to dimension.

| From | To | Cardinality | Direction |
|---|---|---|---|
| `fact_subscription_month[MonthKey]` | `dim_date[MonthKey]` | \* → 1 | Single |
| `fact_subscription_month[CustomerKey]` | `dim_customer[CustomerKey]` | \* → 1 | Single |
| `fact_marketing_spend[MonthKey]` | `dim_date[MonthKey]` | \* → 1 | Single |
| `fact_marketing_spend[ChannelKey]` | `dim_channel[ChannelKey]` | \* → 1 | Single |
| `fact_opex[MonthKey]` | `dim_date[MonthKey]` | \* → 1 | Single |
| `fact_headcount[MonthKey]` | `dim_date[MonthKey]` | \* → 1 | Single |
| `fact_credit_memo[MonthKey]` | `dim_date[MonthKey]` | \* → 1 | Single |
| `fact_credit_memo[CustomerKey]` | `dim_customer[CustomerKey]` | \* → 1 | Single |
| `bridge_cohort_age[CustomerKey]` + `[MonthKey]` | composite — see note | | |

`dim_customer[Segment]` → `dim_segment[Segment]` and
`dim_customer[Channel]` → `dim_channel[Channel]` are optional; the attributes
are already denormalised onto `dim_customer`, so slice from there and keep
`dim_channel`/`dim_segment` for `SalesTouchWeight`, `GrossMargin` and
`SalesEffortIndex` only.

**Cohort bridge.** Power BI cannot make a two-column relationship. Add a
composite key in Power Query on both `fact_subscription_month` and
`bridge_cohort_age`:

```m
= Table.AddColumn(#"Previous Step", "CustMonthKey",
    each Text.From([CustomerKey]) & "-" & Text.From([MonthKey]), type text)
```

Then relate `fact_subscription_month[CustMonthKey]` → `bridge_cohort_age[CustMonthKey]`
(1 → 1, single direction) and use `bridge_cohort_age[MonthsSinceCohort]` on the
columns of your retention matrix.

Set `dim_segment[SortOrder]` as the **Sort by column** for `dim_segment[Segment]`
so SMB / Mid-Market / Enterprise stop sorting alphabetically.

---

## 3. DAX measures

Create a blank table called `_Measures` and put everything below in it.

### 3.1 Revenue base

```dax
MRR = SUM ( fact_subscription_month[Mrr] )

ARR = [MRR] * 12

Credit Memos = SUM ( fact_credit_memo[CreditAmount] )

Recognised Revenue = [MRR] + [Credit Memos]

Active Customers =
CALCULATE (
    DISTINCTCOUNT ( fact_subscription_month[CustomerKey] ),
    fact_subscription_month[IsActive] = 1
)

ARPA = DIVIDE ( [MRR], [Active Customers] )

ARR YoY % =
VAR Prior = CALCULATE ( [ARR], DATEADD ( dim_date[MonthEndDate], -12, MONTH ) )
RETURN DIVIDE ( [ARR] - Prior, Prior )
```

### 3.2 The ARR bridge

`MovementType` values are `opening_balance`, `new`, `expansion`,
`reactivation`, `contraction`, `churn`, `flat`. Contraction and churn are stored
**negative**, so the sign convention below is what makes the bridge foot.

```dax
Movement New =
CALCULATE ( SUM ( fact_subscription_month[MovementArr] ),
            fact_subscription_month[MovementType] = "new" )

Movement Carry-in =
CALCULATE ( SUM ( fact_subscription_month[MovementArr] ),
            fact_subscription_month[MovementType] = "opening_balance" )

Movement Expansion =
CALCULATE ( SUM ( fact_subscription_month[MovementArr] ),
            fact_subscription_month[MovementType] = "expansion" )

Movement Reactivation =
CALCULATE ( SUM ( fact_subscription_month[MovementArr] ),
            fact_subscription_month[MovementType] = "reactivation" )

Movement Contraction =
CALCULATE ( SUM ( fact_subscription_month[MovementArr] ),
            fact_subscription_month[MovementType] = "contraction" )

Movement Churn =
CALCULATE ( SUM ( fact_subscription_month[MovementArr] ),
            fact_subscription_month[MovementType] = "churn" )

Opening ARR =
CALCULATE ( [ARR], PREVIOUSMONTH ( dim_date[MonthEndDate] ) )

Bridge Residual =
[Opening ARR] + [Movement Carry-in] + [Movement New] + [Movement Expansion]
    + [Movement Reactivation] + [Movement Contraction] + [Movement Churn] - [ARR]
```

`Bridge Residual` must render as 0.00 in every month. Put it on the waterfall
page as a hidden card with conditional formatting — if it ever lights up, the
model is wrong and every retention number on the report is unreliable.

### 3.3 Retention

Trailing-12-month, anchored on the customers live twelve months ago. This is
the same construction as `sql/Q05` and `sql/Q06`.

```dax
Anchor MRR =
VAR Anchor = CALCULATE ( [MRR], DATEADD ( dim_date[MonthEndDate], -12, MONTH ) )
RETURN Anchor

NRR % =
VAR AnchorMonth = MAX ( dim_date[MonthIndex] ) - 12
VAR AnchorSet =
    CALCULATETABLE (
        VALUES ( fact_subscription_month[CustomerKey] ),
        FILTER ( ALL ( dim_date ), dim_date[MonthIndex] = AnchorMonth ),
        fact_subscription_month[IsActive] = 1
    )
VAR Base =
    CALCULATE ( [MRR],
        FILTER ( ALL ( dim_date ), dim_date[MonthIndex] = AnchorMonth ),
        AnchorSet )
VAR Now = CALCULATE ( [MRR], AnchorSet )
RETURN DIVIDE ( Now, Base )

GRR % =
VAR AnchorMonth = MAX ( dim_date[MonthIndex] ) - 12
VAR AnchorSet =
    CALCULATETABLE (
        VALUES ( fact_subscription_month[CustomerKey] ),
        FILTER ( ALL ( dim_date ), dim_date[MonthIndex] = AnchorMonth ),
        fact_subscription_month[IsActive] = 1
    )
VAR Base =
    CALCULATE ( [MRR],
        FILTER ( ALL ( dim_date ), dim_date[MonthIndex] = AnchorMonth ),
        AnchorSet )
VAR Capped =
    SUMX (
        AnchorSet,
        VAR Then =
            CALCULATE ( [MRR],
                FILTER ( ALL ( dim_date ), dim_date[MonthIndex] = AnchorMonth ) )
        VAR Now = [MRR]
        RETURN MIN ( Then, Now )
    )
RETURN DIVIDE ( Capped, Base )

Monthly Logo Churn % =
DIVIDE (
    CALCULATE ( COUNTROWS ( fact_subscription_month ),
                fact_subscription_month[MovementType] = "churn" ),
    CALCULATE ( COUNTROWS ( fact_subscription_month ),
                fact_subscription_month[IsActive] = 1 )
)
```

`Monthly Logo Churn %` is a hazard on customer-months at risk, not
`churned ÷ acquired`. The naive version understates churn for any channel that
grew — which is exactly the channel you are trying to judge.

### 3.4 Cohort retention

```dax
Cohort Revenue Retention % =
VAR M0 =
    CALCULATE ( [MRR],
        FILTER ( ALL ( bridge_cohort_age ),
                 bridge_cohort_age[MonthsSinceCohort] = 0 ) )
RETURN DIVIDE ( [MRR], M0 )

Cohort Logo Retention % =
VAR M0 =
    CALCULATE ( [Active Customers],
        FILTER ( ALL ( bridge_cohort_age ),
                 bridge_cohort_age[MonthsSinceCohort] = 0 ) )
RETURN DIVIDE ( [Active Customers], M0 )
```

Matrix: rows `dim_customer[CohortMonth]`, columns
`bridge_cohort_age[MonthsSinceCohort]`, values `Cohort Revenue Retention %`.
Conditional-format the values on a diverging scale centred at 100%.

### 3.5 Unit economics

The cost allocation mirrors `sql/Q07` exactly: channel programme spend, plus
100% of SDR payroll to Outbound, plus a shared AE/SE/marketing pool allocated
on wins × `SalesTouchWeight`, plus commission on new ACV.

```dax
Programme Spend = SUM ( fact_marketing_spend[ProgramSpend] )

New Logos =
CALCULATE (
    DISTINCTCOUNT ( fact_subscription_month[CustomerKey] ),
    fact_subscription_month[MovementType] = "new"
)

New ACV =
CALCULATE ( SUM ( fact_subscription_month[MovementArr] ),
            fact_subscription_month[MovementType] = "new" )

SDR Payroll =
CALCULATE ( SUM ( fact_headcount[TotalCost] ),
            fact_headcount[Role] IN { "sdr", "sdr_manager" } )

Shared Sales Pool =
CALCULATE ( SUM ( fact_headcount[TotalCost] ),
            fact_headcount[Role] IN { "ae", "sales_engineer", "marketing" } )
  + CALCULATE ( SUM ( fact_opex[Amount] ),
                fact_opex[GlAccount] = "Marketing operations" )

Allocation Basis =
SUMX ( VALUES ( dim_channel[Channel] ),
       [New Logos] * MAX ( dim_channel[SalesTouchWeight] ) )

Allocated Sales Cost =
VAR TotalBasis = CALCULATE ( [Allocation Basis], ALL ( dim_channel ) )
RETURN DIVIDE ( [Allocation Basis], TotalBasis ) * CALCULATE ( [Shared Sales Pool], ALL ( dim_channel ) )

Total CAC Spend =
[Programme Spend]
  + IF ( SELECTEDVALUE ( dim_channel[Channel] ) = "outbound_sdr",
         CALCULATE ( [SDR Payroll], ALL ( dim_channel ) ), 0 )
  + [Allocated Sales Cost]
  + [New ACV] * 0.10

CAC = DIVIDE ( [Total CAC Spend], [New Logos] )

Blended Gross Margin =
DIVIDE (
    SUMX ( VALUES ( dim_segment[Segment] ),
           [New ACV] * MAX ( dim_segment[GrossMargin] ) ),
    [New ACV]
)

Monthly Gross Profit per Customer =
DIVIDE ( [New ACV], [New Logos] ) / 12 * [Blended Gross Margin]

CAC Payback Months = DIVIDE ( [CAC], [Monthly Gross Profit per Customer] )

Monthly NRR =
DIVIDE (
    CALCULATE ( SUM ( fact_subscription_month[Mrr] ),
                fact_subscription_month[PriorMrr] > 0 ),
    CALCULATE ( SUM ( fact_subscription_month[PriorMrr] ),
                fact_subscription_month[PriorMrr] > 0 )
)

LTV 60m =
VAR r = [Monthly NRR]
VAR Horizon = IF ( ABS ( 1 - r ) < 0.0000001, 60, DIVIDE ( 1 - r ^ 60, 1 - r ) )
RETURN [Monthly Gross Profit per Customer] * Horizon

LTV to CAC = DIVIDE ( [LTV 60m], [CAC] )
```

**Always apply this filter to any CAC visual:**

```dax
CAC (attributable, in-window only) =
CALCULATE (
    [CAC],
    dim_customer[IsPreWindow] = 0,
    dim_customer[ChannelUnattributed] = 0
)
```

452 of 826 customers were acquired before the S&M ledger begins and carry no
cost. Leaving them in the denominator roughly halves CAC and is the single most
common way this metric gets flattered.

### 3.6 P&L and runway

```dax
COGS = CALCULATE ( SUM ( fact_opex[Amount] ), fact_opex[CostCategory] = "COGS" )
S&M  = CALCULATE ( SUM ( fact_opex[Amount] ), fact_opex[CostCategory] = "SM" )
R&D  = CALCULATE ( SUM ( fact_opex[Amount] ), fact_opex[CostCategory] = "RD" )
G&A  = CALCULATE ( SUM ( fact_opex[Amount] ), fact_opex[CostCategory] = "GA" )

Gross Profit = [Recognised Revenue] - [COGS]
Gross Margin % = DIVIDE ( [Gross Profit], [Recognised Revenue] )
Total Cost = [COGS] + [S&M] + [R&D] + [G&A]
Net Burn = [Total Cost] - [Recognised Revenue]

Net Burn 3M Avg =
AVERAGEX ( DATESINPERIOD ( dim_date[MonthEndDate], MAX ( dim_date[MonthEndDate] ),
                           -3, MONTH ),
           [Net Burn] )

Cash On Hand = 6100000          -- as at 2026-08-31

Runway Months = DIVIDE ( [Cash On Hand], [Net Burn 3M Avg] )

S&M % of Revenue = DIVIDE ( [S&M], [Recognised Revenue] )

Magic Number =
VAR NetNewARR = [ARR] - CALCULATE ( [ARR], PREVIOUSMONTH ( dim_date[MonthEndDate] ) )
VAR PriorSM = CALCULATE ( [S&M], PREVIOUSMONTH ( dim_date[MonthEndDate] ) )
RETURN DIVIDE ( NetNewARR, PriorSM )
```

### 3.7 SDR productivity

```dax
SDR Heads = CALCULATE ( SUM ( fact_headcount[Headcount] ), fact_headcount[Role] = "sdr" )

Outbound Meetings =
CALCULATE ( SUM ( fact_marketing_spend[Leads] ), dim_channel[Channel] = "outbound_sdr" )

Meetings per Rep = DIVIDE ( [Outbound Meetings], [SDR Heads] )

Marginal Meetings per Added Rep =
VAR PriorHeads = CALCULATE ( [SDR Heads], PREVIOUSMONTH ( dim_date[MonthEndDate] ) )
VAR PriorMeet = CALCULATE ( [Outbound Meetings], PREVIOUSMONTH ( dim_date[MonthEndDate] ) )
VAR Added = [SDR Heads] - PriorHeads
RETURN IF ( Added > 0, DIVIDE ( [Outbound Meetings] - PriorMeet, Added ) )
```

---

## 4. Report pages

**Page 1 — The Answer.** Cards: `ARR`, `ARR YoY %`, `Net Burn 3M Avg`,
`Runway Months`, `NRR %`. A 100% stacked bar of ARR by `dim_customer[Segment]`.
A bar of `CAC Payback Months` by `dim_channel[ChannelLabel]` with a constant
line at 18 — everything to the right of the line is being funded by cash the
company does not have.

**Page 2 — ARR bridge.** Waterfall: category `MovementType`, value the movement
measures, breakdown by `dim_date[MonthLabel]`. Add the `Bridge Residual` card.

**Page 3 — Cohorts.** The retention matrix from 3.4. Slicers on `Channel` and
`Segment`. Add a `ref_events` table visual beside it — the Paid Social
lead-quality collapse from 2024-11 is visible as a step change down the
matrix once you slice to that channel.

**Page 4 — Channel economics.** Table: `ChannelLabel`, `New Logos`, `CAC`,
`CAC Payback Months`, `Monthly NRR`, `LTV to CAC`. Cross-filter by
`dim_customer[Segment]` — this is the cut that shows Outbound paying back in
12 months on Enterprise and 40 months on Mid-Market.

**Page 5 — Data quality.** The `etl_audit` table with `RowsIn`, `RowsOut`,
`RowsAffected` and `Severity`. A report nobody can audit is a report nobody
should act on.

---

## 5. Two traps this model is built to avoid

1. **`MovementType = "opening_balance"`** is the pre-existing book landing in
   the first month of the window. It is not new business. Filter it out of any
   new-logo or CAC visual, and keep it in the bridge or the bridge will not
   foot.
2. **Credit memos are not in `Mrr`.** Use `Recognised Revenue` for anything
   P&L or cash related and `MRR`/`ARR` for anything retention related. They are
   different numbers on purpose; a report that mixes them will not tie to
   either the bridge or the cash model.
