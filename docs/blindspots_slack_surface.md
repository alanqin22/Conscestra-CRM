# Blindspots — Internal Chat Surface (Slack / Teams)

> Source: 2026-07-23 blind-spot pass triggered by Salesforce's **Slack** product
> page ("Slack is where the Agentic Enterprise works · Slackbot can now do
> anything Salesforce can — just ask"). This is a DIFFERENT thesis from the
> Agentforce pass in `skills.md`: not "an AI agent," but **agents as authorized,
> proactive participants in shared team channels, with in-thread approvals.**
> Measured against the actual code (`app/core/transports.py`), the surface already
> existed (Phase 3 of [Unified Comms]) but was **fail-open, proactively inert, and
> in violation of Slack's delivery contract.** Ordered by (value × cheapness).

## The meta-blindspot

Unified-Comms Phase 3 shipped `capture_slack` / `capture_teams` + an orchestrator
answer, and it was logged as **done**. "An employee can ask the bot a question" is
the *shallowest* reading of the Salesforce page. Treating the surface as finished
was itself the blind spot: it answered "from every module" with **no authorization,
fail-open signature verification, no audience scoping, and a synchronous handler
that breaks Slack's 3-second contract.**

## Findings (ranked) — security/reliability half SHIPPED

| # | Gap | Why it matters | Status |
|---|-----|----------------|--------|
| 1 | **Fail-OPEN authorization** — `/slack/events` & `/teams/messages` answered from every module with no check that the user maps to an authorized employee | Anyone the bot could hear got full-CRM answers. Voice had `allowed_callers`; chat had nothing | **✅ DONE 2026-07-23** |
| 2 | **Verification fails open / Teams none** — `_slack_verify` returned `True` with no secret; router mounted PUBLIC; Teams validated no JWT | Unauthenticated internet POST → CRM data | **✅ DONE 2026-07-23** |
| 4 | **Slack 3-second contract violated** — full orchestrator round-trip (120s timeout) ran *before* the 200 | Slow LLM → Slack retries (dup answers) → eventually **auto-disables the subscription** | **✅ DONE 2026-07-23** |
| 3 | **Channel-audience leakage** — a linked employee's "what's our pipeline?" in a shared channel broadcast exec-tier data to everyone in the room | No notion of scoping an answer to a channel's audience | **✅ DONE 2026-07-23** |
| 7 | **No rate limit** — every inbound fired the metered orchestrator; no per-user cap | A chatty channel / abusive sender = uncapped LLM spend | **✅ DONE 2026-07-23** |
| 6 | **No interactivity surface** — no slash commands, no Block Kit buttons; approvals arrive as plaintext Approve/Reject URLs that bounce you *out* to a web page | Slack's flagship is the in-thread approve button; and it's one monolith, not `@AgentName`-addressable | **✅ DONE 2026-07-23** |
| 5 | **Proactive in-channel work is inert** — `channel_selector` maps `internal_alert`/`internal_briefing` → slack/teams, but no executor posts them; only governance approvals push (to a DM, not a channel) | The screenshot's headline behavior — agents showing up on their own | **✅ DONE 2026-07-23** |
| 8 | **Teams is receive-only** — replies always drafted ("no Bot Framework connector") | The loop never closes — half a channel | **✅ DONE 2026-07-23** |

Not a finding (verified handled): bot-echo loop guard (`bot_id`/`subtype` filter).

## What shipped (commit `f88f57d`, `app/core/transports.py`, no migration)

Fail-closed on four axes + abuse control. All verified end-to-end on isolated ports.

- **#1 Authorization (fail-closed).** `_authorize_internal()` resolves the platform
  id via `identity.resolve`; only a **linked employee** reaches the CRM. Unknown /
  unlinked ids get a refusal + linking hint and NEVER touch the CRM (no read, no
  LLM) — the attempt is still threaded for audit. Resolver exceptions fail closed.
- **#2 Authentication (fail-closed).** `_slack_verify` REFUSES when
  `SLACK_SIGNING_SECRET` is unset (was dev-permissive) + a 5-min replay window;
  new `_teams_verify` requires a shared bearer secret (`TEAMS_INBOUND_SECRET`,
  interim until a real Bot Framework JWT validator). One explicit
  `TRANSPORTS_DEV_INSECURE` override for local work.
- **#3 Audience scoping.** Surface-based (no content classifier to leak on a miss):
  a DM or a `SLACK_OPEN_CHANNELS`-listed channel is answered in place; **any other
  shared channel → ephemeral to the asker**. Delivery fails closed — an ephemeral
  failure DRAFTS, never a public post. Teams records the equivalent scope for its
  future connector.
- **#4 Slack 3s contract.** ACK immediately, answer in a `BackgroundTask`, drop
  `X-Slack-Retry-Num`. A slow LLM can no longer produce duplicates or auto-disable.
- **#7 Abuse/cost.** Per-(channel, user) in-process sliding window BEFORE any
  DB/LLM work; returns **200 (not 429)** so Slack doesn't retry-then-disable.

New env, all defaulting to the secure behavior: `SLACK_SIGNING_SECRET`,
`TEAMS_INBOUND_SECRET`, `SLACK_OPEN_CHANNELS`, `TRANSPORTS_RATE_LIMIT`. Deploy note:
link employees' ids via `POST /identity/link` (admin) or the surface correctly
refuses everyone; do NOT set the two `*_DEV_*` / `*_INSECURE` flags in prod.

## Participation half SHIPPED (`transports.py` + `governance.py` + `ceo_briefing.py`)

The product decision was made (internal chat = strategic), so #5/#6/#8 shipped.
Verified end-to-end on an isolated port.

- **#6 In-thread approvals + participation.** `POST /slack/interactive` handles
  Block Kit `block_actions`: signature-verified, then FAIL-CLOSED on identity (only
  a linked employee can decide) AND on the same HMAC (approval, action) token the
  one-click email links use. ACKs in <3s and applies the decision in the background
  through the SAME `governance.approve`/`reject` flow, updating the message in place
  via `response_url`. `governance._deliver_approval_chat` now sends native
  Approve/Reject buttons (link-bearing text as fallback). Plus **mention-gating**:
  in a shared channel the bot answers only when `@mentioned` (a DM always answers) —
  a well-behaved channel participant, not a monologue on every message.
- **#5 Proactive posting.** `transports.post_internal(kind, text)` broadcasts an
  alert/briefing INTO a channel (`SLACK_ALERTS_CHANNEL` / `SLACK_BRIEFING_CHANNEL` /
  `SLACK_DEFAULT_CHANNEL`); draft-first (`SLACK_PROACTIVE_ENABLED` + a bot token).
  Wired to a real producer — the CEO briefing (`ceo_briefing.send_briefing`) now
  also posts to Slack — and a dry-run-default admin trigger `POST /comms/announce`.
- **#8 Teams outbound.** `_teams_post` replies via the Bot Framework connector
  (client-credentials token, cached) when `MICROSOFT_APP_ID/PASSWORD` are set; a 1:1
  (personal) chat is answered in place, while channel/group replies stay WITHHELD
  (no ephemeral equivalent → the audience-scoping guarantee holds for Teams too).

Bug caught by live testing (fixed): the interactive background task originally made
synchronous `httpx`/DB calls on the event loop, stalling the single worker (a
response_url POST could deadlock against the same server). Now offloaded via
`asyncio.to_thread`, the same pattern as `_slack_answer_async`.

Deliberately deferred: `@AgentName` routing to specific named custom agents (the
mention is currently a gate, not a router); a Bot Framework private/proactive Teams
message so channel replies can deliver 1:1 instead of being withheld.

## Execution log

- **2026-07-23** — Pass recorded off the Salesforce Slack page. Shipped the
  security/reliability half in one commit (`f88f57d`): #1 authz, #2 auth, #3
  audience, #4 async ack, #7 rate limit. Pure app code, verified live (authz
  refusal with zero CRM read; ~0.1s ACK + async answer; scope = ephemeral /
  channel / dm; rate-limit 200-drop before orchestrator). #5/#6/#8 deferred as
  product-direction calls. Relates to [Unified Comms] and `skills.md` (Agentforce).
- **2026-07-23** — Product call made (internal chat = strategic); shipped the
  participation half #6 (in-thread Block-Kit approvals + mention-gating), #5
  (proactive posting + CEO-briefing producer + `/comms/announce`), #8 (Teams Bot
  Framework outbound). Verified live: approval decisions applied through governance
  (tamper→pending, unlinked→refused, valid→decided), mention-gating (channel
  no-mention ignored / @mention answered / DM answered), proactive drafted when
  disabled, Teams drafted without creds. Fixed an event-loop-blocking bug in the
  interactive handler (sync calls → `asyncio.to_thread`). **ALL 9 items in this
  pass now closed.** Pure app code + no migration; new env documented in
  `transports.py`. Wiring: `main.py` mounts `transports.admin_router` (admin-gated).
