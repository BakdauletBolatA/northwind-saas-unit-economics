-- =============================================================================
-- Q10  "Did any channel's lead quality change, and when?"
-- Six-month revenue retention by channel x acquisition half-year, restricted to
-- cohorts that have actually had six months to be observed. Without that
-- restriction every recent cohort looks catastrophic purely from censoring,
-- which is the classic way this analysis is got wrong.
-- =============================================================================
WITH obs_end AS (SELECT MAX(month_index) AS last_mi FROM v_customer_month),
starts AS (
  SELECT n.customer_key, n.channel, n.signup_month, n.new_mrr AS mrr_m0,
         dd.month_index AS start_mi,
         n.signup_month || '' AS cohort_month
  FROM v_new_business n
  JOIN dim_date dd ON dd.month = n.signup_month
),
eligible AS (
  SELECT s.* FROM starts s, obs_end o WHERE s.start_mi + 6 <= o.last_mi
),
m6 AS (
  SELECT e.customer_key, e.channel, e.cohort_month, e.mrr_m0,
         COALESCE(v.mrr, 0) AS mrr_m6
  FROM eligible e
  LEFT JOIN v_customer_month v
         ON v.customer_key = e.customer_key AND v.month_index = e.start_mi + 6
),
labelled AS (
  SELECT *, substr(cohort_month,1,4) ||
            CASE WHEN CAST(substr(cohort_month,6,2) AS INT) <= 6 THEN ' H1' ELSE ' H2' END
            AS half
  FROM m6
)
SELECT channel, half,
       COUNT(*)                                             AS cohort_logos,
       ROUND(SUM(mrr_m0) * 12, 0)                           AS cohort_new_arr,
       ROUND(AVG(mrr_m0) * 12, 0)                           AS avg_acv,
       ROUND(100.0 * SUM(mrr_m6) / NULLIF(SUM(mrr_m0), 0), 1) AS revenue_retention_m6_pct,
       ROUND(100.0 * SUM(CASE WHEN mrr_m6 > 0 THEN 1 ELSE 0 END) / COUNT(*), 1)
                                                            AS logo_retention_m6_pct
FROM labelled
GROUP BY channel, half
HAVING COUNT(*) >= 3          -- below this the ratio is an anecdote, not a rate
ORDER BY channel, half;
