"""Dump production, restore it, and time the restore. Every run.

WHY THIS VERIFIES RATHER THAN JUST COPIES
-----------------------------------------
A backup nobody has restored is a hypothesis. This project has produced five
cases where a control's documented behaviour and its real behaviour differed, so
a dump file that has never been read back is not evidence of anything.

Each run therefore does the whole loop: dump from Railway, restore into a local
scratch database, compare row counts against the source, and report the elapsed
restore time. That number IS the RTO — measured, not estimated — and it is
re-measured every time rather than being a one-off drill someone remembers doing.

WHAT THIS IS AND IS NOT
-----------------------
It is a LOGICAL backup: RPO equals the interval between runs. Everything written
since the last dump is lost in a disaster.

It is NOT point-in-time recovery, so RPO is the interval between runs.

CORRECTION (2026-08-05): this docstring previously claimed Railway's managed
Postgres CANNOT do PITR, reasoning from archive_mode being context=postmaster
and WAL living on the same volume as the data. That reasoning was sound about
self-managed WAL archiving and wrong about the actual question. Railway sells
Backups and PITR on the Pro plan (Postgres service -> Backups tab). Closing the
RPO gap is a billing decision, not a re-platform.

Kept as a correction rather than deleted: the original claim was confident,
specific, technically literate, and would have sent someone to migrate a
database to solve a problem a plan upgrade solves. Checking what the vendor
sells is not the same as inferring it from pg_settings.

WHERE THE DUMPS GO
------------------
A local directory, which is off-platform and therefore survives a Railway
incident — but is one machine. It survives the provider, not the building. Copy
them somewhere else if the data justifies it.

    python -m scripts.backup_railway              # dump + verify + prune
    python -m scripts.backup_railway --dump-only  # skip the restore drill
"""

from __future__ import annotations

import os
import shutil
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import config as _config          # noqa: E402,F401  (loads .env)

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(ROOT / "backups")))
KEEP = int(os.getenv("BACKUP_KEEP", "14"))
SCRATCH_DB = os.getenv("BACKUP_SCRATCH_DB", "restore_drill")

# Schemas the dump deliberately skips, and the ceiling that keeps that decision
# honest.
#
# This database carries Supabase scaffolding, `auth` and `storage`, left over
# from an earlier platform. Both have row-level security enabled, and pg_dump
# refuses to copy a table whose rows a policy could filter, because the result
# would look like a backup while silently omitting rows. The alternative to
# skipping them is granting the dump role BYPASSRLS, which is a standing
# permission to see through every present and future policy.
#
# They are skipped instead, because on 2026-09-27 all 29 tables were empty:
# 26 occupied zero bytes, and the remaining two held Supabase's own migration
# bookkeeping. Nothing in `public` references either schema by foreign key,
# function or view. The application authenticates against
# `public.auth_credentials`.
#
# That measurement is the premise of the exclusion, so the backup re-checks it
# every run rather than trusting a comment. If these schemas ever hold real
# data, skipping them would be data loss, and the run stops instead.
EXCLUDE_SCHEMAS = [x.strip() for x in
                   os.getenv("BACKUP_EXCLUDE_SCHEMAS", "auth,storage").split(",")
                   if x.strip()]
MAX_EXCLUDED_BYTES = int(os.getenv("BACKUP_MAX_EXCLUDED_BYTES", str(1 << 20)))

# EVERY table is verified, not a sample.
#
# The first version checked ten representative tables and printed only those,
# which read as though ten tables were the entire backup. Sampling was also the
# weaker choice: a restore that drops one obscure table passes a ten-table spot
# check, and 223 exact counts cost one round trip per side when they are issued
# as a single UNION ALL rather than 223 separate queries.
#
# These few are printed individually because a human reading the output wants to
# see business data, audit trail and derived state named. The PASS/FAIL is
# decided across all of them.
HIGHLIGHT = ("accounts", "contacts", "opportunities", "orders", "activities",
             "cases", "audit_log", "content_embeddings",
             "record_field_history", "memory_erasure_log")


