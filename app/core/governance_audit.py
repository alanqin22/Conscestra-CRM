"""Hourly production audit — a declaration that says PENDING may not describe a
file production actually holds.

WHY THIS EXISTS. `OUT_OF_BAND_SQL` in `app.core.deploy_state` is this system's
statement of what each SQL artifact did and whether production has it. Every
integrity check downstream reads it as the truth about production, so a
declaration that has drifted from the database is worse than no declaration: it
is a confident wrong answer.

The drift is real and recurring, not hypothetical. On 2026-09-23 a manual audit
found two stale declarations and cross-referencing the corpus found ten, eight
of them stale for up to three weeks. On 2026-10-10 a Railway-only apply left
`append_only_schema_attestations.sql` declared PENDING while production already
held it. The governance suite can ask this question, but it is operator-run: CI
holds no production DSN, so nothing asked it on a schedule and the answer was
only ever as fresh as the last person who thought to look.

ONE POLICY, NOT A COPY. The marker, the closed backlog and the predicate are
imported from `app.core.deploy_state`. They were relocated there precisely so
this module and the governance detector evaluate the SAME objects in the SAME
process. Two copies of a closed list is how the copy nobody watches goes stale,
which is the failure this audit exists to detect.

READ-ONLY, AND NOT THROUGH THE POOL. Production attestation evidence is read on
a separate direct connection as `crm_readonly`, with the session set read-only.
The pooled application connection is `crm_app`, which can write; an audit has no
business holding a connection that could write to what it inspects.

A FAILED AUDIT IS NOT A CLEAN AUDIT. Every connection, permission, query and
parsing failure raises. `job_ledger.instrument` wraps this job, so a raise
records an `error` row in `scheduled_job_runs` and re-raises into APScheduler's
error listener, which `/health` reports and `scripts/health_watch.py` reads
externally. Reporting "no findings" because the audit could not run is the exact
failure mode this module exists to remove.

NO NEW LEDGER AND NO NEW VOCABULARY. A finding is a `governance_alerts` row
opened through `governance_alerts.open_alert`, which already persists the
obligation before it notifies anyone, already folds a re-detection into the live
alert instead of creating a twin, and already records the notification's
provider outcome in the staff-email ledger. This module reads those records; it
does not keep its own.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Set

from app.core.deploy_state import (OUT_OF_BAND_SQL, PENDING_MARKER,
                                   RECONCILIATION_BACKLOG, declared_pending)

logger = logging.getLogger("governance_audit")

# ── Vocabulary, all of it pre-existing ──────────────────────────────────────
#
# `verification_failed` is a declared member of the `governance_alerts`
# alert_class CHECK. A new class would need a migration, and a migration to
# admit an audit's own findings would make the audit the reason the constraint
# moved -- so the audit uses the class that already means "a check that was
# supposed to hold did not".
ALERT_CLASS = "verification_failed"

# The RULE carries the finding kind, and the DEDUPE KEY carries the artifact.
# `open_alert` resolves the accountable authority and the SLA from the rule and
# folds a re-detection on the key, so one key per artifact is what makes two
# concurrently stale files two obligations rather than one that mentions both.
RULE = "declaration_drift"

# Emitted by this module, NOT by `open_alert` -- that function hard-codes
# `governance.alert_opened` for every alert class, and promoting that type to
# critical would promote every governance alert with it. A type of our own is
# what lets exactly this audit be classified critical in
# `notification_triage.CRITICAL_TYPES`.
EVENT_TYPE = "governance.declaration_drift"

SEVERITY = "high"
SOURCE = "governance-audit"

# ── Outcomes ────────────────────────────────────────────────────────────────
#
# Enumerated rather than left to strings at the call sites, because the whole
# point of this module is that "nothing to report" and "could not tell" are
# different answers. A collapse of the two is what let a stale declaration sit
# for three weeks.
#
#   clean              the audit ran against production and found nothing
#   finding            drift found, recorded as a governed obligation
#   not_configured     no audit DSN; the audit did NOT run. Not a clean result
#   source_absent      connected, but production has no attestation table
#
# Everything else raises: see `run`.
OUTCOME_CLEAN = "clean"
OUTCOME_FINDING = "finding"
OUTCOME_NOT_CONFIGURED = "not_configured"
OUTCOME_SOURCE_ABSENT = "source_absent"

# Notification outcomes, read from the staff-email ledger rather than guessed.
NOTIFY_DELIVERED = "delivered"
NOTIFY_SUPPRESSED = "suppressed"
NOTIFY_FAILED = "delivery_failed"
NOTIFY_NOT_ATTEMPTED = "not_attempted"
NOTIFY_UNKNOWN = "unknown"


class AuditError(RuntimeError):
    """The audit could not be completed. Never reported as a clean result."""


class AuditPolicyError(AuditError):
    """A fail-closed policy guard refused to let the audit proceed.

    Raised when the imported policy is degenerate -- an empty manifest, or an
    empty pending set. Both would make every comparison below trivially empty,
    and the audit would report `clean` for the one reason that guarantees it
    checked nothing.
    """


class AuditSourceError(AuditError):
    """Production evidence could not be read: connection, permission, query or
    parsing. Distinct from `source_absent`, which is an answer about the
    database rather than a failure to ask it."""


class FindingWriteError(AuditError):
    """Drift was detected and the obligation could NOT be recorded.

    Deliberately an error rather than a logged warning: an unrecorded finding
    is indistinguishable from a clean audit to every later reader, and this
    module's whole contract is that those two are never confused.
    """


# ── Where production evidence is read from ──────────────────────────────────

def _dsn() -> Optional[str]:
    """The read-only production DSN, or None.

    GOV_AUDIT_DB_URL first, so the audit can be pointed at a `crm_readonly`
    principal independently of anything else in the environment. There is NO
    fallback to the pooled application connection: an audit that quietly
    answers about its own database reports coverage it does not have.
    """
    for var in ("GOV_AUDIT_DB_URL", "RAILWAY_DB_URL"):
        dsn = (os.getenv(var) or "").strip()
        if dsn:
            return dsn
    return None


def source_connection(dsn: str):
    """A read-only connection to the audited database.

    `set_session(readonly=True)` is belt to the `crm_readonly` grant's braces:
    the grant is the control, and this is the statement of intent that makes a
    mistakenly privileged DSN fail on its first write rather than succeed.

    NOT GUARDED HERE. A configured endpoint that cannot be reached is a failure
    of the audit, and the caller converts it; swallowing it would turn an audit
    that never ran into one that found nothing.
    """
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True)
    return conn


def attested_applied(conn) -> Set[str]:
    """Artifacts `apply_sql` recorded against the audited database.

    `detail` holds the bare filename for an apply_sql row; a migrate row holds
    a summary sentence instead, which the `.sql` suffix filters out.

    This is a QUERY, not policy. The governance detector has its own copy of
    this four-line read and that is deliberate: the shared objects are the
    marker, the backlog and the predicate, which are what a divergence would
    actually corrupt. A second SELECT cannot drift into a different RULE.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT detail::text FROM public.schema_attestations "
            "WHERE source = 'apply_sql' AND detail IS NOT NULL")
        return {r[0] for r in cur.fetchall() if r[0] and r[0].endswith(".sql")}


