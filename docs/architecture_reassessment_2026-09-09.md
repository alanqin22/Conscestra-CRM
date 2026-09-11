# Conscestra CRM — Independent Architecture Reassessment

**2026-09-09. Audit-style, adversarial, assessment-only. No code, schema, configuration
or production data was changed.**

Historical baseline: `docs/architecture_reassessment_2026-09-07.md` (defects D-01…D-22,
new findings N-01…N-12, regressions R-1…R-6). That document is used **only** to record what
was previously claimed. No previous finding, remediation, design decision, document or test
result was accepted as evidence about the system's present state; every conclusion below was
re-measured.

---

## 1. Executive Assessment

**The two P0 architectural defects of September 7 were genuinely fixed, and both fixes are
proven in production. The reliability and accountability outcomes underneath them were not.
Three of the strongest controls in the system — alert escalation email, the alert
acknowledgement lifecycle, and the customer-notification path — do not work in the deployed
system, and none of them reports that it does not work.**

Six facts define the current state. Each is measured, not inferred.

1. **N-01 is closed, and provably so.** `GET /governance/decide` now renders a confirmation
   page and decides nothing; a separate `POST` decides. Tokens carry an executive id and a
   per-issuance nonce, expire at 72h, rotate on every reminder, re-check owner eligibility,
   and are retired when the row leaves `pending`. Probed live against production: an
   old-format bearer link is refused with *"this decision link predates identity binding and
   is no longer valid"* — **every link archived in the shared mailbox before the fix is
   already dead**. All three production decisions since the fix carry
   `decided_by = decided_actor = cfo@agentorc.ca`. The audit chain's missing tenth link is
   closed.

2. **N-02 is closed for the endpoints that matter.** `bound_authority` was extracted into one
   shared function and applied to policy tunables, action-policies, undo, delegate,
   history/delete, renotify, and the `resolve`/`close` alert transitions. Probed live with
   the production ops token: all refused, 403, on paths chosen so that neither outcome could
   mutate anything. A `policy.widen` action type now exists — `HUMAN_APPROVAL`, approver CEO
   — so weakening a control is itself a governed decision. **Residual: `cancel`,
   `acknowledge`, `assign` and `escalate` on the same alert object remain unbound and still
   take `actor` from the request body** (A-03). `cancel` removes an alert from the live
   surface exactly as `resolve` does.

3. ~~**The alert escalation channel has never delivered in production.**~~ **THIS CLAIM WAS
   WRONG AND IS WITHDRAWN — see §1a.** 8 escalations to the CEO are recorded in
   `governance_alert_transitions`; 8 matching in-app notices exist; the `staff_email_ledger`
   contained **zero rows of any `alert%` kind**. I concluded from the empty ledger that no
   email was sent. That inference was invalid: `begin_send` fails *open*, and alert mail was
   in fact being delivered **unrecorded**. The channel works. The surviving finding is an
   auditability gap, restated at §1a and MEDIUM rather than HIGH (A-01).

4. **Acknowledging an overdue alert is impossible.** `sweep_sla` re-escalates any alert in
   `open|assigned|acknowledged|in_progress` whose `due_at < now()`, and no transition moves
   `due_at`. Production history: the CEO acknowledged alert `b9a224e2` at 15:55:06 and the
   sweep re-escalated it at 15:56:28 — **82 seconds later** (A-02). The only escape is
   `resolve`, which is why two production alerts now carry the CEO's resolution note
   `"remove them"` — an instruction typed into an audit-only field.

5. **The 18 shipped orders were drained, and no customer was told.** The alert was resolved
   2026-09-08 13:35 with `"replayed 39 orphaned row(s); processed 39; order.shipped:ok 18"`.
   The database says those 18 `order_notifications` rows are **all `state='skipped'`** —
   recipient unverified, provider never called. Production has accepted **zero** customer
   notifications since 2026-09-05: 372 in five days, 100% skipped, because all 129 production
   contacts sit on `seed.agentorc.ca`, a deliberately blocked domain. The suppression is
   correct. **Nothing measures or reports it** (A-04, A-05).

6. **The deployed governance console is a stale build.** `agentorc.ca/governance-mgmt.html`
   is byte-identical (SHA-256 `53dd741ab437c305…`) to the local backup
   `governance-mgmt.html.bak2`. The working copy — which relabels *Resolve* to *Mark handled*
   and tells the operator "the note is kept for audit only; it is NOT an instruction" — is
   **not deployed** (A-06). Production already contains the exact failure that fix exists to
   prevent.
   **UPDATE 2026-09-10: the drift half of A-06 is CLOSED — the working copy is deployed and
   verified. The structural half (the console is untracked, unreviewed and has no drift
   check) remains OPEN, and a second instance of the failure occurred before the fix
   shipped. See §1a.**

**Did the remediation improve the system?** Materially, yes, and in the places that were
hardest. The test suite went from 25 failures to 4. The identity break in the audit chain is
closed. Segregation of duties between deciding and policy-setting is real and enforced.

**Did it merely change the mechanism anywhere?** In one place: D-04. The orphan *detector*
was fixed, the orphans were drained, and the alert was resolved — while the business outcome
the control existed to protect moved from *visible and unmet* to *invisible and unmet*.

**Verdict: C — ARCHITECTURE NOT YET ACCEPTABLE.** §21 gives the reasoning, §19 the shortest
path. The blockers are no longer design defects in the governance plane. They are three
controls that are wired but not connected, and one anonymous data surface open since the
first assessment.

---

## 1a. Correction Record — 2026-09-10

**Appended, not rewritten.** The original claims above are struck through rather than deleted:
an audit document that silently revises its own history is worth less than one that shows where
it was wrong. Two findings change, one closes, three observations are added.

### C-1 · A-01 is REFUTED as written. The escalation email channel works.

**Original claim:** *"The alert escalation channel has never delivered in production."*
**Status: WITHDRAWN.**

**Refuting evidence, two independent channels:**

1. **DIRECTLY OBSERVED (recipient mailbox).** A screenshot of `cto@agentorc.ca` shows
   `[Action needed] Alert assigned to you: Agent platform degraded — LLM failover: broken`,
   received **2026-09-09 12:00 EDT (16:00 UTC)** — the assignment mail for alert
   `757f77d3`, delivered at a time when `staff_email_ledger` held **zero** `alert%` rows.
   Delivery without a ledger row is therefore a *proven* combination.
2. **DATABASE-VERIFIED (production).** On 2026-09-10 the ledger recorded both kinds:

   | kind | recipient | state | at |
   |---|---|---|---|
   | `alert_assigned` | `cfo@agentorc.ca` | **accepted** | 2026-09-10 13:00:10 |
   | `alert_escalated` | `ceo@agentorc.ca` | **accepted** | 2026-09-10 16:03:38 |

   The second is the escalation of alert `757f77d3`, which breached at 16:00:25 and was
   escalated by `sla-sweep` at 16:03:38.

**Why the original conclusion was wrong.** I treated an empty ledger as proof of
non-delivery. `staff_email.begin_send` fails **open**, and says so in its own comment:
*"an undeclared kind costs the send its LEDGER ROW, never the message."* I read that comment
during the assessment and did not apply it. §3 of this report states the rule that should
have caught it — *"absence of a record is evidence only when the record is written on both
outcomes"* — and I applied it to `staff_email_observations` while failing to apply it to the
ledger itself. `docs/assessment_2026-09-09_defects_A_B_C_r2.md` had already recorded the
correct reading ("no `alert%` rows **while alert mail demonstrably sends**"); I discounted it
as a local-only observation. It was right.

**A-01 RESTATED, and re-ranked MEDIUM (was HIGH):**

> For at least the 8 escalations of 2026-09-07/08 and the assignment of 2026-09-09, alert mail
> was **delivered but not recorded** in `staff_email_ledger`. The ledger is the system of
> record for staff email; it silently under-reported alert mail, so delivery could not be
> verified from the system of record — and an assessor reading it (this one) reached the
> opposite conclusion. The defect is **auditability**, not reliability.

**Cause of the change on 2026-09-10: NOT PROVEN.** It correlates with the 12:49 UTC process
restart, but the only code change in that deploy was two log levels; no migration has touched
the email-kind vocabulary since 2026-08-23 and the CHECK constraint already permitted all nine
kinds at the time of the original measurement; and `staff_email.py` contains no latched
"ledger unavailable" flag or cache that a restart would clear. **This remains open and is the
most valuable unexplained behaviour in the report** — a ledger that stops recording and later
resumes, for unknown reasons, is a worse auditability property than one that never recorded.

**Consequences for the rest of the report:**

- **D-15 is no longer falsified.** Commit `1877fdc`'s decision that email is the paging
  channel stands: the channel delivers.
- **§17.3 link 5 ("Escalation email — BREAK")** is corrected to: *delivered; recording
  unreliable.* Links 6 and 7 (acknowledgement, unbound transitions) are unaffected and remain
  broken.
- **N-05 is unaffected.** The `governance_alerts.py` DEBUG swallows are still there; they were
  simply not the cause of an outage that did not occur.

### C-2 · The diagnostic deployed for A-01 produced a negative result

`app/core/governance_alerts.py:326` and `:349` were raised from `logger.debug` to
`logger.warning` (commit `cfc5793`, merged as `a36167ad800b`, deployed and verified at
2026-09-10 12:49 UTC).

The 16:03:38 escalation ran on that build and its ledger row reads `state='accepted'`, which
`finish_send` writes only **after** a successful provider response. The `try` block at lines
332–347 therefore completed without raising. **Line 349 did not fire.** The branch identified
pre-change as the sole remaining candidate did not occur.

### C-3 · A-06 — drift half CLOSED, structural half OPEN, and a second incident occurred

**CLOSED (DEPLOYMENT-VERIFIED, 2026-09-10).** `agentorc.ca/governance-mgmt.html` now hashes
`9bfb61737f1df4a485070d0b…`, identical to the working copy. `Mark handled` ×2,
`NOT an instruction` ×1, `No action was sent` ×2, and the misleading
`note (kept on the record)` prompt returns **0** occurrences.

**OPEN.** The console is still untracked, unreviewed and has no drift check. Nothing prevents
recurrence; the drift was found by hashing, not by a control.

**A second incident occurred before the fix shipped — DATABASE-VERIFIED.** On 2026-09-10
20:19:41, `cfo@agentorc.ca` resolved alert `757f77d3` with
`resolution_note: "Please investigate root cause, then fix it"`. Nothing ran. Verified by
exhaustive search of production after that timestamp:

| `a2a_dispatches` | `event_queue` | `activities` | `agent_utterances` | `agent_blackboard` | `notifications` | `action_approvals` | `audit_log` | `staff_email_ledger` |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

The instruction text exists in **exactly one place in the entire database**:
`governance_alert_transitions.note`.

**THE FULL EXTENT, MEASURED — this is not two incidents, it is the normal way this system is
operated.** Every `resolution_note` in production, in order:

| # | Alert | Rule | Resolved by | Note | Closure evidence |
|---|---|---|---|---|---|
| 1 | `34fe6159` | `event_orphaned` | `agent-bus:drain` | "replayed 39…; processed 39; `order.shipped:ok` 18" — **the claim A-04 refutes** | NULL |
| 2 | `b9a224e2` | `bus_stalled` | CEO | "remove them" | NULL |
| 3 | `b899a98b` | `platform_degraded` | CEO | "remove them" | NULL |
| 4 | `9778c106` | `unbilled_orders` | CEO | **"bill these 5 shipped orders"** | NULL |
| 5 | `0be8550e` | `stalled_deals` | CEO | "check the root cause, then fix it" | NULL |
| 6 | `7c4fe977` | `unworked_leads` | CEO | "find the root cause, then resolve the issue" | NULL |
| 7 | `ea82d41c` | `ar_spike` | CEO | **"email this alter to CFO"** | NULL |
| 8 | `757f77d3` | `platform_degraded` | CFO | "Please investigate root cause, then fix it" | NULL |

**Seven of eight resolutions are instructions that never executed. All eight have
`closure_evidence: NULL`. Not one alert has ever reached `closed`.** Row 7 is the incident the
console fix's own comment describes — the CEO typed "email this alter to CFO" and reasonably
believed the CFO had been emailed; no mail was sent.

**And the consequence is already visible in the data.** Every one of the four alerts open in
production right now is a **re-raise of a rule that was previously "resolved" with an
instruction that never ran**:

| Open now | Rule | Previously "resolved" as | With the note |
|---|---|---|---|
| `488dece0` | `unbilled_orders` | `9778c106` | "bill these 5 shipped orders" |
| `ce4ef295` | `unworked_leads` | `7c4fe977` | "find the root cause, then resolve the issue" |
| `ff6d97e0` | `stalled_deals` | `0be8550e` | "check the root cause, then fix it" |
| `bc99c3ee` | `ar_spike` | `ea82d41c` | "email this alter to CFO" |

**4 of 4.** The work was never done, so the detector fired again. This is the strongest
single piece of evidence in either assessment that the governance layer is *measured but not
operated*: the alerts are correct, the owners are correct, the SLAs are correct, the
executives are engaging with them — and the loop closes on a text field that nothing reads.

Deploying the console fix stops the **ninth** instance. It does not correct the eight existing
records, and it does not do the work those four open alerts are asking for.

### C-4 · NEW · Alert transitions have no role affinity (A-15 · MEDIUM)

**DATABASE-VERIFIED + CODE-INSPECTED.** Alert `757f77d3` is owned by **CTO Bill Wang**. It was
acknowledged and resolved by **`cfo@agentorc.ca`**.

