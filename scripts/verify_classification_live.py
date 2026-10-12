"""Did the classification actually land, and did nothing else change?

WHY THIS EXISTS. The capability that writes the classification reports its own
row count, and a writer's report of its own success is not evidence. This asks
the database instead, as a WEAKER principal than the writer -- `crm_readonly`
cannot have produced what it is reporting on.

WHAT IT DELIBERATELY DOES NOT DO. It does not re-derive the census. The census
predicate is new code with exactly one definition
(`classification_proposition._CENSUS_SQL`), and a verifier that called it would
be checking the writer's own implementation against itself. So the comparison is
against the FROZEN MANIFEST: the artifact that was approved. Set identity
against a fixed list is a check the writer cannot influence.

AND IT DOES NOT TRUST `action_approvals.result`. The carrier is the authoritative
evidence of classification; the approval row is corroborating execution
metadata. Where the two disagree the verifier reports the DISAGREEMENT, because
"carrier present + approval failed" is a real and recoverable state -- it is not
equivalent to "the classification never happened", and nothing here may read it
that way.

    python -m scripts.verify_classification_live --manifest path/to/manifest.json
    python -m scripts.verify_classification_live --manifest m.json --target railway
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2                                            # noqa: E402

from app.core.config import get_settings                   # noqa: E402

# The five datasets the classification must not have touched, with the column
# projections the Step 34 baseline fingerprinted. Kept here rather than imported
# so the verifier does not depend on the writer's modules at all.
_BASELINE_PROJECTIONS = {
    "invoices": ("invoice_id", "subtotal_amount", "tax_amount", "total_amount",
                 "shipping_amount", "discount_amount", "adjustment_amount",
                 "balance_due", "base_total_amount", "base_balance_due",
                 "status", "paid_at", "exchange_rate"),
    "payments": ("payment_id", "invoice_id", "amount", "status", "payment_date"),
    "orders": ("order_id", "status", "subtotal_amount", "total_amount",
               "deleted_at"),
    "tax_rates": ("tax_rate_id", "country", "region", "tax_name", "rate",
                  "valid_to"),
    "addresses": ("address_id", "parent_type", "parent_id", "label", "country",
                  "province", "is_default"),
}
_LIVE_ONLY = {"invoices": "WHERE COALESCE(is_deleted,false)=false"}


def _dsn(target):
    get_settings()
    if target == "railway":
        dsn = (os.getenv("RAILWAY_DB_URL") or "").strip()
        if not dsn:
            raise SystemExit("RAILWAY_DB_URL is not set")
        return dsn
    return get_settings().db_dsn


def _fingerprint(order_ids):
    ids = sorted(str(x) for x in order_ids)
    return hashlib.sha256(("\n".join(ids) + "\n").encode("utf-8")).hexdigest()


def _table_md5(cur, table, cols):
    expr = " || '|' || ".join(f"COALESCE({c}::text,'~')" for c in cols)
    cur.execute(f"SELECT count(*), md5(string_agg(x, E'\\n' ORDER BY x)) "
                f"FROM (SELECT {expr} AS x FROM {table} "
                f"{_LIVE_ONLY.get(table, '')}) s")
    return cur.fetchone()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True,
                    help="the FROZEN approved manifest; the verifier compares "
                         "the carrier to this and never re-derives it")
    ap.add_argument("--target", choices=["railway"],
                    help="verify Railway instead of the configured DSN")
    ap.add_argument("--baseline", help="optional JSON of the five pre-write "
                                       "table fingerprints")
    args = ap.parse_args()

    m = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    census = m["census_id"]
    want_a = sorted(m["zero_tax_order_ids"])
    want_b = sorted(m["nonzero_unresolved_order_ids"])
    want_all = sorted(want_a + want_b)

    conn = psycopg2.connect(_dsn(args.target))
    # READ-ONLY BY CONSTRUCTION, not by intention. The server refuses a write
    # even if a future edit to this file attempted one.
    conn.set_session(readonly=True, autocommit=True)
    checks = []

    def check(name, ok, note=""):
        checks.append({"check": name, "ok": bool(ok), "note": note})

    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), current_user, "
                    "current_setting('default_transaction_read_only')")
        db, user, ro = cur.fetchone()
        check("connection_is_read_only", ro == "on",
              f"db={db} user={user} read_only={ro}")

        cur.execute("""SELECT order_id::text,
                              evidence->'census'->>'population',
                              state, rule, asserted_by
                         FROM order_financial_assertions
                        WHERE assertion_type = 'economic_correctness'
                          AND evidence->'census'->>'census_id' = %s""",
                    (census,))
        rows = cur.fetchall()
        got_all = sorted(r[0] for r in rows)
        got_a = sorted(r[0] for r in rows if r[1] == "zero_tax")
        got_b = sorted(r[0] for r in rows if r[1] == "nonzero_unresolved")

        missing = sorted(set(want_all) - set(got_all))
        extra = sorted(set(got_all) - set(want_all))
        dupes = len(got_all) - len(set(got_all))
        check("node_set_identity", not missing and not extra and not dupes,
              f"missing={len(missing)} extra={len(extra)} duplicates={dupes}")
        check("total_count", len(set(got_all)) == len(want_all),
              f"{len(set(got_all))} of {len(want_all)}")
        check("population_partition",
              len(got_a) == len(want_a) and len(got_b) == len(want_b),
              f"zero_tax {len(got_a)}/{len(want_a)}, "
              f"nonzero {len(got_b)}/{len(want_b)}")
        check("fingerprint_zero_tax",
              _fingerprint(got_a) == m["fingerprint_zero_tax"])
        check("fingerprint_nonzero_unresolved",
              _fingerprint(got_b) == m["fingerprint_nonzero_unresolved"])
        check("fingerprint_all", _fingerprint(got_all) == m["fingerprint_all"])
        check("assertion_values",
              all(r[2] == "historically_unverified" and r[3] == "no_evidence"
                  for r in rows) if rows else False,
              "every row historically_unverified / no_evidence")
        asserters = {r[4] for r in rows}
        check("single_asserter", len(asserters) == 1,
              f"asserted_by={sorted(asserters)}")

        # No SECOND assertion for the same (order, census). The carrier has no
        # unique constraint, so this is the check that would have caught the
        # concurrency defect in production.
        cur.execute("""SELECT count(*) FROM (
                         SELECT order_id FROM order_financial_assertions
                          WHERE assertion_type='economic_correctness'
                            AND evidence->'census'->>'census_id' = %s
                          GROUP BY order_id HAVING count(*) > 1) d""",
                    (census,))
        check("no_same_census_duplicates", cur.fetchone()[0] == 0)

        if args.baseline:
            base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            for table, cols in _BASELINE_PROJECTIONS.items():
                n, md5 = _table_md5(cur, table, cols)
                check(f"unchanged_{table}", md5 == base.get(table),
                      f"{n} rows, md5={md5}")
        else:
            check("protected_tables_unchanged", False,
                  "NOT EVALUATED -- no --baseline supplied. Not evaluated is "
                  "not passed.")

        # Corroboration only. A disagreement is reported as a disagreement.
        cur.execute("""SELECT status, result->>'inserted', result->>'already_classified'
                         FROM action_approvals
                        WHERE action_type='classification.record_tax_evidence'
                        ORDER BY created_at DESC LIMIT 1""")
        r = cur.fetchone()
        if r:
            carrier_present = len(set(got_all)) == len(want_all)
            if r[0] != "executed" and carrier_present:
                check("approval_and_carrier_agree", False,
                      f"approval status={r[0]} while the carrier holds the "
                      f"full classification. This is a RECOVERABLE "
                      f"INCONSISTENCY, not evidence that nothing executed.")
            else:
                check("approval_and_carrier_agree", True,
                      f"approval status={r[0]} inserted={r[1]} "
                      f"already_classified={r[2]}")
        else:
            check("approval_and_carrier_agree", True, "no approval row yet")
    conn.close()

    width = max(len(c["check"]) for c in checks)
    for c in checks:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['check']:<{width}}  "
              f"{c['note']}")
    bad = [c["check"] for c in checks if not c["ok"]]
    print(f"\n  census {census}: {len(checks) - len(bad)}/{len(checks)} checks pass")
    if bad:
        print("  FAILED: " + ", ".join(bad))
        return 1
    print("  VERIFIED against the frozen manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
