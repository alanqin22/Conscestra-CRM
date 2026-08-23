# Limited Employee Email Notifications — Design & Audit

**Status: DESIGN + AUDIT ONLY. Nothing implemented. No code, schema, configuration
or production data changed by this document.**

Audit performed 2026-08-22 against the working tree on
`feat/order-status-self-service-restore` and the local database.

---

## 0. Product principle (adopted)

> **Notifications describe what happened.
> Worklists describe what needs attention.
> Email signals what cannot safely wait.**

Email is not a second copy of the notification system. It is a scarce attention
channel with a per-recipient budget, and spending it is a governed decision with
an audit trail.

---

## 1. AUDIT — what the codebase actually contains

The prior report was inspected claim by claim. Seven claims are confirmed, three
are corrected, and the audit surfaced **seven findings the report did not
contain, two of which are blocking.**

### 1.1 Confirmed

| # | Claim | Evidence |
|---|---|---|
| C1 | In-app volume makes 1:1 mirroring unviable | 7,956 `in_app` rows; peak 1,728/day; 8 human employees at 486–713 per 7 days |
| C2 | The feed is dominated by ambient machine events | 7d top titles: `payment_created` 1,134, `invoice_created` 1,080, `invoice_paid` 999, `opportunity.stage_changed` 756 |
| C3 | `metadata.kind` is unset on essentially everything | 7,209 of 7,301 `in_app` rows in 7 days have no `kind` |
| C4 | Only 4 identities are authorized to receive work | `assignable_identity` = 4 rows, all executives (`ceo@`, `cfo@`, `coo@`, `cro@agentorc.ca`) |
| C5 | The 8 `@emp.agentorc.ca` staff are the never-granted demo cohort | `assignable.py:7`, `escalation.py:406`; 0 of 8 present in `assignable_identity` |
| C6 | `executives.preferred_channel` + `auto_email_enabled` already drive fan-out | `governance.py:672-681`; all 4 rows are `preferred_channel='all'`, `auto_email_enabled=true` |
| C7 | An email-specific ledger is required, and a proven template exists | `order_notifications` (22 columns, `UNIQUE(order_id, event_type)`, states `queued→attempted→accepted/failed/skipped`) |

### 1.2 Corrected

**D1 — "You currently have no classifier." Incorrect.**
`notification_triage.classify()` (line 120) already implements a three-way
taxonomy that maps 1:1 onto the requested tiers:

```python
CRITICAL_TYPES   = {"supervisor.alert"}                      # → Tier 1 INTERRUPT
ACTIONABLE_TYPES = {"activity.overdue_flagged",              # → Tier 2 WORKLIST
                    "invoice.overdue", "invoice_overdue",
                    "lead.scored"}
# everything else                                            # → Tier 3 AMBIENT
```

It is default-informational, which is the correct fail-safe direction (an
unclassified event defaults to *no email*). **The taxonomy is not missing — its
result is never persisted.** `classify()` is called only inside triage passes, is
derived by joining `events` on `notifications.event_uuid`, and is discarded. Do
not invent a fourth taxonomy; persist this one.

**D2 — "Pass B's digest row is the natural email body." Incorrect, and the
inversion matters.** `_upsert_digest()` (line 219) does produce exactly one row
per `(recipient, UTC day)` — but its content is a rollup of **Tier 3 ambient**
events, which is precisely what a worklist digest must exclude:

```
🧹 Notification digest (115 updates)
  - invoice_created × 92
  - payment_created × 23
```

Pass B is a *noise suppressor*, and it is load-bearing as one. The Tier 2
worklist digest is a **different query over a different set** (Pass C's
still-actionable residue, which Pass B deliberately leaves unread). Reuse Pass
B's per-recipient-per-day *keying discipline*; do not reuse its *content*.

**D3 — "Escalation email needs to be built." Incorrect. It is ~80% built.**
`escalation._email_escalation()` (line 390) already:

- ships dark behind `ESCALATION_EMAIL=0`;
- routes to a **role mailbox** (`ESCALATION_EMAIL_TO=support@agentorc.ca`), with
  the reasoning for preferring a role over a person written into the docstring;
- gates on a curated `_EMAIL_REASONS` set (5 of 13 reasons), deliberately
  decoupled from priority — the comment at line 361 records that nesting the two
  gates silently dropped two reasons, and that tests, not review, caught it;
- writes human-actionable subjects (`[Action needed] Caller could not be
  verified — call them back (order_cancel_unverified)`);
- records `email_state` / `email_to` / `email_detail` onto the escalation, and
  uses `accepted`, never `sent`;
- is covered by 12 tests in `tests/test_escalation_email_routing.py`, including
  `test_a_successful_cancellation_emails_nobody`.

This is the Tier 1 mechanism. The design below extends it rather than replacing it.

### 1.3 New findings

**F1 — BLOCKING. The notification recipient key is an ambiguous identity space.**

`notifications.employee_uuid` is not a foreign key and resolves against three
different tables. One UUID resolves to two different people:

```
a1451ad6-310c-4bcc-ba17-dd383a881ee8
  → employees : julia.martin@emp.agentorc.ca   (staff)
  → owners    : john.smith@example.com         (customer contact)
```

That UUID carries **486 notifications in the last 7 days**. Any implementation of
`notification.employee_uuid → email address` can email a customer content written
for staff. `assignable.identity_space()` (line 129) exists specifically because of
this collision and checks *every* space rather than first-match-wins.

**F2 — BLOCKING. `assigned_to` cannot be a routing key.** Both
`escalations.assigned_to` and `conversations.assigned_to` are free `text` with no
constraint and no identity discipline. Live contents:

```
escalations.assigned_to    : 'charlie.nguyen@example.com' (8)   ← a CUSTOMER contact
                             'Alan Qin' (8)                     ← display name
                             'agent' (16)                       ← a literal
                             'ghost@example.invalid' (4)        ← test fixture
                             'nobody@example.invalid' (4)       ← test fixture
                             'alan' (4)                         ← username
conversations.assigned_to  : 'alan' (24)
```

Emailing "the assignee" today would send to a customer and to two deliberately
invalid addresses. The report's recommendation ("email should follow the
assignment") is correct in principle and **not implementable against this column**
without an identity gate in front of it.

**F3 — The backlog would detonate on flag-flip.** There are **134 live
escalations and every one of them is SLA-breached.** 118 are
`customer_requested_human`, which is in `_EMAIL_REASONS`. Historical open rate is
bursty, not steady:

```
2026-08-12 : 102 escalations opened,  93 of emailable reason
2026-08-01 :  34 escalations opened,  31 of emailable reason
2026-08-21 :   4 escalations opened,   4 of emailable reason
```

A per-escalation unclaimed-reminder loop, switched on without a backlog cutoff,
sends 134 emails on its first tick. The 93-in-one-day burst is the concrete case
the circuit breaker in §G.1 is sized against — it is measured, not hypothetical.

**F4 — Dead schema that is a trap, not a shortcut.** Three artifacts already
exist, unused, and two of them are the *wrong* thing to reuse:

| Artifact | State | Verdict |
|---|---|---|
| `notification_recipients.emailed_at` | 0 of 16,433 populated, 0 code references | **REJECT.** Using it makes the mutable, triage-GC'd notification row the delivery ledger — exactly what the requirement forbids |
| `notification_recipients.email_status` | same | **REJECT**, same reason |
| `notification_channels(employee_uuid, channel, is_enabled, config)` | 0 rows, 0 code references | **REJECT as-is.** Shape is right, key is wrong — `employee_uuid` is the ambiguous space of F1 |

Also relevant: `notifications` is a **VIEW** over `notification_messages` ×
`notification_recipients`, written through three `INSTEAD OF` triggers. Any
column added for tiering must go on a base table.

**F5 — Internal email shares the customer sender identity and the archive.**
`smtp_imap.py` has one sender (`EMAIL_ADDRESS=info@agentorc.ca`, or `RESEND_FROM`)
and BCCs **every** message to `EMAIL_BCC=info@agentorc.ca`. Consequences: staff
email consumes the same domain reputation as order confirmations and OTP mail, and
each staff email also lands in the info@ archive — so the archive that serves as
independent delivery evidence is the same mailbox a volume mistake would flood.

**F6 — `takeover()` has no compare-and-swap.** `agent_console._set_handling()`
(line 208) updates `WHERE conversation_id=%s AND status='open'` with no predicate
on `handling` or `assigned_to`. Two reps clicking "Take over" both succeed; the
second silently overwrites the first, and both are told `ok: True`. Contrast
`order_notifications.acquire()` (line 692), whose docstring records that this
exact defect produced *"8 concurrent notify() calls for one business event →
8 emails and 1 ledger row."* The console has the bug the ledger already fixed.