def source_has_attestations(conn) -> bool:
    """Asked with `to_regclass` because it answers NULL rather than raising.

    A successful connection followed by a permission error is NOT a missing
    table, and collapsing the two would turn a broken audit into a passing one:
    `crm_readonly` losing SELECT on this table would read as "production has no
    attestation ledger", which is the most reassuring possible rendering of a
    control that has stopped working.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_attestations')")
        return cur.fetchone()[0] is not None


# ── Fail-closed policy guards ───────────────────────────────────────────────

def assert_policy_usable() -> Dict[str, int]:
    """Refuse to audit against a degenerate policy. Three guards.

    Each names the rule it enforces, because a fail-closed guard that reports
    only "refused" cannot be acted on.
    """
    manifest = len(OUT_OF_BAND_SQL)
    if manifest == 0:
        raise AuditPolicyError(
            "GUARD 1 (non-empty manifest): OUT_OF_BAND_SQL is empty, so every "
            "comparison below would be trivially empty and the audit would "
            "report clean for the one reason that guarantees it checked "
            "nothing. An empty manifest is a broken import, not a corpus with "
            "no out-of-band artifacts.")

    pending = declared_pending()
    if not pending:
        raise AuditPolicyError(
            f"GUARD 2 (non-empty pending set): no declaration in {manifest} "
            f"manifest entries opens with {PENDING_MARKER!r}. Under the current "
            f"contract that is a broken predicate, not a fully reconciled "
            f"corpus -- the reconciliation backlog still has "
            f"{len(RECONCILIATION_BACKLOG)} members. If the corpus is ever "
            f"genuinely all-reconciled, retire this guard deliberately rather "
            f"than letting a silent empty set satisfy it.")

    if not RECONCILIATION_BACKLOG:
        raise AuditPolicyError(
            "GUARD 3 (non-empty closed backlog): RECONCILIATION_BACKLOG is "
            "empty. The list only shrinks, so an empty one means either the "
            "backlog was genuinely cleared -- in which case delete it and this "
            "guard together -- or the import is broken and every permitted "
            "overlap is about to be reported as a new finding.")

    return {"manifest": manifest, "pending": len(pending),
            "backlog": len(RECONCILIATION_BACKLOG)}


def stale_policy_entries() -> List[str]:
    """Backlog members that name an artifact the manifest no longer has.

    Reported, never silently permitted. A backlog entry is an exception to a
    control; an exception naming a file that no longer exists is an exception
    nobody can evaluate, and leaving it in place is how a closed list quietly
    becomes a permanent allowlist.
    """
    return sorted(RECONCILIATION_BACKLOG - set(OUT_OF_BAND_SQL))


# ── The comparison ──────────────────────────────────────────────────────────

def evaluate(attested: Set[str]) -> Dict[str, Any]:
    """Compare the declarations against what the audited database attests.

    Pure: no connection, no writes. The marker's POSITIONAL semantics are
    whatever `declared_pending()` applies -- this function never re-implements
    the rule, which is the only way the two readers cannot disagree.
    """
    pending = declared_pending()
    overlap = pending & attested
    unpermitted = sorted(overlap - RECONCILIATION_BACKLOG)
    permitted = sorted(overlap & RECONCILIATION_BACKLOG)
    return {
        "pending": len(pending),
        "attested": len(attested),
        "overlap": len(overlap),
        "permitted": permitted,
        "unpermitted": unpermitted,
        "stale_policy": stale_policy_entries(),
    }


def dedupe_key(artifact: str) -> str:
    """Stable and artifact-specific, so a re-detection folds into the live
    obligation instead of opening a twin every hour, while two different stale
    artifacts remain two separate obligations."""
    return f"{RULE}:{artifact}"


# ── Recording a finding ─────────────────────────────────────────────────────

def _persist(artifact: str, kind: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Open (or fold into) the governed obligation for one artifact.

    `open_alert` resolves the accountable authority from the alert policy by
    ROLE, and raises an ownership exception to the escalation role when no
    eligible executive holds it. No identity is named here: an audit that
    hard-codes who answers for it stops being correct the first time somebody
    changes jobs.
    """
    from app.core import governance_alerts as ga
    headline = (f"Declaration drift: {artifact} is declared {PENDING_MARKER} "
                f"but production attests it applied"
                if kind == "unpermitted" else
                f"Stale exception: {artifact} is in the closed reconciliation "
                f"backlog but is absent from the SQL manifest")
    return ga.open_alert(
        ALERT_CLASS, headline,
        rule=f"{RULE}:{kind}",
        severity=SEVERITY,
        source=SOURCE,
        affected_type="sql_artifact",
        affected_id=artifact,
        detail={"artifact": artifact, "finding": kind,
                "marker": PENDING_MARKER, **evidence},
        dedupe_key=dedupe_key(artifact))


