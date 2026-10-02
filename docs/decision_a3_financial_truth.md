# Decision A-3 — the financial truth Conscestra enforces

**2026-09-13.** CEO/CFO business policy. Evidence baseline:
`docs/` A3 investigation series, production-verified read-only on 2026-09-12/13.

**This document is the business authority for all subsequent A3 technical
design.** It establishes what Conscestra considers an economic obligation, when
that obligation may be invoiced, and what authority an agent holds over
financial records. It is not an implementation specification, and it does not
describe the system as it stands.

---

## The governing principle

> Conscestra must never manufacture financial certainty from missing
> information. When financial truth is unknown, the system must preserve the
> unknown, explain why it is unknown, identify the responsible owner, and
> prevent an unauthorised financial consequence.

Ten distinctions follow from that principle and are part of the policy rather
than commentary on it:

| This | is not | this |
|---|---|---|
| a commercial obligation | | invoiceability |
| invoiceability | | an invoice |
| an invoice | | a payment |
| invoiceability | | revenue recognition |
| `completed` | | automatically invoiceable |
| unknown | | zero |
| unknown currency | | CAD |
| missing tax information | | zero tax |
| invoice cancellation | | extinguishment of the obligation |
| AI financial authority | | authority to set financial policy |

---

## A. Decision authority

These nine decisions are business policy set by the CEO and CFO. They are not
technical conclusions and were not derived from the current schema. Where the
system cannot represent a decision, the decision governs and the system is
incomplete, rather than the decision being adjusted to what the schema happens
to support.

Subsequent technical design cites this document. A design choice that
contradicts a decision here is a policy change and requires the same authority
that set it.

---

## B. The nine decisions

**D1 — When a customer obligation arises.** A customer obligation arises when
there is a valid commercial commitment between Conscestra and the customer,
typically an accepted order or contract, under which the customer is obligated
to pay for specified goods or services. An accepted commitment establishes the
economic obligation; fulfilment and revenue recognition are separate matters.

**D2 — When an obligation becomes invoiceable.** An obligation becomes
invoiceable when its contractual billing condition has been satisfied. For a
typical product order this is shipment or another contractually defined
fulfilment event. For services it is the contractual milestone or
service-completion condition. The system is expected to be able to state why a
particular obligation is invoiceable, in the form "invoiceable because goods
shipped on 2026-09-12" rather than "invoiceable because status is completed".

**D3 — The meaning of `completed`.** `completed` means the CRM considers the
operational lifecycle of the order complete. It does not by itself establish
invoiceability, payment obligation, or revenue recognition unless an applicable
commercial policy explicitly makes those equivalent. It is an operational state,
and invoiceability is a financial state; one field does not carry both.

**D4 — The economic effect of invoice cancellation.** Cancelling an invoice
voids or reverses the invoice document according to policy. It does not
automatically cancel the underlying customer obligation. Where the customer
still owes the amount, the correct sequence is void, then reverse or credit,
then reissue. Financial documents are never destructively deleted after
issuance. Every cancellation records a reason, an authorised actor, a timestamp,
the preserved original, the economic disposition, and any credit, reversal or
reissue relationship.

**D5 — Currency.** CAD is Conscestra's corporate and functional default
currency. Every financial transaction nevertheless carries an explicit
transaction currency. Where no other contractual currency has been established
for ordinary Canadian business, the transaction currency defaults to CAD. A null
currency never silently means CAD. Where the transaction currency differs from
the corporate currency, the transaction amount, the corporate amount, the
authoritative rate, the rate source, and the rate's effective date and time are
all recorded. An implicit rate of 1.0 is not used because a rate is missing.

**D6 — Tax determination.** Tax is determined from the applicable tax
jurisdiction and the taxability of the transaction, using the customer's
relevant location together with other legally relevant transaction facts.
`tax_rates` is the authoritative configured source of applicable rates once the
jurisdiction and tax treatment have been established. The rate applied must be
effective for the relevant transaction or invoice date. Tax determination is not
reducible to a lookup from a province or state alone.

**D7 — Missing required financial information.** Materially missing or ambiguous
financial information places the transaction into a governed blocked state until
it is resolved or explicitly authorised under a CFO-defined exception policy.
This applies at minimum to an unresolved currency, an unresolved tax
jurisdiction, an inapplicable or missing tax rate, an unsatisfied contractual
billing condition, a missing required approval, unestablished account ownership,
and an amount that cannot be determined deterministically. The only exception is
where the CFO has explicitly defined a legitimate zero or default business rule
for that case.