**F7 — `APP_URL=http://localhost:8000` locally.** The autosend remediation
document already records this failure mode in production: *"Approval links —
`APP_URL` defaulted to localhost; buttons rendered, mail sent, recipient
clicked."* Any deep link into the console inherits it.

---

## A. Executive decision

**The limited-email strategy is ACCEPTED**, with the scope narrowed by the audit:

1. Email must never be derived from a notification row. Confirmed and adopted.
2. Tier 1 is largely built (D3). The work is **recipient discipline, idempotency
   and safety**, not composition.
3. Tier 2 digest is new work and cannot reuse Pass B's body (D2).
4. **Recipient expansion beyond the 4 assignable executives is out of scope for
   this design** and is gated behind F1/F2 being resolved.

---

## B. Email eligibility matrix

Tier names map onto the existing `notification_triage.classify()` output; the
"Persisted as" column is what a future reader will actually see in the database.

| Event | Tier | Persisted as | In-app | Individual email | Digest | Recipient rule | Rationale |
|---|---|---|---|---|---|---|---|
| `approval.routed` (assigned approval) | 1 | `critical` | ✅ | ✅ immediate | ❌ | Assigned executive from `executives`, via `preferred_channel` | Already live (`GOV_ROUTE_EMAIL=1`); a person must click Approve/Reject. Median 3/day |
| `supervisor.alert` | 1 | `critical` | ✅ | ✅ immediate | ❌ | Role mailbox | Already `CRITICAL_TYPES` |
| Escalation, reason ∈ `_EMAIL_REASONS` | 1 | `critical` | ✅ | ✅ immediate | ❌ | **Role mailbox only** (F2) | Already built; ships dark |
| Escalation unclaimed past threshold | 1 | `critical` | ✅ | ✅ **once**, then cooldown | ❌ | Role mailbox | New. The real failure mode: queue items aged 3h/22h/2d/2d/2d |
| Escalation, reason ∉ `_EMAIL_REASONS` | 2 | `actionable` | ✅ | ❌ | ✅ | Assignable owner | An in-app queue item; the console is where it lives |
| `invoice.overdue` / `invoice_overdue` | 2 | `actionable` | ✅ | ❌ | ✅ | Assignable owner | Already `ACTIONABLE_TYPES` |
| `activity.overdue_flagged` | 2 | `actionable` | ✅ | ❌ | ✅ | Assignable owner | Already `ACTIONABLE_TYPES` |
| `lead.scored` | 2 | `actionable` | ✅ | ❌ | ✅ | Assignable owner | Already `ACTIONABLE_TYPES` |
| `invoice_created` / `invoice.created` | 3 | `informational` | ✅ | ❌ | ❌ | — | 1,080 in 7 days |
| `payment_created` / `payment.received` | 3 | `informational` | ✅ | ❌ | ❌ | — | 1,134 in 7 days |
| `invoice_paid` | 3 | `informational` | ✅ | ❌ | ❌ | — | 999 in 7 days |
| `opportunity.stage_changed` / `.created` | 3 | `informational` | ✅ | ❌ | ❌ | — | 998 in 7 days |
| `order.status_changed`, `activity.completed`, all `*.created`/`*.updated` | 3 | `informational` | ✅ | ❌ | ❌ | — | Machine record |
| **Any event not explicitly listed** | **3** | `informational` | ✅ | ❌ | ❌ | — | **Default is no email.** A new event type cannot acquire email by being added |
| Successful automated action (e.g. AI order cancellation) | 3 | `informational` | ✅ | ❌ | ❌ | — | Explicitly protected by `test_a_successful_cancellation_emails_nobody` |

The last two rows are the ones to defend in review. Default-deny is what stops a
future feature from silently becoming an email broadcaster.

---

## C. Recipient model

### C.1 The rule

```
authorized recipient  :=  assignable_identity.is_active
                          AND email_preference allows this tier
                          AND a role mailbox when no person resolves
```

Never `employees.email`. Never `notifications.employee_uuid → email`. Never
`assigned_to` without passing it through `assignable.resolve()`.

### C.2 Resolution order

```
1. Is there an explicit assignee?
     escalations.assigned_to / conversations.assigned_to (free text — F2)
        → assignable.resolve(value)          # email OR uuid, active only
        → hit?  → that owner_id is the recipient
        → miss? → DISCARD the value entirely and continue.
                  A miss must never fall through to the raw string.
2. Is this an approval with a routed executive?
        → executives.assigned_executive_id  (already implemented)
3. Otherwise
        → the ROLE MAILBOX (ESCALATION_EMAIL_TO)
```

Step 1's discard is the whole safety property. Applied to today's live data, all
six distinct `escalations.assigned_to` values miss (`charlie.nguyen@example.com`
is in `owners`, not `assignable_identity`), so **every live escalation routes to
the role mailbox** — which is the correct and honest answer, and identical to
today's behaviour.

### C.3 Current authorized universe

```
ceo@agentorc.ca   Alan Qin      owner_id db6a9f31…   preferred_channel=all  auto_email=true
cfo@agentorc.ca   Sherman Zhang owner_id 4dde342c…   preferred_channel=all  auto_email=true
coo@agentorc.ca   Yongmei Qin   owner_id e4d99e38…   preferred_channel=all  auto_email=true
cro@agentorc.ca   Daping Qin    owner_id 23f8579b…   preferred_channel=all  auto_email=true
+ role mailbox    support@agentorc.ca                (not an identity; a destination)
```

**Four people and one role mailbox.** The eight `@emp.agentorc.ca` mailboxes do
not appear here and must not be added by this work. Adding them is a `grant()`
call — a deliberate, separate, admin operation — and it is blocked on F1.

---

## D. Email lifecycle

```
event
  │
  ├─ classify()                         persist tier on notification_messages
  │                                     Tier 3 → STOP (no ledger row, no cost)
  ▼
resolve responsible identity            §C.2; miss → role mailbox
  │                                     unauthorized → STOP, log reason
  ▼
check preference                        email_preference(owner_id)
  │                                     disabled / in_app → STOP, log reason
  ▼
check urgency                           Tier 1 → now.  Tier 2 → defer to digest
  │
  ▼
deduplicate                             idempotency_key; already terminal → STOP
  │
  ▼
claim(idempotency_key)                  INSERT … ON CONFLICT DO NOTHING
  │                                     ── ledger row exists BEFORE any send ──
  ▼
acquire(email_id)                       compare-and-swap → state='attempted'
  │                                     lost the race → STOP (someone else has it)
  ▼
circuit breaker + rate limit            §G.1; tripped → mark_skipped, alert
  │
  ▼
mark_attempted()                        attempts+1, written BEFORE the provider call
  │
  ▼
send_email(commercial=False)            outbound guard runs here
  │
  ▼
classify_send_result()                  ACCEPTED / REJECTED / FAILED / UNKNOWN
  │
  ▼
mark_accepted / mark_failed / mark_skipped
                                        vocabulary STOPS at 'accepted'
```

Every `STOP` writes its reason. That is what makes "why was this *not* emailed?"
answerable — the requirement that is easiest to lose.

---

## E. Escalation state machine

Existing states are `open → assigned → resolved` (`escalations.status`), with
`sla_due_at` as the clock. Email is a **projection over** that machine, never a
participant in it.

```
                    ┌──────────────────────────────────────────┐
                    │  open()  — idempotent per conversation   │
                    │  re-ask folds in, raises priority only   │
                    └───────────────────┬──────────────────────┘
                                        │
                 reason ∈ _EMAIL_REASONS│      reason ∉ _EMAIL_REASONS
                          ▼             │                  ▼
                  ┌───────────────┐     │            ┌──────────────┐
                  │ TIER 1 EMAIL  │     │            │ in-app only  │
                  │ ledger: open  │     │            │ → Tier 2     │
                  └───────┬───────┘     │            └──────────────┘
                          │
   ┌──────────────────────┴───────────────────────────────────┐
   │                        status = open                     │
   │   unclaimed past REMIND_AT_FRACTION × SLA?               │
   │        → ONE reminder, cooldown, max REMIND_MAX          │
   └──────────────────────┬───────────────────────────────────┘
                          │
        takeover / assign │                    resolve
                          ▼                        ▼
                 ┌─────────────────┐      ┌──────────────────┐
                 │ status=assigned │      │ status=resolved  │
                 │ REMINDERS STOP  │      │ REMINDERS STOP   │
                 │ (first thing    │      │ ledger row is    │
                 │  the job checks)│      │  historical      │
                 └─────────────────┘      └──────────────────┘
```

### E.1 Race conditions, and what each resolves to

