# Conscestra CRM — Post-Remediation Independent Reassessment

**Independent, adversarial reassessment. 2026-09-07.**

Baseline: `docs/architecture_assessment_2026-09-05.md` (defects D-01…D-22, root causes RC-1…RC-5).
This is a **fresh assessment**, not a remediation sign-off. The baseline is used only to
measure what moved.

Evidence gathered from: the repository at `1877fdc` (branch
`docs/production-verification-2026-09-06`, merged to `origin/master` as `0c7c64a`); the
**running production application** at `orbitcrm-production.up.railway.app`, confirmed on
commit `0c7c64ab2fab` = `origin/master` HEAD; the production application's own admin HTTP
surface (413 paths); the local PostgreSQL 17.9 database; the governance test suite
(2,918 tests executed in this review); `scripts/verify_invariants`; and CI configuration.

| Label | Meaning |
|---|---|
| **FACT** | Measured on a live system or read directly from code during this review |
| **INFERENCE** | Concluded from facts; could be wrong |
| **RISK** | A consequence that has not happened yet, or has happened and could recur |
| **RECOMMENDATION** | What to change |
| **DECISION REQUIRED** | A choice only the owner can make |
| **HUMAN ACTION REQUIRED** | Something a person must do; the system cannot |

Verification status: VERIFIED · PARTIALLY VERIFIED · INFERRED · UNVERIFIED · CONTRADICTED BY EVIDENCE.

### Declared limits of this review

Two evidence channels were unavailable and their absence is stated rather than papered over:

- **FACT / VERIFIED.** Direct SQL access to the Railway database was blocked by this
  environment's sandbox policy. Production database facts in this report were therefore
  obtained through the **running application's own admin endpoints**, which is a weaker
  channel: it reports what the app believes. Schema-level facts (constraints, triggers,
  function bodies) were measured on the **local** database and are labelled as such.
- **FACT / VERIFIED.** Live *mutation* probes against production (anonymous write attempts,
  minting a decision token) were blocked by the same policy. Those findings rest on code
  reading plus non-mutating probes, and are labelled PARTIALLY VERIFIED rather than VERIFIED.

Where a claim could not be established to the standard the baseline used, it says so.

---

## 1. Executive Summary

**The remediation is real, technically excellent, and has moved several controls from
"exists" to "enforced". It has also introduced a governance bypass that is now the
dominant decision channel in production, and it did not close the customer-facing failure
that motivated the baseline.**

The activation work of 2026-09-06 built the strongest part of this system: five attested
executives with individual credentials, decisions bound to the signed-in executive rather
than to a request body, an atomic claim-execute-verify approval path, a per-action-type
decision policy table with no undeclared write capabilities, owner-eligibility enforced by
database trigger on governance objects, and an alert lifecycle with a named accountable
owner, an SLA, an escalation authority and a full state history. Several of these are
better than what most enterprise platforms field.

Three facts define the current state.

1. **FACT / VERIFIED.** `POST /governance/approve` is bound to the signed-in executive and
   refuses an admin token. But `GET /governance/decide?g=…&a=approve&t=…` is an
   **unauthenticated GET that executes the governed action immediately** — no session, no
   confirmation step, no expiry, no eligibility re-check — and it is the channel actually in
   use. Of every decision in production history, **not one was made through an
   authenticated session**: all were `email-link` or the `system` expiry sweep. On
   2026-09-07 three proposals assigned to two different executives (CFO ×2, CRO ×1) were
   created at 13:00:15–13:00:30 and decided at 13:07:20, 13:07:24 and 13:08:29 — three
   decisions across two claimed authorities inside 69 seconds — each recorded with
   `decided_actor = None`. Every such link is also BCC'd to the shared `info@agentorc.ca`
   archive by default, and the escalation reminder re-issues live links every 24 hours until
   someone decides.

2. **FACT / VERIFIED.** The event-orphan control was fixed; the business outcome was not.
   The 18 `order.shipped` events the baseline found unprocessed on 2026-09-02 are **still
   unprocessed five days later**, alongside 21 `order.status_changed` events. What changed
   is that they are now visible, durable, and owned: alert `34fe6159`, class
   `event_orphaned`, severity high, accountable owner **CTO Bill Wang**, 24h SLA, due
   2026-09-07T18:34 — **4.3 hours from breaching**, with `escalation_notices: 0`. The
   detection defect is genuinely closed. The 18 customers still have not been told their
   orders shipped.

3. **FACT / VERIFIED.** Decision authority is bound to a human; **the policy that governs
   those decisions is not**. Of 16 mutating governance endpoints, only 3 —
   `approve`, `reject`, `delegate` — call `_bound_authority`. `PUT
   /governance/action-policies/{action_type}` accepts the ops/machine token, takes
   `updated_by` **from the request body**, and can flip any action class from
   `HUMAN_APPROVAL` to `AUTO_EXECUTE`. `PUT /governance/policies/{key}` can raise
   `gov.hitl_amount` (the human-in-the-loop money floor) or `brand.max_discount_pct` the
   same way. The system refuses to let an administrator approve one discount, and permits
   the same administrator to abolish approval for the entire class.

**Did the remediation introduce regressions?** Yes, three material ones (§19), including a
mail-sending guard that silently disabled 21 tests covering the exact subsystem behind
finding 2.

**Is it fundamentally sound?** Yes — more so than at baseline. **Does it need a rewrite?**
No. **Is it world-class?** No. §28 gives the verdict and §24 the shortest path.

---

## 2. Assessment Scope and Method

Method, in order of execution:

1. Reconstructed the remediation from `git diff 69ff80f..HEAD` — 31 files, +6,744/−184 lines,
   16 commits, all merged and deployed.
2. Confirmed the deployed artefact: `/health.commit = 0c7c64ab2fab`, identical to
   `origin/master` HEAD. **Every remediation commit assessed here is live.** This removes the
   usual ambiguity about whether a fix reached production.
3. Re-tested each baseline defect against live evidence rather than against its fix commit.
4. Ran the full governance test suite (2,918 tests) and `scripts/verify_invariants`.
5. Conducted an independent adversarial pass free to find defects the baseline never
   considered — §18.
6. Searched specifically for second-order effects of the fixes — §19.

**FACT / VERIFIED.** Nothing in this document was taken from `README.md`, `skills.md`, the
activation plan, or the baseline without being re-measured.

**FACT / VERIFIED.** No defect found during this review was fixed during this review. This is
an assessment; the system was not modified.

---

## 3. Evidence and Verification Standard

The baseline's hierarchy was retained. Two rules were applied strictly, and both changed a
conclusion in this review:

- **A control that is enforced is not the same as a control that is exercised.** The
  session-bound decision path is enforced and, in production, has never been exercised.
- **A control that is fixed is not the same as an outcome that is corrected.** The orphan
  detector is fixed. The orphans are still there.

One correction to my own working notes, recorded because the method matters: I first read
`/governance/work` and concluded production alerts were unowned, because the response uses
`accountable_owner`, not `owner`. Reading the full alert record showed every alert **is**
owned, with a named executive, an SLA and a lifecycle history. The alert-ownership
remediation is real. Absence of a key I guessed at is not absence of the control.

---

## 4. Previous Assessment Remediation Matrix

