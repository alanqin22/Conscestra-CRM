-- ============================================================================
-- ROOT NOISE FIX — gate event_queue enqueue on the event_types catalog
-- (2026-07-02)
--
-- Before: trg_fn_events_after_insert §1A queued EVERY event unconditionally —
-- CRUD echoes (activity.completed, product.updated, …) piled up as pending
-- rows nothing would ever consume (~92% of the backlog).
--
-- After: only event types flagged queue_enabled=true enter the WORK QUEUE.
--   • events table still records EVERYTHING (audit / timelines unaffected)
--   • notification fan-out (§1B) is unchanged (already subscription-gated)
--   • KEEP THE LIST IN SYNC with app/core/agent_bus.py:
--       HANDLERS (bespoke)  +  _REACTIONS (orchestrator catch-all react tier)
--
-- Apply locally via psql; apply to Railway manually (never deploy_sp.ps1).
-- ============================================================================

-- 1. Catalog flag (default FALSE = observe-only types never queue)
ALTER TABLE event_types ADD COLUMN IF NOT EXISTS queue_enabled boolean NOT NULL DEFAULT false;

-- 2. Consumable types — bespoke handlers
UPDATE event_types SET queue_enabled = true WHERE event_type IN (
    'invoice.overdue', 'lead.scored', 'activity.overdue_flagged',
    'lead.created', 'invoice_paid', 'order.status_changed');

-- 3. Consumable types — orchestrator catch-all REACT tier (blackboard signals)
UPDATE event_types SET queue_enabled = true WHERE event_type IN (
    'opportunity.closed_won', 'opportunity.closed_lost', 'opportunity.stage_changed',
    'product.stock_changed', 'contact.email_verified', 'lead.converted',
    'account.created', 'invoice_issued');

-- 3b. Catalog rows that are consumed but were never registered (their events
-- come from triggers/fns that INSERT INTO events directly, bypassing
-- emit_event validation) — without these the gate would silently drop them.
INSERT INTO event_types (event_type, description, entity_type, is_active, queue_enabled) VALUES
    ('activity.overdue_flagged',  'Activity flagged overdue (bus handler)',   'activity',    true, true),
    ('opportunity.closed_won',    'Deal closed won (catch-all react)',        'opportunity', true, true),
    ('opportunity.closed_lost',   'Deal closed lost (catch-all react)',       'opportunity', true, true),
    ('lead.converted',            'Lead converted (catch-all react)',         'lead',        true, true),
    ('invoice_issued',            'Invoice issued (catch-all react)',         'invoice',     true, true)
ON CONFLICT (event_type) DO UPDATE SET queue_enabled = EXCLUDED.queue_enabled;

-- 4. Gate §1A of the fan-out trigger
CREATE OR REPLACE FUNCTION trg_fn_events_after_insert() RETURNS trigger AS $$
DECLARE
    v_sub      RECORD;
    v_pass     boolean;
    v_payload  jsonb;
