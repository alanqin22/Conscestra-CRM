-- ============================================================================
-- Purge undeliverable PENDING agent-bus queue entries (2026-07-02)
--
-- Finding: the ~12k pending backlog is synthetic seed noise created in June
-- 2026. Age is NOT the right criterion (all rows are <30 days old) — the
-- right one is: the consumer only has handlers for 6 event types, so pending
-- queue rows of any OTHER type can never be processed into anything.
--
-- This deletes pending QUEUE rows for unhandled event types. The underlying
-- `events` rows are KEPT (full audit history; queue entries can be
-- regenerated:  INSERT INTO event_queue (event_uuid) SELECT ... FROM events).
--
-- Keep this list in sync with HANDLERS in app/core/agent_bus.py.
-- Run locally via psql; apply to Railway manually (never via deploy_sp.ps1).
-- ============================================================================

-- Preview:
SELECT e.event_type, count(*) AS would_purge
FROM event_queue q JOIN events e ON e.event_uuid = q.event_uuid
WHERE q.status = 'pending'
  AND e.event_type NOT IN ('invoice.overdue', 'lead.scored',
                           'activity.overdue_flagged', 'lead.created',
                           'invoice_paid', 'order.status_changed')
GROUP BY 1 ORDER BY 2 DESC;

-- Purge:
DELETE FROM event_queue q
USING events e
WHERE e.event_uuid = q.event_uuid
  AND q.status = 'pending'
  AND e.event_type NOT IN ('invoice.overdue', 'lead.scored',
                           'activity.overdue_flagged', 'lead.created',
                           'invoice_paid', 'order.status_changed');

-- Remaining queue by status:
SELECT status, count(*) FROM event_queue GROUP BY 1 ORDER BY 1;
