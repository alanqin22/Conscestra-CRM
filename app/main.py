"""Unified CRM Agent application — all 12 modules + home index dashboard.

All agent routers are registered here.  Each agent exposes its own endpoint
prefix so existing HTML frontends require zero URL changes.

v2.4.0 — Added EmailAgent module (info@agentorc.ca — SMTP/IMAP + LangGraph).
  • Endpoint: POST /email-chat
  • Health:   GET  /email-health
  • Frontend: email-mgmt.html
  • SMTP: mail.agentorc.ca:465 (SSL)  IMAP: mail.agentorc.ca:993 (SSL)

v2.3.0 — Added Auth module (Conscestra CRM Authentication).
  • Direct DB routing — no AI agent, no LangGraph AI nodes.
  • Endpoints: POST /auth/signup, /auth/signin, /auth/signout,
               /auth/change-password, /auth/password-reset/request,
               /auth/password-reset/confirm, /auth/verify-email
  • Health:   GET  /auth-health
  • Frontend: auth.html

v2.2.0 — Added Store module (CRM Commerce View).
  • Direct SP routing — no AI agent, no LangGraph AI nodes.
  • Endpoint: POST /store-chat
  • Health:   GET  /store-health
  • Frontend: store-home.html
"""

import datetime as _dt
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.database import test_connection
from app.core.memory import active_sessions, clear_session

# -- Home dashboard (sp_home_index — direct SP, no LangGraph)
from app.agents.home.router import router as home_router

# -- All 10 AI agent routers
from app.agents.accounts.router      import router as accounts_router
from app.agents.contacts.router      import router as contacts_router
from app.agents.products.router      import router as products_router
from app.agents.orders.router        import router as orders_router
from app.agents.activities.router    import router as activities_router
from app.agents.cases.router         import router as cases_router
from app.agents.opportunities.router import router as opportunities_router
from app.agents.accounting.router    import router as accounting_router
from app.agents.leads.router         import router as leads_router
from app.agents.analytics.router     import router as analytics_router
from app.agents.notifications.router import router as notifications_router
from app.agents.orchestrator.router  import router as orchestrator_router

# -- Store module (CRM Commerce View — direct SP routing, no AI agent)
from app.agents.store.router import router as store_router

# -- Auth module (direct DB routing — no AI agent)
from app.agents.auth.router import router as auth_router

# -- Email agent (SMTP/IMAP + LangGraph)
from app.agents.email.router import router as email_router

# -- Voice (browser STT auth-token mint for Azure Cognitive Services)
from app.agents.voice.router import router as voice_router

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _run_advance_order_statuses() -> None:
    """Scheduled job: advance order statuses once daily.

    Calls fn_advance_order_statuses() which moves orders through the
    realistic 30-day lifecycle:
      pending(24h) → processing(48h) → ready(12h) → shipped → delivered(7d) → completed(32d)
    The ready→shipped transition fires trgfn_order_create_invoice, creating
    invoices and auto-payments for newly-shipped orders.
    """
    try:
        from app.core.database import execute_sp
        rows = execute_sp(
            "SELECT transition, orders_advanced FROM fn_advance_order_statuses()"
        )
        total = sum(r.get('orders_advanced', 0) for r in rows)
        if total:
            for r in rows:
                if r.get('orders_advanced', 0):
                    logger.info(f"  [OrderAdvance] {r['transition']}: {r['orders_advanced']}")
            logger.info(f"[OrderAdvance] Daily run complete — {total} orders advanced")
        else:
            logger.info("[OrderAdvance] Daily run — no orders needed advancement")
    except Exception as exc:
        logger.error(f"[OrderAdvance] Scheduled job failed: {exc}", exc_info=True)


def _run_generate_daily_orders() -> None:
    """Scheduled job: generate 20-30 new pending orders once daily."""
    try:
        from app.core.database import execute_sp
        rows = execute_sp("SELECT generate_daily_orders() AS result")
        result = rows[0].get('result', '') if rows else 'no result'
        logger.info(f"[DailyOrders] {result}")
    except Exception as exc:
        logger.error(f"[DailyOrders] Scheduled job failed: {exc}", exc_info=True)


def _run_advance_opportunity_stages() -> None:
    """Scheduled job: advance opportunity pipeline stages nightly.

    Moves opportunities through: prospecting → qualification → proposal →
    negotiation → closed_won / closed_lost, with realistic time delays.
    Keeps Qualification and Proposal stages populated so AI Agent searches work.
    """
    try:
        from app.core.database import execute_sp
        rows = execute_sp(
            "SELECT transition, opportunities_advanced FROM fn_advance_opportunity_stages()"
        )
        total = sum(r.get('opportunities_advanced', 0) for r in rows)
        if total:
            for r in rows:
                if r.get('opportunities_advanced', 0):
                    logger.info(f"  [OppAdvance] {r['transition']}: {r['opportunities_advanced']}")
            logger.info(f"[OppAdvance] Nightly run complete — {total} opportunities advanced")
        else:
            logger.info("[OppAdvance] Nightly run — no opportunities needed advancement")
    except Exception as exc:
        logger.error(f"[OppAdvance] Scheduled job failed: {exc}", exc_info=True)


def _run_generate_pipeline_opportunities() -> None:
    """Scheduled job: seed 3-5 new pipeline opportunities daily.

    Creates new Prospecting/Qualification opportunities so the pipeline
    stays populated. fn_advance_opportunity_stages() will age them forward.
    """
    try:
        from app.core.database import execute_sp
        rows = execute_sp("SELECT generate_pipeline_opportunities() AS result")
        result = rows[0].get('result', '') if rows else 'no result'
        logger.info(f"[PipelineGen] {result}")
    except Exception as exc:
        logger.error(f"[PipelineGen] Scheduled job failed: {exc}", exc_info=True)


# Auto-sweep runs live (snooze is non-destructive & reversible). Flip to True
# to have the scheduled run only preview (log what it *would* snooze) instead.
ACTIVITIES_SWEEP_DRY_RUN = False


def _run_activities_auto_sweep() -> None:
    """Scheduled job: auto-snooze non-critical, overdue activities.

    Calls sp_activities_auto_sweep() (v1 SNOOZE-ONLY) which pushes due_at
    forward for open, low-score (<=15) task/note activities that are overdue,
    capped at 3 auto-snoozes each so nothing is deferred forever. Non-
    destructive — it never completes/deletes/reassigns. Set
    ACTIVITIES_SWEEP_DRY_RUN=True to log a preview without changing anything.
    """
    try:
        from app.core.database import execute_sp
        dry = 'true' if ACTIVITIES_SWEEP_DRY_RUN else 'false'
        rows = execute_sp(
            f"SELECT sp_activities_auto_sweep(p_dry_run => {dry}) AS result"
        )
        result = (rows[0].get('result') or {}) if rows else {}
        meta = (result.get('metadata') or {}) if isinstance(result, dict) else {}
        logger.info(f"[ActivitySweep] {meta.get('message', result)}")
    except Exception as exc:
        logger.error(f"[ActivitySweep] Scheduled job failed: {exc}", exc_info=True)
    # Moot-task closure (bottleneck scan findings, 2026-07): auto-generated
    # playbook/courtesy tasks are born open and tied to a moment, but nothing
    # closed them when that moment passed — they piled up by the hundreds.
    # A task is moot when its underlying object settled (deal closed, invoice
    # paid/cancelled, order completed) or, for the courtesy subjects only,
    # when its window expired (30 days past due). Completion is reversible
    # via the activity 'reopen' mode; the weekly bottleneck report still
    # counts fresh piles, so closure keeps the queue actionable without
    # hiding the signal.
    try:
        from app.core.database import get_connection
        conn = get_connection()
        with conn.cursor() as cur:
            n = 0
            # deal tasks on closed/removed opportunities
            cur.execute(
                """UPDATE activities a
                   SET status='completed', completed_at=now(), updated_at=now()
                   WHERE a.status='open' AND a.related_type='opportunity'
                     AND NOT EXISTS (SELECT 1 FROM opportunities o
                                     WHERE o.opportunity_id = a.related_id
                                       AND o.status = 'open')
                   RETURNING 1""")
            n += len(cur.fetchall())
            # invoice tasks (confirm receipt / reminders) on settled invoices
            cur.execute(
                """UPDATE activities a
                   SET status='completed', completed_at=now(), updated_at=now()
                   WHERE a.status='open' AND a.related_type='invoice'
                     AND NOT EXISTS (SELECT 1 FROM invoices i
                                     WHERE i.invoice_id = a.related_id
                                       AND (i.is_deleted IS NULL OR i.is_deleted=false)
                                       AND i.status NOT IN ('paid','cancelled')
                                       AND COALESCE(i.balance_due,0) > 0)
                   RETURNING 1""")
            n += len(cur.fetchall())
            # order courtesy tasks on terminal orders, or 30+ days stale
            cur.execute(
                """UPDATE activities a
                   SET status='completed', completed_at=now(), updated_at=now()
                   WHERE a.status='open' AND a.related_type='order'
                     AND (a.subject ILIKE 'Order shipped%'
                          OR a.subject ILIKE 'Order fulfilled%')
                     AND (a.due_at < now() - interval '30 days'
                          OR NOT EXISTS (SELECT 1 FROM orders o
                                         WHERE o.order_id = a.related_id
                                           AND o.deleted_at IS NULL
                                           AND o.status NOT IN ('completed','cancelled')))
                   RETURNING 1""")
            n += len(cur.fetchall())
            # welcome / first-contact moments that expired unworked
            cur.execute(
                """UPDATE activities a
                   SET status='completed', completed_at=now(), updated_at=now()
                   WHERE a.status='open'
                     AND a.subject IN ('Welcome / Account created',
                                       'First contact / Intro')
                     AND a.due_at < now() - interval '30 days'
                   RETURNING 1""")
            n += len(cur.fetchall())
        conn.commit()
        conn.close()
        if n:
            logger.info(f"[ActivitySweep] completed {n} moot task(s) "
                        "(settled object or expired courtesy window)")
    except Exception as exc:
        logger.error(f"[ActivitySweep] moot-task closure failed: {exc}")


# Milestone auto-complete runs live (completion is reversible via the activity
# 'reopen' mode). Flip to True to only preview the eligible count.
MILESTONE_COMPLETE_DRY_RUN = False


def _run_anonymise_erasure_log() -> None:
    """Monthly: strip the personal link from erasure-register rows over two
    years old (sql/erasure_log_retention.sql).

    The register is append-only and permanent, which is what makes it evidence.
    Left alone it also becomes a permanent index of who asked to be forgotten,
    so the identifiers age out while the EVENT — when, by whom, how many rows,
    under what declared reason — is kept forever.

    Nothing happens for two years after the first erasure; that is the point.
    A job that does nothing today and the right thing in 2028 has to be wired
    now, because nobody will remember to wire it then."""
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT anonymise_old_erasure_log()")
                n = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        if n:
            logger.info(f"[retention] anonymised {n} erasure-register row(s) "
                        f"older than the retention window")
    except Exception as exc:
        logger.error(f"[retention] erasure-register anonymisation failed: {exc}")


