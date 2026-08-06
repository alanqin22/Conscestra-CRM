"""Chaos test for HA leader election: kill the leader, time the promotion.

WHY
---
Background singletons stopped for ten days in July 2026. A rolling deploy left
every process a follower, nobody held the lock, and HTTP stayed healthy on every
node so nothing alerted.

`leader.py` was then given a promotion watcher, and its docstring records a
chaos test of the BROKEN behaviour ("3 workers, leader killed, 0 survivors
claimed leadership after 18 seconds"). What had never been measured is whether
the FIX works. A comment describing a fix is not evidence the fix runs.

MEASURED 2026-08-05: 3 workers, leader killed, promotion in 1.3s, exactly one
survivor promoted.

TWO HARNESS BUGS THIS FILE EXISTS TO NOT REPEAT
-----------------------------------------------
Both produced a confident "leader promotion is broken" for working code.

1. Popen.pid is not the worker's pid. sys.executable is a launcher that
   re-execs, so killing Popen.pid killed a shim and left the real leader alive,
   still holding the lock. No promotion happened because none was needed. Kill
   the pid the worker PRINTS.

2. Reading one pipe from two threads. An earlier version called a drain helper
   twice, each call starting a fresh reader on the same stdout, so lines went to
   whichever thread woke first and the second call saw nothing. Read each pipe
   exactly once, for the life of the test.

A harness that loses evidence produces false failures, and a false failure costs
as much as a false pass — it sends someone to fix code that already works.

Runs against DB_DSN with a TEST-ONLY lock key, so it cannot contend with a real
leader sharing the database.

    python -m scripts.chaos_leader
    python -m scripts.chaos_leader --workers 5
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core import config as _config          # noqa: E402,F401

TEST_KEY = 990001
WATCH = "2"          # seconds; production default is 10

WORKER = '''
import os, sys, time, logging
sys.path.insert(0, r"{root}")
os.environ["HA_LEADER_ELECTION"] = "1"
os.environ["HA_LOCK_KEY"] = "{key}"
os.environ["HA_WATCH_INTERVAL"] = "{watch}"
os.environ["HA_ELECTION_RETRY_SECONDS"] = "0"
logging.disable(logging.CRITICAL)
from app.core import config as _c
from app.core import leader
leader.on_promotion(lambda: print("PROMOTED %d %f" % (os.getpid(), time.time()),
                                  flush=True))
r = leader.begin()
print("ROLE %d %s" % (os.getpid(), leader.role()), flush=True)
if not r:
    leader._watch_for_promotion()
while True:
    time.sleep(0.5)
'''


def _lock_holders() -> int:
    import psycopg2
    conn = psycopg2.connect(os.environ["DB_DSN"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                        "AND objid=%s", (TEST_KEY,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()

    src = WORKER.format(root=str(ROOT), key=TEST_KEY, watch=WATCH)
    print(f"CHAOS: {a.workers} workers, lock key {TEST_KEY}, watch {WATCH}s\n")

    lines: List[Tuple[float, str]] = []
    procs = [subprocess.Popen([sys.executable, "-u", "-c", src],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True) for _ in range(a.workers)]

    def pump(p):                      # one reader per pipe, started once
        for line in p.stdout:
            lines.append((time.time(), line.strip()))

    for p in procs:
        threading.Thread(target=pump, args=(p,), daemon=True).start()

    try:
        time.sleep(5)
        roles = [l for _, l in lines if l.startswith("ROLE")]
        leaders = [l for l in roles if l.endswith("leader")]
        print(f"  elected            : {len(roles)}/{a.workers}")
        print(f"  claimed leader     : {len(leaders)} {leaders}")
        print(f"  advisory locks held: {_lock_holders()}")
        if len(leaders) != 1:
            print(f"\n  FAIL: expected exactly 1 leader, got {len(leaders)}")
            return 1

        leader_pid = leaders[0].split()[1]
        print(f"\n  KILLING leader pid {leader_pid} (the pid it PRINTED)")
        t_kill = time.time()
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", leader_pid],
                           capture_output=True)
        else:
            os.kill(int(leader_pid), 9)

        for _ in range(30):
            time.sleep(1)
            promo = [(t, l) for t, l in lines if l.startswith("PROMOTED")]
            if promo:
                took = promo[0][0] - t_kill
                print(f"\n  PROMOTED after {took:.1f}s — pid "
                      f"{promo[0][1].split()[1]}")
                print(f"  processes promoted : {len(promo)} "
                      f"(more than one would mean duplicate scheduled work)")
                print(f"  advisory locks held: {_lock_holders()}")
                if len(promo) > 1:
                    print("\n  FAIL: split brain — two leaders run the "
                          "singletons, so dunning mail and meetings duplicate")
                    return 1
                print("\n  RESULT: leader death is survived without a restart. "
                      "Background singletons resume.")
                return 0
        print(f"\n  FAIL: no promotion within 30s. Every survivor is still a "
              f"follower and the singletons are stopped — the July outage.")
        return 1
    finally:
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        print("  workers terminated")


if __name__ == "__main__":
    raise SystemExit(main())
