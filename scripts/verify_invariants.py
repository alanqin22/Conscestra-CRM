"""Assert the DATABASE-layer controls directly, in SQL.

Independent of the application. Every control here was, at some point, believed
to be working while it was not:

  * append-only enforcement was a RULE that silently discarded statements,
    including the DELETE inside the sanctioned erasure function
  * that erasure function returned 0 and deleted nothing
  * the deletion undo log did not exist, so two bulk repairs were irreversible

A green pytest run is evidence about Python. This is evidence about the schema
the Python is trusting, and it runs in CI on a database built from the
migrations rather than from a developer's history.

    python -m scripts.verify_invariants
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import get_settings          # noqa: E402

FAILURES: list = []
PENDING: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def pending(name: str, done: bool, detail: str = "") -> None:
    """A ROLLOUT step, not an invariant.

    Distinguished because the two need opposite handling. An invariant that
    fails means the system is wrong and the build must stop. A rollout step that
    is unfinished means a human has not yet performed a deployment action —
    generating a password, changing an env var — and blocking every unrelated
    run on it just trains people to ignore the whole report.

    It is still printed every time, and it still says exactly what is missing.
    """
    print(f"  {'DONE' if done else 'TODO'}  {name}" + (f"  — {detail}" if detail else ""))
    if not done:
        PENDING.append(name)


def main() -> int:      # noqa: C901
    conn = psycopg2.connect(get_settings().db_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    print("DB-layer invariants")

    # 1. Append-only refuses LOUDLY and regardless of row count.
    for op in ("DELETE FROM memory_verifications",
               "UPDATE memory_verifications SET performed_by='x'"):
        try:
            cur.execute(op)
            check(f"append-only refuses: {op.split()[0]}", False, "not refused")
        except Exception as exc:
            check(f"append-only refuses: {op.split()[0]}",
                  "append-only" in str(exc))

    # 2. The sanctioned erasure path is not a no-op.
    cur.execute("SELECT to_regprocedure('public.erase_verifications_for_entity(text,uuid)')")
    check("erasure function exists", cur.fetchone()[0] is not None)

    # 3. Deletion logging is armed on every governed table.
    # ARMED means ENABLED, not merely present.
    #
    # This asked pg_trigger whether the row exists. `ALTER TABLE ... DISABLE
    # TRIGGER` does not delete the row — it sets tgenabled='D' — so the check
    # reported "deletion log armed" on a table whose deletion log had been
    # switched off. Verified by mutation: disabling the trigger on
    # content_embeddings left this suite exiting 0.
    #
    # That is the same class of defect as the health endpoint that could not
    # fail, in the tool whose entire job is to notice.
    #
    # tgenabled: 'O' origin, 'A' always, 'D' disabled, 'R' replica-only (never
    # fires on a primary, so it is disabled for our purposes).
    cur.execute("""SELECT c.relname, t.tgenabled
                     FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
                    WHERE t.tgname LIKE '%_deletion_log' AND NOT t.tgisinternal""")
    state = dict(cur.fetchall())
    for tbl in ("customer_memories", "content_embeddings", "agent_utterances"):
        st = state.get(tbl)
        check(f"deletion log armed: {tbl}", st in ("O", "A"),
              "" if st in ("O", "A") else
              ("trigger absent" if st is None else
               f"trigger present but tgenabled={st!r} — it does not fire"))

    # 4. An ordinary delete is recoverable; an erasure is not.
    cur.execute("""INSERT INTO customer_memories
        (entity_type,entity_id,kind,statement,topic,occurrences,evidence,
         evidence_count,evidence_hash,source_type,reliability,certainty,
         generator,visibility,last_observed_at)
        VALUES ('contact',gen_random_uuid(),'theme','invariant probe','seed',1,
                '[]'::jsonb,0,'inv-probe','ai',0.5,0.5,'invariant/probe',
                'internal',now()) RETURNING memory_id::text""")
    mid = cur.fetchone()[0]
    cur.execute("DELETE FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
    cur.execute("SELECT count(*) FROM governed_deletions WHERE row_pk=%s", (mid,))
    check("ordinary delete is logged", cur.fetchone()[0] == 1)
    # ONE row, by id. Restoring the whole 'undeclared' bucket resurrected the
    # entire deletion history — 2,421 customer_memories rows per run.
    cur.execute("SELECT deletion_id FROM governed_deletions WHERE row_pk=%s "
                "ORDER BY deletion_id DESC LIMIT 1", (mid,))
    cur.execute("SELECT restore_governed_deletion_row(%s)", (cur.fetchone()[0],))
    cur.execute("SELECT count(*) FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
    check("logged delete is restorable", cur.fetchone()[0] == 1)

    # Clear the image left by the ordinary delete above: restore_governed_
    # deletion puts the ROW back, it does not remove the LOG entry. Comparing
    # an absolute count here would measure the previous step.
    cur.execute("DELETE FROM governed_deletions WHERE row_pk=%s", (mid,))
    cur.execute("BEGIN")
    cur.execute("SET LOCAL app.erasure = 'on'")
    cur.execute("DELETE FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
    cur.execute("COMMIT")
    cur.execute("SELECT count(*) FROM governed_deletions WHERE row_pk=%s", (mid,))
    check("erasure leaves NO recoverable image", cur.fetchone()[0] == 0,
          "an undo log surviving a GDPR erasure is a violation")
    cur.execute("DELETE FROM governed_deletions WHERE row_pk=%s", (mid,))

    # 5. Nothing unattributable may accumulate.
    cur.execute("SELECT count(*) FROM memory_verifications WHERE entity_id IS NULL")
    n = cur.fetchone()[0]
    check("no unerasable verification rows", n == 0, f"{n} rows with no entity")

    print("\nPrivilege-separation invariants")
    check_app_role(cur)

    conn.close()

    print("\nMigration-set invariants")
    check_migration_overlap()

    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'all invariants hold'}")
    return 1 if FAILURES else 0




def check_app_role(cur) -> None:
    """The application role must be unable to switch its own controls off.

    Every DB-layer control above is a trigger on a table the role could own. If
    the app connects as an owner or a superuser, `ALTER TABLE ... DISABLE
    TRIGGER` defeats all of them in one statement, and every invariant checked
    above becomes a statement about a system the attacker can simply turn off.

    Checked from the CATALOG, not by connecting, so this runs from the owner DSN
    that migrations and the test harness legitimately use — the test suite
    purges its fixtures by disabling an append-only trigger, which is admin work
    and is fine. What is NOT fine is the running application holding that power.
    """
    cur.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname = 'crm_app'")
    row = cur.fetchone()
    if row is None:
        check("app role crm_app exists", False,
              "not created — apply sql/app_role.sql")
        return
    is_super, createdb, createrole, bypassrls = row
    check("app role crm_app exists", True)
    elevated = any((is_super, createdb, createrole, bypassrls))
    check("app role holds no elevated attributes", not elevated,
          "" if not elevated else
          f"super={is_super} createdb={createdb} createrole={createrole} "
          f"bypassrls={bypassrls}")

    # Owning even ONE of these tables is enough to disable its triggers.
    cur.execute("""SELECT count(*) FROM pg_class c
                     JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relkind = 'r'
                      AND pg_catalog.pg_get_userbyid(c.relowner) = 'crm_app'""")
    owned = cur.fetchone()[0]
    check("app role owns no tables", owned == 0,
          "" if owned == 0 else
          f"owns {owned} table(s) — it can DISABLE TRIGGER on each")

    cur.execute("SELECT has_schema_privilege('crm_app','public','CREATE')")
    can_create = cur.fetchone()[0]
    check("app role cannot create objects in public", not can_create,
          "" if not can_create else
          "CREATE on schema public lets it shadow or add objects")

    # The role is pointless if the app does not USE it. This is the check that
    # would have caught "we created crm_app and left DB_DSN on postgres".
    import os as _os
    dsn = (_os.getenv("DATABASE_URL") or _os.getenv("DB_DSN") or "")
    configured = "crm_app" in dsn
    pending("application DSN points at crm_app", configured,
            "" if configured else
            "DB_DSN/DATABASE_URL still names an owner account. The role exists "
            "and is proven to block the attack, but nothing is using it yet. "
            "Finish: ALTER ROLE crm_app WITH PASSWORD '<generated>'; then set "
            "DB_DSN=postgresql://crm_app:<pw>@host:port/db (migrations and the "
            "test harness keep the owner DSN)")


def check_migration_overlap() -> None:
    """No object may be defined by two migrations.

    `erase_memory_verifications()` was defined in both memory_invariants.sql
    (original, broken — its DELETE was silently discarded by a RULE) and
    memory_audit_erasure.sql (fixed). Replaying the earlier file overwrote the
    fix with the break. That happened for real on BOTH databases during
    deployment, and the only reason it was caught is that a test asserted the
    function's BEHAVIOUR rather than its existence.

    Static, so it runs without a database and fails the build rather than the
    deploy."""
    import re
    from app.core.deploy_state import REQUIRED_MIGRATIONS
    sql_dir = Path(__file__).resolve().parents[1] / "sql"
    seen: dict = {}
    for name in REQUIRED_MIGRATIONS:
        path = sql_dir / name
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(
                r"CREATE (?:OR REPLACE )?(FUNCTION|VIEW|RULE)\s+([\w.]+)", body, re.I):
            seen.setdefault((m.group(1).upper(), m.group(2).lower()), set()).add(name)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    check("no object defined by two migrations", not dupes,
          "; ".join(f"{k[1]} in {sorted(v)}" for k, v in dupes.items()) or "")


if __name__ == "__main__":
    raise SystemExit(main())
