# A3 architecture — financial state and evidentiary provenance

**2026-09-13.** CTO architecture, reconciled against
`docs/decision_a3_financial_truth.md` (D1–D9, H1–H3).

This document is an architecture recommendation. It does not set financial
policy, and where it appears to, the policy document governs.

Labels used throughout: **FACT** (directly observed, read-only, 2026-09-12/13) ·
**POLICY** (decided by CEO/CFO) · **ARCHITECTURE** (recommendation) ·
**UNKNOWN** (evidence insufficient) · **FUTURE** (deliberately out of scope).

---

## 1. What H1–H3 changed

**H3 is the structural correction.** The earlier architecture used a single
disposition field carrying both the economic state and the confidence with which
the system could explain its origin. That is the defect D3 identifies in
`orders.status`, reproduced one layer up. The model is now two axes.

**H1** settles taxability as policy: standard treatment unless an explicit
classification says otherwise, and a product whose treatment cannot be
established is blocked. The representation is still absent; the question is no
longer open.

**H2** settles that absence of an exemption record means not exempt. This is a
declared rule with a stated scope, not an inference drawn by software because it
needed a value, and it removes what would otherwise have blocked every ordinary
taxable customer.

## 2. Corrections to the previous architecture

Three, and the third is material.

**Correction 1 — one axis became two.** See §3.

**Correction 2 — the 2,032 orders are not blocked.** The earlier report had
orders without surviving fulfilment evidence heading toward `blocked`. Under H3
they are recorded by their authoritative financial outcome with provenance
`historically_unverified`. An incomplete event history must not damage an
authoritative financial record.

**Correction 3 — the amount is NOT authoritative for every order.** The earlier
report stated that the amount is authoritative because it is computed
server-side from `order_items.line_total`. That holds only for single-currency
orders. See §5, which is the most consequential finding in this review and is
not an A3 question at all.

## 3. The two-axis model

**ARCHITECTURE.** Two records, each carrying one meaning.

### Axis A — financial disposition

Derived from authoritative, observed financial facts: invoice existence, invoice
status, payment state. It carries no claim about evidence.

`not_yet_invoiceable` · `invoiceable` · `invoiced_outstanding` ·
`invoiced_paid` · `credited_reversed` · `obligation_released` · `excluded` ·
`blocked`

`blocked` belongs on this axis and means one thing only: **a fact required to
take a new financial action now is unknown.** It never means that historical
evidence is missing.

### Axis B — fulfilment provenance

An evidentiary assertion about the basis that made an obligation invoiceable.

`established` · `historically_unverified` · `unknown`

Each assertion records a rule, an evidence reference where one exists, a
decision time, and a responsible actor where applicable.

**The `rule_supports_state` constraint belongs here and only here.**
`corpus_provenance` already implements exactly this shape and is deployed:

```sql
CHECK ( (state='synthetic'  AND rule IN (flagged_synthetic, seed_generator, human_attested))
     OR (state='real'       AND rule IN (external_trace, human_attested))
     OR (state='ambiguous'  AND rule = 'no_evidence') )
```

Read in both directions, the last clause says that unknown may only be asserted
by absence of evidence, and that absence of evidence may only assert unknown.
That is the governing principle as a database constraint. **It must not be
applied to Axis A**, because a paid invoice is not a claim requiring evidentiary
support — it is the evidence.

### The permitted combinations

| Financial disposition | Fulfilment provenance |
|---|---|
| not yet invoiceable | unknown, or established |
| invoiceable | **established only** |
| invoiced outstanding | established, or historically_unverified |
| invoiced paid | established, or historically_unverified |
| credited / reversed | established, or historically_unverified |
| obligation released | as independently known |
| blocked | any — blocking concerns a currently required fact |

`invoiceable` is the one disposition that demands `established`. That single row
is what prevents a new invoice being raised on an unevidenced obligation, while
leaving every historical record truthful.

