# Conscestra CRM — Governance Operationalization & Activation Plan

**2026-09-05.** Baseline: `docs/architecture_assessment_2026-09-05.md`. Implementation
branch: `feat/owner-eligibility-and-employee-grants` (local, uncommitted at the time
of writing). Production (Railway) verified read-only before and during this work; nothing
here has been applied to Railway. Where Railway state is quoted it was measured today.

Labels: **FACT** (measured) · **INFERENCE** · **RISK** · **DECISION REQUIRED** (owner) ·
**HUMAN ACTION REQUIRED** (a person, not software) · status words `IMPLEMENTED` /
`TESTED` / `DEPLOYED` / `PRODUCTION VERIFIED`.

> **Revision 2 — 2026-09-05.** The owner settled §26 and changed the operating
> model: **five authorities (CEO, CRO, CFO, CTO, COO)**, not four with temporary
> CEO routing; **decisions bound to the authenticated executive**, not selected
> in a console; **email escalation immediately**, a paging channel later. The
> superseded four-authority text is not preserved — the second migration and its
> policy history are the record of what changed.
>
> **Revision 3 — 2026-09-05.** Closes the two conditions Revision 2 left open and
> adds the evidence stage:
> **(a)** governance authority is now separated from platform administration —
> the governance routers moved from the admin gate to `require_governance_actor`,
> so an executive with an ordinary application role can work their queue and
> still gets 403 on every admin endpoint (verified live);
> **(b)** an escalation now **repeats every `GOV_REESCALATE_HOURS` (24) until it
> is decided** — a third migration adds the counters, because five approvals had
> been escalated to the CEO and would never have been mentioned again;
> **(c)** fifteen adversarial lifecycle tests (races, replay, forgery, expired
> rows, missing identity) and three new companion documents —
> `five_authority_readiness.md`, `executive_identity_activation.md`,
> `railway_governance_verification.md`.

---

## 1. Purpose

Move Conscestra from *governance mechanisms exist* to *governance mechanisms actively govern*.
The assessment's central finding was "Mechanism: strong. Operation: absent." — proposals
expired unactioned, alerts had no owner, two guardrail layers were unconfigured, and the
only thing between a supervisor auto-action and execution was an editable confidence number.
This plan operationalises the invariant:

> No consequential AI action may proceed without an explicit governance decision, an
> accountable human owner, an authorized principal, and durable evidence of the decision
> and outcome — unless the action belongs to an explicitly declared policy class that is
> authorized for automatic execution.

## 2. Current governance state — activation audit (re-verified today)

| Control | Mechanism exists | Configured | Enforced | Operated | Evidence (2026-09-05) | Gap → status after this work |
|---|---|---|---|---|---|---|
| Approval queue (`action_approvals`) | Yes | Yes | Yes (writes propose) | **No** | Railway 59/66 expired, 2 human decisions ever; local 70/285 expired | ENFORCED BUT NOT OPERATED → SLA/breach/escalation + executive desk (`IMPLEMENTED`, `TESTED`) |
| Expiry | Yes | Yes (72h TTL) | Yes | — | `decided_by='system'` on every expired row | BROKEN semantics → retired; expiry is a breach (`IMPLEMENTED`, `TESTED`) |
| Confidence gate (`gov.act_min`) | Yes | Yes | Yes | — | supervisor AUTOACT at 0.75; one policy edit flips propose→act | BROKEN (authority from a number) → policy by action class; confidence only refuses (`IMPLEMENTED`, `TESTED`) |
| Approval atomicity | No | — | — | — | read-check-then-UPDATE, 3 transactions | MISSING → atomic claim + lease + stranded recovery (`IMPLEMENTED`, `TESTED`) |
| Approval authority check | No | — | — | — | any admin string accepted as `decided_by` | MISSING → five authorities enforced, and over HTTP bound to the signed-in executive (`IMPLEMENTED`, `TESTED`) |
| Executive routing | Yes | Yes (keyword affinity) | Yes | Partly | `route_approval` → CFO/CRO/COO/CEO; 22 approval_routed notices | IMPLEMENTED BUT NOT POLICY → reads `governance_action_policies` (`IMPLEMENTED`) |
| Capability registry (closed by default) | Yes | Yes (45/45) | Yes | Yes | Railway 45 rows, 0 disabled | ACTIVE |
| `allowed_callers` (agent RBAC) | Yes | **No** (NULL 45/45) | — | — | both DBs | CONFIGURED BUT UNUSED → seed is a **HUMAN ACTION** (§20) |
| `agent_capability_grants` (U4) | Yes | No (0 rows) | Yes | No | both DBs | CONFIGURED BUT UNUSED (no authored agent acts) — leave |
| `governance_policies` (numeric guardrails) | Yes | No (0 rows) | Yes | — | both DBs | UNUSED; defaults apply; now informational for act_min |
| Owner eligibility (E2) | Yes (read-side) | — | **No** | — | `grant()` enforces nothing; 39/51 Railway owners are contacts | IMPLEMENTED BUT NOT ENFORCED → `fn_owner_eligible` + triggers on governance objects (`IMPLEMENTED`, `TESTED`) |
| Supervisor alerts | Yes (events) | Yes | — | **No** | 109 alerts/30d, 0 owned, 0 closed | BROKEN as obligations → `governance_alerts` lifecycle (`IMPLEMENTED`, `TESTED`) |
| Bus stall detector | Yes | Yes | Yes | — | 24h lower bound; fired once then silent for 85h | BROKEN → age-unbounded + orphaned state (`IMPLEMENTED`, `TESTED`) |
| Orphaned events | Prospective count only | — | — | — | 39 pending 85h on Railway; 18 shipped orders unnotified | MISSING durable state → `orphaned` + owned alert + audited replay (`IMPLEMENTED`, `TESTED`) |
| Platform health | Yes | Yes | — | No | reported "critical" to no one | ACTIVE but unowned → governance metrics + alert states added (`IMPLEMENTED`) |
| CEO briefing | Yes | Yes | — | Partly | approvals section only | → "Governance today" section (`IMPLEMENTED`) |
| Field-level history | Yes (cases) | — | Cases only | — | 5 rows on Railway | Phase 4 — plan only (§21) |
| Principals on writes | Yes | Yes | Yes | Yes | all Railway write rows since 08-26 carry principal | ACTIVE |
| Audit outcome fidelity | Yes | Yes | Yes | Yes | outcome column live since 08-26 | ACTIVE |
| CI gate | Yes | Yes | Yes | Yes | 12 control files; cannot be skipped | ACTIVE (activation tests not yet in the gate — §22) |
| Deployment verification | Yes | Manual | — | Manual | postdeploy_verify not recorded per deploy | ENFORCED BUT NOT OPERATED — unchanged here |

