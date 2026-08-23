# Stage 4 Adversarial Audit + Stage 5 Readiness Assessment

**Independent review, 2026-08-22.** `STAFF_EMAIL_APPLY` was **not** enabled. No
production email was sent. Stage 5 was **not** implemented.

The audit did not assume the Stage 4 report was correct. It found **three
defects the Stage 4 report did not contain**, one of which would have corrupted
Stage 5's evidence from its first day.

---

## A. Stage 4 verdict

### **CONDITIONAL PASS**

The implementation does what the Stage 4 report claims: the digest is real,
gated, tested, leader-scheduled, and sends nothing today. Every claim in §2 of
the audit brief was verified against the code and the database rather than the
report.

But three findings stand, and one was a live defect:

| | Finding | Severity | State |
|---|---|---|---|
| **F10** | The escalation observer had the **identical ordering bug** as governance, and Stage 4 missed it | **HIGH** | **FIXED + guarded** in this audit |
| **F11** | The `send_email` guard is **module-scoped, not system-scoped** — 20 call sites exist across 17 modules | **MEDIUM** | **FIXED** 2026-08-22 — allowlist in `app/`, enforced at startup |
| **F12** | `send_digest()` **does not call `decide()`** — a second decision path, and the digest is invisible to `observations()` | **MEDIUM** | **FIXED** 2026-08-22 — one decision path |

---

## F10 — the bug Stage 4 fixed in one place and missed in the other

Stage 4 found and fixed an ordering bug in `governance.route_approval`: the
Stage 2 shadow observation ran *after* Stage 3's ledger claim, so `decide()`
correctly answered *"already in the ledger in a terminal state"* and every
approval recorded as `already_handled` instead of the decision actually taken.

**`escalation.open()` had exactly the same defect and was not fixed.**

```python
_email_escalation(...)          # line 364 — Stage 3 claims the ledger key
...
staff_email.observe(...)        # line 378 — decide() now sees a terminal row
```

Proven empirically with `ESCALATION_EMAIL=1`:

```
BEFORE:  ('escalation', 'critical', 'refuse', 'already_handled', 1)
AFTER :  ('escalation', 'critical', 'send',   'eligible',        1)
```

**Why it was invisible.** With `ESCALATION_EMAIL=0` — its shipped default —
`_email_escalation` returns before claiming, so the observation looked correct
in every test and in the Stage 2 end-to-end check. **It activates on precisely
the flag Stage 5 turns on**, which means Stage 5's evidence would have been
worthless from its first day, in the safe-looking direction.

Fixed by moving the observation before the send. The guard (`test_45b`) is now
**parametrized over both call sites** rather than asserting the one that
happened to be noticed — the original test would have passed against the broken
escalation path indefinitely.

**Lesson for the pattern search the brief asked for (§5).** The general shape is:

```
perform action  →  mutate ledger/state  →  observe what happened
```

Every site where an observation reads state that an earlier step in the same
function mutated is suspect. Both known instances are now fixed and guarded. A
repo-wide sweep for `staff_email.observe` found exactly two call sites
(`escalation.py`, `governance.py`) and both are covered.

---

## F11 — the send guard is module-scoped, and the brief asked the right question

The brief (§4) states the requirement precisely:

> future developers must not be able to bypass the email-governance layer simply
> by calling `send_email()` directly.

**They can.** An AST sweep gives **20 call sites across 17 modules** — the first count in this audit said 17/10 and was a truncated grep:

| Module | Sites | Legitimate? |
|---|---|---|
| `agents/email/graph.py`, `router.py`, `structured.py`, `auto_reply.py` | 8 | Yes — the email agent owns customer mail |
| `agents/auth/router.py`, `agents/admin_users/router.py` | 2 | Yes — OTP and account provisioning |
| `core/ceo_briefing.py` | 2 | Yes — pre-existing executive briefing |
| `core/booking.py`, `core/agent_console.py` | 2 | Yes — booking confirmations, rep replies |
| `core/escalation.py`, `core/governance.py` | 3 | Yes — the two Stage 3 paths, ledger-wrapped |
| **`core/staff_email.py`** | **1 (`_deliver`)** | **The Stage 4 authorized path** |

`test_50` asserts that `staff_email.py` reaches a sender from exactly one
function. That is a real guarantee and it mutation-fails correctly. **It is not
the guarantee the brief asks for.** It stops this module growing a second
sender; it does not stop a *new* module emailing staff directly and bypassing
tier, recipient, preference, budget and ledger entirely.

**Indirect invocation** was checked: no `getattr`-style dynamic dispatch to a
sender exists anywhere in the email path, and `finish_send`'s import of
`order_notifications` pulls in a pure classifier, not a sender.

**Options, for an owner decision — none taken:**

1. **A repo-wide allowlist test.** Enumerate the permitted `send_email` call
   sites; fail on any new one. Cheap, exact, and it makes adding a sender a
   deliberate edit to the allowlist. *Recommended.*
2. **A recipient-side guard in `send_email` itself.** Refuse a recipient that
   resolves to `assignable_identity` unless a ledger row exists. Strongest, but
   it puts a database read in the hot path of every send, including customer
   mail.
3. **Accept it and document it.** Defensible today — the staff recipient
   universe is four addresses — and indefensible the moment it grows.

Until one is chosen, the honest statement is: **the governance layer is
authoritative for the paths that use it, and bypassable by a path that does
not.**

---

## F12 — the digest has its own decision path

`send_digest()` does **not** call `decide()`. It re-implements the gates inline:
`preference_for` → `preference_allows` → `budget_allows` → `begin_send`.

Two consequences:

1. **The digest is invisible to `observations()`.** Every other decision in the
   system is counted; this one is not. The Stage 4 exit criterion is measured
   from data the digest does not contribute to.
2. **There are now two decision paths.** That is exactly the drift this design
   fights everywhere else — one taxonomy, one recipient resolver, one outcome
   predicate — and the digest quietly opted out.

It is not currently a *safety* defect: the inline gates are the same gates in
the same order, and `begin_send` still enforces idempotency. It is a
**consistency and observability** defect, and it should be fixed **before**
Stage 5 rather than after, because Stage 5 must not add a third path.

---

## B. Stage 4 invariants that must never regress

Each is currently enforced by at least one mutation-verified test.