`governance_policy.bound_authority` checks only that the session resolves to an *eligible
executive*; it performs no comparison against the alert's `accountable_owner_id`, and
`api_step` sets `actor = ex["email"]` unconditionally. Approvals are different: `_authority_check`
enforces role affinity, so a CFO may not decide a CRO's proposal and the CEO may.

So the accountability object that names one executive as accountable can be discharged by any
of the other four, and the record shows it as properly resolved. Whether that is intended is a
governance decision, not a bug report — but it is currently undeclared either way.

### C-5 · A-02 is unchanged and still live

The 2026-09-10 sequence did not re-exercise it (the CFO acknowledged at 20:18:49 and resolved
52 seconds later, before the next sweep). The finding stands on the 2026-09-08 evidence:
acknowledged 15:55:05 → re-escalated 15:56:28, **82 seconds**.

### Revised severity after this correction record

| ID | Was | Now | Why |
|---|---|---|---|
| **A-01** | HIGH | **MEDIUM** | Delivery works; the defect is unreliable recording, cause unproven |
| **A-06** | HIGH | **HIGH** | Drift closed, but the measured extent is far larger than first reported: **7 of 8 resolutions are unexecuted instructions, 8 of 8 have no closure evidence, and 4 of 4 currently-open alerts are re-raises of them** |
| **A-02** | HIGH | **HIGH** | Unchanged — still the defect that manufactures A-06's failure |
| **A-03** | HIGH | **HIGH** | Unchanged |
| **A-04** | HIGH | **HIGH** | Unchanged |
| **A-05** | HIGH | **HIGH** | Unchanged |
| **A-15** | — | **MEDIUM** | New (C-4) |

**The §21 verdict is unchanged: C — ARCHITECTURE NOT YET ACCEPTABLE.** One HIGH finding was
downgraded on evidence (A-01); one had its drift half closed and its severity *sustained* on a
much larger measured extent (A-06); four HIGH findings remain open; and C-1 adds an
unexplained auditability behaviour that did not exist in the original register.

**The centre of gravity has moved.** The original report framed the problem as *controls that
are wired but not connected*. C-3 shows something sharper: the alert loop is connected at every
technical link — detection, ownership, SLA, email, escalation, transition, history — and closes
on a **free-text field that nothing reads**, with 4 of 4 open alerts proving the work did not
happen. The governance layer's most operated path terminates in a record that looks like an
outcome and is not one. That is the same failure shape as A-04, arrived at independently.

---

## 2. Assessment Scope and Method

Executed in this order:

1. Read the September 7 baseline in full; extracted every material finding into the matrix at
   §5. No status was carried forward.
2. Established the system of record: local branch, `origin/master`, and the **deployed
   commit** via `/health`.
3. Opened four independent evidence channels:
   - the running production application's HTTP surface, anonymous and with the ops token;
   - **direct read-only SQL against the Railway production database** — a channel the
     September 7 review declared unavailable, and the single largest improvement in evidence
     quality between the two reviews;
   - the local PostgreSQL 17.9 database;
   - the repository, the test suite (3,033 tests executed here),
     `scripts/verify_invariants`, and the CI gate configuration.
4. Re-tested every baseline finding against live evidence rather than against its fix commit.
5. Traced four critical properties end to end (§17), stopping at neither the mechanism nor
   the test.
6. Ran negative-path probes designed so that **neither outcome could mutate production**
   (§3).
7. Conducted an independent adversarial pass free to find defects neither assessment
   considered (§9).

**Nothing in this document was taken from `README.md`, `skills.md`, the remediation plans or
the baseline without re-measurement.**

**No defect found during this review was fixed during this review.**

### Declared limits of this review

- **Mail delivery was not confirmed at the recipient.** The `info@agentorc.ca` IMAP archive
  was not read. Positive delivery claims rest on the `staff_email_ledger` provider outcome —
  stronger than a boolean, weaker than an independent inbox. Where the ledger is *empty*, the
  absence is decisive in the other direction and is used that way.
- **The exact failure branch of A-01 is not proven.** Two candidate branches exist and both
  are silent. What is proven is the outcome: 8 escalations, 0 emails, 0 diagnostics above
  DEBUG.
- **Live mutating probes on production were not performed.** Every negative probe was
  constructed so that a *failed* control would still change nothing. Where that was not
  possible — executing `policy.widen`, minting a valid decision link — the property is
  reported as CODE-INSPECTED plus a non-mutating live probe, never as REPRODUCED. The
  operations that would close those gaps are listed in §20.
- **One correction to my own working notes,** recorded because the method matters: I first
  queried `policy_changes`, found no such relation, and drafted a finding that the
  policy-change audit table was missing from production. The table is
  `governance_policy_changes`; it exists on both databases. Production holds 0 rows because
  no policy has ever been changed there. A finding that rests on my own spelling is not a
  finding.

---

## 3. Evidence Standard

Every conclusion carries one of:

| Grade | Meaning |
|---|---|
| **DIRECTLY OBSERVED** | Read from a running system during this review |
| **REPRODUCED** | A probe run here produced the stated result, repeatably |
| **TEST-VERIFIED** | A test executed in this review asserts it |
| **DATABASE-VERIFIED** | Read by SQL from the named database during this review |
| **DEPLOYMENT-VERIFIED** | Measured against the deployed artefact, commit-confirmed |
| **CODE-INSPECTED** | Read from source; not exercised |
| **DOCUMENTED ONLY** | Asserted in a document; not measured |
| **INFERRED** | Concluded from evidence; could be wrong |
| **NOT PROVEN** | The property could not be demonstrated |

**DOCUMENTED ONLY, INFERRED and NOT PROVEN support no claim of operational correctness in
this report.**

Two rules were applied strictly, and each changed a conclusion:

- **A handler returning `ok` is not a business outcome.** The orphan drain reported
  `order.shipped:ok ×18`. The notification rows say `skipped ×18`. `notify()` returns
  `{"status": "ok"}` for `skipped`, `queued`, `accepted` and `already_*` alike, so the drain's
  own breakdown cannot distinguish "the customer was told" from "the customer was not told".
  The alert was resolved on the former reading.
- **Absence of a record is evidence only when the record is written on both outcomes.**
  `staff_email_observations` records refusals as well as sends — but only on the `digest` and
  `approval` paths. An empty observation set for `alert_escalated` therefore proves nothing
  by itself. The empty *ledger*, against 8 recorded escalations and 5 equivalent rows on
  local, does.

### Probe safety

Every negative probe against production was constructed so a *broken* control could not
mutate anything:

| Probe | Why it is safe either way |
|---|---|
| `PUT /governance/policies/{unknown-key}` | `bound_authority` runs before the key lookup; an unbound handler returns "unknown policy" and writes nothing |
| `PUT /governance/action-policies/{unknown}` with a non-editable field | same ordering; `set_policy` raises "nothing to change" before any DML |
| Alert transitions against a non-existent alert id | `transition()` returns "alert not found" on a zero-row `UPDATE` |
| `POST /a2a/dispatch` with `dry_run: true` | returns before the principal, parameter and governance gates |
| Decision links with forged tokens | the GET handler renders only |

---

## 4. Current System State

### 4.1 Source and deployment

