# Runbook — Incident triage and escalation

**Who this is for:** whoever notices first. Currently that is one person, so
"escalation" mostly means *deciding what kind of problem this is* before acting,
and knowing which clocks start running.

---

## 1. Two clocks start immediately

Before diagnosing anything, answer one question: **could personal data have been
exposed, altered, or lost?**

If yes — even *possibly* — a legal clock is running:

| Obligation | Deadline | Trigger |
|---|---|---|
| GDPR Art. 33 — notify the supervisory authority | **72 hours from awareness** | any personal data breach unless unlikely to risk rights and freedoms |
| GDPR Art. 34 — notify affected individuals | without undue delay | high risk to those individuals |

**"Awareness" starts now, not when you finish investigating.** The 72 hours run
while you diagnose. If the answer is "possibly", write down the time you noticed
before doing anything else — you will not reconstruct it accurately later.

Personal data lives in the 34 subject-linked tables listed by
`python -m app.core.dsar --coverage`. Anything touching those is in scope.

## 2. Classify

| Severity | Looks like | Response |
|---|---|---|
| **SEV1** | data exposed to the wrong party, data lost, or the app writing wrong values to customer records | stop writes first, diagnose second |
| **SEV2** | customer-facing function broken (checkout, chat, email) | fix forward or roll back |
| **SEV3** | background work stopped, no customer-visible symptom | [runbook_leader_failure.md](runbook_leader_failure.md) |
| **SEV4** | degraded but correct (slow, partial results) | normal work queue |

**SEV1 acts before it understands.** Everything else understands before acting.

## 3. First five minutes

```powershell
$h = curl -s https://<app>/health | ConvertFrom-Json
$h | Select-Object status, @{n='ha';e={$_.ha.role}},
                   @{n='db';e={$_.database.connected_as}}
$h.scheduler; $h.connections
```

| What you see | What it means |
|---|---|
| HTTP 503 | the database query failed — the app cannot serve. Start at the DB |
| `database.connected_as` = `postgres` | **the privilege separation is off.** The app should connect as `crm_app`. Treat as SEV1: every database-layer control is bypassed |
| `ha.role` = `follower` everywhere | nobody runs background jobs → [runbook_leader_failure.md](runbook_leader_failure.md) |
| `scheduler.last_tick` stale | jobs frozen; same runbook |
| `connections.utilisation` near 1.0 | pool exhausted; requests are queueing |

## 4. The failures this system actually has

Every one of these was real, and every one of them **looked like normal
operation**. When something is wrong but nothing looks wrong, start here.

| Symptom reported | Actual cause |
|---|---|
| "Coupons don't work" | table missing on Railway; every valid code answered "no such coupon" for 15 days |
| "Nothing has run for days" | rolling deploy left every process a follower |
| "Reports look wrong" | 22 of 28 scheduler jobs never registered |
| Metrics all green, behaviour wrong | four observability metrics could not fail |
| Backup "succeeded" | RTO printed for a restore that never ran |
| Inbox flooded after a repair | bulk update fired row triggers — needs `SET LOCAL app.suppress_events='notify'` |

**The pattern:** a signal that cannot fail. When a check says everything is
fine and the behaviour disagrees, suspect the check.

## 5. Decide: roll forward or back

Roll **back** when the last deploy is the likely cause and the previous build
was known good. Railway keeps prior deploys — redeploy the last green one.

Roll **forward** when the cause is data or configuration rather than code. A
redeploy does not fix a missing migration, and rolling back a schema change that
already ran can make things worse.

**Restoring the database is a different decision entirely** — it discards
everything written since the dump. See [runbook_restore.md](runbook_restore.md),
including §4's warning that the production restore path has never been executed.

## 6. Before declaring it resolved

```powershell
python -m scripts.postdeploy_verify --target railway --app-url https://<app>/health
```

That runs secrets, DB invariants, the red team, DSAR coverage and schema drift.
Exit 0 is the bar. Without `--app-url` the red team judges the admin connection
this check uses rather than the app's role and will report an expected breach.

Then confirm the specific thing that broke actually works — not that the health
check is green. The health check was green throughout the ten-day outage.

## 7. Write it down

For anything SEV1 or SEV2, record in `skills.md`:

- when you noticed, and how
- what the *first* wrong signal was, and what it said instead
- why existing checks missed it
- the fix, and the guard that makes the same failure loud next time

That last line is the point. Most incidents here were not caused by a missing
fix — they were caused by a missing signal. A postmortem that ends at the fix
leaves the detection gap in place for the next one.

## 8. Gaps

- **One person.** There is no rota and no second pair of eyes. Every hour of an
  incident is an hour of that person's availability.
- **No paging.** Detection currently depends on someone looking. An uptime
  checker on `/health` is the smallest change that alters this.
- **No status page.** Customers find out by trying.
- **No breach-notification template.** If §1 ever fires, drafting starts from
  nothing against a 72-hour clock.
