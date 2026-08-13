"""Release configuration guard — Foundation Release (C1–C5).

A control that only engages when an environment variable is set is not a
control until somebody sets it. The calendar feed proved this: the secret-URL
pattern was implemented correctly and the feed was still serving 903 KB of
account names, commercial margins and 2,000 email addresses, because
CALENDAR_FEED_TOKEN was unset and the code fell through to its documented
"demo public-read posture".

So the posture itself is now checked at startup. In a deployed environment the
process REFUSES TO START rather than serving unsecured data — a failed deploy
is recoverable in minutes; a public feed of customer names is not.

WHY REFUSE RATHER THAN WARN: a warning in a log nobody reads is the same
outcome as no control at all. That is precisely how this shipped unsecured in
the first place.

THE ESCAPE HATCH IS DELIBERATE, NOT A LOOPHOLE. An operator who genuinely wants
a public calendar can set CALENDAR_FEED_PUBLIC=1. That turns an accident into a
decision, which is the entire point — and it keeps a real deployment from being
locked out by a control it disagrees with.

Local development is unaffected: nothing here fires unless the environment
looks deployed.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger("release_guard")


class UnsafeConfiguration(RuntimeError):
    """A deployed environment is missing a control that protects real data."""


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def is_deployed() -> bool:
    """Does this look like a deployed environment rather than a laptop?

    Checked in order of confidence. An explicit declaration wins; otherwise the
    platform's own markers; otherwise the app's public URL. Every signal is
    positive evidence of deployment — absence of evidence keeps the checks
    quiet, so a developer is never blocked by a guard meant for production.
    """
    declared = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    if declared:
        return declared in ("prod", "production", "staging", "live")
    # Railway sets these itself; their presence is not something a laptop fakes.
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
        return True
    url = (os.getenv("APP_URL") or "").strip().lower()
    if url and not any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0")):
        return True
    return False


# ── the checks ──────────────────────────────────────────────────────────────
# Each returns (ok, severity, message). BLOCKING failures stop startup in a
# deployed environment; ADVISORY ones are logged loudly and reported.

def _check_calendar_feed() -> Dict[str, Any]:
    token = (os.getenv("CALENDAR_FEED_TOKEN") or "").strip()
    if token:
        weak = len(token) < 24
        return {"control": "calendar_feed", "ok": True,
                "severity": "advisory" if weak else "ok",
                "message": ("CALENDAR_FEED_TOKEN is set but short — use at "
                            "least 24 random characters, it is the only thing "
                            "protecting the feed"
                            if weak else "calendar feed requires a token")}
    if _flag("CALENDAR_FEED_PUBLIC"):
        return {"control": "calendar_feed", "ok": True, "severity": "advisory",
                "message": "calendar feed is PUBLIC by explicit choice "
                           "(CALENDAR_FEED_PUBLIC=1) — it exposes account "
                           "names, margins and invoice references"}
    return {"control": "calendar_feed", "ok": False, "severity": "blocking",
            "message": "CALENDAR_FEED_TOKEN is not set. /calendar/activities.ics "
                       "would serve account names, commercial margins and email "
                       "addresses to anyone. Set CALENDAR_FEED_TOKEN to a long "
                       "random value, or set CALENDAR_FEED_PUBLIC=1 to accept "
                       "public access deliberately."}


def _check_api_auth() -> Dict[str, Any]:
    """Report the posture the application IS RUNNING ON.

    Deliberately reads auth_dep's RESOLVED values rather than re-deriving them
    from os.environ. The first version of this check read the env directly and
    reproduced the very trap the release review documented:

        API_SECURITY_MODE=locked  ->  effective 'locked'  ->  reported "not
                                      enforcing"          (false alarm)
        API_SECURITY_MODE=open    ->  effective 'open'    ->  reported "CLEAN"
                                      with API_AUTH_ENABLED=1 also set
                                                          (FALSE ALL-CLEAR)

    The second is why this matters: an application with no authentication at
    all passed its own security check. A guard that reports the wrong posture
    is worse than no guard, because it manufactures confidence.

    Deliberately softer than the calendar check: API_SECURITY_MODE governs a
    staged rollout across many endpoints, and hard-stopping a deploy over a
    posture decision could take down a working system. The calendar feed is one
    endpoint serving customer data with nothing else in front of it.
    """
    try:
        from app.core import auth_dep
        posture = auth_dep.SECURITY_POSTURE
        conflict = getattr(auth_dep, "POSTURE_CONFLICT", "")
    except Exception as exc:
        return {"control": "api_auth", "ok": False, "severity": "advisory",
                "message": f"could not resolve the security posture: {exc}"}

    # Named because it is the trap, not a footnote: an operator who sets
    # API_AUTH_ENABLED=1 beside a stale API_SECURITY_MODE gets the mode's
    # answer silently, and would otherwise read this report as agreement.
    note = (f" ({conflict} is set but IGNORED — API_SECURITY_MODE wins.)"
            if conflict else "")

    if posture == "locked":
        return {"control": "api_auth", "ok": True,
                "severity": "advisory" if conflict else "ok",
                "message": "posture=locked — every data call requires a login."
                           + note}
    if posture == "public-read":
        return {"control": "api_auth", "ok": True, "severity": "advisory",
                "message": "posture=public-read — anyone may READ CRM data; "
                           "writes require an Admin/writer login. Deliberate "
                           "for a demo; use API_SECURITY_MODE=locked otherwise."
                           + note}
    return {"control": "api_auth", "ok": False, "severity": "advisory",
            "message": "posture=open — CRM data endpoints enforce NOTHING. "
                       "Set API_SECURITY_MODE=locked (or public-read) before "
                       "exposing this deployment." + note}


def _check_admin_token() -> Dict[str, Any]:
    tok = (os.getenv("ADMIN_API_TOKEN") or "").strip()
    if not tok:
        return {"control": "admin_token", "ok": False, "severity": "advisory",
                "message": "ADMIN_API_TOKEN is unset — privileged command "
                           "endpoints rely solely on an admin session."}
    return {"control": "admin_token", "ok": True, "severity": "ok",
            "message": "ADMIN_API_TOKEN is set (rotate per environment)"}


def _check_training_ack() -> Dict[str, Any]:
    """LLM_ALT_TIER_TRAINING_ACK accepts that a FREE-tier provider may train on
    whatever is sent. That is defensible for synthetic local data. In a
    deployed environment the CONVERSATIONS are real people even when the CRM
    records are synthetic, so this must not travel."""
    if _flag("LLM_ALT_TIER_TRAINING_ACK") and \
            (os.getenv("LLM_ALT_TIER", "free").strip().lower() != "paid"):
        return {"control": "llm_training_ack", "ok": False,
                "severity": "blocking",
                "message": "LLM_ALT_TIER_TRAINING_ACK=1 with a non-paid tier in "
                           "a deployed environment. This accepts that a free-tier "
                           "provider may train on content — and deployed "
                           "conversations are real people. Remove it, or upgrade "
                           "the key and set LLM_ALT_TIER=paid."}
    return {"control": "llm_training_ack", "ok": True, "severity": "ok",
            "message": "no free-tier training acknowledgement in force"}


def _check_secret_strength() -> Dict[str, Any]:
    """A weak secret behaves exactly like a strong one until it is attacked.

    MEMORY_SIGNING_KEY sat on a development placeholder while the assertion
    gate, the verification trail and four red-team controls were all built on
    top of it — nothing failed, nothing logged, and every test stayed green. It
    is the only control here whose compromise silently converts "the attacker
    must forge an HMAC" into "the attacker types a string from the repo".

    BLOCKING on the signing key specifically, because a deployed environment
    running it with a guessable key can state fabricated claims to customers as
    verified fact. The rest are advisory: they are real problems, but refusing
    to boot over them would strand a running business.
    """
    from app.core.secret_health import report as _secret_report
    r = _secret_report()

    signing = next((f for f in r["secrets"]
                    if f["name"] == "MEMORY_SIGNING_KEY"), None)
    if signing and not signing["ok"]:
        return {"control": "secret_strength", "ok": False,
                "severity": "blocking",
                "message": f"MEMORY_SIGNING_KEY is unusable "
                           f"({'; '.join(signing['problems'])}). It authenticates "
                           f"every human verification; with a guessable key the "
                           f"assertion gate can be forged and the system will "
                           f"state invented claims to customers as verified fact."}

    weak = [n for n in r["weak"] if n != "MEMORY_SIGNING_KEY"]
    if weak or r["shared_values"]:
        parts = []
        if weak:
            parts.append("weak or unset: " + ", ".join(weak))
        parts.extend(r["shared_values"])
        return {"control": "secret_strength", "ok": False, "severity": "advisory",
                "message": "; ".join(parts)}

    fps = ", ".join(f"{f['name'].split('_')[0].lower()}:{f['fingerprint']}"
                    for f in r["secrets"] if f["fingerprint"])
    return {"control": "secret_strength", "ok": True, "severity": "ok",
            "message": f"guarded secrets configured and distinct ({fps})"}


def _check_configuration_integrity() -> Dict[str, Any]:
    """Configuration that changes CORRECTNESS, validated at startup.

    Three settings can each produce a silent, customer-visible failure while
    every other signal reports health. They were previously documented as
    deployment notes, which is the weakest possible control: a note cannot fail
    a deploy.

      WEB_CONCURRENCY      Leader election fails CLOSED under multiple workers
                           and OPEN under one. Unset while running several
                           workers means every worker assumes leadership on a
                           database blip — four schedulers, four IMAP pollers,
                           duplicate dunning email. MEASURED: 4 workers, DB
                           unreachable, 4 leaders.

      HA_LEADER_ELECTION   Set to 0, every worker runs the singletons
                           unconditionally. MEASURED: 4 workers, 4 leaders.

      MEMORY_ANN_EF_SEARCH hnsw.ef_search is per SESSION. At the pgvector
                           default of 40 this corpus returned 31.7% recall on
                           the CUSTOMER channel — degraded retrieval presented
                           as a normal answer. MEASURED: 100% at 100.

    Reported, not fatal, for the same reason the rest of this module reports:
    refusing to start a laptop over a deployment variable gets the check
    deleted. In a DEPLOYED environment the first two are blocking, because
    duplicate outbound email cannot be undone.
    """
    problems, blocking = [], False
    workers = 0
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        try:
            workers = max(workers, int(os.getenv(var, "") or 0))
        except ValueError:
            pass

    if not _flag("HA_LEADER_ELECTION", "1"):
        problems.append("HA_LEADER_ELECTION=0 — every worker runs the "
                        "scheduler, IMAP poller and agent-bus consumer")
        blocking = workers > 1

    if workers > 1 and not (os.getenv("WEB_CONCURRENCY") or "").strip():
        problems.append(f"{workers} workers configured but WEB_CONCURRENCY is "
                        f"unset — leader election cannot tell it is running "
                        f"multi-process and will fail OPEN on a database blip")
        blocking = True

    ef = (os.getenv("MEMORY_ANN_EF_SEARCH", "") or "").strip()
    if ef and int(ef or 0) < 100:
        problems.append(f"MEMORY_ANN_EF_SEARCH={ef} — below 100 this corpus "
                        f"measured 31.7% recall on the customer channel")

    if problems:
        return {"control": "configuration_integrity", "ok": False,
                "severity": "blocking" if blocking else "advisory",
                "message": "; ".join(problems)}
    return {"control": "configuration_integrity", "ok": True, "severity": "ok",
            "message": f"correctness-affecting configuration validated "
                       f"(workers={workers or 1}, leader_election=on)"}


def _check_public_url() -> Dict[str, Any]:
    """APP_URL is the host printed into every link we email a human.

    THE DEFECT THIS EXISTS TO CATCH. governance builds one-click approve/reject
    links as `os.getenv("APP_URL","") or "http://localhost:8000"`. That default
    is right on a laptop and silently wrong once deployed, and nothing said so.
    Measured on the info@ BCC archive: 19 of 19 approval emails carried
    `http://localhost:8000` decision links — including the ones production sent
    to the CFO and CRO. Every one-click approval ever emailed was unusable by
    anyone not running the local server, against the local database, holding
    the local signing secret. The buttons rendered, the mail sent, the recipient
    clicked, and got "Link not valid". Nothing in the system was wrong enough to
    report itself.

    A control whose failure looks exactly like success needs a startup check,
    not a comment. Advisory, not blocking: an unreachable link is a broken
    workflow, not an unsafe one, and refusing to boot over it would be worse.
    """
    url = (os.getenv("APP_URL") or "").strip()
    local = any(h in url.lower() for h in ("localhost", "127.0.0.1", "0.0.0.0"))
    if not is_deployed():
        return {"control": "public_url", "ok": True, "severity": "ok",
                "message": f"APP_URL={url or '(unset → localhost)'} — local"}
    if not url:
        return {"control": "public_url", "ok": False, "severity": "advisory",
                "message": "APP_URL is UNSET in a deployed environment — every "
                           "emailed approval/verification link will point at "
                           "http://localhost:8000 and fail for the recipient"}
    if local:
        return {"control": "public_url", "ok": False, "severity": "advisory",
                "message": f"APP_URL={url} in a deployed environment — emailed "
                           f"links point at the recipient's own machine"}
    return {"control": "public_url", "ok": True, "severity": "ok",
            "message": f"emailed links resolve to {url}"}


CHECKS = (_check_calendar_feed, _check_api_auth, _check_admin_token,
          _check_training_ack, _check_secret_strength,
          _check_configuration_integrity, _check_public_url)


def audit() -> Dict[str, Any]:
    """Run every check. Safe to call anywhere — it never raises."""
    results: List[Dict[str, Any]] = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:              # a broken check must not mask others
            results.append({"control": getattr(check, "__name__", "?"),
                            "ok": False, "severity": "advisory",
                            "message": f"check failed: {exc}"})
    blocking = [r for r in results if r["severity"] == "blocking" and not r["ok"]]
    advisory = [r for r in results if r["severity"] == "advisory" and not r["ok"]]
    return {"deployed": is_deployed(), "checks": results,
            "blocking": blocking, "advisory": advisory,
            "safe_to_start": not (blocking and is_deployed())}


def enforce() -> Dict[str, Any]:
    """Called at startup. Refuses to start a DEPLOYED environment that is
    missing a blocking control; logs everything either way.

    On a laptop this only ever prints, so local development keeps working
    exactly as before — production safety must not be bought with developer
    friction, or it gets disabled."""
    report = audit()
    where = "DEPLOYED" if report["deployed"] else "local"

    for r in report["checks"]:
        if r["ok"] and r["severity"] == "ok":
            logger.info(f"[release-guard] {r['control']}: {r['message']}")
        elif r["severity"] == "blocking" and not r["ok"]:
            logger.critical(f"[release-guard] BLOCKING · {r['control']}: "
                            f"{r['message']}")
        else:
            logger.warning(f"[release-guard] {r['control']}: {r['message']}")

    if report["blocking"] and report["deployed"]:
        detail = "; ".join(f"{r['control']}: {r['message']}"
                           for r in report["blocking"])
        raise UnsafeConfiguration(
            f"Refusing to start a deployed environment with an unsafe "
            f"configuration — {detail}")

    if report["blocking"]:
        logger.warning(
            f"[release-guard] {len(report['blocking'])} blocking issue(s) "
            f"tolerated because this is a {where} environment. They WILL stop "
            f"a deployed start.")
    return report