| Property | Value | Grade |
|---|---|---|
| Working branch | `p2-assessment-working` @ `688fbbc`, fully merged | DIRECTLY OBSERVED |
| `origin/master` | `43c72ebcdb10` (PR #75) | DIRECTLY OBSERVED |
| **Deployed commit** | **`43c72ebcdb10` = `origin/master` HEAD** | **DEPLOYMENT-VERIFIED** (`/health.commit`) |
| Local `master` | `21ec775`, **54 commits behind** `origin/master` | DIRECTLY OBSERVED |
| Untracked in working tree | `governance-mgmt.html.bak`, `.bak2` | DIRECTLY OBSERVED |

**Every backend change assessed here is live.** The frontend is not — see A-06.

### 4.2 Runtime

| Property | Value | Grade |
|---|---|---|
| Version / deployment id | 2.2.0 · `52c75d28-…` | DEPLOYMENT-VERIFIED |
| Process | `debug: false`, `reload: false`, `WEB_CONCURRENCY=2`, `workers_effective: 2` | DEPLOYMENT-VERIFIED |
| DB role in use | `crm_app` (unprivileged) | DEPLOYMENT-VERIFIED |
| Scheduler | running on the leader, **36 jobs**, `last_tick` current | DEPLOYMENT-VERIFIED |
| HA | leader elected, single node, no automatic failover | DEPLOYMENT-VERIFIED |
| Pool | `pool_max 16`, utilisation ~0% | DEPLOYMENT-VERIFIED |
| Posture | `public-read` | DEPLOYMENT-VERIFIED |

### 4.3 Databases

| Property | Production (Railway) | Local | Grade |
|---|---|---|---|
| PostgreSQL | **18.6** | **17.9** | DATABASE-VERIFIED |
| Tables | 166 | 163 | DATABASE-VERIFIED |
| FK constraints | 185 | 164 | DATABASE-VERIFIED |
| RLS policies | **0** | **0** | DATABASE-VERIFIED |
| `audit_log` | 15,197 rows, **no actor column** | no actor column | DATABASE-VERIFIED |
| `record_field_history` | **26 rows** | 1,805 | DATABASE-VERIFIED |
| `capability_registry` | 46 rows, **0 with `allowed_callers`** | 0 | DATABASE-VERIFIED |
| `agent_capability_grants` | **0** | — | DATABASE-VERIFIED |
| `governance_action_policies` | 38 | 38 | DATABASE-VERIFIED |
| `governance_policy_changes` | **0 rows** | 181 | DATABASE-VERIFIED |
| `eval_runs` / `kb_coverage_runs` | absent | absent | DATABASE-VERIFIED |
| `action_approvals.decision_link_*` | present (3 columns) | present | DATABASE-VERIFIED |

Production runs a **different major version** of PostgreSQL from the database on which every
schema, trigger, constraint and function fact in both assessments was measured (A-10).

### 4.4 Governance state (production)

| Metric | Value | Grade |
|---|---|---|
| Authorities | 5 (CEO, CRO, CFO, CTO, COO), all `is_active`, all `auto_email_enabled` | DATABASE-VERIFIED |
| `confidence_grants_authority` | **false** | DEPLOYMENT-VERIFIED |
| `undeclared_write_capabilities` | **[]** | DEPLOYMENT-VERIFIED |
| Auto-executing policies | 2 of 38 (`order.cancel`, `email.send_payment_reminder`) | DATABASE-VERIFIED |
| `policy.widen` | present · `HUMAN_APPROVAL` · approver **CEO** · not auto | DATABASE-VERIFIED |
| Approvals | 1 pending · 12 executed · 60 expired (none since 2026-09-06) | DATABASE-VERIFIED |
| `decisions_30d_by_decider` | `{email-link: 6, cfo@agentorc.ca: 3}` | DEPLOYMENT-VERIFIED |
| Decisions ever made through an authenticated **session** | **0** | DATABASE-VERIFIED |
| Alerts | 4 open · 7 resolved · 0 cancelled · 0 closed | DATABASE-VERIFIED |
| Alert transitions | 11 open · 12 acknowledged · 8 escalated · 7 resolved | DATABASE-VERIFIED |
| Event queue | depth 0 · orphaned 0 · durable-orphaned 0 · failed 0 · stuck 0 | DEPLOYMENT-VERIFIED |

### 4.5 Ownership and accountability (production)

Measured as a **property** (does the owner satisfy `fn_owner_eligible`) rather than as a
count (is `owner_id` non-null):

| Entity | Rows | `owner_id` set | Owner is an **eligible accountable human** | Grade |
|---|---|---|---|---|
| `orders` | 2,439 | 58 (2.4%) | **0** | DATABASE-VERIFIED |
| `activities` | 14,280 | 11,418 (80%) | **297 (2.6% of owned)** | DATABASE-VERIFIED |
| `owners` | 52 | — | 12 eligible; 39 are customer contacts | DATABASE-VERIFIED |
| workflow-created activities | 522 | — | **22 (4.21%)** — reported by `/platform/health` | DEPLOYMENT-VERIFIED |

### 4.6 Tests and verification tooling

| Run | Result |
|---|---|
| Full suite, this review | **4 failed · 3,027 passed · 2 skipped** (4m13s) |
| Baseline, 2026-09-07 | 25 failed · 2,891 passed · 2 skipped |
| Same suite, second run with `-x` | failed on a **different** test (A-07) |
| The 4 failures in isolation | `TestApproval` — **all 11 pass**; hybrid-retrieval — still fail |
| `scripts/verify_invariants` | **all invariants hold**; one TODO (application DSN) |
| CI gate `CONTROL_TESTS` | 12 files; **the governance-activation suite is not among them** |

---

## 5. September 7 Findings — Current Status

Legend: **RESOLVED — PROVEN** · **RESOLVED — PARTIALLY PROVEN** · **PARTIALLY RESOLVED** ·
**OPEN** · **REGRESSED** · **SUPERSEDED** · **NOT PROVEN** · **NO LONGER APPLICABLE**

### 5.1 Baseline defect register (D-01…D-22)

| ID | Original finding | Original sev | Current status | Current evidence | Grade |
|---|---|---|---|---|---|
| **D-01** | Production serves contact PII to anonymous callers | P0 | **OPEN** | Anonymous `POST /contact-chat {mode:list}` returned "Page 2 of 43 \| Total: 129 contacts" with full names, emails, phones, **street addresses**, staff names ("Created by: Karen Patel") and internal `contact_id`/`account_id`/`owner_id` UUIDs. `/openapi.json` = 284 KB anonymous. | DEPLOYMENT-VERIFIED |
| **D-02** | Business records have no accountable human owner | P1 | **OPEN — and now precisely quantified as worse** | `orders` 2,439 rows / 58 with an owner / **0 with an *eligible* owner**. `activities` 11,418 owned / **297 eligible (2.6%)**. `owners` 52 rows / 12 eligible / 39 are customer contacts. | DATABASE-VERIFIED (prod) |
| **D-03** | Governance queue not operated; 89% expire | P1 | **PARTIALLY RESOLVED** | Expiry stopped: last `system` expiry 2026-09-06; 0 since. `median_decision_hours 0.4`, `breach_rate_7d 0%`. 3 of 9 recent decisions name a human. But **0 decisions ever made through an authenticated session**, and `governance_policy_changes` is empty — the console path is enforced and unexercised. | DATABASE-VERIFIED |
| **D-04 (detection)** | Bus orphaned 85h; detector silent after 24h | P1 | **RESOLVED — PROVEN** | `queue_orphaned` = 0, `queue_orphaned_durable` = 0 on production; independently confirmed by `scripts/classify_orphaned_events --target railway` → "orphaned events: 0". Alert `34fe6159` shows the full lifecycle: open → escalated → acknowledged → resolved, with actor and note per transition. | DEPLOYMENT + DATABASE-VERIFIED |
| **D-04 (outcome)** | 18 shipped orders unnotified | P1 | **OPEN — and no longer visible** | The drain created exactly 18 `order.shipped` rows at 2026-09-08 13:35:34–13:35:44, **all `state='skipped'`**, `provider` NULL, `attempts` 0. `accepted` since the drain: **0**. See A-04. | DATABASE-VERIFIED |
| **D-05** | `audit_log` has no actor | P1 | **OPEN** | Production `audit_log` columns: `audit_id, entity, entity_id, action, payload, created_at` — 15,197 rows, no actor. `record_field_history` holds **26 rows** in production (A-08). | DATABASE-VERIFIED |
| **D-06** | Approval execution not atomic or race-safe | P1 | **RESOLVED — PROVEN** | `approvals_stranded: 0`; `test_concurrent_approvals_execute_exactly_once` and `test_a_stranded_execution_is_recovered_and_alerted` both pass. | DEPLOYMENT + TEST-VERIFIED |
| **D-07** | AUTOACT live behind one tunable threshold | P1 | **RESOLVED — PROVEN** | `confidence_grants_authority: false`; 2 of 38 policies auto/sampled; `decision_required: []`; `undeclared_write_capabilities: []`. The one path that can widen a policy is now itself governed. | DEPLOYMENT + DATABASE-VERIFIED |
| **D-08** | Production superuser DSN on developer laptop | P1 | **OPEN** | `.env` still holds `RAILWAY_DB_URL=postgresql://postgres@…`. I used it, read-only, to produce much of this report — which is the proof. `verify_invariants` still reports the TODO. | DIRECTLY OBSERVED |
| **D-09** | 70% of tables without FKs; 0 RLS | P2 | **OPEN** | Production: 166 tables, 185 FK constraints, **0 RLS policies**. | DATABASE-VERIFIED |
| **D-10** | Migration integrity split: two checks, two answers | P2 | **PARTIALLY RESOLVED** | `/deploy/migrations` still returns `ok: false`, but now with `missing: []`, `out_of_order: true` and `note: "schema is current"`. The *contradiction* is gone; the **red signal for a non-problem** remains. | DEPLOYMENT-VERIFIED |
| **D-11** | Status endpoints report per-worker state | P2 | **RESOLVED — PARTIALLY PROVEN** | `/agent-bus/status` returns `this_process_role`, a `cluster` block and an explanatory note. **`/health` was not given the same treatment**: a follower answers `scheduler.running: null, jobs: 0` with no note (A-13). | DEPLOYMENT-VERIFIED |
| **D-12** | Retrieval abstention not gated | P2 | **OPEN** | No `kb_coverage_runs` table on production. | DATABASE-VERIFIED |
| **D-13** | One real role; no least privilege | P2 | **PARTIALLY RESOLVED** | The a2a surface was split: `GET` capabilities/registry/observed-callers → governance actor; `POST` dispatch/sync → admin. An executive can now read what they are deciding about without platform-admin rights. Rep/manager roles still absent. | CODE-INSPECTED + DEPLOYMENT-VERIFIED (403/200 split probed) |
| **D-14** | Silent degradation at DEBUG in ~40 places | P2 | **OPEN — with a proven consequence** | 20 `logger.debug` swallows remain in `governance*.py` alone. A-01 is what one of them costs. | CODE-INSPECTED + DATABASE-VERIFIED |
| **D-15** | No OTel/metrics export; alerts stay in-app | P2 | **OPEN — the decision is now falsified** | Email was declared the paging channel. For alerts it has delivered nothing (A-01). The decision was sound; its premise is not true in production. | DATABASE-VERIFIED |
| **D-16** | `allowed_callers` never seeded | P3 | **OPEN** | 46 capabilities, **0** with `allowed_callers`. Guardrail layer 4 remains inert. | DATABASE-VERIFIED |
| **D-17** | `postdeploy_verify` self-description contradicts itself | P3 | **OPEN** | Docstring: "WRITES: verify_invariants and red_team both MUTATE rows". `argparse` 39 lines later: "Read-only: … never writes to the target." | CODE-INSPECTED |
| **D-18** | Eval results not persisted | P3 | **OPEN** | No `eval_runs` table on production. | DATABASE-VERIFIED |
| **D-19** | Consensus attestation never wired | P3 | **OPEN** | `/deploy/consensus` → `{"ok": true, "replicas": 0, "note": "no recent attestations"}`. A dead control still reporting success. | DEPLOYMENT-VERIFIED |
| **D-20** | `structuredIntent` accepted from request body | P3 | **OPEN** | Declared on the accounts, contacts and leads routers; read in each graph. | CODE-INSPECTED |
| **D-21** | pgvector written, numpy read path | P2 | **OPEN — regression unfixed and widened** | `test_recency_only_output_is_frozen` still fails (`payment reminder` 2 ≠ 4). Two further tests now fail on genuine corpus drift (A-09). | TEST-VERIFIED |
| **D-22** | Local `master` behind `origin/master` | P3 | **REGRESSED** | 18 → 34 → **54** commits behind. Cosmetic; the deployed artefact is correct. | DIRECTLY OBSERVED |

### 5.2 September 7 new findings (N-01…N-12)

| ID | Original finding | Current status | Current evidence | Grade |
|---|---|---|---|---|
| **N-01** | Unauthenticated GET executes governed actions; no expiry; no eligibility check; BCC'd to a shared mailbox; re-issued every 24h | **RESOLVED — PROVEN (mechanism)** / **NOT PROVEN (authenticated human)** | Every sub-defect closed and verified — see §17.1. Residual: possession of a valid 72h link still decides *as* the named executive, so `decided_actor` is a claim from token possession, not from an authenticated session. | DEPLOYMENT-VERIFIED (mechanism); NOT PROVEN (authentication) |
| **N-02** | Policy-setting not bound to an executive while deciding is | **RESOLVED — PARTIALLY PROVEN** | 3 → 9+ bound endpoints; `updated_by` accepted and ignored on both policy models; `policy.widen` governs widening. Live 403 on tunables, action-policies and undo with the ops token. **Residual: 4 alert transitions unbound with a body-supplied actor (A-03).** | DEPLOYMENT-VERIFIED |
| **N-03** | Anonymous PII plus a published 413-path OpenAPI map | **OPEN** | Unchanged; 284 KB OpenAPI anonymous. Governance and ops surfaces correctly 403 on all 7 paths re-probed. | DEPLOYMENT-VERIFIED |
| **N-04** | `expired_7d` reported as two values under one label | **OPEN — unchanged** | Live, same minute: `/governance/status.expired_7d = 4`; `/platform/health.approvals_expired = 0`. Cause unchanged: `governance.py:2111` anchors on `decided_at`, `platform_health.py:468` on `created_at`. | DEPLOYMENT-VERIFIED + CODE-INSPECTED |
| **N-05** | Escalation send outcome logged at DEBUG and counted nowhere | **PARTIALLY RESOLVED — with a proven consequence** | Fixed in `governance.py` (reminder failure is now `logger.warning`). **Not fixed in `governance_alerts.py`**: lines 326 and 349 still swallow the in-app notice and the escalation email at DEBUG. That is the mechanism behind A-01. | CODE-INSPECTED + DATABASE-VERIFIED |
| **N-06** | Owner eligibility checked at write time, never re-validated | **OPEN** | `trg_action_approvals_owner_eligible` is still `BEFORE INSERT OR UPDATE OF accountable_owner_id, status`. | DATABASE-VERIFIED (local) |
| **N-07** | `APP_URL` overloaded as link base and deployment signal | **PARTIALLY RESOLVED** | Both uses remain (`governance.py:2391`, `release_guard.py:58`), but `release_guard` now carries an explicit named check that distinguishes "unset", "local" and "deployed" and says which links would be emitted. The coupling is declared rather than removed. | CODE-INSPECTED |
| **N-08** | 782 alerts cancelled in 2 days | **RESOLVED in production · REGRESSED locally** | Production: 11 alerts total, **0 cancelled**, 7 resolved, 4 open. Local: **1,982 cancelled** of 2,389 (83%). The flood is a local/dev artefact; production alert usage is healthy in shape. | DATABASE-VERIFIED |
| **N-09** | Governance suite outside the CI gate | **OPEN — attempted, reverted, reason declared** | `verify_gate.py` now contains a 30-line record of the attempt: adding the four governance suites turned CI red with 61 failures and 31 undeclared skips, because a gate database has no provisioned executives and nothing may seed credentials in a migration. The gap is real, the reasoning is sound, and the newest controls remain ungated. | CODE-INSPECTED |
| **N-10** | Alert dedupe key `event_orphaned:open` is class-wide | **OPEN** | Unchanged in the production alert record. Latent while no alert of the class is open. | DATABASE-VERIFIED |
| **N-11** | Executive `owner_id` differs between local and production | **OPEN** | All five differ. CEO: local `db6a9f31…`, production `146d0190…`. Accountability records are not portable between the databases. | DATABASE-VERIFIED (both) |
| **N-12** | `/deploy/consensus` returns `ok` while measuring nothing | **OPEN** | Unchanged. | DEPLOYMENT-VERIFIED |

### 5.3 September 7 regressions (R-1…R-6)

| ID | Regression | Current status | Evidence | Grade |
|---|---|---|---|---|
| **R-1** | `is_deployed()` gate silently disabled 21 order-notification tests | **RESOLVED — PROVEN** | The full suite now shows 0 failures in `test_order_lifecycle_notifications.py` and `test_email_send_sp.py`. 23 of the baseline's 25 failures are gone. | TEST-VERIFIED |
| **R-2** | Hybrid-retrieval kill switch no longer restores its pinned contract | **OPEN — unchanged** | `test_recency_only_output_is_frozen` still fails: `{'payment reminder': 2} != 4`. | TEST-VERIFIED |
| **R-3** | Expiry replaced by indefinite escalation with one-click links | **RESOLVED — PROVEN** | Subsumed by N-01's fix: 72h TTL, per-issuance nonce, rotation on every reminder ("reminder n+1 invalidates reminder n"), retirement on decision. Production row `b1875ff2` shows `decision_link_nonce` NULL after execution. | CODE-INSPECTED + DATABASE-VERIFIED |
| **R-4** | Owner-eligibility trigger can make `propose()` fail closed into silence | **OPEN** | `resolve_accountable_owner` still raises; no alert covers the state. Latent: 5 of 5 authorities are eligible today. | CODE-INSPECTED |
| **R-5** | The alert lifecycle breaches its own SLA | **OPEN — and now the dominant alert behaviour** | The orphan alert did breach and escalate. Worse: acknowledgement cannot stop the clock at all (A-02). | DATABASE-VERIFIED |
| **R-6** | `test_K_H_closed_history_is_not_resurrected_as_work` fails | **RESOLVED — PROVEN** | Passes in the full run. | TEST-VERIFIED |

### 5.4 Summary

**8 resolved and proven · 5 partially resolved · 21 open · 1 regressed · 1 resolved-in-prod
but regressed locally.**

The eight clean resolutions (N-01, N-02 core, D-04 detection, D-06, D-07, R-1, R-3, R-6) are
the highest-value items in the register, and they are concentrated exactly where the
September 7 review said the architectural defects were. The open items are concentrated in
Stages 0, 4, 5 and 6 of the original roadmap, which were not attempted.

---

## 6. Verified Improvements

Each of these is stated with the evidence that *proves the property*, not the evidence that
the code exists.

### 6.1 The decision link identifies a person, and the old ones are dead

**Grade: DEPLOYMENT-VERIFIED / REPRODUCED.**

Probed against production:

| Probe | Result |
|---|---|
| `GET /governance/decide?g=…&a=approve&t=<forged>` (old format, no `e`) | **403** — "this decision link predates identity binding and is no longer valid" |
| `GET …&e=<uuid>&t=<forged>` (new format, bad token) | **403** — "not found" |
| `POST /governance/decide` with the same forged values | **403** — "not found" |

The refusal message is itself the proof of the strongest property: **every bearer link ever
BCC'd to `info@agentorc.ca` before this change is refused by shape**, without needing
`GOV_LINK_SECRET` rotated. Rotation moved from urgent to optional, and the code says so
deliberately.

The GET handler contains no mutation: it calls `verify_decision_token` and `_row` (both
reads) and renders a form. A scanner, prefetcher, unfurler or archiving crawler following the
link now sees a confirmation page. *"A scanner follows links, it does not submit forms"* —
and that requires no user-agent allow-list to maintain.

### 6.2 The audit chain's tenth link is closed

**Grade: DATABASE-VERIFIED (production).**

All three decisions since the fix:

| Approval | Action | Authority | `decided_via` | `decided_by` | `decided_actor` |
|---|---|---|---|---|---|
| `b1875ff2` | `supervisor.emit_dunning` | CFO | `email-link` | `cfo@agentorc.ca` | `cfo@agentorc.ca` |
| `b4f52f28` | `supervisor.emit_dunning` | CFO | `email-link` | `cfo@agentorc.ca` | `cfo@agentorc.ca` |
| `d9530ecd` | `supervisor.emit_dunning` | CFO | `email-link` | `cfo@agentorc.ca` | `cfo@agentorc.ca` |

Compare the three immediately before the fix, all `decided_by = decided_actor = "email-link"`,
and the six before those with `decided_actor = NULL`. The transition is visible in the data.

Decision latency also changed shape: 3.4 hours (13:00:19 → 16:23:33) against the baseline's
69-second three-decision burst across two claimed authorities.

### 6.3 Segregation of duties on policy is enforced, and widening is itself governed

**Grade: DEPLOYMENT-VERIFIED.** Probed live with the production ops token:

```
PUT /governance/policies/<key>            → 403 "This changes governance itself and is
PUT /governance/action-policies/<type>    → 403  bound to the signed-in executive …
POST /governance/undo/<uuid>              → 403  a machine token may read the governance
POST /governance/alerts/<id>/resolve      → 403  surface and may not change it."
POST /governance/alerts/<id>/close        → 403
```

`updated_by` is accepted and ignored on both policy models, with the reason written into the
model rather than the field silently deleted. And `governance_action_policies` carries a
`policy.widen` row — `HUMAN_APPROVAL`, approver **CEO** — so *"no control may be weakened by
an identity that could not exercise it"* is now a data-level statement, not a recommendation.

### 6.4 The orphan detector works, and the drain is real

**Grade: DEPLOYMENT + DATABASE-VERIFIED.** Two independent channels agree that production has
zero orphaned events: `/platform/health` (`queue_orphaned`, `queue_orphaned_durable` both 0)
and `scripts/classify_orphaned_events --target railway`, a read-only tool that opens its own
transaction. The alert's `history` array reconstructs the whole incident with actor and note
per transition.

### 6.5 The test seam was restored

**Grade: TEST-VERIFIED.** 23 of the baseline's 25 failures are gone, including all 21
order-notification tests that the `is_deployed()` guard had silently disabled. This was the
baseline's P1 R-1 and it is closed on its merits, not by loosening an assertion.

### 6.6 BCC suppression is structural, not incidental

**Grade: CODE-INSPECTED.** `send_email` now has **three** states rather than two —
`bcc=None` archives as usual, `bcc=NO_BCC` archives nothing, `bcc=<addr>` archives there. The
sentinel exists because `''` is falsy and every falsy value previously meant "use the default
archive". All three governance senders (`route_approval`, `renotify_pending`,
`email_authority`) pass `NO_BCC`, and every decision-link send routes through
`email_authority`.

