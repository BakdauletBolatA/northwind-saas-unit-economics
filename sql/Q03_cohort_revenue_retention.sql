-- =============================================================================
-- Q03  "Do the customers we buy stay bought?"
-- Revenue retention triangle by acquisition cohort. Value = MRR of the cohort
-- in month N as a percentage of that cohort's MRR in its first month.
-- Above 100% means expansion is out-running churn inside the cohort.
--
-- Customers acquired before the observation window are collapsed into a single
-- 'PRE-WINDOW' cohort so that the triangle still foots to total company MRR
-- (checked separately in Q04). Their true acquisition dates are not in the
-- warehouse, so mixing them into a calendar cohort would be a fabrication.
-- =============================================================================
WITH base AS (
  SELECT CASE WHEN is_pre_window = 1 THEN 'PRE-WINDOW' ELSE signup_month END AS cohort,
         customer_key, month_index, mrr, cohort_month
  FROM v_customer_month
),
anchor AS (
  SELECT cohort, MIN(month_index) AS cohort_mi
  FROM base GROUP BY cohort
),
sized AS (
  SELECT b.cohort, a.cohort_mi, b.month_index - a.cohort_mi AS months_since,
         b.customer_key, b.mrr
  FROM base b JOIN anchor a ON a.cohort = b.cohort
),
agg AS (
  SELECT cohort, cohort_mi, months_since,
         SUM(mrr)                                      AS mrr,
         COUNT(DISTINCT CASE WHEN mrr > 0 THEN customer_key END) AS live_logos
  FROM sized GROUP BY cohort, cohort_mi, months_since
)
SELECT a.cohort,
       a.months_since,
       a.live_logos,
       (SELECT live_logos FROM agg z WHERE z.cohort = a.cohort AND z.months_since = 0)
                                                       AS cohort_size,
       ROUND(a.mrr, 2)                                 AS cohort_mrr,
       ROUND((SELECT mrr FROM agg z WHERE z.cohort = a.cohort AND z.months_since = 0), 2)
                                                       AS cohort_mrr_m0,
       ROUND(100.0 * a.mrr /
             NULLIF((SELECT mrr FROM agg z WHERE z.cohort = a.cohort AND z.months_since = 0), 0), 1)
                                                       AS revenue_retention_pct,
       ROUND(100.0 * a.live_logos /
             NULLIF((SELECT live_logos FROM agg z WHERE z.cohort = a.cohort AND z.months_since = 0), 0), 1)
                                                       AS logo_retention_pct
FROM agg a
ORDER BY a.cohort_mi, a.cohort, a.months_since;
