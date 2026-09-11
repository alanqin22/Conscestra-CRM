# Decision — discharging an alert is not role-bound

**2026-09-11.** Closes **A-15** of `docs/architecture_reassessment_2026-09-09.md`
as a *declared decision*, not as a defect.

**Decision: any eligible executive may discharge any alert, whoever it is
accountable to. Role affinity is NOT enforced on alerts, and this is deliberate.**

---

## The finding

The 2026-09-09 reassessment raised A-15 on a single observation: alert
`757f77d3` was accountable to **CTO Bill Wang** and was acknowledged and
resolved by **`cfo@agentorc.ca`**.

The mechanism is an asymmetry between two paths that otherwise look alike:

| | Approvals | Alerts |
|---|---|---|
| Guard | `governance._authority_check` | `governance_policy.bound_authority` |
| Checks you are an eligible executive | yes | yes |
| Compares you to the item's authority/owner | **yes** — a CFO may not decide a CRO's row; the CEO may; an escalated row admits the escalation authority | **no** — `accountable_owner_id` is never consulted |

A-15 was recorded as MEDIUM, and its substance was not "this is wrong" but
**"this is neither enforced nor declared"** — the reader cannot tell a decision
from an oversight.

## The evidence that settled it

Measured on production 2026-09-11, every alert ever discharged:

| Rule | Accountable to | Discharged by | Own desk? |
|---|---|---|---|
| `event_orphaned` | CTO Bill Wang | `agent-bus:drain` | *(machine)* |
| `bus_stalled` | CTO Bill Wang | CEO Alan Qin | **no** |
| `platform_degraded` | CTO Bill Wang | CEO Alan Qin | **no** |
| `unbilled_orders` | CFO Sherman Zhang | CEO Alan Qin | **no** |
| `stalled_deals` | CRO Daping Qin | CEO Alan Qin | **no** |
| `unworked_leads` | CRO Daping Qin | CEO Alan Qin | **no** |
| `ar_spike` | CFO Sherman Zhang | CEO Alan Qin | **no** |
| `platform_degraded` | CTO Bill Wang | CFO Sherman Zhang | **no** |

**7 discharged by an executive, 7 cross-desk, 0 by the accountable owner.**

Cross-desk discharge is not an edge case in this system. It is the only thing
that has ever happened. **Enforcing affinity would not have tightened anything —
it would have blocked every alert discharge ever performed here.**

## Why not enforce it

1. **It would break the operating model, not protect it.** One person operates
   this platform across five executive identities. Affinity would convert every
   discharge into a delegation step that the same human would then perform as
   the other role — bookkeeping with no reader.
2. **Alerts and approvals are different objects.** An approval authorises a
   *consequential action*, so who exercises that authority is the whole point.
   An alert is an *obligation to look at something*; clearing it asserts the
   obligation is discharged, and a second executive is competent to assert that.
3. **The claim already carries a name.** A-03 bound `resolve`, `close`,
   `cancel`, `assign` and `escalate` to the signed-in executive, so a discharge
   is attributable whoever performs it. Affinity would restrict *who may act*;
   binding already guarantees *we know who did*. The second is the property that
   was actually missing, and it is now present.

## What this decision costs, stated plainly

**`accountable_owner` means "who is answerable", not "who acts", and on this data
the two have never once coincided.** An auditor reading
`accountable_owner: CTO Bill Wang` on a resolved alert would reasonably infer
Bill Wang resolved it. In production that has never been true of anyone.

The field is not wrong — it correctly names who the SLA and the escalation are
routed to. It simply does not predict the discharger, and nothing in the schema
says so.

## What was changed instead of enforcement

1. **The console names the desk at the point of action.**
   `governance-mgmt.html` now passes `accountable_owner` into `alertStep()`, and
   when it is not your own the prompt reads:

   > This item is accountable to CTO Bill Wang, not to you.
   > Clearing it is permitted and is recorded against your name, not theirs.

   Informational, not a confirmation dialog: this is the normal case, and a
   warning on the normal case is one that gets clicked through. It appears
   inline in the prompt the operator is already reading, on resolve, cancel,
   escalate, assign and close.

2. **The absence is declared by test.**
   `governance/tests/test_alert_role_affinity_is_declared.py` asserts that
   `bound_authority` does not reference `accountable_owner`, that approvals
   *do* enforce affinity (the contrast — without it the first assertion would
   pass on a system that had simply lost role binding everywhere), and that this
   note exists and still carries its evidence.

   Anyone who later adds affinity must delete a test that explains why it was
   absent, rather than rediscovering the question.

## When to revisit

Enforce affinity if **the operating model changes**: if desks become real, with
each executive working their own queue, and `own_desk = true` starts appearing
in the table above. The trigger is a change in who actually clears alerts, not a
change of opinion about who should.

The measurement that would show it is the one in this note — re-run it. If
cross-desk discharge stops being 7 of 7, this decision has expired.

## Status

| | |
|---|---|
| A-15 | **DECLARED — not a defect** |
| Enforcement | none, deliberately |
| Attribution | present (A-03: the five clearing transitions are session-bound) |
| Visibility | console names the desk at the point of action |
| Declaration | this note + `test_alert_role_affinity_is_declared.py` |