| # | Invariant | Enforced by |
|---|---|---|
| I1 | A notification row is never sufficient authority to send an email | `notification_messages.tier` comment; tier gate; `test_12` |
| I2 | Absence of a positive classification means **no email** — NULL, unknown, unstamped and `informational` all refuse | `test_11`, `test_22`, verified at SQL, Python, classifier, digest and ledger layers |
| I3 | A new event type cannot acquire email by being added anywhere | rules table stores only critical/actionable; informational is **absence** |
| I4 | A colliding uuid never yields an address | `test_20` (Stage 1) |
| I5 | An unresolvable assignee is **discarded**, never used raw | `test_21`, `test_22` (Stage 1) |
| I6 | Preference outranks urgency; missing preference fails closed | `test_33`, `test_30` (Stage 1) |
| I7 | `send_email` is reachable from exactly one function in this module | `test_50` (Stage 4) |
| I8 | Nothing sends without a claimed ledger row | `test_51` (Stage 4) |
| I9 | An empty worklist produces no email **and no ledger row** | `test_10`, `test_11` (Stage 4) |
| I10 | The vocabulary stops at `accepted`; `delivered` is not a writable state | DB CHECK constraint; `test_67` (Stage 1) |
| I11 | Existing mail + bookkeeping failure → **fail OPEN** | `test_10` (Stage 3) |
| I12 | New mail + decision uncertainty → **fail CLOSED** | `test_30` (Stage 4) |
| I13 | Concurrent workers send at most one copy | `test_53` (Stage 4) — 8 threads → 1 email |
| I14 | Observations record the decision **taken**, not one reconstructed from later state | `test_45b`, both call sites |

---

## C. Verified: the fail-rule asymmetry is real and intentional

The brief (§3) is right that this must not be "cleaned up" into consistency.

```
Stage 3 — wrapping mail that ALREADY EXISTS   →  fail OPEN
    An executive is already entitled to this approval email. Our audit table
    being absent is our problem, not theirs. Suppressing the mail converts a
    bookkeeping outage into a governance outage.

Stage 4 — generating NEW mail                 →  fail CLOSED
    Nobody is waiting for a digest. Nothing regresses if it does not arrive.
    Sending mail we cannot record is how a volume incident happens with no
    evidence that it did.
```

Verified against the six cases the brief enumerates:

| Case | Behaviour | Test |
|---|---|---|
| 1. Stage 3 bookkeeping failure does not suppress required mail | ✅ proceeds unrecorded, logs `proceeding UNRECORDED` | `test_10` (S3) + `test_45` integration |
| 2. Stage 4 ledger uncertainty prevents the digest | ✅ `sent: False`, outbox empty | `test_30` (S4) |
| 3. Missing/ambiguous recipient prevents new email | ✅ role mailbox for Tier 1; digest requires a resolved address or refuses | `test_20`/`test_21` (S1) |
| 4. Failed deduplication prevents new email | ✅ `acquire()` returns None → no send | `test_51` (S4) |
| 5. Unavailable preference prevents new email | ✅ fail-closed to `in_app`/`false` | `test_30` (S1), `test_40` (S4) |
| 6. Uncertain eligibility prevents new email | ✅ default tier is `informational` | `test_11` (S1) |

Both directions mutation-verified: making Stage 3 fail closed breaks `test_10`;
copying Stage 3's rule into Stage 4 breaks `test_30`.

---

## D. Remaining Stage 4 blockers

| Blocker | State |
|---|---|
| **Seven-day Stage 2 evidence window** | **NOT STARTED.** Requires Railway; migrations are not applied there |
| **Migrations on Railway** | **NOT APPLIED.** `staff_email_ledger.sql` + `staff_email_stage2.sql` |
| **Tier rules seeded on Railway** | **NOT DONE.** Ships empty; empty = every event informational = safe but blind |
| **Digest preview read by a recipient** | **NOT POSSIBLE YET** — returns `items: 0` for all four |
| **Topology** | ✅ verified — see §9 below |
| **Identity boundary** | ✅ verified — four recipients, traced |
| **Ledger** | ✅ verified — including 8-thread concurrency |
| **Fail-rule asymmetry** | ✅ verified both directions |
| **F11 send-guard scope** | **OPEN — owner decision** |
| **F12 digest bypasses `decide()`** | **OPEN — fix before Stage 5** |

### The evidence window is not neutral

The brief (§10) is explicit and correct: *do not treat missing evidence as
neutral evidence.* Nothing has been measured on Railway. A successful window
must show, from `origin='live'` rows only:

- **`tier3_sends` = 0.** Hard gate, not a threshold.
- **Tier 1 rate single-digit per day**, with the daily series visible — a single
  quiet week proves less than a week containing a busy day.
- **Non-zero sample.** Seven days of zeros proves the observer runs, not that
  the classifier is right. If no Tier 1 decision occurs in the window, the
  window has not concluded.
- **Classification distribution** roughly matching local: overwhelmingly
  informational, a handful actionable, near-zero critical.
- **Refusal-class distribution** — `deferred_to_digest`, `preference_off`,
  `rate_limit_recipient` should appear; `breaker_global` should not.
- **Recipient distribution** — Tier 1 escalation decisions should land on
  `role_mailbox`, approvals on `executive`. Any `assignable` recipient for an
  escalation means an assignee started resolving, which is a change worth
  understanding before it drives mail.

---

## Topology (§9), verified

| Question | Answer | Evidence |
|---|---|---|
| Is Railway the only production scheduler? | Yes | `leader.begin()` gates it; followers build but do not start (`main.py:1314-1321`) |
| Can local accidentally send? | No | `STAFF_EMAIL_APPLY=0` locally, confirmed in `.env` |
| Can two instances both send the digest? | **No — proven** | 8 concurrent threads → **1 email, 1 ledger row**. Now `test_53` |
| Does leader election have a gap? | Yes, a known one | `leader.py` does a **synchronous election at startup**; zero-gap failover is a documented follow-up. During a promotion window the ledger is the only guard — which is why `test_53` matters |
| Can staging reach production recipients? | Not by scheduler | But nothing prevents a developer running `run_digest()` against the Railway DSN with `APPLY=1` in their shell. Mitigated only by the flag |

---

## Identity boundary (§8), traced

```
event → responsibility → assignable identity → authorized recipient
      → email preference → send eligibility
```

`assignable_identity` holds **4 rows**, all executives. Verified that none of
these confer eligibility:

| Having… | Rows | Eligible? |
|---|---|---|
| an `employees` row | 21 | **No** — `demo_employee`, never granted |
| an email address | many | **No** |
| an `@emp.agentorc.ca` mailbox | 8 | **No** — and it is a catch-all (R6) |
| an `owners` row | 44 | **No** — 90% customer contacts |
| a `contacts` row | 182 | **No** |
| **an `assignable_identity` row + `auto_email_enabled`** | **4** | **Yes** |

`email_preference()` fail-closes to `in_app`/`false` for anyone absent. The
defaults on `assignable_identity` are the **inverse** of `executives`', so a
future `grant()` does not silently confer email.

---

## Empty-digest behaviour (§7), verified

```
items = 0  →  no ledger row  →  no provider call  →  no email
```

Confirmed for every case the brief lists:

| Case | Result |
|---|---|
| Only Tier 3 notifications | 0 items, no email — `test_21` |
| Only legacy NULL notifications | 0 items — `test_22` (the recipient genuinely has such rows) |
| One actionable item | 1 item, sends — `test_20` |
| Multiple actionable items | n items, one email — `test_20` |
| Read/suppressed items | excluded by `status <> 'read'` |
| Unauthorized items | unreachable — the query starts from an authorized `owner_id` |
| No recipient preference | refused before composing — `test_40` |

**The invariant holds: the existence of a recipient is never sufficient reason
to send a digest.** Today `run_digest()` returns `recipients: 4, sent: 0`.

