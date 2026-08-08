# Runbook — Background jobs stopped running

**Symptom:** scheduled work silently stops. No errors, no 500s, no alerts — the
API keeps answering perfectly. Orders stop advancing, dunning stops, digests
stop, the agent bus stops draining.

**This has happened.** It ran for ten days in July 2026 before anyone noticed,
because *nothing about it is visible from the outside*. A health check that only
asked "does the API respond?" answered yes throughout.

---

## 1. What runs on exactly one process

Three in-process singletons, guarded by a Postgres session-level advisory lock
(`app/core/leader.py`, key `HA_LOCK_KEY`, default `871123`):

- APScheduler daily jobs (28 registered)
- the IMAP auto-reply poller
- the agent-bus consumer

Exactly one process cluster-wide holds the lock and runs them; every other
process serves HTTP only. If the leader dies its session ends and Postgres
releases the lock automatically.

## 2. Diagnose — one request

```powershell
$h = curl -s https://<app>/health | ConvertFrom-Json
$h | Select-Object status, @{n='ha';e={$_.ha.role}},
                   @{n='db';e={$_.database.connected_as}}
$h.scheduler
```

Read these five fields:

| Field | Healthy | What it means when wrong |
|---|---|---|
| `ha.role` | `leader` on exactly one process | `follower` **everywhere** = nobody runs the jobs. `unelected` = election never completed. `standalone` = `HA_LEADER_ELECTION=0` |
| `scheduler.running` | `true` on the leader | started but not running = APScheduler died |
| `scheduler.jobs` | `33` (as of 2026-08-07) | fewer means jobs failed to register |
| `scheduler.last_tick` | within the last hour | stale = the scheduler is present but frozen |
| `scheduler.overdue_jobs` | `[]` | a job whose last scheduled fire time has no run behind it — see §3D |

`ha.runs_singletons` is the boolean form of `ha.role == "leader"`.

`scheduler.started_at` and `scheduler.last_job` narrow down *when* it stopped.

**The decisive one is `last_tick`.** `running: true` only means the object
exists. A scheduler that is running and never ticking looks identical to a
healthy one in every field except this.

## 3. The four ways this fails

### A. Everyone is a follower — nobody runs anything

The most likely cause and the one that caused the outage. A rolling deploy
starts the new container before the old one exits, so the new process tries to
elect, loses to the still-running old leader, gives up, and becomes a follower
for life. The old container then exits. Nobody holds the lock, nobody is
running the jobs, and both processes report healthy.

**Check:**

```sql
SELECT pid, state, application_name, backend_start
  FROM pg_locks l JOIN pg_stat_activity a USING (pid)
 WHERE l.locktype = 'advisory' AND l.objid = 871123;
```

Zero rows with the app up = orphaned. **Fix: restart the app service.** Election
is synchronous at startup, so a restart re-elects.

Mitigations already in place: `HA_ELECTION_RETRY_SECONDS` (default 45) keeps a
losing process retrying long enough to outlast a rolling deploy, and followers
poll for promotion every `HA_WATCH_INTERVAL` (default 10 s) rather than
accepting follower status for life.

### B. A stale lock nobody released

A killed process whose TCP session lingers can hold the advisory lock. The query
above shows a `pid` whose `backend_start` predates the current deploy.

```sql
SELECT pg_terminate_backend(<pid>);   -- releases the lock; a follower promotes
```

Confirm the pid is genuinely dead first. Terminating the **live** leader causes
the outage you are trying to fix.

### C. The scheduler never registered its jobs

`scheduler.jobs` below the expected count (33 as of 2026-08-07 — recount with
`grep -c '_scheduler.add_job' app/main.py` rather than trusting this number, it
grows). A registration exception at startup skips the rest silently. This was a
real bug: `scheduler.add_job` vs `_scheduler.add_job` meant **22 of 28 jobs
never registered** while the app reported healthy.

Check the deploy logs for `[scheduler]` at startup. Fixing needs a code change,
not a restart.

### D. Everything above is healthy and a job still did not run

Alert text: `N scheduled job(s) missed their last due time`.

