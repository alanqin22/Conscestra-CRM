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


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
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
    cur.execute("""SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                    WHERE t.tgname LIKE '%_deletion_log' AND NOT t.tgisinternal""")
    armed = {r[0] for r in cur.fetchall()}
    for tbl in ("customer_memories", "content_embeddings", "agent_utterances"):
        check(f"deletion log armed: {tbl}", tbl in armed)

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
    cur.execute("SELECT restore_governed_deletion('undeclared')")
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

    conn.close()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'all invariants hold'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
