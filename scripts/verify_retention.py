"""Exercise the erasure-register retention pass — the path that has never run.

WHY
---
`anonymise_old_erasure_log` implements GDPR Art. 5(1)(e): the erasure register
must not become a permanent index of who was erased. It has been on the system
since 2026-08-03 and only its REFUSAL branch has ever executed — the 365-day
floor was verified on production, and the anonymisation itself never was,
because every row in the register is three days old.

"The guard fires" is evidence about the guard. It says nothing about whether the
work the guard protects is correct. A retention function that raises properly
and then anonymises the wrong rows, or the right rows wrongly, would pass every
test performed so far.

WHAT IT PROVES
--------------
  1. Rows past the window ARE anonymised: entity_id and memory_ids nulled.
  2. Rows inside the window are untouched — retention is not a purge.
  3. Accountability survives. Art. 5(2) needs the register to still show THAT
     an erasure happened, by whom and when; only WHO WAS ERASED goes. If
     retention destroyed performed_by or erased_at it would defeat the register
     while appearing to comply.
  4. It is idempotent — a second pass reports 0, so a daily schedule cannot
     churn.
  5. The no-rewrite trigger still refuses an "anonymisation" that also edits
     performed_by. The permitted UPDATE must be narrow or it becomes a way to
     rewrite history with a legitimate-looking shape.

EVERYTHING RUNS IN ONE TRANSACTION AND ROLLS BACK. The register is append-only
by design — DELETE is refused — so a test row inserted for real would be
permanent. A compliance register polluted by its own test is a poor advert for
the control.

    python -m scripts.verify_retention              # DB_DSN
    python -m scripts.verify_retention --target railway
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core import config as _config          # noqa: E402,F401

import psycopg2                                  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}")
    if detail:
        print(f"        {detail}")


def main() -> int:
    dsn = os.getenv("DB_DSN", "")
    if "--target" in sys.argv and sys.argv[sys.argv.index("--target") + 1] == "railway":
        dsn = os.getenv("RAILWAY_DB_URL", "")
    if not dsn:
        raise SystemExit("no DSN")

    conn = psycopg2.connect(dsn)          # autocommit OFF — everything rolls back
    cur = conn.cursor()
    print("RETENTION PASS — exercised inside a transaction that rolls back\n")
    try:
        cur.execute("SELECT count(*) FROM memory_erasure_log")
        before = cur.fetchone()[0]

        # Two synthetic rows: one well past the window, one well inside it.
        cur.execute("""
            INSERT INTO memory_erasure_log
              (entity_type, entity_id, memory_ids, rows_erased, performed_by,
               declared_by, erased_at)
            VALUES ('contact', gen_random_uuid(),
                    ARRAY[gen_random_uuid(), gen_random_uuid()], 7,
                    'retention-test', 'retention-test', now() - interval '3 years'),
                   ('contact', gen_random_uuid(),
                    ARRAY[gen_random_uuid()], 3,
                    'retention-test', 'retention-test', now() - interval '10 days')
            RETURNING erasure_id, erased_at::date""")
        rows = cur.fetchall()
        old_id, recent_id = rows[0][0], rows[1][0]
        print(f"  seeded: old row {old_id} ({rows[0][1]}), "
              f"recent row {recent_id} ({rows[1][1]})\n")

        # ── 1 & 2: the pass itself ──────────────────────────────────────────
        cur.execute("SELECT public.anonymise_old_erasure_log(730)")
        n = cur.fetchone()[0]
        check("anonymises rows past the window", n == 1,
              f"returned {n}; expected exactly the 3-year-old row")

        cur.execute("""SELECT entity_id, memory_ids, anonymised_at
                         FROM memory_erasure_log WHERE erasure_id = %s""", (old_id,))
        eid, mids, anon = cur.fetchone()
        check("subject identifiers removed from the old row",
              eid is None and mids is None and anon is not None,
              f"entity_id={eid} memory_ids={mids} anonymised_at={'set' if anon else 'NULL'}")

        cur.execute("""SELECT entity_id IS NOT NULL, memory_ids IS NOT NULL,
                              anonymised_at IS NULL
                         FROM memory_erasure_log WHERE erasure_id = %s""", (recent_id,))
        has_eid, has_mids, not_anon = cur.fetchone()
        check("row inside the window untouched",
              has_eid and has_mids and not_anon,
              "retention must narrow the register, not purge it")

        # ── 3: accountability survives (Art. 5(2)) ──────────────────────────
        cur.execute("""SELECT entity_type, rows_erased, performed_by, declared_by,
                              erased_at IS NOT NULL, txid IS NOT NULL
                         FROM memory_erasure_log WHERE erasure_id = %s""", (old_id,))
        et, re_, pb, db_, has_when, has_txid = cur.fetchone()
        check("accountability columns preserved",
              et == 'contact' and re_ == 7 and pb == 'retention-test'
              and db_ == 'retention-test' and has_when and has_txid,
              f"still records THAT an erasure happened: type={et} rows={re_} "
              f"by={pb} declared_by={db_} when={has_when} txid={has_txid}")

        # ── 4: idempotent ───────────────────────────────────────────────────
        cur.execute("SELECT public.anonymise_old_erasure_log(730)")
        n2 = cur.fetchone()[0]
        check("second pass is a no-op", n2 == 0,
              f"returned {n2}; a daily schedule must not churn already-done rows")

        # ── 5: the permitted UPDATE stays narrow ────────────────────────────
        try:
            cur.execute("""UPDATE memory_erasure_log
                              SET entity_id = NULL, memory_ids = NULL,
                                  anonymised_at = now(), performed_by = 'someone-else'
                            WHERE erasure_id = %s""", (recent_id,))
            check("trigger refuses a disguised history rewrite", False,
                  "an update that nulled identifiers AND changed performed_by "
                  "was ACCEPTED — retention is a hole in the audit trail")
        except psycopg2.errors.RaiseException as exc:
            conn.rollback()
            check("trigger refuses a disguised history rewrite", True,
                  str(exc).splitlines()[0][:96])
            # rollback undid the seed rows; re-verify the register is unchanged
        cur.execute("SELECT count(*) FROM memory_erasure_log")
        after = cur.fetchone()[0]
        check("register unchanged by this test", after == before,
              f"{before} rows before, {after} after")
        return 0 if all(r[0] == PASS for r in results) else 1
    finally:
        conn.rollback()
        conn.close()
        n_fail = sum(1 for r in results if r[0] == FAIL)
        print(f"\n  {len(results) - n_fail}/{len(results)} checks passed; "
              f"transaction rolled back")


if __name__ == "__main__":
    raise SystemExit(main())
