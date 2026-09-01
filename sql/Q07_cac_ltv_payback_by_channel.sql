-- =============================================================================
-- Q07  THE BOARD QUESTION: "does the sales machine pay back, by channel?"
-- Trailing 12 months (2025-09 .. 2026-08).
--
-- COST ALLOCATION RULE (stated so it can be argued with):
--   direct        = channel programme spend from the marketing GL
--   sdr_direct    = 100% of SDR + SDR-manager payroll -> outbound_sdr only.
--                   That team exists to source outbound; charging it anywhere
--                   else would flatter outbound and penalise everything else.
--   shared        = AE + Sales Engineer + Marketing payroll + marketing ops,
--                   allocated on wins x sales_touch_weight. Touch weight is a
--                   deal-effort index from the CRM, not a guess per channel.
--   commission    = 10% of that channel's new ACV (direct). Expansion
--                   commission is a RETENTION cost and is excluded from CAC.
--
-- EXCLUSIONS, and why:
--   * pre-window customers  - acquired before the S&M ledger begins, so they
--     have no cost. Leaving them in inflates the denominator and cuts CAC by
--     roughly half. This is the single most common way SaaS CAC is flattered.
--   * customers whose channel is missing are EXCLUDED entirely, not spread
--     across channels and not given their own row. The spend that produced
--     them already sits inside a real channel's cost pool, so giving them a
--     row would double-count it and take allocation basis away from the
--     channels being judged. Excluding them makes every CAC below very
--     slightly conservative (4 of 184 logos in the window), which is the
--     right direction of error for a spending decision.
--
-- LTV IS SHOWN THREE WAYS ON PURPOSE. See the notes at the bottom.
-- =============================================================================
WITH p AS (SELECT '2025-09' AS m_from, '2026-08' AS m_to),

-- ---------- wins in the measurement window ----------------------------------
wins AS (
  SELECT n.channel, COUNT(*) AS n_wins, SUM(n.new_acv) AS new_acv,
         SUM(n.new_acv * n.gross_margin) / NULLIF(SUM(n.new_acv), 0) AS blended_gm
  FROM v_new_business n, p
  WHERE n.signup_month BETWEEN p.m_from AND p.m_to
    AND n.channel_unattributed = 0     -- see the exclusion note in the header
  GROUP BY n.channel
),
touch AS (
  SELECT w.channel, w.n_wins, w.new_acv, w.blended_gm,
         w.n_wins * COALESCE(dc.sales_touch_weight,
                             (SELECT AVG(sales_touch_weight) FROM dim_channel
                              WHERE is_attributable = 1)) AS alloc_basis
  FROM wins w LEFT JOIN dim_channel dc ON dc.channel_code = w.channel
),

-- ---------- cost pools ------------------------------------------------------
prog AS (
  SELECT dc.channel_code AS channel, SUM(f.program_spend) AS program_spend
  FROM fact_marketing_spend f
  JOIN dim_channel dc ON dc.channel_key = f.channel_key
  JOIN dim_date    d  ON d.month_key    = f.month_key, p
  WHERE d.month BETWEEN p.m_from AND p.m_to
  GROUP BY dc.channel_code
),
sdr_direct AS (
  SELECT SUM(h.total_cost) AS amt
  FROM fact_headcount h JOIN dim_date d ON d.month_key = h.month_key, p
  WHERE h.role IN ('sdr','sdr_manager') AND d.month BETWEEN p.m_from AND p.m_to
),
shared AS (
  SELECT (SELECT SUM(h.total_cost) FROM fact_headcount h
          JOIN dim_date d ON d.month_key = h.month_key, p
          WHERE h.role IN ('ae','sales_engineer','marketing')
            AND d.month BETWEEN p.m_from AND p.m_to)
       + (SELECT SUM(o.amount) FROM fact_opex o
          JOIN dim_date d ON d.month_key = o.month_key, p
          WHERE o.gl_account = 'Marketing operations'
            AND d.month BETWEEN p.m_from AND p.m_to) AS pool
),

-- ---------- retention behaviour of each channel's in-window customers -------
-- Hazard is estimated on customer-months at risk, which handles the fact that
-- recent cohorts are censored. A naive "churned / acquired" would understate
-- churn for every channel that grew, i.e. exactly the ones under review.
hazard AS (
  SELECT channel,
         SUM(CASE WHEN movement_type = 'churn' THEN 1 ELSE 0 END) AS churned,
         SUM(CASE WHEN mrr > 0 THEN 1 ELSE 0 END)                 AS cust_months,
         1.0 * SUM(CASE WHEN movement_type = 'churn' THEN 1 ELSE 0 END)
             / NULLIF(SUM(CASE WHEN mrr > 0 THEN 1 ELSE 0 END), 0) AS monthly_logo_churn,
         -- Both sides restricted to customers that were already live last
         -- month. Letting new logos into the numerator only would report a
         -- "monthly retention" above 110% and blow up every LTV below.
         SUM(CASE WHEN prev_mrr > 0 THEN mrr      ELSE 0 END)
           / NULLIF(SUM(CASE WHEN prev_mrr > 0 THEN prev_mrr ELSE 0 END), 0)
                                                                   AS monthly_net_rev_retention
  FROM v_customer_month
  WHERE is_pre_window = 0
  GROUP BY channel
),