## 4. Authoritative source for each financial fact

| Fact | Source | Status |
|---|---|---|
| Commercial obligation | the order | **FACT** — the order is the accepted commitment today |
| Fulfilment evidence | `events` — `order.shipped` / `order.delivered`, referenced by `event_uuid` | **FACT** — 460 of 2,492 orders have one |
| Invoice existence and status | `invoices` | **FACT** — 1:1, uniquely indexed, complete |
| Payment state | `invoices.status`, `balance_due` | **FACT** |
| Line amount | `order_items.line_total` | **FACT, and currency-dependent — see §5** |
| Line currency | `product_pricing.currency_code`, matching `products.currency_code` | **FACT** — 6,062 of 6,164 lines agree |
| Tax rate | `tax_rates` | **FACT** — jurisdictional, effective-dated, 67 rows |
| Jurisdiction | order billing/shipping address | **FACT** — 1,681 of 2,492 have a billing address |
| Product taxability | — | **UNKNOWN** — H1 sets policy, no representation |
| Customer exemption | — | **POLICY resolves it** — H2: absence means not exempt |
| Approved proposition | `action_approvals.amount` only | **Partial** — see §7 |

## 5. The finding that outranks A3

**FACT, verified 2026-09-13.** `line_total` is denominated in each line's own
pricing currency. It is not normalised to an order currency.

- `products.currency_code` is populated on all 415 products: **245 CAD, 170 USD**.
- `product_pricing.currency_code` agrees with the product on **6,062 of 6,164**
  priced order lines.
- **1,101 of 2,492 orders (44%) contain lines in both currencies.**
- `orders.total_amount` equals `SUM(order_items.line_total)` on **all 2,492**
  orders, so for those 1,101 it is a sum of two currencies.
- The invoice subtotal is computed the same way.

Worked example, order **SO-2026-100394** (order currency null):

| Line | Currency | Amount |
|---|---|---|
| NATURELO Multivitamins | USD | 2.99 |
| Rubbermaid Brilliance | CAD | 95.91 |
| Post-it Flags | CAD | 35.46 |
| Dishwasher Magnet | USD | 91.08 |
| Naive sum | — | 225.44 |

**INV-000721 was issued as CAD 225.44 subtotal, 29.31 tax, 254.75 total** — a
CAD invoice containing USD 94.07 counted as CAD.

**Exposure, FACT:** 1,003 of those mixed-currency orders have been invoiced,
carrying **663,881.34** of invoiced value, of which **941 invoices are paid**.

**INFERENCE, strongly supported:** those invoices misstate their totals by the
exchange difference on the minority-currency lines.

**UNKNOWN:** the magnitude. The system holds no usable FX rate — 17 invoices are
cross-currency and **none** carries a non-unity `exchange_rate` — so it cannot
compute the size of its own discrepancy.

**This is not an A3 design question.** It is a live property of existing
invoicing, present in paid invoices, and it exists whether or not A3 is ever
built. It is recorded here because it was found during this reconciliation and
because it changes what "the amount is authoritative" means. It is a **BUSINESS
DECISION REQUIRED**, and nothing has been repaired.

**Consequence for A3:** an order whose lines are not all in one currency has no
single transaction currency and therefore, under D5, cannot be invoiced until
the business decides how such an order is priced. Of the 70 currently
uninvoiced orders, **31 are mixed-currency** and 39 are single-currency.

## 5b. Currency architecture (C1, C8, C9)

**B1 — one transaction currency per order.** **ARCHITECTURE, implementing C1.**

> Every invoiceable order has exactly one explicit transaction currency, and
> every monetary component of that order is denominated in it.

Three representations are explicitly rejected as sources for that currency.
`orders.currency = NULL` is not a valid state for an invoiceable order; it is
unknown, and 2,434 orders hold it. The invoice currency is not the source, since
the invoice is produced *from* the order and deriving one from the other would
make the arithmetic self-justifying — which is exactly how 1,003 CAD invoices
were produced from orders that had no currency at all. And CAD is not inferred
because the invoice happens to say CAD.