**On `items = 0` being correct rather than a broken classifier:** verified
positively, not by absence. The trigger stamps correctly (4 parametrized cases
end-to-end), 34 `actionable` messages exist in the database, and the fixture
that creates real Tier 2 items produces a real digest. The four executives have
zero because their 53 unread rows are all pre-Stage-2 (`NULL`) or
`informational` — which is the correct answer, not a silent failure.

---

## E. Stage 5 threat model

Stage 5 is materially higher risk than Stage 4, and not because it is harder to
build. A digest says *"here is your list."* An interrupt says *"something
important is being missed right now"* — a claim about the present that the
system must be able to prove at the instant of sending.

| # | Threat | Mechanism | Severity |
|---|---|---|---|
| T1 | **Backlog email storm** | Enabling Stage 5 with 130 breached escalations in the table | **CRITICAL** |
| T2 | **Stale email** | Escalation claimed/resolved between the eligibility read and the provider call | **HIGH** |
| T3 | **Wrong recipient** | Routing from `assigned_to`, which holds a customer address and two `.invalid` fixtures | **HIGH** |
| T4 | **Nagging** | Unbounded reminders turning an alert channel into noise, guaranteeing it is ignored | **HIGH** |
| T5 | **Duplicate email** | Two workers, or a retry after provider acceptance | **MEDIUM** |
| T6 | **Fan-out** | `escalation._notify` already notifies *every* linked executive; carrying that into email means 4 emails per escalation | **HIGH** |
| T7 | **Misleading email** | Asserting "unattended" about something a rep is actively handling in the console | **MEDIUM** |
| T8 | **Unauthorized email** | A future `grant()` silently making 8 more people reminder recipients | **MEDIUM** |
| T9 | **Sender reputation** | 122 emails to `support@agentorc.ca` in one burst on the shared `mail.agentorc.ca` identity | **MEDIUM** |
| T10 | **Evidence corruption** | F10 — the observation recording `already_handled` for every escalation | **FIXED** |

### T1 quantified — this is not hypothetical

Measured on the live database at audit time:

```
live escalations (open|assigned)     : 130
  ...of which SLA-breached           : 130   (100%)
  ...opened in the last 24 hours     :   0
  ...youngest                        :  10 days old
  ...oldest                          :  21 days old
  reasons: customer_requested_human  : 122   [Tier 1 — remind-eligible]
           complaint                 :   8   [Tier 2 — not remind-eligible]
```

**Every one of the 122 Tier-1-reason escalations would qualify for a reminder
on a naive "breached and unclaimed" predicate.** With the role-mailbox fallback
(all five distinct `assigned_to` values resolve to `None`), that is **122 emails
to one address in one scheduler tick.**

The 24-hour cutoff suppresses all 122 — **verified: zero live escalations are
under 24 hours old.**

---

## F. Stage 5 state machine

Derived from the actual schema (`escalations`: `status`, `assigned_to`,
`assigned_at`, `sla_due_at`, `sla_minutes`, `resolved_at`, `created_at`,
`conversation_id`) and `conversations` (`handling`, `assigned_to`).

```
                    open()
                      │  status='open', assigned_to NULL, sla_due_at set
                      ▼
              ┌───────────────┐
              │    WAITING    │  status='open'
              └───────┬───────┘
                      │
        age >= REMIND_AT_FRACTION × sla_minutes
        AND age <= BACKLOG_CUTOFF
        AND conversations.handling <> 'human'
                      │
                      ▼
              ┌───────────────┐   reminder_ordinal 1
              │REMINDER SENT  │──────────────┐
              └───────┬───────┘              │ cooldown elapsed
                      │                      │ AND ordinal < REMIND_MAX
                      │                      ▼
                      │              ┌───────────────┐
                      │              │  REMINDER 2   │  (terminal for reminders)
                      │              └───────────────┘
                      │
   ┌──────────────────┼──────────────────┬─────────────────────┐
   │ takeover/assign  │ resolve          │ reassign            │ age > cutoff
   ▼                  ▼                  ▼                     ▼
┌──────────┐    ┌───────────┐    ┌──────────────┐    ┌──────────────────┐
│ ASSIGNED │    │ RESOLVED  │    │ REASSIGNED   │    │ AGED OUT         │
│ NO MORE  │    │ NO MORE   │    │ new owner    │    │ NO EMAIL, EVER   │
│ REMINDERS│    │ REMINDERS │    │ inherits the │    │ backlog, not an  │
│          │    │           │    │ SAME ordinal │    │ interrupt        │
└──────────┘    └───────────┘    └──────────────┘    └──────────────────┘
```

**Two states the brief asked about that the schema does not have:**

- **`cancelled`** exists in the CHECK constraint but is unused. Treat identically
  to resolved: no reminders.
- **`expired`** does not exist. "Aged out" is derived from `created_at`, not
  stored — deliberately, because a stored flag would need a job to maintain and
  a job that does not run would make stale rows eligible again.

**"Reassigned inherits the same ordinal"** is a deliberate call: resetting the
counter on reassignment lets an escalation bounce between owners and generate
unlimited mail. The reminder budget belongs to the *escalation*, not to the
person.

---

## G. Stage 5 eligibility predicate — derived, and evaluated *as part of the claim*

The brief's §14 question is the right one: *is this still true RIGHT NOW?* The
answer cannot be a read followed by a decision followed by a send — that leaves
two windows. **The eligibility test must be inside the statement that claims the
reminder**, exactly as `acquire()` makes the state check *be* the claim.

```sql
-- ONE statement: re-reads live escalation state AND claims the reminder.
-- Losing the race and being ineligible are the same outcome: no row, no send.
INSERT INTO staff_email_ledger
      (idempotency_key, email_kind, tier, recipient_email, recipient_kind,
       recipient_owner_id, subject_ref_type, subject_ref_id, state,
       decision_reason, origin)
SELECT 'escalation_remind:' || e.escalation_id || ':remind:' || %(ordinal)s,
       'escalation_remind', 'critical',
       %(recipient_email)s, %(recipient_kind)s, %(recipient_owner_id)s::uuid,
       'escalation', e.escalation_id, 'queued',
       'unclaimed ' || age(now(), e.created_at)::text, %(origin)s
  FROM escalations e
 WHERE e.escalation_id = %(eid)s::uuid
       -- STILL UNATTENDED
   AND e.status = 'open'                    -- 'assigned' means somebody took it
   AND e.resolved_at IS NULL
   AND e.assigned_at IS NULL
       -- NOBODY IS ON IT IN THE CONSOLE RIGHT NOW
   AND NOT EXISTS (SELECT 1 FROM conversations c
                    WHERE c.conversation_id = e.conversation_id
                      AND c.handling = 'human')
       -- OLD ENOUGH TO MATTER
   AND now() - e.created_at
       >= make_interval(mins => (e.sla_minutes * %(remind_fraction)s)::int)
       -- YOUNG ENOUGH TO BE AN INTERRUPT RATHER THAN A BACKLOG REPORT
   AND e.created_at > now() - make_interval(hours => %(backlog_cutoff_hours)s)
       -- THE REASON IS ONE A HUMAN MUST ACT ON
   AND e.reason = ANY(%(email_reasons)s)
       -- COOLDOWN SINCE THE LAST REMINDER FOR THIS ESCALATION
   AND NOT EXISTS (SELECT 1 FROM staff_email_ledger l
                    WHERE l.subject_ref_id = e.escalation_id
                      AND l.email_kind = 'escalation_remind'
                      AND l.created_at > now()
                          - make_interval(mins => %(cooldown_min)s))
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING email_id;
```

