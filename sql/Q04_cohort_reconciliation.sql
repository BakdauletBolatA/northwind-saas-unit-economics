-- =============================================================================
-- Q04  Control: the cohort triangle must foot to total company MRR, every month.
-- If this does not tie, the cohort table in Q03 is decorative and every
-- retention number derived from it is wrong.
-- =============================================================================
WITH cohorted AS (
  SELECT month, month_index,
         CASE WHEN is_pre_window = 1 THEN 'PRE-WINDOW' ELSE signup_month END AS cohort,
         SUM(mrr) AS mrr
  FROM v_customer_month
  GROUP BY month, month_index, cohort
),
by_month AS (
  SELECT month, month_index, COUNT(*) AS cohorts_present, SUM(mrr) AS cohort_sum_mrr
  FROM cohorted GROUP BY month, month_index
),
totals AS (
  SELECT month, month_index, SUM(mrr) AS total_mrr
  FROM v_customer_month GROUP BY month, month_index
)
SELECT t.month,
       b.cohorts_present,
       ROUND(b.cohort_sum_mrr, 2)                    AS cohort_sum_mrr,
       ROUND(t.total_mrr, 2)                         AS warehouse_total_mrr,
       ROUND(b.cohort_sum_mrr - t.total_mrr, 2)      AS variance_must_be_zero,
       CASE WHEN ABS(b.cohort_sum_mrr - t.total_mrr) < 0.005 THEN 'PASS' ELSE 'FAIL' END
                                                     AS status
FROM totals t JOIN by_month b ON b.month_index = t.month_index
ORDER BY t.month_index;