A restart is the usual cause. APScheduler's jobstore here is **in memory**, so
every process start recomputes each `next_run_time` from *now*: a fire time that
elapsed while the process was down is never made up, and `misfire_grace_time`
does not apply because it forgives a late *live* scheduler, not an absent one.
Nothing in §2 can see this — `running`, `jobs` and `last_tick` are all correct
afterwards, because they describe the machinery rather than the work.

Railway restarted this service at 03:30 UTC on 2026-08-06 and again on
2026-08-07. Either one landing in the 21:45–23:45 nightly window would have
skipped the whole batch for that day.

**Check:**

```sql
SELECT job_id, status, started_at, detail
  FROM scheduled_job_runs
 WHERE started_at > now() - interval '48 hours'
 ORDER BY started_at DESC;
```

`/health` carries the same answer under `scheduler.overdue_jobs`, refreshed
hourly by the `job_ledger_audit` job, plus `scheduler.repaired_24h` for misses
that were re-run automatically and `scheduler.failed_jobs_24h` for jobs that
raised.

**A job repaired every single night is a restart problem, not a success.** Look
at the platform's restart history rather than the job.

If `/health` reports `scheduler.ledger: unavailable`, the ledger table is
missing and nothing is being recorded — apply `sql/job_ledger.sql`. The app
cannot create it: `crm_app` has no CREATE on `public` (see
`scripts/verify_runtime_ddl.py`).

## 4. Recover

```powershell
# 1. Confirm which failure mode (§3) before acting.
# 2. Restart the app service in Railway.
# 3. Verify a leader now exists:
curl -s https://<app>/health | ConvertFrom-Json | Select-Object ha
# 4. Verify the scheduler is ticking (wait for one interval):
curl -s https://<app>/health | ConvertFrom-Json |
    Select-Object -ExpandProperty scheduler
```

Then catch up on what was missed. Since 2026-08-07 the leader does this **for
itself** at startup and on promotion: `app/core/job_ledger.py` compares each
cron job's last recorded run to its previous fire time and re-runs what was
skipped. Its limits are deliberate, and what falls outside them is still yours:

- only misses newer than `JOB_CATCHUP_GRACE_MIN` (default 6 h) are re-run — an
  older one acts on a stale view of the world
- at most `JOB_CATCHUP_MAX_JOBS` (default 6) per boot, so recovering from a long
  outage does not stampede the database
- interval jobs are never caught up; they fire again within their own interval
- a job the ledger has never seen records a `baseline` row **instead of
  running**, so a first deploy does not fire all 27 cron jobs at once

Anything it declined is in `scheduler.overdue_jobs` and in the log as
`[JobLedger] catch-up`. Check the event backlog too, and remember to suppress
row triggers on any bulk repair or every affected inbox floods:

```sql
SET LOCAL app.suppress_events = 'notify';
```

## 5. Prevention

- **`HA_LEADER_ELECTION=1` must be set on every replica.** A replica without it
  is always leader, and two leaders means duplicate dunning emails and
  double-booked meetings — worse than none.
- Point an uptime checker at `/health`. It returns **503** when the database
  query fails, so a plain HTTP monitor catches that much. `last_tick` staleness
  needs a content check, and it is the field that would have caught the ten-day
  outage.
- Prefer stop-then-start over a rolling deploy on a single replica, which is the
  configuration that produced failure mode A.
- `scripts/health_watch.py` confirms a failure with a re-check
  (`HEALTH_CONFIRM_RETRIES`) before mailing, because a restart that resolves in
  30 seconds used to page. Suppressed blips are counted, and
  `HEALTH_BLIP_ALERT_COUNT` (default 3) in 24 h alerts on its own — repeated
  restarts are a real problem even when each one self-heals.
- That watcher runs as a Windows scheduled task on a workstation that sleeps, so
  it reports `since_last_check_min` and warns above `HEALTH_MAX_GAP_MIN`. **A
  quiet night is not evidence of a healthy night.** The ledger in §3D is what
  covers those hours: it persists to the database and is still readable the next
  morning.

## 6. Not verified in production

Leader promotion — a follower detecting a dead leader and taking over without a
restart — has been exercised **locally only**. Production runs a different
worker count and different timing, and worker count is precisely what decides
whether `begin()` fails open or closed. Treat §3A's restart as the reliable
remedy and promotion as a convenience that may or may not fire.
