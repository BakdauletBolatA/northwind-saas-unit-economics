-- =============================================================================
-- Q09  "Which segment actually earns its keep?"
-- Unit economics per segment on the trailing 12 months.
--
-- COST ALLOCATION. An earlier version of this query split the S&M pool across
-- segments in proportion to new ACV. That is circular: CAC then scales with
-- ACV, ARPA scales with ACV, and every segment lands on an identical payback
-- by construction. It is replaced by a pass-through of the CHANNEL cost pools
-- from Q07: each channel's cost is spread over its own won deals evenly, then
-- summed into the segment those deals landed in. A segment bought through
-- expensive channels therefore carries an expensive CAC, which is the point.
-- Q07 and Q09 allocate exactly the same dollars.
-- =============================================================================
WITH p AS (SELECT '2025-09' AS m_from, '2026-08' AS m_to),
wins_ch AS (
  SELECT n.channel, COUNT(*) AS n_wins, SUM(n.new_acv) AS new_acv
  FROM v_new_business n, p
  WHERE n.signup_month BETWEEN p.m_from AND p.m_to AND n.channel_unattributed = 0
  GROUP BY n.channel
),
touch AS (
  SELECT w.*, w.n_wins * dc.sales_touch_weight AS alloc_basis
  FROM wins_ch w JOIN dim_channel dc ON dc.channel_code = w.channel
),
prog AS (
  SELECT dc.channel_code AS channel, SUM(f.program_spend) AS program_spend
  FROM fact_marketing_spend f
  JOIN dim_channel dc ON dc.channel_key = f.channel_key
  JOIN dim_date    d  ON d.month_key    = f.month_key, p
  WHERE d.month BETWEEN p.m_from AND p.m_to GROUP BY dc.channel_code
),
sdr_direct AS (
  SELECT SUM(h.total_cost) AS amt FROM fact_headcount h
  JOIN dim_date d ON d.month_key = h.month_key, p
  WHERE h.role IN ('sdr','sdr_manager') AND d.month BETWEEN p.m_from AND p.m_to
),
shared AS (
  SELECT (SELECT SUM(h.total_cost) FROM fact_headcount h
          JOIN dim_date d ON d.month_key = h.month_key, p
          WHERE h.role IN ('ae','sales_engineer','marketing')
            AND d.month BETWEEN p.m_from AND p.m_to)
       + (SELECT SUM(o.amount) FROM fact_opex o
          JOIN dim_date d ON d.month_key = o.month_key, p
          WHERE o.gl_account = 'Marketing operations'
            AND d.month BETWEEN p.m_from AND p.m_to) AS pool
),
ch_cost AS (
  SELECT t.channel, t.n_wins,
         COALESCE(pr.program_spend, 0)
       + CASE WHEN t.channel = 'outbound_sdr' THEN (SELECT amt FROM sdr_direct) ELSE 0 END
       + (SELECT pool FROM shared) * t.alloc_basis / (SELECT SUM(alloc_basis) FROM touch)
       + t.new_acv * 0.10 AS total_cost
  FROM touch t LEFT JOIN prog pr ON pr.channel = t.channel
),
seg_wins AS (
  SELECT n.channel, n.segment, COUNT(*) AS wins, SUM(n.new_acv) AS new_acv
  FROM v_new_business n, p
  WHERE n.signup_month BETWEEN p.m_from AND p.m_to AND n.channel_unattributed = 0
  GROUP BY n.channel, n.segment
),
-- Basis 1: every deal in a channel costs the same to win.
-- Basis 2: deals cost in proportion to dim_segment.sales_effort_index, so an
--          Enterprise deal absorbs 4x the sales cost of an SMB deal in the
--          same channel. If the ranking holds under basis 2 it is not an
--          artefact of the allocation.
eff AS (
  SELECT sw.channel, sw.segment, sw.wins, sw.new_acv,
         sw.wins * ds.sales_effort_index AS eff_units
  FROM seg_wins sw JOIN dim_segment ds ON ds.segment_code = sw.segment
),
ch_eff AS (SELECT channel, SUM(eff_units) AS tot FROM eff GROUP BY channel),
seg_cost AS (
  SELECT e.segment,
         SUM(cc.total_cost * e.wins / cc.n_wins)                  AS alloc_cost,
         SUM(cc.total_cost * e.eff_units / ce.tot)                AS alloc_cost_effort,
         SUM(e.wins)                                              AS wins,
         SUM(e.new_acv)                                           AS new_acv
  FROM eff e
  JOIN ch_cost cc ON cc.channel = e.channel
  JOIN ch_eff  ce ON ce.channel = e.channel
  GROUP BY e.segment
),
behaviour AS (
  SELECT segment,
         1.0 * SUM(CASE WHEN movement_type='churn' THEN 1 ELSE 0 END)
             / NULLIF(SUM(CASE WHEN mrr > 0 THEN 1 ELSE 0 END), 0)   AS monthly_logo_churn,
         SUM(CASE WHEN prev_mrr > 0 THEN mrr      ELSE 0 END)
           / NULLIF(SUM(CASE WHEN prev_mrr > 0 THEN prev_mrr ELSE 0 END), 0)
                                                                     AS monthly_nrr
  FROM v_customer_month GROUP BY segment
),
book AS (
  SELECT segment, SUM(mrr) AS mrr, COUNT(*) AS live_logos
  FROM v_customer_month
  WHERE month = (SELECT MAX(month) FROM v_customer_month) AND mrr > 0
  GROUP BY segment
),
gm AS (SELECT segment_code, gross_margin FROM dim_segment)
SELECT b.segment,
       b.live_logos,
       ROUND(b.mrr * 12, 0)                                          AS book_arr,
       ROUND(100.0 * b.mrr / (SELECT SUM(mrr) FROM book), 1)         AS pct_of_arr,
       sc.wins                                                       AS new_logos_12m,
       ROUND(sc.new_acv / sc.wins, 0)                                AS avg_new_acv,
       ROUND(b.mrr / b.live_logos, 0)                                AS arpa_monthly,
       g.gross_margin,
       ROUND(sc.alloc_cost, 0)                                       AS allocated_sm_cost,
       ROUND(sc.alloc_cost / sc.wins, 0)                             AS cac_equal_split,
       ROUND(sc.alloc_cost_effort / sc.wins, 0)                      AS cac_effort_weighted,
       -- Payback is measured on gross profit at ENTRY ACV. Using the current
       -- book ARPA instead would credit years of subsequent expansion to the
       -- acquisition decision and shorten Enterprise payback by roughly a
       -- month. Both are shown; the entry-ACV number is the one to act on.
       ROUND(sc.new_acv / sc.wins / 12.0 * g.gross_margin, 0)        AS entry_monthly_gp,
       ROUND(b.mrr / b.live_logos * g.gross_margin, 0)               AS book_monthly_gp,
       ROUND(100.0 * bh.monthly_logo_churn, 2)                       AS monthly_logo_churn_pct,
       ROUND(100.0 * bh.monthly_nrr, 2)                              AS monthly_nrr_pct,
       ROUND(sc.alloc_cost / sc.wins
             / NULLIF(sc.new_acv / sc.wins / 12.0 * g.gross_margin, 0), 1)
                                                                     AS payback_months_entry,
       ROUND(sc.alloc_cost_effort / sc.wins
             / NULLIF(sc.new_acv / sc.wins / 12.0 * g.gross_margin, 0), 1)
                                                                     AS payback_months_effort_wtd,
       ROUND(sc.alloc_cost / sc.wins
             / NULLIF(b.mrr / b.live_logos * g.gross_margin, 0), 1)  AS payback_months_book_arpa,
       ROUND(b.mrr / b.live_logos * g.gross_margin *
             CASE WHEN ABS(1-bh.monthly_nrr) < 1e-9 THEN 60.0
                  ELSE (1 - power(bh.monthly_nrr, 60)) / (1 - bh.monthly_nrr) END, 0)
                                                                     AS ltv_60m,
       ROUND(b.mrr / b.live_logos * g.gross_margin *
             CASE WHEN ABS(1-bh.monthly_nrr) < 1e-9 THEN 60.0
                  ELSE (1 - power(bh.monthly_nrr, 60)) / (1 - bh.monthly_nrr) END
             / NULLIF(sc.alloc_cost / sc.wins, 0), 2)                AS ltv_cac,
       ROUND(b.mrr / b.live_logos * g.gross_margin *
             CASE WHEN ABS(1-bh.monthly_nrr) < 1e-9 THEN 60.0
                  ELSE (1 - power(bh.monthly_nrr, 60)) / (1 - bh.monthly_nrr) END
             / NULLIF(sc.alloc_cost_effort / sc.wins, 0), 2)         AS ltv_cac_effort_wtd
FROM book b
JOIN seg_cost  sc ON sc.segment = b.segment
JOIN behaviour bh ON bh.segment = b.segment
JOIN gm        g  ON g.segment_code = b.segment
ORDER BY book_arr DESC;
