# Owner decisions — 2026-09-07

Three decisions taken by the owner during P1 remediation, recorded here
because each changes what the system does and none is derivable from the code.
Decision C qualifies Decision A; read them together.

---

## Decision A — Workflow-engine owner fallback

**Decided: FAIL CLOSED is the destination. It is NOT the next unit. Reverse the
order — ownership backfill (D-02) first, enforcement second. Declare the
invariant in code now as a counted metric so the rate is visible and cannot
worsen; enforce it when resolution can actually succeed.**

### The defect

`workflow_execute_action`'s create_task branch resolves the owner from the
triggering payload and, when that yields nothing, writes the activity with a
NULL owner anyway. The platform manufactures consequential work while already
knowing accountability could not be established.

### The evidence that set the sequence

| | |
|---|---|
| Workflow-created activities, all time | **1,667** |
| …with *an* owner | 1,414 |
| …with an **eligible** owner | **7 (0.42%)** |
| Open and unowned right now | 180 |
| Accounts with an eligible owner | **0 of 181** |

Why the surface exists, by upstream cause: 112 the related entity has no owner,
**63 the related entity's owner is a customer identity**, 5 identity collisions.
The engine is faithfully propagating an ownership vacuum, not creating one.

### Why the invariant was reworded

The first wording — *"may not be persisted with `owner_id = NULL`"* — is too
weak to achieve its own goal. It would refuse 253 of 1,667 (15%) and
**legitimise the 1,407 activities owned by customer identities**, which is D-02
itself. The system would pass the check and remain exactly as unaccountable.

The wording that matches the intent is:

> **No workflow-generated consequential work may be persisted without an
> ELIGIBLE ACCOUNTABLE HUMAN owner.**

…and enforcing *that* today refuses **1,660 of 1,667** creations, because there
is nothing eligible to resolve to. That is not a guard; it is switching invoice
follow-up off. An overdue invoice does not become less overdue because the task
was refused — it becomes invisible.

### What was implemented now

- `work_ownership.WORKFLOW_OWNER_REQUIRED = False` — the enforcement point
  exists, is named, and is one flag away.
- `work_ownership.workflow_ownership_rate()` — the measurement, as a **ratio**,
  not a count. A count decays into a census as the population grows; this file
  has already had to remove that pattern twice.
- `/platform/health` → `workflow_work_accountable` = **0.42%**, WARNING, stating
  plainly that the invariant is declared and why it is not enforced.
- Two gated tests: the rate may rise and never fall; and the flag cannot be
  turned on while the rate is below 95% without failing loudly with the numbers.

### What must happen before enforcement

> **SUPERSEDED IN PART BY DECISION C — read that first.** Two claims below did
> not survive the evidence, and are kept rather than edited away because the
> reasoning that produced them was sound and the correction is the useful part.

~~The D-02 ownership backfill: accounts, invoices and payments acquire eligible
staff owners.~~ **There is no staff layer to acquire them.** All eight employees
are attested synthetic; the accountable population is the five executives.

~~**Not** a fallback to a governance authority.~~ Correct as a principle, and its
precondition is absent — the separation requires two populations and there is
one. Decision C makes resolution through the **declared, governed** action-class
mapping the intended path, which is not the "arbitrary executive" this rejected.

~~**Success condition:** `workflow_work_accountable` climbs on its own as the
backfill lands, without the write path changing.~~ **It does not climb on its
own** — measured after the seven grants, it was unchanged at 0.42%. Granting
changes the eligible population and reassigns no work. Readiness is
`workflow_owner_activation_readiness()`, whose eight conditions are the actual
success condition.

---

## Decision B — The 18 orphaned `order.shipped` notifications

**Decided: WRITE OFF by default with durable reasoning. Send only where the
evidence shows the customer still materially needs it. Never blind-replay.**

### The evidence

