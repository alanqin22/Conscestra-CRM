"""Is this record real, or demonstration data?

THE DECISION THIS SERVES. `API_SECURITY_MODE=public-read` is deliberate: a
prospective client must see Conscestra CRM working without a login. Verified on
production, every CRM read endpoint answers an anonymous caller. That is safe
TODAY only because the corpus is demonstration data — and until this module,
that safety rested on an email-domain heuristic standing in for a fact.

The heuristic was never sound. `sql/seed_email_migration.sql` rewrote EVERY
address on contacts, leads and accounts — `WHERE email IS NOT NULL`, no
synthetic filter, no backup retained — so a real customer and a generated one
were handed the same kind of address by the same statement. Domain proves
nothing about provenance. Neither does `is_email_verified` (true on 172 of 182
contacts), nor `is_synthetic` (coverage runs 0%–54% depending on the table),
nor name similarity (measured 92% false-positive between contacts and leads).

So this module does not RECONSTRUCT provenance. It records what can be
established, marks the rest permanently ambiguous, and — the part that actually
protects anything — provides the tripwire that stops the demo posture from
silently surviving the arrival of real customer data.

    classify()   conservative, idempotent: writes only what evidence supports
    summary()    what the corpus looks like now
    tripwire()   does this deployment hold anything that might be a real
                 customer? Deliberately over-sensitive.

WHY A TRIPWIRE IS NOT EVIDENCE, and why the distinction matters. `classify()`
refuses to guess: a record without positive evidence stays ambiguous forever.
`tripwire()` does the opposite — it treats anything it cannot rule out as cause
for alarm. Those are contradictory standards on purpose, because they answer
different questions. "What is this record?" must never be answered by
inference. "Should someone look at the security posture?" should be answered
by the faintest signal, because a false alarm costs a config check and a miss
costs customer data served to anyone who knows the hostname.

SCOPED TO CUSTOMER SUBJECTS, and that is not a loophole. `public-read` exposes
the CRM's customer records; staff having real work addresses is expected in
every deployment and is not the exposure. Measured on this corpus: contacts,
leads and accounts carry no address outside the domains this deployment owns.
Eight sit in `owners` (staff, out of scope) and eight more in the LEGACY
`customers` table — seven of them on gmail.com. Those eight are the genuine
signal: real addresses, in a fossil table, which no agent read path reaches
(only `upsert_customer` and the admin-gated DSAR export touch it). Present but
not exposed — which is precisely the distinction a tripwire is meant to raise
and a person is meant to settle.

CONFIG (env)
  CORPUS_SYNTHETIC_DOMAINS   comma-separated; defaults below
  PUBLIC_READ_ACCEPT_REAL_DATA  1 = the operator accepts serving real customer
                                data anonymously (see release_guard)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("corpus_provenance")

SYNTHETIC = "synthetic"
REAL = "real"
AMBIGUOUS = "ambiguous"

# Subjects whose exposure the public-read posture actually risks. `owners`,
# `employees` and `executives` are staff: real addresses there are normal and
# are not what an anonymous reader of the CRM is being protected from.
CUSTOMER_SUBJECTS: Tuple[str, ...] = ("contacts", "leads", "accounts", "customers")
ALL_SUBJECTS: Tuple[str, ...] = CUSTOMER_SUBJECTS + ("owners", "employees",
                                                     "executives")

# The primary key and email column differ per table; the legacy `customers`
# table predates every convention in the schema.
_SUBJECT_META: Dict[str, Dict[str, Optional[str]]] = {
    "contacts":   {"pk": "contact_id",  "email": "email",         "synth": "is_synthetic"},
    "leads":      {"pk": "lead_id",     "email": "email",         "synth": None},
    "accounts":   {"pk": "account_id",  "email": "email",         "synth": "is_synthetic"},
    "customers":  {"pk": "customer_id", "email": "email_address", "synth": None},
    "owners":     {"pk": "owner_id",    "email": "email",         "synth": "is_synthetic"},
    "employees":  {"pk": "employee_uuid", "email": "email",       "synth": None},
    "executives": {"pk": "executive_id", "email": "email",        "synth": None},
}

# Domains this deployment OWNS and generates into. Not evidence of provenance —
# the seed migration put real and generated records alike behind seed.agentorc.ca
# — but a usable floor for the tripwire, which is asking a different question.
_DEFAULT_SYNTHETIC_DOMAINS = (
    "seed.agentorc.ca",     # the catch-all the seed migration writes into
    "example.com", "examples.com",
    "system.internal",
    "emp.agentorc.ca",      # staff subdomain
    "agentorc.ca",          # the deployment's OWN domain — a real customer
                            # cannot have an address here, so it is an internal
                            # address by construction, not a tuned exemption
)


def synthetic_domains() -> set:
    raw = os.getenv("CORPUS_SYNTHETIC_DOMAINS", "").strip()
    if raw:
        return {d.strip().lower() for d in raw.split(",") if d.strip()}
    return set(_DEFAULT_SYNTHETIC_DOMAINS)


def _rows(sql: str, args: tuple = ()) -> List[tuple]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


# ============================================================================
# CLASSIFY — conservative by construction
# ============================================================================

def classify(apply: bool = False) -> Dict[str, Any]:
    """Record the provenance that evidence supports, and nothing else.

    Writes ONLY `synthetic`, and only where the row's own `is_synthetic` column
    says so. Everything else is left absent, which the schema defines as
    ambiguous — so nothing here invents a classification, and re-running it
    cannot broaden one.

    `real` is never written automatically. There is no independent external
    trace inside this database to key on: the info@ BCC archive lives outside
    it, payments carry no provider reference, and every other candidate signal
    was ruled out above. A real record is therefore recorded by a person, with
    their name in `decided_by`, or not at all. Deriving `real` from a heuristic
    would be the exact error this module exists to stop, pointed the other way.

    Idempotent: `apply=False` reports what it would do and changes nothing.
    """
    out: Dict[str, Any] = {"ok": True, "apply": apply, "would_record": 0,
                           "recorded": 0, "by_table": {}, "skipped": []}
    conn = get_connection()
    try:
        for table in ALL_SUBJECTS:
            meta = _SUBJECT_META[table]
            if not meta["synth"]:
                out["skipped"].append(
                    f"{table}: no is_synthetic column — nothing to establish, "
                    f"stays ambiguous")
                continue
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {meta['pk']}::text FROM {table} "
                    f"WHERE {meta['synth']} IS TRUE")
                ids = [r[0] for r in cur.fetchall()]
            out["by_table"][table] = len(ids)
            out["would_record"] += len(ids)
            if not apply or not ids:
                continue
            with conn.cursor() as cur:
                for eid in ids:
                    # ON CONFLICT DO NOTHING, never DO UPDATE: a row already
                    # classified is settled, and the trigger would refuse the
                    # revision anyway. Silence here is the correct outcome.
                    cur.execute(
                        "INSERT INTO corpus_provenance "
                        "(entity_type, entity_id, state, rule, evidence, decided_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (entity_type, entity_id) DO NOTHING",
                        (table, eid, SYNTHETIC, "flagged_synthetic",
                         '{"column": "is_synthetic", "value": true}',
                         "classify:2026-08-31"))
                    out["recorded"] += cur.rowcount
            conn.commit()
    except Exception as exc:
        logger.error(f"[corpus_provenance] classify failed: {exc}", exc_info=True)
        out["ok"] = False
        out["error"] = str(exc)[:300]
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    return out


def summary() -> Dict[str, Any]:
    """Counts per subject table: classified, and how much is ambiguous.

    Ambiguous is DERIVED (total minus classified) rather than stored, because
    absence is the definition. A stored ambiguous count would be a second
    representation of the same fact, free to drift from it.
    """
    out: Dict[str, Any] = {"ok": True, "by_table": {}, "totals":
                           {SYNTHETIC: 0, REAL: 0, AMBIGUOUS: 0}}
    try:
        recorded: Dict[Tuple[str, str], int] = {}
        for et, st, n in _rows("SELECT entity_type, state, count(*) "
                               "FROM corpus_provenance GROUP BY 1,2"):
            recorded[(et, st)] = n
        for table in ALL_SUBJECTS:
            meta = _SUBJECT_META[table]
            total = _rows(f"SELECT count(*) FROM {table}")[0][0]
            syn = recorded.get((table, SYNTHETIC), 0)
            real = recorded.get((table, REAL), 0)
            amb = total - syn - real
            out["by_table"][table] = {"total": total, SYNTHETIC: syn,
                                      REAL: real, AMBIGUOUS: amb}
            out["totals"][SYNTHETIC] += syn
            out["totals"][REAL] += real
            out["totals"][AMBIGUOUS] += amb
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)[:300]
    return out


# ============================================================================
# TRIPWIRE — over-sensitive on purpose
# ============================================================================

def tripwire() -> Dict[str, Any]:
    """Does this deployment hold anything that might be a real customer?

    Two signals, either of which trips it:

      1. a customer subject CLASSIFIED real — the definitive case;
      2. a customer subject whose email sits outside the domains this
         deployment generates into — the faint one.

    Signal 2 is NOT evidence and must never be written into
    `corpus_provenance`; the schema's `rule` CHECK makes that impossible rather
    than merely discouraged. It is an alarm, and it is calibrated to be wrong
    in the safe direction: a false alarm costs somebody a look at the config, a
    miss costs real customer records served to anyone with the hostname.

    Fails TRIPPED on error. A tripwire that cannot read the corpus has not
    established that the corpus is safe, and reporting "clear" would be the
    absence of evidence dressed as evidence of absence.
    """
    out: Dict[str, Any] = {"tripped": False, "reasons": [], "ok": True,
                           "checked_domains": sorted(synthetic_domains())}
    domains = synthetic_domains()
    try:
        for table in CUSTOMER_SUBJECTS:
            meta = _SUBJECT_META[table]
            n_real = _rows(
                "SELECT count(*) FROM corpus_provenance "
                "WHERE entity_type=%s AND state=%s", (table, REAL))[0][0]
            if n_real:
                out["tripped"] = True
                out["reasons"].append(
                    f"{table}: {n_real} record(s) classified real")

            col = meta["email"]
            not_ours = _rows(
                f"SELECT count(*) FROM {table} "
                f"WHERE {col} IS NOT NULL AND {col} <> '' "
                f"AND lower(split_part({col}, '@', 2)) <> ALL(%s)",
                (list(domains),))[0][0]
            if not_ours:
                out["tripped"] = True
                out["reasons"].append(
                    f"{table}: {not_ours} address(es) outside the domains this "
                    f"deployment generates into")
    except Exception as exc:
        logger.warning(f"[corpus_provenance] tripwire could not read the "
                       f"corpus: {exc}")
        out["ok"] = False
        out["tripped"] = True          # fail tripped, never clear
        out["reasons"].append(f"could not verify the corpus: {str(exc)[:120]}")
    return out


# ============================================================================
# ROUTER
# ============================================================================

router = APIRouter(tags=["corpus-provenance"])


@router.get("/corpus-provenance/summary")
def corpus_provenance_summary():
    return {"summary": summary(), "tripwire": tripwire()}


@router.post("/corpus-provenance/classify")
def corpus_provenance_classify(body: Optional[Dict[str, Any]] = None):
    return classify(apply=bool((body or {}).get("apply")))