def _run_expire_quotes() -> None:
    """Nightly: mark quotes past their validity as expired (C3.0).

    Idempotent and narrow — it touches only `draft`/`sent` quotes whose
    valid_until is strictly in the past, so accepted and declined offers are
    left alone."""
    try:
        from app.core import quotes
        res = quotes.expire_due()
        if res.get("expired"):
            logger.info(f"[quotes] nightly expiry closed {res['expired']} quote(s)")
    except Exception as exc:
        logger.error(f"[quotes] nightly expiry failed: {exc}")


def _run_complete_settled_activities() -> None:
    """Scheduled job: Activity↔Accounting/Order cooperation — auto-complete the
    auto-generated "milestone record" tasks whose underlying milestone has
    settled (invoice fully paid / order past 'pending'). These records were
    created open and never closed, so they piled up overdue and crushed the
    completion rate (4% → ~34% once cleared). The Activity agent reads the
    Accounting/Order agents' entity state and closes its own records — a
    cooperation that needs no event bus. Idempotent (only touches not-yet-
    completed), subject-gated (never touches genuine follow-up work)."""
    try:
        from app.core.database import execute_sp
        apply = 'false' if MILESTONE_COMPLETE_DRY_RUN else 'true'
        rows = execute_sp(
            f"SELECT fn_complete_settled_milestone_activities(NULL, {apply}) AS result"
        )
        result = (rows[0].get('result') or {}) if rows else {}
        logger.info(f"[MilestoneComplete] eligible={result.get('eligible')} "
                    f"completed={result.get('completed')} (apply={result.get('apply')})")
    except Exception as exc:
        logger.error(f"[MilestoneComplete] Scheduled job failed: {exc}", exc_info=True)


# Per-run cap on how many overdue invoices the nightly job dunns. Kept low for
# the initial production ramp so a brand-new autonomous subsystem proves itself
# on a small batch first; the per-invoice 20h idempotency guard rolls the rest
# forward on subsequent nights. Raise to 200 (the SQL default) once it's trusted.
AGENT_BUS_OVERDUE_MAX = 25


def _run_emit_overdue_invoice_events() -> None:
    """Scheduled job: emit invoice.overdue events for materially past-due
    invoices, feeding the agent-bus consumer (Accounting → Email dunning).

    Gated on the agent bus being enabled — emitting events with no consumer
    would just accumulate queue rows. Idempotent (one event / invoice / 20h).
    """
    try:
        from app.core import agent_bus
        if not agent_bus.ENABLED:
            logger.info("[AgentBus] overdue-invoice emit skipped (AGENT_BUS_ENABLED=0)")
            return
        from app.core.database import execute_sp
        rows = execute_sp(
            "SELECT fn_emit_overdue_invoice_events(%(max)s) AS result",
            {"max": AGENT_BUS_OVERDUE_MAX},
        )
        n = rows[0].get('result') if rows else 0
        logger.info(f"[AgentBus] emitted {n} invoice.overdue event(s)")
    except Exception as exc:
        logger.error(f"[AgentBus] overdue-invoice emit failed: {exc}", exc_info=True)


def _run_emit_hot_lead_events() -> None:
    """Scheduled job: emit lead.scored events for Hot (>=70) open leads, feeding
    the agent-bus consumer (Lead → Activity auto-outreach + Notifications).

    Gated on the agent bus being enabled. Idempotent (one event / lead / 20h).
    """
    try:
        from app.core import agent_bus
        if not agent_bus.ENABLED:
            logger.info("[AgentBus] hot-lead emit skipped (AGENT_BUS_ENABLED=0)")
            return
        from app.core.database import execute_sp
        rows = execute_sp("SELECT fn_emit_hot_lead_events() AS result")
        n = rows[0].get('result') if rows else 0
        logger.info(f"[AgentBus] emitted {n} lead.scored event(s)")
    except Exception as exc:
        logger.error(f"[AgentBus] hot-lead emit failed: {exc}", exc_info=True)


def _run_emit_sequence_step_events() -> None:
    """Scheduled job: emit sequence.step_due events for due playbook steps,
    feeding the agent-bus consumer (multi-step cadences — app/core/sequences.py).

    Gated on both the agent bus and SEQUENCES_ENABLED. Idempotent
    (one event / sequence / step / 2h)."""
    try:
        from app.core import agent_bus, sequences
        if not (agent_bus.ENABLED and sequences.ENABLED):
            return
        from app.core.database import execute_sp
        rows = execute_sp("SELECT fn_emit_sequence_step_events(50) AS result")
        n = rows[0].get('result') if rows else 0
        if n:
            logger.info(f"[Sequences] emitted {n} sequence.step_due event(s)")
    except Exception as exc:
        logger.error(f"[Sequences] step emit failed: {exc}", exc_info=True)


def _run_governance_expiry() -> None:
    """Scheduled job: flip pending approvals past their TTL to 'expired' so the
    queue stays honest (best-effort; tolerates the table not existing). Also
    backfills the independent critic onto any pending approval without one."""
    try:
        from app.core.governance import expire_stale
        res = expire_stale()
        if res.get("expired"):
            logger.info(f"[Governance] expired {res['expired']} stale approval(s)")
    except Exception as exc:
        logger.error(f"[Governance] expiry failed: {exc}", exc_info=True)
    try:
        from app.core import critic
        res = critic.review_pending()
        if res.get("reviewed"):
            logger.info(f"[Critic] backfilled {res['reviewed']} critique(s)")
    except Exception as exc:
        logger.error(f"[Critic] backfill failed: {exc}", exc_info=True)


def _run_intelligence_scoring() -> None:
    """Scheduled job: nightly customer-intelligence scoring pass — persists
    churn risk / RFM / preferred channel per customer account and posts
    churn_risk blackboard notes for the high band. No-op unless INTEL_ENABLED=1."""
    try:
        from app.core import intelligence
        if not intelligence.ENABLED:
            return
        res = intelligence.run_scoring_sync()
        logger.info(f"[Intelligence] scored={res['scored']} bands={res['bands']} "
                    f"high_notes={res['high_risk_notes_posted']}")
    except Exception as exc:
        logger.error(f"[Intelligence] scoring failed: {exc}", exc_info=True)


def _run_dq_propose() -> None:
    """Scheduled job (nightly): data-quality scan → governed fix proposals
    (normalize phones, merge duplicate contacts). Fixes only ever QUEUE —
    execution requires an approval. No-op unless DQ_ENABLED=1."""
    try:
        from app.core.data_quality import propose_fixes
        res = propose_fixes()
        if res.get("proposed"):
            logger.info(f"[DataQuality] proposed: {res['proposed']}")
    except Exception as exc:
        logger.error(f"[DataQuality] pass failed: {exc}", exc_info=True)


def _run_behavior_evals() -> None:
    """Scheduled job (nightly): golden-scenario behavior evals over the
    LLM-facing flows (SDR, auto-reply, planner, KB retrieval) — deterministic
    assertions, supervisor.alert on drift. No-op unless EVALS_ENABLED=1."""
    try:
        from app.core.evals import run_evals
        res = run_evals()
        if res.get("failed"):
            logger.warning(f"[Evals] FAILED: {res['failed']}")
    except Exception as exc:
        logger.error(f"[Evals] run failed: {exc}", exc_info=True)


def _run_idle_distill_pass() -> None:
    """Scheduled job: turn long-idle OPEN conversations into customer memory.

    Distillation previously had exactly ONE trigger — conversation close — and
    threads mostly do not get closed (61 of 62 were open), so the entire memory
    corpus was a single row. The writer was starved, not broken. Threads stay
    OPEN; only the memory is written. Re-distills only when a thread has new
    words since its memory was last written, so a steady-state pass spends no
    LLM budget."""
    try:
        from app.core.conversations import distill_idle
        res = distill_idle()
        if res.get("distilled"):
            logger.info(f"[Memory] distilled {res['distilled']} idle "
                        f"conversation(s) (examined {res['examined']})")
    except Exception as exc:
        logger.error(f"[Memory] idle distill failed: {exc}", exc_info=True)


def _run_retention_pass() -> None:
    """Scheduled job: expire data past its retention period. Self-gates on
    RETENTION_ENABLED (off by default — this DELETES). Only stores with an
    explicit policy and basis are touched; financial and audit records have no
    policy and are therefore untouchable."""
    try:
        from app.core.retention import purge
        res = purge()
        if res.get("total"):
            logger.info(f"[Retention] purged {res['total']} expired row(s)")
    except Exception as exc:
        logger.error(f"[Retention] purge failed: {exc}", exc_info=True)


def _run_memory_consolidation_pass() -> None:
    """Scheduled job: turn a customer's indexed records into COUNTED, evidence-
    linked memories (Customer Memory v1). Retrieval can find records about
    pricing; only consolidation can say the customer raised it four times.
    Cheap in steady state — a theme whose evidence_hash is unchanged is not
    rewritten."""
    try:
        from app.core.memory_consolidation import consolidate_pass
        res = consolidate_pass()
        if res.get("written") or res.get("dropped"):
            logger.info(f"[Memory] consolidated {res['entities']} customer(s): "
                        f"{res['written']} written, {res.get('dropped',0)} dropped")
    except Exception as exc:
        logger.error(f"[Memory] consolidation failed: {exc}", exc_info=True)


def _run_memory_observability_snapshot() -> None:
    """Persist one reading of the memory metrics.

    Never raises: an observer that can break the thing it observes gets
    switched off, and then there is no observability."""
    try:
        from app.core import memory_observability
        out = memory_observability.snapshot(persist=True)
        logger.info(f"[observability] snapshot {out}")
    except Exception as exc:                              # noqa: BLE001
        logger.warning(f"[observability] snapshot failed: {exc}")


def _run_content_index_pass() -> None:
    """Scheduled job: keep the semantic index over the CRM's unstructured text
    current (audit #2). Incremental and budgeted — a pass embeds at most
    CONTENT_INDEX_BATCH rows, and re-embeds only records whose
    (content_hash, model, dims) changed, so steady-state passes are nearly free
    and a model change re-indexes the corpus over subsequent passes with no
    manual step. Runs often because the value is freshness: a call logged this
    morning should be findable by meaning this afternoon."""
    try:
        from app.core.content_index import reindex
        res = reindex()
        if res.get("embedded"):
            logger.info(f"[ContentIndex] embedded {res['embedded']} record(s), "
                        f"{res.get('pending', 0)} pending")
        elif not res.get("ok") and res.get("reason") != "disabled":
            logger.warning(f"[ContentIndex] pass incomplete: "
                           f"{res.get('error') or 'embedding unavailable'}")
    except Exception as exc:
        logger.error(f"[ContentIndex] pass failed: {exc}", exc_info=True)