# THE RESTORE TARGET MUST CARRY PRODUCTION'S EXTENSIONS.
#
# With plain postgres:18 the restore silently lost the `items` table: it uses
# the `vector` type, CREATE EXTENSION vector failed in the container, and the
# dependent table never landed. 223 tables restored against 224 in production,
# and a ten-table spot check called it verified.
#
# pgvector/pgvector is the same postgres image with the extension present.
PG_IMAGE = os.getenv("BACKUP_PG_IMAGE", "pgvector/pgvector:pg18")


def _server_major(dsn: str) -> int:
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            return int(cur.fetchone()[0].split(".")[0])
    finally:
        conn.close()


def _docker(*args, mount: Path = None) -> subprocess.CompletedProcess:
    """Run a PostgreSQL client tool inside a matching-version container.

    pg_dump REFUSES to dump a server newer than itself, and pg_restore cannot
    read an archive from a newer major version. Railway runs 18; the local
    client here is 17. Rather than require a client upgrade on every machine
    that takes a backup, the tool is pinned to the SERVER's major version and
    run in a container — which also makes the drill reproducible on any host
    with Docker, instead of depending on what happens to be installed.
    """
    cmd = ["docker", "run", "--rm", "--network", "host"]
    if mount:
        cmd += ["-v", f"{mount}:/backup"]
    cmd += [PG_IMAGE, *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def _tool(name: str) -> str:
    """Find a PostgreSQL binary. Windows installs are not on PATH by default."""
    from shutil import which
    found = which(name)
    if found:
        return found
    for base in sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin"),
                       reverse=True):
        exe = base / f"{name}.exe"
        if exe.exists():
            return str(exe)
    raise SystemExit(f"{name} not found. Install PostgreSQL client tools, or add "
                     f"them to PATH.")


def _stderr_excerpt(text: str, head: int = 1200, tail: int = 400) -> str:
    """Excerpt a tool's stderr, keeping the beginning.

    PostgreSQL reports the reason first and the offending statement second, and
    pg_dump's statement here is a LOCK TABLE naming every table in the
    database. Keeping only the trailing 800 characters therefore preserved the
    table list and discarded the reason on every failed run between 2026-09-12
    and 2026-09-26. The cause, "permission denied for schema auth", was never
    once recorded, and the outage ran for sixteen nights.
    """
    text = (text or "").strip()
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n  ... {omitted} characters omitted ...\n{text[-tail:]}"


def _prunable(directory: Path) -> tuple:
    """Split the dumps in a directory into (beyond retention, empty leftovers).

    Retention previously sorted every railway-*.dump by filename and kept the
    last KEEP names. A failed run leaves a dated zero-byte file behind, because
    pg_dump creates its output file before it knows whether it can proceed, so
    sixteen empty dumps sorted ahead of every real one. The first successful
    run would have deleted all fourteen surviving backups and retained the
    empty files.

    Retention now applies only to dumps that contain data, ordered by
    modification time, so an artifact that is not a backup can never displace
    one that is. Empty files are reported separately and always removed.
    """
    dumps = list(directory.glob("railway-*.dump"))
    empty = sorted(f for f in dumps if f.stat().st_size == 0)
    usable = sorted((f for f in dumps if f.stat().st_size > 0),
                    key=lambda f: f.stat().st_mtime)
    beyond = usable[:-KEEP] if KEEP else []
    return beyond, empty


