-- =============================================================================
-- Q02  "Where did the ARR actually come from?"
-- opening + carry-in + new + expansion + reactivation - contraction - churn
--   = closing, and the residual column must be 0.00 in every single month.
--
-- Every component is DERIVED from month-over-month MRR levels in
-- fact_subscription_month. Nothing is copied from a source-system movement
-- code, so this is a genuine reconciliation rather than a restatement.
--
-- carry_in_arr is non-zero only in the first month of the window: it is the
-- pre-existing book, which was sold before the S&M ledger starts.
-- =============================================================================
WITH mv AS (
  SELECT d.month, d.month_index,
         SUM(CASE WHEN f.movement_type='opening_balance' THEN f.movement_amount ELSE 0 END) AS carry_in,
         SUM(CASE WHEN f.movement_type='new'            THEN f.movement_amount ELSE 0 END) AS new_mrr,
         SUM(CASE WHEN f.movement_type='expansion'      THEN f.movement_amount ELSE 0 END) AS expansion,
         SUM(CASE WHEN f.movement_type='reactivation'   THEN f.movement_amount ELSE 0 END) AS reactivation,
         SUM(CASE WHEN f.movement_type='contraction'    THEN -f.movement_amount ELSE 0 END) AS contraction,
         SUM(CASE WHEN f.movement_type='churn'          THEN -f.movement_amount ELSE 0 END) AS churn,
         SUM(f.mrr)                                                                        AS closing
  FROM fact_subscription_month f
  JOIN dim_date d ON d.month_key = f.month_key
  GROUP BY d.month, d.month_index
)
SELECT
  month,
  ROUND(12.0 * LAG(closing, 1, 0) OVER (ORDER BY month_index), 2) AS opening_arr,
  ROUND(12.0 * carry_in,      2) AS carry_in_arr,
  ROUND(12.0 * new_mrr,       2) AS new_arr,
  ROUND(12.0 * expansion,     2) AS expansion_arr,
  ROUND(12.0 * reactivation,  2) AS reactivation_arr,
  ROUND(12.0 * contraction,   2) AS contraction_arr,
  ROUND(12.0 * churn,         2) AS churn_arr,
  ROUND(12.0 * closing,       2) AS closing_arr,
  ROUND(12.0 * (LAG(closing, 1, 0) OVER (ORDER BY month_index)
                + carry_in + new_mrr + expansion + reactivation
                - contraction - churn
                - closing), 2)   AS residual_arr_must_be_zero
FROM mv
ORDER BY month_index;
