-- =============================================================================
-- Q12  "Who is actually generating the growth we are celebrating?"
-- Net new ARR over the last 12 months, split into what the sales team sold
-- (new logos) versus what the installed base did on its own (expansion).
-- Then the same split by segment.
--
-- This is the query that decides the recommendation: if most of the growth is
-- expansion inside an existing segment, then adding acquisition capacity is
-- not what produced the last twelve months and should not be priced as if it
-- were.
-- =============================================================================
WITH p AS (SELECT '2025-09' AS m_from, '2026-08' AS m_to),
mv AS (
  SELECT v.segment, v.movement_type, v.movement_amount, v.month
  FROM v_customer_month v, p
  WHERE v.month BETWEEN p.m_from AND p.m_to
),
overall AS (
  SELECT 'TOTAL' AS segment,
         SUM(CASE WHEN movement_type='new'          THEN movement_amount ELSE 0 END)*12 AS new_arr,
         SUM(CASE WHEN movement_type='expansion'    THEN movement_amount ELSE 0 END)*12 AS expansion_arr,
         SUM(CASE WHEN movement_type='reactivation' THEN movement_amount ELSE 0 END)*12 AS reactivation_arr,
         SUM(CASE WHEN movement_type='contraction'  THEN movement_amount ELSE 0 END)*12 AS contraction_arr,
         SUM(CASE WHEN movement_type='churn'        THEN movement_amount ELSE 0 END)*12 AS churn_arr
  FROM mv
  UNION ALL
  SELECT segment,
         SUM(CASE WHEN movement_type='new'          THEN movement_amount ELSE 0 END)*12,
         SUM(CASE WHEN movement_type='expansion'    THEN movement_amount ELSE 0 END)*12,
         SUM(CASE WHEN movement_type='reactivation' THEN movement_amount ELSE 0 END)*12,
         SUM(CASE WHEN movement_type='contraction'  THEN movement_amount ELSE 0 END)*12,
         SUM(CASE WHEN movement_type='churn'        THEN movement_amount ELSE 0 END)*12
  FROM mv GROUP BY segment
)
SELECT segment,
       ROUND(new_arr, 0)                                            AS new_logo_arr,
       ROUND(expansion_arr, 0)                                      AS expansion_arr,
       ROUND(reactivation_arr, 0)                                   AS reactivation_arr,
       ROUND(contraction_arr, 0)                                    AS contraction_arr,
       ROUND(churn_arr, 0)                                          AS churn_arr,
       ROUND(new_arr + expansion_arr + reactivation_arr + contraction_arr + churn_arr, 0)
                                                                    AS net_new_arr,
       ROUND(100.0 * new_arr
             / NULLIF(new_arr + expansion_arr + reactivation_arr
                      + contraction_arr + churn_arr, 0), 1)         AS pct_from_new_logos,
       ROUND(100.0 * expansion_arr
             / NULLIF(new_arr + expansion_arr + reactivation_arr
                      + contraction_arr + churn_arr, 0), 1)         AS pct_from_expansion
FROM overall
ORDER BY CASE WHEN segment='TOTAL' THEN 0 ELSE 1 END, net_new_arr DESC;