| Race | Resolution |
|---|---|
| **Email generated as someone claims it** | Unavoidable and **acceptable by design**, because the email is a *pointer*, not a copy. It says "open the console"; the console shows `assigned_to`. The reminder job re-reads `status` inside the same transaction that claims the ledger row, so the window is one statement wide, not one job-run wide |
| **Two employees claim the same item** | **Currently broken (F6).** `_set_handling` must gain `AND (handling='ai' OR assigned_to IS NULL OR assigned_to=%s)` so the second caller gets `ok:False, "already taken by X"`. This is a **prerequisite**, not a follow-up: without it, "claiming stops the reminder" is a promise the console cannot keep |
| **Reassignment after email generation** | The email named no person (role mailbox) or named a resolved owner and linked to live state. Reassignment changes the console; the link still resolves correctly. No stale-content problem because there is no copied content |
| **Resolution before delivery** | Ledger row reaches `accepted`; escalation reaches `resolved`. Both true, no contradiction — the ledger records *we sent*, not *it was needed*. The reminder job's next tick sees `resolved` and stops |
| **Stale links** | Deep links carry only `escalation_id` / `conversation_id`, never state. Guard `APP_URL` (F7): refuse to emit a link when `APP_URL` contains `localhost` and the process is not local |
| **Repeated reminders** | Bounded three ways: cooldown, `REMIND_MAX`, and a terminal ledger row per `(escalation_id, reminder_ordinal)` |
| **Burst of simultaneous escalations** | The 102-in-one-day case (F3). Per-recipient rate limit collapses to a single "N escalations need attention" summary; the global breaker is the backstop |

---

## F. Digest design

**One email per recipient per day, and only when it would say something.**

### F.1 Included

Tier 2 items that are, at digest time, **still actionable and still theirs**:

- `invoice.overdue` / `invoice_overdue`, `activity.overdue_flagged`, `lead.scored`
  — i.e. `ACTIONABLE_TYPES`, re-validated by triage Pass C's existing liveness
  checks so a paid invoice never appears;
- live escalations assigned to the recipient with reasons outside `_EMAIL_REASONS`;
- for each: one line — priority, what to do, and a link to the record or console.

### F.2 Excluded

- Everything Tier 3. Explicitly including Pass B's ambient rollup (D2).
- Anything already emailed as Tier 1 today — the ledger is the check, so the
  digest never restates an interrupt.
- Anything resolved between generation and send.

### F.3 Skip rule

```
if not items:  no email, no ledger row, one log line.
```

An empty digest is worse than no digest: it trains the recipient that the sender
is noise. This is the single most important line in the digest design.

### F.4 Relationship to Pass B

Pass B keeps running unchanged and keeps suppressing ambient noise in-app. The
digest job reads the residue Pass B *deliberately leaves unread*. They are
complementary; neither is rebuilt.

---

## G. Safety controls

### G.1 Rate limiting and circuit breaker — defensible defaults

Derived from measured volume, not chosen for roundness:

| Measurement | Value |
|---|---|
| Approvals created per day (median, 14d) | 3 |
| Approvals created per day (max, 14d) | 20 |
| Escalations of emailable reason, typical day | 2–4 |
| Escalations of emailable reason, worst measured day | **93** (2026-08-12) |
| Authorized recipients | 4 + 1 role mailbox |
| In-app rows/day at peak | 1,728 |

```
STAFF_EMAIL_ENABLED                  0     master kill switch (ships dark)
STAFF_EMAIL_APPLY                    0     1 = actually send; else decide-and-log
STAFF_EMAIL_MAX_PER_RECIPIENT_HOUR   6
STAFF_EMAIL_MAX_PER_RECIPIENT_DAY    12
STAFF_EMAIL_BREAKER_PER_HOUR         25    global, all recipients
STAFF_EMAIL_BREAKER_PER_DAY          60    global, all recipients
STAFF_EMAIL_REMIND_AT_FRACTION       0.5   of SLA elapsed before first reminder
STAFF_EMAIL_REMIND_COOLDOWN_MIN      60
STAFF_EMAIL_REMIND_MAX               2
STAFF_EMAIL_BACKLOG_CUTOFF_HOURS     24    never remind about anything older
```

Why these numbers hold:

- `6/recipient/hour` is **2× the busiest measured normal day**, so a real day
  never touches it, while the 93-burst is clipped at 6.
- `25/hour` global is above any legitimate simultaneous load across 5
  destinations, and **the 2026-08-12 burst trips it at message 25 of 93** —
  the breaker is sized against a real event.
- `60/day` global is 4× the busiest legitimate day. Reaching it means a
  classifier or routing defect, which is the definition of a trip.
- `BACKLOG_CUTOFF_HOURS=24` is what stops the 134 breached escalations (F3) from
  emailing on the first tick. **All 134 are older than 24h, so the first run
  sends zero.** New escalations flow normally.