---

## 7. Partially Resolved / Unproven Findings

| Property | What is proven | What is NOT proven | Grade of the gap |
|---|---|---|---|
| **A governed decision is attributable to an authenticated human** | The record now names a person, and only an eligible executive named in the link's recipient set can produce that record | That the *named person* produced it. Possession of a valid 72h link decides as that executive. **0 decisions have ever been made through an authenticated session in production.** | NOT PROVEN |
| **A machine token cannot change governance** | Proven for tunables, action-policies, undo, delegate, renotify, history/delete, alert resolve/close | **False** for alert `cancel`, `acknowledge`, `assign`, `escalate` (A-03) | REPRODUCED (the gap) |
| **The escalation authority learns of a breach** | The state transition and the in-app notice both occur | The email — the declared paging channel — has never produced a ledger row (A-01) | DATABASE-VERIFIED (the failure) |
| **An orphaned consequential event is drained or explicitly written off** | Drained: yes, verifiably | That the customers were told. They were not, and the alert says otherwise (A-04) | DATABASE-VERIFIED (the failure) |
| **Alert resolution is an attestation by a named human** | `resolve`/`close` are session-bound over HTTP | The same transitions are reachable internally with any actor string (`agent-bus:drain` resolved a governance alert), and the deployed console still presents the note as an instruction (A-06) | CODE-INSPECTED + DATABASE-VERIFIED |
| **The governance plane's tests pass** | 3,027 pass | That the result is reproducible: two runs of the same commit failed on two different tests, and both pass in isolation (A-07) | REPRODUCED |
| **Owner eligibility is enforced** | On two governance tables, by trigger, including the NULL case | On any business entity. **0 of 2,439 orders** name an eligible accountable human | DATABASE-VERIFIED |
| **`policy.widen` cannot be reached without a CEO decision** | `POST /a2a/dispatch` cannot set `govern_bypass` (not on `_DispatchBody`) and supplies no `principal`, which a write capability refuses; `set_policy` refuses widening unless `allow_widening`, whose only caller is `_sp_policy_widen` | End-to-end under adversarial conditions. Proving it requires either executing a widening or attempting one against production (§20) | CODE-INSPECTED |

---

## 8. Regressions

No regression was introduced by the September 7→9 remediation in the sense of *previously
working behaviour that stopped working*. Two second-order effects are material:

### 8.1 D-04 moved from a visible failure to an invisible one

**Grade: DATABASE-VERIFIED.** On September 7 the state was: 39 orphaned events, an open alert
owned by the CTO with an SLA, and 18 customers not told. Today: 0 orphaned events, the alert
`resolved`, `/platform/health` green on that section — and **18 customers still not told**.
The remediation improved the machinery and closed the record. The obligation is unchanged and
is now harder to find. See A-04.

### 8.2 `record_field_history` did not survive the transition to production

**Grade: DATABASE-VERIFIED.** The baseline measured 1,805 rows locally and treated it as
partial coverage of `audit_log`'s 16,997. Production holds **26** rows against 15,197 — 0.17%.
The actor-bearing audit table is effectively empty in the only environment that matters. This
is not new damage; it is a property the baseline could not measure and that the local figure
overstated by two orders of magnitude. See A-08.

### 8.3 What each remediation added (second-order review)

| Remediation | New dependency / assumption | New failure mode | Observed? |
|---|---|---|---|
| GET/POST decide split | A rendered HTML page is now part of the decision path | An executive on a client that blocks form POSTs cannot decide by link | No |
| Token bound to `(uuid, action, executive, nonce)` | `decision_link_*` columns must exist and be writable | `mint_decision_links` returns `{}` on a non-pending row → the mail falls back to "decide in the console" | No; columns present in production |
| Rotation on every reminder | Reminder n+1 kills reminder n | Two executives mailed separately would invalidate each other — **anticipated and handled** by minting a recipient *set* in one issuance | No |
| `bound_authority` extracted and applied broadly | Every governance mutation now needs an executive session | The scheduler cannot call the bound endpoints — **anticipated**: `POST /governance/expire` was deliberately left unbound with the reason recorded | No |
| `policy.widen` as a governed action | A widening now blocks on a CEO decision | If no CEO is eligible, `propose` raises and the widening cannot even be requested (R-4) | No |
| `resolve`/`close` bound to an executive | The bound set is a whitelist | Everything not on the whitelist stayed unbound — including `cancel` (A-03) | **Yes** |
| `is_deployed()` test seam restored | — | — | Fixed |

---

## 9. New Findings

Fourteen findings not present in either previous assessment. Severity, affected component,
violated property, evidence, failure path, consequence, remediation class.

---

### A-01 · ~~HIGH~~ MEDIUM · ~~The alert escalation email has never delivered in production~~

> **⚠ SUPERSEDED BY §1a / C-1 (2026-09-10). The headline claim below is WRONG.** Alert mail
> *is* delivered; it was not being *recorded*. Read §1a before acting on anything in this
> entry. The measurements below are accurate; the conclusion drawn from them is not.

- **Component:** `app/core/governance_alerts.py::escalate` → `governance_policy.email_authority`
- **Violated property:** ~~*An escalated accountability alert reaches its escalation
  authority.*~~ → *An alert notification that was sent is recorded in the ledger that is its
  system of record.*
- **Evidence — DATABASE-VERIFIED (production):**
  - `governance_alert_transitions` where `to_status='escalated'` → **8**
  - `notifications` where `metadata->>'kind'='governance_alert_escalated'` → **8** (so the
    first block ran and `h` was assigned)
  - `staff_email_ledger` distinct `email_kind` over all history → **`approval, digest`**.
    Zero `alert_escalated`, `alert_assigned` or `alert_remind` rows, ever.
  - The same code on the local database has **5 `alert_escalated` and 5 `alert_assigned`**
    ledger rows, so the path is not structurally broken — it fails in the deployed
    environment only.
  - `staff_email_ledger_email_kind_check` on **both** databases permits all nine kinds, so
    the CHECK constraint is **not** the cause. (A candidate ruled out by measurement.)
- **Failure path:** `escalate()` wraps the entire email block in
  `except Exception: logger.debug("[governance_alerts] escalation email skipped")`. The
  alternative branch — `staff_email.begin_send` returning `proceed: False` — returns a dict
  and writes nothing at all. **Both branches produce no ledger row, no observation row, and
  no log line above DEBUG.** The deployed process runs at `debug: false`.
- **Which branch fires is NOT PROVEN.** The outcome is.
- **Consequence:** The alert lifecycle's SLA, escalation role and repeating reminder — the
  centrepiece of the September 6 activation — terminate in silence. Commit `1877fdc`
  recorded the decision that *email is the paging channel, so OTel is unnecessary*. That
  decision is falsified for alerts: there is no paging channel.
- **Likelihood:** Certain; it is the current state, 8 times over.
- **Remediation class:** code (raise the log level and record a refusal reason), then
  **evidence** (§20) to identify the branch.

---

### A-02 · HIGH · Acknowledging an overdue alert re-escalates it within minutes

- **Component:** `app/core/governance_alerts.py::sweep_sla`
- **Violated property:** *Acknowledgement is a usable state.*
- **Evidence — DATABASE-VERIFIED (production), alert `b9a224e2` history:**

  | At | From → To | Actor |
  |---|---|---|
  | 2026-09-08 13:15:45 | open → escalated | `sla-sweep` |
  | 2026-09-08 14:26:58 | escalated → acknowledged | `ceo@agentorc.ca` |
  | 2026-09-08 14:41:28 | acknowledged → **escalated** | `sla-sweep` (**14.5 min later**) |
  | 2026-09-08 15:55:05 | escalated → acknowledged | `ceo@agentorc.ca` |
  | 2026-09-08 15:56:28 | acknowledged → **escalated** | `sla-sweep` (**82 seconds later**) |
  | 2026-09-09 03:29:45 | escalated → acknowledged | `ceo@agentorc.ca` |
  | 2026-09-09 03:30:58 | acknowledged → resolved | `ceo@agentorc.ca`, note `"remove them"` |

  Alert `b899a98b` shows the identical pattern on the same timestamps.
- **Failure path:** `sweep_sla` selects
  `WHERE status IN ('open','assigned','acknowledged','in_progress') AND due_at < now()`.
  No transition writes `due_at`. Once an alert is past due, every 15-minute sweep re-escalates
  it regardless of who has acknowledged it. This is the same class as the recorded scheduler
  trap *"do not age rows off a timestamp the same job stamped"* — here, a state change that
  does not move the deadline it is judged against.
- **Consequence:** The CEO acknowledged the same alert three times and could not make it stay
  acknowledged. The only exit is `resolve` — which is precisely how two production alerts came
  to carry the resolution note `"remove them"`. This defect **manufactures** A-06's failure.
- **Remediation class:** code + a policy decision (does acknowledgement extend the deadline,
  suppress re-escalation for a fixed window, or require an explicit `snooze` transition?).

---

### A-03 · HIGH · Four alert transitions are unbound and take the actor from the request body

- **Component:** `app/core/governance_alerts.py::api_step`, `_BOUND_ACTIONS`
- **Violated property:** *An accountability signal is cleared only by a named, authenticated
  human.*
- **Evidence — REPRODUCED against production** with the ops token, against a non-existent
  alert id so neither outcome could mutate anything:

  | Action | Result |
  |---|---|
  | `resolve` | **403** — bound to the signed-in executive |
  | `close` | **403** — bound to the signed-in executive |
  | `cancel` | **409 "alert not found"** — reached the handler; actor taken from the body |
  | `acknowledge` | **409 "alert not found"** — same |
  | `assign` | **409 "alert not found"** — same |
  | `escalate` | **200 `{"ok": false, "error": "alert not found"}`** — same |

  The probe body supplied `"actor": "audit-probe-not-a-person"` and it was accepted as the
  actor.
- **Why it matters more than it looks:** `cancelled` is not in
  `('open','assigned','acknowledged','in_progress')`, so **cancelling removes an alert from
  the live surface exactly as resolving does**. The code's own reasoning for the whitelist is
  that *"anyone may acknowledge that they have seen an alert; saying it is dealt with is a
  claim about the world"*. Cancelling is the strongest such claim — *this never needed
  working* — and it is the one transition the local database has used 1,982 times.
- **Exploit path:** A holder of `ADMIN_API_TOKEN` (present in `.env` on the developer laptop
  alongside the production superuser DSN, D-08) can clear any accountability alert and record
  any name as having done it. `escalate` additionally lets that token reassign ownership to
  the escalation authority under a fabricated actor.
