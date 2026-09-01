-- =============================================================================
-- Q11  "What would SDR number 10 through 18 actually produce?"
-- The only evidence available is what happened when the team went from 4 to 9.
-- Meetings per rep are observed monthly; wins per rep are derived from the
-- outbound funnel actually realised.
--
-- The last column is the number the board decision turns on: the MARGINAL
-- meetings each additional rep delivered, not the average across the team.
-- =============================================================================
WITH sdr AS (
  SELECT d.month, d.month_index, h.headcount AS sdr_heads, h.total_cost AS sdr_cost
  FROM fact_headcount h JOIN dim_date d ON d.month_key = h.month_key
  WHERE h.role = 'sdr'
),
mgr AS (
  SELECT d.month, h.total_cost AS mgr_cost
  FROM fact_headcount h JOIN dim_date d ON d.month_key = h.month_key
  WHERE h.role = 'sdr_manager'
),
meet AS (
  SELECT d.month, f.leads AS meetings, f.sqls, f.program_spend
  FROM fact_marketing_spend f
  JOIN dim_channel dc ON dc.channel_key = f.channel_key
  JOIN dim_date    d  ON d.month_key    = f.month_key
  WHERE dc.channel_code = 'outbound_sdr'
),
w AS (
  SELECT signup_month AS month, COUNT(*) AS wins, SUM(new_acv) AS new_acv
  FROM v_new_business WHERE channel = 'outbound_sdr' GROUP BY signup_month
)
SELECT s.month,
       s.sdr_heads,
       ROUND(m.meetings, 1)                                        AS meetings,
       ROUND(m.meetings / s.sdr_heads, 2)                          AS meetings_per_rep,
       ROUND(m.sqls, 1)                                            AS qualified_opps,
       COALESCE(w.wins, 0)                                         AS wins,
       ROUND(COALESCE(w.new_acv, 0), 0)                            AS new_acv,
       ROUND(s.sdr_cost + COALESCE(g.mgr_cost, 0) + m.program_spend, 0)
                                                                   AS outbound_direct_cost,
       -- marginal meetings delivered by the headcount added this month
       s.sdr_heads - LAG(s.sdr_heads) OVER (ORDER BY s.month_index) AS heads_added,
       ROUND((m.meetings - LAG(m.meetings) OVER (ORDER BY s.month_index))
             / NULLIF(s.sdr_heads - LAG(s.sdr_heads) OVER (ORDER BY s.month_index), 0), 2)
                                                                   AS marginal_meetings_per_added_rep
FROM sdr s
JOIN meet m ON m.month = s.month
LEFT JOIN mgr g ON g.month = s.month
LEFT JOIN w     ON w.month = s.month
ORDER BY s.month_index;