calc AS (
  SELECT t.channel,
         t.n_wins,
         ROUND(t.new_acv, 0)                                  AS new_acv,
         ROUND(t.new_acv / t.n_wins, 0)                        AS avg_acv,
         ROUND(t.blended_gm, 3)                                AS gross_margin,
         COALESCE(pr.program_spend, 0)                         AS program_spend,
         CASE WHEN t.channel = 'outbound_sdr'
              THEN (SELECT amt FROM sdr_direct) ELSE 0 END     AS sdr_payroll,
         (SELECT pool FROM shared) * t.alloc_basis
              / (SELECT SUM(alloc_basis) FROM touch)           AS shared_alloc,
         t.new_acv * 0.10                                      AS commission,
         h.monthly_logo_churn, h.monthly_net_rev_retention, h.churned, h.cust_months
  FROM touch t
  LEFT JOIN prog   pr ON pr.channel = t.channel
  LEFT JOIN hazard h  ON h.channel  = t.channel
),
final AS (
  SELECT channel, n_wins, new_acv, avg_acv, gross_margin, churned, cust_months,
         ROUND(program_spend, 0)  AS program_spend,
         ROUND(sdr_payroll, 0)    AS sdr_payroll,
         ROUND(shared_alloc, 0)   AS shared_sales_alloc,
         ROUND(commission, 0)     AS commission,
         ROUND(program_spend + sdr_payroll + shared_alloc + commission, 0) AS total_cac_spend,
         ROUND((program_spend + sdr_payroll + shared_alloc + commission) / n_wins, 0) AS cac,
         ROUND(avg_acv / 12.0 * gross_margin, 0)               AS monthly_gross_profit,
         ROUND(monthly_logo_churn, 5)                          AS monthly_logo_churn,
         ROUND(monthly_net_rev_retention, 5)                   AS monthly_nrr
  FROM calc
)
SELECT
  channel, n_wins,
  CASE WHEN n_wins < 10 THEN 'THIN SAMPLE - directional only' ELSE '' END AS caveat,
  new_acv, avg_acv, gross_margin,
  program_spend, sdr_payroll, shared_sales_alloc, commission, total_cac_spend,
  cac,
  monthly_gross_profit,
  ROUND(100.0 * monthly_logo_churn, 2)                          AS monthly_logo_churn_pct,
  ROUND(100.0 * monthly_nrr, 2)                                 AS monthly_nrr_pct,
  -- CAC payback: months of customer gross profit to repay acquisition cost.
  ROUND(cac / NULLIF(monthly_gross_profit, 0), 1)               AS cac_payback_months,
  -- LTV method A: perpetuity on the logo-churn hazard. Simple, standard, and
  -- structurally optimistic - it ignores contraction, assumes a constant
  -- hazard forever, and has no horizon cap.
  ROUND(monthly_gross_profit / NULLIF(monthly_logo_churn, 0), 0)          AS ltv_a_perpetuity,
  ROUND(monthly_gross_profit / NULLIF(monthly_logo_churn, 0) / NULLIF(cac, 0), 2)
                                                                         AS ltv_cac_a,
  -- LTV method B: 60-month horizon on the OBSERVED monthly net revenue
  -- retention, so expansion and contraction are both in the number and the
  -- horizon is finite. This is the one the recommendation is built on.
  ROUND(monthly_gross_profit *
        CASE WHEN ABS(1 - monthly_nrr) < 1e-9 THEN 60.0
             ELSE (1 - power(monthly_nrr, 60)) / (1 - monthly_nrr) END, 0) AS ltv_b_60m_nrr,
  ROUND(monthly_gross_profit *
        CASE WHEN ABS(1 - monthly_nrr) < 1e-9 THEN 60.0
             ELSE (1 - power(monthly_nrr, 60)) / (1 - monthly_nrr) END
        / NULLIF(cac, 0), 2)                                               AS ltv_cac_b,
  -- LTV method C: method B discounted at 1%/month (~12.7%/yr). Cash a SaaS
  -- business with 18 months of runway will not see for four years is not
  -- worth its face value.
  ROUND(monthly_gross_profit *
        (1 - power(monthly_nrr / 1.01, 60)) / (1 - monthly_nrr / 1.01), 0) AS ltv_c_discounted,
  ROUND(monthly_gross_profit *
        (1 - power(monthly_nrr / 1.01, 60)) / (1 - monthly_nrr / 1.01)
        / NULLIF(cac, 0), 2)                                               AS ltv_cac_c
FROM final
ORDER BY cac_payback_months;
