"""New-org readiness / empty-state guidance (Platform blindspot P2).

The product assumed a populated DB: every dashboard, briefing, KPI and detector
presumes data exists, so a fresh or small org saw zeros and "no data" with no
guidance. This is the "getting started" surface — a single READ-ONLY inspection
of what's configured and populated, returning a prioritized next-step checklist
so "value fast for companies of all sizes" has a visible on-ramp.

Every probe is defensive: a missing table or an unreachable DB degrades to
`unknown`/`action_needed` and never raises. Nothing here writes.

  readiness() → {ready, score{done,total,pct}, next_step, checks[], features{}}

Essentials gate the score; optional items are informational posture. The single
`next_step` is the highest-priority essential still needing action — the one
thing to do next.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection, test_connection
from app.core.config import get_settings

logger = logging.getLogger("readiness")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _count(sql: str) -> Optional[int]:
    """COUNT probe that tolerates a missing table / unreachable DB (→ None)."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return int(cur.fetchone()[0])
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[readiness] count failed ({sql[:40]}...): {exc}")
        return None


def _item(id: str, title: str, category: str, status: str, detail: str,
          action: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    # status: ok | action_needed | not_configured | unknown
    return {"id": id, "title": title, "category": category, "status": status,
            "detail": detail, "action": action}


# ============================================================================
# CHECKS  (ordered by priority — the first essential 'action_needed' becomes
# the headline next_step)
# ============================================================================

def _check_database() -> Dict[str, Any]:
    ok = False
    try:
        ok = test_connection()
    except Exception:
        ok = False
    return _item("database", "Database connection", "essential",
                 "ok" if ok else "action_needed",
                 "Connected." if ok else "Cannot reach the database — check DB_DSN / DATABASE_URL.",
                 None if ok else {"label": "Set the DB connection string", "env": "DATABASE_URL"})


def _check_llm() -> Dict[str, Any]:
    s = get_settings()
    if s.llm_provider == "openai":
        ok = bool(s.openai_api_key)
        return _item("llm", "AI model configured", "essential",
                     "ok" if ok else "action_needed",
                     f"OpenAI · {s.openai_model}." if ok
                     else "OpenAI selected but OPENAI_API_KEY is empty — agents will fall back to deterministic responses.",
                     None if ok else {"label": "Set your OpenAI key", "env": "OPENAI_API_KEY"})
    return _item("llm", "AI model configured", "essential", "ok",
                 f"Local Ollama · {s.ollama_model} ({s.ollama_base_url}).", None)


def _check_core_records() -> Dict[str, Any]:
    a = _count("SELECT count(*) FROM accounts WHERE COALESCE(is_deleted,false)=false")
    c = _count("SELECT count(*) FROM contacts WHERE COALESCE(is_deleted,false)=false")
    l = _count("SELECT count(*) FROM leads WHERE COALESCE(is_deleted,false)=false")
    total = sum(v for v in (a, c, l) if v is not None)
    have = total > 0
    return _item("core_records", "Your book of business is loaded", "essential",
                 "ok" if have else "action_needed",
                 (f"{a or 0} accounts · {c or 0} contacts · {l or 0} leads."
                  if have else "No accounts, contacts or leads yet — import your existing records to get value fast."),
                 None if have else {"label": "Import accounts / contacts / leads (CSV)",
                                    "endpoint": "POST /import/preview", "page": "account-mgmt.html"})


def _check_catalog() -> Dict[str, Any]:
    p = _count("SELECT count(*) FROM products WHERE COALESCE(is_active,true)")
    have = bool(p)
    return _item("catalog", "Product catalog", "optional",
                 "ok" if have else "not_configured",
                 f"{p} active product(s)." if have
                 else "No products yet — add a catalog to enable commerce, store chat and inventory analytics.",
                 None if have else {"label": "Add products", "page": "product-mgmt.html"})


def _check_knowledge() -> Dict[str, Any]:
    # Live KB uses status='active' for published articles; fall back to a total
    # count if the status column/values differ on another deployment.
    k = _count("SELECT count(*) FROM knowledge_articles WHERE status='active'")
    if not k:
        k = _count("SELECT count(*) FROM knowledge_articles")
    have = bool(k)
    return _item("knowledge", "Knowledge base seeded", "essential",
                 "ok" if have else "action_needed",
                 f"{k} approved article(s) grounding the auto-reply & SDR." if have
                 else "No knowledge articles — the auto-reply and SDR have nothing to ground answers in. Ingest docs/URLs or seed the KB.",
                 None if have else {"label": "Ingest knowledge (doc/URL)",
                                    "endpoint": "POST /knowledge/ingest", "page": "knowledge-mgmt.html"})


def _check_email() -> Dict[str, Any]:
    s = get_settings()
    ok = bool(s.smtp_host and s.smtp_user and s.smtp_pass)
    return _item("email", "Email (SMTP) configured", "essential",
                 "ok" if ok else "action_needed",
                 f"Sending as {s.smtp_from} via {s.smtp_host}." if ok
                 else "SMTP not set — signup OTP, order emails, dunning, briefings and the outbound auto-reply can't send.",
                 None if ok else {"label": "Configure SMTP", "env": "SMTP_HOST / SMTP_USER / SMTP_PASS"})


def _check_executives() -> Dict[str, Any]:
    n = _count("SELECT count(*) FROM executives WHERE is_active")
    have = bool(n)
    return _item("executives", "Executives configured", "essential",
                 "ok" if have else "action_needed",
                 f"{n} active executive(s) for briefing delivery & approval routing." if have
                 else "No executives — governance approvals have no one to route to and briefings have no recipients.",
                 None if have else {"label": "Add executives", "page": "executives-mgmt.html"})


def _check_voice() -> Dict[str, Any]:
    s = get_settings()
    ok = bool(s.azure_speech_key)
    return _item("voice", "Voice (Azure Speech)", "optional",
                 "ok" if ok else "not_configured",
                 f"Azure Speech in {s.azure_speech_region}." if ok
                 else "No AZURE_SPEECH_KEY — browser STT falls back to the Web Speech API (still works).",
                 None if ok else {"label": "Set Azure Speech key (optional)", "env": "AZURE_SPEECH_KEY"})


def _check_telephony() -> Dict[str, Any]:
    """Report the carrier that is actually LIVE, and whether IT has keys.

    This used to answer "are any carrier credentials present?" and label the
    result "Twilio" whenever Twilio credentials existed — so an account
    running on Telnyx, with both credential sets configured, was told its
    carrier was Twilio. Worse, leftover credentials for the carrier you are
    NOT using reported "ok" while the live one had no keys at all, which is
    precisely the state a readiness check exists to catch."""
    from app.core.telephony import _provider
    live = _provider()
    name = "Telnyx" if live == "telnyx" else "Twilio"
    env = ("TELNYX_API_KEY" if live == "telnyx"
           else "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")
    ok = (bool(os.getenv("TELNYX_API_KEY")) if live == "telnyx"
          else bool(os.getenv("TWILIO_ACCOUNT_SID")
                    and os.getenv("TWILIO_AUTH_TOKEN")))
    return _item("telephony", "SMS / voice carrier", "optional",
                 "ok" if ok else "not_configured",
                 f"{name} configured (TELEPHONY_PROVIDER={live})." if ok
                 else (f"TELEPHONY_PROVIDER={live} but no {name} keys — SMS "
                       f"and outbound calls are unavailable (all other "
                       f"channels still work)."),
                 None if ok else {"label": f"Add {name} keys (optional)",
                                  "env": env})


CHECKS: List[Callable[[], Dict[str, Any]]] = [
    _check_database, _check_llm, _check_core_records, _check_knowledge,
    _check_email, _check_executives, _check_catalog, _check_voice, _check_telephony,
]


# ============================================================================
# Posture flags (informational — which optional subsystems are switched on)
# ============================================================================

_FEATURE_FLAGS = {
    "agent_bus":          "AGENT_BUS_ENABLED",
    "autosend":           "AGENT_BUS_AUTOSEND",
    "supervisor":         "SUPERVISOR_ENABLED",
    "supervisor_planner": "SUPERVISOR_PLANNER",
    "ceo_briefing":       "CEO_BRIEFING_ENABLED",
    "sequences":          "SEQUENCES_ENABLED",
    "intelligence":       "INTEL_ENABLED",
    "multilingual":       "MULTILINGUAL_ENABLED",
    "explore":            "ANALYTICS_EXPLORE_ENABLED",
    "ha_leader_election": "HA_LEADER_ELECTION",
}


def _features() -> Dict[str, bool]:
    # explore defaults on; the rest default off.
    return {name: _flag(env, "1" if name == "explore" else "0")
            for name, env in _FEATURE_FLAGS.items()}


# ============================================================================
# PUBLIC API
# ============================================================================

def readiness() -> Dict[str, Any]:
    checks = [c() for c in CHECKS]
    essentials = [c for c in checks if c["category"] == "essential"]
    done = sum(1 for c in essentials if c["status"] == "ok")
    total = len(essentials)
    next_step = next((c for c in essentials if c["status"] in ("action_needed", "unknown")), None)
    return {
        "ready": done == total,
        "score": {"done": done, "total": total,
                  "pct": round(100.0 * done / total) if total else 100},
        "next_step": ({"id": next_step["id"], "title": next_step["title"],
                       "detail": next_step["detail"], "action": next_step["action"]}
                      if next_step else None),
        "checks": checks,
        "features": _features(),
        "summary": (f"Setup {done}/{total} essentials complete."
                    + (f" Next: {next_step['title']}." if next_step else " You're ready.")),
    }


# ============================================================================
# Router (admin)
# ============================================================================

router = APIRouter(tags=["readiness"])


@router.get("/setup/readiness")
def setup_readiness():
    """New-org readiness — what's configured/populated + the next step to value."""
    return readiness()
