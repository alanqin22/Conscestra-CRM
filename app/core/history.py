"""The field-history writer — one implementation, for every object.

Extracted from app/core/cases.py (C1 Step 2) when routing policy became the
second thing worth auditing. The extraction, rather than a copy, is the point:
`record_field_history` already has an `entity` column precisely so more than one
object can use it, and a second writer would be a second set of rules about
what "before" means.

    audit_log             who did what, and when          (exists)
    provenance.py         where a value originally came from (exists)
    record_field_history  what the previous value WAS     (this)

THE TRANSACTION RULE: this takes the CALLER'S cursor. A helper that opened its
own connection would look tidier and would quietly break the guarantee that a
change and its history commit together or not at all.
"""

from __future__ import annotations

from typing import Any, Optional


def write(cur, entity: str, entity_id: str, field: str, old: Any, new: Any, *,
          actor: str, actor_id: Optional[str] = None, source: str = "") -> None:
    """Append one before/after record on the caller's open cursor.

    NULL survives as NULL: it means the field was PREVIOUSLY UNSET, which is
    different information from '' or 'unknown'."""
    cur.execute(
        """INSERT INTO record_field_history
             (entity, entity_id, field, old_value, new_value,
              actor, actor_id, source)
           VALUES (%s, %s::uuid, %s, %s, %s, %s, %s::uuid, %s)""",
        (entity, entity_id, field,
         None if old is None else str(old),
         None if new is None else str(new),
         (actor or "system")[:120], actor_id, (source or "")[:60]))


def read(entity: str, entity_id: str, limit: int = 100):
    """The provable chain for one record."""
    from app.core.database import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT field, old_value, new_value, actor, actor_id::text,
                          source, changed_at
                   FROM record_field_history
                   WHERE entity=%s AND entity_id=%s::uuid
                   ORDER BY changed_at, history_id
                   LIMIT %s""", (entity, entity_id, max(1, min(limit, 500))))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        if r.get("changed_at"):
            r["changed_at"] = r["changed_at"].isoformat()
    return rows