| | |
|---|---|
| Orders | 18, **all still `status='shipped'`** |
| Order date | 2026-08-30 |
| Last touched | **2026-09-02** — nothing since |
| Received an order confirmation | 17 of 18 |
| Received **any** email | 17 of 18 |

### The disposition

| Action | Orders | Reason |
|---|---|---|
| **SEND** | **`SO-2026-102219`** (1) | Has received **no communication of any kind** — no confirmation, no shipping notice. The customer does not know the order exists. This is the one case where the notification is still materially useful. |
| **WRITE OFF** | The other **17** | The governed path has only the "your order has shipped" template, which today announces as news something that happened last week. Sending it states something misleading. |

**The write-off reason is NOT "it probably arrived."** These orders have not been
touched since 2026-09-02, so the system's *knowledge* stopped five days ago —
"the customer already has it" is an assumption about a state that is not being
tracked. The honest reason is that the only available message is stale.

### The 21 `order.status_changed` events

Replay freely or leave; either way they contact nobody.
`handle_order_status_changed` closes the order's milestone activity and has no
email half — it was deliberately removed so two senders with two idempotency
stores could not each conclude the other had not sent.

### Execution constraints

- **This is production work and happens after deployment**, not as part of code
  remediation.
- `order_notifications` is keyed `UNIQUE(order_id, event_type)`, so a repeated
  drain re-sends nothing.
- Do **not** `POST /agent-bus/drain` for all 39: it does not honour this split.
  The one send goes through the governed notification path for that order alone.
- Close the `event_orphaned` alert (owner CTO Bill Wang) with
  `closure_evidence` naming the 1 sent and the 17 written off, and this document.

### A finding raised by this evidence, not previously registered

**18 orders have sat at `status='shipped'` for five days with no update.**
`order.delivered` appears never to fire for them. That is a separate reliability
question from the orphaned events and is not in any register yet — the orphan
alert would not catch it, because no event is stuck; none is being produced.

---

## Decision C — Ownership maps to the executives at this scale

**Decided: the accountable population is the five executives. At this scale,
authority IS ownership.**

### This QUALIFIES Decision A, and the record should say so plainly

Decision A stated:

> *"I would also **not** automatically resolve it to CTO/COO/CEO. Authority and
> ownership are different concepts. A governance authority should not become the
> owner of arbitrary customer work merely because the actual owner is missing."*

That was correct as a principle and correct on the evidence available then. It
assumed a staff layer existed to own the work. **It does not.**

`corpus_provenance` (rule `human_attested`, `decided_by attest:owner:2026-09-02`)
records that **all eight employees are attested SYNTHETIC** — Daniel Lee, Karen
Patel, Robert Garcia and the rest are demo personas. The
production-accountable population is, and was always, the five executives.

So the principle is not abandoned; **its precondition is absent.** The
separation between authority and ownership requires two populations, and there
is one. A future reader must not mistake this for a reversal of judgement — the
evidence changed what the judgement applies to.

### The seven grants: applied, then reversed

Applied under Decision 1 of this date, then reversed once Decision C made them
contradictory: `fn_owner_eligible` was returning True for seven attested-
synthetic personas, and the owner-eligibility trigger would have accepted
"Daniel Lee" as the accountable owner of a governed proposal.

Reversal was a **full unwind, not a deactivation**, and only because the
precondition held: **zero work items had been assigned to any of the seven** —
verified across activities, accounts, contacts, leads, opportunities, orders and
action_approvals before deleting anything. `assignable.revoke()` keeps a
deactivated row deliberately, because "who could receive work last quarter is a
real question"; here there was no such past to explain, and a surviving `owners`
row would have blocked a future legitimate grant with `already_linked`.

`assignable.revoke()` was still called first on each, so the reversal has an
audit line of its own.

**Eligible owners now: 5, all attested real.** Local only — Railway never had
the grants.

### What this decision unblocks, and it is more than it looks