**B3 — line denomination is not order currency.** `products.currency_code` and
`product_pricing.currency_code` are **evidence about the denomination of a
line**. They are not, individually or collectively, the order's transaction
currency. For a conforming future order the two must reconcile: every line's
denomination equals the order's declared currency. Reconciliation is the test;
inference from the lines is not a substitute for the declaration.

**B2 — `mixed_transaction_currency` as a blocking condition.** **ARCHITECTURE,
implementing C8.**

| Aspect | Design |
|---|---|
| Detected by | comparison of every line's denomination against the order's declared transaction currency |
| Evidence | the set of distinct line denominations, and the declared order currency |
| Resulting state | Axis A disposition `blocked`, reason `mixed_transaction_currency` |
| Owner | the order's accountable owner, surfaced through `governance_alerts` |
| Resolution | within the current capability: the commercial terms are corrected so that one currency governs, or the order is split into conforming single-currency orders. Conversion is **not available** as a resolution today — see below |
| Prevents execution because | `invoiceable` is unreachable from `blocked`, and the approval proposition binds a currency that cannot be produced |

An order of CAD 100 and USD 50 must never yield CAD 150. It yields a blocked
state with a named reason and a responsible human.

**On conversion, stated precisely.** A mixed-currency order cannot become
invoiceable under the current one-currency-per-order model, and conversion must
not be used to resolve the present condition. That is a **scope limitation of
the current product-order capability, not a permanent prohibition**: under C9,
multi-currency conversion is a FUTURE capability requiring a separate CFO and
CTO decision and an authoritative FX model. The distinction matters, because
recording today's limit as a permanent business rule would be an implementation
constraint quietly promoted to policy.

**B4 — FX is a future capability.** **FUTURE, per C9.** No FX table, column or
partial conversion mechanism is designed in this phase, and none should be
introduced to repair existing data. When multi-currency commercial transactions
become genuinely necessary, FX requires its own authoritative model covering the
rate, its source, its effective date and time, the conversion direction,
rounding, accounting treatment and auditability. Designing a fragment of that
model now, under the pressure of a historical defect, is how an implementation
convenience becomes an accounting policy.

**B5 — where mixed currency belongs in the two-axis model.** Mixed currency is a
**financial eligibility condition on Axis A**. It is neither an incorrect paid
outcome nor a provenance failure, and it must not be collapsed into fulfilment
provenance: the fulfilment evidence for a mixed-currency order may be perfectly
good. Axis B answers whether the fulfilment basis is established; Axis A answers
whether a financial action may proceed now. `mixed_transaction_currency` is the
second question.

---

## 6. Historical-data treatment

**ARCHITECTURE.** Nothing is backfilled, inferred, or repaired.

| Population | Axis A | Axis B |
|---|---|---|
| 2,282 orders with a live invoice | from invoice and payment state | `established` where an event exists, else `historically_unverified` |
| 460 orders with fulfilment events | as above | `established`, citing `event_uuid` |
| 2,032 orders without fulfilment events | unchanged by the absence | `historically_unverified` |
| 70 uninvoiced orders | `not_yet_invoiceable`, or `blocked` | `unknown` |
| 8 orders with incoherent linkage | `blocked` | `unknown` |
| 1,101 mixed-currency orders | **pending the §5 decision** | independent of currency |

Explicitly prohibited: fabricating fulfilment events; inferring shipment from
`completed`; retroactively blocking paid invoices; rewriting historical
outcomes; and treating a missing event as evidence that fulfilment did not
occur.

**B8 — the forward rule does not reach backwards.** These are different
statements about different populations and the architecture must keep them
apart:

| | |
|---|---|
| **Forward rule** | a future order with mixed line denominations is `blocked / mixed_transaction_currency` and cannot become invoiceable |
| **Historical record** | an existing mixed-currency invoice retains its financial outcome, with its economic amount recorded as historically unverified |

