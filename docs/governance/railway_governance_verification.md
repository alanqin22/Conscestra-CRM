# Railway Governance Verification Procedure

**Nothing in this document has been executed.** Railway holds none of the three
activation migrations and none of the new code. This is the procedure to follow *after*
a human authorises the deployment, and the evidence that must exist before the word
"production verified" is used.

Companion to `docs/governance/activation_plan.md` and
`docs/governance/five_authority_readiness.md`. `$APP` is the Railway base URL, `$T` the
admin token, `$DB` the Railway DSN.

---

## 0. Railway baseline, measured 2026-09-05 (re-measure before deploying)

| Fact | Value | Why it matters here |
|---|---|---|
| Activation tables present | **0 of 3** | nothing has been applied |
| Activation columns on `action_approvals` | **0 of 4** | ditto |
| `schema_migrations` rows | 42 | unchanged by this work; all three files are out-of-band |
| Executives | CEO, CRO, CFO, COO — **no CTO** | the CTO must be created there before its policies mean anything |
| Executive credentials | **0** | nobody can decide in the console until §3 |
| COO name | **Yongmei Qin** (local says Alex Zhou) | one is stale; an approval would name the wrong person |
| `action_approvals` | 5 executed, 59 expired, 2 pending | the 59 are history and must stay `expired`, not be revived |
| `event_queue` | 1,266 completed, **39 pending** | the first bus tick after deploy will mark these `orphaned` and open one CEO-owned alert — expected, not an incident |

## 1. Readiness gate — do not deploy until every line is true

- [ ] The three identity tests pass locally after the CTO grant (`test_31`, `J0`, `J1`)
- [ ] `python -m pytest -q governance/tests/test_governance_activation.py` — 67 passed
- [ ] Full local suite green apart from the four documented pre-existing failures
- [ ] `python -m scripts.migrate --check` — "schema is current" locally
- [ ] COO name reconciled between the two databases
- [ ] A CTO executive row exists on Railway (§3 of the identity procedure)
- [ ] `GOV_ROUTE_EMAIL=1` and `GOV_LINK_SECRET` set on Railway, and each executive's
      `auto_email_enabled` is true — otherwise escalation is silent, which is the
      failure this work exists to remove
- [ ] `GOV_REESCALATE_HOURS` decided (default 24)
- [ ] A rollback window agreed (§7) and someone available to use it

## 2. Deployment sequence

```bash
# 2.1 migrations, IN ORDER. Each is idempotent and prints its own self-check.
python -m scripts.apply_sql governance/sql/governance_activation.sql       --target railway
python -m scripts.apply_sql governance/sql/governance_five_authorities.sql --target railway
python -m scripts.apply_sql governance/sql/governance_reescalation.sql     --target railway
```

Read the NOTICEs rather than the exit code. Expect: `16 action policies seeded`;
`eligible CEO owner rows: 1`; a WARNING if Railway still has no CTO; the reescalation
file naming how many escalated rows it stamped. A WARNING about the CEO is a **stop**:
without an eligible CEO, `propose()` raises rather than writing an unowned proposal, and
the queue stops accepting work.

```bash
# 2.2 schema state
python -m scripts.migrate --check --target railway     # expect "schema is current"
# the three files stay OUT_OF_BAND until this deployment is verified; promoting them to
# REQUIRED_MIGRATIONS is step 8, not step 2.
```

```bash
# 2.3 deploy the code (merge to master; the push auto-deploys), then confirm the
#     FEATURE, never the commit SHA — a matching SHA has been true while the change
#     was missing before.
curl -s $APP/health | jq '{commit, database, ha}'
curl -s -H "X-Admin-Token: $T" $APP/governance/status | jq '{confidence_grants_authority, policy_table, authorities, sla_hours}'
# expect: confidence_grants_authority=false, policy_table=true, five authorities, 48
```

## 3. Identity activation on Railway

Follow `docs/governance/executive_identity_activation.md` §3 and §4 against `$APP`/`$DB`.
Then:

```bash
curl -s -H "X-Admin-Token: $T" $APP/governance/authorities | jq '{missing, without_credential}'
# expect: both empty
```

## 4. Post-deploy verification

```bash
python -m scripts.postdeploy_verify --target railway --app-url $APP
```
This is the existing gate (secrets, invariants, red team, DSAR coverage, runtime DDL,
schema drift) and must exit 0. It does not know about governance; §5–§9 below are what
covers this work.

## 5. Identity and authorisation — the invariant that matters most

| # | Check | Command | Expected |
|---|---|---|---|
| 5.1 | machine token is nobody | `curl -H "X-Admin-Token: $T" $APP/governance/whoami` | `executive: null`, `can_decide: false` |
| 5.2 | machine token cannot decide | `POST $APP/governance/approve/<id>` with the token | **403** naming the five authorities; the row stays `pending` |
| 5.3 | each executive can authenticate | sign in as each of the five | `whoami` returns their role, `can_decide: true` |
| 5.4 | governance ≠ administration | as an executive, `GET $APP/deploy/migrations` | **403** — verified locally on a `viewer`-role executive session |
| 5.5 | wrong authority refused | CFO decides a CRO item | **403** naming the assigned authority |
| 5.6 | CEO may decide anything | CEO decides a CRO item | 200 |
| 5.7 | body cannot choose identity | approve with `{"decided_by":"ceo@…"}` on a CFO session | recorded as the **CFO**, never the CEO |

