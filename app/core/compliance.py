"""Compliance & Data-Residency posture — a live, honest Trust Center feed.

Blindspot #8. We already HAVE strong controls — PII masking before any LLM call,
CASL/GDPR consent + suppression, immutable audit logging, RBAC, DB-enforced
read-only channels, four-layer AI guardrails. What we lacked was the packaging: a
coherent, prospect-facing compliance STORY, and one thing Agentforce (a US
platform) can't claim as easily — **Canadian data residency**.

This module surfaces that posture as structured data the Trust Center page renders.
Two honesty rules it lives by:
  1. It reflects the REAL runtime where it can (PII masking on? suppression table
     present? audit log present?) rather than asserting marketing claims.
  2. It never fabricates a certification. Everything here is a SELF-ATTESTED
     control inventory + roadmap — explicitly not a third-party audit (SOC 2 etc.),
     which is called out on the page and in the payload.

CONFIG (env — declared, non-secret)
  DATA_REGION          ''   e.g. 'ca-central-1' — the deployment region you attest
  LLM_ZERO_RETENTION   0    1 if your model-provider contract is zero-retention
  COMPLIANCE_CONTACT   privacy@agentorc.ca
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter

logger = logging.getLogger("compliance")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _table_exists(name: str) -> bool:
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
                return bool(cur.fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return False


def _pii_masking_on() -> bool:
    try:
        from app.core import privacy
        for attr in ("ENABLED", "PII_MASK_ENABLED", "MASK_ENABLED"):
            if hasattr(privacy, attr):
                return bool(getattr(privacy, attr))
        return True  # privacy module present; masking is on by default
    except Exception:
        return False


def _status(implemented: bool) -> str:
    return "implemented" if implemented else "configurable"


def _live_status(present: bool) -> str:
    """Status from a live check. Absent means NOT IMPLEMENTED, not 'configurable'.

    `_status()` maps False to "configurable", which is right for a control that
    exists and merely needs switching on — and wrong for one that is missing.
    Used for a live existence check it would report a vanished DSAR register as
    a configuration choice, and `summary()` does not count "configurable" as a
    gap. The report would have gone quiet about exactly the thing this check was
    added to catch."""
    return "implemented" if present else "not_implemented"


def _fresh_file(path: str, max_age_hours: float) -> bool:
    """Is this file present AND recent? Used for controls whose evidence is a
    file something else keeps updating.

    Freshness, not existence. A monitoring state file that stopped updating a
    week ago is evidence that monitoring STOPPED — reporting the control as
    present because the file is still on disk would be the same mistake as
    reading `scheduler.running` and not `scheduler.last_tick`."""
    import time as _t
    from pathlib import Path as _P
    try:
        p = _P(path)
        if not p.exists():
            return False
        return (_t.time() - p.stat().st_mtime) < max_age_hours * 3600
    except Exception:                                           # noqa: BLE001
        return False


def _newest_mirror_age_hours() -> float:
    """Hours since the most recent off-site copy. -1 when there is none."""
    import time as _t
    from pathlib import Path as _P
    target = _env("BACKUP_MIRROR_DIR")
    if not target:
        return -1.0
    try:
        copies = list(_P(target).glob("railway-*.dump"))
        if not copies:
            return -1.0
        return (_t.time() - max(c.stat().st_mtime for c in copies)) / 3600.0
    except Exception:                                           # noqa: BLE001
        return -1.0


def _db_object(name: str) -> bool:
    """Does this table/function actually exist? A live catalog lookup.

    Most of the inventory below states a status rather than testing one. That is
    how this report came to read 13/16 green on 2026-08-05 while no data-subject
    export existed — it never asked. Where a control leaves a trace in the
    schema, ask the schema."""
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT coalesce(to_regclass(%s)::text, "
                            "to_regproc(%s)::text)",
                            (f"public.{name}", f"public.{name}"))
                return cur.fetchone()[0] is not None
        finally:
            conn.close()
    except Exception:                                           # noqa: BLE001
        return False


def posture() -> Dict[str, Any]:
    """The full, honest posture. Live signals where verifiable; declared config
    where attested; roadmap where not yet done."""
    region = _env("DATA_REGION")
    zero_ret = _flag("LLM_ZERO_RETENTION")
    contact = _env("COMPLIANCE_CONTACT", "privacy@agentorc.ca")

    controls: List[Dict[str, str]] = [
        # ── Data residency ──
        {"area": "Data residency",
         "control": "Single-region hosting (application + PostgreSQL in one region)",
         "status": "configurable",
         "detail": (f"Declared region: {region}." if region else
                    "Set DATA_REGION to attest your deployment region "
                    "(e.g. ca-central-1 for Canadian residency).")},
        {"area": "Data residency",
         "control": "Canadian data residency option",
         "status": "configurable",
         "detail": "The architecture is single-region by design; deploy the app "
                   "and DB in a Canadian region to keep customer data in Canada."},
        # ── Data protection ──
        {"area": "Data protection",
         "control": "PII masking before any LLM prompt",
         "status": _status(_pii_masking_on()),
         "detail": "Emails, phone numbers and card-like digit runs are masked "
                   "before customer text reaches a model (app/core/privacy.py)."},
        {"area": "Data protection",
         "control": "Encryption in transit (TLS)",
         "status": "implemented",
         "detail": "All API and database traffic is TLS-encrypted at the platform."},
        {"area": "Data protection",
         "control": "Credentials & session tokens stored as hashes",
         "status": "implemented",
         "detail": "Sessions store only cryptographic token hashes; they expire "
                   "on inactivity and under an absolute lifetime."},
        # ── AI data handling ──
        {"area": "AI data handling",
         "control": "Model-provider zero-retention",
         "status": "attested" if zero_ret else "roadmap",
         "detail": ("Attested: the configured model provider operates under a "
                    "zero-retention agreement." if zero_ret else
                    "Set LLM_ZERO_RETENTION=1 once a zero-retention contract is "
                    "in place; PII is masked before prompts regardless.")},
        {"area": "AI data handling",
         "control": "Per-call model metering & budgets",
         "status": "implemented",
         "detail": "Every model call is metered (agent, tokens, cost, latency) "
                   "with optional per-agent budgets (app/core/llm_meter.py)."},
        # ── Consent / privacy law ──
        {"area": "Consent & privacy law",
         "control": "CASL/GDPR consent, suppression list & unsubscribe",
         "status": _status(_table_exists("email_suppression")),
         "detail": "Commercial email checks a suppression list, appends the "
                   "CASL footer and honours unsubscribe (HMAC links)."},
        {"area": "Consent & privacy law",
         "control": "Verified-recipient & opt-in send gates",
         "status": "implemented",
         "detail": "Outbound is gated on verified, opted-in addresses; autosend "
                   "is off by default (draft-first)."},
        # ── Access control ──
        {"area": "Access control",
         "control": "Role-based access control + admin-gated command APIs",
         "status": "implemented",
         "detail": "Data endpoints enforce role tiers; privileged command "
                   "endpoints require an admin token/session (fail-closed)."},
        {"area": "Access control",
         "control": "DB-enforced read-only public channels",
         "status": "implemented",
         "detail": "Public channels (e.g. inbound SMS) run every statement in a "
                   "PostgreSQL read-only transaction — the write is refused by "
                   "the database, not merely by policy."},
        {"area": "Access control",
         "control": "Write classification at the DB choke point (coverage-tested)",
         "status": "implemented",
         "detail": "The RESOLVED operation is classified where every statement "
                   "passes, and a test re-derives that from the stored procedures."},
        {"area": "Access control",
         "control": "Rate limiting & brute-force lockout",
         "status": "implemented",
         "detail": "Per-user and per-IP rate limits; password-reset throttling; "
                   "account lockout on repeated failures."},
        # ── Auditability ──
        {"area": "Auditability",
         "control": "Immutable, attributable audit logging",
         "status": _status(_table_exists("audit_log")),
         "detail": "Every create/update/delete is logged and attributable; "
                   "multi-agent plays share a correlation id (/trace/{id})."},
        # ── AI governance ──
        {"area": "AI governance",
         "control": "Human-in-the-loop approvals with amount floor",
         "status": "implemented",
         "detail": "Consequential actions route to an executive; any amount over "
                   "the floor pauses for human approval even at full confidence."},
        {"area": "AI governance",
         "control": "Independent critic + deterministic outbound guard",
         "status": "implemented",
         "detail": "A critic cross-checks every queued action; a deterministic "
                   "guard screens every outgoing message (humans and agents alike)."},

        # ────────────────────────────────────────────────────────────────────
        # ADDED 2026-08-05 after an architecture review found this report
        # returning 13/16 green while a data-subject export did not exist.
        # It was not wrong about the 16 it listed; it was wrong about being a
        # posture. A report that only enumerates controls you built will always
        # look finished, which is the failure it is supposed to detect.
        #
        # Everything below can report NOT IMPLEMENTED, and several currently do.
        # ────────────────────────────────────────────────────────────────────

        # ── Data subject rights ──
        {"area": "Data subject rights",
         "control": "Art. 15/20 — access and portability export",
         "status": _live_status(_db_object("dsar_requests")),
         "detail": "Machine-readable export of everything held about a subject, "
                   "with third-party data withheld under Art. 15(4). Manifest is "
                   "checked against the live schema so an undeclared table makes "
                   "the export refuse rather than under-disclose "
                   "(python -m app.core.dsar --coverage)."},
        {"area": "Data subject rights",
         "control": "Art. 17 — authorised, audited erasure",
         "status": _live_status(_db_object("erase_verifications_for_entity")),
         "detail": "Erasure runs through a SECURITY DEFINER function that checks "
                   "session_user and writes an erasure register."},
        {"area": "Data subject rights",
         "control": "Art. 5(1)(e) — erasure-register retention limit",
         "status": _live_status(_db_object("anonymise_old_erasure_log")),
         "detail": "The register is anonymised in place after two years; the "
                   "function refuses any window under 365 days."},
        {"area": "Data subject rights",
         "control": "Self-service DSAR request channel",
         "status": "not_implemented",
         "detail": "Export is an operator-run CLI. There is no authenticated "
                   "endpoint a subject can use themselves."},

        # ── Resilience ──
        {"area": "Resilience",
         "control": "Backup with verified restore",
         "status": "implemented",
         "detail": "Daily dump, restored into a throwaway database and compared "
                   "table-by-table against production every run. RTO measured at "
                   "3.6s for the restore step; RPO 24 hours."},
        {"area": "Resilience",
         "control": "Point-in-time recovery",
         "status": "not_implemented",
         "detail": "No WAL archiving; anything written between daily dumps is "
                   "unrecoverable (RPO 24h). Available from the hosting "
                   "provider as a paid plan feature — this is an unmade "
                   "purchasing decision, not an engineering limitation."},
        {"area": "Resilience",
         "control": "Off-site backup copy",
         # Live: a mirror that stopped updating is not a mirror. 72h tolerates
         # a disconnected external drive over a long weekend; beyond that the
         # second copy is stale enough to matter.
         "status": _live_status(0 <= _newest_mirror_age_hours() <= 72),
         "detail": (
             f"Verified dumps are mirrored to a second physical device "
             f"({_env('BACKUP_MIRROR_DIR') or 'unset'}); newest copy "
             f"{_newest_mirror_age_hours():.0f}h old."
             if _newest_mirror_age_hours() >= 0 else
             "No second copy found. Dumps are on a single machine — one drive "
             "failure loses the backups and the working tree together.")},
        {"area": "Resilience",
         "control": "Rehearsed production restore",
         "status": "roadmap",
         "detail": "Restore into a scratch database is exercised daily. Restore "
                   "INTO production has never been executed "
                   "(docs/runbook_restore.md §4)."},

        # ── Operations ──
        {"area": "Operations",
         "control": "Availability and job monitoring with alerting",
         # Live: the watchdog writes its state every run, so a state file that
         # stopped updating means the watchdog stopped. Declaring this control
         # "implemented" from a config value alone would reproduce the very
         # failure it exists to catch — a monitor nobody monitors.
         "status": _live_status(
             bool(_env("HEALTH_URL"))
             and _fresh_file(os.path.join(os.path.dirname(os.path.dirname(
                 os.path.dirname(os.path.abspath(__file__)))),
                 "backups", "health_state.json"), 2)),
         "detail": "Three independent layers: an external HTTP poller on "
                   "/health (survives this office losing power), a dead-man's "
                   "switch on the daily backup (detects the ABSENCE of a run, "
                   "which no status check can), and a local watchdog that reads "
                   "inside the response — scheduler last_tick age, database "
                   "role, leader status — and emails on state change. Status "
                   "here reflects whether the local watchdog has actually run "
                   "in the last 2 hours, not merely whether it is configured."},
        {"area": "Operations",
         "control": "Incident and recovery runbooks",
         "status": "implemented",
         "detail": "Restore, leader failure, incident escalation and credential "
                   "rotation, each stating its own unverified sections."},
        {"area": "Operations",
         "control": "Automated post-deploy verification",
         "status": "implemented",
         "detail": "secrets, database invariants, executed red-team attacks, "
                   "DSAR coverage, runtime-DDL audit and schema drift "
                   "(scripts/postdeploy_verify.py). Running it is a manual step."},
        {"area": "Operations",
         "control": "Change management with approval gates",
         "status": "not_implemented",
         "detail": "Migrations are applied by hand and deploys are manual. The "
                   "migration ledger is unreliable in both directions and is not "
                   "used as evidence; live schema comparison is."},

        # ── Key management ──
        {"area": "Key management",
         "control": "Managed key store (KMS/HSM) with rotation policy",
         "status": "not_implemented",
         "detail": "Secrets live in environment variables. No central store, no "
                   "automatic expiry, no access log."},
        {"area": "Key management",
         "control": "Signing key rotation without invalidating history",
         "status": _live_status(bool(_env("MEMORY_SIGNING_KEY"))),
         "detail": "Signatures carry a key id; superseded keys stay in a "
                   "verify-only keyring so rotation does not void past records."},
        {"area": "Key management",
         "control": "Asymmetric signing (verification without forgery)",
         "status": "not_implemented",
         "detail": "Signing is HMAC. Anyone able to verify the audit trail is "
                   "also able to forge it, which caps its evidential value for "
                   "an external auditor."},

        # ── Tenancy ──
        {"area": "Tenancy",
         "control": "Fail-closed tenant resolution",
         "status": _live_status(_db_object("tenants")),
         "detail": "Unknown, inactive and malformed tenants raise rather than "
                   "falling back to the default. Schema names are validated "
                   "before reaching search_path."},
        {"area": "Tenancy",
         "control": "Demonstrated data isolation between tenants",
         "status": "roadmap",
         "detail": "One tenant exists, so cross-tenant leakage cannot currently "
                   "be tested. Required before onboarding a second customer."},

        # ── Certification ──
        {"area": "Certification",
         "control": "SOC 2 Type II",
         "status": "not_implemented",
         "detail": "Several trust-services controls exist in substance. There is "
                   "no control matrix, no evidence collection and no period of "
                   "operation. Do not represent this product as SOC 2."},
        {"area": "Certification",
         "control": "ISO/IEC 27001 ISMS",
         "status": "not_implemented",
         "detail": "No statement of applicability, risk register or asset "
                   "inventory."},
        {"area": "Certification",
         "control": "ISO/IEC 42001 AI management system",
         "status": "not_implemented",
         "detail": "AI-specific controls exist in the product (governance queue, "
                   "human approval floors, guardrails, per-call model metering) "
                   "but are not organised as a management system."},
        {"area": "Certification",
         "control": "Personal data breach notification readiness",
         "status": "not_implemented",
         "detail": "Art. 33 allows 72 hours from awareness. No notification "
                   "template or decision procedure exists; drafting would start "
                   "from nothing against a running clock."},
    ]

    return {
        "product": "Conscestra CRM",
        "data_region": region or None,
        "residency_model": "single-region (application + database co-located)",
        "model_zero_retention_attested": zero_ret,
        "controls": controls,
        "data_subject_rights": [
            "Access / export of personal data on request",
            "Correction of inaccurate data (governed profile updates)",
            "Erasure / suppression (unsubscribe + suppression list)",
            "Consent withdrawal at any time",
        ],
        "sub_processors_note": "Model provider (for inference on PII-masked "
                               "prompts) and hosting/database provider. Maintain "
                               "your current sub-processor list alongside this page.",
        "retention_note": "Operational records are retained for the life of the "
                           "business relationship; audit logs are immutable; "
                           "suppression entries are retained to honour opt-outs.",
        "contact": contact,
        "attestation": "SELF-ATTESTED control inventory — NOT a third-party audit "
                       "or certification (e.g. SOC 2). Certifications, where "
                       "pursued, are tracked on the roadmap.",
    }


def summary() -> Dict[str, Any]:
    """Compact counts for a badge/health line.

    `gaps` is returned alongside the counts on purpose. A caller that renders
    only `by_status` can show a reassuring number; one that renders `gaps` has
    to show what is missing. Making the gap list the same shape as the count
    means a dashboard cannot display the good news without the bad."""
    p = posture()
    by: Dict[str, int] = {}
    for c in p["controls"]:
        by[c["status"]] = by.get(c["status"], 0) + 1
    gaps = [f"{c['area']}: {c['control']}" for c in p["controls"]
            if c["status"] in ("not_implemented", "roadmap")]
    return {"region": p["data_region"], "controls_total": len(p["controls"]),
            "by_status": by, "zero_retention": p["model_zero_retention_attested"],
            "gap_count": len(gaps), "gaps": gaps,
            "complete": not gaps}


# ============================================================================
# Router — PUBLIC (Trust Center content is meant for prospects/reviewers; it
# contains no secrets, only high-level control statements).
# ============================================================================

router = APIRouter(tags=["compliance"])


@router.get("/compliance/posture")
def api_posture():
    return posture()


@router.get("/compliance/summary")
def api_summary():
    return summary()
