-- =============================================================================
-- Q05  "Is the installed base growing or leaking?"
-- Trailing-12-month NRR and GRR, measured on a fixed anchor cohort:
--   cohort  = every customer with MRR > 0 twelve months ago
--   NRR     = their MRR today / their MRR then   (expansion counted)
--   GRR     = SUM(MIN(then, today)) / their MRR then   (expansion capped at 0)
-- Customers signed inside the window are deliberately excluded from the
-- numerator: new logos are acquisition, not retention.
-- =============================================================================
WITH act AS (
  SELECT customer_key, month_index, mrr FROM v_customer_month WHERE mrr > 0
),
pairs AS (
  SELECT a.month_index AS anchor_mi, a.customer_key,
         a.mrr AS mrr_then, COALESCE(e.mrr, 0) AS mrr_now
  FROM act a
  LEFT JOIN act e ON e.customer_key = a.customer_key
                 AND e.month_index  = a.month_index + 12
)
SELECT d.month                                            AS as_of_month,
       COUNT(*)                                           AS anchor_logos,
       ROUND(SUM(p.mrr_then) * 12, 0)                     AS anchor_arr,
       ROUND(100.0 * SUM(p.mrr_now) / SUM(p.mrr_then), 1) AS nrr_pct,
       ROUND(100.0 * SUM(MIN(p.mrr_now, p.mrr_then)) / SUM(p.mrr_then), 1) AS grr_pct,
       ROUND(100.0 * SUM(MAX(p.mrr_now - p.mrr_then, 0)) / SUM(p.mrr_then), 1)
                                                          AS expansion_pct,
       ROUND(100.0 * SUM(CASE WHEN p.mrr_now = 0 THEN p.mrr_then ELSE 0 END)
             / SUM(p.mrr_then), 1)                        AS churn_pct,
       ROUND(100.0 * SUM(CASE WHEN p.mrr_now > 0 THEN MAX(p.mrr_then - p.mrr_now, 0) ELSE 0 END)
             / SUM(p.mrr_then), 1)                        AS contraction_pct,
       ROUND(100.0 * SUM(CASE WHEN p.mrr_now = 0 THEN 1 ELSE 0 END) / COUNT(*), 1)
                                                          AS logo_churn_pct
FROM pairs p
JOIN dim_date d ON d.month_index = p.anchor_mi + 12
WHERE d.is_actual = 1
GROUP BY d.month, d.month_index
ORDER BY d.month_index;