def notification_outcome(alert_id: str) -> Dict[str, Any]:
    """What became of the owner's email, read from the staff-email ledger.

    `open_alert` mails the accountable authority through
    `governance_policy.email_authority`, which claims a ledger row per
    (kind, ref) and writes the provider's verdict back to it. So the outcome is
    already recorded by existing machinery and is read here rather than
    inferred: a module's own belief that it sent something is not evidence that
    anything was delivered.

    ABSENCE IS ITS OWN ANSWER. No row means no send was attempted -- either
    GOV_ROUTE_EMAIL is off, or the owner has no address on file. That is
    distinct from a send that was attempted and refused.
    """
    try:
        from app.core import staff_email as se
        row = se.get(se.idempotency_key("alert_assigned", alert_id))
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("[governance_audit] could not read the send ledger for "
                       "%s: %s", alert_id[:8], str(exc).splitlines()[0][:160])
        return {"notified": NOTIFY_UNKNOWN, "why": "ledger unreadable"}
    if not row:
        return {"notified": NOTIFY_NOT_ATTEMPTED,
                "why": "no ledger row: GOV_ROUTE_EMAIL off, or no address on file"}
    state = row.get("state")
    if state == "accepted":
        return {"notified": NOTIFY_DELIVERED, "provider": row.get("provider"),
                "message_id": row.get("provider_message_id")}
    if state in ("skipped", "rejected"):
        return {"notified": NOTIFY_SUPPRESSED, "state": state,
                "why": row.get("failure_reason") or row.get("decision_reason")}
    if state == "failed":
        return {"notified": NOTIFY_FAILED, "state": state,
                "why": row.get("failure_reason")}
    return {"notified": NOTIFY_UNKNOWN, "state": state}