- **Consequence:** The alert object is the system's accountability record. Three of its six
  state-clearing or ownership-moving transitions accept an unauthenticated identity claim.
- **Remediation class:** code (extend `_BOUND_ACTIONS`), plus a decision on whether
  `acknowledge` should remain open to any operator.

---

### A-04 · HIGH · An alert was resolved on a handler status that does not mean the outcome occurred

- **Component:** `agent-bus drain` → `governance_alerts.resolve_by_class`
- **Violated property:** *A resolution record states what actually happened.*
- **Evidence — DATABASE-VERIFIED (production):**
  - Alert `34fe6159`, `resolved_by: "agent-bus:drain"`, `resolved_at: 2026-09-08T13:35:44`,
    `resolution_note: "replayed 39 orphaned row(s); processed 39; breakdown
    {'order.shipped:ok': 18, 'order.status_changed:ok': 21}"`, `closure_evidence: null`.
  - `order_notifications` created 2026-09-08 13:35:34–13:35:44: **18 rows, `event_type =
    order.shipped`, `state = 'skipped'` on all 18**, `provider` NULL, `attempts` 0,
    `failure_reason` "… is not a verified, deliverable recipient".
  - `accepted` notifications since the drain: **0**.
  - `order_notifications.notify()` returns `{"status": "ok"}` for `skipped`, `queued`,
    `accepted` and `already_*` alike. The drain's breakdown therefore **cannot** distinguish
    "told" from "not told", and the resolution note was written from it.
- **Consequence:** An auditor reading the alert concludes the 18 customers were notified. The
  database says they were not. The September 7 remediation invariant was *"an orphaned
  consequential event is drained or explicitly written off, never aged out"* — it was drained
  into a suppression, and the write-off decision the invariant contemplates was never
  recorded. `closure_evidence` is NULL on **all 11** production alerts; not one has reached
  `closed`.
- **Note on scope:** every affected recipient is on `seed.agentorc.ca`. Recorded corpus
  provenance holds that real-vs-synthetic **cannot** be reconstructed for this data
  (`contacts.is_synthetic` marks 176 of 181 seed-domain contacts *not* synthetic), so "no real
  customer was affected" is **NOT PROVEN** and must not be asserted.
- **Remediation class:** code (the drain must resolve on the notification `state`, not the
  handler status), plus an operational decision to record the write-off.

---

### A-05 · HIGH · The customer-notification path delivers to nobody, and no control reports it

- **Component:** `app/core/order_notifications.py`, `agent_bus._is_real_email`, `/platform/health`
- **Violated property:** *A total outage of a customer-facing delivery path is
  distinguishable from normal operation.*
- **Evidence — DATABASE-VERIFIED (production):**

  | Day | Notifications | Accepted | Skipped |
  |---|---|---|---|
  | 2026-09-10 | 62 | **0** | 62 |
  | 2026-09-09 | 71 | **0** | 71 |
  | 2026-09-08 | 89 | **0** | 89 |
  | 2026-09-07 | 79 | **0** | 79 |
  | 2026-09-06 | 71 | **0** | 71 |
  | 2026-09-05 | 67 | 62 | 5 |
  | 2026-09-04 | 73 | 70 | 3 |
  | …through 2026-08-20 | ~65/day | ~93% | ~5% |

  Last accepted customer notification of any kind: **2026-09-05 02:17**. All **129** production
  contacts with an email are on `seed.agentorc.ca` (112 of them flagged
  `is_email_verified = true`).
- **The suppression itself is correct and well-reasoned.** `seed.agentorc.ca` was added to
  `_PLACEHOLDER_EMAIL_DOMAINS` to close a measured provider-quota leak; the code declines a
  per-domain provenance inference and exempts only named mailboxes via `EMAIL_E2E_ALLOWLIST`,
  empty by default. **The gate is not the defect.**
- **The defect is that nothing measures it.** `/platform/health`'s obligations section — *"Are
  we keeping the promises we made?"* — reports four escalation metrics and **zero**
  notification metrics, and is currently all-green. No supervisor detector references
  `order_notifications`. A revoked provider key, an SMTP outage, or a bad recipient-gate
  deploy would produce exactly the surface the system shows today.
- **Consequence:** The subsystem behind the incident that motivated the entire September 5
  baseline is in a 100%-failure state that the monitoring built in response cannot see, and a
  real regression in it would be indistinguishable from the intended state.
- **Remediation class:** code (a `notification_suppression_rate` metric with an owner) plus a
  decision on what the intended steady state is.

---

### A-06 · HIGH · The deployed governance console is a stale build, and production already contains the failure its fix prevents

- **Component:** `governance-mgmt.html` (untracked; deployed to `agentorc.ca` by hand)
- **Violated property:** *The deployed operator surface matches the reviewed one.*
- **Evidence — DEPLOYMENT-VERIFIED:**
  - `sha256(agentorc.ca/governance-mgmt.html)` = `53dd741ab437c3056dfce7ac…` (78,433 bytes)
  - `sha256(governance-mgmt.html.bak2)` = `53dd741ab437c3056dfce7ac…` — **identical**
  - `sha256(governance-mgmt.html)` (working copy, modified 2026-09-09 13:26) =
    `9bfb61737f1df4a4…` (80,585 bytes) — **not deployed**
  - The undeployed delta relabels *Resolve* → *Mark handled* and replaces the prompt "note
    (kept on the record)" with an explicit statement: *"The note is kept for audit only. It is
    NOT an instruction: no email, agent or action will run from what you type here."* It also
    appends "— recorded. No action was sent." to the success toast.
- **Evidence that the failure has already occurred — DATABASE-VERIFIED:** production alerts
  `b9a224e2` and `b899a98b` are `resolved` by `ceo@agentorc.ca` with
  `resolution_note: "remove them"` — an imperative. Nothing parsed it; nothing ran.
- **Consequence:** Two of the seven resolved production alerts carry a resolution note that is
  an unexecuted instruction, and the executive who wrote them reasonably believed otherwise.
  The audit record for those alerts is misleading about what the human intended and about what
  happened. Compounded by A-02, which left `resolve` as the only exit.
- **Structural note:** the console is deliberately untracked (repo policy excludes `*.html`),
  so **there is no CI, no review and no drift check on the primary operator surface for the
  governance plane.** This is the one artefact class where "deployed state over local state"
  currently has no automated answer.
- **Remediation class:** deployment (ship the working copy), plus operational process (a
  deployed-artefact hash check for the console, of the kind `/health.commit` provides for the
  backend).

---

### A-07 · MEDIUM · The governance test suite is order-dependent and not reproducible

- **Component:** `governance/tests/`
- **Violated property:** *A passing test run means the same thing twice.*
- **Evidence — REPRODUCED:**
  - Run 1 (`pytest -x`): failed at
    `test_governance_activation.py::TestDailyProposalCap::test_a_capped_dispatch_is_a_structured_refusal_not_an_exception`
  - Run 2 (full suite, same commit, same database): that test **passed**; instead
    `test_governance_activation.py::TestApproval::test_a_second_approval_of_the_same_row_is_refused`
    failed — and it failed on the *first* approval (`assert approve(...)["ok"]` → False), never
    reaching the property it names.
  - Run 3 (`TestApproval` in isolation): **all 11 pass in 2.26s.**
- **Mechanism (partly proven):** `propose()` enforces `daily_proposal_cap` against
  `action_approvals` rows created since `date_trunc('day', now())`. `kb.publish` is capped at
  3/day on both databases. Any suite that creates `kb.publish` proposals therefore behaves
  differently depending on what ran before it that calendar day. The `TestApproval` failure has
  a different, unidentified ambient-state cause.
- **Consequence:** "3,027 passed" is not a reproducible statement about the governance plane.
  Combined with N-09 — this suite is the one excluded from the CI gate — the newest and most
  consequential controls are verified by a non-deterministic, ungated suite.
- **Remediation class:** code (test isolation: a per-test date window or a dedicated schema),
  and it is a prerequisite for closing N-09.

---

### A-08 · MEDIUM · The only actor-bearing audit table covers 0.17% of production mutations

- **Component:** `audit_log`, `record_field_history`
- **Violated property:** *Every recorded mutation can be attributed.*
- **Evidence — DATABASE-VERIFIED (production):** `audit_log` = **15,197 rows, no actor
  column**. `record_field_history` = **26 rows** (local: 1,805). Ratio: **0.17%**.
- **Consequence:** D-05 is worse in production than the baseline's local measurement implied.
  Outside the governance chain, "who changed this" is unanswerable for essentially every
  business record.
- **Remediation class:** schema + code; large. Correctly deferred by the original roadmap, but
  the production figure should replace the local one in any planning.

---

### A-09 · MEDIUM · The retrieval vector pool has drifted past its validated state

- **Component:** `app/core/content_index.py`
- **Violated property:** *Semantic retrieval draws from a pool no single template can fill.*
- **Evidence — TEST-VERIFIED:** two tests fail with
  `template crowding worsened: largest group 558 against N_VEC 500 is a ratio of 1.12, versus
  0.96 when the pool size was validated (480 against 500)`. A third logs
  `search ranked 4000 of 14899 matching records (27%) — results are drawn from the most recent
  slice only`.
- **Assessment:** the control is working correctly and is reporting a real change. The largest
  single template group can now **more than fill** the entire vector pool, so one template can
  crowd out semantic retrieval entirely. `revalidate` is correctly still `True` (a permanent,
  deliberate thin-margin disclosure) and `changed_since_validation` has correctly flipped —
  the two-field split the code argues for is doing exactly its job.
- **Consequence:** nobody is receiving the signal. `test_hybrid_retrieval.py` is not in
  `CONTROL_TESTS`, so it fires only when someone runs the full suite by hand.
- **Remediation class:** evidence first (re-derive `N_VEC`), then a decision; plus gating.

---

### A-10 · MEDIUM · Production runs a different major PostgreSQL version from the audited one

- **Component:** database platform
- **Violated property:** *Schema-level evidence describes the system it is cited about.*
- **Evidence — DATABASE-VERIFIED:** production **PostgreSQL 18.6**; local **17.9**.
- **Consequence:** every trigger definition, constraint body, function source and planner-
  dependent behaviour measured locally — in both assessments, and in `verify_invariants` —
  describes a different major version from the deployed one. Nothing observed here is *known*
  to diverge, and nothing is *known* not to.
- **Remediation class:** operational process (run `verify_invariants` against production
  read-only, or align the local major version).

---

### A-11 · LOW · Platform-health detectors report "no problem" for metrics they never computed

- **Component:** `app/core/platform_health.py:625-632`
- **Violated property:** *A health check distinguishes "healthy" from "not measured".*
- **Evidence — CODE-INSPECTED:**
  ```python
  try:
      metrics = platform_metrics()
  except Exception as exc:
      logger.debug(f"[platform_health] detector skipped: {exc}")
      return None          # ← indistinguishable from "nothing is wrong"
  ```
- **Consequence:** if `platform_metrics()` raises, `detect_platform_degraded` returns `None`,
  no alert opens, and the only trace is a DEBUG line in a process running at
  `debug: false`. The D-14 pattern in the newest monitoring code.
- **Remediation class:** code.

---

### A-12 · LOW · `dry_run` reports `accepted` for a dispatch that would be refused three times over

- **Component:** `app/core/a2a.py::dispatch`
- **Evidence — REPRODUCED (production):** a dry-run dispatch of `policy.widen` — the one
  capability that can weaken a governance control — returned
  `{"ok": true, "outcome": "accepted", "output": "[dry-run] would route 'policy.widen' →
  governance via structured SP (data)"}`.
- **Why:** `if dry_run: return` sits *before* the principal gate, the parameter gate and the
  governance gate. That ordering is what makes dry-run a safe probe (§3), and it means dry-run
  answers "would this route" while reading as "would this be permitted".
- **Consequence:** an operator using dry-run as a policy simulator gets a false positive on
  the most consequential capability in the mesh. No security impact; a real reasoning hazard.
- **Remediation class:** code (report the gates that were not evaluated).

---

### A-13 · LOW · `/health` still reports per-worker scheduler state without explanation

- **Component:** `app/main.py` health handler
- **Evidence — DEPLOYMENT-VERIFIED:** a follower answered `scheduler: {running: null, jobs: 0,
  last_tick: null}`, `ha.role: "follower"`, `process.note: null`. Six subsequent calls hit the
  leader: `running: true, jobs: 36`.
- **Consequence:** D-11 was fixed on `/agent-bus/status`, which now carries a `cluster` block
  and a `running_note` explaining which answer to trust. `/health` — the endpoint an operator
  or an uptime probe reaches first — was not given the same treatment, and answers "the
  scheduler is not running" one time in two.
- **Remediation class:** code.

---

### A-14 · INFORMATIONAL · The decision confirmation page discloses the action to any link holder

- **Component:** `governance_decide_confirm`
- **Evidence — CODE-INSPECTED:** the GET renders `action_type`, amount, an action summary, and
  the named executive's full name and email to anyone presenting a valid token.
- **Assessment:** this is the intended and correct design — an executive must see what they are
  deciding before they decide it, and the token is already scoped to one executive and one
  approval for 72h. Recorded because it is the residual disclosure surface of the N-01 fix, not
  because it is a defect.

---

## 10. Security Assessment

### Verified sound

