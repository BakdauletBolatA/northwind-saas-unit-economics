-- =============================================================================
-- Q01  "Where do we actually stand?"
-- One row per month: ARR, growth, gross margin, burn and implied runway.
-- Runway uses the trailing-3-month average burn, not the single latest month —
-- a single month moves +/- $40k on commission timing alone.
-- =============================================================================
WITH p AS (
  SELECT (SELECT CAST(json_extract(value,'$') AS REAL)
          FROM ref_assumptions WHERE key='cash_on_hand') AS cash
),
m AS (
  SELECT month, month_index, arr, mrr, recognised_revenue, gross_profit,
         cogs, sm, rd, ga, total_cost, net_burn
  FROM v_pnl_month
)
SELECT
  m.month,
  ROUND(m.arr, 0)                                                  AS arr,
  ROUND(m.arr - LAG(m.arr, 1) OVER (ORDER BY m.month_index), 0)    AS net_new_arr,
  ROUND(100.0 * (m.arr / NULLIF(LAG(m.arr, 12) OVER (ORDER BY m.month_index), 0) - 1), 1)
                                                                   AS arr_yoy_pct,
  ROUND(m.recognised_revenue, 0)                                   AS recognised_revenue,
  ROUND(100.0 * m.gross_profit / NULLIF(m.recognised_revenue, 0), 1) AS gross_margin_pct,
  ROUND(m.sm, 0)                                                   AS sales_and_marketing,
  ROUND(100.0 * m.sm / NULLIF(m.recognised_revenue, 0), 1)         AS sm_pct_of_revenue,
  ROUND(m.net_burn, 0)                                             AS net_burn,
  ROUND(AVG(m.net_burn) OVER (ORDER BY m.month_index
                              ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 0)
                                                                   AS burn_3m_avg,
  ROUND((SELECT cash FROM p)
        / NULLIF(AVG(m.net_burn) OVER (ORDER BY m.month_index
                                       ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 0), 1)
                                                                   AS runway_months
FROM m
ORDER BY m.month_index;