**FACT — the five authorities, measured today.** CEO, CFO, CRO and COO have executive
rows and are eligible owners on both databases. **The CTO has a row locally but no
membership (not eligible), and no row at all on Railway.** No authority has a sign-in
credential on either database, which under D5 means the console cannot decide until one
exists — see §20.

## 3. Owner decisions (authoritative)

D1 Railway is a demo operated with production-grade governance · **D2 approval authority
= CEO, CRO, CFO, CTO, COO** · D3 48-hour decision SLA from submission · D4 breach →
escalate to the CEO; expiry is never a decision. Encoded as data or constraints, not
prose: `approver_role`/`escalation_role` CHECK over the five roles (D2), `sla_hours`
default 48 (D3), `sla_sweep()` + `escalation_role='CEO'` (D4), `release_guard`'s
deployed-environment detection unchanged (D1).

**D5 (new) — identity is bound, not selected.** A decision is recorded against the
executive the request is *authenticated as*. An administrator may read every desk and
decide on none. The one non-session path is the HMAC decision link, where possession of a
token mailed to that executive's own address is the authentication, and the row records
`decided_actor='email-link'` so it can never be read as a person at a keyboard.

**D7 (new, Revision 3) — governance authority is not platform administration.**
The governance routers are gated by `auth_dep.require_governance_actor`, which admits the
ops token, an admin session, or **an active executive's session whatever its application
role**, and nothing else. An executive credential no longer has to be an admin credential:
"you may approve a discount" stops implying "you may erase a customer". Verified live with
a `viewer`-role executive session — governance 200, `/deploy/migrations` 403.

**D8 (new, Revision 3) — an escalation is a standing obligation.** A breached, escalated
and still-undecided approval is re-announced every `GOV_REESCALATE_HOURS` (default 24),
numbered, to the escalation authority. **Acknowledgement is the decision**: there is no
"seen" control for an approval, because a reminder that can be dismissed without deciding
rebuilds the silence this work exists to remove. For an alert, acknowledging pauses the
reminders and its own lifecycle takes over.

**D6 — the seven §26 questions are answered:** CTO owns technology/data; COO owns
operations; `email.send_payment_reminder` stays SAMPLED_REVIEW at 10% under the CFO;
the CRO owns the `order.cancel` standing policy; enforced eligibility now with the
staff/contact split as target architecture; email now, paging later; bound identity as
above. The only class still unassigned is **knowledge** (`kb.publish`) — see §26.

## 4. Governance operating model

```
intent → principal → agent → capability (a2a) → POLICY (governance_action_policies)
   ├─ HUMAN_APPROVAL ──► proposal (owner, authority, due_at) → critic → routed desk
   │                      → decision by an AUTHORITY (atomic claim) → execution
   │                      → post-action verification → audit (approval.decided)
   │                      └─ 48h passed → BREACHED → escalated to CEO (alert + notice)
   ├─ SAMPLED_REVIEW ───► executes under a NAMED policy owner → ledgered
   │                      → sample_rate share opens a review work item
   └─ AUTO_EXECUTE ─────► executes under a NAMED policy owner → ledgered (policy:<owner>)
```
Every branch writes an `action_approvals` row; the decider category is truthful
(`user` / `token` / `policy` / `service`), and the accountable human is a separate column.

## 5. Ownership model

- `fn_owner_eligible(uuid)`: active `assignable_identity` row, not a `contacts` PK, not a
  service identity (`role='agent'` / `@system.internal`), not an identity collision.
- Enforced by trigger on `action_approvals.accountable_owner_id` (and NOT NULL for
  `pending`) and on `governance_alerts.accountable_owner_id` / `escalated_to_owner_id`.
- `resolve_accountable_owner(role)`: the authority's executive; if none is eligible, the
  CEO with `ownership_exception=true` and an `ownership_exception` alert. If no CEO is
  eligible either, `GovernanceConfigError` — the write does not happen.
- **Staff identity split from contacts (target):** deferred. The governing decision on
  record is P1-C (`docs/owner_eligibility_contract_gate.md`): `owners` stays a mixed
  population and eligibility is an explicit layer. This plan enforces that layer on every
  governance object. Splitting the table is **DECISION REQUIRED** (§26) and is not needed
  for the invariant above to hold.
- Business-record owner columns (orders 97.5% NULL on Railway) are unchanged here:
  backfilling from accounts is forbidden by an existing decision, and a NOT NULL would
  fail closed on 2,257 rows. Phase 1 exit criteria measure *new* rows.

## 6. Approval authority model

| Authority | Owns | Approve | Delegate | Receives SLA escalation | Local | Railway |
|---|---|---|---|---|---|---|
| CEO | enterprise / exceptional · every escalation | any proposal | if policy allows | **Yes** | row ✓ eligible ✓ sign-in ✗ | row ✓ eligible ✓ sign-in ✗ |
| CRO | revenue / commercial | own desk | if policy allows | no | row ✓ eligible ✓ sign-in ✗ | row ✓ eligible ✓ sign-in ✗ |
| CFO | financial / payment / accounting | own desk | if policy allows | no | row ✓ eligible ✓ sign-in ✗ | row ✓ eligible ✓ sign-in ✗ |
| CTO | technology / data / architecture / security | own desk | if policy allows | no | row ✓ **eligible ✗** sign-in ✗ | **no row** |
| COO | operations / fulfilment / operational policy | own desk | if policy allows | no | row ✓ eligible ✓ sign-in ✗ | row ✓ eligible ✓ sign-in ✗ |

**FACT (2026-09-05):** no authority has a sign-in credential on either database, and the
CTO has no membership row locally and no executive row on Railway. Under D5 that means
**the console cannot decide anything until executive credentials exist** — the queue is
worked through the one-click email links until then. This is the intended consequence of
binding identity, and it is the first human action in §20.