| Property | Evidence | Grade |
|---|---|---|
| Governance and ops endpoints refuse anonymous callers | 7/7 re-probed → 403 (`/governance/status`, `/governance/authorities`, `/governance/alerts`, `/platform/health`, `/agent-bus/status`, `/deploy/migrations`, `/a2a/registry`) | DEPLOYMENT-VERIFIED |
| The app runs unprivileged | `/health.database.connected_as = crm_app`; `verify_invariants` proves the role holds no elevated attributes, owns no tables and cannot create in `public` | DEPLOYMENT + TEST-VERIFIED |
| A machine token cannot decide | 403 on approve/reject/undo with the ops token | REPRODUCED |
| A machine token cannot change policy | 403 on both policy endpoints | REPRODUCED |
| Decision tokens fail closed | No secret → refused; no executive → refused; wrong nonce → refused; not in recipient set → refused; >72h → refused; ineligible executive → refused; non-pending row → "already decided" | CODE-INSPECTED + REPRODUCED (first four) |
| `govern_bypass` is not reachable from HTTP | Absent from `_DispatchBody`; `A2ARequest` constructed without it; the sole setter is the approved-execution re-dispatch | CODE-INSPECTED |
| A write capability refuses an anonymous initiator | `POST /a2a/dispatch` supplies no `principal`; a write with `principal is None` is REJECTED | CODE-INSPECTED |
| The LLM emits no SQL on any path | Unchanged from both prior assessments | CODE-INSPECTED |

### Open

| ID | Finding | Severity |
|---|---|---|
| **D-01 / N-03** | Anonymous callers receive 129 contacts with names, emails, phones, street addresses, staff names and internal UUIDs; the full 284 KB OpenAPI map is public | **HIGH** |
| **A-03** | Four alert transitions accept a body-supplied actor with an ops token | **HIGH** |
| **D-08** | The production superuser DSN sits on the developer laptop beside `ADMIN_API_TOKEN` and `RAILWAY_ADMIN_API_TOKEN`; I used it to write this report | **HIGH** |
| **D-16** | 46 capabilities, 0 `allowed_callers` — guardrail layer 4 inert | MEDIUM |
| **D-09** | 0 RLS policies | MEDIUM |
| **D-20** | `structuredIntent` accepted from the request body | LOW |

**On D-01.** Recorded decisions hold `public-read` as the intentional marketing posture, and
that decision is the owner's to make. What is *not* decided is its consequence: corpus
provenance is unrecoverable, so "these are synthetic records" cannot be asserted as fact about
the 129 contacts currently served with street addresses to anonymous callers. This is the
oldest open P0 in the register and the only one that is purely a decision.

**On the residual N-01 risk.** The link is no longer a bearer token for *anyone* — it names
one executive, one approval, one action, one issuance, and 72 hours. It remains a bearer token
for *that executive*: whoever holds it decides as them, and the audit will say so. With the
BCC removed the exposure is the executive's own mailbox and mail in transit rather than a
shared archive. That is a large reduction and it is not the same as authentication.

---

## 11. Governance Assessment

### What is genuinely strong, re-verified

- **`_bound_authority` / `bound_authority`** take the deciding identity from the session,
  refuse an admin token outright, and re-check eligibility at decision time. The extraction
  into one shared function is the correct structural response to N-02, and its docstring names
  the reason: *"three copies of this check would be three places for one of them to be
  forgotten."*
- **`_authority_check`** enforces role affinity, and `verify_decision_token` now runs it with
  `via="session"` on the email path — closing the short-circuit that let a departed
  executive's link decide.
- **`principal_for_decider`** still refuses to launder a channel into a person, and now has
  less work to do because the channel supplies a person.
- **Post-execution verification is real:** production row `b1875ff2` carries
  `{"ok": true, "checks": [{"check": "dispatch_audit_row", "note": "outcome=accepted"}],
  "verified_at": "2026-09-09T16:23:34"}`.
- **`policy.widen`** makes governance govern itself, with the CEO as approver and
  `allow_widening` reachable from exactly one caller.
- **Ownership eligibility** is enforced by trigger on `action_approvals` and
  `governance_alerts`, including the NULL case.

### What the operation actually shows

| Question | Answer | Grade |
|---|---|---|
| Has any decision ever been made through an authenticated session in production? | **No.** 0 of 72 rows. | DATABASE-VERIFIED |
| Has any policy ever been changed in production? | **No.** `governance_policy_changes` = 0 rows. | DATABASE-VERIFIED |
| Has any alert ever been *closed* with evidence in production? | **No.** 0 closed; `closure_evidence` NULL on all 11. | DATABASE-VERIFIED |
| Has any escalation email ever been delivered for an alert? | **No.** 8 escalations, 0 ledger rows. | DATABASE-VERIFIED |
| Do proposals still expire silently? | **No.** Last expiry 2026-09-06; 0 since. | DATABASE-VERIFIED |
| Are decisions attributable to a named human? | **Since the fix, yes — as a token-possession claim.** | DATABASE-VERIFIED |

The governance plane is now enforced in the places it was bypassed, and **exercised in only
one of its channels**. The console — the channel that would produce authenticated decisions —
is running a stale build (A-06), and the only production decisions still arrive by link.

### Governance gaps

| ID | Gap |
|---|---|
| A-03 | Four alert transitions outside the binding |
| A-04 | A resolution record that misstates the outcome |
| A-06 | The operator console is not under version control, review or drift detection |
| N-06 | Owner eligibility never re-validated after write |
| N-09 | The governance suite is outside the CI gate, for a declared and sound reason |
| A-07 | …and that suite is not reproducible |
| R-4 | `propose()` fails closed into silence if no authority is eligible; no alert covers it |
| N-10 | Class-wide alert dedupe |

---

## 12. Architecture Assessment

The system map is unchanged in shape from September 7; the governance plane grew and the a2a
surface split.

```
EDGE   app/main.py — FastAPI, 2 workers, 36 scheduler jobs on the leader
  auth_dep: posture(open|public-read|locked) · require_admin · require_governance_actor
       │
GOVERNANCE PLANE
  governance_policy.py  38 action policies · 5 authorities · policy.widen · bound_authority
  governance.py         propose → route → [session-bound | identity-bound email link] →
                        confirm(GET) → decide(POST) → atomic claim → dispatch → verify → finish
  governance_alerts.py  alert lifecycle: owner · SLA · escalate · acknowledge · resolve · close
                        └── escalation email: NOT CONNECTED (A-01)
                        └── acknowledgement: CANNOT HOLD (A-02)
                        └── cancel/ack/assign/escalate: UNBOUND (A-03)
       │
CAPABILITY MESH  a2a.py — 46 capabilities · 0 allowed_callers · 0 agent grants
  read_router (GET, governance actor) | router (POST dispatch, admin)
       │
DETERMINISTIC EXECUTION  execute_sp · ~26 SPs · triggers · registered event types
       │
DATA  PostgreSQL 18.6 (prod) / 17.9 (local) · 166 tables · 185 FKs · 0 RLS
```

### Responsibility boundaries

- **Correct and improved:** the a2a read/write split; `bound_authority` in one module used by
  three; `mint_decision_links` owning issuance and rotation in one place; the drain delegating
  all notification logic to `order_notifications` so there is one sender with one idempotency
  store (the commit removing the second sender records the four distinct bugs the duplicate
  path had).
- **Duplicated source of truth — still open (N-04):** "Approvals expired (7d)" is computed
  twice, from two different time anchors, and reported by two consoles to one operator. Live
  values today: 4 and 0. The Metric Registry exists to prevent exactly this and is still not on
  the path new metrics take.
- **Duplicated source of truth — new (A-05 / A-04):** the drain's handler status and the
  notification `state` both purport to say whether a customer was told, and they disagree. The
  alert was resolved from the weaker one.
- **Hidden coupling — still open (N-07):** `APP_URL` is both the decision-link base and the
  deployment signal. `release_guard` now declares the coupling explicitly rather than removing
  it, which is an improvement in honesty and not in structure.
- **Mechanism outside the control plane — new (A-06):** the governance console is untracked,
  unreviewed, hand-deployed and currently stale. The primary operator surface for the
  governance plane sits outside every process that governs the backend.
- **The three write boundaries are still three.** RC-4 is untouched, as the original
  sequencing intended.

---

## 13. Data Integrity Assessment

| Property | State | Grade |
|---|---|---|
| SQL invariants | **All pass** — append-only refuses DELETE and UPDATE; erasure leaves no recoverable image; deletion logs armed on three tables; logged deletes restorable; no object defined by two migrations | TEST-VERIFIED (local) |
| Privilege separation | Role `crm_app` exists, holds no elevated attributes, owns no tables, cannot create in `public`; production runs as it | TEST-VERIFIED + DEPLOYMENT-VERIFIED |
| Application DSN | **TODO** — the local DSN still names an owner account (D-08) | TEST-VERIFIED |
| FK coverage | 185 constraints across 166 production tables | DATABASE-VERIFIED |
| RLS | **0 policies** | DATABASE-VERIFIED |
| Orphaned events | **0** | DEPLOYMENT + DATABASE-VERIFIED |
| Invalid ownership | **0 of 2,439 orders** and **297 of 11,418 owned activities** name an eligible accountable human; 39 of 52 `owners` rows are customer contacts | DATABASE-VERIFIED |
| Identity portability | All five executives carry different `owner_id` on local and production (N-11) | DATABASE-VERIFIED |
| Contradictory sources of truth | N-04 (expired counts); A-04 (drain status vs notification state); one open alert says "LLM failover: broken" while `/platform/health` says `failover_readiness: ready` | DEPLOYMENT + DATABASE-VERIFIED |
| Stale state | Alert `757f77d3` has been open and un-updated since 2026-09-09 16:00 while the condition it reports has cleared; no detector closes an alert when its condition resolves (except the bus drain, which does) | DATABASE-VERIFIED |

The ownership figures deserve emphasis because they are the clearest case in this review of
measuring a **property** rather than a **proxy**. Counting non-null `owner_id` says 80% of
activities are owned. Applying the system's own `fn_owner_eligible` predicate says 2.6% are
owned by someone who could be held accountable, and **no order is**. The platform's new
`workflow_work_accountable` metric (4.21%) measures the property correctly and is the right
model for the rest.

---

## 14. Auditability Assessment

Reconstruction attempted on the most recent production action —
`supervisor.emit_dunning`, approval `b1875ff2`, decided 2026-09-09 16:23:33.

| Question | Answer available? | Evidence |
|---|---|---|
| What was proposed, when, by whom | **YES** | `proposed_by: supervisor`, `created_at: 13:00:19`, `confidence: 0.75`, `params` |
| Under what authority | **YES** | `authority_role: CFO`, `accountable_owner: "CFO Sherman Zhang"` |
| Which policy permitted it | **YES** | `decision_mode: HUMAN_APPROVAL`, `policy_version: 1` |
| Was approval required | **YES** | `HUMAN_APPROVAL`, `due_at: 2026-09-11` |
| **Who approved** | **YES — newly** | `decided_by` and `decided_actor` both `cfo@agentorc.ca` |
| How was the approver identified | **PARTIAL** | Possession of a nonce-bound, executive-scoped, 72h link. Not an authenticated session. |
| When | **YES** | `decided_at`, to the second |
| What executed | **YES** | a2a dispatch with a correlation id |
| Was the result verified | **YES** | `verification.checks[0].check = "dispatch_audit_row"`, `verified_at` |
| Was the link retired | **YES** | `decision_link_nonce` NULL, `recipients` NULL, `issued_at` NULL after execution |
| Could it be reversed | **PARTIAL** | undo where a handler is declared, now bound to an executive |
| What an external auditor receives | **PARTIAL** | No signed export; reconstruction requires the platform |

**Nine and a half of ten links.** The chain is materially better than on September 7: the
identity gap is closed, and the link-retirement evidence is a genuine addition.

Outside the governance chain, auditability is **worse than the baseline recorded**: production
`audit_log` holds 15,197 rows with no actor column, and `record_field_history` — the only
actor-bearing table — holds **26** (A-08).

Two audit records in production are actively misleading:

- alerts `b9a224e2` / `b899a98b`: `resolution_note: "remove them"` — an instruction the
  executive believed would execute (A-06);
- alert `34fe6159`: a resolution note stating 18 shipped-order notifications were processed,
  where the notification rows say `skipped` (A-04).

---

## 15. Reliability / Operations Assessment

| Property | State | Grade |
|---|---|---|
| Event queue | depth 0 · orphaned 0 · failed 0 · stuck 0 · delayed 0 · drain rate 127/h | DEPLOYMENT-VERIFIED |
| Scheduler | running on the leader, 36 jobs, `overdue_jobs: []`, ticking | DEPLOYMENT-VERIFIED |
| Leader election | working; one leader, one follower, deterministic | DEPLOYMENT-VERIFIED |
| Approval races | `approvals_stranded: 0`; atomic claim + lease + sweeper; concurrency test passes | DEPLOYMENT + TEST-VERIFIED |
| Retry / idempotency (notifications) | `UNIQUE(order_id, event_type)` claimed **before** the send; a retryable failure raises into backoff and re-claims the same row | CODE-INSPECTED |
| **Alert acknowledgement** | **Broken** — re-escalates within 82 seconds (A-02) | DATABASE-VERIFIED |
| **Alert escalation notification** | **Never delivered** — 8 escalations, 0 emails (A-01) | DATABASE-VERIFIED |
| **Customer notification delivery** | **100% suppressed for 5 days, unmonitored** (A-05) | DATABASE-VERIFIED |
| Silent failure paths | 20 `logger.debug` swallows in `governance*.py`; detectors return `None` on exception (A-11) | CODE-INSPECTED |
| Alert ownership | Every production alert names an executive with an SLA and a due date | DATABASE-VERIFIED |
| Configuration drift | **Present** on the governance console (A-06); backend commit matches exactly | DEPLOYMENT-VERIFIED |
| Environment drift | PostgreSQL 18.6 vs 17.9 (A-10) | DATABASE-VERIFIED |

