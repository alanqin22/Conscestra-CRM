# Scaling & Concurrency — the honest model (blindspot #7)

> Status doc for how Conscestra CRM runs today, where the ceilings are, and the
> path past each one. Written 2026-07-23 as part of the Agentforce-parity
> blindspots pass (see `skills.md`). The point of #7 is not to claim
> "enterprise scale" — it's to make the real limits **known, bounded, and
> planned** instead of surprising.

## What runs today

A **single FastAPI application** (`app/main.py`), typically one process, plus
three **in-process background singletons** started in the `lifespan` handler:

| Singleton | What it does | Module |
|-----------|--------------|--------|
| APScheduler | Daily jobs at 22:xx ET (pipeline/order advance, seeds, dunning + hot-lead emits) + the supervisor tick | inline in `app/main.py` |
| IMAP poller | Autonomous inbound-email auto-reply loop | `app/agents/email/imap_poller.py` |
| agent-bus consumer | Drains `event_queue`, runs cooperation handlers | `app/core/agent_bus.py` |

Request handlers reach Postgres through `app/core/database.py`.

## The two real ceilings

### 1. Background singletons are not multi-process safe — **FIXED (leader election)**
Running more than one worker/replica used to mean each one fired the scheduler,
polled the mailbox, and drained the bus — duplicate dunning emails, double-booked
meetings, racing writes. As of 2026-07-23 (`app/core/leader.py`) a **Postgres
session-level advisory lock** elects exactly **one leader** cluster-wide; only the
leader runs the three singletons, every other process serves HTTP only. This is
what makes horizontal scaling of the web tier safe.

- Config: `HA_LEADER_ELECTION=1` (default; `0` = always-leader single-process
  dev), `HA_LOCK_KEY=871123`.
- Observability: `GET /health` → `ha: { role, runs_singletons }`
  (`leader` | `follower` | `standalone`).
- **To scale the web tier:** run N replicas/workers pointed at the same
  Postgres. One becomes `leader`, the rest `follower`. No duplicate background
  work.
- **Follow-up (documented, not yet built):** zero-gap automatic failover — a
  live follower promoting itself the instant the leader dies, without a restart.
  Today the lock frees on leader death and the next process to (re)start and win
  the lock becomes leader (orchestrator-restart failover). Closing the gap needs
  the singleton-start logic refactored into a restartable callback + a follower
  watchdog that calls it on lock acquisition.

### 2. Database connections are opened **per call — there is no pool** (the #1 throughput bottleneck)
`database.get_connection()` does `psycopg2.connect(...)` on **every** call and the
caller closes it. That's simple and correct, but under real concurrency it means
a fresh TCP + auth handshake per query and pressure on Postgres `max_connections`
— the first thing that will bend under load.

**The zero-code fix: put a transaction pooler in front of Postgres and point
`DB_DSN` at it.** No application change required:
- Railway/Supabase: use the **pooled connection string** (Supabase "Transaction"
  pooler / PgBouncer) instead of the direct one.
- Self-hosted: run **PgBouncer** in `transaction` pooling mode; set `DB_DSN` to it.

**The code fix (larger, later):** replace `get_connection()` with a
`psycopg2.pool.ThreadedConnectionPool` and a context manager that returns
connections to the pool instead of closing them. This touches every call site
(~170 files call `get_connection`/`execute_sp`), so it's a deliberate refactor,
not a quick change — the pooler above buys the same win first with none of the
risk.

## Other known limits (tracked)

- **Railway volume ceiling:** the DB volume has run near-full (~433 MiB); never
  `VACUUM FULL` it (crash-loops recovery on No-space). Resize or
  TRUNCATE-and-restore. (See memory `feedback_railway_vacuum_full`.)
- **`event_queue` backlog:** a ~12k-row backlog was found and drained during the
  Railway cutover; the bus consumer's heartbeat detector alerts if it dies
  silently. With leader election the consumer now runs on exactly one process.
- **SSE connections** (`/notifications/stream`, the live console) each hold a
  connection and poll the DB every few seconds (`SSE_POLL_SECS`); at high client
  counts raise the poll interval or move to `LISTEN/NOTIFY`.

## Scaling path, in order of leverage

1. **Point `DB_DSN` at a transaction pooler.** Biggest win, zero code. Do this
   before adding replicas.
2. **Run N web replicas** (now safe via leader election). Scales request
   throughput; background work stays single-owner.
3. **Externalize the bus / scheduler** if background throughput becomes the
   limit — move the singletons into a dedicated worker process (the leader lock
   already lets you run exactly one), or adopt a real queue.
4. **Connection pool in-process** (the `get_connection` refactor) if you can't
   run an external pooler.
5. **Zero-gap failover watchdog** for the singletons (the #1 follow-up above).