The mapping from work to executive **already exists and is already governed**:
`governance_action_policies` assigns every action class to an approver role —
CRO revenue/commercial, CFO financial/payment/accounting, CTO technology/data,
COO operations/fulfilment, CEO escalation — and
`governance_policy.resolve_accountable_owner(role)` already resolves a role to
an eligible owner. No new mapping is invented by this decision.

**This changes the Unit-1 remediation design for the better.** Decision A chose
FAIL CLOSED because the alternative was "resolve to an arbitrary executive".
Under Decision C that alternative is no longer arbitrary: the workflow engine
can resolve the owner through the *declared, governed* action-class mapping, and
fail closed only when even that yields nothing. The write stops manufacturing
unaccountable work **without** switching invoice follow-up off — which was the
objection to shipping fail-closed first.

**RECOMMENDATION for the next unit:** re-open Decision A's remedy as
*resolve-through-the-governed-mapping, else fail closed*, rather than
*fail closed*. That is a materially different and better design, and it exists
only because Decision C supplied a principled owner to resolve to.

### What this decision does NOT resolve

- **jmartin** remains HELD. The collision is in the employee/owner identity
  space and is unaffected by who owns work.
- The workflow write path still creates unowned work; nothing shipped.
- `WORKFLOW_OWNER_REQUIRED` stays OFF. Condition 3 ("books of business
  explicitly assigned") is now *answerable* — the action-class mapping is the
  assignment — but answering it is a separate implementation step.
- The ~4,700 existing customer-owned work items are untouched historical debt.

---

## Decision B — EXECUTION RECORD, 2026-09-08

Executed after deploy #72. **Outcome: 39 events drained, 0 orphaned remaining,
alert resolved, and ZERO emails sent.**

| | |
|---|---|
| `order.status_changed` | 21 processed — that handler has no email half, so nobody was contacted |
| `order.shipped`, written off by decision | **17** — `order_notifications` rows at `state='skipped'` carrying the Decision B reasoning |
| `order.shipped`, intended to SEND | **1** — `SO-2026-102219` |
| Emails actually sent | **0** |

### The one send was refused, and the refusal was correct

```
lila.brooks-d8cb@seed.agentorc.ca is not a verified, deliverable recipient
(is_email_verified is false, or the domain is a reserved placeholder)
```

`SO-2026-102219`'s customer is a **synthetic seed contact**: an unverified
address on `seed.agentorc.ca`, which is a reserved placeholder domain. The
outbound guard refused it for two independent reasons, and would have refused it
on 2026-09-02 exactly as it did today.

### THE CORRECTION, stated plainly because it was asserted repeatedly

Across several exchanges this order was described as **"a real customer with no
communication at all since 30 August"**, and was written into
`docs/p2_assessment_brief.md` as the item that *"outranks the assessment"*
because *"a real customer is waiting"*.

**No real customer was waiting.** That was an INFERENCE presented as a FACT.

The reasoning was: *no notification exists → nobody was told → a person is
affected*. The first two steps were true. The third does not follow, and the
evidence to check it — `contacts.is_email_verified` and the recipient domain —
was available the whole time and was never looked at. It is the same
proxy-versus-property failure this engagement kept finding, committed by the
assessor: **"no communication exists" was used as a proxy for "a person is
waiting", and it diverged for the eighteen records where it mattered.**

The urgency was wrong. **The disposition was not.** Writing off 17 and refusing
to blind-replay all 39 was correct on its own merits, and the mechanism —
recording the write-off in the subsystem's own idempotency ledger, so `notify()`
short-circuits on `TERMINAL_STATES` rather than relying on anything bolted
alongside — is what a later replay will meet.

### What this says about the corpus, which IS worth carrying forward

An orphaned customer-notification backlog that looked like eighteen unkept
promises was eighteen synthetic records that could never have been emailed. The
event fabric defect was real; **the customer harm was not.** A P2 assessor
weighing "consequential business effect" should establish whether a subject is
real *before* costing the consequence — `corpus_provenance` and
`is_email_verified` are the fields that answer it.
