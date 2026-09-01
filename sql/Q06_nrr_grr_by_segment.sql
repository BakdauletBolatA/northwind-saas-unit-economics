-- =============================================================================
-- Q06  "Whose retention is it, really?"
-- Same trailing-12-month construction as Q05, split by segment. This is where
-- a healthy-looking blended number usually falls apart.
-- =============================================================================
WITH act AS (
  SELECT customer_key, segment, month_index, mrr
  FROM v_customer_month WHERE mrr > 0
),
pairs AS (
  SELECT a.month_index AS anchor_mi, a.segment, a.customer_key,
         a.mrr AS mrr_then, COALESCE(e.mrr, 0) AS mrr_now
  FROM act a
  LEFT JOIN act e ON e.customer_key = a.customer_key
                 AND e.month_index  = a.month_index + 12
)
SELECT d.month                                            AS as_of_month,
       p.segment,
       COUNT(*)                                           AS anchor_logos,
       ROUND(SUM(p.mrr_then) * 12, 0)                     AS anchor_arr,
       ROUND(100.0 * SUM(p.mrr_now) / SUM(p.mrr_then), 1) AS nrr_pct,
       ROUND(100.0 * SUM(MIN(p.mrr_now, p.mrr_then)) / SUM(p.mrr_then), 1) AS grr_pct,
       ROUND(100.0 * SUM(MAX(p.mrr_now - p.mrr_then, 0)) / SUM(p.mrr_then), 1)
                                                          AS expansion_pct
FROM pairs p
JOIN dim_date d ON d.month_index = p.anchor_mi + 12
WHERE d.is_actual = 1
GROUP BY d.month, d.month_index, p.segment
ORDER BY d.month_index, p.segment;