| ID | Baseline finding | Claimed remediation | Current evidence | Status | Regression risk |
|---|---|---|---|---|---|
| **D-01** | Production serves contact PII to anonymous callers | none | **FACT/VERIFIED.** Anonymous `POST /contact-chat {mode:"list"}` to production returned names, emails, phones, full street addresses and staff names, "Page 1 of 26 \| Total: 129 contacts". `API_SECURITY_MODE=public-read`. | **NOT FIXED** | — |
| **D-02** | Business records have no accountable human owner | E2 predicate + trigger on governance objects; `repoint_ownership_exceptions.py` | **FACT/VERIFIED (local).** `fn_owner_eligible` now enforced by `trg_action_approvals_owner_eligible` and `trg_governance_alerts_lifecycle`, and a pending approval with a NULL owner is **refused**. But `owners` = 45 rows, of which **39 are customer contacts, 1 is an employee, 5 are eligible**; `orders` 2,124 rows / **58 owned (2.7%)**; no constraint references the predicate on any business entity. | **FIXED — PARTIALLY VERIFIED** (governance objects only) | Owner eligibility is checked at write time and never re-validated — see N-06 |
| **D-03** | Governance queue not operated; 89% expire | 5 authorities, action-policy table, 48h SLA, escalation, repeating reminders | **FACT/VERIFIED.** `governance_action_policies` = 37 rows, `decision_required: []`, `undeclared_write_capabilities: []`. Production `median_decision_hours 6.5`, `breach_rate_7d 0%`, `pending 0`. But `expired_7d = 5` against `created_7d = 9`, and **`decisions_30d_by_decider = {"email-link": 6}`** — no named human. | **FIXED — PARTIALLY VERIFIED** | The mechanism now works; the operator is a bearer link (N-01) |
| **D-04** | 18 shipped orders unnotified; bus orphaned 85h; detector silent after 24h | durable orphan state, owned alert, age-unbounded detection, cluster-aware status | **FACT/VERIFIED.** `/agent-bus/status.orphaned` = 39 (`order.shipped` 18, `order.status_changed` 21), oldest **2026-09-02**, unchanged. Alert `34fe6159` owns it: CTO Bill Wang, 24h SLA, due today 18:34, `escalation_notices: 0`. | **Detection FIXED — VERIFIED. Outcome NOT FIXED** | Alert dedupe key is class-wide (`event_orphaned:open`) — a second incident merges into the first |
| **D-05** | No field history outside cases; `audit_log` has no actor | none | **FACT/VERIFIED (local).** `audit_log` columns are `audit_id, entity, entity_id, action, payload, created_at` — **still no actor**, 16,997 rows. `record_field_history` does carry `actor, actor_id, source` but holds 1,805 rows against 16,997 audit rows. | **NOT FIXED** | — |
| **D-06** | Approval execution not atomic or race-safe | atomic claim + execution token + lease + stranded sweeper | **FACT/VERIFIED.** `_claim_execution` is `UPDATE … WHERE status='pending' RETURNING`; finish is `WHERE execution_token=<ours> AND status='executing'`; `EXECUTION_LEASE_MINUTES=15`; `sla_sweep()` recovers stranded rows. Production `approvals_stranded: 0`. | **FIXED — VERIFIED** | New `executing` state and lease — see §19 R-3 |
| **D-07** | AUTOACT live behind one tunable threshold | explicit per-action `auto_execute` + `confidence_grants_authority=false` | **FACT/VERIFIED.** `/governance/status` returns `"confidence_grants_authority": false`. Exactly two of 37 policies auto-execute: `order.cancel` (AUTO_EXECUTE, owner CRO, standing policy) and `email.send_payment_reminder` (SAMPLED_REVIEW). | **FIXED — VERIFIED** | The flag is settable over HTTP by a machine token — N-02 |
| **D-08** | Production superuser DSN on developer laptop | laptop autosend disabled | **FACT/VERIFIED.** `.env` still contains `RAILWAY_DB_URL` with role **`postgres`** against `shinkansen.proxy.rlwy.net`. `verify_invariants` still reports `TODO application DSN points at crm_app`. The *sending* half was fixed; the *credential* was not. | **PARTIALLY FIXED** | — |
| **D-09** | 70% of tables without FKs; 0 RLS | none targeted | **FACT/VERIFIED (local).** 163 tables, 164 FK constraints, **90 tables (55%) carry at least one FK**, **0 RLS policies**, 72 non-internal triggers. Better than the baseline's 30%, unmeasured on Railway. | **PARTIALLY IMPROVED** | — |
| **D-10** | Migration integrity split: two checks, two answers | none | **FACT/VERIFIED.** `/deploy/migrations` on production still returns `ok: false` with a long `required` list, while `migrate --check` reports current. | **NOT FIXED** | — |
| **D-11** | Status endpoints report per-worker state | `cluster` block read from DB + explanatory note | **FACT/VERIFIED.** `/agent-bus/status` now returns `this_process_role: "follower"`, `running: false`, and `running_note: "answered by a follower; the consumer runs on the leader — read cluster for the truth"`, plus a `cluster` block. The endpoint no longer lies; it explains. | **FIXED — VERIFIED** | — |
| **D-12** | Retrieval abstention not gated; 7/10 out-of-scope grounded | none (the `knowledge.py` diff is the proposal cap, not abstention) | **FACT/VERIFIED.** No `kb_coverage_runs` table, no `eval_runs` table. Abstention still ungated. | **NOT FIXED** | — |
| **D-13** | One real role (`admin`); no least privilege | `require_governance_actor` — a third caller class | **FACT/VERIFIED.** An executive session now reaches the governance routers **without** platform-admin rights: "'You may approve a discount' should not imply 'you may erase a customer'". A genuine, well-reasoned privilege split. Rep/manager roles still absent. | **PARTIALLY FIXED — VERIFIED** | — |
| **D-14** | Silent degradation at DEBUG in ~40 places | none | **FACT/VERIFIED.** New code continues the pattern: `logger.debug("staff-email claim skipped")`, `logger.debug("reminder email skipped")` on the escalation path. | **NOT FIXED — and extended** | The new escalation mail path fails silently at DEBUG |
| **D-15** | No OTel/metrics export; alerts stay in-app | email escalation added; paging explicitly settled as unnecessary | **FACT/VERIFIED.** Commit `1877fdc` records the decision that email is the paging channel. No OTel. This is now a **documented decision**, not an oversight. | **TRANSFORMED INTO A DIFFERENT RISK** | The paging channel is the same channel that carries approval authority — N-01 |
| **D-16** | `allowed_callers` never seeded | none | **FACT/VERIFIED (local).** `capability_registry`: 45 rows, `allowed_callers` **NULL on 45/45**, 0 disabled. Production `/a2a/registry` returns 45 rows with no `allowed_callers` field at all. Guardrail layer 4 remains inert. | **NOT FIXED** | — |
| **D-17** | `postdeploy_verify` self-description contradicts itself | none | **FACT/VERIFIED.** Module docstring: "WRITES: verify_invariants and red_team both MUTATE rows". `argparse` description, 20 lines below: "Read-only: it opens read-only transactions and never writes to the target." | **NOT FIXED** | — |
| **D-18** | Eval results not persisted | none | **FACT/VERIFIED.** No `eval_runs` table. | **NOT FIXED** | — |
| **D-19** | Consensus attestation never wired | none | **FACT/VERIFIED.** `/deploy/consensus` → `{"ok": true, "replicas": 0, "note": "no recent attestations"}`. Still a dead control reporting `ok: true`. | **NOT FIXED** | The control returns `ok` while measuring nothing |
| **D-20** | `structuredIntent` accepted from request body | none | **FACT/VERIFIED.** `ContactChatInput.structuredIntent: Optional[Dict[str, Any]]` is still declared and accepted from the body. | **NOT FIXED** | — |
| **D-21** | pgvector written, numpy read path | hybrid retrieval live | **FACT/PARTIALLY VERIFIED.** Hybrid is live per memory and prior verification; the frozen-contract test for the kill switch now **fails** (§19 R-2). | **PARTIALLY FIXED — with a regression** | The rollback path is no longer pinned |
| **D-22** | Local `master` 18 commits behind | none | **FACT/VERIFIED.** Local `master` is now **34 commits behind** `origin/master`. | **REGRESSED** | Cosmetic |

**Summary: 4 fixed and verified · 5 partially fixed · 10 not fixed · 1 transformed · 1 regressed · 1 fixed-with-regression.**

The four clean fixes (D-06, D-07, D-11, and the detection half of D-04) are the highest-value
ones in the register. The ten unfixed are concentrated in Stages 0, 4, 5 and 6 of the
baseline roadmap, which were not attempted — the owner correctly went for Stages 1–3 first.

---

## 5. Current Architecture

**FACT / VERIFIED.** The system map from the baseline holds; the changes are additive:

```
EDGE   app/main.py — FastAPI, 36 scheduler jobs, 413 live API paths
  auth_dep: posture(open|public-read|locked) · require_admin
           · require_governance_actor  ← NEW: ops token | admin session | ACTIVE EXECUTIVE
       │
GOVERNANCE PLANE (new, ~2,260 lines)
  governance_policy.py  37 action policies · 5 authorities · HUMAN_APPROVAL|SAMPLED_REVIEW|AUTO_EXECUTE
  governance.py         propose → route → [session-bound decide | HMAC email link] →
                        atomic claim → dispatch → verify → finish · SLA · escalate · reminders
  governance_alerts.py  alert lifecycle: owner · SLA · escalate · acknowledge · resolve · close
       │
CAPABILITY MESH  a2a.py — 45 capabilities, 0 with allowed_callers
       │
DETERMINISTIC EXECUTION  execute_sp · ~26 SPs · 72 triggers · 70 event types
       │
DATA  PostgreSQL · 163 tables · 164 FKs (90 tables) · 0 RLS
```

**FACT / VERIFIED.** The three write boundaries remain three; `write_call_sites.py` still opens
"There are three write boundaries, not one". RC-4 is untouched, as the baseline's own
sequencing intended (Stage 4).

**INFERENCE / PARTIALLY VERIFIED.** The governance plane is now the largest single subsystem
added in the project's recent history, and it is the least covered by the CI gate (§13).

---

## 6. Current Governance Architecture

### What is genuinely strong

**FACT / VERIFIED.** `_bound_authority(request)`:

- takes the deciding identity from the **session**, never from the request body;
- refuses an admin token outright — "an administrator cannot decide on an executive's behalf";
- **re-checks eligibility at decision time**, so a revoked or deactivated executive's live
  session cannot decide. This answers the brief's "can stale sessions continue to decide?"
  with a clean **no**, for this path.

**FACT / VERIFIED.** `_authority_check` enforces role affinity: a CFO may not decide a
CRO's row; the CEO may; an escalated row admits the escalation authority. `"human"`,
`"admin"` and an unlinked Slack id are **refused, not recorded**.

**FACT / VERIFIED.** `principal_for_decider` refuses to launder a channel into a person. It
records `policy:` decisions as `kind="policy"`, the expiry sweep as a service, and the email
link as `kind="token"`, with the reasoning written into the docstring: "by this codebase's own
doctrine a FALSE record is worse than an absent one". This is the single most admirable piece
of engineering in the remediation.

**FACT / VERIFIED.** Post-execution verification is real: production history rows carry
`verification: {ok: true, checks: [{check: "dispatch_audit_row", note: "outcome=accepted"}],
verified_at: …}`. "A function returning is not the same as the business effect existing."

### What the operation actually shows

**FACT / VERIFIED (production, `/governance/history`).** Every decided approval visible in
production history was decided by `system` (the 4-day expiry sweep, always at 01:45:00) or by
`email-link`. **Zero** by an authenticated session.

**FACT / VERIFIED (local database, whole history).** `decided_via` distribution:

| decided_via | decided_actor | count |
|---|---|---|
| *(null — pre-activation)* | *(null)* | 280 |
| `policy` | *(null)* | 25 |
| `email-link` | `email-link` | 8 |
| **`session`** | **`ceo@agentorc.ca`** | **1** |

**One decision in the system's entire history was made by an authenticated named human at a
keyboard.**

**FACT / VERIFIED.** Production decision timings, 2026-09-07:

| action | authority | created | decided | delta |
|---|---|---|---|---|
| `supervisor.emit_dunning` | CFO | 13:00:24 | 13:07:20 | 6.9 min |
| `supervisor.emit_dunning` | CFO | 13:00:15 | 13:07:24 | 7.2 min |
| `supervisor.emit_hot_leads` | CRO | 13:00:30 | 13:08:29 | 8.0 min |

**INFERENCE / PARTIALLY VERIFIED.** Three decisions attributed to two different executives,
inside 69 seconds, roughly seven minutes after the notification email, each with
`decided_actor = None`, is not the signature of two executives independently reviewing
proposals. It is the signature of a **single actor processing one mailbox** — most plausibly
the shared `info@agentorc.ca` BCC archive — or of an automated link fetcher. I cannot prove
which, and the system cannot either: that is the finding.

---

## 7. Identity and Accountability

**FACT / VERIFIED (production `/governance/authorities`).** Five executives — CEO Alan Qin,
CRO Daping Qin, CFO Sherman Zhang, CTO Bill Wang, COO Alex Zhou — each with a distinct
`executive_id`, an `owner_id`, an `employee_uuid`, `eligible: true`, `has_credential: true`,
and `identity_mismatch: null`. `missing: []`, `without_credential: []`. This is a complete,
attested authority set and it is a real achievement.

**FACT / VERIFIED (local).** The eligibility predicate is well-designed:

```sql
fn_owner_eligible(p_owner) =
     EXISTS (assignable_identity WHERE owner_id = p_owner AND is_active)
 AND NOT EXISTS (contacts WHERE contact_id = p_owner)          -- not a customer
 AND NOT EXISTS (employees … role='agent' OR email LIKE '%@system.internal')  -- not an AI
 AND NOT EXISTS (… identity collision between owners.email and employees.email)
```

It refuses customers, service identities and AI agents as accountable owners, and it catches
the collision case where a display name and an owner identity name different people.

**FACT / VERIFIED (local).** It is enforced on exactly **two** tables — `action_approvals` and
`governance_alerts` — by trigger, including the NULL case:

```
IF NEW.status = 'pending' AND NEW.accountable_owner_id IS NULL THEN RAISE …
   'a pending approval must name an eligible accountable owner'
```

**FACT / VERIFIED (local).** It is enforced on **no business entity**. `orders.owner_id` is
populated on 58 of 2,124 rows (2.7%). `owners` holds 45 rows of which 39 are customer
contacts. The question "which human is accountable for this order" still has no answer for
97.3% of orders.

**FACT / VERIFIED.** The local and production `owner_id` values for the same five executives
**differ** (local CEO `db6a9f31…`, production `146d0190…`). **RISK:** accountability records
are not portable between the two databases, so a decision reconstructed locally cannot be
matched to the production row by owner identity.

**Verdict on the accountability chain: it now closes for governance objects and remains open
for business objects.** That is real progress on RC-1, and RC-1 is not resolved.

---

## 8. AI-Agent Architecture

**FACT / VERIFIED.** The control chain the brief specifies is present and mostly ordered
correctly:

```
intent → identity(principal) → registry-enabled → allowed_callers[INERT] → principal-required-for-writes
       → params_schema → policy(decision_mode) → HITL amount floor → propose | auto-execute
       → atomic claim → dispatch → post-action verification → audit → undo (where declared)
```

**FACT / VERIFIED.** `undeclared_write_capabilities: []` — **every write capability now has a
declared decision policy.** This is a strong invariant and it is measured continuously by the
system itself.

**FACT / VERIFIED.** `confidence_grants_authority: false`. The brief's warning — "confidence
must never become a substitute for authorization" — is explicitly answered in the running
system. `act_min` is retained and labelled *informational*.

**FACT / VERIFIED.** `agent_capability_grants` = **0 rows**. No authored agent currently holds
any capability grant, so the U4 "authored agents can act" surface is dormant. That is a safe
default, not a defect.

**FACT / VERIFIED.** Layer 4 of the advertised four-layer guardrail model remains inert:
`allowed_callers` NULL on 45/45 capabilities. **Any registered caller may invoke any enabled
capability**; the only per-capability restriction is the write/principal gate.

**RISK / INFERRED.** Agent-to-agent chaining is bounded by `planner.max_writes = 2` and
`planner.max_steps = 6`, both defaults with no DB override. The brief's question — "can an
agent chain individually permitted actions into a prohibited outcome?" — is mitigated by the
step cap but not answered by any composite-effect policy. No evidence of an exploit; no
control that would prevent one.

---

## 9. Mutation and Consequential-Action Boundary

**FACT / VERIFIED.** The moat claim — *the governed action record is the only path from AI
intent to consequential business effect* — is **still true for AI intent** and **false as a
general statement about the system**, for the same reason as at baseline: three write
boundaries, ~204 declared direct-DML sites. The enumeration remains the control.

**The new and more serious boundary problem is not on the write path — it is on the
authority path.**

**FACT / VERIFIED.** Of 16 mutating governance endpoints, 3 bind to the signed-in executive:

| Endpoint | Bound to executive? | What it can do |
|---|---|---|
| `POST /governance/approve/{uuid}` | **YES** | decide one proposal |
| `POST /governance/reject/{uuid}` | **YES** | decide one proposal |
| `POST /governance/delegate/{uuid}` | **YES** | reassign one proposal |
| `PUT /governance/action-policies/{action_type}` | **NO** | **flip any class to AUTO_EXECUTE**; `updated_by` from body |
| `PUT /governance/policies/{key}` | **NO** | **raise `gov.hitl_amount`**, **raise `brand.max_discount_pct`** |
| `DELETE /governance/policies/{key}` | **NO** | remove a tunable override |
| `POST /governance/undo/{uuid}` | **NO** | reverse an executed decision |
| `POST /governance/expire` | **NO** | force-expire pending proposals |
| `POST /governance/history/delete` | **NO** | clear decided rows *(archived — see below)* |
| `POST /governance/alerts/{id}/{action}` | **NO** | resolve/close an accountability alert |
| `POST /governance/alerts` · `/alerts/sweep` · `/sla-sweep` · `/renotify` · `/critique`×2 | **NO** | — |

**FACT / VERIFIED.** `require_governance_actor`'s docstring states the intended invariant: the
ops/machine token "may READ the governance surface. It still cannot DECIDE." That invariant
holds for approve/reject/delegate and **fails for every operation that is strictly more
powerful than deciding one row.**

**Credit where due — FACT / VERIFIED.** `POST /governance/history/delete` is **not** audit
destruction: `trg_action_approvals_deletion_log` archives the whole row into
`governed_deletions` first, and `restore_governed_deletion()` can put it back. Pending rows can
never be deleted. I initially ranked this as tampering and the code proved me wrong.

---

## 10. Event Architecture and Reliability

**FACT / VERIFIED (production).**

| Metric | Value |
|---|---|
| `queue_depth` | 0 |
| `queue_orphaned` | **39** (`order.shipped` 18, `order.status_changed` 21) |
| `queue_orphaned_durable` | **39**, since 2026-09-06 18:34, owned by an alert |
| oldest orphan | **2026-09-02T02:05:00** — unchanged since the baseline |
| `queue_failed` / `queue_stuck` / `queue_delayed` | 0 / 0 / 0 |

**FACT / VERIFIED.** The three baseline event defects are closed *as controls*: orphans are a
durable state, the alert is owned and dated, and `/agent-bus/status` reports cluster truth
rather than per-worker state.

**FACT / VERIFIED.** The alert that owns them is `due_at: 2026-09-07T18:34`, `hours_left: 4.3`,
`escalation_notices: 0`. In 4.3 hours the platform's own SLA on its own reliability alert
breaches and escalates to the CEO — over the same email channel identified in N-01.

**RISK / VERIFIED.** `dedupe_key: "event_orphaned:open"` is class-wide. While this alert stays
open, a *new and unrelated* orphan incident merges into it rather than raising a distinct,
separately-owned signal. The headline updates; the SLA clock does not restart.

---

## 11. Auditability

I attempted the brief's reconstruction on a real production action: approval
`supervisor.emit_hot_leads`, decided 2026-09-07 13:08:29.

| Question | Answer available? |
|---|---|
| What was proposed, when, by whom | **YES** — `proposed_by`, `created_at`, `confidence`, `params` |
| Under what authority | **YES** — `authority_role: CRO`, `accountable_owner: "CRO Daping Qin"` |
| Which policy permitted it | **YES** — `governance_action_policies` row, `policy_version` |
| Was approval required | **YES** — `decision_mode: HUMAN_APPROVAL` |
| **Who approved** | **NO** — `decided_by: "email-link"`, `decided_actor: null` |
| When | **YES** — `decided_at`, to the second |
| What executed | **YES** — dispatch through a2a with a correlation id |
| Was the result verified | **YES** — `verification.checks[].check = "dispatch_audit_row"` |
| Could it be reversed | **PARTIAL** — undo where a handler is declared |
| What an external auditor receives | **PARTIAL** — no signed export; the chain is reconstructable only through the app |

**FACT / VERIFIED.** Nine of ten links in the chain are present and machine-readable. **The one
that is missing is the identity of the human who authorised it** — and the system says so
honestly rather than inventing a name.

**FACT / VERIFIED.** Outside the governance chain, auditability is materially weaker:
`audit_log` (16,997 rows) has **no actor column at all**; `record_field_history` (1,805 rows)
has `actor`/`actor_id`/`source` but covers a fraction of mutations. D-05 is untouched.

---

## 12. Data Integrity

**FACT / VERIFIED (local).** 163 tables · 164 FK constraints · 90 tables (55%) with ≥1 FK ·
**0 RLS policies** · 72 non-internal triggers · 70 registered event types.

**FACT / VERIFIED.** `scripts/verify_invariants` — **all pass**, including append-only refusal
of DELETE/UPDATE, erasure leaving no recoverable image, deletion logs armed on three tables,
and privilege separation (role `crm_app` exists, holds no elevated attributes, owns no tables,
cannot create in `public`). One TODO: *"application DSN points at crm_app"* — the local DSN
still names an owner account.

**FACT / VERIFIED.** `governance_alerts` lifecycle counts (local): **782 cancelled**, 51 closed,
20 resolved, 10 open, 7 escalated — **all created 2026-09-05 to 2026-09-06**. The new alerting
layer produced ~870 alerts in two days, 90% of which were cancelled. **RISK:** a signal that
must be mass-cancelled on the day it ships trains its operators to cancel; that is how the
89%-expiry pattern the remediation set out to fix reappears in a new table.

---

## 13. Security

### N-01 · P0 · One-click approval is an unauthenticated GET, archived to a shared mailbox

**FACT / VERIFIED.** `GET /governance/decide?g=<uuid>&a=approve&t=<hmac>` is mounted on
`public_router` (no session gate — an anonymous request with a bad token receives the app's own
403 page, proving the path is reached without authentication), and on a valid token it calls
`await approve(...)` which **dispatches and executes the action immediately**.

Five properties compound:

1. **FACT / VERIFIED.** It is a **GET with side effects**. Any link-following agent — an
   enterprise mail security scanner, a URL-rewriting gateway, a chat unfurler, a browser
   prefetcher, an archiving crawler — approves and executes the action by *scanning the
   message*. **RISK:** the executive never clicked.
2. **FACT / VERIFIED.** `send_email(to=…, subject=…, body_html=…, body_text=…)` is called with
   no `bcc` argument on every governance path, so `bcc_addr = bcc or _bcc_address()` resolves
   to `EMAIL_BCC = info@agentorc.ca`. **Every routed-approval, escalation and reminder mail —
   each carrying live approve/reject links — is copied to a shared operational mailbox.**
3. **FACT / VERIFIED.** The token has **no expiry**. `decision_token = HMAC(secret,
   "uuid:action")[:32]`; validity ends only when the row leaves `pending`. Since the
   remediation replaced expiry with indefinite escalation, a pending row can now hold a
   permanently valid approval URL.
4. **FACT / VERIFIED.** The re-escalation loop **re-issues live links every
   `REESCALATE_HOURS` (24h), without bound**: "This repeats every 24h until a decision exists."
   One undecided proposal emits an unbounded stream of bearer approval tokens into that shared
   archive.
5. **FACT / VERIFIED.** `_authority_check` short-circuits for `via == "email-link"` and returns
   `ok: True` **without any eligibility check**, so a departed or deactivated executive's
   outstanding links keep deciding — the exact case the session path correctly refuses.

**FACT / VERIFIED.** `_link_secret()` falls back `GOV_LINK_SECRET → UNSUBSCRIBE_SECRET →
ADMIN_API_TOKEN`. All three are set, so the fallback is not active today, but the coupling
means an admin-token compromise would also be an approval-forgery compromise for every
approval UUID.

**INFERENCE / PARTIALLY VERIFIED.** This is the channel that made 6 of 6 production decisions
in 30 days, and the 69-second three-decision burst in §6 is consistent with a single mailbox
being processed rather than two executives deciding.

**RISK.** The remediation's stated purpose was that governance decisions be attributable to a
named human. The channel it shipped, and the only one in use, is an unauthenticated bearer
token that the system's own audit record explicitly declines to attribute to a person.

### N-02 · P0 · Policy is not bound to an executive; deciding is