**The identity chain the model now asserts** (each link enforced, not assumed):

```
authenticated human  → session identifier (auth_sessions)
                     → executives.email          governance_policy.session_authority()
                     → authority role            CEO | CRO | CFO | CTO | COO   (CHECK)
                     → accountable owner id      fn_owner_eligible()           (trigger)
                     → action class policy       governance_action_policies    (versioned)
                     → decision                  atomic claim + execution token
                     → audit                     decided_by (authority) + decided_actor
                                                 (person) + approval.decided event
```

`_authority_check()` still resolves an authority for non-HTTP callers (the bus, the
scheduler, tests) and refuses "human", "admin", a non-executive email or an unlinked
Slack id. Over HTTP, `_bound_authority()` runs first and takes the identity from the
session; `decided_by` in the request body is ignored. Delegation requires
`delegation_allowed` and does not restart the clock; it records both roles.

## 7. 48-hour SLA

Columns on `action_approvals`: `sla_hours`, `due_at` (= `created_at` + policy SLA),
`breached_at`, `escalated_at`, `escalation_status ∈ {none, breached, escalated}`,
`alert_id`. Backfilled for existing rows. `expires_at` is written NULL for new rows and
no reader filters on it. `sla_sweep()` runs every 15 minutes on an interval trigger
(`governance_sla_sweep`, replaces `governance_expiry`). `GET /governance/my-approvals`
and the console show hours left, due-soon (< 12 h), breached, escalated.

## 8. CEO escalation

At `due_at`: `breached_at` set, `escalation_status='escalated'`, in-app notice to the
original authority (`approval.breached`) and to the CEO (`approval.escalated`), an
`approval_breach` work item owned by the CEO, and the row **stays `pending`**. The CEO
(or the original authority) decides it. Nothing is executed, rejected, deleted or
recycled. Second sweeps are idempotent.

## 9. Governance policy model

`governance_action_policies` (versioned by trigger; append-only history).

| Action type | Class | Mode | Approver → esc. | SLA | Auto | Owner | Status |
|---|---|---|---|---|---|---|---|
| email.send_payment_reminder | financial | SAMPLED_REVIEW (10%) | CFO → CEO | 48h | yes | CFO | **decision_required** (grandfathered: 150 auto-sends/14d on Railway) |
| supervisor.emit_dunning | financial | HUMAN_APPROVAL | CFO → CEO | 48h | no | — | active |
| quote.generate | financial | HUMAN_APPROVAL | CFO → CEO | 48h | no | — | active |
| campaign.winback, meeting.book, sms.send, contact.update_profile, supervisor.emit_hot_leads | commercial | HUMAN_APPROVAL | CRO → CEO | 48h | no | — | active |
| order.cancel | commercial | AUTO_EXECUTE | CRO → CEO | 48h | yes | CRO | **decision_required** (existing OTP standing policy) |
| kb.publish | knowledge | HUMAN_APPROVAL | CEO → CEO | 48h | no | — | **decision_required** — the one class with no named owner (§26) |
| tuning.adjust, scoring.activate | technology | HUMAN_APPROVAL | **CTO** → CEO | 48h | no | — | active |
| data.normalize_phones, data.merge_contacts, identity.materialize_link | data | HUMAN_APPROVAL | **CTO** → CEO | 48h | no | — | active |
| data.erase_record | data | HUMAN_APPROVAL | CEO → CEO | 48h | no | — | active (irreversible stays with the CEO) |
| alert:* (21 rules) | per rule | — | CFO 3 · CRO 6 · **CTO 6** · **COO 3** · CEO 3 → CEO | 24–48h | — | — | active |

Rules enforced by CHECK: `auto_execute` needs `policy_owner` and a non-human mode;
HUMAN_APPROVAL can never be auto; approver/escalation ∈ {CEO,CRO,CFO,CTO};
SAMPLED_REVIEW needs a sample rate. An action type with **no row fails closed** to
HUMAN_APPROVAL → CEO and is listed under `undeclared_write_capabilities`.

## 10. Agent governance

`a2a.dispatch` for a write: registry → allowed_callers → principal → params_schema →
**policy** (`may_auto_execute`) → HITL amount floor (still overrides a standing policy)
→ confidence floor (refuse below `propose_min`; never grants) → propose | execute →
post-action `_verify_execution` (dispatch audit row + capability-specific state for
order.cancel / kb.publish / campaign.winback) → audit event. Supervisor auto-actions use
the same `may_auto_execute` and ledger themselves under `policy:<owner>`.

## 11. Alert lifecycle

`governance_alerts`: class, rule, severity, source, headline, detail, affected object,
accountable owner (eligible, enforced), assignee, status, SLA/due, acknowledged/resolved/
closed (actor + timestamp), closure evidence (required to close), escalation, dedupe key,
correlation id. Transitions enforced by trigger:
`open → assigned → acknowledged → in_progress → resolved → closed`; `escalated` from any
live state; `cancelled` from any live state; `resolved → in_progress` (reopen). No DELETE.
Append-only `governance_alert_transitions`. Sources: supervisor rules (deduped per rule),
orphaned events, approval breaches, stranded executions, verification failures, sampled
reviews, ownership exceptions, manual.

## 12. Event-processing guarantees

Invariant: *every consequential event is either processed or remains visibly owned and
actionable.* Each bus tick marks pending, never-attempted, dispatchable rows created
before the cutoff as `status='orphaned'` (durable), opens one `event_orphaned` work item
for the batch, and emits `event.orphaned`. `detect_bus_stall` counts unclaimed rows at
**any age** plus orphaned rows. `POST /agent-bus/drain` replays orphaned rows (audit_log
`replay_orphaned` + `event.replayed`; the replay refuses to proceed if it cannot record
itself) and RESOLVES the orphan alert when none remain — closure stays human.
`/agent-bus/status` now carries a `cluster` block read from the database so a follower
worker no longer reports `running:false` as if it were the truth. Health distinguishes
healthy / delayed / orphaned / failed / escalated (§17).

## 13. Audit model

