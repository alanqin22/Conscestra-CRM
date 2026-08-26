# Email Autosend — Remediation Design

**Status: DESIGN ONLY. Nothing implemented. No code, configuration, or production
data changed.**

Addresses two defects confirmed by the Production Email-Autosend Integrity Audit
(2026-08-13). Written against seven acceptance criteria set at the design gate.

---

## 1. The two defects, restated

**Defect A — authentication.** `a2a._invoke` calls the application's own
admin-gated `/email-chat` over an in-process ASGI transport and sends no
`X-Admin-Token`. `require_admin` (auth_dep.py:164) reads a normal request header;
`ASGITransport` does not bypass dependencies. Result: **403, in every
environment.** `/email-chat` is never entered, so this is not an SMTP, provider,
or generation problem — the path is blocked before the email operation begins.

**Defect B — false success.** `a2a.py:934` derives success from body-key absence:

```python
ok = (resp.get("success") is not False) and not resp.get("error")
```

HTTP status is never inspected. Observed 403 body `{"detail":"Admin authorization
required"}` carries neither key, so `ok=True` → `sent=True` → an activity reading
*"Payment reminder (urgent) sent – INV-…"*.

**A is an email-path defect. B is an audit-integrity defect.** B's blast radius
is every A2A intent, not only email: 400/401/403/404/409/422/429/500/502, empty
bodies, and non-JSON responses all read as success. Fixing A does not fix B —
with A fixed, every future timeout and 500 still records as sent.

---

## 2. Scope — the two defects are not the same size

Measured by introspecting the live route table against the capability registry:

```
capabilities registered            44   across 12 endpoints
endpoints behind an admin gate      2   /email-chat, /marketing/campaigns
capabilities behind an admin gate   3

Defect A (auth)            affects   3 of 44
Defect B (false success)   affects  ALL 44
```

Ten of twelve A2A endpoints are ungated and return 200 today. **Defect B silently
mis-reads failure for every capability in the system**, including the 41 that
Defect A never touches — a 500 from `/accounting-chat` reads as success exactly
the same way. Nobody had looked, because email was the capability being watched.

This asymmetry drives the sequencing below. Bundling a narrow fix with a broad
one makes the broad one wait.

---

## 3. Sequencing: D alone, then B

**D ships first and independently.** It is the audit-integrity fix, it is a
predicate change with a regression matrix, it protects all 44 capabilities, and
it does not depend on the transport. If B ever lands, D remains correct and
unchanged — that independence is the test of a good split.

**B follows, and is far smaller than first estimated.**

### The in-process path already exists

`Capability` already carries an optional direct-execution callable, and
`dispatch()` already prefers it over the HTTP hop:

```python
# a2a.py:93-96
# Optional STRUCTURED input contract: params → structured data, via the
# owning agent's SQL builder + SP (no NL parsing, no AI, no HTTP). When set,
# dispatch() prefers this for deterministic agent-to-agent data exchange.
sp: Optional[Callable[[Dict[str, Any]], Any]] = None

structured = cap.sp is not None and not req.prose     # a2a.py:849
data = await asyncio.to_thread(cap.sp, req.params)    # a2a.py:900
```

`email.send_payment_reminder` simply has no `sp=`, so it falls through to the
ASGI hop and meets the admin gate. **B is therefore not a refactor of capability
routing — it is applying a pattern the codebase already uses** to three
capabilities that were left on the HTTP path.

An earlier draft of this document described B as a large refactor. That was wrong;
the options were characterised before the registry was measured.

Re-measured 2026-08-14, it is smaller still: **two capabilities, not three.**
`campaign.winback` already carries `sp=_sp_campaign_winback`, so only
`email.send_payment_reminder` and `email.query` remain on the HTTP path.

### …and the pattern it applies was itself broken (D2)

**This is the finding that changes B.** `dispatch()`'s structured branch read:

```python
    if structured:
        try:
            data = await asyncio.to_thread(cap.sp, req.params)
        except Exception as exc:
            return A2AResult(False, ...)
        return A2AResult(True, req.intent, cap.agent, cid, data=data, ...)   # ← always True
```

`A2AResult(True, …)` unconditionally. The SP's **return value was never
inspected**, so an SP that refused by returning `{'ok': False}` dispatched as
`ACCEPTED`. It is the same defect as B/D — *absence of an error read as proof of
success* — wearing different clothes: an exception where the HTTP path had a
status code.

Measured across the 20 SPs registered on 2026-08-14, six already refuse this way
and were all being dispatched as success:

| SP | returns | dispatched as |
|---|---|---|
| `sms.send` | `{'ok': False, 'error': "unusable phone number"}` | `ACCEPTED` |
| `account.context` | `{'error': 'account_id required'}` | `ACCEPTED` |
| `contact.update_profile` | `{'ok': False, 'error': …}` | `ACCEPTED` |
| `scoring.activate` | `{'ok': False, 'error': …}` | `ACCEPTED` |
| `web.consult` | `{'error': …}` | `ACCEPTED` |
| `crm.simulate` | `{'ok': False, 'error': …}` | `ACCEPTED` |