Then, unchanged from Stage 3/4: `acquire()` → `mark_attempted()` → provider →
`finish_send()`.

**What is deliberately NOT in the predicate:**

- `sla_due_at < now()`. Redundant with the age test and worse: an escalation
  whose SLA was set generously would be reminded on the same schedule as an
  urgent one. The reminder threshold is a *fraction of that escalation's own
  SLA*, so priority already scales it (`_SLA_BY_PRIORITY`: urgent 0.25×).
- Any check that reads state in Python before the INSERT. Every such check is a
  race the single statement above does not have.

**Recipient resolution** (per §17, and this is the narrow answer):

```
escalations.assigned_to → assignable.resolve()
    hit  → that one person, one email
    miss → the ROLE MAILBOX, one email
```

**Never** `escalation._notify`'s fan-out to every linked executive. That is
correct for an in-app notice, where four people glancing at a queue costs
nothing, and wrong for email, where it means four interruptions for one problem.
On today's data every assignee misses, so **every reminder goes to exactly one
role mailbox** — which is also why the per-recipient cap (6/hour) is the
binding constraint, not the global breaker.

---

## H. Stage 5 implementation plan

| File | Change | Size |
|---|---|---|
| `app/core/staff_email.py` | **First: fix F12** — route `send_digest` through `decide()`. Then `claim_reminder()` (the SQL above), `reminder_candidates()`, `run_reminders()` | medium |
| `app/core/escalation.py` | Expose `reminder_state(escalation_id)`; no behaviour change | small |
| `sql/staff_email_stage5.sql` | Index on `escalations (status, created_at)` for the candidate scan; index on `staff_email_ledger (subject_ref_id, email_kind, created_at)` for the cooldown check | small |
| `app/main.py` | Hourly job `staff_email_reminders`, leader-gated | small |
| `.env` | `STAFF_EMAIL_STAGE5_APPLY=0`, `STAFF_EMAIL_REMIND_*`, `STAFF_EMAIL_BACKLOG_CUTOFF_HOURS=24` | — |
| `tests/test_staff_email_stage5.py` | §J | — |

**Not to be built:** a `reminders` table. The ledger already carries the state
(`subject_ref_id` + `email_kind` + `idempotency_key` with an ordinal). A second
table would be a second source of truth about whether somebody was contacted.

---

## I. Stage 5 dry-run design (mandatory before activation)

`STAFF_EMAIL_STAGE5_APPLY=0` — a **separate flag from `STAFF_EMAIL_APPLY`**, so
the digest and the reminders can be enabled independently. Conflating them would
make the first reminder ride out on the digest's authorization.

The dry run runs the **real** candidate query and the **real** predicate, and
stops before the claim. It must answer, without sending anything:

> If Stage 5 were enabled right now, exactly which emails would it attempt?

```json
{
  "as_of": "2026-08-22T22:00:00Z",
  "applying": false,
  "candidates_scanned": 130,
  "would_send": 0,
  "by_suppression_reason": {
    "older_than_backlog_cutoff": 122,
    "reason_not_remind_eligible": 8
  },
  "would_send_detail": [],
  "budget_after_hypothetical_send": {
    "recipient_hour": 0, "cap": 6,
    "global_hour": 0, "cap_global": 25
  },
  "recipients_that_would_be_used": {"role_mailbox": 0, "assignable": 0}
}
```

Two properties make it trustworthy:

1. **Same predicate, one code path.** The dry run must not have its own copy of
   the eligibility logic — F12's mistake, repeated.
2. **`would_send` is a list of concrete escalation ids**, not a count. A count
   can be right for the wrong reasons.

**Expected output today: `would_send: 0`, all 122 suppressed by the cutoff.**
That number is the Stage 5 canary's precondition — if a dry run ever reports a
three-digit `would_send`, activation stops.

---

## J. Stage 5 test plan

### Normal
1. Fresh unclaimed escalation past its threshold → one reminder, one ledger row.
2. Reminder names the escalation, its age, the responsible party, a console link.
3. Priority scales the threshold (urgent 0.25× SLA reminds sooner).

### Negative
4. Reason outside `_EMAIL_REASONS` → no reminder.
5. Escalation younger than the threshold → no reminder.
6. Escalation older than the cutoff → no reminder.
7. `status='assigned'` → no reminder.
8. `resolved_at` set → no reminder.
9. `conversations.handling='human'` → no reminder (somebody is on it).
10. Recipient preference off → no reminder.
11. Recipient unauthorized / unresolvable → role mailbox, still exactly one.

### Concurrency & restart
12. Eight workers on one escalation → exactly one email.
13. Crash after provider acceptance → no immediate resend (lease).
14. Failed send → retryable without corrupting escalation state.
15. Scheduler offline across the threshold → still eligible until the cutoff, then never.

### The race the brief names (Case B)
16. Claimed *between* the claim statement and the provider call → email goes out;
    assert its body instructs verification and links to live state. **This race
    cannot be eliminated, only bounded and disclosed.**

### Backlog
17. **130 breached escalations → 0 emails on first run.** The activation test.
18. Cutoff raised to 720h → the breaker trips at 25, remainder `skipped`, one
    `supervisor.alert`. Proves the second line of defence.

### Mutation (each must fail)
| | Mutation | Expected |
|---|---|---|
| A | Remove the `status='open'` / unclaimed condition | tests 7, 12 fail |
| B | Remove the backlog cutoff | test 17 fails — **122 emails** |
| C | Allow multiple recipients (restore `_notify` fan-out) | test 11 fails |
| D | Remove the cooldown / ordinal dedup | test 12 fails |
| E | Move the eligibility read outside the claim statement | test 16 exposes the widened race |
| F | Allow resolved escalations | test 8 fails |
| G | Reset the ordinal on reassignment | a new "bouncing escalation" test fails |
| H | Default missing preference to send | test 10 fails |
| I | Add a second `send_email` call site | `test_50` fails |
| J | Disable empty-backlog suppression | test 17 fails |

---

## K. GO / NO-GO — three separate decisions

### `Stage 4 APPLY=1` → **NO-GO**

Not because the implementation is unsound — it is sound. Because **both halves
of its own exit criterion are unmet**, and neither is a formality:

1. The seven-day evidence window has not started. The migrations are not on
   Railway, the tier rules are not seeded there, and per F8 local evidence does
   not substitute.
2. No recipient has read a digest, and none can — the preview returns
   `items: 0` for all four.

**Missing evidence is not neutral evidence.** Re-assess when the window has run
with a non-zero Tier 1 sample and `tier3_sends = 0`.

