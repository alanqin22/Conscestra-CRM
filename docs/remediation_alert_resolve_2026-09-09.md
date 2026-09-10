# Remediation — Alert Resolve semantics

**2026-09-09.** Branch `p2-assessment-working`.

---

## ⚠ EPISTEMIC STATUS OF THIS WORK — READ BEFORE THE REST

> **This remediation proceeds on the P1 implementer's UNVERIFIED HYPOTHESIS.
> No P2 assessment has been performed. There is no confirmed finding.**

`docs/p2_world_class_architecture_assessment_*.md` **does not exist**. The
handoff brief (`docs/p2_assessment_brief.md`, commit `182cd52`) records the
Resolve question as *a hypothesis to falsify*, explicitly framed so an
independent assessor would test it rather than inherit it. That assessment has
not run. The owner elected to proceed anyway, with this notice attached.

The hypothesis being acted on, stated by the same person who wrote the code:

> *"`Resolve` means the human has handled the alert; it is not a work order.
> `transition()` only UPDATEs status / resolved_by / resolution_note, sets the
> `app.actor` and `app.note` GUCs so a trigger writes history, and logs.
> Nothing parses the note; nothing dispatches."*

### Items the owner named as NEVER CHECKED

Recorded verbatim, at the owner's instruction:

1. the operational status of the idempotency guard
2. the ledger write path and its failure mode
3. upstream mail-authority invariants
4. cross-component causal chain for alert creation → assignment → ledger
5. whether the observed behaviour generalises beyond the local environment

**Note on scope, stated rather than smoothed over:** items 1–4 belong to a
*different* open finding — that `staff_email_ledger` holds no `alert%` rows
while alert mail demonstrably sends. They are **not** the premise this
remediation rests on, and nothing below addresses them. They remain open.

### The unverified items THIS remediation actually rests on

Distinct from the list above, and the ones that matter here:

- **U-1** the UI Resolve handler — what `governance-mgmt.html` calls besides the
  transition endpoint
- **U-2** SQL triggers on `governance_alerts` — a trigger could act on the note
  with no Python involved
- **U-3** any later supervisor / workflow / agent pass that reads
  `resolution_note`
- **U-4** whether the semantic contract is documented anywhere as *execution*

§1 below converts U-1…U-4 from unverified to measured **before** any change is
made, because verifying them is minutes of work and remediating on an untested
premise is the failure this whole engagement has been cataloguing. What §1
cannot establish is stated as still-unverified.

### Evidence labels used throughout

**MEASURED** — observed directly in this session, command and result shown.
**INFERRED** — concluded from measurement; could be wrong.
**UNVERIFIED** — not checked. Never silently upgraded.

---

## 1. Verification performed before changing anything

*(filled in below as each check runs)*

### U-1 — the UI Resolve handler · MEASURED

`governance-mgmt.html:1032-1046`, `alertStep(id, step)`:

```js
if (step === 'resolve' || …) { const note = prompt(`${STEP_LABEL[step]} — note (kept on the record):`); … body.note = note; }
const r = await api(`/governance/alerts/${id}/${step}`, 'POST', body);
toast(r.ok ? `Alert → ${r.status}` : …);
```

The handler posts `{actor, note}` to one endpoint and makes **no second call**.
No dispatch, no agent invocation, no mail. **Hypothesis holds for U-1.**

### U-2 — SQL triggers on `governance_alerts` · MEASURED

Two triggers: `trgfn_governance_alerts_lifecycle`, `trgfn_governance_alerts_no_delete`.
The lifecycle trigger **does** touch `resolution_note` and `app.note` — the one
place that could have falsified the hypothesis. Read in full, it:

- enforces owner eligibility on every write;
- enforces the legal state machine (`open → assigned → acknowledged →
  in_progress → resolved → closed`, plus escalate/cancel/reopen exits);
- requires `resolved_by` to reach `resolved`;
- requires `closed_by` **and** `closure_evidence` to reach `closed`;
- writes the note into `governance_alert_transitions` **as text**.

Copying text into a history row is the only thing done with it. **Hypothesis
holds for U-2.**

#### CORRECTION to a statement made to the owner on 2026-09-09

I said:

> *"`closure_evidence` is optional and bypassed… the strongest control in the
> alert lifecycle — evidence — is optional, and the weakest — an uninterpreted
> note — is the default path."*

**The first half is wrong.** `closure_evidence` is **mandatory** to reach
`closed`, enforced by `ck_governance_alerts_closure_evidence` in the database
trigger, *and* independently by the UI (`if (!ev) { toast('Closure needs
evidence'); return; }`). It is not optional and not bypassable.

