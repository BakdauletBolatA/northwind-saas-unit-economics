-- =============================================================================
-- Q13  "Can I trust the numbers above?"
-- The ETL audit trail plus what was quarantined and why. Every rule reports
-- rows in, rows out and rows affected; nothing is dropped silently.
-- =============================================================================
SELECT a.step_no, a.rule_id, a.rule_name, a.table_name,
       a.rows_in, a.rows_out, a.rows_affected, a.severity,
       COALESCE(q.quarantined, 0) AS rows_quarantined,
       a.rule_description
FROM etl_audit a
LEFT JOIN (SELECT rule_id, COUNT(*) AS quarantined
           FROM dq_quarantine GROUP BY rule_id) q ON q.rule_id = a.rule_id
ORDER BY a.step_no;