### `Stage 5 implementation` → **GO, conditional**

Conditions, in order:

1. **Fix F12 first.** Route `send_digest` through `decide()`. Stage 5 must not
   add a third decision path to a system that already has two.
2. **Decide F11.** The allowlist test (option 1) is cheap and is the honest
   answer to "can a future developer bypass this?"
3. Build behind `STAFF_EMAIL_STAGE5_APPLY=0` with the dry run first, and the
   dry-run report reviewed before any flag moves.

The design in §F–§I is implementable now and does not depend on the evidence
window. Building it dark while the window runs is good use of the time.

### `Stage 5 production send` → **NO-GO**

Blocked on everything above, plus:

- a dry run on **Railway** reporting `would_send: 0` against the real backlog;
- Stage 4 having completed its own window and sent successfully at least once;
- the canary defined below.

**Canary, when the time comes:** one recipient (the role mailbox), a hard
`STAFF_EMAIL_REMIND_MAX_PER_RUN=1`, hourly, for three days. Reversible by a flag
that does not also disable the digest. Watch: emails per day, median
email→claim time, and whether the 130-item backlog starts falling. If the
backlog does not move, the reminder is not working and more of it will not help.

---

## The question that actually gates Stage 5

Not *"can Stage 5 send an email?"* — it can, the machinery is proven. It is:

> **Can Stage 5 prove, in the instant before sending, that this particular
> person should be interrupted about this particular unresolved escalation —
> and that it will not interrupt anyone about the other 129?**

The predicate in §G is the answer to the first half: eligibility is evaluated
*inside* the claim, so "still unattended" is not a stale read.

The 24-hour cutoff is the answer to the second half, and it is measured rather
than assumed: **zero of the 130 live escalations are under 24 hours old**, so the
first run sends nothing.

Neither answer is trustworthy until a dry run on Railway says so in numbers.

---

## Remediation — F12 and F11 implemented 2026-08-22

No flag was changed. `STAFF_EMAIL_APPLY=0`, `ESCALATION_EMAIL=0`. Nothing sent.

### F12 — the digest now has one decision path

`send_digest()` re-implemented preference, address and budget inline. It now
calls `observe()` → `decide()` like everything else.

Three changes made it possible:

1. **`resolve_recipient(owner_id=…)`** — a third resolution mode for a caller
   that already holds an authorized identity. It **re-derives the address from
   membership** rather than trusting one handed in, so a grant revoked between
   the caller's read and the call stops resolving. A caller can no longer
   redirect a digest.
2. **`decide(items=…)`** — the empty-worklist rule became a **gate** rather
   than a special case outside the function. Placed above `ledgerable`, so an
   empty digest is *observed* (evidence that the digest ran and found nothing)
   but claims no ledger row (nothing was contacted).
3. **`send_digest`** lost four inline gates.

The digest is now visible in `observations()`:

```
run_digest → recipients 4, sent 0

email_kind  tier        decision  reason_class          n
digest      actionable  refuse    nothing_actionable    4
```

Before this, "the digest ran and found nothing" and "the digest did not run"
were indistinguishable in the evidence the Stage 4 gate reads.

### A further defect found while fixing F12

Two `kb.publish` ledger rows from a pytest run were labelled **`origin='live'`**.

`PYTEST_CURRENT_TEST` is set by pytest *per test* and unset between tests and
after the session — so a background thread or scheduled callback that outlived
its test wrote as production evidence. `_origin()` now checks
`"pytest" in sys.modules` as well, which is true for the whole process and
cannot be unset mid-run. Guarded by `test_75`, which unsets the env var and
asserts the answer is still `test`.

The two mislabelled rows were removed.

### F11 — the allowlist lives in `app/`, not `tests/`

The audit recommended a test. That was the wrong home:
`.github/workflows/ci.yml` states that *"the tests/ directory is also outside
this repository by policy, so CI cannot run any test at all."* A test-only
guard is enforced by whoever remembers to run pytest.

**`app/core/email_call_sites.py`** holds `AUTHORIZED_SENDERS` — all **20** call
sites, each with a reason, grouped by what the mail is *for*. It is enforced
twice from one list:

| Enforcer | When | On a finding |
|---|---|---|
| `release_guard.enforce()` | every startup | **BLOCKING** — a deployed environment refuses to start |
| `tests/test_email_sender_allowlist.py` | during development | fails, naming module and function |

The startup check is the control; the test is the convenience.

**Verified against a real bypass.** Dropping a new module that emails
`sarah.johnson@emp.agentorc.ca` directly produces:

```
BLOCKING - 1 undeclared send_email call site(s):
  app/core/sneaky_feature.py::notify_the_team().
  If it can reach staff, route it through app.core.staff_email;
  if it is customer mail, declare it in app/core/email_call_sites.py.
```

**Stale entries are also reported** (advisory): an allowlist still naming a
deleted call site is one nobody is maintaining, and that is where a future
bypass hides.

**A scan failure degrades to advisory, not to `ok`.** We cannot tell a bypass
from a parse error, and refusing to boot on our own tooling failure is its own
outage — but reporting `ok` would be a false all-clear, which is the exact shape
this codebase has been bitten by four times.

### Verification

| Check | Result |
|---|---|
| F12 tests | 6 new (`test_70`–`test_75`) |
| F11 tests | 14 new |
| Mutation — drop the `sys.modules` arm of `_origin()` | `test_75` fails |
| Mutation — remove the `nothing_actionable` gate | 4 tests fail |
| Mutation — let `resolve_recipient` trust a handed-in `owner_id` | `test_74` fails |
| Mutation — **a new module emails an employee directly** | 2 tests fail + startup BLOCKING |
| Mutation — remove the check from `release_guard.CHECKS` | `test_30` fails |
| Full regression | **1998 passed, 1 skipped** (was 1978) |
| Ledger rows / live observations after a full run | **0 / 0** |
| Flags | unchanged: `APPLY=0`, `ESCALATION_EMAIL=0` |

### Consequence for Stage 5, as predicted

Stage 5's reminder sender **will fail the F11 guard until it is declared** in
`email_call_sites.py`. That is the control working: adding a staff-email sender
is now a deliberate, reviewed edit rather than an invisible one.

### One incidental find, not fixed

`app/core/kb_resolver_devset.py` has `'\w'` in a non-raw string, emitting a
`SyntaxWarning`. Unrelated to this work and out of scope, but the scanner
suppresses it rather than printing somebody else's lint on every boot —
reporting noise trains people to ignore a control's output.

---

# Second adversarial audit — F11/F12/F13 as runtime controls, 2026-08-22

No flag changed. `STAFF_EMAIL_APPLY=0`, `ESCALATION_EMAIL=0`. No email sent.
Stage 5 not implemented. **The F11 guard was not weakened, relocated or
excepted.**

## A. F11 verdict — PASS, after three bypasses found and closed here

### The enforcement boundary is real