What is actually true is narrower: **`resolved` is a legitimate intermediate
state requiring only `resolved_by`**, and an alert may sit there indefinitely
without ever reaching `closed`. That is a lifecycle design question, not an
evidence bypass. The stronger claim was made from reading Python and not the
trigger — the same mistake this remediation's header warns about.

### U-3 — any later reader of `resolution_note` · MEASURED

The only reference to `governance_alerts.resolution_note` in application code is
the **write** in `transition()` (`governance_alerts.py:244`).

Two same-named columns on *other* tables were excluded and confirmed unrelated:
`escalations.resolution_note` (`escalation.py`) and
`crm_agent_memory.resolution_note` (`sp_agent_memory`). Three tables share a
column name; only one is this one.

`governance_alert_transitions` is read in exactly one place — `AS history`, for
display (`governance_alerts.py:495`). Nothing executable consumes it.
**Hypothesis holds for U-3.**

### U-4 — is Resolve documented as execution anywhere · MEASURED

No. Nothing in the UI or code describes Resolve as performing an action. The
dialog reads *"Resolve — note (kept on the record)"*, which is literally
accurate but **does not say that no action follows** — and that is the gap a
real executive fell into.

### What remains UNVERIFIED after §1

- Whether this generalises beyond the local environment (deployed HTML is the
  owner's to publish; the running Railway app was not re-inspected here).
- The four ledger items in the header. Untouched, still open.
- Everything a genuine P2 assessment would examine. **This section verified four
  specific claims; it is not an assessment.**

---

## 2. The finding this remediation acts on

**Classification: B — UX/semantic ambiguity.** Non-execution is intended and
correctly implemented; the interaction lets an executive reasonably believe
otherwise.

Not **C** (governance integrity): no false evidence is produced, no verification
object is fabricated, and the audit trail records exactly what happened —
`resolved_by`, the note as text, and a history row. The record is truthful.

Not **D** (execution defect): nothing is meant to execute here.

The defect is that **an action-shaped button plus a free-text box plus a success
message reads as "instruction accepted"**, and the confirmation says
`Alert → resolved` without stating that nothing else occurred.
---

## 3. Changes made

Nothing in the execution path was touched, because §1 found it correct. The
change is confined to what an executive reads at the moment of acting.

### 3.1 `governance-mgmt.html` — the interaction (3 edits)

*Rests on: U-1, U-4 (MEASURED). Not deployed — the owner publishes this file.*
Backup at `governance-mgmt.html.bak2`; 77,526 → 79,663 chars.

| | before | after |
|---|---|---|
| button | `Resolve` | `Mark handled` |
| dialog | `Resolve — note (kept on the record):` | `Mark as handled — you are recording that YOU dealt with this.` … `The note is kept for audit only. It is NOT an instruction: no email, agent or action will run from what you type here.` |
| toast | `Alert → resolved` | `Alert → resolved — recorded. No action was sent.` |

`Cancel` gets the same treatment; `Escalate` gets an honest and *different*
message, because escalate genuinely acts (§4.3). The old wording was not false —
"kept on the record" is literally what happens. It simply never said that
**nothing else** happens, and an action-shaped button plus free text plus a
success toast supplied the missing half by implication.

### 3.2 No change to Python, SQL, triggers, endpoints or the audit trail

**MEASURED:** `git diff --stat app/` is empty. The `app/` tree is byte-identical
to `HEAD`. The transition, the trigger, the lifecycle, the ledger and the
approval machinery are untouched.

### 3.3 Ten regression tests — `governance/tests/test_governance_activation.py`

**MEASURED:** diff is `283 insertions, 0 deletions`; collection goes 104 → 114.
Purely additive.

---

## 4. Tests

### 4.1 The negative case, and proof it can fail

The mandate asks for the negative case first. A test asserting *"nothing
happened"* passes just as well when the probe is wired to the wrong target, so
it was **mutation-proved** rather than trusted:

**MEASURED.** A mutant was installed in `transition()` — on `resolved`, if the
note contains "email", call `send_email`. The test **FAILED** at the transport
assertion. Mutant reverted; `git diff app/core/governance_alerts.py` is empty.

The test pins the reported scenario verbatim: drive the alert to `resolved` with
the note `"email this alert to CFO"` and assert **no mail transmitted · no
capability dispatched · no new row in `action_approvals`, `staff_email_ledger`
or `event_queue` · the note stored verbatim · `closure_evidence` still NULL**.

> **A trap worth recording.** The probes are installed *after* the alert exists.
> `open_alert()` legitimately emails the owner — the `alert_assigned` fix shipped
> in P1 — so probing earlier would have counted a real, correct email as a
> Resolve side effect. The test would have failed for a true reason and a false
> cause.

### 4.2 The business condition (mandate §10)

`test_resolving_the_alert_does_not_touch_the_business_condition` asserts the
overdue-invoice count is unchanged across the resolution — using **the same
definition the detector reads** (`accounting_invoice_pipeline` where
`computed_balance_due > 0 AND due_date::date < CURRENT_DATE`, per the
`ar_summary` block of the base schema), not a plausible-looking query of my own.
My first attempt invented `invoices.payment_status`, a column that does not
exist; had it happened to exist, the test would have measured a proxy. That is
the antipattern this engagement removed six instances of in one day.

### 4.3 Alternate routes (mandate §9)

`TestNoAlertRouteInterpretsTheNote` drives **resolve · close · cancel** through
the real HTTP handler (`api_step`) with a real bound executive session, asserts
each reaches its intended terminal status, and asserts no mail and no dispatch.

**Escalate is included precisely because it DOES act** — it emails the
escalation authority. The tests must be able to tell an acting route from a
non-acting one, rather than asserting silence everywhere and calling that a
result. What is pinned is that the effect comes from the **alert**, never the
note: recipient is the escalation authority, and neither the note nor "CFO"
appears in the message.

> **Both route tests were vacuous when first written, and were caught.**
> (a) The parametrised loop broke on `step == action`, which never matched
> `"cancel"` — that case silently re-ran `resolve` and passed. Fixed by asserting
> the **final status**. (b) `GOV_ROUTE_EMAIL` is off locally, so escalate
> produced **zero** mails and the assertion loop never executed. Fixed by
> enabling the flag (transport already faked) and asserting `len(sent) == 1`, so
> it cannot go vacuous again.

### 4.4 The invariant that keeps Resolve non-executable

`test_resolution_note_has_exactly_one_writer_and_no_reader` scans `app/` and
requires exactly one occurrence — the write. A future `SELECT resolution_note`
is not automatically wrong, but it must now be a deliberate decision.

### 4.5 Results

**MEASURED**, full suite (`testpaths = governance/tests`):

```
3 failed, 2986 passed, 2 skipped in 247.27s
FAILED test_hybrid_retrieval.py::test_recency_only_output_is_frozen
FAILED test_hybrid_retrieval.py::test_the_standing_disclosure_and_the_drift_signal_are_not_the_same_flag
FAILED test_hybrid_retrieval.py::test_status_carries_both_pool_signals
```

**These three are pre-existing and unrelated** — they reproduce in isolation,
touch no alert code, and are not environmental noise. They are a **drift guard
doing its job**:

> `the corpus moved away from the validated state: template crowding worsened:
> largest group 548 against N_VEC 500 is a ratio of 1.10, versus 0.96 when the
> pool size was validated (480 against 500)`

The content corpus has grown past the pool size the retrieval config was
validated at. That is a real signal needing a revalidation decision. **Out of
scope here, and recorded rather than absorbed.**

### 4.6 One intermittent failure, mechanism identified

`TestDailyProposalCap::test_hitting_the_cap_opens_one_owned_work_item_per_day`
failed once, then passed on an identical re-run and in every narrower scope.
Rather than write it off as flake, the mechanism was measured:

**MEASURED.** The test asserts exactly one non-terminal `sampled_review` alert
for the test intent, but a later test in the suite leaves one **open**, and the
dedupe key is date-stamped (`proposal_cap:test.activation_write:2026-09-09`). On
the first run of a new day the stale row from yesterday no longer folds — two
open rows, assertion fails. Its own `finally` then cancels both, so every later
run that day passes. The database confirms the pattern: 09-08 has five rows, all
cancelled and none open, cleaned up by today's first failing run.

A census assertion over a population that persists across days — the same
proxy-vs-property shape corrected six times in P1, surviving in a test. It will
recur on the first full run of **2026-09-10**. **Pre-existing, not caused by this
work** (the leaked row predates it), and **not fixed here**: it is a different
finding, and the scoping discipline of this mandate is worth more than the
one-line fix. **RECOMMENDATION:** scope the predicate to the current day.

---

## 5. Two things found while verifying, which are NOT this remediation

Recorded because the mandate forbids collapsing unknowns into assumptions — and
equally forbids leaving a now-measured fact filed as unknown.

### 5.1 The alert-mail ledger gap — root cause MEASURED (header items 1 & 2)

The header lists *"the operational status of the idempotency guard"* and *"the
ledger write path and its failure mode"* as **never checked**. Enabling
`GOV_ROUTE_EMAIL` to de-vacuum the escalate test measured both by accident:

```
[staff_email] claim failed: new row for relation "staff_email_ledger" violates
check constraint "staff_email_ledger_email_kind_check"
DETAIL: Failing row contains (..., alert_escalated, critical, ...)
[staff_email] ledger unavailable for alert_escalated:607babe1... — proceeding UNRECORDED
```

```sql
CHECK (email_kind = ANY (ARRAY['approval','escalation','escalation_remind','digest']))
```

The code emits three kinds that are **not in that list**:
`alert_assigned` (`governance_alerts.py:209`), `alert_escalated` (`:347`),
`alert_reescalation` (`:421`).

**MEASURED:** every alert email fails its ledger claim, the failure is swallowed
by the documented fail-open, and the mail sends **unrecorded**. This is the
mechanism behind "65 ledger rows, not one of them `alert%`, while the CEO's
inbox holds escalation notices".

**INFERRED:** the `(kind, ref)` idempotency guard is therefore **not operating
for any alert mail**; the only thing left preventing duplicate escalations is the
`escalation_notices` counter on the alert row — a different mechanism with a
different failure mode, doing a job the code believes the ledger is doing.

**NOT FIXED HERE.** The fix is a one-line constraint change, but it is a schema
migration, it is a different finding, and the header states this remediation does
not address it. **DECISION REQUIRED.**

### 5.2 `closure_evidence` is enforced present, never checked for truth

**MEASURED:** the only occurrence of `closure_evidence` in `app/` is the write
(`governance_alerts.py:246`). The trigger requires it to be non-null to reach
`closed`; nothing reads it back. Adjacent to this finding and **explicitly not
the same defect** — evidence that is required but unverified is a weaker claim
than a note that implies execution. Recorded, not acted on.

### 5.3 §11 audit — no other instance of the confirmed pattern

**MEASURED:** 25 free-text prompt sites across 4 pages
(`governance-mgmt` 16, `email-mgmt` 6, `lead-mgmt` 2, `agent-studio` 1). Every
other one is either **consumed as a structured parameter** (assignee, search
term, decision mode, approver role, SLA hours, daily cap, sample rate) or a
**justification attached to an action that genuinely executes** (policy change,
capability disable, delegation, override, approval, queue clear). Neither shape
can produce this gap, because in both the world actually changes.

Resolve was unique in having all three of: a button naming a business outcome,
unconstrained text, and no change beyond a label. **No other instance found.**

---

## 6. Regression assessment

**No P0 or P1 control changed.** `git diff --stat app/` is empty; no SQL, no
trigger, no endpoint, no policy row was modified. N-01, N-02, the bound-identity
split, owner resolution and the orphaned-event path are untouched.

Two regressions were introduced *during* this work and both were caught:

1. **A P1 control class was silently deleted.** A truncate-and-reappend script
   cut at the wrong `\n\n\n` and removed `TestUnattendedTestSeam` — the five
   tests restored by P1 Unit 2, which prove a laptop cannot become an unattended
   sender. Caught by reading `git diff --numstat` (`1952 / 1670` for what should
   have been a 200-line addition) rather than trusting a green run: **the deleted
   tests could not fail, so the suite stayed green.** Restored from `HEAD`;
   collection is now `104 → 114`, diff `283 / 0`.
2. **Line endings.** The same script rewrote CRLF as LF across the file,
   producing an unreviewable diff. Restored.

Neither reached a commit. Both are recorded because "the suite was green" was
exactly the wrong evidence in case 1, and that is worth more than the fix.

---

## 7. Before and after, for the exact scenario reported

**Before.** The CEO opens the console, sees an alert reading *16 invoices
overdue*, clicks a button labelled **Resolve**, is asked for *"note (kept on the
record)"*, types **"email this alert to CFO"**, clicks OK, and is told
**`Alert → resolved`**. The CFO is not emailed. The invoices are still overdue.
Every word the system said was true and the executive still left with a false
belief.

