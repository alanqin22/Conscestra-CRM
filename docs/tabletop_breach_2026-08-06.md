# Tabletop exercise — personal data breach

**Date:** 2026-08-06 · **Facilitator/participant:** Alan Qin (sole operator)
**Scenario:** privilege separation silently off in production
**Duration:** ~45 minutes · **Next exercise due:** 2027-08-06

This is the record of an exercise that was **run**, not a plan for one. The
technical steps were executed against production; the judgement steps were
walked through. Findings are at the end, and two produced code changes the same
day.

---

## Why a tabletop at all

Every other control in this system has been rehearsed against a scratch copy —
the restore into a throwaway container, leader promotion in a chaos test,
retention inside a transaction that rolls back. Breach response cannot be. You
cannot cause a breach to practise responding to one, so the first real use is
otherwise the first test, under a statutory clock, alone.

A tabletop is the only substitute: run the questions, not the incident.

---

## Scenario

> **08:15.** You open `/health` for an unrelated reason and see
> `database.connected_as: postgres`. It should be `crm_app`.
>
> The privilege separation is off. Every database-layer control — append-only
> audit, the erasure guard, the no-rewrite triggers — is bypassable by whatever
> is holding that connection. You do not know for how long, or whether anything
> used it.

Chosen because it is plausible (a restore strips grants — §4a of the restore
runbook), silent (nothing else reports it), and genuinely ambiguous: it may be a
misconfiguration with no exposure at all, or an intruder.

### Injects

| T+ | Inject |
|---|---|
| 0 min | The `/health` observation above |
| 10 min | `pg_stat_activity` shows **six** sessions, four as `postgres`, from four different addresses |
| 20 min | Two are `pgAdmin`, one is Railway's data browser. **One has no `application_name` at all** |
| 30 min | `audit_log` shows 82 writes in the last 24 h. Normal volume — or cover |
| 40 min | Decision: is this a breach of security safeguards? Notify, or record and close? |

---

## What was executed

All nine forensic queries were run against production. **Every one answered, in
0.32 s total.**

| Question (OPC report item) | Answer | Time |
|---|---|---|
| Who is connected, as what role? | 6 sessions, 4 as `postgres` | 35 ms |
| Individuals affected (item 4) | **229** (129 contacts + 100 leads) | 34 ms |
| Reachable for direct notice | **129** have email and phone | 30 ms |
| Categories of personal data (item 3) | 18 columns of email/phone/password_hash | 52 ms |
| Written during the window | 82 audit rows in 24 h | 54 ms |
| Deleted during the window | 0 | 31 ms |
| Subject data exported | 0 | 29 ms |
| Credentials exposed | 1 record | 30 ms |
| Live sessions to revoke | 0 | 28 ms |

**Assessment reached:** direct notice is *available* (129 of 229 reachable), so
under PIPEDA it is also *expected* — indirect notice is only permitted when you
cannot reach people. That is a decision the numbers make for you, and it is
better to know it in advance than to discover it at hour 20.

## Findings

### F1 — Our own tooling was indistinguishable from an intruder · **fixed**

The first question an operator asks is "who is connected?". It returned four
`postgres` sessions. pgAdmin identified itself; Railway's data browser tagged its
queries; **the connections opened by our own scripts had an empty
`application_name`** — precisely what an unauthorised session looks like.

The single query incident response depends on most could not separate our tools
from an attacker.

*Fixed the same day:* `database.py` now sets `application_name` (default
`conscestra-crm`, overridable via `PG_APPLICATION_NAME`). Note there were **two**
connect sites in that module and nearly every real connection comes from the
pooled one — patching only the direct path left every production session
anonymous while the change looked complete.

### F2 — No baseline for "normal"

Even with names attached, there is no record of what a normal connection set
looks like. "Four `postgres` sessions" is only alarming if you know the usual
number. During an incident you would be reconstructing normality from memory.

*Not fixed.* Cheapest option: have the health watchdog record the connected-role
counts it already fetches, giving a rolling baseline for free.

### F3 — The 30-day and 72-hour clocks have no timer

`dsar_subject_requests.due_at` is enforced in the schema. Breach deadlines are
not: `breach_register` records `became_aware_at` but nothing counts down from
it, and nothing alerts as the date approaches.

*Not fixed.* A daily job over open `breach_register` rows would close it.

### F4 — Every judgement step is one person

Containment, RROSH assessment, and drafting the notification are all Alan. The
technical answers arrive in 0.32 s; the decisions take hours and have no second
opinion. Legal counsel is still unidentified — and the moment counsel is needed
is the moment there is least time to find one.

*Not fixed. Not fixable by code.* Identifying a lawyer in advance is a 30-minute
task that has to happen before it is needed.

### F5 — The notification path could be part of the incident

§6 of the runbook says not to send breach notices through the CRM if the CRM is
implicated. In this scenario it would be. There is no tested alternative path
for reaching 129 people.

*Not fixed.* Exporting the contact list to a separate provider is the fallback;
it has never been exercised.

---

## What worked

- Every forensic question was answerable, quickly, without improvisation.
- `dsar --coverage` gives the scope boundary — the 34 tables holding a subject
  link — without anyone having to remember them.
- `breach_register` accepted the incident and refused to let the awareness date
  be moved.
- The runbook's PIPEDA-first ordering held up. The instinct under pressure is to
  reach for the famous 72-hour GDPR clock, which mostly does not apply here.

## Verdict

The **mechanics** are ready: the data needed for an OPC report can be assembled
in under a minute. The **judgement and communication** paths are not: one person,
no counsel, no tested out-of-band notification, no countdown on the deadline.

That is an honest place to be, and it is the opposite of where this started —
the mechanics were the unknown, and they turned out to be the strong part.

## Actions

| # | Action | Owner | Status |
|---|---|---|---|
| F1 | `application_name` on every connection | — | **done 2026-08-06** |
| F2 | Baseline connected-role counts in the watchdog | Alan | open |
| F3 | Daily job alerting on approaching breach deadlines | Alan | open |
| F4 | Identify legal counsel **before** it is needed | Alan | open |
| F5 | Test an out-of-band notification path for 129 people | Alan | open |

Re-run this exercise annually, or after any change to the privilege model.
