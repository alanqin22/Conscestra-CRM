"""Deploy state — which migrations ran, and do all replicas agree on policy?

Two failures this codebase actually hit, neither detectable at the time:

MIGRATION ORDER. Three memory migrations (v1/v2/v3) shipped with no version
table and no ordering enforcement. v2 widened a primary key and silently
disabled the indexer; `reindex` reported "embedded: 0", which is
indistinguishable from "nothing was stale". The failure surfaced days later
through an unrelated test. Nothing recorded what had been applied.

CONFIG DIVERGENCE. Every safety parameter — the assertion floor, decay
half-lives, verify roles, the signing key — is read from per-process
environment. Two replicas can gate differently and nothing compares them. An
attacker who can set one replica's env can lower its floor; an operator who
forgets one replica creates the same effect by accident.

    applied_migrations()   what the database says has run
    check_migrations()     ordered list + what is missing
    safety_fingerprint()   hash of the parameters that decide what may be said
    attest()               record this replica's fingerprint; compare replicas

The fingerprint deliberately EXCLUDES secret values and includes only whether a
secret is present — an attestation endpoint must not become a key oracle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("deploy_state")

# Ordered. A later migration may depend on an earlier one; applying out of order
# is how the primary-key widening broke the indexer.
REQUIRED_MIGRATIONS: List[str] = [
    "metric_registry_migration.sql",
    "metric_registry.sql",
    "content_embeddings.sql",
    "memory_grounding.sql",
    "provenance_enrichment.sql",
    "customer_memories.sql",
    "customer_memories_v2.sql",
    "customer_memories_v3.sql",
    "data_sources.sql",
    "memory_invariants.sql",
    "activity_direction.sql",
    "activity_direction_revert.sql",
    "memory_audit_erasure.sql",
    "governed_mutation.sql",
    "activity_direction_enforcement.sql",
    "customer_memories_actor_key.sql",
    "shadow_paired_eval.sql",
    "memory_eval_labels.sql",
    "memory_eval_instrument.sql",
    "content_index_parent.sql",
    "theme_breadth.sql",
    "memory_observability.sql",
    "app_role.sql",
    "erasure_authorization.sql",
    "erasure_log_retention.sql",
    "executives_audit_and_touch.sql",
    # Applied to BOTH local and Railway on 2026-08-15, and declared in the same
    # change — which is the rule this list exists to enforce. Ordered: the
    # address trigger must be repaired before the backfill restores addresses,
    # or the backfill's work is undone by the next line-item edit.
    "fix_order_address_overwrite.sql",
    "backfill_contact_shipping_addresses.sql",
    "order_lifecycle_notifications.sql",
    "order_cancellation_voice.sql",
    # Must follow order_cancellation_voice.sql: it warns (not fails) when
    # 'order.cancelled' is not yet a registered event type, and the ordering
    # here is what stops that warning being the normal case on a fresh database.
    "order_status_self_service.sql",
    # Extends the file above. A separate migration rather than an edit to it:
    # that one is already recorded with a checksum everywhere, and migrate.py
    # reports a changed file as drifted instead of re-running it.
    "order_cancel_reason.sql",
    # verify_order_test_contacts.sql is DELIBERATELY NOT DECLARED, and this is
    # not the same reason as tier1 below. It is not a schema requirement at all
    # — it flips is_email_verified on a handful of contacts so live sends can be
    # exercised. Declaring it would assert that every database MUST have those
    # people emailable, which is false for a fresh environment and false for
    # production. It is also not portable: it names contact_ids, and on Railway
    # four of the five do not exist (see the file's own header).
    # tier1_audit_instrumentation.sql is DELIBERATELY NOT DECLARED. The file
    # exists and is validated, but applying it has not been authorized. This
    # list means "the schema must have this", so declaring an unapplied,
    # unauthorized migration turns `migrate --check` red for a decision nobody
    # has taken — it states a proposal as a requirement. Add the line in the
    # same change that applies the migration, not before.
]

# The parameters that decide what an agent may SAY. A difference in any of these
# between two replicas is a policy difference, not a config nuance.
SAFETY_PARAMS: List[str] = [
    "MEMORY_ASSERT_FLOOR",
    "MEMORY_VERIFY_ROLES",
    "MEMORY_DUAL_APPROVALS",
    "MEMORY_HALF_LIFE_DAYS",
    "MEMORY_HL_STABLE",
    "MEMORY_HL_VOLATILE",
    "MEMORY_DORMANT_BELOW",
    "MEMORY_CLUSTER_SIM",
    "MEMORY_MAX_RECORDS",
    "CONTENT_INDEX_MIN_SIM",
    "PROVENANCE_TRUST_FLOOR",
    "EMBED_MODEL",
    "EMBED_DIMS",
    "METRICS_TZ",
]

# Presence-only: a fingerprint that embedded the key would leak it to anyone who
# can read the attestation.
SAFETY_SECRETS: List[str] = ["MEMORY_SIGNING_KEY"]


def ensure_table() -> bool:
    """Ensure the two deploy-state tables are USABLE. Returns True when they are.

    The original version conflated two outcomes that need opposite responses:

      * the tables exist and this role may not CREATE  -> perfectly fine
      * the tables are genuinely missing               -> a deployment fault

    Under the privilege separation `crm_app` has USAGE but not CREATE, and
    PostgreSQL checks CREATE permission BEFORE the IF NOT EXISTS short-circuit —
    so the statement fails with 'permission denied for schema public' even when
    the table is right there. The old code logged that at warning and returned
    False, which read as 'no deploy state available'. replica_attestations then
    silently recorded nothing from 2026-08-03 until it was noticed on 08-05.

    Returning False when the tables are present and writable is the bug. The
    inability to CREATE something that already exists is not a failure."""
    missing = _missing_objects()
    if not missing:
        return True                       # present and usable; CREATE not needed

    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS public.schema_migrations (
                        filename    text PRIMARY KEY,
                        applied_at  timestamptz NOT NULL DEFAULT now(),
                        applied_by  text,
                        checksum    text
                    );
                    CREATE TABLE IF NOT EXISTS public.replica_attestations (
                        replica       text PRIMARY KEY,
                        fingerprint   text NOT NULL,
                        params        jsonb NOT NULL DEFAULT '{}'::jsonb,
                        attested_at   timestamptz NOT NULL DEFAULT now()
                    );""")
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        # Genuinely absent AND uncreatable. This is a real deployment fault:
        # apply the migration. Named explicitly so the fix is obvious.
        logger.error(f"[deploy] MISSING and uncreatable by this role: "
                     f"{', '.join(missing)} — apply the migration that declares "
                     f"them. ({str(exc).splitlines()[0][:100]})")
        return False