Per consequential action: correlation id (a2a → approval `_correlation_id` → trace),
intent, principal (kind + id), accountable owner, agent, capability, parameters,
policy decision (`decision_mode`, `policy_version`), authority, decider + via
(ui / email-link / chat / api / policy), decision timestamp, execution timestamp,
result, **verification** (checks + ok), breach/escalation timestamps, execution token.
Events: `approval.decided`, `approval.breached`, `approval.escalated`,
`approval.delegated`, `governance.alert_opened`, `governance.alert_escalated`,
`governance.policy_changed`, `event.orphaned`, `event.replayed` (audit-only, not queued).
Model/prompt version and before/after field state are **not** captured here — Phase 4.

## 14. Schema changes (`governance/sql/governance_activation.sql`, idempotent)

- `fn_owner_eligible(uuid)`; `executives.owner_id` (closes the one column drift).
- `governance_action_policies` + `governance_action_policy_history` (+ triggers).
- `action_approvals`: 16 columns (authority, owner, SLA, breach, escalation, execution
  token, verification, decided_via) + status CHECK + owner-eligibility trigger + indexes +
  backfill of `due_at`, `authority_role`, `accountable_owner_id` for live rows.
- `governance_alerts` + `governance_alert_transitions` (+ lifecycle / no-delete / append-only triggers).
- `event_queue`: `orphaned_at`, `alert_id`, `replayed_at`, `replayed_by`.
- 9 event types registered (`queue_enabled=false`).
- Disposition: `OUT_OF_BAND_SQL` PENDING DEPLOYMENT (promote to REQUIRED after Railway).

`governance/sql/governance_five_authorities.sql` (second file, same day, must follow the
first): widens both role CHECKs to five, moves technology/data to the CTO and operations
to the COO, confirms the two grandfathered standing policies, and adds
`action_approvals.decided_actor`. Every policy move passes through the versioning trigger,
so `governance_action_policy_history` carries the before/after of the owner's decision.

`governance/sql/governance_reescalation.sql` (third file, applies last): adds
`escalation_notices` and `last_escalation_notice_at` to `action_approvals` and
`governance_alerts`, with partial indexes, and stamps rows already escalated so applying it
does not re-announce everything at once.

Applied LOCALLY 2026-09-05 (`apply_sql`; schema attested `ccaa1ff63a9b39ed`,
`07b2c18e2e006378`, then `1183e844071722c0`). **None of the three is on Railway.**

## 15. API / backend changes

| Module | Change |
|---|---|
| `app/core/governance_policy.py` (new) | policy_for / alert_policy_for / may_auto_execute / authorities (with `has_credential`) / resolve_accountable_owner / decider_role / set_policy / **session_authority / executive_for_identifier / email_authority**; `GET /governance/action-policies`, `PUT …/{action_type}`, `GET /governance/authorities` (+ `without_credential`), `GET /governance/owner-eligibility/{uuid}`, **`GET /governance/whoami`** |
| `app/core/governance_alerts.py` (new) | open_alert / transition / escalate / sweep_sla / resolve_by_class / list / metrics; `GET /governance/alerts`, `POST /governance/alerts`, `POST /governance/alerts/{id}/{assign|acknowledge|start|resolve|close|cancel|reopen|escalate}`, `POST /governance/alerts/sweep` |
| `app/core/governance.py` | propose() writes owner/authority/SLA; route_approval reads policy; approve()/reject() authority-checked, atomic, verified; delegate(); sla_sweep(); expire_stale() retired; my_approvals(); metrics(); pending() breached-first, no expires filter; `GET /governance/my-approvals`, `/governance/work`, `/governance/metrics`, `POST /governance/sla-sweep`, `/governance/delegate/{id}`; **approve/reject/delegate take their identity from the session (`_bound_authority`) and 403 anything else**; breach and escalation now **email** the authority and the CEO with one-click links, ledgered per (approval, role) |
| `app/core/a2a.py` | write gate reads the policy; confidence can only refuse; policy executions ledgered (`_ledger_policy_execution`), sampled reviews opened |
| `app/core/supervisor.py` | `_govern_autoact` reads policy; auto-actions ledgered; `_emit_alert` opens an owned work item; `detect_bus_stall` age-unbounded + orphaned |
| `app/core/agent_bus.py` | `_mark_orphans_sync` each tick; `_replay_orphans_sync` in drain; `orphaned_sync` counts durable rows; cluster block on status |
| `app/core/platform_health.py` | queue states (durable orphaned, delayed) + governance metrics (breached, due soon, stranded, ownership exceptions, decision latency, verification failures, alerts) |
| `app/core/ceo_briefing.py` | "Governance today" section (text, HTML, per-role); approvals query no longer hides breached items |
| `app/core/transports.py` | Slack/Teams decisions pass `via="chat"`; refusals surfaced |
| `app/main.py` | `governance_sla_sweep` every 15 min (replaces `governance_expiry`); new routers under `_ADMIN` |
| `app/core/deploy_state.py` | disposition for the migration |

## 16. UI/UX changes (`governance-mgmt.html`, local-only file, user deploys)

- **My decisions**: a **whoami bar** stating which executive this session is and, when it
  is not one, that you may view every desk and decide on none (with the exact remedy).
  The role buttons choose **which desk to view** — never whose decision it is — and show
  all five with their eligibility and credential state. Tiles (pending / due <12h /
  breached / escalated to me), SLA pill per row, what-you-are-approving, owner + exception
  flag, critic stance. Approve / Reject / Delegate are **disabled** unless the session is
  an eligible executive, and the request body no longer carries an identity.
- **Alert Center**: filter by state; owner, due, severity, evidence, history; lifecycle
  buttons limited to legal transitions; closure prompts for evidence.
- **All governance work**: every pending proposal with authority, owner, mode, state.
- **Decision policies**: every action class and alert rule; Change… (reason required) and
  Confirm for `decision_required` rows; undeclared write capabilities listed.
- Header tiles now say "Confidence grants authority? NO" and the refuse-floor.
- Not built: a separate Action Detail page (detail is inline per row).

## 17. Monitoring