**FACT / VERIFIED.** See §9. `PUT /governance/action-policies/{action_type}` accepts the ops
token and a body-supplied `updated_by`. `_EDITABLE` includes `decision_mode`, `auto_execute`,
`approver_role`, `sla_hours`, `sample_rate`, `delegation_allowed`, `status`. A holder of
`ADMIN_API_TOKEN` can set `sms.send` to `AUTO_EXECUTE` and attribute the change to the CEO.

**FACT / VERIFIED.** `PUT /governance/policies/{key}` is gated identically and reaches
`gov.hitl_amount` (bounds `0…10,000,000`) and `brand.max_discount_pct` (bounds `0…100`) — the
two deterministic money guardrails.

**Mitigations that are real:** the change is versioned (`policy_version`), historied, and the
`reason` is mandatory — "widening authority without saying why is the failure mode". A DB CHECK
refuses `auto_execute` without an owner. So the change is *recorded*; it is not *authorised*.

**FACT / VERIFIED.** `ADMIN_API_TOKEN` and `RAILWAY_ADMIN_API_TOKEN` are present in `.env` on
the developer laptop, alongside the production **superuser** DSN (D-08).

### N-03 · P1 · Production still serves PII to anonymous callers, and the anonymous surface is fully documented

**FACT / VERIFIED.** D-01 unchanged. Additionally, `GET /openapi.json` returns **281 KB / 413
paths** to an anonymous caller — a complete map of the attack surface. **FACT / VERIFIED:** the
governance and ops surfaces are correctly gated (18/18 probed endpoints returned 403
anonymously), so the disclosure is a reconnaissance aid rather than a direct breach.

**DECISION REQUIRED.** Memory records `public-read` as the *intentional* marketing posture.
That decision is the owner's to make, and it was made. What has not been decided is the
consequence: corpus provenance is unrecoverable (real vs synthetic cannot be reconstructed),
so "these are synthetic records" cannot be asserted as fact about the 129 contacts currently
served to anonymous callers with street addresses and staff names.

### Security items verified as sound

- **FACT / VERIFIED.** Governance/ops endpoints refuse anonymous callers: 403 on all 18 probed.
- **FACT / VERIFIED.** `/health.database.connected_as = crm_app` — the app runs unprivileged.
- **FACT / VERIFIED.** The auto-reply loop through the BCC archive **is** closed:
  `_is_our_own_mail(sender, own_address)` skips the archived copy before the RFC 3834 check.
  Governance mail does not set `Auto-Submitted`, so this was worth testing; the earlier guard
  catches it.
- **FACT / VERIFIED.** `_verify_token` fails closed when no secret is configured.
- **FACT / VERIFIED.** The LLM emits no SQL on any path (unchanged from baseline).

---

## 14. Retrieval and Knowledge Governance

**FACT / VERIFIED.** D-12 and D-18 are untouched: no `kb_coverage_runs`, no `eval_runs`,
abstention still ungated. The `knowledge.py` change in this remediation is the **daily proposal
cap**, not abstention.

**FACT / VERIFIED — and this one is good design.** `ProposalCapReached` is handled at all 8
producer sites, and in each the candidate is neither published, queued, nor silently discarded:
it is counted and returned as `deferred_by_cap`, with `source_ref` as the idempotency anchor so
tomorrow's pass re-mines it. "A pass that returns proposed=0 must be distinguishable from one
that had nothing to say."

**FACT / VERIFIED.** Only `kb.publish` carries a cap (3/day). **RISK / INFERRED:** the cap is an
attention budget applied to a *proposal* queue. Applied later to a consequential class such as
`supervisor.emit_dunning`, it would become a silent throttle on governed business action. The
deferral is visible today; the invariant "a capped class must surface as a work item" is
asserted in comments, not in a test that is gated.

---

## 15. Production / Railway Posture

**FACT / VERIFIED.**

| Property | State |
|---|---|
| Deployed commit | `0c7c64ab2fab` = `origin/master` HEAD — **every remediation commit is live** |
| DB role | `crm_app`, unprivileged |
| Process | `debug: false`, `reload: false`, `WEB_CONCURRENCY=2`, `workers_effective: 2` |
| Scheduler | running, 36 jobs, `overdue_jobs: []` |
| HA | `role: leader`, `lock_held: true`, single node, no automatic failover |
| Pool | `pool_max 16`, `in_use 1`, utilisation 6% |
| Migrations | `/deploy/migrations` → **`ok: false`** (D-10 unfixed) |
| Consensus attestation | `replicas: 0` — dead control returning `ok: true` (D-19) |
| Posture | `public-read` (D-01) |
| Governance | 5 authorities, 37 policies, live and operating |

**FACT / VERIFIED.** Local `master` is **34 commits behind** `origin/master`.

**Production-grade:** privilege separation, deploy proof via `/health.commit`, governance plane,
alert lifecycle, leader election, backups.
**Demo-grade:** posture, traffic volume (13 LLM calls/24h), single node, no PITR.
**Not implemented:** RLS, multi-tenancy, OTel, rep/manager roles, signed audit export.

---

## 16. Observability and Operations

**Materially improved — FACT / VERIFIED.**