**D8 — AI financial authority.** An agent may calculate, reconcile, identify
discrepancies, prepare invoice and credit proposals, forecast, classify,
explain, and notify. An agent may execute automatically only where the action
falls within an explicitly governed policy, all required financial facts are
known, authorisation is already established, idempotency is guaranteed,
execution-time validation passes, and the action does not exceed its delegated
authority. Exceptional adjustments, material credits and write-offs, changes to
financial policy, overrides of tax or currency controls, actions outside
delegated limits, and ambiguous obligations require human approval. An agent may
operate the financial machinery; it may not create or alter financial policy.

**D9 — The approved financial proposition.** Approval applies to a specific
economic transaction rather than to an action type. The proposition comprises at
minimum the customer and account, the order or contract identity, the
transaction currency, the subtotal, discounts, the tax basis, the tax amount,
fees and shipping, the total, the invoiceable-event basis, the authorisation,
and the effective financial date. The proposition approved by the authorised
person must be materially identical to the proposition the system executes.
Approval of an invoice for one amount does not authorise execution at another,
even where the order has since changed.

---

## B2. Supplementary decisions H1–H3

Set 2026-09-13, after the CTO architecture review surfaced three questions the
first nine did not answer. They carry the same authority.

**H1 — Product and service taxability.** Every product and service has a defined
tax treatment for the jurisdictions in which it is sold. Products without a
specific exemption or special classification are treated according to the
applicable standard tax rule for the determined jurisdiction. A missing product
tax code does not become a permanent global rule that everything is taxable
everywhere: where the catalogue is not uniformly standard-rated, the system must
distinguish standard taxable, zero-rated, exempt, special treatment, and
unknown. **A product or service whose tax treatment cannot be established is
blocked from invoicing until it is resolved.**

**H2 — Tax exemption.** A customer is not tax-exempt unless a valid exemption has
been explicitly established and is effective for the transaction date and the
applicable jurisdiction. **Absence of an exemption record means not exempt.** An
invalid, expired, or jurisdictionally inapplicable exemption also means not
exempt. An exemption, once represented, carries its jurisdiction, type,
certificate or reference, effective and expiry dates, validating authority, and
evidence.

This is the one place where absence is knowledge rather than ignorance, and the
reason is stated rather than assumed: the business has defined absence as a
known negative. Treating it as unknown would block every ordinary taxable
customer, which would be a false application of the governing principle rather
than an instance of it.

**H3 — Historical financial truth.** A paid invoice remains financially
authoritative as a paid invoice. **Payment establishes the historical financial
outcome; it does not retroactively establish the fulfilment evidence that
originally made the obligation invoiceable.** Conscestra preserves the
distinction between the financial outcome and the provenance of the
invoiceability that produced it.

Two conclusions are therefore both rejected. *Paid, therefore fulfilment
occurred* is not supported by payment. *No surviving fulfilment event, therefore
the paid invoice is invalid* is not supported either. An invoice may truthfully
be recorded as paid while its original invoiceability is recorded as
historically unverified, and the two statements do not conflict.

### The principle this establishes

> A financial outcome and the evidence supporting its historical origin are
> separate truths. The financial state describes the economic position; the
> provenance describes how confidently the system can explain how that position
> arose. They are related, and they are not the same field.

---

## B3. Currency-integrity decisions C1–C9

Set 2026-09-13, after a reconciliation established that the system aggregates
monetary amounts of different denominations without a conversion basis. These
carry the same authority as D1–D9.

**C1 — One transaction currency per order.** An order is a single commercial
transaction and has one explicit transaction currency. Every monetary line must
be denominated in it. An order containing lines in more than one currency is not
invoiceable as represented. Multi-currency orders are not adopted at this stage.

**C2 — Historical amounts are never reinterpreted.** Original recorded monetary
values and audit history are preserved. Order totals, invoice subtotals, invoice
totals, payment amounts and balances are not silently recalculated. Today's rate
is not used to restate a past transaction, and neither CAD nor USD is assumed
for a value that did not declare one.

**C3 — Financial outcome and economic correctness are separate facts.** Each
affected invoice carries both independently: what happened financially, and
whether the amount invoiced can be established as economically correct. A paid
invoice is neither automatically correct nor automatically wrong.

**C4 — A paid invoice remains financially authoritative.** Payment establishes
the historical financial outcome. Affected invoices are not retrospectively
marked unpaid, blocked or invalid because the order contained mixed currencies.
Where appropriate they are recorded as paid with the economic amount unverified,
not as blocked.

**C5 — Outstanding affected invoices require review before further action.** No
automatic collection, modification, reissue, credit or write-off. They are
classified outstanding with the economic amount unverified, pending financial
review.

**C6 — Reconstruction uses historical evidence only.** Permitted: the original
product price and pricing currency, the order and invoice dates, order terms,
historical FX evidence where it exists, and authoritative external accounting
records. Prohibited: today's exchange rate, an assumption of CAD because the
company is Canadian, and the current product price. No historical financial fact
may be reconstructed from a value that did not exist or was not authoritative at
the transaction date.