## 6. Policy routing

```bash
curl -s -H "X-Admin-Token: $T" $APP/governance/action-policies | jq '
  {undeclared: .undeclared_write_capabilities,
   pending_decision: .decision_required,
   routing: [.actions[] | {action_type, action_class, approver_role, status}]}'
```
Expect technology and data → CTO (except `data.erase_record` → CEO), operations alerts →
COO, commercial → CRO, financial → CFO, `undeclared_write_capabilities: []`, and
`kb.publish` the only `decision_required` unless it has been decided by then.

## 7. Approval lifecycle, on a real proposal

Use a genuine low-stakes item from the queue, or raise one deliberately.

| Step | Expected evidence |
|---|---|
| approve as the routed authority | `status='executed'`, `verification.ok=true`, `decided_by`=their email, `decided_actor`=same, `decided_via='session'` |
| the same approval again | refused: "not pending" |
| two approvals at once | exactly one succeeds; one execution; verified locally with six concurrent threads |
| reject | `status='rejected'`, no execution |
| delegate (policy permitting) | `authority_role` moves, `due_at` unchanged, both roles in the reason |
| one-click link | works once; the second use says "Already decided"; `decided_actor='email-link'` |

```sql
-- the audit answer, per decision
SELECT action_type, decided_by AS authority, decided_actor AS person, decided_via,
       decision_mode, policy_version, decided_at, status,
       verification->>'ok' AS verified
  FROM action_approvals WHERE decided_at > now() - interval '1 day' ORDER BY decided_at DESC;
```

## 8. SLA, escalation and email

```sql
-- create a controlled breach on a test proposal rather than waiting 48 hours
UPDATE action_approvals SET due_at = now() - interval '1 minute'
 WHERE approval_uuid = '<test id>' AND status='pending';
```
```bash
curl -s -X POST -H "X-Admin-Token: $T" $APP/governance/sla-sweep | jq
```

| Expected | Where |
|---|---|
| `breached_at` and `escalated_at` set, `escalation_status='escalated'` | `action_approvals` |
| **status is still `pending`** — a breach is not a decision | same row |
| in-app notice to the original authority and to the CEO | `notifications` |
| email to both, carrying the one-click links | `staff_email_ledger`, and the info@ BCC archive |
| an `approval_breach` alert owned by the CEO | `governance_alerts` |
| nothing set to `expired` | `SELECT count(*) FROM action_approvals WHERE status='expired' AND decided_at > <deploy time>` → **0** |
| a reminder after `GOV_REESCALATE_HOURS`, numbered, and none before | `escalation_notices`, `last_escalation_notice_at` |

**Verify the email against the archive, not against the ledger.** A ledger row saying
`accepted` is the system's own account of itself; the info@ BCC archive is the
independent signal.

## 9. Event fabric

```bash
curl -s -H "X-Admin-Token: $T" $APP/agent-bus/status | jq '{this_process_role, cluster}'
```
The 39 pending rows should appear as `cluster.orphaned_durable` after the first tick, with
one `event_orphaned` alert owned by the CEO. Then, deliberately:

```bash
curl -s -X POST -H "X-Admin-Token: $T" "$APP/agent-bus/drain?max_total=200&since_days=30" | jq
```
Expect `replayed_orphans: 39`, a fall in `orphaned_remaining` to 0, an `audit_log` row
`replay_orphaned`, and the alert moving to `resolved`. **Closing it stays human** and
requires evidence — including whether the 18 orders that shipped without a notification
should now be notified or deliberately waived. That is a customer-facing decision, not a
queue operation.

## 10. Promotion and closure

```bash
# only after §4–§9 hold
# 1. move the three files from OUT_OF_BAND_SQL into REQUIRED_MIGRATIONS, in order,
#    in the same commit that records the Railway application
# 2. update the disposition pins in test_sql_disposition_governance.py
# 3. add test_governance_activation.py to scripts/verify_gate.py::CONTROL_TESTS
# 4. push the governance repo, then update .governance-pin, then push the public repo
python -m scripts.migrate --check --target railway
python -m scripts.control_inventory
```

## 11. Rollback

| Symptom | Action | Cost |
|---|---|---|
| queue refuses new proposals | an eligible CEO is missing — restore the membership; the refusal is correct | proposals are not written meanwhile |
| executives locked out | check `whoami`; the gate needs an `executives` row matching the sign-in email | the one-click links still work |
| reminder flood | raise `GOV_REESCALATE_HOURS`; each row is stamped before the send, so a restart cannot replay | none |
| governance code faulty | revert the deploy; the schema is additive and the previous code tolerates it | breach/escalation stops; **expiry does not resume** unless the old build is restored |
| a policy is wrong | change it in the console with a reason; it is versioned | none |

The schema needs no rollback: every change is additive, nullable or defaulted, and the
previous code ignores all of it. Reverting the code is the whole rollback.

## 12. What this procedure does not prove

- **Load.** Traffic on Railway is a handful of requests a day. Nothing here says the
  approval path holds under concurrency beyond the six-thread race test run locally.
- **That the queue will be worked.** Deployment makes the obligation visible and
  repeating; only people decide things. Ninety days of met SLA is the evidence that
  matters, and it cannot be produced on deployment day.
- **Delivery.** Provider acceptance is the strongest in-process signal; the BCC archive is
  the check. Neither proves a person read the mail.