def _run_kb_draft_pass() -> None:
    """Scheduled job (nightly): the knowledge loop's mining side — pair
    resolved support threads with their human resolution, LLM-draft articles,
    and PROPOSE them through governance (kb.publish). Publishes nothing
    itself. No-op unless KB_DRAFT_ENABLED=1 (draft_pass self-gates)."""
    try:
        from app.core.knowledge import draft_pass
        res = draft_pass()
        if res.get("proposed"):
            logger.info(f"[Knowledge] proposed {len(res['proposed'])} article(s) "
                        f"from {res['threads']} resolved thread(s)")
    except Exception as exc:
        logger.error(f"[Knowledge] draft pass failed: {exc}", exc_info=True)


def _run_kb_gap_pass() -> None:
    """Scheduled job (nightly): the knowledge loop's demand side — take the
    most-asked questions the KB could NOT answer (kb_gaps, logged by every
    public channel), get the general answer from the owning module agent over
    A2A (read-only), LLM-generalize, and PROPOSE kb.publish through
    governance. No-op unless KB_DRAFT_ENABLED=1 — one switch for all KB
    mining (gap_pass self-gates)."""
    try:
        import asyncio as _aio

        from app.core.knowledge import gap_pass
        res = _aio.run(gap_pass())
        if res.get("proposed") or res.get("covered"):
            logger.info(f"[Knowledge] gap pass: {len(res.get('proposed') or [])} "
                        f"proposed, {res.get('covered', 0)} already covered")
    except Exception as exc:
        logger.error(f"[Knowledge] gap pass failed: {exc}", exc_info=True)


def _run_bottleneck_pass() -> None:
    """Scheduled job (weekly, Monday morning): the learning loop's process-
    health scan — untouched leads, stalled deals, unchased overdue invoices,
    aging tasks, unanswered inbound — consolidated into ONE upserted
    Orchestrator notification. Self-gates on BOTTLENECKS_ENABLED."""
    try:
        from app.core.learning import bottleneck_pass
        res = bottleneck_pass()
        if res.get("findings"):
            logger.info(f"[Learning] bottlenecks: {len(res['findings'])} "
                        f"area(s) flagged")
    except Exception as exc:
        logger.error(f"[Learning] bottleneck pass failed: {exc}", exc_info=True)


def _run_kb_hygiene_pass() -> None:
    """Scheduled job (weekly, Monday morning): KB staleness scan — articles
    past review_after or never used — ONE upserted Orchestrator notification.
    Self-gates on KB_HYGIENE_ENABLED; report-only (retiring stays human)."""
    try:
        from app.core.knowledge import hygiene_pass
        res = hygiene_pass()
        if res.get("findings"):
            logger.info(f"[Knowledge] hygiene: {len(res['findings'])} "
                        f"finding(s) flagged")
    except Exception as exc:
        logger.error(f"[Knowledge] hygiene pass failed: {exc}", exc_info=True)


def _run_scoring_train() -> None:
    """Scheduled job (weekly): predictive lead-scoring training — fit a
    candidate on settled leads and, when it beats the baseline, PROPOSE
    activating it through governance. A model can never activate itself.
    No-op unless SCORING_TRAIN_ENABLED=1 (train_and_propose self-gates)."""
    try:
        from app.core.scoring import train_and_propose
        res = train_and_propose()
        if res.get("proposed") or res.get("trained"):
            logger.info(f"[Scoring] trained={res.get('trained')} "
                        f"version={res.get('version')} "
                        f"proposed={res.get('proposed')} "
                        f"reason={res.get('reason')}")
    except Exception as exc:
        logger.error(f"[Scoring] training failed: {exc}", exc_info=True)


def _run_tuning_proposals() -> None:
    """Scheduled job (weekly): calibration → governance-proposed tuning. Reads
    the churn model's calibration and, when the evidence warrants, queues
    bounded tuning.adjust proposals for human approval. Proposes only — a
    parameter changes ONLY when an executive approves. No-op unless
    TUNING_PROPOSALS_ENABLED=1 (propose_from_calibration self-gates)."""
    try:
        from app.core.tuning import propose_from_calibration
        res = propose_from_calibration()
        if res.get("proposed") or res.get("inverted"):
            logger.info(f"[Tuning] proposed={res.get('proposed')} "
                        f"inverted={res.get('inverted')} skipped={res.get('skipped')}")
    except Exception as exc:
        logger.error(f"[Tuning] proposal pass failed: {exc}", exc_info=True)


def _run_objectives_pass() -> None:
    """Scheduled job: nightly goal-oriented objectives pass (Phase 8) — snapshot
    every active business objective (weekends included, when the supervisor tick
    doesn't run) and alert on at-risk/off-track ones. Runs after intelligence
    scoring so the churn metric reads tonight's bands. No-op unless
    OBJECTIVES_ENABLED=1 (run_objectives_pass self-gates)."""
    try:
        from app.core.objectives import run_objectives_pass
        res = run_objectives_pass()
        if res.get("alerted") or res.get("acted"):
            logger.info(f"[Objectives] alerted={res['alerted']} acted={res['acted']}")
    except Exception as exc:
        logger.error(f"[Objectives] pass failed: {exc}", exc_info=True)


def _run_supervisor_tick() -> None:
    """Scheduled job: proactive supervisor tick (Phase 3) — read the executive
    KPI pack, detect breaches, emit supervisor.alert events. No-op unless
    SUPERVISOR_ENABLED=1 (run_supervisor_tick self-gates).

    Runs ONCE PER ACTIVE TENANT inside an explicit tenant + SYSTEM actor context
    (blind spot #9). Single-org today → exactly one iteration on the default
    tenant, i.e. unchanged behaviour; in multi-tenant mode no tenant is silently
    skipped, and one tenant failing never blocks the rest."""
    try:
        from app.core.supervisor import run_supervisor_tick
        from app.core import tenancy
        out = tenancy.for_each_tenant(run_supervisor_tick, job="supervisor_tick")
        for tid, res in (out.get("results") or {}).items():
            if isinstance(res, dict) and res.get('breaches'):
                logger.info(f"[Supervisor][{tid}] breaches={res['breaches']} "
                            f"alerted={res.get('alerted')} acted={res.get('acted')}")
        for tid, err in (out.get("errors") or {}).items():
            logger.error(f"[Supervisor][{tid}] tick failed: {err}")
    except Exception as exc:
        logger.error(f"[Supervisor] tick failed: {exc}", exc_info=True)


def _run_notification_triage() -> None:
    """Scheduled job: notification triage — digest + auto-read the non-critical
    alert backlog so 'unread' reflects real work, not fan-out noise. No-op unless
    NOTIF_TRIAGE_ENABLED=1; writes only when NOTIF_TRIAGE_APPLY=1 (else dry-run)."""
    try:
        from app.core.notification_triage import run_triage_tick
        res = run_triage_tick()
        if not res.get("skipped"):
            logger.info(f"[NotifTriage] apply={res.get('apply')} "
                        f"before={res.get('unread_before')} after={res.get('unread_after')}")
    except Exception as exc:
        logger.error(f"[NotifTriage] tick failed: {exc}", exc_info=True)


def _run_pipeline_hygiene() -> None:
    """Scheduled job: pipeline hygiene — Orchestrator + Opportunity + Activity
    agents clean stale/slipped open deals (close-lost the dead, re-engage the
    slipped) so Active Pipeline reflects reality. No-op unless
    PIPELINE_HYGIENE_ENABLED=1; writes only when PIPELINE_HYGIENE_APPLY=1."""
    try:
        from app.core.pipeline_hygiene import run_pipeline_hygiene_tick
        res = run_pipeline_hygiene_tick()
        if not res.get("skipped"):
            logger.info(f"[PipelineHygiene] apply={res.get('apply')} "
                        f"closed_lost={res.get('closed_lost')} reengaged={res.get('reengaged')}")
    except Exception as exc:
        logger.error(f"[PipelineHygiene] tick failed: {exc}", exc_info=True)


def _run_ceo_briefing() -> None:
    """Scheduled job: email the CEO the morning strategic briefing (08:00 ET).
    No-op unless CEO_BRIEFING_ENABLED=1 and CEO_BRIEFING_EMAIL is set. Internal
    admin email — the CEO recipient lives in env config, not accounts/contacts.

    Runs once per active tenant in a tenant + SYSTEM actor context (blind spot
    #9) — single-org today means one iteration, unchanged."""
    try:
        from app.core.ceo_briefing import send_briefing
        from app.core import tenancy
        out = tenancy.for_each_tenant(send_briefing, job="ceo_briefing")
        for tid, res in (out.get("results") or {}).items():
            if isinstance(res, dict) and not res.get("skipped"):
                logger.info(f"[CEOBriefing][{tid}] {res}")
        for tid, err in (out.get("errors") or {}).items():
            logger.error(f"[CEOBriefing][{tid}] send failed: {err}")
    except Exception as exc:
        logger.error(f"[CEOBriefing] send failed: {exc}", exc_info=True)


