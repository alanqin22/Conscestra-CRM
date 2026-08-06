"""Watch /health and email when it stops being true.

WHY THIS EXISTS
---------------
Background jobs stopped for ten days in July 2026 and nobody noticed, because
nothing about that failure is visible from outside: the API answered every
request correctly the whole time. Every runbook in docs/ opens with "check
/health", and until now nothing checked it.

WHY IT DOES NOT IMPORT THE APPLICATION
--------------------------------------
`app/agents/email/smtp_imap.py` already sends mail, and reusing it would be the
DRY choice everywhere else in this codebase. Not here. An alerting path that
imports the system it monitors shares that system's failure modes — a bad
migration, a broken import, an exhausted connection pool would take down the
thing meant to tell you about it. This talks SMTP directly and imports nothing
from `app/` except the .env loader.

WHAT IT CHECKS
--------------
  transport      /health answers at all, within the timeout
  http status    200 (the endpoint returns 503 when the database is unreachable)
  database       database.ok is true
  db role        database.connected_as is the expected app role, not a superuser
  leader         exactly one process claims to run the singletons
  scheduler      scheduler.last_tick is fresher than HEALTH_MAX_TICK_AGE_MIN

The scheduler tick is the one that matters most and the one a generic uptime
service cannot see. `running: true` only means the object exists; a scheduler
that is running and never firing looks identical in every other field.

NOISE CONTROL
-------------
Emails on TRANSITION (ok -> bad, bad -> ok) and once per HEALTH_REPEAT_HOURS
while a problem persists. A checker that mails every 15 minutes trains you to
filter it, which is the same as not having one.

    python -m scripts.health_watch                    # check once, email on change
    python -m scripts.health_watch --url https://x/health
    python -m scripts.health_watch --test-email       # prove the alert path works
    python -m scripts.health_watch --force            # email regardless of state
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core import config as _config          # noqa: E402,F401  (loads .env only)

STATE = Path(os.getenv("HEALTH_STATE_FILE", str(ROOT / "backups" / "health_state.json")))
TIMEOUT = float(os.getenv("HEALTH_TIMEOUT_S", "20"))
MAX_TICK_AGE_MIN = float(os.getenv("HEALTH_MAX_TICK_AGE_MIN", "120"))
REPEAT_HOURS = float(os.getenv("HEALTH_REPEAT_HOURS", "12"))
EXPECT_ROLE = os.getenv("HEALTH_EXPECT_DB_ROLE", "crm_app").strip()


# ── the checks ───────────────────────────────────────────────────────────────

def _age_minutes(ts: Any) -> Optional[float]:
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:                                           # noqa: BLE001
        return None


def check(url: str) -> Tuple[List[str], Dict[str, Any]]:
    """Return (problems, facts). Empty problems means healthy."""
    problems: List[str] = []
    facts: Dict[str, Any] = {"url": url,
                             "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "health_watch"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            code = resp.getcode()
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        code, body = exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:                                    # noqa: BLE001
        # Unreachable is the loudest possible result and must never be silent.
        problems.append(f"UNREACHABLE: {type(exc).__name__}: {exc}")
        facts["reachable"] = False
        return problems, facts

    facts["reachable"] = True
    facts["http_status"] = code
    if code != 200:
        problems.append(f"HTTP {code} (503 means the database query failed)")

    try:
        h = json.loads(body)
    except Exception:                                           # noqa: BLE001
        problems.append("response was not JSON — is this the right URL?")
        return problems, facts

    db = h.get("database") or {}
    facts["db_ok"] = db.get("ok")
    facts["connected_as"] = db.get("connected_as")
    if db.get("ok") is False:
        problems.append(f"database not answering: {str(db.get('error'))[:120]}")
    if EXPECT_ROLE and db.get("connected_as") and db["connected_as"] != EXPECT_ROLE:
        # Not a crash — a silently weaker security posture, which is worse
        # because nothing else will report it.
        problems.append(f"app connected as '{db['connected_as']}', expected "
                        f"'{EXPECT_ROLE}' — privilege separation may be off")

    ha = h.get("ha") or {}
    facts["ha_role"] = ha.get("role")
    if ha.get("role") == "unelected":
        problems.append("leader election never completed (ha.role=unelected)")

    sched = h.get("scheduler") or {}
    facts["scheduler_running"] = sched.get("running")
    facts["scheduler_jobs"] = sched.get("jobs")
    facts["last_tick"] = sched.get("last_tick")
    age = _age_minutes(sched.get("last_tick"))
    facts["last_tick_age_min"] = round(age, 1) if age is not None else None

    if ha.get("role") in ("leader", "standalone", None):
        if sched.get("running") is False:
            problems.append("scheduler is not running on the leader")
        if age is None and sched.get("running"):
            problems.append("scheduler has never ticked (last_tick absent)")
        elif age is not None and age > MAX_TICK_AGE_MIN:
            problems.append(f"scheduler last ticked {age:.0f} min ago "
                            f"(threshold {MAX_TICK_AGE_MIN:.0f}) — background "
                            f"jobs are not running")
    elif ha.get("role") == "follower":
        # A follower not running the scheduler is correct. But if EVERY process
        # is a follower nobody runs it, and one probe cannot see the others.
        facts["note"] = ("this process is a follower; scheduler state not "
                         "assessed. Confirm some process reports leader.")
    return problems, facts


# ── alerting ─────────────────────────────────────────────────────────────────

def _send(subject: str, text: str) -> bool:
    host = os.getenv("EMAIL_SMTP_HOST", "mail.agentorc.ca")
    port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
    addr = os.getenv("EMAIL_ADDRESS", "info@agentorc.ca")
    pwd = os.getenv("EMAIL_PASSWORD", "")
    to = os.getenv("HEALTH_ALERT_TO", "").strip() or addr
    if not pwd:
        print("EMAIL_PASSWORD not set — cannot send", file=sys.stderr)
        return False
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"Conscestra Health <{addr}>"
    msg["To"] = to
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(addr, pwd)
            s.sendmail(addr, [a.strip() for a in to.split(",")], msg.as_string())
        print(f"  alert sent to {to}")
        return True
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ALERT SEND FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:                                           # noqa: BLE001
        return {}


def _save_state(d: Dict[str, Any]) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except Exception as exc:                                    # noqa: BLE001
        print(f"  could not persist state: {exc}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Alert when /health stops being true")
    ap.add_argument("--url", default=os.getenv("HEALTH_URL", "").strip())
    ap.add_argument("--force", action="store_true", help="email regardless of state")
    ap.add_argument("--test-email", action="store_true",
                    help="send a test alert and exit — proves the path works "
                         "BEFORE you need it")
    a = ap.parse_args()

    if a.test_email:
        ok = _send("Conscestra health watch — TEST",
                   "This is a test of the health alert path.\n\n"
                   "If you are reading this, alerts can reach you. That is the "
                   "only thing this proves; it says nothing about the system's "
                   "health.\n")
        return 0 if ok else 1

    url = a.url
    if not url:
        print("no URL. Pass --url or set HEALTH_URL "
              "(e.g. https://<app>.up.railway.app/health)", file=sys.stderr)
        return 2

    problems, facts = check(url)
    now = datetime.now(timezone.utc)
    healthy = not problems

    print(f"health check {url}")
    for k, v in facts.items():
        print(f"  {k:22} {v}")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("\n  healthy")

    state = _load_state()
    was_healthy = state.get("healthy")
    last_alert = _age_minutes(state.get("last_alert_at"))
    transition = (was_healthy is not None) and (was_healthy != healthy)
    stale_alert = (last_alert is None) or (last_alert > REPEAT_HOURS * 60)

    should = a.force or (not healthy and (transition or stale_alert)) or \
             (healthy and transition)
    if should:
        if healthy:
            subj = "Conscestra: RECOVERED"
            body = f"{url} is healthy again at {now.isoformat()}.\n\n"
        else:
            subj = f"Conscestra: {len(problems)} problem(s)"
            body = ("Problems detected:\n\n"
                    + "\n".join(f"  - {p}" for p in problems)
                    + "\n\nRunbooks:\n"
                      "  docs/runbook_leader_failure.md      (scheduler / leader)\n"
                      "  docs/runbook_incident_escalation.md (triage, 72h clock)\n"
                      "  docs/runbook_restore.md             (data loss)\n\n")
        body += "Facts:\n" + "\n".join(f"  {k}: {v}" for k, v in facts.items())
        if _send(subj, body):
            state["last_alert_at"] = now.isoformat()

    state["healthy"] = healthy
    state["checked_at"] = now.isoformat()
    state["problems"] = problems
    _save_state(state)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
