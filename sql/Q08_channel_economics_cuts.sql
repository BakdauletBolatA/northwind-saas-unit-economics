-- =============================================================================
-- Q08  "Which slice of which channel works?"
-- The same cost allocation as Q07, sliced two ways and stacked in one result:
--
--   cut_type = 'by_period'   channel over three consecutive 12-month periods.
--                            A channel whose CAC climbs while its ACV falls is
--                            being scaled past the point where it worked.
--   cut_type = 'by_segment'  channel x segment over the trailing 12 months.
--                            This is the decision-critical cut: a blended
--                            channel payback hides the fact that the same
--                            motion can pay back in a year on one segment and
--                            never on another.
--
-- Within a channel, cost per won deal is held constant across segments
-- (cac_equal_split); cac_effort_weighted re-charges it on the stated
-- dim_segment.sales_effort_index instead. Both are shown because the choice
-- moves Enterprise payback by several months.
-- =============================================================================
WITH periods(period, m_from, m_to) AS (
  VALUES ('P1 2023-09..2024-08','2023-09','2024-08'),
         ('P2 2024-09..2025-08','2024-09','2025-08'),
         ('P3 2025-09..2026-08','2025-09','2026-08')
),

-- ---------------- cost pool per channel per period --------------------------
wins AS (
  SELECT pe.period, n.channel, COUNT(*) AS n_wins, SUM(n.new_acv) AS new_acv
  FROM v_new_business n
  JOIN periods pe ON n.signup_month BETWEEN pe.m_from AND pe.m_to
  WHERE n.channel_unattributed = 0
  GROUP BY pe.period, n.channel
),
touch AS (
  SELECT w.*, w.n_wins * dc.sales_touch_weight AS alloc_basis
  FROM wins w JOIN dim_channel dc ON dc.channel_code = w.channel
),
prog AS (
  SELECT pe.period, dc.channel_code AS channel, SUM(f.program_spend) AS program_spend
  FROM fact_marketing_spend f
  JOIN dim_channel dc ON dc.channel_key = f.channel_key
  JOIN dim_date    d  ON d.month_key    = f.month_key
  JOIN periods     pe ON d.month BETWEEN pe.m_from AND pe.m_to
  GROUP BY pe.period, dc.channel_code
),
sdr_direct AS (
  SELECT pe.period, SUM(h.total_cost) AS amt
  FROM fact_headcount h
  JOIN dim_date d  ON d.month_key = h.month_key
  JOIN periods  pe ON d.month BETWEEN pe.m_from AND pe.m_to
  WHERE h.role IN ('sdr','sdr_manager') GROUP BY pe.period
),
shared AS (
  SELECT pe.period,
         SUM(CASE WHEN h.role IN ('ae','sales_engineer','marketing')
                  THEN h.total_cost ELSE 0 END) AS pool
  FROM fact_headcount h
  JOIN dim_date d  ON d.month_key = h.month_key
  JOIN periods  pe ON d.month BETWEEN pe.m_from AND pe.m_to
  GROUP BY pe.period
),
ch_cost AS (
  SELECT t.period, t.channel, t.n_wins, t.new_acv,
         COALESCE(pr.program_spend, 0)
       + CASE WHEN t.channel = 'outbound_sdr' THEN sd.amt ELSE 0 END
       + sh.pool * t.alloc_basis
         / (SELECT SUM(alloc_basis) FROM touch x WHERE x.period = t.period)
       + t.new_acv * 0.10 AS total_cost
  FROM touch t
  LEFT JOIN prog       pr ON pr.period = t.period AND pr.channel = t.channel
  LEFT JOIN sdr_direct sd ON sd.period = t.period
  LEFT JOIN shared     sh ON sh.period = t.period
),

-- ---------------- cut 1: channel over time ----------------------------------
by_period AS (
  SELECT 'by_period' AS cut_type, c.period AS cut_key, c.channel, '' AS segment,
         c.n_wins,
         c.new_acv / c.n_wins                        AS avg_acv,
         c.total_cost / c.n_wins                     AS cac_equal_split,
         c.total_cost / c.n_wins                     AS cac_effort_weighted,
         (SELECT SUM(n.new_acv * s.gross_margin) / NULLIF(SUM(n.new_acv), 0)
          FROM v_new_business n
          JOIN dim_segment s ON s.segment_code = n.segment
          JOIN periods pe2 ON pe2.period = c.period
          WHERE n.channel = c.channel AND n.channel_unattributed = 0
            AND n.signup_month BETWEEN pe2.m_from AND pe2.m_to) AS gm
  FROM ch_cost c
),

-- ---------------- cut 2: channel x segment, trailing 12 months --------------
seg_wins AS (
  SELECT cc.period, cc.channel, n.segment, COUNT(*) AS n_wins,
         SUM(n.new_acv) AS new_acv,
         COUNT(*) * ds.sales_effort_index AS eff_units,
         cc.total_cost AS channel_cost, cc.n_wins AS channel_wins,
         ds.gross_margin
  FROM v_new_business n
  JOIN periods pe ON n.signup_month BETWEEN pe.m_from AND pe.m_to
  JOIN ch_cost cc ON cc.period = pe.period AND cc.channel = n.channel
  JOIN dim_segment ds ON ds.segment_code = n.segment
  WHERE pe.period = 'P3 2025-09..2026-08' AND n.channel_unattributed = 0
  GROUP BY cc.period, cc.channel, n.segment, ds.sales_effort_index, ds.gross_margin,
           cc.total_cost, cc.n_wins
),
by_segment AS (
  SELECT 'by_segment' AS cut_type, 'P3 2025-09..2026-08' AS cut_key,
         sw.channel, sw.segment, sw.n_wins,
         sw.new_acv / sw.n_wins                                  AS avg_acv,
         sw.channel_cost / sw.channel_wins                       AS cac_equal_split,
         sw.channel_cost * sw.eff_units
           / (SELECT SUM(x.eff_units) FROM seg_wins x WHERE x.channel = sw.channel)
           / sw.n_wins                                           AS cac_effort_weighted,
         sw.gross_margin                                         AS gm
  FROM seg_wins sw
),
stacked AS (SELECT * FROM by_period UNION ALL SELECT * FROM by_segment)
SELECT cut_type, cut_key, channel, segment, n_wins,
       CASE WHEN n_wins < 8 THEN 'THIN' ELSE '' END        AS caveat,
       ROUND(avg_acv, 0)                                   AS avg_acv,
       ROUND(cac_equal_split, 0)                           AS cac_equal_split,
       ROUND(cac_effort_weighted, 0)                       AS cac_effort_weighted,
       ROUND(avg_acv / 12.0 * gm, 0)                       AS monthly_gross_profit,
       ROUND(cac_equal_split / NULLIF(avg_acv / 12.0 * gm, 0), 1)
                                                           AS payback_months,
       ROUND(cac_effort_weighted / NULLIF(avg_acv / 12.0 * gm, 0), 1)
                                                           AS payback_months_effort_wtd
FROM stacked
ORDER BY cut_type, channel, cut_key, segment;
