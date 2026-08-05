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

It is NOT point-in-time recovery. Railway's managed Postgres cannot do PITR:
`archive_mode` has context=postmaster so it needs a server restart, and the only
persistent storage is the same volume that holds the data — archiving WAL beside
the data means losing the volume loses both. Shipping WAL off-host needs wal-g
or pgbackrest inside the Postgres container, which the managed image does not
allow. If contractual PITR is required, that is a platform move, not a config
change.

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

# Tables whose counts must match after a restore. Not every table — a
# representative spread across business data, audit trail and derived state, so
# a partial restore cannot pass by matching one of them.
VERIFY_TABLES = ("accounts", "contacts", "opportunities", "orders", "activities",
                 "cases", "audit_log", "content_embeddings",
                 "record_field_history", "memory_erasure_log")


PG_IMAGE = os.getenv("BACKUP_PG_IMAGE", "postgres:18")


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


def _counts(dsn: str) -> dict:
    import psycopg2
    out = {}
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            for t in VERIFY_TABLES:
                try:
                    cur.execute(f"SELECT count(*) FROM {t}")
                    out[t] = cur.fetchone()[0]
                except Exception:
                    conn.rollback()          # table absent in this schema
    finally:
        conn.close()
    return out


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
    major = _server_major(src)
    print(f"  production is PostgreSQL {major}; using {PG_IMAGE} client tools")
    t0 = time.perf_counter()
    r = _docker("pg_dump", src, "-Fc", "--no-owner", "--no-acl",
                "-f", f"/backup/{dump.name}", mount=BACKUP_DIR)
    dump_s = time.perf_counter() - t0
    if r.returncode != 0:
        raise SystemExit(f"pg_dump failed:\n{r.stderr[-800:]}")
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
        want, got = _counts(src), _counts(target)
        bad = [t for t in want if want[t] != got.get(t)]
        print(f"\n{'table':24} {'production':>11} {'restored':>10}")
        for t in sorted(want):
            flag = "" if want[t] == got.get(t) else "   <-- MISMATCH"
            print(f"  {t:24} {want[t]:11} {got.get(t, 0):10}{flag}")
    finally:
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True)

    old = sorted(BACKUP_DIR.glob("railway-*.dump"))[:-KEEP] if KEEP else []
    for f in old:
        f.unlink()
    if old:
        print(f"\npruned {len(old)} dump(s) beyond the {KEEP} most recent")

    if bad:
        print(f"\nRESTORE VERIFICATION FAILED on: {', '.join(bad)}")
        print("  The dump exists but does not reproduce production. Do not")
        print("  treat it as a backup until this is explained.")
        return 1
    print(f"\nverified: every checked table matches production")
    print(f"  RPO = time since this ran.  RTO = {restore_s:.0f}s + provisioning.")
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