**Can an operator discover a governance failure before a customer discovers its business
consequence?** For the incident that motivated all of this: the detector now fires, the alert
is owned, the SLA runs, the escalation transition happens — **and the notification never
arrives**. The operator discovered it in the console, three times, and could not make
acknowledgement stick. The answer is "only if they are already looking".

---

## 16. AI Agent Governance Assessment

| Property | State | Grade |
|---|---|---|
| Agent authority vs intended authority | `agent_capability_grants` = **0**. No authored agent holds any capability grant; the U4 action surface is dormant. A safe default, not a defect. | DATABASE-VERIFIED |
| Confidence never grants authority | `confidence_grants_authority: false`; `act_min` retained and labelled informational; the decision is read from the policy row | DEPLOYMENT-VERIFIED |
| Every write capability has a declared policy | `undeclared_write_capabilities: []`, measured continuously by the system itself | DEPLOYMENT-VERIFIED |
| Tool access vs policy | `allowed_callers` NULL on **46/46** — any registered caller may invoke any enabled capability (D-16) | DATABASE-VERIFIED |
| Uncontrolled write capabilities | None: a write with no principal is refused; `POST /a2a/dispatch` supplies none; `govern_bypass` is unreachable from HTTP | CODE-INSPECTED |
| Human approval boundary | Enforced before the structured branch, so structured writes cannot slip past; the HITL amount floor overrides a standing auto policy | CODE-INSPECTED |
| Agent identity | `Principal.service(...)` required for unattended writes; `from_agent` names the component, never the authority | CODE-INSPECTED |
| Provenance / reconstruction | Correlation id carried through propose → dispatch → verify; `_correlation_id` stripped before re-dispatch so an approved execution validates | CODE-INSPECTED + DATABASE-VERIFIED |
| Prompt/config bypass | No path found from prompt or configuration to a governed action without a policy row | CODE-INSPECTED |
| Composite effects | `planner.max_writes = 2`, `planner.max_steps = 6`, both defaults with no DB override; **no composite-effect policy** | CODE-INSPECTED |

`confidence_grants_authority: false` remains the single most important AI-safety property in
this system and it is verifiable from outside. The open items are D-16 (layer 4 inert) and the
absence of a composite-effect policy — no evidence of exploitation, and no control that would
prevent one.

---

## 17. Critical End-to-End Control Traces

### 17.1 "A governed action requires a named executive's approval"

| # | Link | State | Grade |
|---|---|---|---|
| 1 | Who requests it | `supervisor` detector, or an a2a caller | DATABASE-VERIFIED |
| 2 | How the actor is identified | `Principal`; a write with none is refused | CODE-INSPECTED |
| 3 | How the policy is selected | `policy_for(action_type)` — a row, never the confidence score | CODE-INSPECTED |
| 4 | How the required authority is determined | `approver_role` on the policy row; `resolve_accountable_owner` refuses if nobody is eligible | CODE-INSPECTED |
| 5 | How the proposal is recorded | `action_approvals` INSERT; owner-eligibility trigger refuses a NULL or ineligible owner | DATABASE-VERIFIED |
| 6 | How the approver is authenticated | **BREAK.** Session path: fully authenticated, and **never used in production**. Link path: possession of a nonce-bound, executive-scoped, 72h token. | NOT PROVEN |
| 7 | How approval is bound to the action | `HMAC(secret, uuid:action:executive_id:nonce)`; executive must be in `decision_link_recipients`; row must be `pending` | REPRODUCED (negative cases) |
| 8 | How execution is prevented without approval | Atomic claim `UPDATE … WHERE status='pending' RETURNING`; finish `WHERE execution_token=<ours> AND status='executing'`; 15-minute lease; stranded sweeper | TEST-VERIFIED |
| 9 | How execution is recorded | a2a dispatch + `dispatch_audit_row` verification stored on the approval | DATABASE-VERIFIED |
| 10 | Can the chain be reconstructed | **Yes**, with link 6 qualified | DATABASE-VERIFIED |

**Exact break: link 6.** The chain proves *which executive the authorisation was issued to*.
It does not prove *that executive authorised it*.

### 17.2 "A weakened control requires a CEO decision"

| # | Link | State | Grade |
|---|---|---|---|
| 1 | Requester | Must hold an executive session — `bound_authority` on both policy endpoints | REPRODUCED (403 with ops token) |
| 2 | Classification | `classify_policy_change` / `classify_tunable_change` decide whether the change weakens | CODE-INSPECTED |
| 3 | Refusal | `set_policy` raises `PolicyWideningRequiresApproval` unless `allow_widening` | CODE-INSPECTED |
| 4 | Proposal | `propose("policy.widen", …)`, severity high, confidence 1.0; HTTP 202, "Nothing has changed yet" | CODE-INSPECTED |
| 5 | Routing | `governance_action_policies.policy.widen` → `HUMAN_APPROVAL`, approver **CEO** | DATABASE-VERIFIED |
| 6 | Execution | `_sp_policy_widen` is the only caller that may pass `allow_widening=True`, and carries the `approval_uuid` | CODE-INSPECTED |
| 7 | Bypass via a2a | `POST /a2a/dispatch` cannot set `govern_bypass` (absent from `_DispatchBody`) and supplies no `principal`, which a write capability refuses | CODE-INSPECTED |
| 8 | Audit | `governance_policy_changes` records every applied change, widening or not | DATABASE-VERIFIED (0 rows in production) |

**No break found.** The property is **NOT PROVEN** end-to-end only because proving it requires
a mutating probe (§20). Note that `_sp_policy_widen` *records* the `approval_uuid` from its
params rather than *verifying* it — safe today because the only route to that function is
`_execute` after an atomic claim, but it is a verification that rests on reachability rather
than on a check.

### 17.3 "An accountability alert reaches a named human and is cleared by one"

| # | Link | State | Grade |
|---|---|---|---|
| 1 | Detection | Supervisor detectors and the bus open alerts with a rule and severity | DATABASE-VERIFIED |
| 2 | Ownership | `accountable_owner_id` resolved by role; eligibility enforced by trigger | DATABASE-VERIFIED |
| 3 | SLA | `due_at` set from `sla_hours` | DATABASE-VERIFIED |
| 4 | In-app notice | Written — 8 of 8 | DATABASE-VERIFIED |
| 5 | **Escalation email** | **BREAK — never delivered, 8 of 8, silently** (A-01) | DATABASE-VERIFIED |
| 6 | **Acknowledgement** | **BREAK — cannot hold on an overdue alert** (A-02) | DATABASE-VERIFIED |
| 7 | Resolution binding | `resolve`/`close` bound over HTTP; **`cancel`/`assign`/`escalate` are not** (A-03); internal callers may resolve with any actor string | REPRODUCED |
| 8 | Closure evidence | **0 of 11 production alerts have ever reached `closed`**; `closure_evidence` NULL on all | DATABASE-VERIFIED |
| 9 | Reconstruction | Full `history` array with actor and note per transition — genuinely good | DATABASE-VERIFIED |

**Three breaks, at links 5, 6 and 7.** This is the weakest chain in the system.

### 17.4 "A shipped order results in the customer being told"

| # | Link | State | Grade |
|---|---|---|---|
| 1 | Transition detection | DB trigger `trgfn_order_lifecycle_notify` emits on the transition | CODE-INSPECTED |
| 2 | Event delivery | Bus handler `handle_order_lifecycle_notification` | CODE-INSPECTED |
| 3 | Idempotent claim | `UNIQUE(order_id, event_type)` claimed before the send; retry re-claims the same row | CODE-INSPECTED |
| 4 | Recipient gate | `_is_real_email` — verified **and** not a placeholder/seed domain | CODE-INSPECTED |
| 5 | **Delivery** | **BREAK — 0 accepted in 5 days; all 129 production contacts are on a blocked domain** | DATABASE-VERIFIED |
| 6 | Outcome recorded | `state='skipped'` with an accurate `failure_reason` — the record is honest | DATABASE-VERIFIED |
| 7 | **Monitoring** | **BREAK — no metric, no detector, no alert; `/platform/health` obligations is all-green** | DEPLOYMENT-VERIFIED |
| 8 | **Incident closure** | **BREAK — the orphan alert was resolved claiming these were processed** | DATABASE-VERIFIED |

**Three breaks, at links 5, 7 and 8.** Link 5 is intended behaviour; links 7 and 8 are not.

---

## 18. Risk-Ranked Findings

### CRITICAL

*None.* No finding in this review permits unauthorised execution of a governed action,
privilege escalation, or destruction of audit history. The two September 7 P0s that would have
qualified are closed and proven closed.

### HIGH

| ID | Finding | Violated property | Remediation class |
|---|---|---|---|
| ~~**A-01**~~ | ~~Alert escalation email has never delivered~~ **MOVED TO MEDIUM — see §1a/C-1. Mail delivers; recording is unreliable** | — | — |
| **A-02** | Acknowledging an overdue alert re-escalates it within 82 seconds | Acknowledgement is a usable state | code + policy decision |
| **A-03** | `cancel`/`acknowledge`/`assign`/`escalate` unbound, actor from the request body | An accountability signal is cleared only by a named human | code |
| **A-04** | An alert was resolved on a handler status that does not mean the outcome occurred | A resolution record states what happened | code + operational decision |
| **A-05** | Customer notification delivery 100% suppressed for 5 days, unmonitored | A delivery outage is distinguishable from normal operation | code + decision |
| ~~**A-06**~~ | ~~The deployed governance console is a stale build~~ **DRIFT CLOSED 2026-09-10; structural half + 3 misleading records MOVED TO MEDIUM — see §1a/C-3** | — | — |
| **D-01/N-03** | Anonymous PII: 129 contacts with addresses and staff names; 284 KB OpenAPI | Customer data is not disclosed to anonymous callers | decision, then configuration |
| **D-08** | Production superuser DSN on the developer laptop beside both admin tokens | Least privilege at the operator endpoint | operational process |
| **D-02** | 0 of 2,439 orders and 2.6% of owned activities name an eligible accountable human | Every business record has an accountable human | schema + code + data |

### MEDIUM

| ID | Finding |
|---|---|
| **A-01** *(re-ranked §1a/C-1)* | Alert mail was delivered but not recorded in `staff_email_ledger`; it began recording again on 2026-09-10 for reasons that are **NOT PROVEN** |
| **A-06** *(re-ranked §1a/C-3)* | The governance console is untracked, unreviewed and has no drift check; 3 production alerts carry resolution notes that are unexecuted instructions |
| **A-15** *(new, §1a/C-4)* | Alert transitions have no role affinity: any eligible executive can discharge an alert accountable to another |
| **A-07** | The governance suite is order-dependent; two runs, two different failures; both pass in isolation |
| **A-08** | The only actor-bearing audit table covers 0.17% of production mutations |
| **A-09** | Retrieval vector pool drifted past its validated state (largest template group 558 > `N_VEC` 500) |
| **A-10** | Production PostgreSQL 18.6; all schema evidence measured on 17.9 |
| **N-04** | `expired_7d` = 4 and `approvals_expired` = 0 under one label, live, on two consoles |
| **N-05** | The `governance_alerts` half of the silent-degradation fix was not applied — the mechanism behind A-01 |
| **N-06** | Owner eligibility never re-validated after write |
| **N-09** | The governance suite is outside the CI gate (attempted, reverted, reason declared) |
| **N-11** | Executive `owner_id` differs between local and production for all five humans |
| **D-09** | 0 RLS policies |
| **D-10** | `/deploy/migrations` returns `ok: false` for an ordering artefact while the schema is current |
| **D-12** | Retrieval abstention still ungated |
| **D-14** | 20 silent DEBUG degradation paths in `governance*.py` alone |
| **D-16** | `allowed_callers` inert on 46/46 capabilities |
| **R-2** | The hybrid-retrieval kill switch still does not restore its pinned contract |
| **R-4** | `propose()` fails closed into silence if no authority is eligible; no alert covers it |

### LOW

A-11 (detectors report "no problem" for uncomputed metrics) · A-12 (`dry_run` reports
`accepted` for a refusable call) · A-13 (`/health` per-worker scheduler state without a note) ·
N-07 (`APP_URL` overload, now declared) · N-10 (class-wide alert dedupe) · N-12
(`/deploy/consensus` returns `ok` measuring nothing) · D-17 (contradictory self-description) ·
D-18 (evals unpersisted) · D-19 (dead consensus control) · D-20 (`structuredIntent` from body) ·
D-22 (local `master` 54 behind)

### INFORMATIONAL

A-14 (the confirmation page discloses the action to a link holder — intended) · N-08
(alert-cancel flood is a local artefact; production is clean)

---

## 19. Remediation Priorities

Ordered by risk reduction × leverage × the cost of not doing it. Recommendations only; nothing
below was performed.

### P-1 · Connect the alert lifecycle (A-01, A-02, N-05) — days

The alert object is excellent and three of its links do not work. In order:

1. Raise `governance_alerts.py:326` and `:349` from `logger.debug` to `logger.warning`, and
   record the `email_authority` return (`{"sent": …, "why": …}`) rather than discarding it.
   This alone converts A-01 from invisible to diagnosable.
2. Decide what acknowledgement means, then implement it: extend `due_at`, or suppress
   re-escalation for a fixed window, or add an explicit `snooze`. Any of the three fixes A-02;
   none of them can be chosen for the owner.
3. Extend `_BOUND_ACTIONS` to `cancel`, `assign` and `escalate` (A-03). `acknowledge` is a
   separate decision — the code's argument for leaving it open is defensible.
4. **Invariant:** *An escalation that produced no notification is itself an alert.*