| Question | Answer |
|---|---|
| Where does it run? | `release_guard.enforce()`, inside FastAPI's `lifespan` |
| When? | **char 447** of `lifespan` — before `leader.begin()` at char 862, which gates the scheduler |
| Can it be swallowed? | **No.** Not wrapped in try/except; `UnsafeConfiguration` propagates and kills startup |
| Does failure stop the app? | Yes in a *deployed* environment; a laptop gets a log line |
| Can a worker that sends staff email start without it? | **No** — the scheduler starts at line 1254, inside the same lifespan, after `enforce()` |

**Residual, stated honestly:** a human running
`python -c "from app.core import ceo_briefing; send_briefing()"` bypasses the
lifespan entirely. That is not a production worker, and no startup control
reaches it.

### Three bypasses the first version did not catch

I attacked my own scanner with six classes. **Three got through**, and two of
them are ordinary style rather than attacks:

| Class | Before | After |
|---|---|---|
| A. direct call | detected | detected |
| **B. `from … import send_email as mail`** | **BYPASSED** | detected |
| C. `smtp.send_email(...)` | detected | detected |
| **D. `getattr(mod, "send_email")(...)`** | **BYPASSED** | detected (heuristic) |
| E. wrapper helper | detected | detected |
| **F. re-export chain** | **BYPASSED** | **cut at the source** |

**B is the one that mattered.** `from … import send_email as _send` is what
anybody writes to avoid a name clash — the scanner matched on the *called name*
and walked straight past it. Fixed by building a per-module alias map from
`Import`/`ImportFrom` nodes.

**F is cut at its source, not at the consumer.** A binding at MODULE scope is
importable by anything, so that module is itself an email capability and must
declare itself (`<re-export:name>`). Only 3 such bindings exist; all are now
declared. A consumer of a *declared* re-export is still not individually
enumerated — that is the boundary, and `test_57` asserts it stays documented.

**D cannot be closed statically** and is documented rather than papered over.
`_dynamic_suspects()` flags the naive `getattr(m, "send_email")` literal so the
common form is noisy; a computed name defeats it, as it defeats any static
control.

**The guarantee, stated precisely:**

> A sender reached by name, alias, attribute, wrapper or module-level
> re-export is declared, or a deployed process refuses to start.

## B. F12 verdict — PASS

Verified structurally, not by test outcome:

```
_deliver()  called from exactly 1 place  (line 1324, inside send_digest)
send_digest returns: 6
  ...of which precede the observe() call: 0
external callers of send_digest / _deliver / decide: none
```

Every path through `send_digest` passes the decision function. No early return
can send, refuse, suppress or deduplicate without being observed.

*(`governance.decide()` appears in `a2a.py` and `supervisor.py` — a different
function of the same name, not a second staff-email decision path.)*

## C. F13 verdict — PASS, after replacing last round's fix

The brief asked the right question: does merely importing pytest relabel
production activity as test? Measured — `pytest` is not in `sys.modules` after
importing `app.main`, and nothing under `app/` imports it, so the previous fix
was not broad *today*. But the risk was real and fails in the invisible
direction: one dependency importing pytest at runtime and production evidence
silently disappears while the gate reads a clean zero.

**Replaced inference with an explicit marker.** `tests/conftest.py` sets
`CRM_TEST_SESSION=1` in `pytest_configure` — once per session, inherited by
threads and subprocesses, impossible to acquire by accident in production.
`sys.modules` is deliberately no longer checked.

```
production-shaped process          : live
with CRM_TEST_SESSION              : test
pytest merely IMPORTED, no marker  : live   <- the case that mattered
```

## D. Mutation methodology — was NOT trustworthy; now has a harness

The brief is right that this is the meta-problem. **Three mutations silently did
not land** during this work, and each produced green:

1. a replacement string that did not match (whitespace in a continuation line) — 14 tests "passed";
2. a `/tmp` path that did not resolve, so the mutated file was never written;
3. a fixture `DELETE` keyed on the wrong column, matching nothing.

All three are the same defect this codebase has catalogued repeatedly:
**absence of an error read as evidence of success.**

`tests/mutation.py` now provides `mutate()` and `assert_mutation_detected()`,
which refuse to proceed unless:

- the target is present, and present exactly `expected_count` times;
- the replacement actually changes the text;
- the file **on disk** differs after writing;
- the mutated source still parses (a broken file makes every test fail for the
  wrong reason, which reads as success);
- the original is restored in a `finally`.

## E. Control matrix

| Control | Enforcement point | Failure mode | Test | Runtime protection |
|---|---|---|---|---|
| Call-site allowlist | `release_guard.enforce()` at startup | BLOCKING deployed; log locally | 22 cases | **Yes** — process refuses to start |
| Scanner failure | same | **advisory**, never `ok` | `test_32` | Degrades safely |
| One decision path | structural — `_deliver` has one caller | n/a | `test_50`, `test_70` | Enforced by construction |
| Empty-digest gate | inside `decide()` | refuse + observe | `test_10`, `test_71` | Yes |
| Recipient re-resolution | `resolve_recipient(owner_id=)` | role mailbox | `test_73`, `test_74` | Yes |
| Ledger idempotency | `UNIQUE(idempotency_key)` + CAS | skip | `test_53` — 8 threads → 1 | **Database-level** |
| `delivered` unwritable | CHECK constraint | insert fails | `test_67` | **Database-level** |
| Origin labelling | explicit session marker | live (safe direction) | `test_75` | Yes |

## F. A declaration is not an authorization

Verified that these are **not** equivalent:

```
declared call site  !=  authorized email
```

Adding an entry to `email_call_sites.py` grants nothing. A staff email still
requires, independently: a governed decision (`decide()`), an authorized
recipient (`assignable_identity`), preference allowing it, tier eligibility,
budget headroom, and a claimed ledger row. The allowlist answers only *"is this
function an approved place from which the transport may be reached"* — and
`test_74` proves the separation by showing an unauthorized `owner_id` resolving
to the role mailbox even from a fully declared call site.

## G. Stage 5 declaration design — it should need no declaration at all

The brief anticipates adding Stage 5 to the allowlist. **That would be the wrong
answer, and needing it is the signal.**

`_deliver()` is already the single governed transport boundary inside
`staff_email`. A Stage 5 reminder built where it belongs —
`staff_email.send_unclaimed_escalation_reminder()`, calling `_deliver` after its
own eligibility pipeline — adds **zero** entries to the allowlist. The list stays
at 23.

```
escalation -> claim_reminder()      eligibility INSIDE the claim (one statement)
           -> acquire() -> mark_attempted()
           -> _deliver()            the existing, already-declared boundary
           -> finish_send()
```

The rule for Stage 5 and everything after it:

> **If a new staff-email capability requires a new allowlist entry, it is in the
> wrong module.** The allowlist is a detector of misplacement, not a permission
> slip.

The only case justifying a new entry is a sender that genuinely cannot live in
`staff_email`. None is foreseen; `ceo_briefing` is the existing example, already
declared with a note that it is a candidate to fold in.

**Recipient re-resolution (brief §17) is satisfied by construction:** Stage 5
must resolve through `assignable.resolve()` and `resolve_recipient()`, which
re-derive the address from current membership. No caller-supplied address, no
stale owner id, no copied recipient string.