- `/platform/health` now answers three named questions ("Is the machinery running?", "Are we
  keeping the promises we made?", "Are our own controls being respected?") with 25 metrics, per-metric
  thresholds in the `detail` string, and a `problems` array. Current state `warning`, problems:
  orphaned ×2, expired approvals.
- `/agent-bus/status` distinguishes leader from follower and says which answer to trust.
- The alert object carries owner, SLA, due date, escalation role, acknowledgement, resolution,
  closure evidence and a full `history` array with actor and note per transition.

**Can an operator discover a governance failure before a customer discovers its business
consequence?** **FACT / VERIFIED: for this incident, no.** The 18 shipped orders orphaned on
2026-09-02; the owning alert was not created until 2026-09-06 18:34 — **4 days later**, at
activation. The machinery that would now catch it did not exist when it happened, and the
events are still unprocessed today.

### N-04 · P2 · Two production endpoints report different values under the same label

**FACT / VERIFIED.** Same system, same minute, same label "Approvals expired (7d)":

- `/governance/status` → `expired_7d: 5` — `status='expired' AND decided_at > now() - 7d`
- `/platform/health` → `approvals_expired: 3` — `status='expired' AND created_at > now() - 7d`

Two different time anchors, one label, two consoles, one operator. This is precisely the drift
class the **Metric Registry** exists to prevent, appearing in the newest code — which means the
registry is not on the path that new metrics take.

### N-05 · P2 · The escalation path degrades silently

**FACT / VERIFIED.** `logger.debug("[governance] reminder email skipped …")` and
`logger.debug("[governance] staff-email claim skipped …")`. D-14's pattern was extended into
the escalation path, where a silent failure means an executive is never told their SLA
breached. `email_authority` is documented as "FAIL-OPEN on the bookkeeping, never on the
address" — correct intent — but the outcome of the send is logged at DEBUG and counted nowhere.

---

## 17. Enterprise Readiness

**FACT / VERIFIED.** Five executives with distinct credentials and role-affinity routing is a
genuine multi-executive model — better than the single-approver assumption at baseline.
Delegation exists and is session-bound.

**FACT / VERIFIED.** Still missing for enterprise: rep/manager roles (`auth_credentials` has one
real role), RLS (0 policies), segregation of duties between policy-setting and deciding (N-02
is exactly this gap), signed audit bundle export, load evidence at any scale, PITR.

**RECOMMENDATION.** The single-organisation decision remains correct. Multi-tenancy would add a
second isolation axis to a system whose first axis (`public-read`) is currently open. Do not
build it — see §26.

---

## 18. New Defects Discovered

| ID | Sev | Finding | Evidence | Status |
|---|---|---|---|---|
| **N-01** | **P0** | One-click approval is an unauthenticated GET with side effects, no expiry, no eligibility check, BCC'd to a shared mailbox, and re-issued every 24h indefinitely | §13 | VERIFIED (mechanism); INFERRED (that this explains the 69-second burst) |
| **N-02** | **P0** | Deciding is bound to an executive; **setting the policy that governs deciding is not**. 13 of 16 mutating governance endpoints accept a machine token; `updated_by` is body-supplied | §9, §13 | VERIFIED |
| **N-03** | P1 | Anonymous PII (D-01) plus a fully published 413-path OpenAPI map | §13 | VERIFIED |
| **N-04** | P2 | `expired_7d` reported as 5 and 3 by two production consoles under one label; the Metric Registry is not on the path new metrics take | §16 | VERIFIED |
| **N-05** | P2 | Escalation send outcome logged at DEBUG and counted nowhere | §16 | VERIFIED |
| **N-06** | P2 | Owner eligibility is checked at write time and **never re-validated**. `trg_action_approvals_owner_eligible` fires `BEFORE INSERT OR UPDATE OF accountable_owner_id, status`; a row whose owner later becomes ineligible is never re-examined | local trigger definition | VERIFIED |
| **N-07** | P2 | `APP_URL` is overloaded: it is both the **decision-link base** (`decision_links()`) and a **deployment signal** (`release_guard.is_deployed()`). Setting it to the production URL to generate correct links simultaneously re-arms unattended sending from that process | `governance.py:2194`, `release_guard.py:58` | VERIFIED |
| **N-08** | P2 | The alerting layer produced ~870 alerts in 2 days, **782 cancelled** — a 90% cancel rate on its first run | local `governance_alerts` | VERIFIED |
| **N-09** | P2 | The CI gate's 12 control tests include **none** of the governance-activation suite. `test_governance_activation.py` and `test_escalation_email_routing.py` are NOT GATED | `scripts/verify_gate.py:61-74` | VERIFIED |
| **N-10** | P3 | Alert dedupe key `event_orphaned:open` is class-wide; a second incident merges into the first | production alert record | VERIFIED |
| **N-11** | P3 | Executive `owner_id` values differ between local and production for the same five humans | §7 | VERIFIED |
| **N-12** | P3 | `/deploy/consensus` returns `ok: true` while measuring nothing (`replicas: 0`). A dead control that reports success is worse than one that reports unknown | production | VERIFIED |

---

## 19. Regression Analysis

| # | Remediation | New complexity | New failure mode | Evidence | Severity |
|---|---|---|---|---|---|
| **R-1** | "Stop the laptop emailing real executives" — `release_guard.is_deployed()` gate on unattended send | A deployment check below the test seam | **21 tests in `test_order_lifecycle_notifications.py` now fail**: `assert res["state"] == "accepted"` → `'queued'`, with `WARNING [unattended] email autosend is ON but this process does not look deployed`. The test suite for the **order-notification subsystem — the exact subsystem behind the 18 unsent shipped notifications — no longer verifies anything on a developer machine.** | this review's pytest run | **P1** |
| **R-2** | Hybrid retrieval enablement | A kill switch whose output is pinned by test | `test_recency_only_output_is_frozen` **fails**: `{'payment reminder': 2} != {'payment reminder': 4}` — "The kill switch no longer restores the pre-hybrid contract." The rollback path for retrieval is no longer the behaviour it was pinned to. | this review's pytest run | **P1** |
| **R-3** | Expiry replaced by indefinite escalation with one-click links | Rows stay `pending` forever; reminders repeat every 24h | The old failure was fail-safe (89% expired: nothing happened). The new one is **fail-open**: an undecided proposal broadcasts live bearer approval tokens into a shared mailbox until something clicks one. Expiry was a bad *decision policy* and a good *token lifetime*; removing it removed both. | §13 N-01 | **P0** (folded into N-01) |
| **R-4** | Owner-eligibility trigger on `action_approvals` | A pending row cannot exist without an eligible owner | If every authority is ineligible (all five deactivated, or `assignable_identity` unpopulated after a restore), **`propose()` raises and no proposal can be created at all** — governance fails closed into silence rather than into a queue. Correct direction; no alert covers the state. | trigger definition | P2 |
| **R-5** | Alert lifecycle with SLA | A second SLA clock, on the platform's own alerts | The `event_orphaned` alert is 4.3 hours from breaching its own 24h SLA and escalating to the CEO — over the email channel of N-01. A control that breaches its own SLA teaches operators that this SLA does not mean anything. | production | P2 |
| **R-6** | `test_work_ownership::test_K_H_closed_history_is_not_resurrected_as_work` | — | **Fails: `assert 174 < 100` — "the live surface must not be the size of the historical population".** The live work surface has grown past its own guard, consistent with N-08's alert flood. | this review's pytest run | P2 |

**Test suite result (local, this review): 2,891 passed · 25 failed · 2 skipped · 4m12s.**
21 of the 25 failures are R-1; 1 is R-2; 1 is R-6; 2 are `test_email_send_sp` (same
`is_deployed` root cause as R-1).

**FACT / VERIFIED.** None of these 25 failures can turn CI red: the workflow header states
plainly that the wider suite is excluded, and `verify_gate.py::CONTROL_TESTS` names 12 files,
none of them governance-activation or order-notification. **The newest and most consequential
controls are the least gated** (N-09).

---

## 20. World-Class Benchmark Scorecard

Scored on evidence, not sophistication. A mechanism that exists but is not reliably operated
does not receive full credit.

| # | Dimension | Score | Evidence |
|---|---|---|---|
| 1 | CRM architecture | **7**/10 | 413 endpoints, 163 tables, coherent SP/agent layering; three write boundaries, not one |
| 2 | AI architecture | **7**/10 | LLM emits no SQL on any path; typed capability mesh; retrieval abstention ungated |
| 3 | Agent orchestration | **7**/10 | intent router, planner caps, supervisor detectors; `allowed_callers` inert on 45/45 |
| 4 | Governance | **6**/10 | 37 declared policies, 0 undeclared write capabilities, atomic verified execution — and one unauthenticated bypass carrying 100% of real decisions |
| 5 | Security | **4**/10 | anonymous PII, unauthenticated GET that executes, policy unbound from identity, superuser DSN on a laptop |
| 6 | Data integrity | **6**/10 | all SQL invariants pass, 55% FK coverage, append-only enforced; 0 RLS, owners are customers |
| 7 | Reliability | **4**/10 | 39 events orphaned for 5 days; 18 customer promises unkept; queue otherwise clean |
| 8 | Observability | **7**/10 | large genuine improvement; two consoles disagree under one label; silent DEBUG degradation |
| 9 | Auditability | **6**/10 | 9 of 10 chain links present with post-execution verification; the missing one is *who* |
| 10 | Enterprise readiness | **4**/10 | five real executives; no rep/manager roles, no RLS, no signed export, no SoD on policy |
| 11 | Scalability | **3**/10 | single node, 2 workers, 13 LLM calls/24h, no load evidence at any scale |
| 12 | Product differentiation | **8**/10 | the governed-action record with declared policy and verified execution is genuinely rare |
| 13 | AI safety / control | **7**/10 | `confidence_grants_authority: false` is exemplary; composite-effect chaining unbounded by policy |
| 14 | Operational maturity | **4**/10 | one authenticated human decision ever; 782 alerts cancelled in 2 days; 25 red tests outside the gate |
| 15 | Governance identity / accountability | **5**/10 | the bound path is excellent and unused; the used path attributes to nobody |

**Mean 5.7 / 10.** Baseline-comparable dimensions moved up (governance, observability,
auditability, AI safety); security and reliability did not move, and identity/accountability
gained a mechanism while losing ground in practice.

---

## 21. Architectural Moat Assessment

**Claim:** *the governed-action record is the sole path from AI intent to consequential effect.*

| Property | State |
|---|---|
| Human accountability | **Partial** — governance objects yes; 97.3% of orders no |
| Policy enforcement | **Real** — 37 declared policies, 0 undeclared write capabilities |
| Identity binding | **Built and bypassed** — 3 of 16 endpoints bound; 0 of 6 production decisions used the bound path |
| Auditability | **Strong within governance**, weak outside (`audit_log` has no actor) |
| Verification | **Real** — post-execution `dispatch_audit_row` checks, stored per decision |
| Reversibility | **Partial** — undo where declared |
| Exportability | **No** — no signed bundle; reconstruction requires the platform |
| Difficult to reproduce | **Yes** — the declared-policy + verified-execution + honest-principal combination is not commodity |

**Verdict: partially implemented, and genuinely differentiated where implemented.** It is not
yet operationally enforced, because the enforcement point that matters — who authorised this —
is answered by "a link in an email" in every production case. **The moat is one control away
from being real**, and that control is N-01.

---

## 22. Findings by Priority

**P0 — can cause unauthorised consequential action or false accountability**
- **N-01** Unauthenticated GET executes governed actions; no expiry; no eligibility check; BCC'd to a shared mailbox; re-issued every 24h (subsumes R-3)
- **N-02** Policy-setting is not bound to an executive while deciding is
- **D-01/N-03** Production serves contact PII to anonymous callers

**P1 — material enterprise, governance, security, reliability or integrity weakness**
- **D-04 (outcome)** 18 shipped orders unnotified for 5 days; owning alert 4.3h from breach
- **R-1** `is_deployed` gate silently disabled 21 order-notification tests
- **R-2** Hybrid-retrieval kill switch no longer restores its pinned contract
- **D-02** 97.3% of orders unowned; `owners` is 87% customer contacts; predicate enforced on no business entity
- **D-08** Production superuser DSN on a developer laptop
- **D-05** `audit_log` has no actor

**P2**
- N-04 metric drift · N-05 silent escalation degradation · N-06 eligibility never re-validated ·
  N-07 `APP_URL` overload · N-08 782 cancelled alerts · N-09 governance suite outside the CI gate ·
  R-4 fail-closed-into-silence · R-5 alert breaching its own SLA · R-6 work surface past its guard ·
  D-09 0 RLS · D-10 migration split · D-12 abstention ungated · D-14 silent DEBUG degradation

**P3**
- N-10 class-wide dedupe · N-11 divergent executive owner ids · N-12 `/deploy/consensus` returns `ok` while measuring nothing ·
  D-16 `allowed_callers` inert · D-17 contradictory self-description · D-18 evals unpersisted ·
  D-20 `structuredIntent` from body · D-22 local `master` 34 behind

---

## 23. World-Class Blockers

1. **N-01.** A governed AI-agent CRM cannot claim that consequential actions are attributable
   to a named human while its only working decision channel is an unauthenticated bearer link
   whose own audit record says `decided_actor: null`.
2. **N-02.** Segregation of duties fails at the point where it matters most: the authority to
   *remove* the approval requirement is weaker than the authority to *exercise* it.
3. **D-01.** No enterprise buyer accepts a production system that serves contact PII, street
   addresses and staff names to anonymous callers.
4. **D-04 outcome.** 18 customer promises unkept for 5 days, with the machinery to detect it
   working correctly and nobody acting, is the definition of governance that is measured but
   not operated.

---

## 24. Recommended Remediation Sequence

Ordered by risk reduction × architectural leverage × governance importance.

### G-1 · Close the decision-link bypass (N-01, R-3) — days, not weeks

1. **Root cause.** The link was designed as a *convenience channel* and became the *only*
   channel, so its threat model was never raised to match its role.
2. **Why existing controls failed.** `_bound_authority` guards the session path only;
   `_authority_check` short-circuits for `via == "email-link"` before any identity or
   eligibility test.
3. **Architectural remedy.** The email link must **identify a person and confirm an intent**, not
   carry authority by possession.
4. **Smallest safe implementation:**
   - Make the link a **GET that renders a confirmation page** and a **POST that decides**.
     This single change defeats every prefetcher, scanner and unfurler, and costs one click.
   - Bind the token to the **executive**, not only to `(uuid, action)`:
     `HMAC(secret, uuid:action:executive_id:issued_at)`; record `decided_actor = <executive email>`.
   - Add **expiry** — `sla_hours`, or 72h — and re-mint on each reminder rather than re-sending
     the same eternal token.
   - **Suppress the BCC on governance mail**: pass `bcc=""` explicitly on all
     `send_email` calls in `governance.py` and `governance_policy.py`.
   - Re-check `fn_owner_eligible` on the email path as the session path already does.
5. **Schema.** `action_approvals`: `decision_token_issued_at`, `decision_token_executive_id`.
6. **Tests.** A GET must not mutate; an expired token is refused; a token minted for the CFO
   cannot decide a CRO row; `decided_actor` is never null on a successful decision.
7. **Production evidence.** `decisions_30d_by_decider` names people, not `email-link`.
8. **Rollback.** Feature flag on the POST requirement; the GET confirmation page is inert.
9. **Success invariant.** *No approval reaches `executed` without an identified human or a
   named policy with a human owner.*
10. **HUMAN ACTION REQUIRED.** Audit `info@agentorc.ca` for archived decision links and treat
    every still-`pending` approval's link as compromised; rotate `GOV_LINK_SECRET`, which
    invalidates all outstanding links.

### G-2 · Bind policy change to an executive (N-02) — days

- Apply `_bound_authority(request)` to `PUT /governance/action-policies/{action_type}`,
  `PUT`/`DELETE /governance/policies/{key}`, `POST /governance/undo`, `POST /governance/expire`,
  and the alert `resolve`/`close` transitions. Delete `updated_by` from `_PolicyChange`; take it
  from the session, exactly as `approve` does.
- **DECISION REQUIRED.** Should widening a policy (`HUMAN_APPROVAL → AUTO_EXECUTE`, or raising
  `gov.hitl_amount`) itself require an approval — governance governing governance? The
  recommendation is yes, with the CEO as the approver and a `policy.widen` action type, because
  it is the only change class that can disable every other control.
- **Invariant.** *No control may be weakened by an identity that could not exercise it.*

### G-3 · Drain the 39 orphaned events (D-04 outcome) — hours

- `POST /agent-bus/drain` is the declared remedy and is audited. **HUMAN ACTION REQUIRED**: the
  18 customers whose orders shipped on or before 2026-09-02 should receive a correct
  notification, or a deliberate decision to suppress a stale one should be recorded on the
  alert's `closure_evidence`.
- **Invariant.** *An orphaned consequential event is drained or explicitly written off, never
  aged out.*

### G-4 · Restore the test seam and gate the governance suite (R-1, R-2, N-09) — 1 week

- Give `is_deployed()` a test-scoped override the fixtures set, so a deployment guard cannot
  silently disable a subsystem's tests. **The 21 failing tests must go green on their own
  merits, not by loosening the assertion.**
- Re-pin or re-derive the hybrid kill-switch contract (R-2) as a deliberate, recorded decision.
- Add `test_governance_activation.py`, `test_escalation_email_routing.py`,
  `test_executive_identity_mismatch.py` and the order-notification suite to
  `verify_gate.py::CONTROL_TESTS`.
- **Invariant.** *A control shipped without a gated test is not shipped.*

### G-5 · Decide the production posture (D-01) — 1 week, then G-6/G-7 per the baseline roadmap

Unchanged from the baseline's Stage 0. It is the oldest open P0 and the only one that is purely
a decision.

---

## 25. Human Decisions Required

1. **DECISION REQUIRED.** Production posture: `locked` with a separate synthetic-only demo
   deployment, or `public-read` retained with a written acceptance that unprovenanced contact
   PII is public. (D-01, standing since baseline.)
2. **DECISION REQUIRED.** Should policy widening require its own approval? (G-2.)
3. **DECISION REQUIRED.** The 18 shipped orders: notify late, or suppress with recorded
   reasoning? (G-3.)
4. **DECISION REQUIRED.** Is the one-click email link retained at all after G-1, or do
   executives decide only in the console? Retaining it is defensible; retaining it unchanged is
   not.
5. **HUMAN ACTION REQUIRED.** Rotate `GOV_LINK_SECRET`; audit `info@agentorc.ca` for archived
   live decision links.
6. **HUMAN ACTION REQUIRED.** Remove the production superuser DSN from the laptop (D-08,
   standing since baseline).
7. **HUMAN ACTION REQUIRED.** Close or act on the `event_orphaned` alert before 18:34 today, or
   let it breach deliberately and record why.

---

## 26. What Not to Build Yet

| Deferred | Why it waits |
|---|---|
| **Multi-tenancy / RLS-per-tenant** | The first isolation axis (`public-read`) is open. A second isolation model over an open one adds surface, not safety. |
| **OTel / external paging** | Commit `1877fdc` settled that email is the paging channel. That decision is sound **once the email channel stops carrying approval authority** (N-01). Fix the channel before instrumenting it. |
| **More AI agents or capabilities** | 45 capabilities have `allowed_callers` NULL and `agent_capability_grants` = 0. Adding capability breadth widens an ungated surface. |
| **A visual workflow designer** | The workflow that matters — propose → decide → execute → verify — has one broken link. Build the link, not the designer. |
| **Additional vector stores / graph retrieval** | Retrieval abstention is ungated and no coverage run is persisted. Retrieval *quality* work before retrieval *governance* work optimises the wrong axis. |
| **Rep/manager RBAC** | Worth doing, but after N-02: adding roles to a system where a machine token can rewrite the policy governing all roles adds bookkeeping, not authorisation. |
| **Automatic HA failover** | Per memory, PgBouncer transaction mode breaks `leader.py`'s session advisory lock. Single-node is the honest posture at 13 LLM calls/day. |

---

## 27. Readiness Gates

| Gate | State | What blocks it |
|---|---|---|
| **1 · Architecture Ready** | **PARTIAL** | Three write boundaries remain; owner identity space still shared with customers |
| **2 · Governance Ready** | **NO** | N-02: policy-setting unbound from identity; `allowed_callers` inert |
| **3 · Identity Ready** | **NO** | N-01: 0 of 6 production decisions attributable to a named human |
| **4 · Security Ready** | **NO** | N-01, N-02, D-01, D-08 |
| **5 · Reliability Ready** | **NO** | 39 events orphaned 5 days; 18 promises unkept |
| **6 · Audit Ready** | **PARTIAL** | Chain is complete except the deciding human; `audit_log` has no actor; no signed export |
| **7 · Operational Ready** | **NO** | 1 authenticated human decision ever; 782 alerts cancelled in 2 days |
| **8 · Production Verified** | **PARTIAL** | `/health.commit` proves the deploy; `/deploy/migrations` says `ok: false`; `/deploy/consensus` measures nothing |
| **9 · Enterprise Ready** | **NO** | No SoD on policy, no rep/manager roles, no RLS, no load evidence |
| **10 · World-Class Ready** | **NO** | Gates 2–5, 7, 9 |

Gate 1 and Gate 6 moved from NO to PARTIAL since the baseline. Gate 3 gained its mechanism and
did not gain its evidence.

---

## 28. Final Verdict

> ## GOVERNED AI-AGENT CRM — NOT YET WORLD-CLASS

**The remediation did the hard part and left the easy part open.**

The parts that are genuinely difficult — an atomic claim-execute-verify approval path, a
per-action decision policy with zero undeclared write capabilities, a principal resolver that
refuses to launder a channel into a person, an owner-eligibility predicate enforced by database
trigger including the NULL case, an alert object with an owner, an SLA and a lifecycle history,
and a privilege split that stops "you may approve a discount" from meaning "you may erase a
customer" — are built, deployed and working. Several are better than commercial practice.

What is open is comparatively easy and disproportionately consequential: a GET that should be a
POST, a BCC that should be suppressed, a token that should expire, and thirteen endpoints that
should call the function the other three already call. Until those are closed, the system's own
audit trail says that no human has ever authorised anything in production — and that is the
sentence a serious enterprise would read first.

The verdict is not "architecture ready — operational gaps remain", because N-01 and N-02 are
architectural: an unauthenticated GET that executes a governed action, and an authorisation
model in which weakening a control requires less authority than exercising it, are design
defects, not operating defects.

**If Conscestra were entrusted tomorrow with consequential business decisions, what could still
go wrong that the architecture should prevent?** An enterprise mail gateway scanning an
escalation email approves a dunning campaign; a machine token sets `sms.send` to `AUTO_EXECUTE`
and attributes it to the CEO; eighteen customers are never told their orders shipped.
**Can the system prove it prevented them?** For the third: no — it happened, and it is still
happening. For the first two: no — it would record them as legitimate.
**Can an independent external party verify that proof?** Not yet: there is no signed export, and
the chain terminates at `decided_actor: null`.

**Estimated distance to a defensible "world-class" claim: G-1 through G-4 is roughly two weeks
of work.** None of it is a rewrite. That is a short distance, and it is the honest one.

---

## 29. Evidence Appendix

| # | Claim | Source | Command / probe |
|---|---|---|---|
| E-01 | Production runs `0c7c64ab2fab` = `origin/master` HEAD | production | `GET /health` |
| E-02 | Anonymous PII: 129 contacts, 26 pages, emails/phones/addresses/staff names | production | `POST /contact-chat {"chatInput":{"mode":"list","pageSize":5,"routerAction":true}}`, no auth |
| E-03 | Governance/ops endpoints refuse anonymous callers | production | 18 paths probed → 403 each |
| E-04 | `/governance/decide` is publicly reachable and token-gated | production | `GET /governance/decide?g=…&a=approve&t=deadbeef` → app's own 403 page |
| E-05 | 5 attested authorities, no gaps | production | `GET /governance/authorities` |
| E-06 | `confidence_grants_authority: false`; `decisions_30d_by_decider = {"email-link": 6}`; `expired_7d: 5` | production | `GET /governance/status` |
| E-07 | 37 action policies; 2 auto/sampled; `order.cancel` owner CRO | production | `GET /governance/action-policies` |
| E-08 | 39 orphans (`order.shipped` 18), oldest 2026-09-02; `approvals_expired: 3` | production | `GET /platform/health`, `GET /agent-bus/status` |
| E-09 | Orphan alert owned by CTO Bill Wang, due 2026-09-07T18:34, `escalation_notices: 0` | production | `GET /governance/alerts` |
| E-10 | Three decisions, two authorities, 69 seconds, `decided_actor: null` | production | `GET /governance/history?limit=40` |
| E-11 | `/deploy/consensus` `replicas: 0`; `/deploy/migrations` `ok: false` | production | GET each |
| E-12 | 45 capabilities, 0 `allowed_callers` | production + local | `GET /a2a/registry`; `select count(allowed_callers) from capability_registry` |
| E-13 | `owners` 45 rows: 39 contacts, 1 employee, 5 eligible | local | SQL, read-only txn |
| E-14 | `orders` 2,124 rows / 58 owned (2.7%) | local | SQL |
| E-15 | `fn_owner_eligible` enforced by 2 triggers, 0 constraints, 0 business entities | local | `pg_proc.prosrc`, `pg_trigger`, `pg_constraint` |
| E-16 | `audit_log` has no actor column, 16,997 rows | local | `information_schema.columns` |
| E-17 | `decided_via`: 280 null / 25 policy / 8 email-link / **1 session** | local | SQL |
| E-18 | `governance_alerts`: 782 cancelled of ~870, all 2026-09-05/06 | local | SQL |
| E-19 | 163 tables · 164 FKs · 90 tables with FK · 0 RLS · 72 triggers | local | catalog |
| E-20 | Test suite 2,891 passed / 25 failed / 2 skipped | local | `pytest governance/tests -q` |
| E-21 | R-1 root cause | local | `pytest …::test_processing_to_shipped_produces_one_notification` → `'queued' != 'accepted'` + release_guard WARNING |
| E-22 | All SQL invariants pass; DSN TODO outstanding | local | `python -m scripts.verify_invariants` |
| E-23 | 3 of 16 mutating governance endpoints bind to an executive | repo | decorator/body scan of `governance*.py` |
| E-24 | BCC default `info@agentorc.ca` on every governance send | repo | `smtp_imap.py:166,201`; `governance.py:973`, `governance_policy.py:512` |
| E-25 | Reminder re-issues links every 24h without bound | repo | `governance.py:1920-1933` |
| E-26 | CI gate excludes the governance suite | repo | `scripts/verify_gate.py:61-74`, `.github/workflows/ci.yml` |
| E-27 | Production superuser DSN present on laptop | local | `.env` — role `postgres` @ `shinkansen.proxy.rlwy.net` |
| E-28 | `expired_7d` uses `decided_at`; `approvals_expired` uses `created_at` | repo | `governance.py:1999` vs `platform_health.py:467` |

**Not established in this review, and why:** direct Railway SQL (sandbox-blocked — production
figures come from the app's admin API); live anonymous *write* probes (sandbox-blocked —
the write gate's fail-closed behaviour is carried forward from the baseline as PARTIALLY
VERIFIED); whether a mail gateway is in fact fetching the decision links (would require mail
server logs — the 69-second burst is INFERENCE, and N-01 stands on the mechanism regardless).