def ledger_health() -> Dict[str, Any]:
    """Does the ledger account for every DECLARED migration?

    CORRECTED 2026-08-06. The first version compared the ledger against every
    *.sql file in sql/ — 196 of them — and reported 12.8% coverage and
    reliable=False. That was a false alarm from the wrong denominator: most of
    sql/ is stored procedures, seeds and one-off fixes, never meant to be
    tracked. The ledger tracks REQUIRED_MIGRATIONS, and both databases hold all
    25 of them.

    Two lessons, kept because they were expensive. A coverage metric is only as
    good as the set it divides by — a wrong denominator produces a confident
    number pointing at nothing. And I wrote that check while hunting misleading
    signals and made one, so this now names its own denominator in the output.

    What WAS real: migrations applied by hand in pgAdmin never call
    record_migration(), so the ledger can miss rows for migrations that ARE
    applied. That is a process gap, not a coverage gap, and the compensating
    control is the live schema comparison in scripts/postdeploy_verify.py."""
    recorded: set = set()
    files = set(REQUIRED_MIGRATIONS)
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT filename FROM public.schema_migrations")
                recorded = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception as exc:                                    # noqa: BLE001
        logger.warning(f"[deploy] ledger unreadable: {exc}")
        return {"readable": False, "reliable": False, "error": str(exc)[:120]}

    unrecorded = sorted(files - recorded)
    coverage = (len(recorded & files) / len(files)) if files else 0.0
    return {
        "readable": True,
        "denominator": "REQUIRED_MIGRATIONS (declared), not every file in sql/",
        "declared_migrations": len(files),
        "recorded_rows": len(recorded),
        "coverage": round(coverage, 3),
        "unrecorded": unrecorded,
        # Extra rows are fine and expected: a migration applied by hand and then
        # recorded appears here without being in the declared list.
        "reliable": not unrecorded,
        "authoritative_alternative": "compare live schemas — "
                                     "scripts/postdeploy_verify.py",
    }


def _missing_objects() -> List[str]:
    """Which of this module's tables are absent. Cheap catalog lookup."""
    out: List[str] = []
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                for t in ("schema_migrations", "replica_attestations"):
                    cur.execute("SELECT to_regclass(%s)", (f"public.{t}",))
                    if cur.fetchone()[0] is None:
                        out.append(t)
        finally:
            conn.close()
    except Exception as exc:                                    # noqa: BLE001
        logger.warning(f"[deploy] could not check state tables: {exc}")
    return out