def _run_capture_forecast_snapshot() -> None:
    """Scheduled job (monthly): capture a point-in-time pipeline forecast via
    generate_forecast_snapshot(90).

    Each month's snapshot is the pre-period forecast that the forecast-accuracy
    report (fn_forecast_accuracy / opportunity 'forecast accuracy') later grades
    against the revenue that actually closed. Running on the 1st means the
    snapshot predates the month it forecasts, which is exactly what makes the
    accuracy comparison meaningful. Each call inserts a fresh snapshot row."""
    try:
        from app.core.database import execute_sp
        rows = execute_sp("SELECT generate_forecast_snapshot(90) AS result")
        sid = rows[0].get('result') if rows else None
        logger.info(f"[ForecastSnapshot] captured snapshot {sid}")
    except Exception as exc:
        logger.error(f"[ForecastSnapshot] capture failed: {exc}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=== CRM Agent starting up (all 12 modules + home index + auth + email) ===")

    # Release configuration guard — BEFORE anything binds or serves. A deployed
    # environment missing a blocking control does not start; a laptop only gets
    # a log line. See app/core/release_guard.py for why refusing beats warning.
    from app.core import release_guard
    release_guard.enforce()

    db_ok = test_connection()
    logger.info(f"Database: {'OK' if db_ok else 'FAILED -- check DB_DSN in .env'}")

    # HA leader election (#7): the scheduler, IMAP poller and agent-bus consumer
    # are cluster-wide singletons — with >1 worker/replica only the LEADER may run
    # them, or every replica would duplicate dunning, bookings and bus drains.
    from app.core import leader
    _run_bg = leader.begin()
    if not _run_bg:
        logger.info("[HA] follower — background singletons (scheduler / IMAP / agent-bus) "
                    "run on the leader; this process serves HTTP only")

    # ── Daily order-status advancement scheduler (Windows-compatible) ──────────
    # Uses APScheduler so the same code runs on Windows (no pg_cron) and on
    # Railway/Linux. Wrapped in try/except so a missing package never crashes
    # the server — the app starts normally and pg_cron can be used instead.
    _scheduler = None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
        # All daily jobs run at 10 PM US Eastern. Using the named zone (not a
        # fixed UTC offset) means the same wall-clock 22:00 ET fires correctly
        # on Railway (UTC host) AND on a local machine in any timezone, and it
        # follows EST/EDT daylight-saving automatically. pytz (an APScheduler
        # dependency) ships the IANA database, so this also resolves on Windows.
        _scheduler = BackgroundScheduler(timezone="America/New_York")
        # Jobs are staggered within the 22:xx ET hour so the "advance" passes
        # run before the "seed" passes (age existing rows forward, then add new),
        # and concurrent writes to the same tables don't collide.
        _scheduler.add_job(
            _run_advance_opportunity_stages,
            trigger=CronTrigger(hour=22, minute=0),  # 10:00 PM ET — advance opp pipeline
            id="advance_opportunity_stages",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.add_job(
            _run_advance_order_statuses,
            trigger=CronTrigger(hour=22, minute=5),  # 10:05 PM ET — advance order statuses
            id="advance_order_statuses",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.add_job(
            _run_activities_auto_sweep,
            trigger=CronTrigger(hour=22, minute=10), # 10:10 PM ET — snooze non-critical overdue activities
            id="activities_auto_sweep",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.add_job(
            _run_complete_settled_activities,
            trigger=CronTrigger(hour=22, minute=12), # 10:12 PM ET — close milestone records whose invoice/order settled
            id="complete_settled_activities",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.add_job(
            _run_generate_daily_orders,
            trigger=CronTrigger(hour=22, minute=15), # 10:15 PM ET — seed 20-30 new orders
            id="generate_daily_orders",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.add_job(
            _run_generate_pipeline_opportunities,
            trigger=CronTrigger(hour=22, minute=20), # 10:20 PM ET — seed 3-5 new pipeline opps
            id="generate_pipeline_opportunities",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        # C3.0 — close out offers whose validity has passed. An expired quote
        # that still reads as `sent` is a commitment the business no longer
        # has, and it inflates every open-quote figure. Safe at 22:25 because
        # the predicate is `valid_until < current_date`: a quote valid THROUGH
        # today survives until the date actually rolls over.
        _scheduler.add_job(
            _run_expire_quotes,
            trigger=CronTrigger(hour=22, minute=25), # 10:25 PM ET — expire stale quotes
            id="expire_quotes",
            replace_existing=True,
        )
        # Retention for the erasure register — monthly, not nightly. It only
        # touches rows older than two years, so running it daily would be 30
        # no-op queries a month for no benefit.
        _scheduler.add_job(
            _run_anonymise_erasure_log,
            trigger=CronTrigger(day=1, hour=3, minute=0),   # 03:00 ET, 1st of month
            id="anonymise_erasure_log",
            replace_existing=True,
            misfire_grace_time=86400,   # a missed month should still run late
        )

        # Agent-bus nightly sweeps — emit events for the consumer to act on.
        # Run after the seed passes so they see the freshest data. No-op unless
        # AGENT_BUS_ENABLED=1 (the job functions self-gate).
        _scheduler.add_job(
            _run_emit_overdue_invoice_events,
            trigger=CronTrigger(hour=22, minute=25), # 10:25 PM ET — emit invoice.overdue
            id="emit_overdue_invoice_events",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.add_job(
            _run_emit_hot_lead_events,
            trigger=CronTrigger(hour=22, minute=30), # 10:30 PM ET — emit lead.scored (Hot)
            id="emit_hot_lead_events",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Agent sequences (cadences) — every 30 min, turn due playbook steps into
        # sequence.step_due bus events. Self-gates on AGENT_BUS_ENABLED +
        # SEQUENCES_ENABLED. Steps are day-granularity; 30 min is ample.
        _scheduler.add_job(
            _run_emit_sequence_step_events,
            trigger=IntervalTrigger(minutes=30),
            id="emit_sequence_step_events",
            replace_existing=True,
            misfire_grace_time=1800,
        )
        # Governance expiry — nightly 21:45 ET: stale pending approvals →
        # 'expired' (audited) instead of lingering as silent liabilities.
        _scheduler.add_job(
            _run_governance_expiry,
            trigger=CronTrigger(hour=21, minute=45),
            id="governance_expiry",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Customer-intelligence scoring — nightly 22:35 ET, after the seed and
        # advance passes so profiles reflect the day's final state. Self-gates
        # on INTEL_ENABLED. Feeds ai_summary + the supervisor churn detector.
        _scheduler.add_job(
            _run_intelligence_scoring,
            trigger=CronTrigger(hour=22, minute=35),
            id="intelligence_scoring",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Business objectives (Phase 8, goal-oriented supervisor) — nightly
        # 22:50 ET, after intelligence scoring (22:35) so the churn metric sees
        # fresh bands. Guarantees a daily snapshot even on weekends, when the
        # supervisor tick (which also runs the pass) is off.
        _scheduler.add_job(
            _run_objectives_pass,
            trigger=CronTrigger(hour=22, minute=50),
            id="objectives_pass",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Data-quality scan→propose — nightly 23:20 ET. Self-gates on
        # DQ_ENABLED; fixes queue for approval, nothing mutates directly.
        _scheduler.add_job(
            _run_dq_propose,
            trigger=CronTrigger(hour=23, minute=20),
            id="dq_propose",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Behavior evals — nightly 23:45 ET: golden scenarios through the
        # LLM-facing flows; supervisor.alert on drift. Self-gates on
        # EVALS_ENABLED. Runs LAST so it sees the day's final prompt/data state.
        _scheduler.add_job(
            _run_behavior_evals,
            trigger=CronTrigger(hour=23, minute=45),
            id="behavior_evals",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Knowledge-loop mining — nightly 23:00 ET: resolved support threads →
        # LLM-drafted article proposals (governed kb.publish). Self-gates on
        # KB_DRAFT_ENABLED; publishes nothing without an approval.
        # Idle-conversation distillation — hourly. Feeds One Customer Memory,
        # which the content index and every agent recall path read from. Runs
        # BEFORE the content-index pass conceptually: memories written here are
        # picked up as indexable content on a later sweep.
        _scheduler.add_job(
            _run_idle_distill_pass,
            trigger=IntervalTrigger(hours=1),
            id="memory_idle_distill",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=1800,
        )
        # Retention — daily at 03:30, off-peak. No-op unless RETENTION_ENABLED=1.
        _scheduler.add_job(
            _run_retention_pass,
            trigger=CronTrigger(hour=3, minute=30),
            id="retention_purge",
            replace_existing=True,
            misfire_grace_time=7200,
        )
        # Memory consolidation — every 6h, after the index has had time to
        # absorb the day's records. Themes change slowly; running it often would
        # spend clustering effort to rewrite the same statements.
        _scheduler.add_job(
            _run_memory_consolidation_pass,
            trigger=IntervalTrigger(hours=6),
            id="memory_consolidation",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        # Semantic index over CRM unstructured text — every 20 minutes. Cheap
        # when nothing changed (two catalogue reads, no API call); bounded when
        # it has (CONTENT_INDEX_BATCH rows per pass). coalesce+max_instances=1
        # so a slow pass can never stack on itself and double-spend embeddings.
        _scheduler.add_job(
            _run_content_index_pass,
            trigger=IntervalTrigger(minutes=20),
            id="content_index_pass",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )

        # Phase 5: one reading of every memory metric, KEPT. Every measurement
        # surface in this system was point-in-time, which made "drift over
        # time" uncomputable rather than merely unimplemented — the index moved
        # 7278 -> 8049 -> 7278 -> 7394 within one session and none of that is
        # recoverable now. Runs at 22:55, after the other daily jobs, so it
        # observes the state they left behind.
        _scheduler.add_job(
            _run_memory_observability_snapshot,
            trigger=CronTrigger(hour=22, minute=55),
            id="memory_observability_snapshot",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.add_job(
            _run_kb_draft_pass,
            trigger=CronTrigger(hour=23, minute=0),
            id="kb_draft_pass",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Knowledge-gap mining — nightly 23:05 ET (right after the thread
        # miner, so freshly-proposed articles can mark gaps covered next
        # night). Self-gates on KB_DRAFT_ENABLED (shared with the thread
        # and transcript miners); proposals only.
        _scheduler.add_job(
            _run_kb_gap_pass,
            trigger=CronTrigger(hour=23, minute=5),
            id="kb_gap_pass",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Process-bottleneck scan — weekly, Monday 08:30 ET, so the week
        # starts with a fresh "where is work piling up" heartbeat. Reads
        # only; self-gates on BOTTLENECKS_ENABLED.
        _scheduler.add_job(
            _run_bottleneck_pass,
            trigger=CronTrigger(day_of_week="mon", hour=8, minute=30),
            id="bottleneck_pass",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # KB hygiene — weekly, Monday 08:45 ET (right after bottlenecks):
        # flags stale/never-used articles into one Orchestrator notification.
        _scheduler.add_job(
            _run_kb_hygiene_pass,
            trigger=CronTrigger(day_of_week="mon", hour=8, minute=45),
            id="kb_hygiene_pass",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Calibration→tuning proposals — weekly, Monday 23:15 ET (after the
        # nightly scoring stack). Self-gates on TUNING_PROPOSALS_ENABLED;
        # queues governed tuning.adjust proposals, never writes parameters.
        _scheduler.add_job(
            _run_tuning_proposals,
            trigger=CronTrigger(day_of_week="mon", hour=23, minute=15),
            id="tuning_proposals",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Predictive lead-scoring training — weekly, Monday 23:30 ET. Self-
        # gates on SCORING_TRAIN_ENABLED; trains a candidate and proposes
        # activation through governance, never activates.
        _scheduler.add_job(
            _run_scoring_train,
            trigger=CronTrigger(day_of_week="mon", hour=23, minute=30),
            id="scoring_train",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Proactive supervisor (Phase 3) — every 3 hours, business hours (ET).
        # Self-gates on SUPERVISOR_ENABLED; reads KPIs, alerts on breaches.
        _scheduler.add_job(
            _run_supervisor_tick,
            trigger=CronTrigger(day_of_week="mon-fri", hour="9,12,15,18", minute=0),
            id="supervisor_tick",
            replace_existing=True,
            misfire_grace_time=1800,
        )
        # Notification triage — every NOTIF_TRIAGE_EVERY_HOURS (<24, e.g. 2 = the
        # orchestrator keeps unread handled ON TIME), else the legacy nightly
        # 21:55 ET run. Self-gates on NOTIF_TRIAGE_ENABLED; dry-run unless
        # NOTIF_TRIAGE_APPLY=1. Retention (pass F) rides along on every run.
        _triage_hours = int(os.getenv("NOTIF_TRIAGE_EVERY_HOURS", "24"))
        _scheduler.add_job(
            _run_notification_triage,
            trigger=(IntervalTrigger(hours=max(1, _triage_hours))
                     if _triage_hours < 24 else CronTrigger(hour=21, minute=55)),
            id="notification_triage",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Pipeline hygiene — daily 21:50 ET, just before notification triage.
        # Self-gates on PIPELINE_HYGIENE_ENABLED; dry-run unless PIPELINE_HYGIENE_APPLY=1.
        _scheduler.add_job(
            _run_pipeline_hygiene,
            trigger=CronTrigger(hour=21, minute=50),
            id="pipeline_hygiene",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # CEO morning briefing — daily 08:00 ET. Self-gates on CEO_BRIEFING_ENABLED
        # + CEO_BRIEFING_EMAIL (recipient is env config, not a contact/account).
        _scheduler.add_job(
            _run_ceo_briefing,
            trigger=CronTrigger(hour=8, minute=0),  # 8:00 AM ET — strategic CEO briefing
            id="ceo_briefing",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # Monthly forecast snapshot — 1st of the month, 00:30 ET — so the capture
        # predates the month it forecasts (builds forecast-accuracy history). A
        # full-day grace window means a restart anytime on the 1st still captures.
        _scheduler.add_job(
            _run_capture_forecast_snapshot,
            trigger=CronTrigger(day=1, hour=0, minute=30),
            id="capture_forecast_snapshot",
            replace_existing=True,
            misfire_grace_time=86400,
        )
        if not _run_bg:
            # Follower: the scheduler object is built but NOT started — only the
            # leader fires the daily jobs (HA singleton, #7).
            logger.info("[Scheduler] built but not started (HA follower)")
            _scheduler = None
        else:
            _scheduler.start()
            # A DEAD SCHEDULER MUST NOT LOOK LIKE A LIVE ONE.
            #
            # A single typo — `scheduler.add_job` for `_scheduler.add_job` —
            # raised NameError partway through setup. The broad handler below
            # logged it and the app carried on serving happily, so 22 of 28 jobs
            # never registered and start() never ran. Nightly order advancement,
            # quote expiry, activity sweeps, agent-bus emission and the
            # supervisor tick had simply not fired, and nothing anywhere said so.
            #
            # The count is recorded and reported on /health, so "the scheduler is
            # running" becomes a claim that can be contradicted.
            # A HEARTBEAT, BECAUSE SILENCE WAS INVISIBLE FOR TEN DAYS.
            #
            # Background work stopped on 2026-07-24 and was found on 2026-08-04.
            # Nothing alerted, because every signal that existed described
            # whether the process was SERVING, and it was. What nobody could
            # see was whether the scheduler had actually FIRED.
            #
            # Recording the last execution makes the failure detectable by one
            # HTTP call, and detection covers the failures nobody predicted —
            # which is most of them. Re-election fixes one cause; this catches
            # the next one.
            from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

            def _tick(event):
                app.state.scheduler_last_tick = _dt.datetime.now(
                    _dt.timezone.utc).isoformat(timespec="seconds")
                app.state.scheduler_last_job = getattr(event, "job_id", None)
                if event.code == EVENT_JOB_ERROR:
                    app.state.scheduler_last_error = (
                        f"{getattr(event, 'job_id', '?')}: "
                        f"{str(getattr(event, 'exception', ''))[:120]}")

            _scheduler.add_listener(_tick, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
            app.state.scheduler_started_at = _dt.datetime.now(
                _dt.timezone.utc).isoformat(timespec="seconds")

            app.state.scheduler_jobs = len(_scheduler.get_jobs())
            app.state.scheduler_running = True
            logger.info(
                "[Scheduler] Started (America/New_York) — "
            "opps advance 22:00 ET | orders advance 22:05 ET | "
            "activity sweep 22:10 ET | orders seed 22:15 ET | "
            "pipeline seed 22:20 ET | overdue-invoice emit 22:25 ET | "
            "hot-lead emit 22:30 ET | supervisor tick 9/12/15/18 ET (Mon-Fri)"
        )
    except ImportError:
        logger.warning(
            "[OrderAdvance] apscheduler not installed — daily scheduler skipped. "
            "Install with: pip install 'apscheduler>=3.10,<4'  "
            "Or use pg_cron on Railway/Supabase (see sql/fn_advance_order_statuses.sql)."
        )
    except Exception as exc:
        # Recorded, not just logged: a failure only present in a log line is a
        # failure nobody sees.
        app.state.scheduler_running = False
        app.state.scheduler_error = str(exc).splitlines()[0][:160]
        logger.error(f"[OrderAdvance] Scheduler setup failed: {exc}", exc_info=True)
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                pass
        _scheduler = None

    # Start autonomous inbound-email auto-reply poller (LEADER only — #7).
    from app.agents.email.imap_poller import start_poller, stop_poller
    if _run_bg:
        try:
            from app.agents.email.smtp_imap import EMAIL_ADDRESS
            start_poller(own_address=EMAIL_ADDRESS)
            logger.info("ImapPoller started — auto-reply active for info@agentorc.ca")
        except Exception as exc:
            logger.warning(f"ImapPoller failed to start: {exc}")

    # Start the agent-bus consumer (event-driven agent cooperation, Phase 1).
    # LEADER only — a second consumer would double-drain the event queue (#7).
    # No-op unless AGENT_BUS_ENABLED=1 — see app/core/agent_bus.py.
    if _run_bg:
        try:
            from app.core.agent_bus import start_agent_bus
            start_agent_bus()
        except Exception as exc:
            logger.warning(f"agent_bus failed to start: {exc}")

    yield

    try:
        from app.core.agent_bus import stop_agent_bus
        await stop_agent_bus()
    except Exception:
        pass
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    try:
        stop_poller()
    except Exception:
        pass
    try:
        leader.release()   # release the HA advisory lock (also freed on session end)
    except Exception:
        pass
    logger.info("=== CRM Agent shutting down ===")


app = FastAPI(
    title="CRM Agent",
    description=(
        "Unified CRM AI Agent -- all 12 modules on a single FastAPI server: "
        "accounts, contacts, products, orders, activities, opportunities, "
        "accounting, leads, analytics, notifications, store, auth, email. "
        "Plus /home-index for the dashboard KPI cards."
    ),
    version="2.4.0",
    lifespan=lifespan,
)

# Safety net: any WritePermissionError that escapes an agent (all routers map it
# themselves) becomes the real HTTP status — 401 anonymous (frontend auth shim
# opens the sign-in modal) / 403 signed-in viewer — instead of a generic 500.
from app.core.write_guard import WritePermissionError


# SATURATION IS NOT A FAULT, and must not look like one.
#
# PoolExhausted means every pooled connection is busy — the bound working as
# designed, keeping load off the database. Surfaced as a 500 it is
# indistinguishable from a crash: alerting pages someone for a bug that does
# not exist, load balancers keep sending traffic to a node that is merely full,
# and clients retry immediately instead of backing off.
#
# 503 with Retry-After is the honest answer. It is the one status that says
# "correct, temporarily out of capacity, come back".
from app.core.database import PoolExhausted                  # noqa: E402


@app.exception_handler(PoolExhausted)
async def _pool_exhausted_handler(request: Request, exc: PoolExhausted):
    logger.warning(f"[db] saturated, shedding request to {request.url.path}")
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "1"},
        content={"detail": "The service is at capacity. Please retry shortly.",
                 "reason": "database_connections_saturated"})


@app.exception_handler(WritePermissionError)
async def _write_permission_handler(request: Request, exc: WritePermissionError):
    return JSONResponse(status_code=exc.http_status, content={"detail": str(exc)})


class _PrivateNetworkMiddleware(BaseHTTPMiddleware):
    """Intercept Chrome Private Network Access preflights before CORSMiddleware.

    Chrome 94+ sends Access-Control-Request-Private-Network: true on OPTIONS
    preflights from null (file://) origins to localhost.  Must be registered
    AFTER CORSMiddleware so it wraps it and runs first.
    """
    async def dispatch(self, request: Request, call_next):
        if (request.method == "OPTIONS" and
                request.headers.get("access-control-request-private-network") == "true"):
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin":          request.headers.get("origin", "*"),
                    "Access-Control-Allow-Methods":         "POST, GET, OPTIONS",
                    "Access-Control-Allow-Headers":         "Content-Type",
                    "Access-Control-Allow-Private-Network": "true",
                    "Access-Control-Max-Age":               "600",
                },
            )
        return await call_next(request)


# Browser origins allowed to call the API (security hardening — CORS). Defaults
# to the production site + local dev; override with a comma-separated
# CORS_ALLOW_ORIGINS ('*' re-opens to everyone, e.g. for a throwaway demo).
_CORS_ORIGINS = [o.strip() for o in os.getenv(
    "CORS_ALLOW_ORIGINS",
    "https://agentorc.ca,https://www.agentorc.ca,"
    "http://localhost:8000,http://127.0.0.1:8000",
).split(",") if o.strip()]
logger.info(f"[security] CORS allow_origins: {_CORS_ORIGINS}")

from app.core.rate_limit import POSTURE as _RATE_LIMIT_POSTURE
logger.info(_RATE_LIMIT_POSTURE)

# Middleware stack — registered in reverse execution order (last added = first run).
# 1. CORSMiddleware  — added first → runs second
# 2. _PrivateNetworkMiddleware — added second → runs first (intercepts before CORS)
app.add_middleware(CORSMiddleware,
                   allow_origins=_CORS_ORIGINS,
                   allow_credentials=False,
                   allow_methods=["*"],
                   allow_headers=["*"])
app.add_middleware(_PrivateNetworkMiddleware)


@app.middleware("http")
async def normalise_path(request: Request, call_next):
    """Collapse leading double-slash (e.g. //order-chat -> /order-chat)."""
    if request.url.path.startswith("//"):
        corrected = "/" + request.url.path.lstrip("/")
        scope = dict(request.scope)
        scope["path"] = corrected
        scope["raw_path"] = corrected.encode()
        request = Request(scope, request.receive, request._send)
    return await call_next(request)


# -- API auth (security hardening #1) ───────────────────────────────────────
#   require_session : staged session gate on DATA endpoints — no-op until
#                     API_AUTH_ENABLED=1 (so the current frontend keeps working).
#   require_admin   : hard gate on privileged COMMAND endpoints — enforced once
#                     ADMIN_API_TOKEN is set; the frontend never calls these.
from fastapi import Depends
from app.core.auth_dep import require_admin, require_data_access

# _DATA = unified data gate. With API_PUBLIC_READ=1 (demo): anyone may READ, but
# create/update/delete require a logged-in Admin/authorized (member) session.
# With API_PUBLIC_READ=0: every data call requires a session (full lockdown).
_DATA  = [Depends(require_data_access)]
_ADMIN = [Depends(require_admin)]

# -- Home dashboard (registered first for fast routing).
#    PUBLIC: the landing page / KPI summary must render for anonymous visitors
#    (the marketing front page), so it is not session-gated.
# /home-index returns aggregate pipeline / leads / orders / alert counts. No
# customer records, but anonymous access lets anyone infer business scale, so it
# carries the same data dependency as every other CRM read.
app.include_router(home_router, dependencies=_DATA)

# -- Register all 10 AI agent routers
app.include_router(accounts_router,      dependencies=_DATA)
app.include_router(contacts_router,      dependencies=_DATA)
app.include_router(products_router,      dependencies=_DATA)
app.include_router(orders_router,        dependencies=_DATA)
app.include_router(activities_router,    dependencies=_DATA)
app.include_router(cases_router,          dependencies=_DATA)
app.include_router(opportunities_router, dependencies=_DATA)
app.include_router(accounting_router,    dependencies=_DATA)
app.include_router(leads_router,         dependencies=_DATA)
app.include_router(analytics_router,     dependencies=_DATA)
app.include_router(notifications_router, dependencies=_DATA)
app.include_router(orchestrator_router,  dependencies=_DATA)

# -- SSE push for notifications (EventSource can't send auth headers, so this
#    follows the read posture of the data endpoints).
from app.core.notify_stream import router as notify_stream_router
app.include_router(notify_stream_router, dependencies=_DATA)

# -- External integrations: calendar feed (PUBLIC — calendar clients can't send
#    headers; guarded by CALENDAR_FEED_TOKEN) + ERP CSV exports (admin).
from app.core.integrations import router_public as integrations_public_router
from app.core.integrations import router_admin as integrations_admin_router
app.include_router(integrations_public_router)
app.include_router(integrations_admin_router, dependencies=_ADMIN)

# -- Store module (direct SP routing — no AI agent).
#    PUBLIC: the customer-facing storefront must be browsable without a CRM login.
app.include_router(store_router)

# -- Auth module (direct DB routing — no AI agent). MUST stay open: it issues the
#    sessions the other endpoints check (login can't require being logged in).
app.include_router(auth_router)

# -- Consent / unsubscribe (CASL). MUST stay open: recipients opt out via the
#    signed link in commercial emails without logging in (links are HMAC-signed,
#    so the endpoint can't be used to unsubscribe arbitrary addresses).
from app.core.consent import router as consent_router
app.include_router(consent_router)

# -- Email agent (SMTP/IMAP + LangGraph) — ADMIN-ONLY regardless of the data
#    posture: this module reads the info@agentorc.ca mailbox and sends mail as
#    it, so public-read must not expose it. Admin = ADMIN_API_TOKEN or a
#    signed-in admin-role session. The public Contact Us form stays open.
from app.agents.email.router import public_router as email_public_router
app.include_router(email_router, dependencies=_ADMIN)
app.include_router(email_public_router)

# -- Voice (Azure Speech token mint)
app.include_router(voice_router, dependencies=_DATA)

# -- Agent bus (event-driven agent cooperation — status + on-demand tick)
from app.core.agent_bus import router as agent_bus_router
app.include_router(agent_bus_router, dependencies=_ADMIN)

# -- A2A protocol (Phase 2 — typed capability registry + dispatch)
from app.core.a2a import router as a2a_router
app.include_router(a2a_router, dependencies=_ADMIN)

# -- Supervisor (Phase 3 — proactive KPI breach detection)
from app.core.supervisor import router as supervisor_router
app.include_router(supervisor_router, dependencies=_ADMIN)

# -- Analytics trend anomalies (blindspot A1 — win-rate WoW / stalled / slump)
from app.core.analytics_signals import router as analytics_signals_router, act_router as analytics_act_router
app.include_router(analytics_signals_router, dependencies=_ADMIN)
# The on-demand "act on this" (A5) is _DATA-gated so the analytics page can call
# it; the proposal it creates still needs admin approval in the governance queue.
app.include_router(analytics_act_router, dependencies=_DATA)

# -- Governed READ authorization (P0 Trusted Semantic Core, step 3). Cross-record
# aggregate reads (metrics / explore) are gated to authorized roles ON TOP of the
# _DATA read gate: _DATA runs first (stamps request.state.session), then
# require_analytics_access denies non-admin/anonymous callers. Individual-record
# CRUD is unaffected — it stays governed by its own SPs. Row-level scoping (rep /
# customer) is deferred but config-ready in access.DataAccessContext.
from fastapi import Depends as _Depends
from app.core.access import require_analytics_access
_ANALYTICS = _DATA + [_Depends(require_analytics_access)]

# -- Ad-hoc / semantic-layer analytics (blindspot A2 — governed explore)
from app.core.semantic_query import router as analytics_explore_router
app.include_router(analytics_explore_router, dependencies=_ANALYTICS)

# -- Metric Registry (P0 Trusted Semantic Core, step 2) — one canonical
# definition per metric, self-describing.
from app.core.metrics import router as metrics_router
app.include_router(metrics_router, dependencies=_ANALYTICS)

# -- Governed CSV data onboarding (platform blindspot P1 — value-fast import)
from app.core.data_import import router as data_import_router
app.include_router(data_import_router, dependencies=_ADMIN)

# -- New-org readiness / empty-state guidance (platform blindspot P2)
from app.core.readiness import router as readiness_router
app.include_router(readiness_router, dependencies=_ADMIN)

# -- Data readiness / quality scoring (P1) — is the data good ENOUGH to decide on?
from app.core.data_readiness import router as data_readiness_router
app.include_router(data_readiness_router, dependencies=_ADMIN)

# -- Identity resolution (P1) — fuzzy duplicate candidates (accounts/contacts/leads)
from app.core.identity_resolution import router as identity_resolution_router
app.include_router(identity_resolution_router, dependencies=_ADMIN)

# -- Reversible identity links + resolved "golden record" view (P1). Links only —
# records are never rewritten, so every decision is reversible.
from app.core.identity_links import router as identity_links_router
app.include_router(identity_links_router, dependencies=_ADMIN)

# -- Data lifecycle / erasure (#8) — explicit delete/anonymize/retain policy across
# the distributed copies (custom fields, memories, transcripts, identity links);
# erasure itself is governed and irreversible.
from app.core.lifecycle import router as lifecycle_router
app.include_router(lifecycle_router, dependencies=_ADMIN)

# -- Demo / sample-data seed (platform blindspot P6 — time-to-value)
from app.core.demo import router as demo_router
app.include_router(demo_router, dependencies=_ADMIN)

# -- Custom Fields (platform blindspot P3 — data-model extensibility)
from app.core.custom_fields import router as custom_fields_router
app.include_router(custom_fields_router, dependencies=_ADMIN)

# -- Industry Starter Packs (platform blindspot P5 — verticalization)
from app.core.industry_packs import router as industry_packs_router
app.include_router(industry_packs_router, dependencies=_ADMIN)

from app.core.notification_triage import router as notif_triage_router
app.include_router(notif_triage_router, dependencies=_ADMIN)
from app.core.ceo_briefing import router as ceo_briefing_router
app.include_router(ceo_briefing_router, dependencies=_ADMIN)
from app.agents.executives.router import router as executives_router
app.include_router(executives_router)  # router already require_admin on every route

# -- Pipeline hygiene (Orchestrator + Opportunity + Activity cooperation)
from app.core.pipeline_hygiene import router as pipeline_hygiene_router
app.include_router(pipeline_hygiene_router, dependencies=_ADMIN)

# -- Identity Resolution (Unified Communication Layer, Phase 1a) — (channel,
#    handle) → one CRM party (external contact / internal employee).
from app.core.identity import router as identity_router
app.include_router(identity_router, dependencies=_ADMIN)

# -- Unified Conversation Object (Unified Communication Layer, Phase 1b) — one
#    cross-channel thread per person; ingest() resolves identity + threads.
from app.core.conversations import router as conversations_router
app.include_router(conversations_router, dependencies=_ADMIN)

# -- Live Human-Agent Takeover Console (blindspot #1) — the human SEAT on the
#    conversation spine: queue, takeover/release, AI-suggested reply, human send.
from app.core.agent_console import router as agent_console_router
app.include_router(agent_console_router, dependencies=_ADMIN)

# C5.0 — the customer portal. Registered with NO staff dependency: it is
# customer-facing, and its authorization is the customer session resolved by
# portal.customer_context, which opens the ONE customer scope. Adding _DATA or
# _ADMIN here would gate customers behind a staff role and quietly make the
# portal unreachable.
from app.core.portal import router as portal_router
app.include_router(portal_router)

# -- Universal Escalation Object (U1) — the durable obligation created when an
#    agent promises a human will follow up: owner, priority, SLA deadline. Wires
#    the no-code/embedded agents (#3/#6) into the takeover console (#1).
from app.core.escalation import router as escalation_router
app.include_router(escalation_router, dependencies=_ADMIN)

# -- Agent-Program Operations Analytics (blindspot #4) — the AI fleet as a
#    service operation: containment / escalation / CSAT proxy / cost per convo.
from app.core.agent_ops import router as agent_ops_router
app.include_router(agent_ops_router, dependencies=_ADMIN)

# -- No-Code Agent Authoring (blindspot #3) + Employee/IT internal service (#5).
#    Data-defined agents: authoring CRUD is admin-gated; the chat endpoint
#    self-gates (internal agents require a signed-in session, external are public
#    + rate-limited). Runtime is grounded, tool-less and write-less (safe by
#    default). The IT + People/HR agents are seeded rows (employee_service_seed).
from app.core.custom_agents import (admin_router as custom_agents_admin_router,
                                    public_router as custom_agents_public_router)
app.include_router(custom_agents_admin_router, dependencies=_ADMIN)
app.include_router(custom_agents_public_router)

# -- Agent Versioning & Publish Gate (U2). Data-defined agents are a DEPLOYMENT
#    system, so they get a deployment lifecycle: draft → evaluate → publish →
#    live, with version history and one-click rollback. Closes the hole where #3
#    (no-code authoring) bypassed #9 (the pre-deploy eval gate).
from app.core.agent_versions import router as agent_versions_router
app.include_router(agent_versions_router, dependencies=_ADMIN)

# -- Platform Self-Observability (U3). agent_ops measures what the AI workforce
#    ACHIEVES; this measures whether the workforce is FUNCTIONING, keeping the
#    promises it made (U1 escalation SLAs) and respecting its own controls
#    (U2 gate overrides). The 12k event backlog was found by accident — this is
#    how the platform notices its own failures instead.
from app.core.platform_health import router as platform_health_router
app.include_router(platform_health_router, dependencies=_ADMIN)

# -- LLM Provider Failover (U5). Policy-aware provider graph at the ONE LLM
#    chokepoint. Ships DISABLED (LLM_FAILOVER_ENABLED=0) because the Google key
#    is free-tier, where content may be used to train; the policy gate refuses
#    customer/internal data to a free-tier provider independently of the flag.
from app.core.llm_router import router as llm_router_router
app.include_router(llm_router_router, dependencies=_ADMIN)

# -- Action Authorization Layer (U4). Authored agents can be granted a curated
#    subset of EXISTING capabilities: reads execute scoped, writes become
#    governed proposals in the same approval queue an executive already
#    ratifies. An agent can neither invent a capability nor widen its own grant.
from app.core.agent_capabilities import router as agent_caps_router
app.include_router(agent_caps_router, dependencies=_ADMIN)

# -- MCP Client (U6). We already SERVE MCP (app/mcp_server.py); this lets our
#    agents CONSUME external MCP servers. Deliberately governed by U4's grants
#    rather than a second permission model, plus one extra rule: calling a third
#    party is egress, so internal-tier content is refused (U5's rule).
from app.core.mcp_client import router as mcp_client_router
app.include_router(mcp_client_router, dependencies=_ADMIN)

# -- Distributable Widget SDK (blindspot #6). One <script> tag embeds an
#    EXTERNAL custom agent on any site. Key CRUD is admin-gated; the /embed/v1
#    endpoints + /widget.js are public and origin-scoped per key (never expose
#    an internal agent). Builds on the custom-agents runtime.
from app.core.embed import (admin_router as embed_admin_router,
                            public_router as embed_public_router)
app.include_router(embed_admin_router, dependencies=_ADMIN)
app.include_router(embed_public_router)

# -- Compliance & Data-Residency Trust Center (blindspot #8). PUBLIC posture feed
#    (no secrets — prospect/reviewer-facing control inventory), rendered by
#    trust.html. Reflects real runtime controls; self-attested, not a cert.
from app.core.compliance import router as compliance_router
app.include_router(compliance_router)

# -- Testing at Scale: synthetic-utterance regression suite + pre-deploy gate
#    (blindspot #9). Admin-gated on-demand runs; also a CLI CI gate
#    (python -m app.core.eval_suite).
from app.core.eval_suite import router as eval_suite_router
app.include_router(eval_suite_router, dependencies=_ADMIN)

# -- Intelligent channel selection (Unified Communication Layer, Phase 4) — best
#    communication action for an objective + party (also A2A comms.select_channel).
from app.core.channel_selector import router as channel_selector_router
app.include_router(channel_selector_router, dependencies=_ADMIN)

# -- Executive Intelligence — executive = role + intelligence profile on the
#    Person/Employee (owners) identity; links execs to employees + per-exec profile.
from app.core.executive_intelligence import router as exec_intel_router
app.include_router(exec_intel_router, dependencies=_ADMIN)

# -- Blackboard (Phase 4 — shared agent memory)
from app.core.blackboard import router as blackboard_router
app.include_router(blackboard_router, dependencies=_ADMIN)

# -- Agent sequences (multi-step timed playbooks / cadences). Importing the
#    module also registers the sequence.step_due bus handler.
from app.core.sequences import router as sequences_router
app.include_router(sequences_router, dependencies=_ADMIN)

# -- Customer intelligence (nightly churn/preference profile scorer)
from app.core.intelligence import router as intelligence_router
app.include_router(intelligence_router, dependencies=_ADMIN)

# -- Marketing agent (segment → CASL-gated campaigns → measure)
from app.core.marketing import router as marketing_router
app.include_router(marketing_router, dependencies=_ADMIN)

# -- Learning loop (agent performance analytics + churn calibration read-side)
from app.core.learning import router as learning_router
app.include_router(learning_router, dependencies=_ADMIN)

# -- Business objectives (Phase 8 — goal-oriented supervisor)
from app.core.objectives import router as objectives_router
app.include_router(objectives_router, dependencies=_ADMIN)

# -- Governed tuning (calibration-proposed, human-approved model parameters)
from app.core.tuning import router as tuning_router
app.include_router(tuning_router, dependencies=_ADMIN)

# -- Context hydration (agents "born with context": the compact 360 pack)
from app.core.context import router as context_router
app.include_router(context_router, dependencies=_ADMIN)

# -- Knowledge loop (resolved cases → governed KB articles → smarter replies)
from app.core.knowledge import router as knowledge_router
app.include_router(knowledge_router, dependencies=_ADMIN)

# -- Semantic retrieval (embedding index over the KB; rag_block fuses FTS+meaning)
from app.core.semantic import router as semantic_router
app.include_router(semantic_router, dependencies=_ADMIN)

# -- Semantic index over the CRM's own unstructured text — activities, cases,
#    case comments, conversation messages, interaction memories (audit #2).
#    Admin-gated: this index contains INTERNAL notes. Customer-facing retrieval
#    never comes through here; it goes via customer_memory.recall_relevant,
#    which passes the verified customer's own scope + a 'customer' audience that
#    content_index.search enforces fail-closed.
from app.core.content_index import router as content_index_router
app.include_router(content_index_router, dependencies=_ADMIN)

# -- Customer Memory v1: consolidated, evidence-linked themes derived from the
#    index. Admin-gated; customer-facing recall goes through the audience-gated
#    memory_consolidation.recall(), never this router.
from app.core.memory_consolidation import router as customer_memories_router
app.include_router(customer_memories_router, dependencies=_ADMIN)

# -- Retention (#7): time-based expiry. DELETES data, so it is OFF by default
#    (RETENTION_ENABLED) and preview() is the approval surface.
from app.core.retention import router as retention_router
app.include_router(retention_router, dependencies=_ADMIN)

# -- Source freshness (#8): what "as of" is allowed to claim. A briefing can be
#    internally consistent and still describe three-day-old facts.
from app.core.data_sources import router as data_sources_router
app.include_router(data_sources_router, dependencies=_ADMIN)

# -- Deploy state: which migrations ran, and do all replicas apply the SAME
#    safety policy? Config divergence between replicas was undetectable.
from app.core.deploy_state import router as deploy_state_router
app.include_router(deploy_state_router, dependencies=_ADMIN)

# -- Assurance: shadow mode (what an agent WOULD say), calibration of the
#    confidence model against human judgement, and safety-path metrics
#    including verification-bias detection.
from app.core.memory_assurance import router as memory_assurance_router
app.include_router(memory_assurance_router, dependencies=_ADMIN)

# Phase 5 observability. Admin-gated like the assurance surface: these readings
# describe how much the system trusts its own claims about customers, which is
# not something to expose more widely than the claims themselves.
from app.core.memory_observability import router as memory_observability_router
app.include_router(memory_observability_router, dependencies=_ADMIN)

# Phase 2 paired shadow evaluation.
from app.core.shadow_eval import router as shadow_eval_router
app.include_router(shadow_eval_router, dependencies=_ADMIN)

# -- Verification throughput: the capacity plan that turns "human review does
#    not scale" into a staffing figure, plus the sampled-review work queue.
from app.core.verification_policy import router as verification_policy_router
app.include_router(verification_policy_router, dependencies=_ADMIN)

# -- Correlation-id trace (one id → the whole play across a2a/events/approvals)
from app.core.trace import router as trace_router
app.include_router(trace_router, dependencies=_ADMIN)

# -- Scenario simulation (read-only what-if over objectives math; audit #6)
from app.core.simulator import router as simulator_router
app.include_router(simulator_router, dependencies=_ADMIN)

# -- Outbound guard (deterministic triage on every outgoing message)
from app.core.outbound_guard import router as outbound_guard_router
app.include_router(outbound_guard_router, dependencies=_ADMIN)

# -- LLM meter (per-agent usage, budgets, tiering — the fleet's fuel gauge)
from app.core.llm_meter import router as llm_meter_router
app.include_router(llm_meter_router, dependencies=_ADMIN)

# -- Behavior evals (nightly CI for prompts: golden scenarios + drift alerts)
from app.core.evals import router as evals_router
app.include_router(evals_router, dependencies=_ADMIN)

# -- Data-quality agent (nightly detectors → governed, undoable fixes)
from app.core.data_quality import router as dq_router
app.include_router(dq_router, dependencies=_ADMIN)

# -- Bounded planner (goal → validated plan over registered capabilities;
#    reads execute, writes queue for governance approval)
from app.core.planner import router as planner_router
app.include_router(planner_router, dependencies=_ADMIN)

# -- Predictive lead scoring v2 (train → candidate → governed activation)
from app.core.scoring import router as scoring_router
app.include_router(scoring_router, dependencies=_ADMIN)

# -- Telephony channel (Twilio SMS + voice). The inbound webhook is PUBLIC —
#    the X-Twilio-Signature (HMAC over URL+params with the auth token) IS
#    the authorization; everything else is admin-gated.
from app.core.telephony import router as telephony_router
from app.core.telephony import public_router as telephony_public_router
app.include_router(telephony_router, dependencies=_ADMIN)
app.include_router(telephony_public_router)

# -- Channel transports (Unified Communication Layer, Phase 3) — provider
#    webhooks: WhatsApp (external), Slack + Teams (internal). PUBLIC by nature;
#    signature-verified when the provider secret is set (dev-permissive otherwise).
#    Slack /slack/interactive (in-thread approvals) is signature+identity gated.
from app.core.transports import router as transports_router
from app.core.transports import admin_router as transports_admin_router
app.include_router(transports_router)
app.include_router(transports_admin_router, dependencies=_ADMIN)   # /comms/announce (#5)

# -- Autonomous SDR (prospect-facing web chat + conversational voice).
#    PUBLIC by nature: chat is gated SDR_CHAT_ENABLED + per-IP rate-limited;
#    voice webhooks are Twilio-signature-verified + gated SDR_VOICE_ENABLED.
from app.core.sdr import router as sdr_router
from app.core.sdr import public_router as sdr_public_router
app.include_router(sdr_router, dependencies=_ADMIN)
app.include_router(sdr_public_router)

# -- External knowledge ingestion: upload a document / point at a URL →
#    chunk → LLM-draft articles → the SAME governed kb.publish queue.
#    Idempotent per (content hash, chunk), bounded per call by KB_INGEST_CAP.
from app.core.kb_ingest import router as kb_ingest_router
app.include_router(kb_ingest_router, dependencies=_ADMIN)

# -- Intent router (Orchestrator v2): LLM intent classification with the
#    keyword router as fallback; used by orchestrator delegation, the SMS +
#    voice operator tiers, and the KB gap miner. Kill switch
#    INTENT_ROUTER_ENABLED=0 → pure keyword routing, exactly v1 behavior.
from app.core.intent_router import router as intent_router_router
app.include_router(intent_router_router, dependencies=_ADMIN)

# -- Unified customer memory ("One Customer Memory"): cross-channel
#    conversation memory + owed commitments; written on conversation close,
#    recalled via the context pack and the verified voice greeting.
from app.core.customer_memory import router as customer_memory_router
app.include_router(customer_memory_router, dependencies=_ADMIN)

# -- Real-time voice (media streams): the WS endpoint is PUBLIC — carriers
#    cannot sign a WebSocket connect, so the HMAC token minted inside the
#    signature-verified inbound webhook IS the authorization. Status is
#    admin-gated. Gated VOICE_STREAM_ENABLED (default off = Gather loop).
from app.core.voice_stream import router as voice_stream_router
from app.core.voice_stream import router_ws as voice_stream_ws_router
app.include_router(voice_stream_router, dependencies=_ADMIN)
app.include_router(voice_stream_ws_router)

# -- Customer support voice line (tiered trust: KB for anyone, live read-only
#    CRM for allowlisted staff, OTP-verified account-scoped answers for
#    customers; changes become governance proposals). Webhooks are PUBLIC —
#    the carrier signature is the authorization; gated VOICE_SUPPORT_ENABLED.
from app.core.voice_support import router as voice_support_router
from app.core.voice_support import public_router as voice_support_public_router
app.include_router(voice_support_router, dependencies=_ADMIN)
app.include_router(voice_support_public_router)

# -- Real meeting booking (availability + booked meeting + signed .ics invite).
#    The invite link is PUBLIC because the HMAC token is the authorization
#    (same pattern as unsubscribe / governance decide links).
from app.core.booking import router as booking_router
from app.core.booking import public_router as booking_public_router
app.include_router(booking_router, dependencies=_ADMIN)
app.include_router(booking_public_router)

# -- Lead qualification (win probability + recommended rep)
from app.core.qualification import router as qualification_router
app.include_router(qualification_router, dependencies=_ADMIN)

# -- Governance (Phase 5 — confidence-gating + approval queue)
from app.core.governance import router as governance_router
app.include_router(governance_router, dependencies=_ADMIN)
# One-click approve/reject from the routed-approval email — PUBLIC because the
# HMAC token is the authorization (same pattern as the unsubscribe endpoint).
from app.core.governance import public_router as governance_public_router
app.include_router(governance_public_router)

# -- Admin Users console (manage auth_credentials). Router self-gates on require_admin.
from app.agents.admin_users.router import router as admin_users_router
app.include_router(admin_users_router)


@app.get("/auth.html")
async def serve_auth_html():
    """Serve auth.html so email verification redirect works at http://localhost:8000/auth.html"""
    return FileResponse("auth.html", media_type="text/html")


@app.get("/product-mgmt.html")
async def serve_product_chat_html():
    """Serve product-mgmt.html over http so AudioWorklet / blob: URLs work for the
    Azure Speech SDK (file:// origins are blocked from loading blob: workers)."""
    return FileResponse("product-mgmt.html", media_type="text/html")


# ── Chat-page routes ───────────────────────────────────────────────────────
# Serve every *-mgmt.html over http://<host>/<filename>.html so the Azure
# Speech SDK can use AudioWorklet (blocked on file:// origins). Each route
# is registered explicitly (rather than via StaticFiles) so we don't
# accidentally expose the whole project directory.
_CHAT_PAGES = [
    "account-mgmt.html",
    "accounting-mgmt.html",
    "activity-mgmt.html",
    "analytics-mgmt.html",
    "case-mgmt.html",
    "customer-portal.html",
    "contact-mgmt.html",
    "email-mgmt.html",
    "lead-mgmt.html",
    "notifications-mgmt.html",
    "opportunity-mgmt.html",
    "orchestrator-mgmt.html",
    "order-mgmt.html",
    "store-home.html",
    "executives-mgmt.html",
    "admin-users.html",
    "governance-mgmt.html",
    "knowledge-mgmt.html",
    "agent-console.html",
    "agent-ops.html",
    "agent-studio.html",
    "platform-health.html",
    "widget-demo.html",
    "trust.html",
    "setup.html",
    "index.html",
]

def _register_chat_page(filename: str) -> None:
    @app.get(f"/{filename}", name=f"serve_{filename.replace('-', '_').replace('.', '_')}")
    async def _serve():
        return FileResponse(filename, media_type="text/html")

for _page in _CHAT_PAGES:
    _register_chat_page(_page)


# ── Legacy redirects ─────────────────────────────────────────────────────────
# The *-chat.html modules were renamed to *-mgmt.html. Redirect the old URLs
# (existing bookmarks / search-indexed links) to the new names permanently.
_RENAMED_PAGES = [
    "account", "accounting", "activity", "analytics", "contact", "email",
    "lead", "notifications", "opportunity", "orchestrator", "order", "product",
]

def _register_legacy_redirect(slug: str) -> None:
    @app.get(f"/{slug}-chat.html", name=f"redirect_{slug}_chat_html", include_in_schema=False)
    async def _redirect():
        return RedirectResponse(url=f"/{slug}-mgmt.html", status_code=301)

for _slug in _RENAMED_PAGES:
    _register_legacy_redirect(_slug)


@app.get("/favicon.ico")
async def serve_favicon():
    """Silence the auto-requested /favicon.ico 404 across every page."""
    return FileResponse("logo/Conscestra_CRM_Logo.png", media_type="image/png")


@app.get("/")
async def root():
    return {
        "status":  "healthy",
        "service": "CRM Agent",
        "version": "2.4.0",
        "agents":  [
            "accounts", "contacts", "products", "orders",
            "activities", "opportunities", "accounting", "leads",
            "analytics", "notifications", "store", "auth", "email",
        ],
        "endpoints": {
            "home_index":    "GET /home-index",
            "accounts":      "/account-chat",
            "contacts":      "/contact-chat",
            "products":      "/prod-chat",
            "orders":        "/order-chat  (alias: /orders-chat)",
            "activities":    "/activity-chat",
            "opportunities": "/opportunity-chat",
            "accounting":    "/accounting-chat",
            "leads":         "/lead-chat  (alias: /leads-chat)",
            "analytics":     "/analytics-chat",
            "notifications": "/notifications-chat  (alias: /notification-chat)",
            "store":         "/store-chat  (direct SP — no AI agent)",
            "auth":          "/auth/signin, /auth/signup, /auth/signout, ...",
            "email":         "/email-chat  (SMTP/IMAP + LangGraph)",
        },
    }


@app.get("/health")
async def health():
    """Aggregate health check — home index + all 11 modules."""
    from app.agents.accounts.graph      import get_graph as ga
    from app.agents.contacts.graph      import get_graph as gc
    from app.agents.products.graph      import get_graph as gp
    from app.agents.orders.graph        import get_graph as go
    from app.agents.activities.graph    import get_graph as gact
    from app.agents.opportunities.graph import get_graph as gopp
    from app.agents.accounting.graph    import get_graph as gacc
    from app.agents.leads.graph         import get_graph as gl
    from app.agents.analytics.graph     import get_graph as gan
    from app.agents.notifications.graph import get_graph as gno
    from app.agents.store.graph         import get_graph as gstore
    try:
        from app.core import leader
        _ha = {"role": leader.role(), "runs_singletons": leader.is_leader()}
    except Exception:
        _ha = {"role": "unknown"}
    # DOES THE DATABASE ACTUALLY ANSWER?
    #
    # This endpoint reported "healthy" unconditionally: it checked that eleven
    # LangGraph objects had been constructed in memory and nothing else. When
    # DATABASE_URL was pointed at a role whose password did not match, /health
    # returned 200 "healthy" while every data request failed with
    # `password authentication failed`. Railway's health check passed, so the
    # broken deploy was rolled out and marked good.
    #
    # A health check that cannot fail is not a health check. This one runs a
    # real query, and the endpoint returns 503 when it cannot — which is what
    # makes a platform stop the rollout instead of completing it.
    _db: Dict[str, Any] = {"ok": False}
    try:
        from app.core.database import get_connection
        _conn = get_connection()
        try:
            with _conn.cursor() as _cur:
                _cur.execute("SELECT current_user, 1")
                _who, _ = _cur.fetchone()
            # Which role the app ACTUALLY connects as — the one fact that
            # cannot be established from outside the running process, and the
            # thing privilege separation lives or dies on.
            _db = {"ok": True, "connected_as": _who}
        finally:
            _conn.close()
    except Exception as exc:                                  # noqa: BLE001
        _db = {"ok": False, "error": str(exc).splitlines()[0][:180]}

    # Background jobs: reported, never fatal. A follower legitimately runs none,
    # and an HTTP node that cannot schedule can still serve reads correctly.
    try:
        from app.core.database import pool_utilisation
        _pool = pool_utilisation()
    except Exception:                                     # noqa: BLE001
        _pool = None

    _sched = {"running": getattr(app.state, "scheduler_running", None),
              "jobs": getattr(app.state, "scheduler_jobs", 0),
              "started_at": getattr(app.state, "scheduler_started_at", None),
              "last_tick": getattr(app.state, "scheduler_last_tick", None),
              "last_job": getattr(app.state, "scheduler_last_job", None)}
    # Age is what an alert can threshold on. A timestamp needs a human to do
    # arithmetic; seconds since the last fire does not.
    if _sched["last_tick"]:
        try:
            _sched["seconds_since_tick"] = int(
                (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.datetime.fromisoformat(_sched["last_tick"])).total_seconds())
        except Exception:                                     # noqa: BLE001
            pass
    if getattr(app.state, "scheduler_last_error", None):
        _sched["last_error"] = app.state.scheduler_last_error
    if getattr(app.state, "scheduler_error", None):
        _sched["error"] = app.state.scheduler_error

    _status = "healthy" if _db["ok"] else "degraded"
    _payload = {
        "status":  _status,
        "version": "2.2.0",
        "database": _db,
        "scheduler": _sched,
        "connections": _pool,
        "ha": _ha,   # leader | follower | standalone — which process runs the background singletons (#7)
        "home_index": {"endpoint": "GET /home-index", "sp": "sp_home_index"},
        "agents": {
            "accounts":      {"graph_ready": ga()     is not None},
            "contacts":      {"graph_ready": gc()     is not None},
            "products":      {"graph_ready": gp()     is not None},
            "orders":        {"graph_ready": go()     is not None},
            "activities":    {"graph_ready": gact()   is not None},
            "opportunities": {"graph_ready": gopp()   is not None},
            "accounting":    {"graph_ready": gacc()   is not None},
            "leads":         {"graph_ready": gl()     is not None},
            "analytics":     {"graph_ready": gan()    is not None},
            "notifications": {"graph_ready": gno()    is not None},
            "store":         {"graph_ready": gstore() is not None,
                              "ai_agent": False, "direct_sp": True},
        },
        "memory_window_size": settings.memory_window_size,
        "auth": {
            "graph_ready": True,
            "ai_agent": False,
            "direct_db": True,
            "endpoints": [
                "POST /auth/signup", "POST /auth/signin", "POST /auth/signout",
                "POST /auth/change-password",
                "POST /auth/password-reset/request",
                "POST /auth/password-reset/confirm",
                "POST /auth/verify-email",
            ],
        },
    }
    if not _db["ok"]:
        # 503, not 200-with-a-sad-field. A platform health check reads the
        # STATUS CODE; a body saying "degraded" behind a 200 is decoration.
        return JSONResponse(status_code=503, content=jsonable_encoder(_payload))
    return _payload


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    clear_session(session_id)
    return {"status": "cleared", "session_id": session_id}


@app.get("/sessions")
async def list_sessions():
    return {"sessions": active_sessions()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.debug)
