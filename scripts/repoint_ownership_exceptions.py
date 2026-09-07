"""Re-point live governance work whose owner was a fallback, once the real
authority becomes eligible.

WHY THIS EXISTS. `route` assigns the policy's approver when that authority has an
eligible owner, and falls back to the CEO with `ownership_exception=true` when it
does not. Both halves are correct. What nothing does is REVISIT the fallback once
the proper authority is granted an identity -- deliberately, because an exception
is meant to be visible and resolved by a person, and both `governance.metrics()`
and the alerts summary already report the count.

The gap this closes is procedural. After the CTO grant on 2026-09-06 the manual
step re-pointed `action_approvals` and not `governance_alerts`, so a high-severity
`event_orphaned` alert sat on the CEO's desk for five hours while policy said CTO.
Run this after ANY identity grant, and the omission cannot repeat.

    python -m scripts.repoint_ownership_exceptions            # dry run
    python -m scripts.repoint_ownership_exceptions --apply
    python -m scripts.repoint_ownership_exceptions --apply --target railway

Scope is deliberately narrow: only OPEN alerts, only where the exception flag is
set, only where the policy's authority is NOW eligible. Status, timestamps,
dedupe keys and transition history are untouched -- this changes whose desk the
work is on and nothing else. No decision is manufactured; the alert stays open
and unacknowledged, and a person still has to work it.
"""
import os
import sys

sys.path.insert(0, 'd:/a/crm_agent')
from dotenv import load_dotenv
load_dotenv('d:/a/crm_agent/.env')
import psycopg2

APPLY = '--apply' in sys.argv
if '--target' in sys.argv and 'railway' in sys.argv:
    _dsn = os.getenv('RAILWAY_DB_URL', '').strip()
    print('TARGET: RAILWAY')
else:
    from app.core.config import get_settings
    _dsn = get_settings().db_dsn
    print('TARGET: LOCAL')
c = psycopg2.connect(_dsn)
c.autocommit = False
cur = c.cursor()

SEL = """
SELECT a.alert_id::text, a.rule, a.severity, a.status,
       a.accountable_owner, a.ownership_exception,
       p.approver_role AS policy_owner_role,
       e.role_code || ' ' || e.full_name AS should_be,
       COALESCE(e.owner_id, e.employee_uuid)::text AS should_be_id,
       to_char(a.created_at AT TIME ZONE 'UTC','MM-DD HH24:MI') AS opened,
       to_char(a.due_at   AT TIME ZONE 'UTC','MM-DD HH24:MI') AS due
  FROM governance_alerts a
  JOIN governance_action_policies p
    ON p.action_type = 'alert:' || a.rule AND p.kind = 'alert'
  JOIN executives e
    ON e.role_code = p.approver_role AND e.is_active
 WHERE a.status = 'open'
   AND a.ownership_exception
   AND fn_owner_eligible(COALESCE(e.owner_id, e.employee_uuid))
"""

cur.execute(SEL)
rows = cur.fetchall()
print(f'candidates: {len(rows)}\n')
for r in rows:
    print(f'  alert      : {r[0]}')
    print(f'  rule       : {r[1]}   severity={r[2]}   status={r[3]}')
    print(f'  owned by   : {r[4]}   ownership_exception={r[5]}')
    print(f'  policy says: {r[6]}  ->  {r[7]}')
    print(f'  opened     : {r[9]} UTC   due: {r[10]} UTC')

if not rows:
    print('nothing to correct')
    sys.exit(0)

if not APPLY:
    print('\n(dry run — pass --apply to write)')
    sys.exit(0)

cur.execute("""
UPDATE governance_alerts a
   SET accountable_owner_id = e.oid,
       accountable_owner    = e.label,
       ownership_exception  = false,
       updated_at           = now()
  FROM governance_action_policies p,
       LATERAL (SELECT COALESCE(x.owner_id, x.employee_uuid) AS oid,
                       x.role_code || ' ' || x.full_name     AS label
                  FROM executives x
                 WHERE x.role_code = p.approver_role AND x.is_active) e
 WHERE p.action_type = 'alert:' || a.rule AND p.kind = 'alert'
   AND a.status = 'open'
   AND a.ownership_exception
   AND fn_owner_eligible(e.oid)
""")
print(f'\nupdated {cur.rowcount} alert(s)')
c.commit()

cur.execute("""SELECT rule, status, accountable_owner, ownership_exception,
                      to_char(created_at AT TIME ZONE 'UTC','MM-DD HH24:MI'),
                      to_char(due_at    AT TIME ZONE 'UTC','MM-DD HH24:MI'),
                      acknowledged_at IS NOT NULL
                 FROM governance_alerts""")
print('\nafter:')
for r in cur.fetchall():
    print(f'  {r[0]}  status={r[1]}  owner={r[2]}  exception={r[3]}')
    print(f'  opened={r[4]} UTC  due={r[5]} UTC  acknowledged={r[6]}')
c.close()