## H. Verdicts

```
F11 control:                    GO   — runtime boundary verified, 3 bypasses closed
F12 decision path:              GO   — structurally complete
F13 origin classification:      GO   — explicit marker, correct in both directions
Mutation methodology:           GO   — harness added after 3 silent no-ops
Stage 5 declaration:            GO   — and it requires ZERO allowlist changes
Stage 5 dry-run:                NO-GO until built; design unchanged from the first audit
Stage 5 production activation:  NO-GO — Stage 2 evidence window still not started
Stage 4 APPLY=1:                NO-GO — unchanged; both exit conditions unmet
```

### Verification

| Check | Result |
|---|---|
| Bypass matrix | 6 classes: 5 detected, 1 documented boundary |
| New tests | 8 bypass-matrix cases + `tests/mutation.py` harness |
| Full regression | **2006 passed, 1 skipped** (was 1998) |
| Allowlist | 23 sites, 23 declared, 0 undeclared, 0 stale |
| Ledger / live observations after a full run | **0 / 0** |
| Flags | unchanged — `APPLY=0`, `ESCALATION_EMAIL=0` |

---

# Production deployment + Stage 1 evidence-gate audit — 2026-08-23

**Nothing was deployed. No flag was set. No email was sent.** Deployment is a
push to a protected branch and a production build — the owner's step, not mine.
This audit establishes the three categories of truth, verifies the deployment
package, and defines the gate.

Every claim below names its evidence source.

---

## A. Deployment truth — three categories, never merged

### APPLICATION — evidence: the running build, via HTTP

```
GET /health                    200   version 2.2.0, status healthy
   scheduler                   running=True, jobs=34, started 2026-08-22T22:39:47Z
   database                    ok=True, connected_as=crm_app

GET /escalations-status        200   PRESENT   ← pre-Stage-1 code
GET /console/queue             200   PRESENT
GET /governance/queue          200   PRESENT
GET /staff-email/status        404   ABSENT
GET /staff-email/observations  404   ABSENT
GET /staff-email/digest-preview 404  ABSENT
```

**The deployed build predates Stage 1.** Endpoint presence is the only honest
proof of deployed code, and all three staff-email routes are absent.

**Finding — the deployed commit is unverifiable.** `/health` exposes
`version: 2.2.0` (a hand-maintained string) and **no commit SHA, no build
timestamp, no release id**. The brief asks for the deployed commit; the
application cannot answer. Railway injects `RAILWAY_GIT_COMMIT_SHA` — surfacing
it in `/health` is a few lines and makes deployment truth checkable rather than
inferred. Recommended, not done (out of scope for this audit).

**Topology:** one service, `pools: 1`, scheduler running *inside* the web
process (the same `/health` reports both). So web and scheduler are the same
build by construction — there is no separate worker release to drift.

### DATABASE — evidence: direct SQL against the Railway DSN

Verified by **object**, not by ledger row, because a migration can be recorded
while its object is missing:

```
table  staff_email_ledger              OK
table  staff_email_observations        OK
table  notification_tier_rules         OK
col    notification_messages.tier      OK
col    staff_email_ledger.origin       OK
col    staff_email_observations.origin OK
col    assignable_identity.preferred_channel OK
func   fn_notification_tier            OK
uniq   uq_staff_email_idem             OK
idx    ix_staff_email_breaker          OK

state CHECK excludes 'delivered'          YES
insert trigger calls fn_notification_tier YES
INSTEAD OF triggers on notifications      3 of 3

object-level failures: 0
migration ledger missing: none
```

### ENVIRONMENT — evidence: **none available, and that is the finding**

I cannot read Railway's environment variables. Nor can I infer them: pointing a
local process at the Railway DSN leaves every `os.getenv` reading *my* `.env`.

**I nearly mis-reported this.** An earlier probe printed
`enabled: True / applying: False` while connected to Railway. That was my local
shell's `STAFF_EMAIL_ENABLED=1`, not Railway's. The only trustworthy way to read
the running process's environment is an endpoint it serves — and the endpoint
that would report it (`/staff-email/status`) does not exist on that build.

**Consequence:** environment state is currently *unknowable* and also *moot* —
no deployed code reads those variables.

### LOCAL SHELL — evidence: `.env`, this working tree

```
STAFF_EMAIL_ENABLED=1   STAFF_EMAIL_APPLY=0   ESCALATION_EMAIL=0
```

These govern **my machine only** and have no bearing on Railway.

---

## B. Pre-deployment findings

**The schema is ahead of the application.** The migrations landed; the code that
uses them did not. Concretely:

| Capability | Schema present | Code deployed | Effect today |
|---|---|---|---|
| Tier stamping | ✅ (SQL trigger) | n/a — pure SQL | **Runs on next notification** |
| Stage 2 observation | ✅ | ❌ | Nothing recorded |
| Stage 3 ledger wrapping | ✅ | ❌ | Approvals email with no ledger |
| Stage 4 digest | ✅ | ❌ | No digest job exists |
| F11 startup guard | n/a | ❌ | **Never run in production** |

> **A database can contain the schema for a feature the running application does
> not implement.** That is exactly the current state, and it is why "migrations
> applied" was not the same as "Stage 3 is live".

---

## C. Deployment package — verified, not deployed

```
branch  feat/order-status-self-service-restore   (master is push-protected)
HEAD    43a9db1

modified   app/core/agent_console.py      +66/-4    Stage 0 takeover CAS
           app/core/assignable.py         +56       email_preference()
           app/core/deploy_state.py       +12       2 migrations declared
           app/core/escalation.py        +109       observe + ledger wrap
           app/core/governance.py         +72       observe + ledger wrap
           app/core/release_guard.py      +20       F11 startup check
           app/main.py                    +48       router + digest job
new        app/core/staff_email.py                  the governed path
           app/core/email_call_sites.py             the F11 allowlist

7 modified + 2 new, 368 insertions. All uncommitted.
```

**Note what else rides along:** `agent_console.py` carries the Stage 0 takeover
compare-and-swap. That is a real behaviour change to the console — correct, and
tested — but it is not staff-email, and it will go live with this deploy.

Pre-deployment gates, all green:

```
full suite                2006 passed, 1 skipped
F11 allowlist             23 sites, 23 declared, 0 undeclared, 0 stale
F11 bypass matrix         5 of 6 classes detected, 1 documented boundary
F12 decision path         _deliver has 1 caller; 0 returns precede observe()
F13 origin                explicit CRM_TEST_SESSION marker
local migrate --check     schema is current
```

---

## D. F11's first production run — a real control test, not a formality

F11 has only ever run on a laptop. The deploy will be its **first production
enforcement boundary**, and it runs *before* the scheduler starts (char 447 of
`lifespan` vs `leader.begin()` at 862), un-wrapped, so a finding kills startup.

It passes locally against the same tree that will be deployed, so an unexpected
production block would mean the deployed tree differs from this one — which is
information worth having, and **not** a reason to disable the check.

**If F11 blocks the deploy: do not disable it.** Investigate the difference.

---

## E. Tier classifier — verified, and projected against real traffic