def _emit(outcome: str, detail: Dict[str, Any],
          alert_id: Optional[str] = None) -> Optional[str]:
    """Emit the audit's own critical-tiered event.

    Best-effort and LAST: the obligation is already committed by the time this
    runs, so a failure here costs visibility, never the finding. `events` has
    no event_type CHECK, so this type needs no migration; it is classified
    critical in `notification_triage.CRITICAL_TYPES`, which is what keeps it out
    of the ambient digest that never emails.
    """
    try:
        import json
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT emit_event(%s,'sql_artifact',NULL,%s::jsonb,"
                    "NULL,'governance')",
                    (EVENT_TYPE, json.dumps({"outcome": outcome,
                                             "alert_id": alert_id,
                                             **detail}, default=str)))
                ev = cur.fetchone()[0]
            conn.commit()
            return str(ev) if ev else None
        finally:
            conn.close()
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("[governance_audit] event emit failed (the finding is "
                       "already recorded): %s", str(exc).splitlines()[0][:160])
        return None


# ── The scheduled job ───────────────────────────────────────────────────────

def run() -> Dict[str, Any]:
    """One audit pass. Returns its outcome; raises when it could not run.

    THE RETURN/RAISE SPLIT IS THE CONTRACT. A return value is an answer about
    production. A raise means no answer was obtained -- and because
    `job_ledger.instrument` wraps this job, a raise is recorded as an `error`
    row and re-raised into APScheduler's error listener, so the failure reaches
    `/health` and the external monitor instead of being averaged into silence.
    """
    counts = assert_policy_usable()          # raises AuditPolicyError

    dsn = _dsn()
    if not dsn:
        # NOT a skip and NOT clean. The audit did not run, and says so.
        logger.warning(
            "[governance_audit] NOT CONFIGURED: neither GOV_AUDIT_DB_URL nor "
            "RAILWAY_DB_URL is set, so no production declaration audit ran. "
            "This is not a clean result.")
        return {"outcome": OUTCOME_NOT_CONFIGURED, "ran": False, **counts}

    try:
        conn = source_connection(dsn)
    except Exception as exc:                                   # noqa: BLE001
        raise AuditSourceError(
            f"the audited database is configured but could not be reached, so "
            f"no audit was performed: {str(exc).splitlines()[0][:200]}") from exc

    try:
        try:
            if not source_has_attestations(conn):
                logger.warning("[governance_audit] the audited database has no "
                               "schema_attestations table, so there is nothing "
                               "to compare against. Not a clean result.")
                return {"outcome": OUTCOME_SOURCE_ABSENT, "ran": False, **counts}
            attested = attested_applied(conn)
        except Exception as exc:                               # noqa: BLE001
            # Permission, query or parsing. Explicitly NOT folded into
            # `source_absent`: a revoked SELECT must not read as an absent
            # ledger.
            raise AuditSourceError(
                f"connected to the audited database but could not read its "
                f"attestations, so no audit was performed: "
                f"{str(exc).splitlines()[0][:200]}") from exc
    finally:
        try:
            conn.close()
        except Exception:                                      # noqa: BLE001
            pass

    report = evaluate(attested)
    findings = ([(a, "unpermitted") for a in report["unpermitted"]]
                + [(a, "stale_policy") for a in report["stale_policy"]])

    if not findings:
        logger.info("[governance_audit] clean: %d pending declarations, %d "
                    "attested artifacts, %d permitted overlaps, 0 findings",
                    report["pending"], report["attested"],
                    len(report["permitted"]))
        return {"outcome": OUTCOME_CLEAN, "ran": True, **report}

    recorded: List[Dict[str, Any]] = []
    failed: List[str] = []
    for artifact, kind in findings:
        # GUARDED, because `open_alert` is not guarded all the way down:
        # it takes its connection OUTSIDE its own try, so a database that is
        # entirely unavailable raises straight through it. Both shapes --
        # a returned `ok: False` and a raised connection error -- must land
        # in the same place, or total unavailability would escape the one
        # error that says the finding was not recorded.
        try:
            res = _persist(artifact, kind,
                           {"attested_count": report["attested"],
                            "permitted": report["permitted"]})
        except Exception as exc:                               # noqa: BLE001
            failed.append(f"{artifact}: {type(exc).__name__}: "
                          f"{str(exc).splitlines()[0][:160]}")
            continue
        if not res.get("ok"):
            failed.append(f"{artifact}: {res.get('error') or 'unknown'}")
            continue
        aid = res["alert_id"]
        recorded.append({"artifact": artifact, "finding": kind,
                         "alert_id": aid, "created": res.get("created"),
                         **notification_outcome(aid)})

    if failed:
        # The findings that DID persist are named, so the raise does not
        # discard what was recorded -- but the pass is still an error, because
        # at least one finding exists only in this log line.
        raise FindingWriteError(
            f"{len(failed)} of {len(findings)} findings could not be recorded "
            f"as obligations; they are NOT in governance_alerts and must not be "
            f"treated as reported: " + "; ".join(failed)
            + (f" (recorded: {[r['artifact'] for r in recorded]})"
               if recorded else ""))

    for r in recorded:
        _emit(OUTCOME_FINDING, {"artifact": r["artifact"],
                                "finding": r["finding"],
                                "notified": r["notified"]},
              alert_id=r["alert_id"])

    logger.error("[governance_audit] %d FINDING(S): %s",
                 len(recorded),
                 ", ".join(f"{r['artifact']} ({r['finding']}, "
                           f"alert {r['alert_id'][:8]}, {r['notified']})"
                           for r in recorded))
    return {"outcome": OUTCOME_FINDING, "ran": True,
            "findings": recorded, **report}
