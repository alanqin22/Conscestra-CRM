"""Every object the application creates at RUNTIME must already exist.

WHY THIS EXISTS
---------------
Several modules create their tables at first use with `CREATE TABLE IF NOT
EXISTS`. That worked while the app connected as a superuser. It cannot work now:
`crm_app` has USAGE but not CREATE on `public`, and PostgreSQL checks CREATE
permission BEFORE the IF NOT EXISTS short-circuit — so the statement fails even
when the table is already there.

The consequence is not loud. `deploy_state.ensure_table()` logged the permission
error at warning and returned False, so `replica_attestations` recorded nothing
from 2026-08-03 until it was noticed two days later. Nothing else reported a
problem, because from the application's point of view nothing had gone wrong: it
had asked for a table, been refused, and carried on.

Measured 2026-08-05: every such object DOES currently exist on both databases,
because they were all created before the privilege separation. So this is not a
backlog of damage — it is a trap for the NEXT one. A developer adds a module
with a lazy CREATE, it works on their laptop where they own the schema, and it
is inert in production.

WHY THERE IS NO MANIFEST
------------------------
A declared list of expected objects would drift from the code that creates them,
and the drift would be invisible in exactly the case that matters — someone adds
a new lazy CREATE and forgets the list. So the SOURCE is the manifest: this
scans for the CREATE statements themselves and checks each object. A new lazy
CREATE is picked up the first time this runs, without anyone remembering to
declare it.

    python -m scripts.verify_runtime_ddl          # uses DB_DSN / DATABASE_URL
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import config as _config          # noqa: E402,F401  (loads .env)

import psycopg2                                  # noqa: E402

TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?:public\.)?[\"']?(\w+)", re.I)
INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+[\"']?(\w+)", re.I)


def scan() -> Dict[str, List[Tuple[str, str]]]:
    """{object_name: [(kind, source_file), ...]} for every lazy CREATE in app/."""
    found: Dict[str, List[Tuple[str, str]]] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:                                       # noqa: BLE001
            continue
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for kind, rx in (("table", TABLE_RE), ("index", INDEX_RE)):
            for name in rx.findall(text):
                found.setdefault(name, []).append((kind, rel))
    return found


def main() -> int:
    dsn = (os.getenv("DATABASE_URL") or os.getenv("DB_DSN") or "").strip()
    if not dsn:
        print("no DATABASE_URL or DB_DSN configured", file=sys.stderr)
        return 2

    objects = scan()
    if not objects:
        # Zero findings is suspicious, not clean: the regex may have rotted.
        print("NO lazy CREATE statements found in app/ — either they are all "
              "gone (good) or this scanner no longer matches them (bad). "
              "Verify by hand before trusting this result.", file=sys.stderr)
        return 1

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    missing: List[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, "
                        "has_schema_privilege(current_user,'public','CREATE')")
            who, can_create = cur.fetchone()
            print(f"runtime-DDL audit — connected as '{who}', "
                  f"CREATE on public: {can_create}")
            print(f"  {len(objects)} object(s) created lazily by app code\n")
            for name in sorted(objects):
                cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
                exists = cur.fetchone()[0] is not None
                kind, src = objects[name][0]
                mark = "ok  " if exists else "GONE"
                print(f"  {mark} {kind:5} {name:30} {src}")
                if not exists:
                    missing.append(f"{name} ({src})")
    finally:
        conn.close()

    print()
    if missing:
        print(f"MISSING {len(missing)} object(s) the application expects to "
              f"create itself:")
        for m in missing:
            print(f"    {m}")
        print("\nUnder the non-superuser app role these will NOT be created at "
              "runtime. The lazy CREATE will be refused, the module will log and "
              "carry on, and the feature will be silently inert. Declare each in "
              "a migration under sql/ and apply it.")
        return 1

    if not can_create:
        print("all present — and this role cannot CREATE, so the lazy statements "
              "are decorative here. That is the intended state: the objects come "
              "from migrations, not from the application.")
    else:
        print("all present. NOTE: this role CAN create, so a missing object "
              "would have been silently created by this very check on a "
              "privileged connection. Re-run as the application role to prove "
              "anything about production.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