### P-2 · Make delivery failure visible (A-05, A-04) — days

1. Add a `notification_suppression_rate` metric to `/platform/health`'s obligations section
   with an accountable owner. Today's correct answer is 100% and the surface should say so.
2. Change the drain to resolve on the notification **state**, not the handler status, and to
   write `closure_evidence` naming what was actually sent and what was suppressed.
3. **Human action:** record the write-off decision for the 18 shipped orders on alert
   `34fe6159`, and correct the resolution note that says they were processed.
4. **Invariant:** *A control may not be resolved by a status that cannot distinguish success
   from suppression.*

### P-3 · Deploy the console and put it under drift detection (A-06) — hours, then process

1. Deploy the working copy of `governance-mgmt.html`.
2. Add a deployed-artefact hash check for the console equivalent to `/health.commit` for the
   backend. The repository policy that keeps HTML untracked is settled; a hash manifest does
   not require tracking the file.
3. **Human action:** review the two production alerts whose resolution note is an instruction
   and record what was actually intended.
4. **Invariant:** *An operator surface with no drift check is not deployed, it is assumed.*

### P-4 · Make the governance suite reproducible, then gate it (A-07, N-09) — 1 week

The `verify_gate.py` analysis is correct: these suites need an operated governance database and
credentials do not belong in a migration. A governance-identity seed stage with throwaway
fixture executives is the declared path. **A-07 is a prerequisite** — gating a suite that fails
on a different test each run produces exactly the red-for-the-wrong-cause outcome the gate's own
header warns against.

### P-5 · Decide the production posture (D-01) — 1 week

Unchanged from both prior assessments. The oldest open finding and the only one that is purely
a decision. Its consequence is undecided, not just its answer: corpus provenance is
unrecoverable, so the records currently served anonymously cannot be asserted to be synthetic.

### P-6 · Remove the production superuser DSN from the laptop (D-08) — hours

Standing since the September 5 baseline. This review used it, read-only, which is the argument.

### Then, in the original roadmap's order

Business-entity ownership (D-02, using `fn_owner_eligible` as the measure, not `owner_id`
non-nullity) · the Metric Registry on the path new metrics take (N-04) · `allowed_callers`
(D-16) · retrieval governance (D-12, D-18, A-09, R-2) · `audit_log` actor (D-05, A-08).

---

## 20. Required Evidence for Unproven Properties

Each item names the operation required and the authorization it needs. **None was performed.**

| Property | Currently | Operation required | Authorization needed |
|---|---|---|---|
| The named executive, not a link holder, made the decision | NOT PROVEN | An authenticated console decision in production, then read `decided_via='session'` | Deploy the console (P-3), then an executive signs in and decides once |
| Which branch of A-01 fires | NOT PROVEN | Raise the log level and read one escalation, **or** call `email_authority` against production with `kind='alert_escalated'` and inspect the return | Code change, or an explicit authorization to send one test escalation |
| A widening is actually refused end-to-end | CODE-INSPECTED | `PUT /governance/action-policies/sms.send {"changes":{"decision_mode":"AUTO_EXECUTE"}}` with an **executive session**, expecting HTTP 202 and a `policy.widen` proposal | **Explicit authorization — this creates a real pending approval in production** |
| An ops token cannot cancel a real alert | REPRODUCED against a non-existent id | The same call against a real alert id | **Explicit authorization — a successful call would cancel a live accountability alert** |
| An expired (>72h) link is refused | CODE-INSPECTED | Mint a link, wait 72h, present it | Time, plus a pending approval to bind it to |
| A superseded link is refused | CODE-INSPECTED | Mint, re-mint via reminder, present the first | **Explicit authorization — re-minting invalidates the executive's live link** |
| Schema facts hold on PostgreSQL 18.6 | Measured on 17.9 | Run `verify_invariants` read-only against production | Read-only production access (available) |
| Emails reach the recipient | Ledger only | Read the `info@agentorc.ca` IMAP archive and match to ledger rows | Mailbox credentials |
| Anonymous callers cannot write | Carried forward as PARTIALLY VERIFIED | An anonymous write probe against production | **Explicit authorization — a failed control would write to production** |

---

## 21. Overall Architecture Verdict

**Is the architecture fundamentally sound?** Yes, and more so than on September 7. The
governed-action record with a declared policy, an atomic claim, post-execution verification, a
principal resolver that refuses to invent a person, and now an identity-bound decision link
with rotation and expiry, is a genuinely differentiated design. `confidence_grants_authority:
false` and `undeclared_write_capabilities: []` are properties most platforms in this category
cannot state at all, let alone expose on an endpoint.

**Does it need a rewrite?** No. Nothing found in this review is a design that must be replaced.

**Can we prove the architecture enforces the properties it claims to enforce, under realistic
failure and adversarial conditions?**

| Claimed property | Provable today? |
|---|---|
| A governed action cannot execute without a decision by the routed authority | **Yes**, for the decision. **No**, for the authentication of the human behind it. |
| A control cannot be weakened by an identity that could not exercise it | **Yes for policy. No for alerts** — four transitions accept a body-supplied actor. |
| An escalated accountability alert reaches its owner | **No.** 8 of 8 delivered nothing. |
| An operator can acknowledge an alert they are working | **No.** 82 seconds. |
| An orphaned consequential event is drained or explicitly written off | **Drained: yes. Written off: no** — and the record says otherwise. |
| A customer whose order ships is told | **No**, and no control reports it. |
| The deployed operator surface is the reviewed one | **No** for the governance console. |
| An AI agent cannot exceed its granted authority | **Yes** — 0 grants, no principal-less writes, no HTTP path to `govern_bypass`. |
| Customer data is not disclosed to anonymous callers | **No.** Unchanged since the first assessment. |

Five material properties cannot be proven, and three of them fail on evidence rather than for
want of it.

The September 7 verdict was that the remediation *"did the hard part and left the easy part
open"*. That judgement was correct and the easy part is now done: a GET became a POST, a BCC
was suppressed, a token gained an owner and an expiry, and thirteen endpoints were made to call
the function the other three already called. All of it is deployed and all of it verifies.

What this review finds in its place is a different and more uncomfortable pattern. **The
controls that were built to make failure visible are themselves failing invisibly.** An
escalation that notifies nobody, an acknowledgement that cannot hold, an alert resolved on a
status that does not mean what it says, a delivery path at 0% with a green health check, and a
console running a build nobody reviewed. Each is individually small. Together they mean that
the system's own account of its condition — 4 open alerts, an empty queue, a clean governance
queue, `state: warning` — is not yet a trustworthy account.

That is not a governance-design failure. It is the gap between a control being *enforced* and a
control being *connected*, and it is the gap this system now has to close.

> ## VERDICT: C — ARCHITECTURE NOT YET ACCEPTABLE
>
> **Material governance, reliability and security findings remain.** The architecture is
> sound, the September 7 P0s are closed and proven closed, and no critical finding survives.
> But six HIGH findings are open, five claimed properties cannot be proven, and three of the
> platform's own accountability controls do not work in the deployed system while reporting
> that they do.

This verdict is not selected because the work was insufficient — the work was substantial,
well-reasoned and largely successful. It is selected because the standard is *"can we prove the
current architecture enforces what it claims, under realistic failure and adversarial
conditions"*, and for five material properties the answer is no.

**Distance to verdict B (acceptable with controlled remediation):** P-1, P-2 and P-3 —
approximately one week. All three are code and deployment, none is a design change, and each
has a measurable production signal that would prove it. **Distance to verdict A:** P-5 (the
posture decision) and D-02 (business-entity accountability) sit behind it, and those are the
original roadmap's Stage 0 and Stage 4.

---

## 22. Recommended Next Step

**Raise `governance_alerts.py:326` and `:349` from DEBUG to WARNING, redeploy, and read the
next escalation.**

It is a two-line change to a log level. It costs nothing, risks nothing, and converts the
single largest unproven property in this report — *why has the escalation channel never
delivered* — from NOT PROVEN into a measured fact. Every other item in P-1 depends on knowing
which branch fires.

Do that first, then P-1 in full, then P-2, then P-3.

**Before any of it, one decision is required and only the owner can make it:** the 18 shipped
orders were drained into a suppression and the alert says they were processed. That record
should be corrected either by notifying, or by a written-off decision recorded on the alert's
`closure_evidence`. Leaving it as it stands means the system's audit trail asserts a customer
outcome that did not happen — which is the one failure mode this project's own doctrine says is
worse than an absent record.

---

## 23. Evidence Appendix

| # | Claim | Source | Probe |
|---|---|---|---|
| E-01 | Production runs `43c72ebcdb10` = `origin/master` HEAD | production | `GET /health` |
| E-02 | Old-format decision links are refused | production | `GET /governance/decide?…&t=<forged>` → 403 "predates identity binding" |
| E-03 | New-format forged tokens refused on GET and POST | production | both → 403 |
| E-04 | Ops token refused on policy tunables, action-policies, undo, alert resolve/close | production | 5 × 403, on unknown keys / non-editable fields / bogus ids |
| E-05 | Ops token **reaches** alert cancel/acknowledge/assign/escalate with a body actor | production | 4 × "alert not found" (409/200) |
| E-06 | 3 production decisions name `cfo@agentorc.ca` in both `decided_by` and `decided_actor` | Railway SQL | `action_approvals` |
| E-07 | 0 decisions ever via `session` in production | Railway SQL | `decided_via` distribution |
| E-08 | 8 escalations, 8 in-app notices, 0 `alert%` ledger rows | Railway SQL | `governance_alert_transitions`, `notifications`, `staff_email_ledger` |
| E-09 | The email-kind CHECK constraint permits all nine kinds on both databases | Railway + local SQL | `pg_constraint` |
| E-10 | Local has 5 `alert_escalated` ledger rows | local SQL | `staff_email_ledger` |
| E-11 | CEO acknowledged at 15:55:05, sweep re-escalated at 15:56:28 | Railway SQL | alert `b9a224e2` history |
| E-12 | The drain produced 18 `order.shipped` rows, all `skipped` | Railway SQL | `order_notifications`, 2026-09-08 13:35 window |
| E-13 | 0 accepted notifications since 2026-09-05; 372 skipped in 5 days | Railway SQL | daily accept/skip trend |
| E-14 | All 129 production contacts are on `seed.agentorc.ca` | Railway SQL | `contacts` domain distribution |
| E-15 | `seed.agentorc.ca` is a declared placeholder domain | repo | `agent_bus._PLACEHOLDER_EMAIL_DOMAINS` |
| E-16 | Live console SHA-256 == `governance-mgmt.html.bak2`; working copy differs | agentorc.ca + local | hash comparison, then `diff` |
| E-17 | Production alerts carry `resolution_note: "remove them"` from the CEO | Railway SQL | alerts `b9a224e2`, `b899a98b` |
| E-18 | Orphans are 0 by two independent channels | production + Railway SQL | `/platform/health`; `classify_orphaned_events --target railway` |
| E-19 | `expired_7d` 4 vs `approvals_expired` 0, same minute | production | `/governance/status`, `/platform/health` |
| E-20 | 0 of 2,439 orders and 297 of 11,418 owned activities have an eligible owner | Railway SQL | `fn_owner_eligible` applied to `orders`, `activities` |
| E-21 | `audit_log` 15,197 rows with no actor; `record_field_history` 26 rows | Railway SQL | `information_schema.columns`, counts |
| E-22 | 46 capabilities, 0 `allowed_callers`, 0 agent grants, 0 RLS | Railway SQL | catalog + `capability_registry` |
| E-23 | `policy.widen` exists, `HUMAN_APPROVAL`, approver CEO | Railway SQL | `governance_action_policies` |
| E-24 | `governance_policy_changes` has 0 rows in production, 181 local | Railway + local SQL | counts |
| E-25 | Test suite 3,027 passed / 4 failed; two runs, two different failures; `TestApproval` passes in isolation | local | three `pytest` runs |
| E-26 | The governance suites were added to the gate and reverted, with reasons | repo | `scripts/verify_gate.py:61-113` |
| E-27 | Anonymous PII: 129 contacts, addresses, staff names, internal UUIDs | production | `POST /contact-chat {mode:list}`, no auth |
| E-28 | 284 KB OpenAPI anonymous; 7/7 governance and ops paths 403 anonymous | production | `GET /openapi.json`; 7 probes |
| E-29 | `govern_bypass` is absent from `_DispatchBody`; its only setter is the approved re-dispatch | repo | `a2a.py:1862-1875`, `governance.py:1354` |
| E-30 | `dry_run` of `policy.widen` returns `ok: true, outcome: accepted` | production | `POST /a2a/dispatch` |
| E-31 | All SQL invariants pass; the application-DSN TODO stands | local | `python -m scripts.verify_invariants` |
| E-32 | Production PostgreSQL 18.6, local 17.9 | both | `select version()` |
| E-33 | Executive `owner_id` differs on all five between databases | both | `executives` |
| E-34 | Scheduler running on the leader, 36 jobs; a follower answers `running: null, jobs: 0` | production | 7 × `GET /health` |
| E-35 | All governance senders pass `NO_BCC`; the sentinel gives `send_email` three states | repo | `governance.py:1005,1082`, `governance_policy.py:595`, `smtp_imap.py:37,182` |

**Not established in this review, and why:** the recipient-side confirmation of any email (the
IMAP archive was not read); the exact failure branch of A-01 (both candidates are silent, and
distinguishing them requires a code change or an authorized send); anonymous *write* probes
against production (a failed control would have written); and end-to-end proof of the
`policy.widen` refusal (proving it creates a real pending approval). Each is listed in §20 with
the operation and the authorization it would need.