The breaker is **DB-backed, not in-process** — counted from the ledger — because
`leader.py` gives one scheduler leader but HTTP replicas can send too, and
`rate_limit.py` is explicitly documented as per-instance ("which only makes
limits more generous, never stricter"). That is the wrong direction for this.

**Trip behaviour:** stop sending; write `mark_skipped(reason='breaker')` for every
suppressed message so nothing is lost silently; raise ONE `supervisor.alert`; do
not auto-reset — require an operator or a rolling-window expiry. A breaker that
resets itself hides the incident that tripped it.

### G.2 Deduplication

`idempotency_key` is deterministic and content-free:

```
approval          approval:{approval_uuid}
escalation open   escalation:{escalation_id}:open
escalation remind escalation:{escalation_id}:remind:{ordinal}
digest            digest:{owner_id}:{utc_date}
```

`UNIQUE(idempotency_key)` on the ledger. A repeat of the same business event
collides and is a no-op — the guarantee `claim()` already provides for orders.

### G.3 Recipient authorization

One choke point: a single `resolve_recipient()` that returns
`(owner_id | None, email, why)`. Nothing else in the codebase may compute a staff
email address. Enforced by a test that greps for `send_email(` inside the module
and fails on any call not routed through the ledger.

### G.4 Preference checks

See §H.2. Missing preference = **no email** (fail-closed), not "all".

### G.5 Environment / job singleton

The digest and reminder jobs register through the existing scheduler, which
`leader.py` already gates. Two further protections: the ledger's unique key makes
a double-run a no-op even if two leaders ever coexist, and `STAFF_EMAIL_APPLY`
stays 0 in the local environment so only Railway sends — the same topology
already used for briefings.

### G.6 Audit logging

Every decision, send and non-send is a ledger row or a structured log line with
`decision`, `reason`, `tier`, `recipient`, `idempotency_key`. Both governance
questions — *why was this sent to this person* and *why was this valid
notification not emailed* — must be answerable from the database alone.

### G.7 Delivery-state correctness

Adopt the vocabulary already ratified in `docs/email_autosend_remediation_design.md`:

| State | Meaning | Recordable |
|---|---|---|
| `queued` | claimed, nothing attempted | yes |
| `attempted` | provider call in flight (written before the call) | yes |
| `accepted` | provider returned acceptance / message id | yes |
| `failed` | transport or server error | yes |
| `skipped` | deliberate refusal (preference, breaker, guard, unauthorized) | yes |
| `rejected` | provider evaluated and declined | yes |
| **`delivered`** | **no in-process evidence exists** | **NO — must not be claimed** |

The permanent invariant carries over verbatim:

> There must be no execution path in which absence of an error is sufficient
> evidence of success.

Independent verification uses the `info@` BCC archive, not `success: true`.
Note F5: staff email lands in that archive too, so the archive query must filter
by subject prefix to stay usable as evidence.

---

## H. Schema changes

Four changes. All additive; none touch the `notifications` view or its triggers.

### H.1 `notification_messages.tier` — persist the classification

```sql
ALTER TABLE notification_messages
  ADD COLUMN tier text
    CHECK (tier IN ('critical','actionable','informational'));
COMMENT ON COLUMN notification_messages.tier IS
  'Email eligibility class, from notification_triage.classify(). '
  'NULL and informational both mean: never email. '
  'A notification row is NEVER sufficient authority to send email — '
  'see docs/employee_email_notifications_design.md';
```

On `notification_messages` (content), not `notification_recipients` (fan-out):
tier is a property of *what happened*, eligibility is a property of *who*.
Nullable, no backfill — 7,301 existing rows stay NULL and therefore never email.

### H.2 `assignable_identity` — preferences on the authorized identity

```sql
ALTER TABLE assignable_identity
  ADD COLUMN preferred_channel   text    NOT NULL DEFAULT 'in_app',
  ADD COLUMN auto_email_enabled  boolean NOT NULL DEFAULT false;
```

Column names deliberately match `executives`, so there is one vocabulary. The
defaults are the inverse of `executives`' (`'all'` / `true`) because **granting
someone assignability must not silently grant them email.** One resolver:

```
email_preference(owner_id):
    executives row for this owner_id?   → use it        (the 4 today)
    else assignable_identity row?       → use it        (future grants)
    else                                → {'in_app', auto_email: false}
```

| Case | Tier 1 | Tier 2 digest |
|---|---|---|
| `auto_email_enabled=false` | ❌ in-app only | ❌ |
| `preferred_channel='in_app'` | ❌ | ❌ |
| `preferred_channel='email'` | ✅ | ✅ |
| `preferred_channel='all'` | ✅ | ✅ |
| `preferred_channel='slack'/'teams'` | chat + in-app; no email | ❌ |
| No preference row | ❌ **fail closed** | ❌ |
| Not in `assignable_identity` | ❌ **role mailbox instead** | ❌ |
| Unassigned escalation | ❌ per-person; role mailbox after threshold | ❌ |

`notification_channels` is **not** used and should be dropped or commented as
superseded, so a future developer does not resurrect an `employee_uuid`-keyed
preference path (F1).

### H.3 `staff_email_ledger` — the new table

Modeled directly on `order_notifications`, which has 461 rows of production
service and a documented CAS.

```sql
CREATE TABLE staff_email_ledger (
    email_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key     text        NOT NULL,
    email_kind          text        NOT NULL,   -- approval|escalation|escalation_remind|digest
    tier                text        NOT NULL,   -- critical|actionable
    recipient_owner_id  uuid,                   -- NULL ⇒ role mailbox
    recipient_email     text        NOT NULL,
    recipient_kind      text        NOT NULL,   -- assignable|executive|role_mailbox
    subject_ref_type    text,                   -- escalation|approval|digest
    subject_ref_id      uuid,
    state               text        NOT NULL DEFAULT 'queued',
    decision_reason     text,                   -- why sent, or why NOT
    provider            text,
    provider_message_id text,
    provider_response   text,
    failure_reason      text,
    attempts            integer     NOT NULL DEFAULT 0,
    first_attempted_at  timestamptz,
    last_attempted_at   timestamptz,
    accepted_at         timestamptz,
    event_uuid          uuid,
    correlation_id      uuid,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uq_staff_email_idem ON staff_email_ledger (idempotency_key);
CREATE INDEX ix_staff_email_state      ON staff_email_ledger (state, last_attempted_at);
CREATE INDEX ix_staff_email_breaker    ON staff_email_ledger (accepted_at)
                                        WHERE state = 'accepted';
CREATE INDEX ix_staff_email_recipient  ON staff_email_ledger (recipient_email, created_at DESC);
```

`ix_staff_email_breaker` is partial so the breaker's trailing-window count stays
an index-only scan as the table grows.

**Why a new table rather than `notification_recipients.emailed_at`:** the
requirement is that email idempotency survive independently of the notifications
table. `notification_recipients` rows are read, digested, marked and (via
retention Pass F) deleted by triage. Storing delivery state there means a GC pass
can erase the proof that an email was sent, and the next run sends it again.
Separate table, separate lifecycle.

**Crash after provider acceptance, before commit.** The `attempted` state is
written *before* the provider call, so a crashed process leaves `state='attempted'`
with a stale `last_attempted_at`. `acquire()` will not reclaim it for
`ATTEMPT_LEASE` (15 minutes). After that, reclaim is possible and a duplicate is
*possible but bounded* — a known, accepted residual risk, identical to the one the
order path carries. Reducing it further needs provider-webhook reconciliation,
which is explicitly out of scope here.

### H.4 `agent_console` compare-and-swap (F6)

No schema change; a `WHERE` clause change in `_set_handling()`. Listed here
because it is a **prerequisite**, not an enhancement.

---

## I. Files and modules that change

| File | Change | Size |
|---|---|---|
| `app/core/notification_triage.py` | export `classify()` as the shared tier authority; persist tier at write time | small |
| **`app/core/staff_email.py`** *(new)* | ledger + `resolve_recipient()` + preference + breaker + `claim/acquire/mark_*`. **The only module permitted to email staff** | ~400 lines |
| `app/core/assignable.py` | `email_preference()`; expose prefs in `directory()`/`grant()` | small |
| `app/core/escalation.py` | route `_email_escalation` through the ledger; add `unclaimed_reminders()` | medium |
| `app/core/governance.py` | route the existing approval email through the ledger (behaviour unchanged, evidence gained) | small |
| `app/core/agent_console.py` | CAS in `_set_handling()` (F6) | small |
| `app/main.py` | two jobs: `staff_email_digest` (daily), `escalation_reminders` (hourly) | small |
| `sql/staff_email_ledger.sql` *(new)* | H.1–H.3 | — |
| `tests/test_staff_email_*.py` *(new)* | §J | — |

`app/agents/email/smtp_imap.py` is **unchanged**. The outbound guard, CASL
handling (`commercial=False` — internal operational mail, no unsubscribe link)
and the BCC archive all work as-is.

---

## J. Testing strategy

| # | Test | Asserts |
|---|---|---|
| 1 | Tier 1 approval emails the routed executive | one ledger row, `accepted`, correct recipient |
| 2 | Tier 1 escalation of an emailable reason emails the role mailbox | existing 12 tests still pass |
| 3 | Tier 2 never emails individually | `invoice.overdue` → 0 ledger rows, appears in digest |
| 4 | Tier 3 never emails | all four high-volume types → 0 rows |
| 5 | **Unknown event type defaults to no email** | the default-deny guarantee |
| 6 | Unauthorized employee is never a recipient | `julia.martin@emp.agentorc.ca` and her colliding UUID both yield role mailbox, never `john.smith@example.com` (**F1 regression test**) |
| 7 | `assigned_to='charlie.nguyen@example.com'` does not email a customer | miss → discard → role mailbox (**F2 regression test**) |
| 8 | `auto_email_enabled=false` blocks a Tier 1 interrupt | preference outranks urgency |
| 9 | Missing preference row → no email | fail-closed |
| 10 | Duplicate business event → one email | unique key collision is a no-op |
| 11 | Retry after `failed` re-sends exactly once | `acquire()` reclaims `failed` |
| 12 | Crash after acceptance does not immediately resend | `attempted` + fresh lease → `acquire()` returns None |
| 13 | Claim **before** send suppresses the reminder | assigned → no email |
| 14 | Claim **immediately after** send does not retract | ledger stays `accepted`; next tick sends nothing |
| 15 | Reassignment does not generate a second email | |
| 16 | Resolution before delivery leaves an honest record | `accepted` + `resolved` coexist |
| 17 | **Two takeovers: exactly one wins** (F6) | second gets `ok:False` |
| 18 | 93 simultaneous escalations → breaker trips at 25 | remainder `skipped`, one `supervisor.alert`, replayed against real 2026-08-12 data |
| 19 | Per-recipient hourly cap collapses a burst | 7th message in an hour is `skipped` |
| 20 | Empty digest sends nothing | 0 ledger rows |
| 21 | Backlog cutoff: 134 breached escalations → 0 emails on first run | **the flag-flip safety test** |
| 22 | `send_email` is unreachable except through the ledger | source-level guard (mutation: delete the ledger call → test fails) |
| 23 | `APP_URL=localhost` emits no deep link | F7 |

Tests 6, 7, 17, 21 and 22 are the load-bearing ones. Each must be
**mutation-verified**: remove the protection and confirm the test fails, following
the precedent set by `tests/test_email_send_sp.py` (23 cases, "removing `sp=`
fails 13 of them").

---

## K. Migration and rollout

Smallest safe path. Each stage is independently valuable and independently
revertible; **no stage sends email until stage 4.**

| Stage | Change | Flags | Exit criterion |
|---|---|---|---|
| **0** ✅ | `agent_console` CAS (F6) — **DONE 2026-08-22** | none | Two-takeover test passes. `tests/test_console_takeover_race.py`, 11 cases, mutation-verified. Full suite 1837 passed |
| **1** ✅ | `sql/staff_email_ledger.sql` (H.1–H.3); `staff_email.py` **decide-only, no send** — **DONE (local) 2026-08-22** | `STAFF_EMAIL_ENABLED=0` | Local applied, 58 tests, 4 mutations verified. **Railway apply is the owner's step — see §K.1** |
| **2** ✅ | Persist `tier`; observe every eligibility decision — **DONE (local) 2026-08-22** | `ENABLED=1, APPLY=0` | Machinery live and observing. **The 7-day window starts now** — verdict read from `/staff-email/observations`. See §K.2 |
| **3** ✅ | Route the *existing* approval + escalation emails through the ledger — **DONE (local) 2026-08-22** | unchanged (`GOV_ROUTE_EMAIL=1`, `ESCALATION_EMAIL=0`) | Volume identical; ledger rows now exist, and a repeated route no longer double-emails. See §K.3 |
| **4** ⚙ | Digest, executives only (4 recipients) — **BUILT (local) 2026-08-22, `APPLY` still 0** | `APPLY=1`, digest only | **Not yet flipped.** Gated on Stage 2's 7 days and on a recipient reading one. See §K.4 |
| **5** | One interrupt category: escalation-unclaimed, role mailbox only | `ESCALATION_EMAIL=1` + backlog cutoff | Emails/day in single digits; median email→claim time measured |
| **6** | Measure. Expand only on evidence | — | §L metrics reviewed before any widening |

**Never in this sequence:** granting `@emp.agentorc.ca` staff assignability. That
is a separate authorization, blocked on F1, and it is the change that would
multiply the recipient universe from 4 to 12.

---

## L. Success metrics

Delivery volume is explicitly **not** a success metric.

| Metric | Target | Source |
|---|---|---|
| Emails per recipient per day | **≤ 2** | ledger |
| Share of staff emails that are Tier 1 | 100% (digest counted separately) | ledger |
| Median time from email → claim | < 30 min | ledger `accepted_at` vs `escalations.assigned_at` |
| Emailed escalations still unclaimed at SLA | ↓ from today's **100%** (134/134) | escalations |
| Duplicate-email rate | **0** | unique key violations |
| In-app rows correctly classified non-email | > 99.9% | tier distribution |
| Opt-out rate | 0 | `auto_email_enabled` transitions |
| Customer-email reputation impact | none | bounce/complaint rate on `info@` |
| Incidents caused by classifier/routing error | **0** | breaker trips |

Baseline to beat, measured today: **134 live escalations, 134 breached, 0 emails
sent.** The system currently has perfect email hygiene and zero attention. The
target is a small number of emails and a measurable drop in that 134.

---

## M. Risks to resolve before coding

All nine were investigated before coding on 2026-08-22. Severities below are
**post-investigation**; §M.1 records what changed and why.

| # | Risk | Severity | Resolution — DECIDED |
|---|---|---|---|
| R1 | UUID collision (F1) lets a customer receive staff mail | ~~BLOCKING~~ → **HIGH** | **Quarantine, do not repair.** `resolve_recipient()` refuses any uuid with `collision: true` → role mailbox. Test 6 |
| R2 | `assigned_to` free text (F2) routes to customers/invalid addresses | **BLOCKING** | **Add a typed `assigned_owner_id uuid`** beside the text label, copying `action_approvals`. Discard-on-miss covers the transition. Test 7 |
| R3 | 134 breached escalations detonate on flag-flip (F3) | **HIGH** | `BACKLOG_CUTOFF_HOURS=24`. Test 21 |
| R4 | No CAS on takeover (F6) | ~~HIGH~~ → **RESOLVED** | Stage 0 shipped 2026-08-22 — see §K.0 |
| R5 | Staff email shares customer sender reputation (F5) | ~~MEDIUM~~ → **CLOSED** | **The separated sender already exists.** Railway sends as `info@mail.agentorc.ca` via Resend — SPF + DKIM verified and aligned. Nothing to build. See §M.1 |
| F8 | Sender identity is split by environment (local SMTP vs Railway Resend) | **MEDIUM** | Not a defect — a testing constraint. **Stage 4 acceptance must be measured on Railway against the BCC archive, never locally** |
| R6 | `emp.agentorc.ca` may be a catch-all to one mailbox | ~~MEDIUM~~ → **CLOSED** | **Confirmed a catch-all, by design.** Reinforces the existing NO-GO; no new work |
| R7 | `APP_URL=localhost` (F7) | **MEDIUM** | Refuse to emit links when unresolvable. Test 23 |
| R8 | Crash between provider acceptance and commit | **LOW** | Bounded by `ATTEMPT_LEASE`; residual risk accepted, same as orders |
| R9 | Staff mail floods the `info@` BCC archive | **LOW** | Subject-prefix filter for evidence queries |

**No open owner decisions remain.** R5 and R6 were resolved by evidence rather
than by preference.

---

## M.1 Risk investigation, 2026-08-22

### R1 — the collision is real, bounded, and must not be repaired

**Blast radius.** The colliding uuid appears in roughly **500 rows across 25
tables** — `invoices.updated_by` 86, `payments.updated_by` 36,
`invoices.owner_id` 33, `products.created_by` 32, `opportunities.owner_id` 24,
and so on. Repair means deciding, per column, which of the two people was meant.
**The database does not record that**, so every decision would be a guess.

**Severity re-rated down, on evidence.** `identity_space()` classifies the
`owners` side as **`legacy_owner`, not `customer_contact`** — there is no shared
`contact_id` and no matching contact email. And `john.smith@example.com` sits on
`example.com`, which is RFC 2606 reserved *and* already listed in
`agent_bus._PLACEHOLDER_EMAIL_DOMAINS`. **The address is undeliverable by
construction.** The concrete harm — a real customer receiving staff mail —
cannot occur today.

**Population sweep.** Across all 14 distinct in-app notification recipients in
30 days, there is **exactly one** collision:

```
  3107  demo_employee   collision=False  admin@system.internal
   728  demo_employee   collision=False  sarah.johnson@emp.agentorc.ca
   633  demo_employee   collision=False  robert.garcia@emp.agentorc.ca
   567  demo_employee   collision=False  mike.chen@emp.agentorc.ca
   502  demo_employee   collision=False  karen.patel@emp.agentorc.ca
   502  demo_employee   collision=False  sophia.nguyen@emp.agentorc.ca
   502  demo_employee   collision=False  lisa.jones@emp.agentorc.ca
   502  demo_employee   collision=False  daniel.lee@emp.agentorc.ca
   502  legacy_owner    collision=True   john.smith@example.com,
                                         julia.martin@emp.agentorc.ca
    43  assignable      collision=False  ceo@agentorc.ca
    25  assignable      collision=False  cro@agentorc.ca
    21  assignable      collision=False  cfo@agentorc.ca
    12  assignable      collision=False  coo@agentorc.ca
     4  demo_employee   collision=False  agent.orchestrator@system.internal
```

**The four authorized recipients receive 101 of 7,650 in-app notifications —
1.3%.** The notification system is almost entirely addressed to identities that
cannot receive email. This is the strongest single argument that mirroring in-app
to email was never a coherent option.

**Decision: quarantine, do not repair.** Three reasons, in order of weight:

1. The codebase already set this precedent —
   `test_33_the_unresolvable_rows_are_quarantined_not_repaired` — and the
   autosend remediation applied the same reasoning to its 25 false-positive
   activities: *"rewriting them would substitute one fabricated history for
   another."*
2. Repair needs ~500 semantic decisions no evidence supports.
3. The email risk closes at the **routing** layer, not the data layer, and one
   line does it.

**Implementation trap — write this into the module.** `identity_space()` returns
a top-level `space` key that picks **one** of the colliding spaces (here,
`legacy_owner`). Code branching on `.get("space")` receives a single, confident,
wrong answer. **Only `collision` is safe to branch on.** Detection already exists
and is already tested (`test_22b`); what is missing is a consumer that honours it.

### R2 — the fix already exists one table over

`assigned_to` has exactly four writers, and only two real entry points:
`agent_console._set_handling()` and `escalation.assign()` /
`assign_for_conversation()` (both reached from console takeover).

The fourth writer is the answer. `governance.py:685` writes `assigned_to` on
**`action_approvals`**, which already carries *both*:

```
action_approvals.assigned_executive_id   uuid    ← what routing reads
action_approvals.assigned_to             text    ← what a human reads
```

**Decision: copy that shape.** Add `assigned_owner_id uuid` to `escalations` and
`conversations`; keep `assigned_to` as a display label with a `COMMENT` marking
it explicitly non-authoritative. Routing reads only the typed column.

Why this beats discard-on-miss alone: discard-on-miss makes email *safe* but
leaves person-level routing permanently impossible, freezing stage 5 at
role-mailbox-only forever. The typed column is what eventually unfreezes it —
about 15 lines across two writers. Discard-on-miss stays as the runtime guard
during the transition, and as the permanent guard for legacy rows.

### R5 — resolved: the separated sending identity already exists

**A first pass of this section raised an SPF alarm. It was wrong, and the
correction matters** — it changes the decision from "build separation" to "use
the separation you already have."

The first pass probed `send.agentorc.ca` and `resend._domainkey.agentorc.ca`,
found nothing, and concluded Resend was unauthorized. Those were the wrong names.
A negative DNS answer disproves only the name asked about.

**What production actually sends** — from the `info@` BCC archive, which is the
authoritative evidence this codebase's own doctrine names:

```
From:         Conscestra CRM <info@mail.agentorc.ca>
Return-Path:  …@send.mail.agentorc.ca
via:          smtp-out.amazonses.com          (Resend runs on SES)
```

**The verification records, confirmed present and complete:**

```
send.mail.agentorc.ca            TXT   v=spf1 include:amazonses.com ~all
                                 MX    feedback-smtp.us-east-1.amazonses.com
resend._domainkey.mail.…         TXT   p=MIGfMA0GCSqGSIb3DQEB…    valid DKIM
_dmarc.mail.agentorc.ca          —     none; inherits p=none from the org domain
```

`RESEND_FROM` on Railway is `info@mail.agentorc.ca`, and **`mail.agentorc.ca` is
a fully verified Resend sending domain.** SPF passes, DKIM passes, and both align
under the organizational domain. Confirmed end-to-end on Railway: **391 order
notifications accepted with `provider='resend'`, zero failures**, most recent
2026-08-22.

**Decision: R5 is closed. Nothing to build, nothing to decide.** The separate
sending subdomain this risk asked about already exists and already carries all
production traffic. Staff email rides the same warmed identity.

Volume context, retained because it justifies not adding a *third* identity:
customer order email runs at **40–75 accepted sends/day**; staff email at its
design target is **≤10/day**, a 13–20% addition to an already-separated sender.

### F8 — the sender identity is split by environment

Discovered while resolving R5. One codebase, two senders:

| | From | Path | Authentication |
|---|---|---|---|
| **Local** | `info@agentorc.ca` | cPanel SMTP → CanSpace | SPF via `51.161.117.187`, DKIM `default._domainkey` |
| **Railway** | `info@mail.agentorc.ca` | Resend → SES | SPF via `include:amazonses.com`, DKIM `resend._domainkey` |

Both align correctly, so neither is broken. The consequence for this design is a
**testing constraint, not a defect**:

> A local send test proves nothing about the production From header, DKIM
> alignment or deliverability. Stage 4's acceptance criterion must be measured
> **on Railway, against the BCC archive** — never locally.

This is the same split-by-environment trap `_send_via_resend` already names in
its own comment about RFC 3834 headers: *"Railway sends via Resend, so omitting
this here would leave the loop guard working on local (SMTP) and not in
production."*

### DMARC — separate work, now demonstrably low-risk

`_dmarc.agentorc.ca` is `p=none` — monitoring only — and the subdomain inherits
it (no `sp=`, no `_dmarc.mail.agentorc.ca`). Moving to `p=quarantine` is what
actually protects the domain.

The evidence gathered here makes that change safe: **both sending paths produce
aligned SPF *and* aligned DKIM**, so the usual reason to stay on `p=none` — an
unknown unaligned sender somewhere — does not apply. Still out of scope for this
design, but the blocker to doing it is now known to be absent.

### R6 — closed, by the repository's own migration

`sql/employee_emails_to_emp_subdomain.sql` (2026-08-20) states the arrangement
outright:

> "A subdomain we own, **with a catch-all landing in emp@agentorc.ca**, so an
> agent can email them, the mail is inspectable, and no stranger receives
> anything. Mirrors the seed.agentorc.ca arrangement for synthetic customers
> exactly."

DNS confirms the shape:

```
emp.agentorc.ca   MX   — none —
                  A    51.161.117.187          (the same cPanel host)
                  TXT  v=spf1 +a +mx +ip4:51.161.117.187 ~all
```

No MX means delivery falls back to the A record (RFC 5321 §5.1) — the same host
— where the catch-all routes everything to `emp@`.

**The eight employees therefore do not have personal inboxes.** They have
addresses that all land in one shared inspection mailbox, created deliberately as
a *containment device* so a future employee-email feature could be built and
tested without mailing strangers — the previous `@company.com` addresses were a
real domain belonging to somebody else.

**Decision: R6 is closed, and it closes as a reinforcement of the existing
NO-GO.** Emailing the eight today would deliver eight people's account data into
one shared mailbox. That is an audience-boundary problem, not a deliverability
one, and the migration's author anticipated it.

Making those addresses real would require: per-person cPanel mailboxes, an MX
record for `emp.agentorc.ca`, and then a `grant()` per person. Until all three
exist, stages 0–4 target the four executives and nothing about this plan changes.

---

## N. GO / NO-GO

### GO — for stages 0 through 4

Scope: console CAS, the ledger, persisted tiering, routing the two *already
existing* email paths through the ledger, and a digest to the four authorized
executives.

Justification: the classification taxonomy already exists (D1), the Tier 1
mechanism already exists and already ships dark (D3), the ledger pattern is
proven in production (C7), the recipient universe is four people who already
receive email and already have preferences (C6), and every stage before 4 sends
nothing.

### CONDITIONAL GO — stage 5

Escalation-unclaimed reminders to the **role mailbox only**, conditional on:
R3 (backlog cutoff) implemented and tested, R4 (CAS) shipped in stage 0, and
stage 4 having run 7 days with ≤2 emails/recipient/day.

### NO-GO — until separately authorized

1. **Any email to an `@emp.agentorc.ca` address.** Blocked on R1 and R6. Requires
   an explicit `grant()`.
2. **Any email routed from `assigned_to` without `assignable.resolve()`.** Blocked
   on R2.
3. **Any per-person escalation email.** No trustworthy person-level routing key
   exists; the role mailbox is the honest answer until ownership is real.
4. **Any path from a notification row directly to an email.** Permanently.

### The principle to encode

> A notification row is never sufficient authority to send an email.

It belongs in three places so it cannot be bypassed by accident: the
`COMMENT ON COLUMN notification_messages.tier`, the module docstring of
`staff_email.py`, and test 22 — which fails if any code path reaches `send_email`
for a staff recipient without a ledger row.

---

## O. Not in scope

Provider webhook ingestion for true delivery confirmation; a separate sending
subdomain; repairing the `employees`/`owners` UUID collision; granting
assignability to any additional person; Slack/Teams delivery of these same tiers;
and any change to the 134 existing escalations. Each requires its own
authorization.

---

## K.0 Stage 0 — shipped 2026-08-22

**Change.** `agent_console._set_handling()` claims a conversation with a
compare-and-swap instead of a bare `UPDATE`:

```sql
WHERE conversation_id = %(cid)s::uuid AND status='open'
  AND (handling <> 'human' OR assigned_to IS NULL OR assigned_to = %(who)s)
```

The three arms are each load-bearing: nobody holds it; a pre-migration row is
flagged `human` with no holder and must stay claimable; and re-taking your own
conversation must remain idempotent for a double-click or a page refresh.
`assigned_at` moves only when the holder actually changes, so a re-click does
not rewind "how long has a human had this".

**A lost claim is now distinguishable from a dead one.** `{ok: False,
conflict: True, assigned_to: <winner>}` for a conversation someone else holds;
the original "not found or already closed" for everything else. Collapsing the
two is how a rep chases a colleague who never had it.

**Release is deliberately NOT swapped.** Handing work back to the AI is a
recovery path; requiring the original holder would strand a conversation whose
rep has gone home. Recorded as `test_11` so the asymmetry is a decision rather
than an oversight.

**Verification.**

| | |
|---|---|
| New tests | `tests/test_console_takeover_race.py` — 11 cases, all passing |
| Mutation A | Remove the whole CAS predicate → **3 failed, 8 passed** (tests 02, 03, 04) |
| Mutation B | Remove only the `assigned_to IS NULL` arm → **1 failed, 10 passed** (test 10) |
| Regression | Full suite **1837 passed, 1 skipped** (pre-existing skip) |
| Orphans | 0 stray conversations, messages, escalations or cases after the fixture runs |

**Files.** `app/core/agent_console.py` (+62/-4) is the only committable change.
`tests/test_console_takeover_race.py` and the `agent-console.html` conflict
branch are under gitignored paths (`/tests/`, `*.html`) and stay local by the
repository's existing convention — the HTML needs the owner's own deploy.

**UI.** `agent-console.html`'s takeover handler gains a conflict branch: a
colleague getting there first is not an error, so it names the holder and
refreshes the pane instead of reporting "could not take over" beside an
unchanged view.

---

## K.1 Stage 1 — shipped locally 2026-08-22

**Decide only. There is no send path, and a test enforces that.**

### What was added

| File | What |
|---|---|
| `sql/staff_email_ledger.sql` | H.1–H.3, applied **locally only** |
| `app/core/staff_email.py` | tier → recipient → preference → budget → ledger. ~560 lines |
| `app/core/assignable.py` | `email_preference()` — one read path over two tables |
| `app/main.py` | two read-only admin routes |
| `tests/test_staff_email_stage1.py` | 58 cases |

### The three decisions worth reviewing

**1. `decide()` refuses in cost order, and a Tier 3 refusal writes nothing.**
The cheap certain refusals come first, so an ambient event costs one dictionary
lookup and never touches the database — `ledgerable: False`, and the recipient
is not even looked up. At 1,728 notifications on a peak day, a `skipped` row per
ambient event *is* the volume problem restated in a different table.

**2. `resolve_recipient()` is the only door, and it discards rather than falls
back.** Verified against live data:

```
assignee = a1451ad6-… (collision)      → role_mailbox  "identity collision; refused"
assignee = charlie.nguyen@example.com  → role_mailbox  "not in assignable_identity; discarded"
assignee = ghost@example.invalid       → role_mailbox
assignee = 'agent' / 'alan' / 'Alan Qin' → role_mailbox
assignee = ceo@agentorc.ca             → assignable    owner_id db6a9f31-…
assignee = none                        → role_mailbox  "ownership is not recorded"
```

Every distinct value currently in `escalations.assigned_to` misses — so every
live escalation routes to the role mailbox, which is both correct and identical
to today's behaviour.

**3. `'delivered'` is absent from the ledger's `state` CHECK, deliberately.**
The vocabulary stops at `accepted`, and the constraint is what stops a future
writer upgrading it without adding webhook ingestion first. `test_67` asserts
the database itself refuses the word.

### Verification

| Check | Result |
|---|---|
| New tests | `tests/test_staff_email_stage1.py` — **58 passing** |
| Mutation 1 — remove the collision refusal | **test_20 fails** |
| Mutation 2 — use an unresolved assignee raw | **6 tests fail** (test_21, test_22 ×5) |
| Mutation 3 — add a `send_email` import | **test_60 fails** |
| Mutation 4 — check the breaker before the per-recipient cap | **test_72 fails** |
| Full regression | **1895 passed, 1 skipped** (was 1837 — +58, no change) |
| Ledger rows left by tests | 0 |
| Callers of `staff_email` outside the module | **only `main.py`'s router include** |

`test_60` is parsed as an AST, not grepped: a substring search flags the
module's own docstring — which discusses `send_email` by name — and a test that
fails on its subject being *described* teaches people to weaken it.

### Zero behaviour change, demonstrated

- `STAFF_EMAIL_ENABLED` defaults to `0`; `decide()` refuses everything with
  `"STAFF_EMAIL_ENABLED=0"`.
- Nothing in the codebase calls `decide()`. The module is reachable only through
  two read-only admin routes, and `/staff-email/explain` justifies a decision
  **without taking it** (`test_81` asserts it writes no ledger row).
- `notification_messages.tier` is nullable and **not backfilled** — 7,301
  existing rows stay NULL, and NULL means never email.
- `assignable_identity` defaults are `in_app` / `false`, the inverse of
  `executives`', so the migration granted nobody anything.

### ⚠ Hand-off — the owner's step

`sql/staff_email_ledger.sql` is applied to **local only**. It is **deliberately
not declared** in `deploy_state.REQUIRED_MIGRATIONS`, following that list's own
rule:

> "This list means 'the schema must have this', so declaring an unapplied,
> unauthorized migration turns `migrate --check` red for a decision nobody has
> taken. Add the line in the same change that applies the migration, not before."

To complete Stage 1 on Railway:

```
python -m scripts.apply_sql sql/staff_email_ledger.sql --target railway --dry-run
python -m scripts.apply_sql sql/staff_email_ledger.sql --target railway
```

then add `"staff_email_ledger.sql"` to `REQUIRED_MIGRATIONS` **in the same
change**. Never pgAdmin — it applies the SQL and swallows the NOTICEs that are
the file's own verification report.

---

## K.2 Stage 2 — shipped locally 2026-08-22

**Decide and observe. Still no send path.** `STAFF_EMAIL_ENABLED=1` so decisions
are real decisions rather than `"disabled"`; `STAFF_EMAIL_APPLY=0`, and the
module still contains no sender — `test_55` re-asserts it.

### What was added

| File | What |
|---|---|
| `sql/staff_email_stage2.sql` | rules table, tier function, trigger change, observations table — **local only** |
| `app/core/staff_email.py` | `sync_tier_rules()`, `tier_for_escalation()`, `observe()`, `observations()` |
| `app/core/escalation.py` | shadow observation in `open()` |
| `app/core/governance.py` | shadow observation in the approval route |
| `.env` | the Stage 2 flag pair + the budget, restated where it is operated |
| `tests/test_staff_email_stage2.py` | 37 cases |

### The four decisions worth reviewing

**1. The tier is stamped by the INSERT trigger, not by callers.** Notifications
are written from at least eight Python modules plus SQL, all through the
`notifications` view's `INSTEAD OF` trigger. Stamping there means a writer added
next year is covered without knowing this feature exists. Verified end to end:

```
supervisor.alert       → critical
invoice.overdue        → actionable
invoice_created        → informational
order.status_changed   → informational
```

**2. The taxonomy has ONE author, and the database holds a copy, not a rule.**
The trigger needs the map in SQL. Restating `classify()` in PL/pgSQL would give
the codebase two copies of "which events matter", and the copy that drifts is
always the one nobody watches. So `notification_tier_rules` is **generated** by
`sync_tier_rules()` from `notification_triage`'s own sets, and `test_11` fails
the moment they disagree. Only `critical` and `actionable` get rows —
**informational is expressed as absence**, which is what makes "a new event type
cannot acquire email by being added" structural rather than promised.

**3. The escalation tier is read off the gate that already exists.**
`tier_for_escalation()` translates `escalation._EMAIL_REASONS` into tier
vocabulary; it does not re-decide it. `test_32` adds a reason to that set at
runtime and asserts the mapping follows — a literal list here would not.

**4. Observation is aggregate, and test-caused rows are labelled, not
suppressed.** One row per decision would rebuild the volume problem inside the
observability table. The grain is
`(day, kind, tier, decision, reason_class, recipient_kind, origin)`.

`origin` was added after a real defect: the test suite opens **real**
escalations, which fire the real observer, and **one suite run put 45 synthetic
`send` decisions into the day's counters** — which would have made the Stage 2
verdict a lie in the safe-looking direction. Suppressing the observer under test
would have left the write path untested, so the rows are labelled `test`
instead: visible in `decisions`, excluded from `gates`. After the fix, a full
suite run leaves **0 live decisions**.

### End-to-end shadow observation, from a real escalation

```
opened customer_requested_human   (in _EMAIL_REASONS)  → Tier 1
opened complaint                  (outside it)         → Tier 2

email_kind   tier         decision  reason_class        recipient_kind
escalation   critical     send      eligible            role_mailbox
escalation   actionable   refuse    deferred_to_digest  none

ledger rows: 0     ← observation takes no action
```

The Tier 1 decision routes to the **role mailbox** because nothing owns an
escalation at `open()` time — §C.2 working exactly as specified.

### Verification

| Check | Result |
|---|---|
| New tests | `tests/test_staff_email_stage2.py` — **37 passing** |
| Mutation 1 — sync stops deleting demoted rules | **test_13 fails** |
| Mutation 2 — remove the observer's guard around `decide()` | **test_40 fails** |
| Mutation 3 — remove the host's try/except in `escalation.open()` | **test_41 fails** |
| Mutation 4 — drop `tier` from the trigger's INSERT | **4 cases of test_20 fail** |
| Mutation 5 — drop the `origin == 'live'` filter | **test_53b fails** |
| Full regression | **1932 passed, 1 skipped** (was 1895) |
| Live decisions left by a full suite run | **0** |
| Ledger rows left by a full suite run | 0 |

### A defect this stage found in its own tests

`test_41` leaked an escalation row the first time it was mutation-checked: its
cleanup was a line *after* the assertion, and a failing assertion never reaches
it. **A test that only tidies up when it passes leaves its evidence behind
exactly when something went wrong.** Cleanup moved into a fixture teardown.

### The Stage 3 gate

Read it from `GET /staff-email/observations?days=7` after seven days:

```json
"gates": {
  "tier1_max_per_day": 0,
  "tier1_single_digit": true,      // must stay true
  "tier3_sends": 0,                // HARD GATE — must be exactly 0
  "tier3_never_emailable": true
}
```

`tier3_sends` is a **count that must be zero**, not a threshold. A single Tier 3
`send` means the classifier is wrong, and no amount of good behaviour elsewhere
makes that safe.

### ⚠ Hand-off — the owner's step

`sql/staff_email_stage2.sql` is applied to **local only** and, like the Stage 1
file, is **deliberately not declared** in `REQUIRED_MIGRATIONS`.

```
python -m scripts.apply_sql sql/staff_email_stage2.sql --target railway --dry-run
python -m scripts.apply_sql sql/staff_email_stage2.sql --target railway
```

Then, in the same change, add **both** files to `REQUIRED_MIGRATIONS` in order:

```python
"staff_email_ledger.sql",
"staff_email_stage2.sql",
```

and after applying, call `POST /staff-email/sync-tier-rules` on Railway — the
rules table ships empty by design, and an empty table means every event is
informational, which is safe but blind.

**Note the environment split (F8):** Railway is where the seven days of evidence
must be gathered. Local observations are real decisions about local data, and
local data is not what Stage 3 is being judged on.

---

## K.3 Stage 3 — shipped locally 2026-08-22

**The ledger wraps the senders that already exist.** Neither path was moved,
rewritten or re-gated: `governance.route_approval` and
`escalation._email_escalation` keep their own composition, recipients and
`send_email` call. What they gain is a ledger row around the attempt.

`staff_email.py` still contains **no sender** — `begin_send()` claims,
`finish_send()` interprets what the *caller's* provider returned. `test_50`
parses the AST to keep it that way, and Stage 3 is the stage where breaking that
is most tempting, because the module now stands directly beside two real
senders.

### The distinction the whole stage rests on

```
"the ledger is unavailable"      →  PROCEED, unrecorded.
"someone else already has this"  →  SKIP.
```

They look alike in code and are opposites in consequence. The first is our
bookkeeping failing, and **bookkeeping must never cost an executive their
approval email** — especially while the migration is applied locally and not yet
on Railway. The second is the duplicate the ledger exists to prevent. Collapsing
them either drops real mail or sends it twice. `test_10` and `test_11` are that
pair, and mutating either one flips a test.

### What changed, precisely

| File | Change |
|---|---|
| `app/core/staff_email.py` | `begin_send()` / `finish_send()`; `origin` on the ledger; `budget()` counts only `origin='live'` |
| `app/core/escalation.py` | claim before the send; `_record_email_outcome()` extracted so the new "not sent, already handled" path records itself the same way |
| `app/core/governance.py` | claim before the approval send; `renotify_pending()` deliberately left unwired |
| `sql/staff_email_ledger.sql` | `origin` column, idempotent upgrade |
| `tests/test_staff_email_stage3.py` | 25 cases |
| `tests/test_escalation_email_routing.py` | fixture now clears the ledger rows its sends cause |

**`renotify_pending()` is deliberately not wired.** It exists so a human can
re-send an approval with a better template — the one case where suppressing a
duplicate is the wrong answer.

**The outcome predicate is imported, not restated.** `finish_send` uses
`order_notifications.classify_send_result`, which already encodes the doctrine
whose absence produced 25 false `sent` records: the default is failure, and only
positive evidence promotes it. A second copy is a second chance to get it subtly
wrong.

### Live verification — the governance path

```
routed to      : CFO Sherman Zhang
emails sent    : 1 -> cfo@agentorc.ca          ← behaviour unchanged
ledger state   : accepted
recipient_kind : executive
owner recorded : 4dde342c-178a-480d-9f45-3a3fb51dc3cb

after re-route : 1 email(s) — IDEMPOTENT       ← this is new
```

The last line is the concrete gain. Before Stage 3, routing the same approval
twice emailed the executive twice.

### Verification

| Check | Result |
|---|---|
| New tests | `tests/test_staff_email_stage3.py` — **25 passing** |
| Mutation 1 — fail *closed* when the ledger is unavailable | **test_10 fails** |
| Mutation 2 — drop the `acquire()` compare-and-swap | **3 tests fail** |
| Mutation 3 — promote a bare `success` to accepted | **test_20[resend-no-id] fails** |
| Existing escalation-email suite | **22 passing, unchanged** |
| Full regression | **1957 passed, 1 skipped** (was 1930) |
| Ledger rows after a full suite run | **0** |
| `origin='live'` rows after a full suite run | **0** |

### Two defects this stage found in its own tests

**1. A cleanup that matched nothing looked exactly like one that worked.** The
Stage 3 fixture deleted `WHERE idempotency_key = <ref>`, but the ledger stores
`escalation:<ref>` — so it matched nothing and **70 rows survived every run**.
Now matched on the ref.

**2. Test sends spent the real recipient budget.** The suite exercises the
*real* send path, which is the point — and one run wrote **17 accepted rows for
the role mailbox, past the 12/day cap**, making an unrelated test fail for
reasons invisible from its own code. Fixed the same way Stage 2 fixed
observations: an `origin` column, `budget()` counting only `live`. Labelling
beats suppressing — the rows stay visible and the path stays tested.

### ⚠ Hand-off — the owner's step

`sql/staff_email_ledger.sql` **changed in this stage** (it gained `origin`). It
is idempotent, so re-applying is safe and required:

```
python -m scripts.apply_sql sql/staff_email_ledger.sql --target railway
python -m scripts.apply_sql sql/staff_email_stage2.sql  --target railway
```

Both remain undeclared in `REQUIRED_MIGRATIONS` until you apply them there.

**Until the migration lands on Railway, Stage 3 is a no-op in production** —
`begin_send()` finds no table, logs `proceeding UNRECORDED`, and both emails go
out exactly as they do today. That is the fail-open path working as designed,
not a silent failure.

---

## K.4 Stage 4 — built locally 2026-08-22, **not switched on**

The digest exists, is tested, and is wired into the scheduler at 07:30 ET.
`STAFF_EMAIL_APPLY` is **still 0**, so it composes and records without sending.
Flipping it is a separate, gated decision — see the bottom of this section.

### The guarantee changed shape rather than disappearing

Stages 1–3 asserted this module *could not send at all*. Stage 4 is where that
stopped being true, so:

> `send_email` is called from **exactly one function — `_deliver`** — and
> `_deliver` is unreachable except through claim → acquire → mark_attempted.

`test_50` asserts the structure; `test_51` asserts the behaviour by breaking
`acquire()` and confirming nothing goes out. The three earlier stages' tests were
**narrowed to the same assertion**, not deleted — weakening a guard to let a
change through is the failure mode those guards exist to prevent.

### The fail rule INVERTS here

```
wrapping mail that ALREADY EXISTS (Stage 3)  →  fail OPEN
    bookkeeping must never cost an executive an approval they were
    already receiving.
sending NEW mail (Stage 4 digest)            →  fail CLOSED
    nobody is waiting for it, nothing regresses if it does not arrive,
    and mail we cannot record is how a volume incident goes unnoticed.
```

`test_30` states that inversion as a test, so the next reader cannot "fix" it
into consistency with Stage 3.

### The empty digest is the feature

Measured on the day this shipped: **all four authorized recipients have zero
Tier 2 items**, so a live run sends zero emails.

```
recipients: 4     sent: 0
  db6a9f31…  items 0  — nothing actionable — digest skipped
  23f8579b…  items 0  — nothing actionable — digest skipped
  4dde342c…  items 0  — nothing actionable — digest skipped
  e4d99e38…  items 0  — nothing actionable — digest skipped
```

Their 53 unread notifications are all `unstamped` (pre-Stage-2) or
`informational`. NULL means never email, and that is the right answer for rows
written before an email decision existed.

### Reading `notifications.employee_uuid` — the safe direction

F1 says that column is an ambiguous identity space. `digest_items()` reads it
anyway, in the **safe direction only**: it starts from an `owner_id` already
proven authorized and asks what is waiting for them. The forbidden direction is
the reverse — taking a notification's uuid and deriving whom to email. One
resolves work for a known person; the other invents a person from a row.

### Verification

| Check | Result |
|---|---|
| New tests | `tests/test_staff_email_stage4.py` — **18 passing** |
| Mutation 1 — remove the empty-digest skip | **3 tests fail** |
| Mutation 2 — copy Stage 3's fail-OPEN rule into Stage 4 | **test_30 fails** |
| Mutation 3 — add a second `send_email` call site | **test_50 fails** |
| Mutation 4 — move the approval observation back after the send | **test_45b fails** |
| Full regression | **1976 passed, 1 skipped** (was 1957) |
| Ledger rows / live observations after a full suite run | **0 / 0** |

### A silent bug this stage surfaced

The Stage 2 shadow observation for approvals sat **after** the email block. By
the time it ran, Stage 3 had already claimed the ledger key — so `decide()`
correctly answered *"already in the ledger in a terminal state"*, and **every
approval observed as `already_handled` instead of as the decision actually
taken.** The Stage 2 evidence for approvals was quietly worthless. Nothing
failed; it was caught by reading one live counter row.

Fixed by moving the observation before the send, verified end to end
(`send / eligible` now, `already_handled` before), and guarded by `test_45b` —
which asserts source order, because the symptom is a plausible counter value
rather than an exception.

### New surfaces

| Endpoint | What |
|---|---|
| `POST /staff-email/digest-preview` | Renders one person's digest **without sending or claiming**. This is how "is it useful?" — the Stage 4 exit criterion the code cannot answer for itself — gets answered by the person who would receive it |
| job `staff_email_digest` | Daily 07:30 ET, before the CEO briefing so a reader meets their own queue first |

### ⚠ The gate — do not flip `APPLY=1` yet

Stage 4's exit criterion is *"≤1 email/recipient/day for 7 days; recipients
confirm it is useful"*, and both halves are still unmet:

1. **The Stage 2 evidence window has not run.** It needs seven days on
   **Railway** (F8), and the migrations are not applied there yet.
2. **Nobody has read a digest.** Use `POST /staff-email/digest-preview` with an
   `owner_id` — it renders without sending. Today it returns
   `{"items": 0, "would_send": false}` for all four, which is itself the answer
   that there is nothing to evaluate yet.

When both are satisfied, `STAFF_EMAIL_APPLY=1` on **Railway only** — local stays
0, matching the briefing topology, or every executive gets two copies.