def record_migration(filename: str, applied_by: str = "manual",
                     checksum: str = "") -> bool:
    ensure_table()
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO schema_migrations (filename, applied_by, checksum)
                       VALUES (%s,%s,%s) ON CONFLICT (filename) DO NOTHING""",
                    (filename, applied_by, checksum))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[deploy] could not record migration: {exc}")
        return False


def applied_migrations() -> List[str]:
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT filename FROM schema_migrations ORDER BY applied_at")
                return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


def check_migrations() -> Dict[str, Any]:
    """Ordered status. `out_of_order` matters as much as `missing`: applying a
    later migration first is what silently disabled the indexer."""
    ensure_table()
    applied = applied_migrations()
    applied_set = set(applied)
    missing = [m for m in REQUIRED_MIGRATIONS if m not in applied_set]

    positions = {m: i for i, m in enumerate(REQUIRED_MIGRATIONS)}
    seen = [positions[m] for m in applied if m in positions]
    out_of_order = any(b < a for a, b in zip(seen, seen[1:]))

    return {"ok": not missing and not out_of_order,
            "required": REQUIRED_MIGRATIONS,
            "applied": applied,
            "missing": missing,
            "out_of_order": out_of_order,
            "note": ("apply the missing files in the order listed"
                     if missing else "schema is current")}


def safety_fingerprint() -> Dict[str, Any]:
    """Hash of the parameters that decide what may be asserted."""
    params = {k: os.getenv(k, "") for k in SAFETY_PARAMS}
    # Secrets contribute PRESENCE only — never their value.
    for k in SAFETY_SECRETS:
        params[f"{k}__set"] = "1" if os.getenv(k, "").strip() else "0"
    blob = json.dumps(params, sort_keys=True)
    return {"fingerprint": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16],
            "params": params}


def attest(replica: Optional[str] = None) -> Dict[str, Any]:
    """Record this process's safety fingerprint so replicas can be compared."""
    ensure_table()
    fp = safety_fingerprint()
    name = replica or f"{socket.gethostname()}:{os.getpid()}"
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO replica_attestations (replica, fingerprint, params)
                       VALUES (%s,%s,%s::jsonb)
                       ON CONFLICT (replica) DO UPDATE SET
                         fingerprint=EXCLUDED.fingerprint,
                         params=EXCLUDED.params, attested_at=now()""",
                    (name, fp["fingerprint"], json.dumps(fp["params"])))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[deploy] attestation failed: {exc}")
    return {"replica": name, **fp}


def consensus(max_age_minutes: int = 60) -> Dict[str, Any]:
    """Do all recently-seen replicas agree on safety policy?

    Divergence is reported with the SPECIFIC parameters that differ, because
    "replicas disagree" is not actionable and "replica B has
    MEMORY_ASSERT_FLOOR=0.1" is."""
    ensure_table()
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT replica, fingerprint, params, attested_at
                         FROM replica_attestations
                        WHERE attested_at > now() - (%s || ' minutes')::interval
                        ORDER BY attested_at DESC""", (str(max_age_minutes),))
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    if not rows:
        return {"ok": True, "replicas": 0, "note": "no recent attestations"}

    fingerprints = {r[1] for r in rows}
    diverging: Dict[str, List[Any]] = {}
    if len(fingerprints) > 1:
        keys = set().union(*[set(r[2].keys()) for r in rows])
        for k in sorted(keys):
            vals = {json.dumps(r[2].get(k)) for r in rows}
            if len(vals) > 1:
                diverging[k] = sorted(vals)

    return {"ok": len(fingerprints) == 1,
            "replicas": len(rows),
            "fingerprints": sorted(fingerprints),
            "diverging_params": diverging,
            "detail": [{"replica": r[0], "fingerprint": r[1],
                        "attested_at": r[3].isoformat()} for r in rows]}


router = APIRouter(tags=["deploy-state"])


@router.get("/deploy/migrations")
def deploy_migrations():
    return check_migrations()


@router.get("/deploy/safety-fingerprint")
def deploy_fingerprint():
    return attest()


@router.get("/deploy/consensus")
def deploy_consensus(max_age_minutes: int = 60):
    """Do all live replicas apply the same safety policy?"""
    return consensus(max_age_minutes)
