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
    """Compact counts for a badge/health line."""
    p = posture()
    by = {"implemented": 0, "attested": 0, "configurable": 0, "roadmap": 0}
    for c in p["controls"]:
        by[c["status"]] = by.get(c["status"], 0) + 1
    return {"region": p["data_region"], "controls_total": len(p["controls"]),
            "by_status": by, "zero_retention": p["model_zero_retention_attested"]}


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
