"""Is the deployed order procedure the one the repository says it should be?

READ-ONLY. The session is opened read-only and verified to be read-only before
any production state is read; the only statements issued are SELECTs against the
catalogue. The script cannot write even if it were wrong.

WHY THIS EXISTS. sp_orders_v5e.sql was applied to Railway through pgAdmin rather
than through apply_sql.py, which left no schema_migrations row, no attestation and
no apply output. "Applied" was recorded only as a human statement, and a human
statement is not evidence that the right bytes landed. A paste can be partial,
stale, or from the wrong buffer, and the failure is silent: the procedure still
runs, it just runs the old logic.

WHAT IS THE AUTHORITY. The repository's `.governance-pin` names the governance
commit this working repository is verified against, and that commit's artifact is
the expected procedure. The pin is resolved through the repository's own resolver
(app.core.artifact_paths.governance_pin) rather than a second implementation, so
this script cannot disagree with the rest of the repository about what is pinned.
A commit may be supplied explicitly for ad-hoc checks, but never silently: the
output always names where the expectation came from.

WHAT EQUIVALENCE MEANS HERE, PRECISELY. Three things are compared, each derived
from the published artifact:

  * the procedure BODY -- pg_proc.prosrc against the dollar-quoted body of the
    published CREATE statement, with carriage returns stripped from both sides
    because the applied text may have been normalised in transit;
  * the security context -- pg_proc.prosecdef against whether the published
    CREATE declares SECURITY DEFINER;
  * the published COMMENT -- obj_description against the COMMENT ON FUNCTION
    text in the artifact, because that string is applied to pg_description and
    is the function's published contract.

Two things are deliberately NOT proven, and the output says so rather than
letting body equality imply them:

  * EXECUTE grants (pg_proc.proacl). CREATE OR REPLACE preserves the existing
    ACL, so grants are not a property of this artifact and cannot be derived
    from it. A separate privilege control owns that question.
  * the argument signature. Deriving normalised PostgreSQL type names from a
    SQL parameter list is guesswork, and a wrong guess here would be worse than
    the gap; the duplicate-definition check below catches the overload case that
    matters in practice.

STRUCTURAL PROFILE. The structural counts are derived from the published body and
compared against the live body; none of them is a literal constant. When the
bodies hash equal the profile necessarily agrees, so it adds nothing — its value
is in the mismatch case, where it names WHICH property diverged instead of only
reporting "different".

    python -m scripts.verify_order_sp_live --target railway
    python -m scripts.verify_order_sp_live --target railway --commit <sha>
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

# Resolved from this file, never from the current working directory: invoking the
# script from elsewhere must not make it read a different repository's pin.
REPO_ROOT = Path(__file__).resolve().parents[1]
GOV_DIR = REPO_ROOT / "governance"
ARTIFACT = "sp/sp_orders_v5e.sql"
FUNCTIONS = ("fn_recalc_order_totals", "sp_orders")

# The controls are the PATTERNS. Their expected values come from the published
# artifact, so a future published change moves the expectation with it.
CONTROLS = [
    ("fn_recalc_order_totals", "seal predicate (snapshot_sealed_at IS NULL)",
     r"i\.snapshot_sealed_at IS NULL"),
    ("fn_recalc_order_totals", "historical status-list predicate",
     r"NOT IN\s*\n?\s*\('paid'"),
    ("fn_recalc_order_totals", "balance_due assignment", r"balance_due\s*=[^=]"),
    ("fn_recalc_order_totals", "base_balance_due assignment",
     r"base_balance_due\s*=[^=]"),
    ("fn_recalc_order_totals", "base_total_amount assignment",
     r"base_total_amount\s*=[^=]"),
    ("fn_recalc_order_totals", "paid_at assignment", r"paid_at\s*=[^=]"),
    ("sp_orders", "deferred shipped-status staging", r"v_defer_shipped"),
    ("sp_orders", "-44 refusal site", r"'code', -44"),
    ("sp_orders", "order.cancel message", r"Use the order\.cancel capability"),
    ("sp_orders", "cancellation-to-invoice propagation (must stay absent)",
     r"v_cancel_invoice_count|already settled and cannot be cancelled"),
]


class Refused(SystemExit):
    """Every failure path raises this, so the script always fails closed."""


def _git(args, cwd: Path) -> str:
    r = subprocess.run(["git", *args], capture_output=True, cwd=str(cwd))
    if r.returncode != 0:
        raise Refused("git %s failed: %s"
                      % (" ".join(args[:2]), r.stderr.decode("utf-8", "replace")[:200]))
    return r.stdout.decode("utf-8", "replace")


# ── the published artifact ──────────────────────────────────────────────────

def expected_commit(explicit: str | None) -> tuple[str, str]:
    """(sha, where it came from). The pin is the default and the authority."""
    if explicit:
        sha = explicit.strip().lower()
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise Refused("--commit must be a full 40-character sha, got %r" % explicit)
        return sha, "--commit (explicit override; the pin was NOT consulted)"
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from app.core.artifact_paths import governance_pin
    except Exception as exc:                                   # pragma: no cover
        raise Refused("cannot load the repository's pin resolver: %s" % exc)
    sha = governance_pin()
    if not sha:
        raise Refused(".governance-pin is absent, malformed, or names no commit; "
                      "there is nothing to verify against")
    return sha, "%s (via app.core.artifact_paths.governance_pin)" % (REPO_ROOT / ".governance-pin")


def prove_published(sha: str, remote: str) -> str:
    """The commit must resolve on the governance REMOTE, not merely locally.

    A local object-store hit proves only that the commit exists on this machine,
    which is exactly the case a verifier must not accept.
    """
    try:
        _git(["cat-file", "-e", sha + "^{commit}"], GOV_DIR)
    except Refused:
        raise Refused("commit %s does not exist in %s" % (sha[:12], GOV_DIR))
    out = _git(["ls-remote", remote], GOV_DIR)
    rows = [ln.split("\t") for ln in out.splitlines() if "\t" in ln]
    tips = {r[0]: r[1] for r in rows}
    if sha in tips:
        return "published: it is the tip of %s on %s" % (tips[sha], remote)
    for tip_sha, ref in tips.items():
        if subprocess.run(["git", "cat-file", "-e", tip_sha + "^{commit}"],
                          capture_output=True, cwd=str(GOV_DIR)).returncode != 0:
            continue
        if subprocess.run(["git", "merge-base", "--is-ancestor", sha, tip_sha],
                          capture_output=True, cwd=str(GOV_DIR)).returncode == 0:
            return "published: reachable from %s on %s" % (ref, remote)
    raise Refused(
        "commit %s exists locally but is NOT reachable from any ref on %s. A "
        "local-only commit is not published, and a verifier must not accept it "
        "as the expected artifact." % (sha[:12], remote))


def published_source(sha: str) -> str:
    return _git(["cat-file", "blob", "%s:%s" % (sha, ARTIFACT)], GOV_DIR)


# ── dollar-quoted body extraction ───────────────────────────────────────────

_AS_DOLLAR = re.compile(r"\bAS\s*(\$[A-Za-z_0-9]*\$)")


def extract_body(src: str, func: str) -> str:
    """The body of one function, delimited by its OWN dollar-quote tag.

    Keying on a bare `$$` is not safe in this repository: functions here are
    written with both `$$` and `$fn$`, and a `$$`-only reader walks straight past
    a `$fn$` body and returns text bounded by a LATER function's delimiter --
    a confident wrong answer rather than an error.
    """
    m = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+" + re.escape(func) + r"\s*\(",
                  src)
    if not m:
        raise Refused("the published artifact defines no function %s" % func)
    o = _AS_DOLLAR.search(src, m.end())
    if not o:
        raise Refused("no dollar-quoted body found for %s" % func)
    tag = o.group(1)
    close = src.find(tag, o.end())
    if close == -1:
        raise Refused("the body of %s opens with %s and never closes with it; "
                      "refusing rather than guessing a delimiter" % (func, tag))
    return src[o.end():close]


def declares_security_definer(src: str, func: str) -> bool:
    """Whether the published CREATE for this function says SECURITY DEFINER."""
    m = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+" + re.escape(func) + r"\s*\(",
                  src)
    if not m:
        raise Refused("the published artifact defines no function %s" % func)
    o = _AS_DOLLAR.search(src, m.end())
    if not o:
        raise Refused("no dollar-quoted body found for %s" % func)
    return "SECURITY DEFINER" in src[m.end():o.start()].upper()


def published_comment(src: str, func: str) -> str | None:
    """The COMMENT ON FUNCTION text, or None when the artifact sets none."""
    m = re.search(r"COMMENT\s+ON\s+FUNCTION\s+" + re.escape(func) + r"\b[^']*'", src)
    if not m:
        return None
    i = m.end()
    out = []
    while i < len(src):
        if src[i] == "'":
            if i + 1 < len(src) and src[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out)
        out.append(src[i])
        i += 1
    raise Refused("the COMMENT ON FUNCTION %s string is unterminated" % func)


# ── comparison helpers ──────────────────────────────────────────────────────

def norm(s: str) -> str:
    return (s or "").replace("\r", "")


def h(s: str) -> str:
    return hashlib.sha256(norm(s).encode("utf-8")).hexdigest()


def profile(body: str, func: str) -> dict:
    return {label: len(re.findall(pat, norm(body)))
            for fn, label, pat in CONTROLS if fn == func}


# ── main ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify the deployed order procedure "
                                             "against the pinned published artifact.")
    ap.add_argument("--target", choices=("railway",), default=None,
                    help="railway reads RAILWAY_DB_URL; production is never a default")
    ap.add_argument("--commit", default=None,
                    help="verify against this governance commit instead of the pin "
                         "(explicit ad-hoc use; the output records that the pin was "
                         "not consulted)")
    ap.add_argument("--remote", default="origin",
                    help="governance remote on which the commit must be published")
    ap.add_argument("--expect-db", default=None,
                    help="fail unless the server reports this database name")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    sha, origin_of = expected_commit(args.commit)
    print("repository root   : %s" % REPO_ROOT)
    print("expected commit   : %s" % sha)
    print("  taken from      : %s" % origin_of)
    print("  %s" % prove_published(sha, args.remote))

    src = published_source(sha)
    want_body = {f: extract_body(src, f) for f in FUNCTIONS}
    want_secdef = {f: declares_security_definer(src, f) for f in FUNCTIONS}
    want_comment = {f: published_comment(src, f) for f in FUNCTIONS}

    sys.path.insert(0, str(REPO_ROOT))
    import app.core.config  # noqa: F401  -- loads .env the way the app does
    if args.target == "railway":
        dsn, label = (os.getenv("RAILWAY_DB_URL") or "").strip(), "railway"
        if not dsn:
            raise Refused("RAILWAY_DB_URL is not set")
        if "sslmode" not in dsn.lower():
            raise Refused("RAILWAY_DB_URL has no sslmode; libpq would silently "
                          "downgrade to plaintext. Refusing to connect.")
    else:
        dsn = (os.getenv("DATABASE_URL") or os.getenv("DB_DSN") or "").strip()
        label = "configured DSN"
        if not dsn:
            raise Refused("no DATABASE_URL or DB_DSN configured")

    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True, autocommit=True)
    live_body, live_secdef, live_comment = {}, {}, {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, "
                        "current_setting('server_version'), "
                        "current_setting('default_transaction_read_only')")
            db, user, server_version, ro = cur.fetchone()
            print("\nconnection target : %s" % label)
            print("  database        : %s" % db)
            print("  role            : %s" % user)
            print("  server version  : PostgreSQL %s" % server_version)
            print("  read-only session: %s" % ro)
            if ro != "on":
                raise Refused("the session is not read-only (default_transaction_"
                              "read_only=%r); refusing to read production state "
                              "from a session that could write." % ro)
            if args.expect_db and db != args.expect_db:
                raise Refused("connected to database %r but --expect-db said %r"
                              % (db, args.expect_db))
            for fn in FUNCTIONS:
                cur.execute("""SELECT p.prosrc, p.prosecdef,
                                      obj_description(p.oid, 'pg_proc')
                                 FROM pg_proc p
                                 JOIN pg_namespace n ON n.oid = p.pronamespace
                                WHERE p.proname = %s AND n.nspname = 'public'""", (fn,))
                rows = cur.fetchall()
                if len(rows) != 1:
                    raise Refused("%s has %d definitions in schema public; a single "
                                  "definition is required for an unambiguous "
                                  "comparison" % (fn, len(rows)))
                live_body[fn], live_secdef[fn], live_comment[fn] = rows[0]
    finally:
        conn.close()

    ok = True
    print("\n=== procedure body (pg_proc.prosrc vs the published dollar-quoted body,")
    print("    carriage returns stripped from both) ===")
    for fn in FUNCTIONS:
        same = h(live_body[fn]) == h(want_body[fn])
        ok &= same
        print("  %-26s %s" % (fn, "IDENTICAL" if same else "DIFFERS"))
        print("      published %s  (%d chars)" % (h(want_body[fn])[:16], len(norm(want_body[fn]))))
        print("      live      %s  (%d chars)" % (h(live_body[fn])[:16], len(norm(live_body[fn]))))

    print("\n=== security context (pg_proc.prosecdef vs the published CREATE) ===")
    for fn in FUNCTIONS:
        same = bool(live_secdef[fn]) == want_secdef[fn]
        ok &= same
        print("  %s  %-26s published %s, live %s"
              % ("PASS" if same else "FAIL", fn,
                 "SECURITY DEFINER" if want_secdef[fn] else "SECURITY INVOKER",
                 "SECURITY DEFINER" if live_secdef[fn] else "SECURITY INVOKER"))

    print("\n=== published COMMENT (pg_description vs COMMENT ON FUNCTION) ===")
    for fn in FUNCTIONS:
        w, l = want_comment[fn], live_comment[fn]
        if w is None:
            print("  n/a   %-26s the artifact sets no COMMENT" % fn)
            continue
        same = norm(w).strip() == norm(l).strip()
        ok &= same
        print("  %s  %-26s %s" % ("PASS" if same else "FAIL", fn,
                                  "matches" if same else "DIFFERS"))
        if not same:
            print("      published: %r" % norm(w).strip()[:90])
            print("      live     : %r" % norm(l or "").strip()[:90])

    print("\n=== structural profile, derived from the published body ===")
    print("    (identical bodies agree here by construction; this block exists to")
    print("     name WHICH property moved when they do not)")
    for fn in FUNCTIONS:
        pw, pl = profile(want_body[fn], fn), profile(live_body[fn], fn)
        for label in pw:
            same = pw[label] == pl[label]
            ok &= same
            print("  %s  %-26s %-46s published %d, live %d"
                  % ("PASS" if same else "FAIL", fn, label, pw[label], pl[label]))

    print("\n" + "=" * 70)
    print("NOT PROVEN by this check: EXECUTE grants (proacl), because CREATE OR")
    print("REPLACE preserves the ACL and grants are not a property of this")
    print("artifact; and the argument signature, because deriving normalised type")
    print("names from a SQL parameter list would be guesswork.")
    print("DEPLOYED PROCEDURE MATCHES THE PINNED PUBLISHED ARTIFACT: %s" % ok)
    if not ok and not profile(live_body.get("sp_orders", ""), "sp_orders").get(
            "deferred shipped-status staging"):
        print("\nNOTE: the live sp_orders has no deferred shipped-status staging, so "
              "it predates the invoice-ordering correction. Invoices created by the "
              "nightly order jobs are issued before the order's economics exist, so "
              "tax is not charged and the flat shipping fee is applied to orders "
              "that qualify for free shipping.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