**C7 — No blanket correction.** The affected population is classified before any
treatment: paid and verified, paid and unverified, outstanding and verified,
outstanding and unverified, cancelled, or demonstrably different. The paid
invoices are neither reopened merely for belonging to the population, nor
declared correct without evidence.

**C8 — Future mixed-currency orders are blocked.** An order cannot become
invoiceable unless every monetary component shares the order's explicit
transaction currency. An order of CAD 100 and USD 50 must not produce CAD 150.
It enters a blocked financial state with the reason `mixed_transaction_currency`
and a responsible owner.

**C9 — FX conversion is deferred.** Conscestra supports a CAD order and a USD
order, and does not support automatic conversion of a mixed-currency order. If
mixed-currency commercial transactions become genuinely necessary, that is a
separate CFO and CTO decision and must not be introduced as a workaround for
existing data.

### The recorded policy

> Conscestra shall maintain one explicit transaction currency per commercial
> order. Every monetary component of an order must be denominated in that
> currency before the order can become invoiceable. Conscestra shall never
> aggregate monetary values of different currencies without an explicitly
> authorised historical or current FX conversion basis.
>
> Existing historical invoices shall not be silently rewritten to conform to
> this policy. Their financial outcomes remain authoritative, while the economic
> correctness of affected amounts is assessed independently. Paid status
> establishes the historical payment outcome but does not establish that the
> original invoice amount was economically correct. Historical correction, where
> required, must use authoritative evidence applicable to the original
> transaction and must follow an explicit accounting correction process.

---

## B4. Historical remediation decisions C10–C13

Set 2026-09-13, governing the 1,003 invoices affected by mixed-currency
arithmetic. They authorise **classification only**.

**C10 — Paid invoices (941). Decided: P1.** Preserve the invoice and the payment
exactly as historically recorded, preserve the authoritative `paid` outcome, and
retain the economic amount status as `historically_unverified`. No FX conversion
is calculated or applied. No credit, refund, reissue, balance change or payment
change is made.

This does **not** certify the original economic amount as correct. It states
that the financial outcome is authoritative and known while economic correctness
remains historically unverified. **P1 is preservation pending evidence, not
permanent immunity from correction:** where authoritative external accounting
evidence is later obtained, a specific invoice may be reviewed under the
correction process.

**C11 — Issued and partial invoices (62). Decided: O1.** Place them in
`HOLD_PENDING_FINANCIAL_REVIEW` before any further consequential commercial
action. They are not to be automatically collected, reissued, credited,
refunded, cancelled, written off, or have balances altered.

The hold is a financial-control decision and is **not** evidence that the
invoice amount is incorrect. The asymmetry with C10 is deliberate: a settled
transaction has its outcome preserved, while an unsettled one has further
consequence prevented until the proposition is understood.

**C12 — Historical FX.** No historical FX conversion may be performed unless an
authoritative FX basis applicable to the original transaction is subsequently
established and explicitly accepted through the accounting process. A current
rate, estimated rate, average rate, market-history approximation,
payment-inferred rate, engineering-selected rate or fabricated rate is not an
acceptable substitute. Absence of an authoritative historical rate means
`economic_amount_status = historically_unverified`.

**C13 — Remediation vocabulary.**

| Class | Meaning |
|---|---|
| `PRESERVE_PAID_HISTORICALLY_UNVERIFIED` | paid outcome preserved; economic amount unverified |
| `HOLD_PENDING_FINANCIAL_REVIEW` | no further consequential action until reviewed |
| `CORRECTION_AUTHORIZED` | only after authoritative evidence and explicit accounting authorisation |
| `NO_REMEDIATION_REQUIRED` | only where authoritative review establishes no correction is needed |

Current classification: **941 → `PRESERVE_PAID_HISTORICALLY_UNVERIFIED`**,
**62 → `HOLD_PENDING_FINANCIAL_REVIEW`**. `CORRECTION_AUTHORIZED` is assigned to
none. `NO_REMEDIATION_REQUIRED` is never inferred from payment.

### The remediation boundary

Historical remediation is **policy-decided and not implemented**. These
decisions authorise classification only. They do not authorise database,
invoice, payment or accounting mutation, a credit note, a refund, a reissue, or
an FX conversion. An actual accounting correction requires a separate authorised
accounting action.

---

## C. Policy clarifications

Recorded because each has already been misread once, in this system or in the
analysis of it.

- CAD is the corporate and functional default currency, and a default is a
  defaulting rule rather than an accounting assumption. A null currency is
  unknown, not CAD.
- The customer's province, state or location is an important tax input. Tax
  determination is not reducible to a province-to-rate lookup.
- `tax_rates` is authoritative for the applicable configured rate once the
  jurisdiction and taxability have been established. It is not authoritative for
  whether a transaction is taxable.
- Missing tax information does not become zero tax.
- `completed` is an operational state and does not itself establish
  invoiceability.