**Consequence for sequencing: B could not have shipped as planned.** Giving
`email.send_payment_reminder` an `sp=` would have moved it *off* the path D
repaired and *onto* this one — restoring the original defect on the exact
capability it was found in, and `send_email`'s three refusal shapes
(`blocked` by the outbound guard, `skipped: unsubscribed` under CASL, and a
caught `SMTPAuthenticationError`) all return `success: False` rather than
raising, so all three would have recorded as sent.

`classify_sp_result()` now applies the same doctrine to the structured path.
Two refusal conventions are honoured — `success` (email) and `ok` (sms,
`contact.update_profile`, `scoring.activate`, the `data.*` family) — and only
the **singular** `error` key counts, because `crm.plan` returns a plural
`errors` list that is empty on success. Anything completing without raising and
without a refusal signal stays `ACCEPTED`, so read capabilities returning bare
lists and row dicts are unaffected.

**D2 is a prerequisite of B, not part of it.** Like D it stands alone, protects
all 20 structured capabilities rather than the two being moved, and is correct
whether or not B ever lands.

### Why B over A

| | A — authenticate `_invoke` | B — `sp=` direct callable |
|---|---|---|
| New credential | **yes** — scoped internal token + `require_admin` change | **none** |
| Auth surface | grows | unchanged |
| Follows existing pattern | no | **yes, already in use** |
| HTTP hop | retained, authenticated | removed |
| Risk of spreading | credential-passing propagates | none |

And a correction to the framing that made A look reasonable: **the HTTP boundary
here is not a trust boundary.** The A2A caller runs in the same process and can
already import and call anything. The admin gate exists to stop *external*
callers. Authenticating an in-process call therefore prevents no escalation — it
performs a check that protects nothing, and charges a new credential for it. B
removes the ceremony instead of paying for it.

---

## 4. D — structured outcome, never a bare boolean

```
ACCEPTED  provider took the message and returned an identifier
REJECTED  the request was refused
FAILED    transport or server error — the request did not complete
UNKNOWN   response unparseable, or completed with no usable signal
```

`UNKNOWN` is not a synonym for either success or failure. It exists precisely so
that "we cannot tell" stops being silently coerced into "it worked."

---

## 5. Acceptance criteria — how each is met

**1. No A2A caller receives an unnecessarily broad admin credential.**
Option B removes the credential requirement instead of distributing it. No caller
gains admin. Under the A+C fallback, a *scoped* internal credential would be
required — never `ADMIN_API_TOKEN` itself — and that is strictly worse on least
privilege, which is why B is preferred.

**2. Every transport failure is failure or unknown, never implicit success.**
Success requires a positive signal. The predicate inverts: the default is
`FAILED`, and only an explicit provider acceptance promotes it. Absence of an
error field is no longer evidence of anything.

**3. `sent` is not recorded without explicit positive evidence.**
The activity writer takes the outcome, not a boolean. Only `ACCEPTED` may produce
send-language. `FAILED`/`UNKNOWN`/`REJECTED` still record the attempt — the
demand signal is preserved — but in language that does not claim transmission.

**4. The system distinguishes attempted, accepted, sent, delivered.**
Today it can distinguish none of them; it records "attempted" as "sent." The
strongest evidence available in-process is **provider acceptance**, so:

| State | Evidence available | Recordable today? |
|---|---|---|
| attempted | the dispatch ran | yes |
| accepted | provider returned a message id | yes, once captured |
| sent | SMTP handoff completed | approximately, via acceptance |
| **delivered** | **none in-process** | **no — must not be claimed** |

Delivery is unknowable without provider webhooks or bounce processing. The
vocabulary must stop at *accepted*. `"Payment reminder … sent"` overstates even a
correct outcome; wording is a small follow-on decision, not a blocker.

**5. Historical false positives are not silently rewritten.**
No `UPDATE` to the 25 activities. They are a true record of what the system
*recorded*, and rewriting them to say `failed` would substitute one fabricated
history for another — the same reasoning that leaves the two empty migration
checksums empty and declines to backfill `updated_at`.

**6. The 25 remain identifiable but are not transmission evidence.**
Recorded here as a durable finding, with the exact predicate:

```sql
-- Activities recorded as sent by the defective path. NOT evidence of transmission.
SELECT * FROM activities
 WHERE subject ILIKE 'Payment reminder%sent%'
   AND created_at BETWEEN '2026-06-26 18:25' AND '2026-06-26 18:28';
-- n = 25
```

Independent check against the `info@` BCC archive for 26–27 June 2026: **two
messages total, neither a payment reminder; zero payment reminders in the archive
at any date before 2026-07-02.** Caveat preserved: BCC configuration on that date
is not independently proven, though the archive was demonstrably capturing other
traffic in the same window.

    recorded as sent      : 25   confirmed
    actually transmitted  :  0   no independent evidence found
    delivered             :  —   unestablished, and moot

**Classification: historical false-positive audit records — CONFIRMED.**

**7. Regression test proves failure cannot produce `sent=True`.**
Table-driven, asserting the inverse of the audit's truth table:

| Input condition | Required outcome | May record send-language? |
|---|---|---|
| provider accepts + message id | `ACCEPTED` | **yes** |
| provider explicitly refuses | `REJECTED` | no |
| **401 / 403** | **`REJECTED`** — decided; see note | no |
| 404 | `FAILED` | no |
| 429 | `FAILED` | no |
| 500 / 502 | `FAILED` | no |
| timeout / transport raise | `UNKNOWN` | no |
| malformed / non-JSON body | `UNKNOWN` | no |
| empty body `{}` | `UNKNOWN` | no |
| no provider response | `UNKNOWN` | no |

Two rows carry the most weight: `ACCEPTED` proves success is still reachable, and
`timeout` proves an exception is not silently swallowed into success.

**Note on 401/403 → `REJECTED`.** Decided by the owner. The alternative argument,
recorded because it has a downstream consequence: `REJECTED` most naturally reads
as *the provider evaluated the message and declined it* — a bad address,
suppression, policy — which is **not retryable**. A 401/403 never reached the
provider and **is** retryable once the credential is corrected. If retry logic is
added later it must therefore key off something other than the outcome enum
alone, because `REJECTED` will contain both non-retryable and retryable cases.

**Permanent invariant:**

> There must be no execution path in which absence of an error is sufficient
> evidence of success.

---

## 7. Design principle

> **A system must never use its own unverified success record as proof that the
> operation succeeded. An activity record generated by the same failure path is
> not independent evidence of success.**

This is not email-specific. The same shape has now appeared four times in this
codebase:

| Control | How it failed | How it looked |
|---|---|---|
| CEO briefing | guard blocked the send | `results` counted it; nothing read `results` |
| Approval links | `APP_URL` defaulted to localhost | buttons rendered, mail sent, recipient clicked |
| Migration check | CLI ignores ordering | `note: "schema is current"` beside `ok: false` |
| Email autosend | 403 before send | activity says `sent` |

In each case the control's failure mode was indistinguishable from success **in
the field a human reads**. Independent evidence — the `info@` BCC archive, the
recipient's inbox, the ledger's `applied_at` — existed in every case and was
consulted by no code.

---

## 7a. B — implemented 2026-08-14

`email.send_payment_reminder` now carries
`sp=_sp_email_send_payment_reminder` → `app/agents/email/structured.py`.
Dispatch takes the structured branch, the ASGI hop is not performed, and the
403 is unreachable. Verified live: `outcome=rejected, status=None` — a `None`
status is the proof that no HTTP call happened at all.

**Scope corrected again: one capability, not two.** `email.query` is registered
as a *"natural-language passthrough to the email agent"*. Prose IS its contract,
and `sp=` means "no NL parsing, no AI" — the wrong shape for it. It keeps the
HTTP path and therefore still meets the 403, which since D is reported honestly
as `rejected`. Nothing calls it today. Confirmed the only `prose=True` caller
(`orchestrator/router.py`) resolves `/email-chat` to `email.query`, never to the
reminder, so the reminder always takes the structured path.

**The gate moved to where it cannot be skipped.** The admin gate answered "is
this caller an admin", which for an in-process peer protects nothing. The
question that matters for outbound mail is "may we email THIS PERSON", and it is
now answered inside the capability, from the database:

| gate | question | owner |
|---|---|---|
| `AGENT_BUS_AUTOSEND` | should the robot act unattended? | the CALLER |
| `is_email_verified` | may we email this person at all? | the CAPABILITY |

Deliberately not merged: a human approving one reminder should not be blocked
because the automatic loop is off, and the automatic loop must never reach an
address nobody confirmed. Previously only `agent_bus` checked, so a governance
approval, the planner or an MCP client could have sent to an unverified seed
address. The lookup uses `bool_and`, so a duplicate contact row cannot
manufacture consent, and an address absent from `contacts` is refused outright.

**Composition is deterministic** — no LLM. A dunning notice asserts a debt, and
the wording of a debt claim is not something to regenerate per send. Tone
escalates by age (gentle <30d, firm 30–59d, urgent ≥60d), matching
`agent_bus._compose_reminder` so an approved reminder and an automatic one read
identically.

**Transactional, not commercial** (`commercial=False`, as the order-confirmation
path already does). CASL's commercial path appends an unsubscribe link, and
"unsubscribe from invoice reminders" is not a choice this system should offer.

**Tests:** `tests/test_email_send_sp.py`, 23 cases. Mutation-verified — removing
`sp=` fails 13 of them, including a tripwire that fails if `_invoke` is called at
all. Every one of `send_email`'s three refusal shapes (outbound guard, CASL
opt-out, SMTP error) is asserted to classify as `REJECTED`; all three returned
`ACCEPTED` before D2.

**Local state after B:** 0 verified contacts and 12 overdue invoices, so every
local dunning run still drafts and sends nothing. B makes sending *possible*,
not automatic — the recipient gate is what decides.

## 8. Not in scope

Implementation, wording of activity subjects,
provider webhook ingestion for true delivery confirmation, and any change to the
25 historical records. Each requires its own authorization.