`GET /governance/metrics` and `GET /platform/health` → governance section:
pending, due_soon, breached_open, escalated_open, pending_ownership_exceptions,
in_flight, stranded, created_24h, approved_24h, auto_executed_24h, rejected_24h,
failed_24h, breached_7d, expired_7d, breach_rate_7d_pct, verification_failed_7d,
median/p95 decision hours, pending_by_authority, decisions_30d_by_decider; alerts:
live/open/escalated/past_due/ownership_exceptions/critical/opened_24h/resolved_24h/
closed_24h; queue: depth, orphaned (prospective + durable), delayed, drain rate, failed.
Not yet exported to a pager (the assessment's D-15) — see §26.

## 18. Executive reporting

The CEO briefing gains "GOVERNANCE TODAY — what leadership must know or decide":
approvals requiring attention (by authority), approaching 48h, SLA breaches and rate,
CEO escalations by id, unowned work held by the CEO as exception, stranded executions,
critical/escalated alerts, AI actions executed (approved / standing policy / rejected /
failed / awaiting), governance exceptions (unverifiable executions), decision latency.
Role briefings (CRO/CFO/COO) receive the section when it contains a breach or a stranded
execution; the CEO always receives it.

## 19. Operational policies (owner · authority · trigger · action · SLA · escalation · evidence · exception)

1. **Approval handling** — owner: each authority; trigger: proposal routed; action: decide in the console/email link; SLA 48h; escalation CEO; evidence `action_approvals` + `approval.decided`; exception: delegation where policy allows.
2. **48-hour SLA** — owner: CEO (policy), enforced by `sla_sweep`; trigger `due_at`; action breach + escalate; evidence `breached_at`, alert; exception: per-class `sla_hours` (1–720h) changed with a reason.
3. **CEO escalation** — owner CEO; trigger breach; action decide or delegate; evidence `approval.escalated`, alert, **and an email to both the original authority and the CEO carrying the one-click links** (§26.6); no automatic resolution.
4. **Delegation** — owner: current authority or CEO; requires `delegation_allowed`; reason mandatory; clock does not restart; evidence `approval.delegated` with both roles.
4b. **Bound identity (D5)** — owner CEO; trigger: any decision; action: refuse anything not authenticated as an eligible executive; evidence `decided_by` + `decided_actor` + `decided_via`; exception: the HMAC link, recorded as a token rather than a person.
5. **Ownership** — owner CEO; trigger: any governance write; action: eligible owner or CEO-as-exception + alert; evidence `ownership_exception` rows; exception process: appoint the missing executive (CTO).
6. **Alert acknowledgement** — owner: the alert's owner; SLA per rule (24–48h); unacknowledged past due → escalated to CEO.
7. **Alert closure** — owner: the alert's owner; requires resolved_by and closure evidence; bus may resolve orphan alerts, never close.
8. **Event failure** — owner CEO (until CTO); trigger `failed`/`orphaned`; action: alert, drain or cancel with reason; evidence event_queue columns + audit_log.
9. **Event replay** — owner CEO (until CTO); trigger orphan alert; action `POST /agent-bus/drain`; evidence `replay_orphaned` audit row + `event.replayed`; refused if it cannot be recorded.
10. **Automatic execution** — owner: the named `policy_owner`; trigger AUTO_EXECUTE/SAMPLED_REVIEW row; evidence `policy:<owner>` ledger rows; exception: HITL amount floor forces human approval.
11. **Irreversible actions** — owner CEO; `reversible=false` shown to the approver; `data.erase_record` CEO-only; exception: none.
12. **Emergency intervention** — owner CEO; action: disable the capability in the registry (`POST /a2a/registry/{intent}`) or set `decision_mode=HUMAN_APPROVAL`; evidence registry + policy history.
13. **Governance override** — there is no bypass of authority or ownership; `govern_bypass` exists only for re-dispatching an approved row and is not callable over HTTP.
14. **Audit retention** — approval rows are archived to `governed_deletions` on clearance; alert and policy history are append-only; retention beyond that unchanged (`retention.py`).
15. **Incident escalation** — `docs/runbook_incident_escalation.md` unchanged; platform_health "critical" now maps to an owned alert.

## 20. Human actions required

**Revision 5, 2026-09-06 (end of day).** Items 1–4 are all done, on **both** databases,
and deployed. What remains of this whole plan is one decision (§26.1) and elapsed time
(§27). Audited against the running system, not against this document — see §28.

1. ~~**Create a sign-in credential for each of the five executives**~~ — **all five done,
   on both databases.** Four locally 2026-09-05, the fifth (COO) 2026-09-06, and all five
   on Railway the same day. Every executive has since set their own password through the
   deployed self-service flow. Each holds `access_role='viewer'`. **None is an admin and none needs to be:** the
   governance gate admits an executive on the strength of the `executives` row alone.
   Verified live — an executive session reaches `/governance/whoami` with
   `can_decide=true` and is refused 403 on `/executives`, `/agent-bus/status`,
   `/supervisor/status`, `/corpus-provenance/summary` and `/demo/status`.

   Each password was generated at random and **discarded unread**; every executive sets
   their own through **Reset password** on `auth.html`. Read §20.4 before telling anyone
   to do that.

   The fifth is withheld — see §20.3.

2. ~~**Give the CTO an owner identity.**~~ **Done 2026-09-05** under the CEO's written
   authorisation: membership, owner `585f003c-679e-4101-a6f9-13c12310670d`, the link on
   both `owner_id` and `employee_uuid`, and a credential. The eleven policy classes moved
   off the CEO's exception queue, and the three controls that were failing truthfully —
   `test_assignable_identity::test_31`, `test_work_ownership::test_J0`/`::test_J1` — now
   pass **without having been modified**.

   The CEO also attested that Bill Wang is a real person. That statement is recorded in
   `corpus_provenance` where the system reads it, not left in a chat log, and it moved
   `eligible_production` from 0 to 1 — the first owner on this corpus that can be proven
   to be a real human.

   **On Railway the CTO executive row now exists, but the activation migrations do not**,
   so `owner_id` and `fn_owner_eligible` are absent there. Run the grant *after* the
   migrations.

3. ~~**Supply Alex Zhou's own email address.**~~ **DONE 2026-09-06 — and the premise was
   wrong.** Alex Zhou and Yongmei Qin are the same person. The rename had been applied to
   `executives.full_name` alone; it was completed in place across the owner row and the
   directory membership, on the one uuid `e4d99e38…`. The 48 activities and 2
   opportunities never moved, and the `owners` and directory counts stayed at 45 and 5,
   which is the evidence it was a rename rather than a succession. The fifth credential
   followed, so **all five authorities can now sign in and decide**.

   ```bash
   python -m scripts.provision_executive --role COO --name "Alex Zhou" \
       --email coo@agentorc.ca --by "Alan Qin (CEO)" \
       --i-am-renaming-the-same-person --credential --apply
   ```

   > **Superseded, kept deliberately.** This entry previously read: *the COO office has
   > changed hands; `executives.COO` names Alex Zhou but points at the previous
   > post-holder's owner row, so a credential on the role mailbox would stamp Alex Zhou's
   > decisions with Yongmei Qin's owner id.* That was built on a misreading, and it is
   > left here because of how it was caught. Run without
   > `--i-am-renaming-the-same-person`, `provision_executive.py` **refused**, naming the
   > 50 records it would have reattributed, and that refusal is what turned an assumption
   > into a question somebody had to answer. A script that had quietly done the obvious
   > thing would have been right by luck here and silently wrong the first time an office
   > really does change hands. The succession branch stays for that day.

   Still open: nobody has stated whether the COO is a real person, so unlike the CTO there
   is no `corpus_provenance` attestation and they do not count toward
   `eligible_production`. A rename is not evidence of realness.

4. ~~**Authorise deployment of the password-reset fix**~~ — **DEPLOYED 2026-09-06** and
   verified against the running host: a real identifier and an invented one both return
   `reset_token: null` and the same sentence. The original entry follows.

   ~~Authorise deployment of the password-reset fix~~, independently of the four
   migrations. `POST /auth/password-reset/request` takes no authentication and returned a
   working reset token in its response body for any real account — verified locally, and
   present in the commit Railway reports as deployed. It became critical the moment
   individual executive credentials existed, because a forged reset produces a *genuine*
   executive session and therefore an approval that is indistinguishable in the audit
   trail from a real one. Fixed, tested, mutation-checked, **not pushed**. Full account in
   `five_authority_readiness.md` §12.


## 21. Implementation phases

| Phase | Objective | Status | Production evidence required |
|---|---|---|---|
| 0 Safety & posture | credential rotation; posture decision | HUMAN ACTION / DECISION REQUIRED | `.env` without superuser DSN; `/auth-health.security_mode` per decision |
| 1 Accountable ownership | eligibility enforced on governance objects; exceptions visible | `IMPLEMENTED`, `TESTED` (local) | 100% of new proposals/alerts carry an eligible owner; `/governance/authorities` shows no missing role, or exceptions counted |
| 2 Event reliability | orphaned state, owned alert, audited replay, age-unbounded detector | `IMPLEMENTED`, `TESTED` (local) | Railway: 39 rows → orphaned → alert → drained → alert resolved/closed; `queue_orphaned_durable=0` for 14 days |
| 3 Governance operations | policy by class, authority, 48h, escalation, atomic execution, desk UI, briefing | `IMPLEMENTED`, `TESTED` (local) | breach rate < 10%; 0 stranded; every executed row names an authority or a policy owner; decisions by executive emails only |
| 3b Identity + separation | bound identity, governance-vs-admin gate, repeating escalation, adversarial coverage | `IMPLEMENTED`, `TESTED` (local); **BLOCKED on executive credentials and the CTO grant** | every `decided_actor` an executive identity; no executive holds admin solely to decide; no pending approval with `ownership_exception` |
| 4 Mutation/audit integrity | one mutation primitive per entity, field history everywhere, actor via `SET LOCAL app.principal`, model/prompt version on AI actions | **PLAN ONLY** — not started, by design: Phase 3 must be production-verified first | `record_field_history` rows/day > 0 per entity |

## 22. Test plan

`governance/tests/test_governance_activation.py` — **67 tests**, all passing locally.
Revision 3 added fifteen adversarial cases: approve/reject, approve/delegate and
delegate/delegate races; one-click replay, a forged token and a token reused for the other
action; a legacy `expired` row can never execute; an escalated proposal stays decidable by
both authorities; proposal creation fails closed with no eligible owner; the governance
gate admits an executive and refuses a non-executive viewer; the admin gate still stamps
the session bound identity depends on; and the reminder fires after its window, once, and
stops on a decision.
Revision 2 added: the five roles are admissible and a sixth is refused by the database;
technology/data route to the CTO and operations to the COO; the CEO's eligibility is a
hard precondition while another authority's absence must degrade *visibly*; an authority
without a credential is reported; a machine token and a non-executive admin session are
both refused by `_bound_authority`; an executive session resolves to its authority; the
endpoint cannot take its identity from the body; the actor is recorded beside the
authority; the email link records a token, not a person. The original coverage:
ownership (7: customer/service ineligible, authorities eligible, alert + proposal
constraints named, CEO-as-exception, propose writes owner/authority/clock);
policy-not-confidence (9: HUMAN proposes at 0.99, act_min lowered changes nothing,
auto needs owner, HUMAN never auto, COO inadmissible, declared AUTO executes and is
ledgered to the policy owner, undeclared fails closed, versioned history append-only,
reason required, supervisor gate reads policy); approval (10: "human" refused, admin
email refused, wrong authority refused, routed authority approves + verification +
event, CEO approves anything, role code accepted, rejected cannot execute, second
approval refused, **six concurrent approvals execute once**, stranded recovered +
alerted, delegation needs policy); SLA (3: breach → escalate → still decidable, nothing
expires, breached first and visible to the CEO); alerts (6: owner + deadline, lifecycle
order enforced, closure needs evidence, no delete, redetection folds + raises severity,
overdue escalates to CEO, supervisor signal opens owned alert); events (4: detector sees
3-day-old event, orphaned + owned + still detected, replay audited, orphaned never
claimed as ordinary work).

Wider suite: run locally with the four pre-existing failures from Appendix A of the
assessment deselected (they predate this work). Gate: `test_governance_activation.py`
should be added to `scripts/verify_gate.py::CONTROL_TESTS` once the migration is in
REQUIRED_MIGRATIONS (the gate builds from the chain, so it cannot precede promotion).

## 23. Production verification (HUMAN ACTION — Railway)

```
# 1. apply BOTH migrations, in order (idempotent; each prints its self-check)
python -m scripts.apply_sql governance/sql/governance_activation.sql --target railway
python -m scripts.apply_sql governance/sql/governance_five_authorities.sql --target railway
#    the second WARNs if Railway still has no CTO executive row — create it, then re-run
# 2. merge + deploy (master push auto-deploys); confirm the FEATURE, not the SHA:
curl -H "X-Admin-Token: $T" $APP/governance/status          # confidence_grants_authority=false, policy_table=true
curl -H "X-Admin-Token: $T" $APP/governance/authorities     # five roles; who is not eligible, who has no sign-in
curl -H "X-Admin-Token: $T" $APP/governance/whoami          # a TOKEN caller: executive=null, can_decide=false
#    then, signed in as an executive: can_decide=true, and a decision records
#    decided_by=<their email>, decided_actor=<same>, decided_via='session'
curl -H "X-Admin-Token: $T" $APP/agent-bus/status           # cluster.orphaned_durable (39 after first tick)
curl -H "X-Admin-Token: $T" $APP/governance/alerts          # event_orphaned alert owned by CEO
# 3. approval end to end: create a demo proposal → desk → approve as an authority → verify
#    /trace/{cid} shows approval.decided with authority; row has verification.ok
# 4. SLA: PUT a 1h sla on a test policy, propose, wait/force POST /governance/sla-sweep →
#    breached_at, escalated, CEO notice, approval_breach alert; then decide it as CEO
# 5. ownership: POST /governance/alerts with a contact owner id → 409 constraint named
# 6. events: POST /agent-bus/drain → replayed_orphans=39, alerts_resolved≥1; close with evidence
# 7. promote governance_activation.sql to REQUIRED_MIGRATIONS in the same change that
#    records the Railway apply; python -m scripts.migrate --check --target railway
```

## 24. Readiness gates

- **Governance Development Ready** — schema, policy, UI, API, tests implemented: **met locally**.
- **Governance Test Ready** — activation suite green (41/41) incl. race, SLA, ownership, escalation, orphan: **met locally**; wider suite: see §22.
- **Governance Operational Ready** — **met in constitution, NOT in use.** All five
  executives can sign in and decide on both databases; `/governance/authorities` reports
  empty `missing`, `without_credential` and `identity_mismatch`. But **no executive has yet
  decided anything in production**: `decided_actor IS NOT NULL` counts **0** on Railway
  (9 locally, from tests and probes). There are also 0 pending proposals there, so nothing
  is currently waiting on them. The console is constituted and unexercised.
- **Governance Production Verified** — **deployed 2026-09-06; the 14-day window starts
  now.** Measured on day 0: 0 pending proposals, 0 missing an owner / authority / due date,
  0 rows expired since the deploy, 1 governance alert opened by the machinery itself.
  Criteria: 100% new proposals with eligible owner and SLA timestamps; breaches escalate to CEO **and email**; no silent expiry; 0 stranded; 0 unowned consequential alerts; orphaned events visible; auto actions have explicit policy; every consequential action auditable; **and every console decision bound to an authenticated executive — `decided_actor` never null, never an administrator standing in for an executive**.
- **World-Class Governance Ready** — not claimable: requires 90 days of SLA compliance, external reconstruction, replayable decisions, independent assurance.

## 25. Rollback plan

Code: revert the branch; the schema is additive and tolerated by the previous code
(new columns nullable or defaulted; `expires_at` still present). Data: pending rows keep
`due_at`; nothing was expired, so nothing needs restoring. Migration: no destructive
statement; leaving the tables in place is safe. Triggers: `DROP TRIGGER
trg_action_approvals_owner_eligible ON action_approvals` re-opens ownerless pending
rows if that is ever needed (record why). Policy table: `decision_mode='HUMAN_APPROVAL'`
everywhere is the safe state and is one UPDATE with a reason.

## 26. Decisions — settled and still open

**Settled by the owner, 2026-09-05, and implemented** (the numbering follows the original
list so the history reads straight):

| # | Question | Decision | Where it now lives |
|---|---|---|---|
| 1 | CTO | **Active authority** for technology / data / architecture / security | 11 policy rows; role CHECK |
| 2 | COO | **Active authority** for operations | 3 alert rules; role CHECK |
| 3 | `email.send_payment_reminder` | SAMPLED_REVIEW 10%, CFO owns it | policy row, status active |
| 4 | `order.cancel` | CRO owns the standing policy | policy row, status active |
| 5 | Staff identity | Enforced eligibility **now**; clean staff/contact separation as target architecture | `fn_owner_eligible` + triggers; split deferred to Phase 4 |
| 6 | Pager | **Email immediately**; a real paging channel afterwards | `email_authority()` on breach + escalation |
| 7 | Console identity | **Bound to the authenticated executive**; no free authority selection | `_bound_authority`, `/governance/whoami` |

**Settled in Revision 3:**

| # | Question | Decision | Where it lives |
|---|---|---|---|
| 3 | Executive credential model | **Resolved, not deferred.** Governance access comes from the `executives` row, not from an admin role | `require_governance_actor`; executives are created with an ordinary application role |
| 2b | Escalations that go unread | **Resolved.** Repeating, numbered reminders until a decision exists; acknowledgement of an approval *is* the decision | `sla_sweep` REMIND pass; `remind_escalated` |

**Settled 2026-09-05 (owner):**

| # | Question | Decision | Where it lives |
|---|---|---|---|
| 1 | `kb.publish` | **CRO · HUMAN_APPROVAL · 48h · at most 3 proposals per day** | `governance_kb_publish_policy.sql`; policy v2, history records CEO→CRO |

**Zero action classes now carry `decision_required`.** The daily cap is a new governance
primitive (`daily_proposal_cap`): a statement about how much of one executive's day a
class may consume, versioned in the row that names them, enforced in `propose()`, and
**never silent** — a capped candidate raises `ProposalCapReached`, the producer counts it
as deferred, and the first refusal each day opens a low-severity work item owned by that
authority. Six producers were made cap-aware; `a2a.dispatch` returns a structured
`REJECTED` result rather than letting the refusal escape as an exception.

**Settled 2026-09-05 (owner), and executed:**

| # | Question | Decision | Where it lives |
|---|---|---|---|
| 3 | CTO identity | **Granted.** Bill Wang is a real person; the CEO authorised the grant and attested it | membership + owner `585f003c…` + credential; `corpus_provenance` attestation |
| 4 | Executive credentials | **Four issued**, own addresses, `access_role='viewer'`, none admin | `auth_credentials`; verified 403 on five admin surfaces |

**Still open — DECISION REQUIRED:**

1. **Paging destination beyond email.** Minimum next step: route `severity='critical'` and
   CEO escalations to SMS or push through the existing transports, reusing
   `email_authority`'s ledger shape so idempotency and the attention budget still apply. A
   delivery decision, not a model change.
5. ~~The COO's own email address.~~ **Settled 2026-09-06 (owner): same person, renamed.**
   Reconciled in place on one identity; all five authorities now hold a credential and
   `/governance/authorities` reports empty `missing`, `without_credential` and
   `identity_mismatch`. ~~Open sub-item: the COO is not attested.~~ **Closed 2026-09-06:
   the CEO attested all five executives as real people, on both databases.
   `eligible_production` is 5 of 5.**
6. ~~**Deploy the password-reset fix?**~~ **DONE 2026-09-06** — deployed, and the closure
   verified against the running host rather than inferred from the commit. The original
   entry follows.

   ~~An unauthenticated caller could obtain a working
   reset token for any real account, including every executive and the platform
   administrator — verified locally, and present in the commit Railway reports as
   deployed. Fixed, tested and mutation-checked here; **not pushed**. It is one endpoint
   and does not depend on the four migrations, so it can go separately and sooner. The
   evidence, classified, is in `five_authority_readiness.md` §12.

## 27. Definition of Done

Done when Railway shows, for 14 consecutive days after deploy: every new proposal with
an eligible owner, an authority and a due date; zero rows set to `expired`; every breach
escalated to the CEO within 15 minutes; zero rows stranded past the lease; zero
`orphaned` events older than 24 hours without an owned, acknowledged alert; every
executed action naming a human authority or a named policy owner; and the executives
using the console **signed in as themselves** — every `decided_actor` an executive
identity, none an administrator acting on someone's behalf, and none holding admin
privilege merely in order to decide. Until then this work is `IMPLEMENTED` and `TESTED`,
not `DEPLOYED` and not `PRODUCTION VERIFIED`.

**Companion documents (Revision 5):** `five_authority_readiness.md` — the evidence matrix
and what is blocked; `executive_identity_activation.md` — the exact identity procedure,
its verification and its rollback; `railway_governance_verification.md` — the deployment
gate and the production evidence that must exist before "production verified" is said.

## 28. Completion audit — 2026-09-06

Audited against the running systems, not against this document. Every "done" below was
checked by querying the database or calling the endpoint.

### Done

| § | Item | Evidence |
|---|---|---|
| 14 | Schema changes | all five SQL files applied to local **and** Railway; `migrate --check` reports **schema is current** on both |
| 15 | API / backend | governance routers behind `require_governance_actor`; `/governance/whoami`, `/governance/authorities`, SLA sweep every 15 min |
| 20.1 | Five executive credentials | 5 of 5 on both databases, all `viewer`, none admin; every executive set their own password through the deployed flow |
| 20.2 | CTO identity grant | membership + owner + credential + `fn_owner_eligible` true, on both |
| 20.3 | COO identity | half-applied rename reconciled in place on one uuid, on both; 57 records kept their owner on Railway |
| 20.4 | Password-reset fix | deployed; a real and an invented identifier both return `reset_token: null` |
| 26.1–26.7 | Owner decisions | all seven settled and implemented |
| 26.3 | CTO identity decision | granted and attested |
| 26.4 | Executive credentials | issued |
| 26.5 | COO | settled as a rename; all five now attested real |
| 26.6 | Deploy the reset fix | done and verified in production |
| — | Attestation | all five executives attested real by the CEO, both databases; `eligible_production` 5 of 5 |
| — | Promotion | the five files moved into `REQUIRED_MIGRATIONS` (41→46 declared, 238→233 out-of-band) after production executed them |
| — | Baseline | regenerated so its ledger covers the declared chain; verification gate passes every stage |
| 24 | Development Ready | met |
| 24 | Test Ready | met — full suite 2,733 passed, 2 skipped, 0 failed |

### Not done, and why

**1. Paging beyond email (§26.1) — the only open DECISION.** Nothing but email exists.
`email_authority` carries SLA breaches and CEO escalations; there is no SMS or push path,
and `severity='critical'` reaches an inbox like everything else. The plan already states the
minimum next step — route critical alerts and CEO escalations through the existing
transports, reusing `email_authority`'s ledger shape so idempotency and the attention budget
still apply. It is a delivery decision, not a model change, and it is genuinely undone.

**2. No executive has decided anything in production (§24, Operational Ready).** The console
is constituted and unexercised: `decided_actor IS NOT NULL` counts **0** on Railway. The
mechanism is proven — it counts 9 locally, from tests and authorisation probes, each
carrying a bound identity — but the loop has not yet been closed by a real person on the
real system. There are also 0 pending proposals there, so nothing is waiting on anyone. This
is not a defect; it is the difference between a console that works and a console that is
used, and only time and business activity close it.

**3. Definition of Done (§27) — day 0 of 14.** It requires fourteen consecutive days of
production evidence. Deployment was today. Measured on day 0, every criterion that can be
measured yet holds: 0 pending proposals missing an owner, authority or due date; 0 rows
expired since the deploy; 1 governance alert, opened by the machinery itself and owned.

**The last silent expiry was 2026-09-06 01:45**, roughly seventeen hours before the
activation code shipped at 18:26. Sixty legacy `expired` rows remain as historical record.
Nothing can add to them: `expire_stale()` is deprecated and returns `{"expired": 0,
"deprecated": true}`, and `_run_governance_expiry` is now an alias for the SLA sweep, kept
only so an old import still resolves. **The clock on "zero silent expiry" therefore starts
from the deploy, not from this document.**

**4. World-Class Governance Ready (§24) — 90 days,** plus external reconstruction,
replayable decisions and independent assurance. Not claimable and not claimed.

### The honest summary

Every buildable item in this plan is built, deployed and verified on both databases. What
remains is one delivery decision, and elapsed time under real use. The plan's own standard
is the right one to hold to: this work is now `DEPLOYED`, and it becomes
`PRODUCTION VERIFIED` on the fourteenth consecutive day that the evidence above still holds.