**After.** The button reads **Mark handled**. The dialog reads *"Mark as handled
— you are recording that YOU dealt with this. The note is kept for audit only.
It is NOT an instruction: no email, agent or action will run from what you type
here. Why is this handled?"* The confirmation reads **`Alert → resolved —
recorded. No action was sent.`**

**Unchanged:** the row, the trigger, the audit trail, the note stored verbatim,
`closure_evidence` still required to close. The record was always truthful; only
the interaction was not.

---

## 8. What remains UNVERIFIED

- **Whether any of this generalises beyond local.** The HTML change is not
  deployed — the owner publishes `agentorc.ca`. Nothing here was observed on
  Railway. The header's item 5 stands.
- **Header items 3 and 4** — upstream mail-authority invariants, and the causal
  chain alert creation → assignment → ledger. Item 4 is now partly illuminated
  by §5.1, but the chain was not traced end to end. **Still open.**
- **Whether the CEO, seeing the new wording, would read it correctly.** Changed
  text is a hypothesis about a human, and no human has seen it yet. The tests
  pin that the words are present; they cannot pin that they work.
- **The three drift failures** (§4.5) need a revalidation decision.
- **§4.6 and §5.1 need decisions**, not quiet patches.

This section verified four specific claims and measured two more by accident.
**It is still not a P2 assessment.**