Applying the forward rule backwards would invalidate 941 settled invoices on the
strength of a rule written after they were issued. C2 and C4 forbid it, and so
does the governing principle: the blocking condition concerns a fact required to
act **now**, not the completeness of a historical record.

## 7. Approval proposition

**ARCHITECTURE.** A system-computed snapshot, written by the proposal path and
never by the caller, immutable once approved, bound to the approval, and
revalidated at execution.

**B6 — currency is part of the proposition, not context around it.** An approval
of "invoice order X" that does not bind the currency approves an amount without
a denomination. Execution refuses divergence in the currency exactly as it
refuses divergence in the total.

Fields: account and customer · order identity · **transaction currency** · subtotal ·
discounts · tax basis, jurisdiction and rate · tax amount · fees and shipping ·
total · invoiceability basis (the evidence reference) · authorising identity and
role · policy version · effective financial date.

**It must not live in `action_approvals.params`.** That field is caller-supplied
input; the proposition is what the system computed and a human approved. Storing
it there would let the proposer author the record that approval is bound to.

`policy_version` and `decision_mode` already exist and are populated on 12 of 78
approvals; they belong in the snapshot.

**Execution refuses divergence** in total, currency, tax basis, invoiceability
basis, or disposition. The existing `verification_failed` path carries the
outcome. No second verification framework.

## 8. AI and governance boundary

**ARCHITECTURE, implementing D8.** Authority is expressed as a declared policy
naming the action class, decision mode, approving role, monetary limits, and the
conditions permitting automatic execution — all of which
`governance_action_policies` already supports. A3 requires its own row; **A1's
must not be inherited**, because A1 authorises a primitive that cannot act
without a human supplying identifiers, while A3 determines which obligations
exist.

The A1 rule holds unchanged: an agent may not supply a financial amount. The
amount is computed server-side and that authority does not move.

**Insufficient today:** nothing compares the approved amount at execution (§7),
and no mechanism binds a disposition to the approval it was granted under.

**Blocked states surface through `governance_alerts`**, which already provides an
accountable owner, SLA, escalation, delegation and closure evidence. A second
exception queue would duplicate and drift.

## 7b. Historical remediation in the model (C10–C13)

**HISTORICAL REMEDIATION.** The two-axis model carries these decisions without a
new mechanism, which is the test of whether the model was right.

| Population | Axis A — financial disposition | Economic correctness | Remediation class |
|---|---|---|---|
| 941 paid | `invoiced_paid` — authoritative | `historically_unverified` | `PRESERVE_PAID_HISTORICALLY_UNVERIFIED` |
| 37 issued | `invoiced_outstanding` + hold | `historically_unverified` | `HOLD_PENDING_FINANCIAL_REVIEW` |
| 25 partial | `invoiced_outstanding` + hold | `historically_unverified` | `HOLD_PENDING_FINANCIAL_REVIEW` |

**Economic correctness is a third recorded fact, not a disposition.** Axis A says
what happened financially; Axis B says whether the fulfilment basis is
established; economic correctness says whether the amount can be shown to be
right. Collapsing the third into the first would make a paid invoice look
unpaid, and collapsing it into the second would make a currency question look
like a fulfilment question.

**The hold is a control, not a verdict.** `HOLD_PENDING_FINANCIAL_REVIEW`
prevents further consequential action on an unsettled transaction; it asserts
nothing about whether the amount is wrong.

**Nothing here is enforced by the system.** These classifications exist in an
analytical artefact outside the repository. Representing them in the database is
an implementation step that has not been authorised.

---

## 8b. Tax architecture under H1 and H2 (B7)

**Currency and tax are independent determinations, and the mixed-currency
finding does not touch tax semantics.**