def _assert_excluded_are_empty(dsn: str) -> None:
    """Confirm the schemas the dump skips still hold nothing worth keeping.

    Size is read from the catalog rather than counted, because row-level
    security is what makes these schemas unreadable in the first place: a
    SELECT would return whatever the policies allow and report empty tables
    that are not. pg_relation_size is not subject to policy.
    """
    if not EXCLUDE_SCHEMAS:
        return
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT n.nspname, c.relname, pg_relation_size(c.oid) AS bytes
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE c.relkind = 'r' AND n.nspname = ANY(%s)
                 ORDER BY bytes DESC""", (EXCLUDE_SCHEMAS,))
            rows = cur.fetchall()
    finally:
        conn.close()
    total = sum(r[2] for r in rows)
    print(f"  skipping {', '.join(EXCLUDE_SCHEMAS)} "
          f"({len(rows)} tables, {total:,} bytes)")
    if total > MAX_EXCLUDED_BYTES:
        biggest = "; ".join(f"{n}.{t} {b:,} bytes" for n, t, b in rows[:5])
        raise SystemExit(
            f"the schemas this backup skips now hold {total:,} bytes, above "
            f"the {MAX_EXCLUDED_BYTES:,} allowed.\n"
            f"  largest: {biggest}\n"
            f"  These were empty when they were excluded. Continuing would "
            f"omit real data from every backup.\n"
            f"  Either include them again -- which needs "
            f"ALTER ROLE <dump role> BYPASSRLS, because they use row-level "
            f"security -- or raise BACKUP_MAX_EXCLUDED_BYTES once the content "
            f"is known to be disposable.")


def _counts(dsn: str, tables=None) -> dict:
    """Exact row counts for every ordinary table in `public`, in one round trip.

    reltuples would be cheaper and is an ESTIMATE — useless for deciding whether
    a restore reproduced the data. A generated UNION ALL gives exact counts for
    all 223 tables at the cost of a single query."""
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            if tables is None:
                cur.execute("""SELECT c.relname FROM pg_class c
                                 JOIN pg_namespace n ON n.oid = c.relnamespace
                                WHERE n.nspname = 'public' AND c.relkind = 'r'
                                ORDER BY 1""")
                tables = [r[0] for r in cur.fetchall()]
            if not tables:
                return {}
            # A table present in production but ABSENT here is the failure this
            # exists to catch, so it must be reported rather than raised.
            cur.execute("""SELECT c.relname FROM pg_class c
                             JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname='public' AND c.relkind='r'""")
            present = {r[0] for r in cur.fetchall()}
            missing = [t for t in tables if t not in present]
            tables = [t for t in tables if t in present]
            if not tables:
                return {t: None for t in missing}
            union = " UNION ALL ".join(
                f'SELECT {chr(39)}{t}{chr(39)} AS t, count(*) AS n FROM public."{t}"'
                for t in tables)
            cur.execute(union)
            out = {r[0]: r[1] for r in cur.fetchall()}
            out.update({t: None for t in missing})   # None = table not present
            return out
    finally:
        conn.close()


def main() -> int:
    src = (os.getenv("RAILWAY_DB_URL") or "").strip()
    if not src:
        raise SystemExit("RAILWAY_DB_URL is not set")
    local = (os.getenv("DB_DSN") or "").strip()
    if not local:
        raise SystemExit("DB_DSN is not set — needed for the restore drill")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dump = BACKUP_DIR / f"railway-{stamp}.dump"

    # ── dump ────────────────────────────────────────────────────────────────
    # COUNT BEFORE DUMPING, NOT AFTER.
    #
    # pg_dump takes a consistent snapshot at its START, and this dump runs for
    # ~200s over the public proxy while the scheduler keeps indexing. Comparing
    # the restore against production AFTER the dump therefore compares a
    # snapshot with a moving source: content_embeddings read 3,570 restored
    # against 3,698 live and the run failed, with nothing actually wrong.
    #
    # The correct assertion is that a restore contains AT LEAST what existed
    # when the dump began. Anything written during the window is legitimately
    # absent; anything MISSING is data loss.
    major = _server_major(src)
    print(f"  production is PostgreSQL {major}; using {PG_IMAGE} client tools")
    _assert_excluded_are_empty(src)
    before = _counts(src)
    print(f"  production before dump: {len(before)} tables, "
          f"{sum(v for v in before.values() if v):,} rows")
    # Write under a name the retention glob cannot match, and rename only
    # after pg_dump reports success.
    #
    # A failed dump is not always empty. On 2026-09-27 two runs stopped partway
    # through COPY and left 2.3 MB archives that pg_restore reads without
    # complaint, because the table of contents is written first. They were
    # counted as backups and displaced three real ones. Neither file size nor
    # archive readability separates a partial custom-format dump from a
    # complete one, so neither can be the test. The exit code is the only
    # signal that means anything, and a file that does not yet carry the final
    # name cannot be mistaken for a backup by anything -- including a run
    # killed partway, or a power loss.
    partial = dump.with_suffix(".dump.partial")
    for stale in BACKUP_DIR.glob("railway-*.dump.partial"):
        stale.unlink()
    t0 = time.perf_counter()
    r = _docker("pg_dump", src, "-Fc", "--no-owner", "--no-acl",
                *[f"--exclude-schema={x}" for x in EXCLUDE_SCHEMAS],
                "-f", f"/backup/{partial.name}", mount=BACKUP_DIR)
    dump_s = time.perf_counter() - t0
    if r.returncode != 0:
        if partial.exists():
            partial.unlink()
        raise SystemExit(f"pg_dump failed:\n{_stderr_excerpt(r.stderr)}")
    partial.rename(dump)
    size_mb = dump.stat().st_size / 1e6
    print(f"  dumped   {size_mb:7.1f} MB in {dump_s:6.1f}s -> {dump.name}")

    if "--dump-only" in sys.argv:
        print("  --dump-only: restore drill skipped (RTO NOT measured this run)")
        return 0

    # ── restore into a THROWAWAY container of the production major version ──
    # Restoring into a different major version proves nothing about whether
    # production could be rebuilt from this file.
    cname = f"restore-drill-{stamp}"
    subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
    up = subprocess.run(
        ["docker", "run", "-d", "--name", cname, "-e", "POSTGRES_PASSWORD=drill",
         "-p", "55432:5432", "-v", f"{BACKUP_DIR}:/backup", PG_IMAGE],
        capture_output=True, text=True)
    if up.returncode != 0:
        raise SystemExit(f"could not start the restore container:\n{up.stderr[-500:]}")

    bad, restore_s = [], 0.0
    try:
        # READINESS: pg_isready IS NOT ENOUGH.
        #
        # The postgres image starts a temporary server on a unix socket to run
        # initdb, then RESTARTS it. pg_isready succeeds against that temporary
        # server, so a check that trusts it proceeds against a server about to
        # bounce — createdb fails, pg_restore fails, and (before this was fixed)
        # the script printed "restored in 0.2s <-- THIS IS THE RTO" for a restore
        # that never happened.
        #
        # A real TCP connection from the host, twice a second apart, is the only
        # signal that the final server is accepting work.
        import psycopg2
        probe = f"postgresql://postgres:drill@127.0.0.1:55432/postgres"
        ready = 0
        for _ in range(90):
            time.sleep(1)
            try:
                psycopg2.connect(probe, connect_timeout=2).close()
                ready += 1
                if ready >= 2:
                    break
            except Exception:
                ready = 0
        else:
            raise SystemExit("restore container never accepted TCP connections")

        r = subprocess.run(["docker", "exec", cname, "createdb", "-U", "postgres",
                            SCRATCH_DB], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"createdb failed — the restore did NOT run:\n"
                             f"{r.stderr[-400:]}")

        t0 = time.perf_counter()
        r = subprocess.run(
            ["docker", "exec", cname, "pg_restore", "-U", "postgres", "-d",
             SCRATCH_DB, "--no-owner", "--no-acl", "-j", "4",
             f"/backup/{dump.name}"], capture_output=True, text=True)
        restore_s = time.perf_counter() - t0

        # pg_restore exits non-zero for WARNINGS too (extensions and roles it
        # cannot recreate), so the exit code alone cannot decide. What settles it
        # is whether tables actually landed — checked before any timing is
        # reported as an RTO, because a number attached to a failed restore is
        # worse than no number.
        chk = subprocess.run(
            ["docker", "exec", cname, "psql", "-U", "postgres", "-d", SCRATCH_DB,
             "-tAc", "SELECT count(*) FROM information_schema.tables "
                     "WHERE table_schema='public'"],
            capture_output=True, text=True)
        landed = int((chk.stdout or "0").strip() or 0)
        if landed == 0:
            raise SystemExit(f"pg_restore produced NO tables — the restore "
                             f"failed:\n{r.stderr[-600:]}")
        print(f"  restored {landed:4} tables in {restore_s:6.1f}s  <-- THIS IS THE RTO")

        target = f"postgresql://postgres:drill@127.0.0.1:55432/{SCRATCH_DB}"
        want = _counts(src)                       # every table in production
        got = _counts(target, list(want))         # the same list, restored
        bad = [t for t in want if want[t] != got.get(t)]
        print(f"\ncomparing ALL {len(want)} tables; showing the main ones:")
        print(f"\n{'table':24} {'production':>11} {'restored':>10}")
        for t in sorted(HIGHLIGHT):
            if t in want:
                flag = "" if want[t] == got.get(t) else "   <-- MISMATCH"
                print(f"  {t:24} {want[t]:11} {got.get(t, 0):10}{flag}")
        for t in sorted(bad):                     # never hide a mismatch
            if t not in HIGHLIGHT:
                g = got.get(t)
                shown = "ABSENT" if g is None else str(g)
                print(f"  {t:24} {want[t]:11} {shown:>10}   <-- MISMATCH")
    finally:
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True)

    old, empty = _prunable(BACKUP_DIR)
    for f in old + empty:
        f.unlink()
    if old:
        print(f"\npruned {len(old)} dump(s) beyond the {KEEP} most recent")
    if empty:
        print(f"  removed {len(empty)} zero-byte file(s) left by a failed run")

    if bad:
        print(f"\nRESTORE VERIFICATION FAILED on: {', '.join(bad)}")
        print("  The dump exists but does not reproduce production. Do not")
        print("  treat it as a backup until this is explained.")
        _ping(False, f"restore verification failed on: {', '.join(bad)}")
        return 1
    print(f"\nverified: every checked table matches production")
    print(f"  RPO = time since this ran.  RTO = {restore_s:.0f}s + provisioning.")

    _mirror(dump)
    _ping(True, f"verified {len(want)} tables; restore {restore_s:.1f}s")
    return 0


def _ping(ok: bool, detail: str = "") -> None:
    """Tell a dead-man's-switch service this run finished, and how.

    The backup verifies itself thoroughly and then tells nobody. Every failure
    mode that matters is SILENT from outside: the machine is off, Docker is not
    running, the script raised, the task was deleted. In each case the outcome
    is identical — no backup — and the only evidence is a log file on the
    machine that did not run it.

    A dead-man's switch inverts that. Success must be actively reported, so the
    absence of a report IS the alert. That is the property a status check cannot
    have: it can only tell you about runs that happened.

    Never raises. A monitoring call that can break a backup is a bad trade."""
    base = os.getenv("BACKUP_PING_URL", "").strip()
    if not base:
        return
    url = base.rstrip("/") + ("" if ok else "/fail")
    try:
        import urllib.request
        req = urllib.request.Request(
            url, data=detail.encode("utf-8")[:9000] if detail else None,
            headers={"User-Agent": "backup_railway"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"  pinged monitor ({'ok' if ok else 'FAIL'}): {resp.getcode()}")
    except Exception as exc:                                    # noqa: BLE001
        print(f"  monitor ping failed (backup itself is unaffected): "
              f"{type(exc).__name__}: {exc}")


def _mirror(dump_path: Path) -> None:
    """Copy the verified dump to a second physical device.

    Only VERIFIED dumps are mirrored — copying one that failed verification
    would propagate a bad backup and, worse, make the off-site copy look
    healthier than the primary.

    A missing drive is reported but never fails the run. An external disk that
    happens to be unplugged is a normal Tuesday; turning that into a backup
    failure would train someone to ignore backup failures. The staleness is
    what matters, so the age of the newest mirrored copy is always printed —
    'the mirror is 9 days old' is the sentence that needs to reach a human.
    """
    target = os.getenv("BACKUP_MIRROR_DIR", "").strip()
    if not target:
        return
    dest = Path(target)
    try:
        if not dest.parent.exists():
            print(f"\nMIRROR SKIPPED — {dest.parent} not available "
                  f"(external drive not connected?). The off-site copy is NOT "
                  f"up to date.")
            return
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dump_path, dest / dump_path.name)
        # Mirror the retention policy too, or the second copy grows forever.
        # The same rule applies: an empty file must never displace a backup.
        old, empty = _prunable(dest)
        for f in old + empty:
            f.unlink()
        copies = sorted(dest.glob("railway-*.dump"))
        newest = max((f.stat().st_mtime for f in copies), default=0)
        age_h = (time.time() - newest) / 3600 if newest else -1
        print(f"\nmirrored to {dest}  ({len(copies)} dump(s) there, "
              f"newest {age_h:.1f}h old)")
        if old:
            print(f"  pruned {len(old)} old mirror copy/copies")
    except Exception as exc:                                    # noqa: BLE001
        print(f"\nMIRROR FAILED — the off-site copy is NOT up to date: "
              f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