The classifier is pure SQL and version-independent, so it can be evaluated
*before* any code ships. Projected over **30 days of real Railway traffic**
using the live rules (computed, nothing written):

```
informational   4,113   92.9%   ~137.1/day
actionable        239    5.4%   ~  8.0/day
critical           76    1.7%   ~  2.5/day
TOTAL           4,428
```

**~2.5 critical/day.** The brief asked me not to accept "single digits" as an
assumption — measured against production, it holds, with margin.

Only four event types are emailable at all:

```
invoice.overdue           actionable   ~3.8/day
lead.scored               actionable   ~3.7/day
supervisor.alert          critical     ~2.5/day
activity.overdue_flagged  actionable   ~0.5/day
```

Everything else — `activity.created` (1,278), `order.status_changed` (293),
`invoice_created` (259) — is informational, which is the correct answer.

### Two findings inside that number

**1. Every Tier 1 event carries the same title.** All 76 `supervisor.alert`
messages render as `system → supervisor.alert`. Fan-out is modest (1.6 recipient
rows per message, 3 distinct recipients), so this is not a duplication defect —
but **the only interrupt-level signal in production has no distinguishing
content**. An email whose subject is identical 76 times is one nobody reads.
This must be fixed before Tier 1 email is useful, and it is independent of every
gate below.

**2. No Tier 1 event reaches a human.** The three recipients are
`agent.notifications@system.internal`, `agent.orchestrator@system.internal` and
`admin@system.internal` — two AI service accounts and the sysadmin. All classify
as `demo_employee`; none is authorized. **Nobody is reading the supervisor
alerts today**, by email or in-app.

---

## F. Baseline, captured 2026-08-23 12:07:54 UTC

```
notification_messages total          4,732
   ...by tier                        NULL 4,732  (100% — no backfill, correct)
   window                            2026-06-22 → 2026-08-23 02:50 UTC
messages/day (last 7 full days)      506, 570, 587, 554, 546, 551, 788
distinct recipients (30d)            25
authorized identities                4  (ceo/cfo/coo/cro@agentorc.ca)
   ...with auto_email_enabled        0 of 4
staff_email_observations             0
staff_email_ledger                   0
```

**Traffic is bursty, not continuous** — nothing since 02:50 UTC, ~9 hours before
the baseline. The window must be measured in days, not hours.

Per §6: **no test event was manufactured in production.** The tier column stays
empty until real traffic arrives, which is the honest state.

---

## G. The seven-day window — and an important limit on what it can prove

The window may only be called started when **all six** hold, each verified from
the running process:

1. the Stage 1–F13 code is deployed (`/staff-email/status` returns 200);
2. that endpoint reports the environment the process actually read;
3. `STAFF_EMAIL_ENABLED=1` confirmed *in that response*, not in a shell;
4. `STAFF_EMAIL_APPLY=0` confirmed likewise;
5. `ESCALATION_EMAIL=0`;
6. `staff_email_observations` has at least one `origin='live'` row.

Record that timestamp. Seven days from **there**.

### The limit, stated plainly

Given §E-2 and the preference baseline, the window will record **almost entirely
refusals**:

- no Tier 1/2 notification reaches an authorized identity → `resolve_recipient`
  yields the role mailbox or refuses;
- 0 of 4 authorized identities have `auto_email_enabled` → `preference_off`;
- the digest will report `nothing_actionable` for all four every day.

> **A window that only ever records refusals proves the refusals work. It does
> not prove the sends are correct.**

That is a *safe* outcome, not a *complete* one. Two honest options at the end of
seven days:

- **Accept a refusal-only window** as proof of the negative case, and treat
  Stage 4's first real send as the actual test — with a canary of one recipient.
- **Enable `auto_email_enabled` for one executive** partway through, converting
  the last days into a live positive-path test while `APPLY` stays 0. This
  exercises `eligible` decisions without sending.

I recommend the second, from day 4, on one identity.

---

## H. Migration order — documented, not rewritten

```
out_of_order: True   on BOTH databases   — 4 inversions, all historical

activity_direction_enforcement.sql (#14)  applied before  memory_invariants.sql (#9)
memory_eval_instrument.sql (#18)          applied before  governed_mutation.sql (#13)
app_role.sql (#22)                        applied before  memory_audit_erasure.sql (#12)
executives_audit_and_touch.sql (#25)      applied before  metric_registry.sql (#1)
```

**Classification: known historical migration-order deviation.**

- All four predate this work; `migrate.py`'s own docstring records that
  migrations "were applied by hand, out of band."
- The two staff-email files are the **last two applied**, in correct relative
  order (`ledger` before `stage2`, which the trigger requires).
- No staff-email object depends on any inverted pair — verified by the
  object-level check in §A, which passes with 0 failures.

**Not fixed, deliberately.** Reordering the ledger would fabricate a history to
make a flag green; the schema is what it is, and the flag has been red for
reasons unrelated to this work.

---

## I. Safety verification

```
STAFF_EMAIL_APPLY        0   (local; Railway has no code that reads it)
ESCALATION_EMAIL         0   (same)
staff_email_ledger rows  0   on Railway — nothing has been contacted
observations             0   on Railway — no code to record them
outbound staff email     0
```

Test pollution (§18) cannot reach production evidence by three independent
mechanisms: `origin` defaults to `live` but is set to `test` by an explicit
session marker; production and test use different databases; and the local
`.env` cannot make a local process the production application.

---

## J. Remaining risks before Stage 4 promotion

| | Risk | Severity |
|---|---|---|
| 1 | **Every Tier 1 event has the same title** — the interrupt signal carries no content | **HIGH** — fix before any Tier 1 email |
| 2 | **No Tier 1/2 event reaches an authorized human** — the window cannot exercise the send path | **HIGH** — see §G |
| 3 | **Deployed commit is unverifiable** — `/health` exposes no SHA | MEDIUM |
| 4 | F11 has never run in production | MEDIUM — resolved by the deploy itself |
| 5 | Stage 0 console CAS ships in the same deploy | LOW — tested, but not staff-email |
| 6 | Historical migration inversions | LOW — documented, inert |

---

## K. Final recommendation

```
Deploy current code:            GO      package verified, 2006 tests green,
                                        F11 clean, F12/F13 verified

Begin seven-day observation:    GO      — but ONLY after the deploy is verified
                                        from the running process, and with the
                                        §G limit understood

Enable STAFF_EMAIL_APPLY=1:     NO-GO   the window has not started, let alone
                                        completed
```

**I cannot perform the deploy.** It requires a commit to a protected branch and
a production build. The package is verified and ready; the merge point is yours.

After deploying, the verification sequence is:

```
1. GET /health                    -> confirm it is the NEW build
2. GET /staff-email/status        -> must be 200, not 404
3. read `enabled`/`applying` FROM THAT RESPONSE — not from a shell
4. set STAFF_EMAIL_ENABLED=1 on Railway; re-read /staff-email/status
5. confirm applying=false in the same response
6. wait for the first origin='live' observation; record that timestamp
```

Step 3 is the one that matters. It is the whole lesson of this audit.