| Element | Status |
|---|---|
| Product taxability | **POLICY (H1)** — standard treatment unless an explicit classification says otherwise; unestablished treatment blocks |
| Customer exemption | **POLICY (H2)** — absence of a valid, effective, jurisdictionally applicable exemption means not exempt |
| Tax jurisdiction | **ARCHITECTURE** — determined independently from the customer's relevant location and other legally relevant facts; not derived from currency |
| Applicable rate | **FACT** — `tax_rates`, jurisdictional and effective-dated, consulted only after jurisdiction and treatment are established |

A mixed-currency order is blocked on Axis A before tax is reached, so the two
conditions never interact. That ordering is deliberate: an order with an
undefined aggregate amount has no tax basis to determine, and computing one
would imply the amount was sound. **No tax schema is added in this phase.**

---

## 9. Representation gaps, reclassified

| Gap | Classification |
|---|---|
| Contract / milestone entity | **FUTURE — out of scope.** All 415 products carry stock; the catalogue is physical goods, so D2's services clause is inert today. The order is the commitment and the billing condition is expressible as a provenance rule. **Do not create the entity.** |
| Product taxability | **POLICY settled (H1), architecture decision required.** Whether the current catalogue is uniformly standard-rated is a business fact not held in the database; if it is, a policy statement suffices and no column is needed. |
| Account exemption | **POLICY settled (H2), no representation needed yet.** Absence is a known negative, so ordinary customers are not blocked. Representation is required only when a first exemption exists. |
| Approval proposition | **ARCHITECTURE DECISION REQUIRED** — form and storage. Necessity is not in question. |
| **Order transaction currency** | **POLICY SETTLED (C1, C8, C9).** One currency per order; mixed denomination blocks; FX deferred. Architecture in §5b. What remains is the historical population — a separate remediation decision, not an architecture gap. |

## 10. Reconciliation invariant

Every order answers both questions, and neither answer may substitute for the
other:

> **Financial:** what is the authoritative disposition?
> **Evidentiary:** what is known about the evidence for the basis that produced it?

> A financially authoritative outcome must not be invalidated because its
> historical provenance is incomplete.

> Missing historical provenance must not be manufactured into positive evidence.

## 11. Readiness

**A3 — NOT READY FOR IMPLEMENTATION — BUSINESS DECISION REQUIRED.**

The verdict changed category. Before this reconciliation the business questions
were settled and only architecture remained. §5 introduced a new one, and it is
larger than A3: **1,003 issued invoices, 941 of them paid, were computed by
adding two currencies.** A3 must not be implemented on an amount whose currency
is undefined for 44% of orders.

**The category has changed since the previous revision.** C1, C8 and C9 settle
the currency policy that §5 opened, so the forward rules are no longer waiting
on a business decision.

| Item | Classification |
|---|---|
| One currency per order; mixed blocks; FX deferred | **POLICY** — settled (C1, C8, C9) |
| Disposition and provenance vocabularies, and where each record lives | **ARCHITECTURE DECISION** |
| Approval snapshot storage form | **ARCHITECTURE DECISION** |
| Product taxability: column or policy statement | **ARCHITECTURE DECISION**, following H1 |
| Treatment of the 1,003 historical invoices | **HISTORICAL REMEDIATION** — undecided |
| Whether an authoritative historical FX basis exists | **FACT: none exists in the system.** Verified 2026-09-13: no FX table; `exchange_rate` is exactly 1.000000 on all 968 related payments; `base_amount` equals `amount` on 968 of 968; no external accounting reference on any of the 1,003 invoices |
| Multi-currency commercial transactions, FX conversion | **FUTURE** |
| Contract and milestone entity | **FUTURE** |

A3 is not implementation-ready while the historical remediation is undecided,
because an implementation would have to choose how existing records are
represented, and that choice is a financial decision rather than a technical
one.

---

## Related

- `docs/decision_a3_financial_truth.md` — the policy this implements.
- `app/core/route_exposure.py` — closed-by-default declaration, the same
  discipline applied to route exposure.