- Cancelling an invoice does not extinguish the underlying obligation.
- Financial documents are not destructively deleted after issuance.
- An agent may not create or alter financial policy.
- Approval applies to the specific economic proposition, not merely to an action
  type.

---

## D. Representation gaps

**FACT, observed 2026-09-13 against production, read-only.** These are recorded
as the current state of the system, not as defects scheduled for repair. No
schema change is proposed by this document.

| Policy concept | Current representation |
|---|---|
| D1, D2 — contract, agreement, milestone, contractual billing condition | **None.** No contract, agreement, subscription or milestone entity exists. The order is presently the only representation of a commercial commitment, and the only available billing condition is a fulfilment event. |
| D6, H1 — taxability of the transaction | **None.** `products` carries no tax code and no taxable flag. H1 defines the policy; the representation is still absent, and a product whose treatment cannot be established is blocked. |
| D6, H2 — customer tax exemption, effective-dated | **None.** `accounts` carries no tax, exemption, currency, payment-terms or credit columns. H2 makes absence a known negative, so this gap does not block ordinary taxable customers; it blocks only the recording of an actual exemption. |
| D9 — the approved proposition | **Partial.** `action_approvals` records `amount` only. It holds no currency, subtotal, discount, tax basis or total. |

The absence of a schema object does not mean the business concept does not
exist. It means the system cannot presently express a concept the business has
decided is authoritative.

---

## E. Validation against current data

**FACT, measured 2026-09-13, read-only.** Applying D2 and D3 to the orders that
carry no live invoice:

| Status | Orders | With a qualifying fulfilment event | Disposition under this policy |
|---|---|---|---|
| pending | 28 | 0 | obligation exists, not yet invoiceable |
| processing | 21 | 0 | obligation exists, not yet invoiceable |
| ready | 19 | 0 | obligation exists, not yet invoiceable |
| completed | 2 | 0 | outstanding, blocked for want of a fulfilment basis |
| **Invoiceable today** | **0** | | |

**The previous `unbilled_orders` result of 5 does not represent invoiceable
revenue leakage under this policy.** Four of those five orders carry invoices
that are issued and paid; the fifth is the case below. The detector reported
them because it consulted a linkage table rather than a fulfilment event. The
detector is unchanged by this document.

**SO-2026-100352.** The order is `completed` and its only invoice, INV-000035,
is cancelled with its balance due retained. Under D4 the cancellation voids the
document and does not extinguish the obligation, so the amount remains
outstanding. Under D2 the order carries no qualifying fulfilment evidence, so it
is not invoiceable. Its correct disposition is **outstanding, blocked pending
fulfilment evidence**: it is neither revenue leakage nor a candidate for
automatic reissue.

### Two axes, not one

Under H3 a financial disposition and its fulfilment provenance are recorded
separately. An order does not become blocked merely because the evidence that
originally justified its invoice has not survived; that would allow an
incomplete event history to corrupt an authoritative financial record.

| Historical situation | Financial disposition | Fulfilment provenance |
|---|---|---|
| No invoice, no fulfilment evidence | not invoiceable, or blocked | unknown |
| Invoice outstanding, no fulfilment evidence | invoiced outstanding | historically unverified |
| Invoice paid, no fulfilment evidence | **invoiced paid** | historically unverified |
| Invoice present, fulfilment event recorded | invoiced | fulfilment established |
| Invoice cancelled | per D4 | provenance preserved |

**FACT, measured 2026-09-13:** 460 of 2,492 orders can receive fulfilment
evidence from the event stream. For the remaining 2,032 the original fulfilment
cannot be established from any surviving record, and most of those orders carry
invoices that are issued and paid. Under H3 they are recorded as paid with their
invoiceability provenance unverified. They are not blocked, and no fulfilment
event is inferred for them.

---

## F. The boundary between this policy and the system

The business policy is now established. The current system cannot yet represent
every concept that policy requires, as recorded in section D.

**A3 remains NOT READY FOR IMPLEMENTATION.**

Nothing in this document is enforced by the system today. It states what
Conscestra has decided, so that subsequent technical design implements a written
policy rather than a remembered one, and so that a later reader cannot mistake
an engineering convenience for a financial decision.

The technical design question this document hands to the CTO, and does not
answer, is what the smallest durable domain model is that can represent this
policy without treating the present incomplete schema as authoritative. That
review is a separate gate, and the design target it inherits is that **producing
a false positive financial obligation must be harder than producing a blocked or
unknown state.**

---

## Related

- `docs/decision_d01_retire_public_read.md` — the same discipline applied to
  anonymous customer-subject reads: the invariant satisfied by removing the
  exposure rather than by manufacturing certainty.
- `app/core/route_exposure.py` — closed-by-default declaration, the pattern this
  policy's blocked state follows.
