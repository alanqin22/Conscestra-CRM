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

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
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


# Milestone auto-complete runs live (completion is reversible via the activity
# 'reopen' mode). Flip to True to only preview the eligible count.
MILESTONE_COMPLETE_DRY_RUN = False


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


def _run_supervisor_tick() -> None:
    """Scheduled job: proactive supervisor tick (Phase 3) — read the executive
    KPI pack, detect breaches, emit supervisor.alert events. No-op unless
    SUPERVISOR_ENABLED=1 (run_supervisor_tick self-gates)."""
    try:
        from app.core.supervisor import run_supervisor_tick
        res = run_supervisor_tick()
        if res.get('breaches'):
            logger.info(f"[Supervisor] breaches={res['breaches']} "
                        f"alerted={res.get('alerted')} acted={res.get('acted')}")
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
    admin email — the CEO recipient lives in env config, not accounts/contacts."""
    try:
        from app.core.ceo_briefing import send_briefing
        res = send_briefing()
        if not res.get("skipped"):
            logger.info(f"[CEOBriefing] {res}")
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
    db_ok = test_connection()
    logger.info(f"Database: {'OK' if db_ok else 'FAILED -- check DB_DSN in .env'}")

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
        _scheduler.start()
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
        logger.error(f"[OrderAdvance] Scheduler setup failed: {exc}", exc_info=True)
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                pass
        _scheduler = None

    # Start autonomous inbound-email auto-reply poller
    from app.agents.email.imap_poller import start_poller, stop_poller
    try:
        from app.agents.email.smtp_imap import EMAIL_ADDRESS
        start_poller(own_address=EMAIL_ADDRESS)
        logger.info("ImapPoller started — auto-reply active for info@agentorc.ca")
    except Exception as exc:
        logger.warning(f"ImapPoller failed to start: {exc}")

    # Start the agent-bus consumer (event-driven agent cooperation, Phase 1).
    # No-op unless AGENT_BUS_ENABLED=1 — see app/core/agent_bus.py.
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
app.include_router(home_router)

# -- Register all 10 AI agent routers
app.include_router(accounts_router,      dependencies=_DATA)
app.include_router(contacts_router,      dependencies=_DATA)
app.include_router(products_router,      dependencies=_DATA)
app.include_router(orders_router,        dependencies=_DATA)
app.include_router(activities_router,    dependencies=_DATA)
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

from app.core.notification_triage import router as notif_triage_router
app.include_router(notif_triage_router, dependencies=_ADMIN)
from app.core.ceo_briefing import router as ceo_briefing_router
app.include_router(ceo_briefing_router, dependencies=_ADMIN)
from app.agents.executives.router import router as executives_router
app.include_router(executives_router)  # router already require_admin on every route

# -- Pipeline hygiene (Orchestrator + Opportunity + Activity cooperation)
from app.core.pipeline_hygiene import router as pipeline_hygiene_router
app.include_router(pipeline_hygiene_router, dependencies=_ADMIN)

# -- Blackboard (Phase 4 — shared agent memory)
from app.core.blackboard import router as blackboard_router
app.include_router(blackboard_router, dependencies=_ADMIN)

# -- Governance (Phase 5 — confidence-gating + approval queue)
from app.core.governance import router as governance_router
app.include_router(governance_router, dependencies=_ADMIN)

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
    return {
        "status":  "healthy",
        "version": "2.2.0",
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