BEGIN
    -- 1A. AUTO-QUEUE — only catalog types flagged queue_enabled reach the
    -- work queue; everything else stays audit-only in `events`.
    INSERT INTO event_queue (event_uuid, status)
    SELECT NEW.event_uuid, 'pending'
    WHERE EXISTS (SELECT 1 FROM event_types t
                  WHERE t.event_type = NEW.event_type AND t.queue_enabled)
    ON CONFLICT (event_uuid) DO NOTHING;

    -- 1B. FAN-OUT NOTIFICATIONS via event_subscriptions (unchanged)
    v_payload := NEW.payload;
    FOR v_sub IN
        SELECT * FROM event_subscriptions
        WHERE event_type = NEW.event_type
          AND (entity_type IS NULL OR entity_type = NEW.entity_type)
          AND (entity_uuid IS NULL OR entity_uuid = NEW.entity_uuid)
          AND is_enabled = true
    LOOP
        v_pass := true;

        IF v_sub.conditions ? 'min_amount' THEN
            BEGIN
                IF (v_payload->'after'->>'amount')::numeric
                   < (v_sub.conditions->>'min_amount')::numeric THEN
                    v_pass := false;
                END IF;
            EXCEPTION WHEN OTHERS THEN
                v_pass := false;
            END;
        END IF;

        IF v_sub.conditions ? 'stage_in' THEN
            IF NOT (
                (v_payload->'after'->>'stage') = ANY (
                    SELECT jsonb_array_elements_text(v_sub.conditions->'stage_in')
                )
            ) THEN
                v_pass := false;
            END IF;
        END IF;

        IF v_sub.conditions ? 'json_path' THEN
            BEGIN
                IF (jsonb_path_query_first(v_payload, (v_sub.conditions->>'json_path')::jsonpath) #>> '{}')
                   IS DISTINCT FROM (v_sub.conditions->>'equals') THEN
                    v_pass := false;
                END IF;
            EXCEPTION WHEN OTHERS THEN
                v_pass := false;
            END;
        END IF;

        IF NOT v_pass THEN
            CONTINUE;
        END IF;

        INSERT INTO notifications (
            employee_uuid, event_uuid, channel, status, title, body, metadata
        )
        VALUES (
            v_sub.employee_uuid, NEW.event_uuid, v_sub.channel, 'pending',
            format('%s → %s', NEW.entity_type, NEW.event_type),
            format('Event %s occurred on %s %s',
                   NEW.event_type, NEW.entity_type, NEW.entity_uuid),
            jsonb_build_object(
                'entity_type',       NEW.entity_type,
                'entity_uuid',       NEW.entity_uuid,
                'payload',           v_payload,
                'event_type',        NEW.event_type,
                'subscription_uuid', v_sub.subscription_uuid
            )
        );
    END LOOP;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 5. Remove emit_event's redundant enqueue (§4 documented itself as redundant
-- — the gated trigger above is now the ONLY enqueue path).
CREATE OR REPLACE FUNCTION public.emit_event(
    p_event_type text, p_entity_type text, p_entity_uuid uuid,
    p_payload jsonb DEFAULT '{}'::jsonb,
    p_created_by_employee_uuid uuid DEFAULT NULL::uuid,
    p_source_system text DEFAULT 'crm'::text,
    p_correlation_id uuid DEFAULT NULL::uuid,
    p_root_event_uuid uuid DEFAULT NULL::uuid,
    p_workflow_run_uuid uuid DEFAULT NULL::uuid)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
    v_event_uuid uuid;
    v_now timestamptz := now();
BEGIN
    -- 1. Validate event type exists and is active
    PERFORM 1 FROM event_types
    WHERE event_type = p_event_type AND is_active = true;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'emit_event(): Unknown or inactive event_type: %', p_event_type;
    END IF;

    -- 2. Enforce canonical envelope keys
    p_payload := jsonb_strip_nulls(
        jsonb_build_object(
            'before',  COALESCE(p_payload->'before',  '{}'::jsonb),
            'after',   COALESCE(p_payload->'after',   '{}'::jsonb),
            'diff',    COALESCE(p_payload->'diff',    '{}'::jsonb),
            'context', COALESCE(p_payload->'context', '{}'::jsonb),
            'meta', jsonb_build_object(
                'version', 1, 'emitted_at', v_now, 'source_system', p_source_system)
        )
    );

    -- 3. Insert into events — the AFTER-INSERT trigger performs the (gated)
    --    enqueue + notification fan-out. No direct enqueue here any more.
    INSERT INTO events (
        event_type, entity_type, entity_uuid, payload,
        created_by_employee_uuid, source_system, correlation_id
    )
    VALUES (
        p_event_type, p_entity_type, p_entity_uuid, p_payload,
        p_created_by_employee_uuid, p_source_system,
        COALESCE(p_correlation_id, gen_random_uuid())
    )
    RETURNING event_uuid INTO v_event_uuid;

    RETURN v_event_uuid;
END;
$$;

-- 6. Verify
SELECT event_type, queue_enabled FROM event_types WHERE queue_enabled ORDER BY 1;
