# Conscestra CRM — Capability Blindspots & Roadmap

> Source: 2026-07-23 blindspots pass comparing Conscestra against Salesforce
> Agentforce (salesforce.com/ca/agentforce). These are gaps confirmed against
> the actual codebase — features Agentforce treats as first-class that
> Conscestra does not yet have. Ordered by (value × cheapness given our
> architecture). Update `Status` as items ship.

## Where we're already ahead (don't rebuild these)

Four-layer guardrails, independent critic on every approval, correlation
tracing (proposal → approved execution), unified cross-channel memory,
DB-enforced read-only channels, self-tuning-with-ratification, per-agent LLM
cost metering. Agentforce either buries these in add-ons or lacks them.

---

## Blindspots (ranked)

| # | Gap | Why it matters | Fix leverage | Effort | Status |
|---|-----|----------------|--------------|--------|--------|
| 1 | **Live human-agent takeover console (agent-assist)** | Our HITL is async approval only. No live rep picks up a conversation mid-stream with transcript + AI-suggested replies. Every contact centre needs "AI handles 80%, warm-transfer the hard 20% to a human with context." | Reuse SSE `/notifications/stream` + `conversations.py` + unified envelope. We have the plumbing, missing the human seat. | Medium | **✅ DONE 2026-07-23** |
| 4 | **Agent-program operations analytics** (containment / deflection / resolution / escalation / CSAT trend / cost-per-resolved-conversation) | The scorecard a buyer of an *AI agent platform* clicks before trusting it. We have BUSINESS KPIs + internal learning briefing, but no agent-fleet-as-service-operation view. | Assembly job on data we already collect: `llm_meter`, CSAT proxy in `ceo_briefing.py`, sentiment, conversation objects. | Medium | **✅ DONE 2026-07-23** |
| 2 | **Multilingual — especially French** | Confirmed English-only. We're a Toronto-HQ Canadian company; French is a market/compliance expectation AND a differentiator Agentforce can't claim as uniquely ours. | Language detection on inbound envelope → locale-scoped KB retrieval + response-language instruction. KB tiering proves we can partition retrieval; add a language axis. | Medium | **✅ DONE 2026-07-23 (text channels)** |
| 3 | **No-code agent/topic authoring for business admins** | "Playbooks as data" covers cadences + existing capabilities, not authoring NEW agent behavior. A new agent/intent still needs a dev writing prompt/pre_router/graph. Difference between "a platform" and "a codebase one team maintains." | Registry + intent router + playbooks-as-data are the substrate. A thin authoring UI writing validated registry rows + prompt fragments. | Large | **✅ DONE 2026-07-23** |
| 5 | **Employee-facing / internal service** (IT helpdesk, HR onboarding) | Literally the whitespace in Agentforce's hero (Employee Service, IT Service). Our fleet is 100% customer + executive facing. An entire second market that reuses our governance + KB + memory stack. | New agent package + internal-audience KB tier (audience tiering already exists). | Medium | **✅ DONE 2026-07-23 (as #3 rows, not code)** |
| 6 | **Distributable channel SDK / embeddable widget + mobile in-app** | Agentforce sells a drop-in web SDK + mobile in-app messaging customers deploy on THEIR properties. Our store bubble targets our own backend only. | Packaged, tenant-keyed delivery surface over existing agent brains. Only relevant if we productize for others. | Large | **✅ DONE 2026-07-23 (web widget)** |
| 7 | **Scale / HA / concurrency posture** | Single FastAPI server, in-process event bus, one pool. Agentforce headline is "enterprise scale, 24/7." Already hit Railway volume ceiling + found 12k event_queue backlog — symptoms of the ceiling. | Document concurrency model + limits now; externalize the bus longer-term. | Large | **✅ DONE 2026-07-23 (leader election + doc)** |
| 8 | **Compliance & data-residency as a SOLD feature** | We have CASL/GDPR consent + PII masking + audit trails, but don't formalize Canadian data residency (a real edge vs US Salesforce), SOC 2 posture, or model-provider zero-retention. | Mostly documentation + one architectural claim (residency). Turns existing strengths into a sellable compliance story. | Small | **✅ DONE 2026-07-23** |
| 9 | **Testing at scale — synthetic utterance gen + pre-deploy regression gate** | Nightly evals (5 golden scenarios) + RAGAS-lite are good, but Agentforce Testing Center batch-runs hundreds of synthetic variations and gates deploy. We have 5 scenarios, no synth-gen, no pre-merge gate. | Generate utterance variations off KB/intents → run through the eval harness we already built → wire as CI gate. | Medium | **✅ DONE 2026-07-23** |

## Explicitly out of scope (decisions, not oversights)

- **Field Service** (technician dispatch/scheduling/mobile) — we have meeting booking, not dispatch. Not our market.
- **AgentExchange-style marketplace** of prebuilt actions/templates — only matters at platform scale.
- **Full multi-tenancy** — only if we sell to multiple orgs.

---

## Execution log

- **2026-07-23** — Blindspots pass recorded. Starting #1 (live human takeover
  console) and #4 (agent-ops analytics): both are assembly jobs on existing
  plumbing (SSE, conversations, llm_meter, sentiment) and both are what an
  evaluator looks for first.
- **2026-07-23** — **#1 and #4 SHIPPED** (smoke-tested green locally). New code:
  - `app/core/agent_console.py` — queue / takeover / release / suggest_reply
    (LLM agent-assist, KB-grounded, metered) / send_reply (outbound-guard
    screened, delivers on email+SMS, threads everything). `is_human_handled()`
    stands the AI down while a rep holds the wheel.
  - `app/core/agent_ops.py` — `/ops/agent-metrics` (containment, escalation,
    CSAT proxy, cost/conversation, daily trend, by-channel).
  - `sql/agent_console.sql` — adds handling / assigned_to / assigned_at /
    escalated to `conversations` (+ index). **Railway needs this migration.**
  - `agent-console.html` (live rep console) + `agent-ops.html` (dashboard),
    both added to `_CHAT_PAGES`; routers wired in `app/main.py` under `_ADMIN`.
  - `app/agents/email/auto_reply.py` — suppression hook: no auto-reply while a
    conversation is human-handled.
  - Bug fixed during test: system-note append no longer rolls the conversation's
    live `channel` to 'system' (was corrupting queue display + send routing).
  - **Railway cutover TODO**: apply `sql/agent_console.sql`; the two HTML pages
    deploy with the agentorc.ca frontend (user deploys HTML themselves).
  - Not yet wired: takeover surfaces for SDR web-chat / voice are record-only
    (no live push adapter) — email + SMS deliver; webchat threads only. A future
    pass can add a webchat push channel. Suppression hook currently covers the
    email auto-reply (the main autonomous responder); SDR/voice run their own
    deterministic state machines and can gain the same `is_human_handled` check.
- **2026-07-23** — **#2 (multilingual / French) SHIPPED for text channels**
  (smoke-tested green: French/English/Spanish/German detection 12/12; live LLM
  replies confirmed French on SDR chat, email auto-reply, and console co-pilot).
  - `app/core/language.py` — deterministic stopword+diacritic detector
    (fr/es/de/en, defaults en on weak signal; no dependency, no LLM cost;
    `MULTILINGUAL_ENABLED` kill switch) + `respond_in(text)` reply-language
    directive. French (fr-CA) first-class.
  - Wired into the customer-facing TEXT responders: email `auto_reply`,
    `sdr._llm_reply`, SDR catalog + store-agent composes, and
    `agent_console.suggest_reply`. The customer's language is detected from
    their own text; the model replies in it, translating the SUBSTANCE of the
    same approved (English) knowledge — governance/approval unchanged.
  - **No migration, no schema change** — detection is stateless per message.
    Deploys as pure app code (nothing for the user to run on Railway).
  - **Deferred follow-ups** (noted, not blocking):
    1. *Voice* left English on purpose — a French reply spoken by an English
       TTS voice sounds broken. Real voice i18n = switch the carrier `<Gather>`
       STT recognition language + the TTS voice per detected/declared language.
    2. *KB language axis* (a `lang` column on `knowledge_articles`, prefer
       same-language with English fallback) — inert today (zero French
       articles); the LLM translates approved-English substance meanwhile.
       Add the column + two-pass retrieve once French articles are authored.
    3. *Preferred-language as a stored customer signal* (like preferred
       channel/hour in intelligence.py) — currently detected per-message,
       which is robust; persist it later for proactive outreach language.
    4. *UI/frontend localization* (the *-mgmt.html + store) is the larger
       "#2 cousin" — separate effort; this pass is the conversational win.
- **2026-07-23** — **#3 (no-code agent authoring) + #5 (employee/IT internal
  service) SHIPPED** (validated in-process; HTTP routes pending a clean server
  restart — see note). #5 is built AS #3 (agents are data rows, not code).
  - `app/core/custom_agents.py` — data-defined agents: CRUD + a SAFE-BY-DEFAULT
    runtime (grounded in approved KB on the granted tier, NO tools, NO CRM
    access, NO writes; PII-masked input; outbound-guard screened; replies in the
    customer's language via [[project_blindspots]] #2). Authoring router is
    admin-gated; the chat router self-gates — `internal` agents require a
    signed-in session (internal knowledge never reaches anon), `external` are
    public + rate-limited.
  - `sql/custom_agents.sql` — `custom_agents` table (slug/display_name/
    description/instructions/scope/kb_audience/examples/enabled).
  - `agent-studio.html` — no-code authoring UI (list/create/edit/delete +
    live test panel); added to `_CHAT_PAGES`, routers wired in main.py.
  - `sql/employee_service_seed.sql` — 8 internal IT+HR KB articles
    (audience='internal') + the `it-service` and `people-hr` agent rows. #5
    delivered without a bespoke package.
  - **Validated in-process:** IT agent answers phishing/VPN from internal KB;
    HR agent answers a FRENCH question from internal HR KB; a runtime-authored
    external "Returns Assistant" answers from public KB; and — the key safety
    check — a PUBLIC-audience agent asked an IT question retrieves NOTHING
    internal (no VPN/password leak, defers to a human). Internal-tier isolation
    holds at retrieval.
  - **Railway needs:** `sql/custom_agents.sql` + `sql/employee_service_seed.sql`
    (the seed also needs `sql/kb_enrichment.sql`'s `audience` column). The HTML
    page deploys with the agentorc.ca frontend (user deploys HTML).
  - **⚠ HTTP retest pending:** the local dev server that was running predates the
    main.py edit and there is no auto-reload (reload=settings.debug=False), so
    `/custom-agents` 404s until a FRESH `python main.py` binds the port. The
    routes are confirmed registered in-process; the plumbing is identical to the
    already-working console/ops routers.
  - **Deferred follow-up:** orchestrator/intent-router AUTO-routing to custom
    agents (today they're directly addressed via the studio + chat endpoint).
    Add enabled custom agents into `intent_router.AGENTS` so "give it a goal"
    reaches them. Bounded that out of this pass to keep the hot routing path
    low-risk.
- **2026-07-23** — **#6 (distributable widget SDK) + #7 (scale/HA) SHIPPED**
  (validated on isolated ports 8001/8002; leader/follower proven with 2 procs).
  - **#6** `app/core/embed.py` + `sql/embed_keys.sql` + `widget-demo.html`:
    a one-tag embeddable chat widget —
    `<script src="…/widget.js" data-embed-key="ek_…"></script>` — mapping a
    public, origin-scoped key to an EXTERNAL custom agent (#3). `/widget.js` is
    served inline (self-contained IIFE, no deps); `/embed/v1/{key}/config|chat`
    are public, per-key **origin-scoped CORS** (echoes ACAO only to
    allowed_origins; empty=any for dev) + per-(key,IP) rate limit. Key CRUD is
    admin-gated. Security: only `external`-scope agents can be embedded (internal
    refused), runtime is the same grounded/tool-less/write-less/guard-screened
    custom-agents brain. Verified: allowed origin 200+ACAO, disallowed 403,
    preflight 204, grounded chat, internal-agent embed refused.
  - **#7** `app/core/leader.py` + `docs/scaling_and_concurrency.md`: **Postgres
    session-level advisory-lock leader election** gates the three in-process
    singletons (APScheduler, IMAP poller, agent-bus consumer) in `main.py`
    lifespan — with >1 replica only the LEADER runs them; followers serve HTTP
    only ("[Scheduler] built but not started"). `GET /health` → `ha:{role,
    runs_singletons}`. Config `HA_LEADER_ELECTION=1`, `HA_LOCK_KEY=871123`.
    Fails OPEN to leader if the DB is unreachable at election (single-process
    deploys never silently lose jobs). Lock released on shutdown. Verified: 2
    processes on one DB → one leader (singletons up) + one follower (singletons
    down).
  - **Real finding documented:** `database.get_connection()` is **connect-per-
    call, no pool** (the #1 throughput bottleneck, despite the README's "one
    pool"). The doc's zero-code fix: point `DB_DSN` at a transaction pooler
    (PgBouncer / Supabase pooler) BEFORE adding replicas.
  - **Railway needs:** `sql/embed_keys.sql` (#6). #7 is pure app code (no
    migration); set `HA_LEADER_ELECTION=1` on all replicas sharing one DB. HTML
    (`widget-demo.html`) deploys with the frontend.
  - **Deferred follow-ups:** #6 mobile in-app SDK + full multi-tenant isolation
    (per-key tenant scoping beyond origin); #7 zero-gap automatic failover
    (live follower self-promotion without restart — needs the singleton-start
    logic as a restartable callback), and the in-process connection-pool
    refactor (~170 call sites — the pooler buys the same win first, risk-free).
- **2026-07-23** — **#8 (compliance & data residency) + #9 (testing at scale)
  SHIPPED. ALL 9 BLINDSPOTS NOW CLOSED.** Validated on isolated port 8001.
  - **#8** `app/core/compliance.py` + `docs/compliance_and_data_residency.md` +
    `trust.html`: a live, **honest** Trust Center. `/compliance/posture` (PUBLIC,
    no secrets) returns a 16-control inventory that REFLECTS real runtime (PII
    masking on? `email_suppression`/`audit_log` present? → status implemented)
    rather than asserting marketing claims. Canadian residency is a configurable
    architectural claim via `DATA_REGION`; `LLM_ZERO_RETENTION` attested via env.
    Explicit honesty rule: **self-attested inventory, NOT a third-party cert** —
    stated in the payload and on the page (no fabricated SOC 2). `trust.html`
    renders it grouped by area with status pills. Config: `DATA_REGION`,
    `LLM_ZERO_RETENTION`, `COMPLIANCE_CONTACT`.
  - **#9** `app/core/eval_suite.py`: Testing-Center-style batch regression.
    `generate_cases()` synthesizes realistic paraphrases from EVERY approved KB
    article's own problem+keywords (deterministic, no LLM/cost, CI-safe);
    `run_retrieval_suite()` fires them at the REAL hybrid retriever
    (CONCURRENT — ThreadPoolExecutor, since each query is one OpenAI embedding
    round-trip; 390 cases in ~31s vs >120s sequential) and checks top-K
    findability + flags any UNREACHABLE article; `run_safety_batch()` runs 8
    prompt-injection variants through the real SDR brain (0 may leak).
    `run_gate()` → PASS/FAIL, exposed as `/evals/suite` + `/evals/gate` (admin)
    AND a **CLI CI gate** `python -m app.core.eval_suite` (exit 0/1 — verified
    both paths). Calibrated defaults: top-2, 0.85 threshold (live KB ~1.0 top-2,
    ~0.99 top-1 → real headroom, still fails on a broken embedding pipeline /
    removed / mis-indexed article). Reuses [evals] leak detection + [knowledge]
    retriever verbatim.
  - **Railway needs:** nothing new for #8/#9 (pure app code). Set `DATA_REGION`
    (+ `LLM_ZERO_RETENTION=1` when contracted). Wire `python -m app.core.eval_suite`
    into CI as a deploy gate. `trust.html` deploys with the frontend.
  - **Deferred:** #8 automated DSAR endpoint + signed sub-processor list + actual
    SOC 2 audit; #9 optional LLM-generated (lexically-divergent) paraphrases for
    a harder semantic test (default stays deterministic/free for CI).

---

## ✅ ALL 9 BLINDSPOTS CLOSED (2026-07-23)

Every ranked gap from the Agentforce comparison is now shipped (#1 takeover
console, #2 multilingual, #3 no-code authoring, #4 agent-ops analytics, #5
employee/IT service, #6 widget SDK, #7 HA leader election, #8 compliance Trust
Center, #9 testing-at-scale gate). Out-of-scope items (field service,
AgentExchange marketplace, full multi-tenancy) remain deliberately unaddressed.
Consolidated Railway migrations still to apply: `sql/agent_console.sql`,
`sql/custom_agents.sql`, `sql/employee_service_seed.sql`, `sql/embed_keys.sql`.
New env to set on deploy: `HA_LEADER_ELECTION=1` (per replica), `DATA_REGION`,
optional `LLM_ZERO_RETENTION=1`. New CI step: `python -m app.core.eval_suite`.

---
---

# Analytics Blindspots & Roadmap (a SEPARATE axis)

> Source: 2026-07-23 blindspots pass comparing Conscestra's Analytics module
> against **Salesforce Analytics / Tableau / CRM Analytics / Data Cloud**
> (salesforce.com/ca/analytics) — a DIFFERENT product from Agentforce above.
> Its thesis: *"turn data into action — decide at the point of insight, across
> sales, service AND marketing."* Our Analytics agent was built as a
> report-printer over one stored proc; these are the gaps against that pitch.
> Confirmed against the code, ordered by (value × cheapness). Update `Status`.

## Where we're already fine (don't rebuild)

- **Visualization is covered** — `analytics-mgmt.html` renders `dashboardData`
  via Chart.js (`buildDashboardHTML`), including inside the AI chat page. "No
  charts" would be wrong.
- **Forecast calibration** (predicted-vs-actual, attainment %) in
  `analytics/formatter.py` is genuinely ahead of a stock dashboard.
- Data-freshness timestamp is present (only lineage/confidence is thin).

## Blindspots (ranked)

| # | Gap | Why it matters | Fix leverage | Effort | Status |
|---|-----|----------------|--------------|--------|--------|
| A1 | **The Analytics agent advertised "take action at the point of insight" and did NONE of it — the prompt was fiction the code never ran.** `analytics/prompt.py` declared `anomaly_alert`, HEARTBEAT ACTIONS, anomaly thresholds (win-rate drop >10% WoW, stalled deal >5 days) + an ALERT/collab protocol; `graph.py` had **no** node that detected an anomaly. | This is the screenshot's core claim. The one agent named for alerting couldn't alert. | Assembly on existing plumbing: anomaly rule → supervisor alert/emit → governed proposal. | Medium | **✅ DONE 2026-07-23** |
| A2 | **Only ~15 pre-baked sections of ONE stored proc. No ad-hoc exploration / semantic layer.** `sql_builder._validate` hard-rejects `mode != 'dashboard'`; coverage = 6 fixed params into `sp_analytics_dashboard`. No "revenue by region", no arbitrary pivot/group-by. Salesforce's whole "Ask Data" (NL → arbitrary query/viz) is absent. | The self-service BI premise of the product is missing. | Guarded NL→SQL over a curated view/semantic layer (read-only role, allow-listed columns) OR expand SP sections. | Large | **✅ DONE 2026-07-23** — governed semantic layer (`semantic_model.py` + `semantic_query.py`) + agent `mode='explore'`; LLM emits a validated JSON spec (never SQL), compiled to parameterized read-only SQL. |
| A3 | **"Sales, service AND marketing in one place" is three silos.** Analytics agent is ~90% sales+finance. Service metrics (containment/deflection/CSAT/cost-per-resolution) live in `agent_ops.py` on a different page, invisible to Analytics. Marketing = just `lead_source_performance` (no campaign ROI/attribution/funnel). | The unified cross-domain view the pitch sells is fragmented across pages that don't know about each other. | Surface `agent_ops` metrics + marketing sections as `reportType`s in the Analytics dashboard. | Medium | **✅ DONE 2026-07-23** — `mode='service_analytics'` (agent_ops scorecard) + `mode='marketing_analytics'` (new `marketing.marketing_analytics()` rollup) now answer inside the Analytics agent. |
| A4 | **No proactive/scheduled insight delivery.** Insight only appears when a human opens `/analytics-chat` and asks. "At the point of decision" means it finds the decision-maker (digest/notification/Slack). | We own the plumbing (`ceo_briefing`, SSE `/notifications/stream`, digest job); Analytics just isn't wired into it. | Scheduled anomaly/KPI push through the existing notification path (pairs with A1). | Medium | **✅ DONE 2026-07-23** — anomalies now ride the daily 08:00 executive briefing (email + its Slack broadcast) AND, when `SUPERVISOR_ENABLED=1`, the per-tick supervisor alert → notification/orchestrator inboxes (A1). |
| A5 | **No drill-down-to-action / write-back bridge.** "$X overdue on Account Y" or "deal stalled 5 days" is a dead-end markdown table — no one-click "create collections task / draft dunning email". Every other module has the proposal→governance-queue path; Analytics output does not. | The literal "turn data into **action**" gap. | Reuse the proposal/governance queue the rest of the CRM already has. | Medium | **✅ DONE 2026-07-23** — anomalies become governed proposals via the supervisor planner bridge AND an inline "⚡ Act on this" button per anomaly in `analytics-mgmt.html` → `POST /analytics/anomalies/act` composes a plan on demand (writes queued for governance approval). |

## Explicitly out of scope (decisions, not oversights)

- **Data Cloud-style external data unification** (BYO warehouse / external
  datasets) — only relevant at platform scale; today analytics is over our own
  Postgres + `web_search` live lookup.
- **Full drag-and-drop visual query builder (Tableau canvas)** — the chat +
  Chart.js dashboard is the surface; a WYSIWYG builder is a large frontend bet
  not justified until A2 (a real query layer) exists.

## Execution log

- **2026-07-23** — Analytics blindspots pass recorded (prompted by the
  Salesforce Analytics screenshot, distinct from the Agentforce comparison).
  **Fixed the one latent correctness bug found:** `analytics/prompt.py`
  advertised `MODES: dashboard, kpi, trend, forecast, cohort`, but
  `sql_builder._validate` rejects every mode except `dashboard` (would 500 if
  the LLM emitted one). Prompt now states dashboard is the only SP mode and
  sections are selected via `reportType`. A1–A5 remain open; A1+A5 are the
  high-signal "insight→action" loop and mostly assembly on existing plumbing.
- **2026-07-23** — **A1 SHIPPED + A5 PARTIAL** (the insight→action loop).
  Smoke-tested green against the local DB.
  - `app/core/analytics_signals.py` — the period-over-period trend anomalies the
    Analytics prompt always promised but nothing computed: **win_rate_drop**
    (win rate trailing-7d vs prior-7d, ≥10pp drop, min sample each window),
    **stalled_deals** (open opps untouched ≥5d — `updated_at` proxy for stage
    change, distinct from supervisor's slipped=past-close-date and the exec
    pack's idle=no-activity), **revenue_slump** (closed-won revenue WoW ≥30%
    drop over a real baseline). READ-ONLY, defensive (any error → no signal),
    all thresholds env-tunable (`ANALYTICS_*`). Signals use supervisor's exact
    shape, so `supervisor.py` extends `DETECTORS` with them and they ride its
    alert-emit + 12h dedupe + governance + **planner bridge** unchanged — that
    bridge turning a breach into a governed proposal a human approves is the A5
    write-back (nothing mutates directly). `BREACH_GOALS` gives the planner a
    goal per rule (merged into `supervisor._breach_goal`).
  - **Analytics agent surface** (closes A1 where it was advertised): new
    `mode='anomalies'` — deterministic pre-router route ("any anomalies?",
    "what needs attention?", "what changed this week?", "red flags?") →
    `graph.py` db_node runs `detect_all()` → `formatter` renders the briefing
    with recommended actions + a note on how they become approved actions.
    Prompt documents the mode. Verified: "any anomalies?" → live detection
    (found 74 stalled deals / $1.18M); "Show AR aging report" still → dashboard
    (no regression).
  - Admin endpoint `GET /analytics/anomalies` (wired under `_ADMIN`) → live
    anomalies + thresholds, for the UI and testing.
  - **Verified:** 3 detectors registered in the supervisor loop (11 total);
    `stalled_deals` fires inside a real tick alongside ar_spike/unbilled/etc.;
    `_breach_goal` returns a specific goal for each new rule; anomaly routing
    hits mode=anomalies for 8 phrasings and does NOT steal report queries;
    briefing renders empty + populated. **Pure app code — no migration.**
    To activate the auto-emitted alerts + governed plays on schedule, set the
    existing `SUPERVISOR_ENABLED=1` (+ `SUPERVISOR_PLANNER=1` for A5 proposals).
  - **Deferred:** A5's inline per-insight "act on this" button in
    `analytics-mgmt.html` (frontend); A2/A3 still open.
- **2026-07-23** — **A4 SHIPPED** (proactive/scheduled anomaly delivery).
  Smoke-tested green. Wired A1's `analytics_signals.detect_all()` into the
  **existing** daily executive briefing — the highest-leverage single wire,
  since `ceo_briefing.send_briefing` already runs 08:00 ET AND already
  broadcasts the briefing text to Slack (`transports.post_internal`). So one
  section addition delivers **email + Slack, to the actual exec subscribers, on
  the existing schedule**:
  - `ceo_briefing.gather()` now pulls `anomalies` (best-effort, never breaks the
    briefing); `_anomaly_lines()` shared helper; a prominent "What Changed —
    Trend Anomalies (auto-detected)" section rendered high in the flagship
    `render()` (text after the Five Numbers; HTML after the decision box) and in
    every role briefing `render_role()` (CFO/CRO/COO), text + HTML.
  - Verified: `gather()` returns the live anomaly (74 stalled deals / $1.18M);
    the section appears in flagship text + HTML and in the CRO role briefing;
    the Slack broadcast inherits it (it posts the flagship text, which now
    carries the section). Rides `CEO_BRIEFING_ENABLED` (digest) / `SUPERVISOR_ENABLED`
    (per-tick alerts). **Pure app code — no migration.**
  - **Deferred:** A5 inline "act" button (frontend); A2 (ad-hoc/semantic BI) +
    A3 (unify service/marketing into the analytics surface) remain open.
- **2026-07-23** — **A3 SHIPPED** (sales + service + marketing in ONE place).
  Smoke-tested green against the local DB.
  - Two new Analytics-agent modes, same pattern as `anomalies` (deterministic
    pre-router route → graph db_node → formatter), so the agent that was ~90%
    sales/finance now answers service + marketing too:
    - `mode='service_analytics'` → reuses `agent_ops.metrics(days)` verbatim
      (containment / escalation / CSAT proxy / avg-to-close / cost-per-
      conversation / by-channel). Verified live: 43 conversations, 100%
      containment, by-channel breakdown.
    - `mode='marketing_analytics'` → new `marketing.marketing_analytics(days)`
      aggregate (campaigns by status, emails sent, CASL suppressions, reply
      rate, orders + attributed revenue since launch, top campaigns). Read-only,
      degrades cleanly when tables/data absent; `GET /marketing/analytics`
      endpoint added. Verified: empty-window graceful path + all aggregate SQL
      validated against the live schema.
  - Pre-router routes "service analytics / containment / CSAT / how is support
    doing" and "marketing analytics / campaign performance / ROI" with an
    optional "last N days/months" window; **does NOT steal** the sales report
    types (lead_source / pipeline / ar_aging) or `anomalies` — verified.
    `analytics/formatter.py` gained `_format_service_analytics` +
    `_format_marketing_analytics`; prompt documents both modes.
  - **Pure app code — no migration.** (service metrics want
    `sql/agent_console.sql` for containment/escalation, already on the A1/#4
    Railway list; marketing wants the existing `marketing` tables.)
  - **Analytics axis now: A1 + A3 + A4 done, A5 partial; only A2 (ad-hoc /
    semantic-layer BI) remains open.**
- **2026-07-23** — **A2 SHIPPED. Analytics axis now COMPLETE (A1–A4 done, A5
  partial).** Self-service "Ask Data" via a GOVERNED SEMANTIC LAYER — the LLM
  never writes SQL. Plan-approved design, smoke-tested green.
  - `app/core/semantic_model.py` — curated registry of **explores**
    (opportunities / orders / leads), each a vetted FROM/JOIN base + allow-listed
    dimensions / measures / filters, every one mapped to a hand-written SQL
    fragment. The LLM references only string KEYS from this catalog.
  - `app/core/semantic_query.py` — `compile(spec)` validates every
    explore/dimension/measure/filter/op against the model (unknown → reject) and
    builds parameterized SQL (filter VALUES are psycopg2 binds, never inlined;
    single statement; clamped LIMIT). `run_readonly()` executes in a **DB
    read-only transaction** (`set_session(readonly=True)`, reused from
    `execute_sp`) + `statement_timeout`. `plan_explore()` = the LLM planner
    (JSON spec only, one repair retry). `POST /analytics/explore` +
    `/analytics/explore/catalog` under `_DATA`; `ANALYTICS_EXPLORE_ENABLED` switch.
  - Agent `mode='explore'` (pre_router → graph → formatter, same pattern as
    `anomalies`): routed as the LAST resort before AI passthru so the ~15 canned
    reports keep precedence; handles the long-tail "group X by Y" asks. Formatter
    renders a generic dims+measures table + an interpreted-spec echo (trust).
  - **Safety verified:** compiler rejects unknown fields/ops; an injection
    payload (`x'; DROP TABLE …`) lands in `params`, NOT the SQL text; read-only
    txn REFUSED live UPDATE/INSERT/DELETE; LIMIT clamps. **Live verified:**
    opportunities-by-stage, win-rate-by-lead-source, orders-by-month, leads-by-
    source-conversion. **End-to-end verified:** NL "how many leads by source",
    "avg lead score by rating", etc. → correct tables; canned "AR aging" still
    routes to dashboard (no regression).
  - **Bug fixed during test:** the planner JSON has no `mode` key, so
    `graph_utils.parse_ai_json` (mode-gated) discarded valid specs — added a
    local `_parse_spec_json`. **Pure app code — no migration.**
  - **Deferred (v1 scope):** no free-form/raw SQL even for admins (structured
    spec only); no undefined joins / window funcs / sub-selects; a WYSIWYG visual
    query builder is a separate frontend effort. Extend explores by editing the
    registry.
- **2026-07-23** — **A5 FULLY DONE (analytics axis A1–A5 all complete).** The
  inline "act on this" button — the last frontend follow-up — shipped:
  - Backend: `mode='anomalies'` now returns the STRUCTURED signals in
    `dashboardData.anomalies` (graph + formatter), and a new **`POST
    /analytics/anomalies/act`** (in `analytics_signals.py`, `_DATA`-gated so the
    analytics page can call it) maps an anomaly `rule`+`headline` → its
    `BREACH_GOALS` goal → **`planner.run_plan(goal)`** — the SAME planner bridge
    the supervisor uses. Reads run, writes queue for governance approval;
    idempotent (reuses `supervisor._plan_already_queued`); the resulting proposal
    still needs an admin approval (no privilege escalation).
  - Frontend (`analytics-mgmt.html`, local-only — user deploys): `buildResponseHTML`
    intercepts `mode==='anomalies'` → `buildAnomaliesHTML` renders a severity card
    per anomaly with an "⚡ Act on this" button; `actOnAnomaly()` POSTs to the
    endpoint, shows the result inline + a link to the Governance queue.
  - Verified: structured anomalies reach the payload; the act endpoint composed a
    plan (honest "read-only, nothing to approve" when the play was reads; "queued
    N for approval" when it includes writes); idempotent (no dup proposals); bad
    rule rejected. Backend is pure app code; the HTML deploys with the frontend.

---
---

# Platform Blindspots & Roadmap (a THIRD axis)

> Source: 2026-07-24 blindspots pass comparing Conscestra against the Salesforce
> **Products / platform** pitch (salesforce.com/ca/products) — a DIFFERENT axis
> from Agentforce (agents) and Analytics (BI) above. Its thesis: *"every agent
> runs on YOUR data, workflows, and customer history — not standalone AI; humans
> + agents + apps + data on ONE platform; value fast for companies of all sizes;
> Industries."* These are about grounding, extensibility, onboarding and
> multi-size fit. Confirmed against the code, ordered by (value × cheapness).

## Where we're already ahead — the pitch's HEADLINE claim is our strength

*"Agents run on your data / workflows / customer history, not standalone AI."*
This is exactly what we over-invested in — **do not rebuild**: `context.py`
"born with context" hydration, One Customer Memory (cross-channel), KB grounding
+ retrieval, per-customer intelligence profiles, the blackboard. Our agents are
MORE grounded in the org's live data than a generic deployment. Lead with it.

## Strategic framing (user, 2026-07-24) — the Time-to-Value Engine

These six are NOT equal. **P1+P2 prevent a new customer from reaching value;
P3 determines whether we grow WITH the customer; P4 is a SaaS-architecture
decision; P5+P6 improve adoption/expansion.** The greatest risk isn't AI
capability — it's the DISTANCE between an empty database and the first moment a
customer says *"this system understands my business."* The three features that
shorten that distance most — the **Customer Activation Layer** — are:

> **Import my business (P1) → Guide me (P2) → Show me intelligence (P6).**

Recommended build order: **Phase 1 First Value = P6 + P2 + P1 (ALL SHIPPED
2026-07-24)** · Phase 2 Adaptability = P3 (custom fields, cheap on our JSONB
substrate) + P5 (industry packs) · Phase 3 Platformization = P4 multi-tenant +
full custom objects + marketplace. On P4, the principle: *you can postpone full
multi-tenancy; you must not make it impossible* — don't foreclose an
`organization_id` ownership column later.

## Blindspots (ranked)

| # | Gap | Why it matters | Fix leverage | Effort | Status |
|---|-----|----------------|--------------|--------|--------|
| P1 | **No customer-data onboarding / bulk import — no on-ramp to "value fast".** `integrations.py` is EXPORT-only (QuickBooks/Xero CSV out) + ICS meetings + KB docs in. A new company CANNOT load its existing accounts/contacts/leads/products except one-at-a-time via chat or raw SQL. | The literal "value fast for companies of all sizes" has no on-ramp. | Governed CSV import: map columns → create via the existing entity SPs (`build_*_query` mode=create), dedupe-aware (reuse `data_quality`), dry-run preview. Every downstream piece exists. | Medium | **✅ DONE 2026-07-24** |
| P2 | **No empty-state / new-org readiness — the product assumes a populated DB.** Every dashboard/briefing/KPI/anomaly detector presumes data exists; a fresh or small org sees zeros / "no data". No readiness surface, no guided setup. | "Companies of all sizes" includes the small shop with sparse data. | A `/setup/readiness` endpoint inspecting what's configured/populated → a prioritized next-step checklist + graceful empty-state copy. | Small | **✅ DONE 2026-07-24** — `readiness.py` → `GET /setup/readiness`: defensive checks (DB/LLM/core-records/KB/email/executives + optional catalog/voice/telephony) + posture flags + a single prioritized `next_step`; a fresh org is steered straight to the P1 importer. |
| P3 | **No data-model extensibility (custom fields/objects).** Salesforce's defining trait: admins add fields/objects without code. We're a fixed schema. | The core "platform vs product" gap — lets us grow WITH the customer. | LIGHT: custom fields via a sidecar defs+values store (no SP changes), DB+UI+Agent+Analytics-aware. Full custom-objects deferred. | Large | **✅ DONE 2026-07-24 (fields)** — `custom_fields.py` + `sql/custom_fields.sql`; authored in setup.html; agent-aware (context + ai_summary); analytics-aware (A2 explore group/filter by custom field). |
| P4 | **Single-org — no multi-tenancy.** A product/architecture DECISION, not an oversight: single-company product today; full SaaS deferred. | "Companies of all sizes / SaaS" implies multi-tenant; don't foreclose it. | Ratified: schema-per-tenant default, DB-per-tenant for enterprise/residency, served by a routing seam at the ONE `get_connection` chokepoint (no SP/table changes). | Large | **◑ Phase 0 SHIPPED 2026-07-24** — tenant-routing seam (`tenancy.py` + `sql/tenants.sql` + tenant-aware `get_connection`); full SaaS deferred/roadmapped (`docs/multi_tenancy.md`). |
| P5 | **No industry verticalization.** Salesforce sells Industry Clouds; we're horizontal. | Vertical fit accelerates value + differentiates. | LIGHT: an "industry starter pack" (KB seeds + example chips + objective templates per vertical) on the playbooks-as-data + custom-agents + KB-tier substrate. | Medium | **✅ DONE 2026-07-24** — `industry_packs.py` (SaaS / Real Estate / Music School): one-click apply seeds custom fields (P3) + KB + objectives + a vertical Q&A agent (#3), all via existing writers; UI card in setup.html. |
| P6 | **No time-to-value tooling (guided tour / demo-org / sample-data toggle).** Rich NIGHTLY seed generators exist, but no self-serve "spin up a demo org in 2 min" or in-product guided tour. | "Take guided tour", "New to Salesforce", "value fast". | A one-command demo-seed + a guided-tour overlay (frontend). | Small | **✅ DONE 2026-07-24** — `demo.py` → `POST /demo/seed` builds a sample business (book via the P1 importer + shaped opportunities) and returns an intelligence HEADLINE; `/demo/status` + `/demo/clear` (fully removable). |

## Explicitly out of scope (decisions, not oversights)
- **Full multi-tenancy** (P4) — only if we sell to multiple orgs on one instance.
- **Full custom-objects / metadata platform** — the opinionated product is the
  point; light custom-FIELDS (P3) is the pragmatic middle.
- **AppExchange-style app marketplace** — platform-scale only (also noted on the
  Agentforce axis).

## Execution log
- **2026-07-24** — Platform blindspots pass recorded (prompted by the Salesforce
  Products/platform screenshot). Starting P1 (governed CSV import) — the single
  cheapest, highest-value on-ramp, and everything downstream already exists.
- **2026-07-24** — **P1 SHIPPED (governed CSV data onboarding).** Smoke-tested
  green against the local DB.
  - `app/core/data_import.py` — bulk importer for **accounts / contacts / leads**
    that REUSES each entity's own create path (`build_*_query(mode='create')` →
    `execute_sp`), so every imported row gets the same validation, guards and
    audit a chat-created record does. Alias-based header mapping
    (case/space-insensitive: "Company Name" → accountName) with an optional
    explicit `{field: header}` override; per-entity required fields + custom
    validators (leads need first OR last name); dedupe probes (accounts by name,
    contacts/leads by email); 2000-row cap.
  - Two-step, safe-by-default: **`preview`** (read-only dry-run — classifies
    every row create / duplicate / error, reports unmapped columns) then
    **`commit`** (creates only the 'create' rows; re-checks dedupe per row so a
    re-run can't double-insert). Admin action (the human is the approver), like
    a marketing-campaign launch.
  - Router `GET /import/schema`, `POST /import/preview`, `POST /import/commit`
    wired under `_ADMIN` in main.py.
  - **Verified:** header aliasing; preview create/duplicate/error split; commit
    creates real records; **idempotent** (2nd commit skipped all as duplicates);
    contacts + leads clean; bad entity + missing-required rejected; all test rows
    cleaned up.
  - **Real finding (worked around, SP NOT modified per policy):** `sp_accounts`
    create UNCONDITIONALLY writes a billing address and `addresses` has NOT NULL
    `line1`/`city`/`country` — so importing an account requires those three. The
    importer therefore maps address columns and assembles the billing-address
    JSONB (street/city/country required, province/postal optional); contacts and
    leads have no such dependency. (A cleaner long-term fix is to make the SP's
    address insert conditional on a non-empty `p_billing_address` — a DB-layer
    change for the user, out of scope here.)
  - **Pure app code — no migration.** **Deferred:** P1 UI (an import panel in the
    mgmt pages — frontend), account-linking for imported contacts, products
    import, and column-mapping persistence.
- **2026-07-24** — **P2 SHIPPED (new-org readiness / empty-state).** Smoke-tested
  green (live DB + a simulated empty org).
  - `app/core/readiness.py` → `GET /setup/readiness` (admin): one READ-ONLY
    inspection of what's configured + populated, returning
    `{ready, score{done,total,pct}, next_step, checks[], features{}}`.
  - Essentials (gate the score): database, LLM configured, **core records
    loaded** (accounts+contacts+leads — the empty-state signal), KB seeded,
    email/SMTP, executives. Optional (informational): product catalog, voice
    (Azure), telephony (Twilio/Telnyx). Plus a posture map of feature flags
    (agent_bus / autosend / supervisor / ceo_briefing / …).
  - Every probe is DEFENSIVE (`_count` tolerates a missing table / unreachable
    DB → the org still gets guidance); the single `next_step` is the
    highest-priority essential still needing action — the one thing to do next.
  - **The P1↔P2 pairing works:** on a fresh/empty org the top next_step is
    "import your book of business", pointing straight at the P1 CSV importer
    (`POST /import/preview`). Verified: live org 5/6 (next = SMTP); simulated
    empty org 2/6 (next = core-records import).
  - **Bug fixed during test:** the KB check queried `status='approved'` but the
    live table uses `status='active'` (65 articles) — corrected + a total-count
    fallback so it's accurate across deployments. **Pure app code — no migration.**
  - **Deferred:** the frontend empty-state panels / "getting started" card that
    consume this endpoint (a mgmt-page UI follow-up, like P1's import UI).

## ✅ Platform axis so far: P1 + P2 shipped
P1 (governed CSV import) + P2 (new-org readiness) — the "value fast for companies
of all sizes" on-ramp — are both live. Remaining: P3 (light custom-fields on the
existing JSONB substrate / scope), P5 (industry starter packs), P6 (demo-org +
guided tour). P4 (multi-tenancy) stays deliberately out of scope. The frontend
surfaces for P1 (import panel) and P2 (getting-started card) are the two deferred
UI follow-ups.
- **2026-07-24** — **P6 SHIPPED (demo / sample-data seed).** Smoke-tested green
  (full seed → headline → idempotent re-seed → clear → DB pristine).
  - `app/core/demo.py` → `POST /demo/seed` · `GET /demo/status` · `POST /demo/clear`
    (admin). Takes an org from empty to a living sample business in one call.
  - **Reuses P1:** the sample book of business (10 accounts / 14 contacts / 12
    leads — classic fictional names + @demo.conscestra.local) is loaded through
    the SAME governed importer (`data_import.commit`), so it's dedupe-safe and
    also demonstrates the import path. Then 16 shaped opportunities are inserted
    on the demo accounts (`is_synthetic=true`) with varied stages/dates — stalled,
    slipped, won, lost — so anomalies + win-rate + pipeline all have something to
    show.
  - **Returns the intelligence HEADLINE** — the "this understands my business"
    moment: e.g. _"Your demo company has $595,000 in open pipeline across 11
    deals — 5 stalled with no movement, 3 slipped past their close date, a 60%
    win rate, and 12 new leads to work."_
  - Fully **removable**: everything demo is identifiable (accounts by the name
    set, people by the @demo domain, opps by is_synthetic on demo accounts);
    `clear()` deletes opps → contacts → leads → addresses → accounts (FK-safe).
    Verified: seed idempotent (2nd run = 0 new opps); clear left status all-zero.
  - Bug fixed in test: `account_id = ANY(%s)` needed a `::uuid[]` cast. **Pure
    app code — no migration.**

## ✅ Phase 1 (First Value / Customer Activation Layer) COMPLETE
P1 (governed import) + P2 (readiness) + P6 (demo seed) are all live — the
Import → Guide → Show-intelligence on-ramp. Remaining platform axis: Phase 2
(P3 custom fields, P5 industry packs), Phase 3 (P4 multi-tenancy — deliberately
deferred). The only follow-ups on shipped items are the FRONTEND surfaces
(P1 import panel, P2 getting-started card, P6 "Try with sample data" button).
- **2026-07-24** — **Phase-1 FRONTEND shipped: `setup.html` (Getting Started /
  Readiness Center).** One page surfacing all three activation-layer backends,
  registered in `_CHAT_PAGES` (served at `/setup.html`) + linked from the
  index.html modules nav:
  - **Readiness Center** (P2) — `GET /setup/readiness` → a % ring, the single
    "recommended next action" banner, the full capability checklist (status +
    inline links), and enabled-subsystem chips.
  - **Import Your Data** (P1) — entity picker + CSV paste/file → **Preview**
    (create/duplicate/error chips, unmapped/mapped columns) → **Import**; the
    checklist auto-refreshes after a successful import.
  - **Try With Sample Data** (P6) — **Seed** shows the intelligence HEADLINE on a
    dark card + counts + a jump to Analytics; **Clear** removes it. Refreshes
    readiness after each.
  - Self-contained (inline CSS, light/dark), same `_API_BASE` pattern as the
    sibling mgmt pages, same-origin fetch. HTML is local-only — user deploys.
- **2026-07-24** — **P3 SHIPPED (Custom Fields — the LIGHT / grow-with-the-customer
  version).** Smoke-tested green against the local DB.
  - **Premise corrected:** the core entities have NO JSONB column (only
    `payments.metadata`), so the "free on existing JSONB" assumption was wrong.
    Built as a **sidecar defs+values store** — zero changes to existing tables or
    the entity SPs. `sql/custom_fields.sql`: `custom_field_defs`
    (entity/field_key/label/type/options/required) + `custom_field_values`
    (entity, entity_id, field_key, value_text). **Railway needs this migration.**
  - `app/core/custom_fields.py` (mirrors `custom_agents.py`): defs CRUD + typed
    value coercion/validation (text/number/select/date/bool) + `get_values` /
    `set_values` / `get_values_labeled` / `enrich`; admin router
    `/custom-fields/defs` + `/custom-fields/values/{entity}/{id}`.
  - **All four "awareness" requirements delivered + verified:**
    - **DB** — the store.
    - **UI** — a Custom Fields card in `setup.html` (author fields; load+edit a
      record's values). (Inline panel on account-mgmt.html deferred as a
      fast-follow — the endpoints + setup.html surface exercise everything.)
    - **Agent** — injected into `context.hydrate()` (a "Custom:" line in the
      ≤12-line block) + the account `ai_summary` fact sheet, so an agent answering
      "show me our Enterprise accounts" actually sees `customer_tier`.
    - **Analytics** — `semantic_model.resolved_explore()` merges def-driven custom
      dimensions/filters into the A2 explores (subquery on `custom_field_values`,
      typed casts); verified **"opportunities by customer_tier"** returns
      Enterprise/Premium/Standard groups, and a custom-field filter value stays a
      **bound param** (injection-safe, reusing the A2 compiler discipline).
  - Verified: def CRUD; value coercion + rejection of bad select/date/unknown key;
    context + ai_summary show the fields; group + filter by a custom field; all
    test defs/values cleaned up. **Deferred (v1):** custom OBJECTS (new entity
    types — the store is object-ready), per-mgmt-page inline value panels beyond
    setup.html, custom NUMBER measures (dimensions/filters only), field RBAC.
- **2026-07-24** — **P5 SHIPPED (Industry Starter Packs). Platform axis now
  P1/P2/P3/P5/P6 all done; only P4 (multi-tenancy) deliberately deferred.**
  Smoke-tested green (apply → verify pieces → idempotent → remove → hard-clean).
  - `app/core/industry_packs.py` — 3 packs (SaaS, Real Estate, Music/Piano
    School) as PURE DATA. Applying a pack = a one-click bundle that REUSES every
    existing writer, no new schema/code path:
    - custom fields (P3) via `custom_fields.create_def` (idempotent),
    - KB articles via `knowledge.publish` (idempotent by a `pack:{id}:{i}` source_ref),
    - objectives via `objectives.create` — mapped onto the 6 REGISTERED metrics
      (revenue_30d / high_churn_accounts / lead_conversion_rate_30d / …), deduped
      by name; `_default_target` nudges the current metric when a pack leaves the
      target open,
    - a vertical Q&A agent via `custom_agents.upsert` (external, grounded in the
      pack's now-public KB).
  - `apply` / `status` / `remove` + router `GET /industry/packs`,
    `POST /industry/packs/{id}/apply|remove` (`_ADMIN`); UI card in setup.html
    ("Fit my industry" — apply/remove per pack). `INDUSTRY_PACKS_ENABLED`.
  - **Verified:** all 3 packs list with counts; music_school apply seeded 5 fields
    + 4 KB + 2 objectives + 1 agent (0 errors); idempotent re-apply skipped KB +
    objectives; real_estate's lead/opportunity custom fields appeared in the A2
    explore catalog (property_type, budget, mls_number) — so pack fields are
    **analytics-aware**; agents landed external+enabled. All artifacts hard-cleaned.
  - **Honest limitation:** custom fields on CONTACTS (music_school) are authoring/
    values-supported but NOT analytics-aware (no contacts explore) and contact
    context resolves to the account — so the SaaS (accounts) + Real Estate (leads/
    opps) packs are the full 4-aware demos; a contacts explore is a follow-up.
    Pure app code — no NEW migration (uses P3's `sql/custom_fields.sql` + the
    existing KB/objectives/custom_agents tables).

## ✅ Platform axis effectively complete
P1 (import) · P2 (readiness) · P3 (custom fields) · P5 (industry packs) · P6
(demo seed) — all shipped, all surfaced in setup.html. Only P4 (multi-tenancy)
remains, deliberately deferred (principle: don't foreclose an `organization_id`).
Across the session: 3 Salesforce screenshots → 3 blindspot axes (Agentforce 9 /
Analytics A1-A5 / Platform P1-P6) — all closed except the one deliberate defer.
- **2026-07-24** — **P4 Phase 0 SHIPPED (tenant-routing seam).** The ratified
  decision: single-org product today, full SaaS DEFERRED, recommended future model
  **schema-per-tenant** (DB-per-tenant for enterprise/residency). Phase 0 = the
  minimal seam that keeps the door open, with **NO changes to business tables or
  stored procedures**. Smoke-tested green.
  - `app/core/tenancy.py` — request-scoped `tenant_context` (same pattern as
    write_guard's role ctx) + `resolve(tenant_id)→(dsn,schema)` over a `tenants`
    registry. Explicit `"default"` fallback; unknown/inactive tenant in MT mode
    **fails closed** (raises, never silent default); schema names validated
    (`^[a-z][a-z0-9_]{0,62}$`). `MULTI_TENANT_ENABLED` (default 0).
  - `sql/tenants.sql` — control-plane `tenants` registry (public), one seeded
    `default` row. **Railway needs this migration.** No business table touched.
  - `database.get_connection` — resolves `(dsn,schema)` and applies the schema via
    the **search_path connection OPTION on every connection** (safe from a stale/
    pooled search_path; validated identifier, never interpolated). `public` when
    off = today's exact behaviour.
  - Sessions carry `tenant_id` (`auth/router.get_session` setdefault 'default' —
    future-proof, no auth_sessions migration); `auth_dep` stamps it into
    `tenant_context` next to `set_request_role`.
  - `docs/multi_tenancy.md` — the decision + phased roadmap + the explicit caveat
    that **Phase 0 proves ROUTING, not full isolation** (background-worker / cache
    / rate-limit / billing / hard tenant isolation are Phases 1–4).
  - **Verified against the ratified success criterion:** (1) MT off → existing
    behaviour identical (accounts query, readiness 5/6, analytics explore, all
    routes) — zero regression; (2) a THROWAWAY `tenant_probe` schema + registry row
    + `MULTI_TENANT_ENABLED=1` → the probe tenant routes to its schema and writes a
    row the **default tenant cannot see** (`UndefinedTable`) — isolated routing
    proven on the ONE chokepoint; (3) unknown tenant **fails closed**. Throwaway
    schema dropped; `tenants=['default']`. No business tables/SPs touched.

## ✅ PLATFORM AXIS COMPLETE (P1–P6). ALL THREE BLINDSPOT AXES CLOSED (see 4th axis below, 2026-07-25).
P1 import · P2 readiness · P3 custom fields · P5 industry packs · P6 demo — all
shipped + surfaced in setup.html; **P4 Phase 0 seam shipped, full SaaS a ratified
deferral** (not an oversight). Across the session: 3 Salesforce screenshots →
3 axes (Agentforce #1-9 / Analytics A1-A5 / Platform P1-P6) — every gap either
shipped or a documented, deliberate deferral. Outstanding Railway migrations from
the platform work: `sql/custom_fields.sql` (P3/P5) + `sql/tenants.sql` (P4).

---
---

# Agentforce Round-2 Blindspots (a FOURTH axis — the unknown unknowns)

> Source: 2026-07-25 second Agentforce pass (salesforce.com/ca/agentforce +
> Agentforce 3/360 2026 announcements: configurable Atlas reasoning, **Agent
> Script** hybrid determinism, **Agentforce Voice GA**, **Command Center**
> observability, **MCP gateway** (30+ partner servers), AgentExchange (~200
> prebuilt actions), **Flex Credits** consumption billing, multi-model BYO-LLM
> incl. Gemini). The 2026-07-23 pass closed the gaps we could SEE; this pass
> hunted what that list itself missed. Every item below is verified against the
> code, not assumed. Ranked by (value × cheapness).

## Verified NON-gaps this round (checked, don't rebuild)

- Per-caller daily LLM budgets with fail-closed deterministic fallback
  (`llm_meter.check_budget` → `LLMBudgetExceeded`) — runaway cost is covered.
- Model tiering exists (`_get_llm(tier="lite")` → `LLM_MODEL_LITE`).
- Agent-bus retries with exponential backoff → `status='failed'`
  (`AGENT_BUS_MAX_ATTEMPTS`) — no lost-event hole.
- WhatsApp INBOUND exists (Meta + Twilio parse → conversation → KB reply,
  draft-first; `transports.py`) — channel breadth better than assumed. Only the
  real outbound sender is unwired (credential-gated by design).

## Blindspots (ranked)

| # | Gap | Why it matters | Fix leverage | Effort | Status |
|---|-----|----------------|--------------|--------|--------|
| U1 | **Custom/embedded agents' escalation is a VERBAL DEAD-END.** `custom_agents.py:250` instructs the model to say "a human teammate will follow up" — but NOTHING creates a queue item, notification, or escalation flag. On the #6 widget (third-party sites) a visitor gets a promise no human ever sees. | The #1 takeover console exists but authored/embedded agents can't reach it. A promised follow-up that never happens is worse than no promise. | On the defer branch: mark conversation `escalated` → agent_console queue + SSE notify. All plumbing exists. | **Small** | **✅ DONE 2026-07-25** |
| U2 | **Data-defined agents BYPASS the #9 eval gate + have no change management.** `custom_agents` has no versioning/draft-publish/rollback/changed-by audit; studio edits are LIVE INSTANTLY — including on embedded third-party widgets. The CI gate (#9) gates CODE deploys; agents-as-data (#3) never pass through it. #3 and #9 quietly cancel each other out. | Agentforce: agent versions + Testing Center pre-deploy + sandboxes. Our no-code authoring shipped without the safety story the rest of the platform has. | Version/audit columns + draft→publish step that runs `eval_suite.run_safety_batch()` against the EDITED agent before it goes live. | Medium | **✅ DONE 2026-07-25** |
| U3 | **Command Center's other half: ZERO platform self-observability.** `agent_ops.py` measures business outcomes (containment/CSAT/cost). Nothing watches the platform itself: event_queue depth, failed-event count, handler error rates, LLM error/latency (the `ok` flag in `llm_usage` is recorded but never read), budget exhaustion. The 12k event_queue backlog was found BY ACCIDENT. | "Manage digital labor" = watch the workers, not just their output. We'd learn of an agent-fleet outage from a customer. | New supervisor DETECTORS (the pattern exists): queue depth, failed events, llm error-rate from `llm_usage.ok`, budget-exhaustion — riding the existing alert→notification path. | Small–Med | **✅ DONE 2026-07-25** (widened: + U1 obligations + U2 governance health) |
| U4 | **Authored agents can't ACT — and there's no Agent-Script-style middle ground.** `custom_agents` runtime is deliberately tool-less/write-less (right default!), but there is NO opt-in path to grant an authored agent a vetted, allow-listed capability set. Note the Agentforce hero examples are all ACTIONS: "processing leave request", "resolving help desk ticket". Our #5 employee agents can EXPLAIN the leave policy but cannot process a leave request. Acting agents still require a dev. | The 2026 Agentforce core move (Agent Script: deterministic conditionals + tool use in authored agents). It's the difference between authored FAQ bots and authored WORKERS. | The registry + dispatch_intent + governance queue are the substrate: per-agent `allowed_capabilities[]` where every write lands in the EXISTING proposal→approval path (never autonomous). | Large | Open |
| U5 | **No LLM provider failover at runtime.** `llm_provider` is a static config choice (`openai`\|`ollama`); no retry-on-alternate-provider anywhere. An OpenAI outage degrades the whole conversational fleet at once (deterministic fallbacks catch budget errors, not provider errors). Agentforce runs OpenAI+Anthropic+Gemini on Bedrock. | 24/7 autonomous support that dies with one vendor's status page isn't 24/7. | One wrapper at the `_get_llm` chokepoint: on provider error, retry once on the OTHER already-configured provider (ollama is already wired). | Small | Open |
| U6 | **MCP client-side: our agents can't CONSUME external tools.** `mcp_server.py` EXPOSES 7 tools (we're a good MCP citizen inbound) but no agent can call an external MCP server; the fleet's only external reach is `web_tools.py`. Agentforce ships an MCP gateway to 30+ partner servers + ~200 AgentExchange actions. | The ecosystem/extensibility play: every new integration is custom code for us, a config row for them. | An MCP client with an admin-allow-listed server registry, calls governed like any a2a capability (outbound_guard + governance for writes). | Medium | Open |

## Re-ranked (known, but the external bar moved)

- **Voice**: Agentforce Voice is now GA (low latency, brand voice, native
  takeover). Ours: `VOICE_STREAM_ENABLED=0`, English-only (deliberate), console
  record-only for voice. Was a deferred footnote on #1/#2 — now the single
  widest channel-parity gap.
- **Per-tenant/per-embed-key entitlement metering** (Flex-Credits-style):
  `llm_meter` budgets are per AGENT (caller), not per customer/key/tenant. Only
  matters when productizing — but #6 embed keys + P4 tenancy Phase 0 opened
  that door; don't foreclose a usage table keyed by (tenant, embed_key).

## Execution log

- **2026-07-25** — Round-2 pass recorded. Method: fresh Agentforce 3/360
  feature sweep (Command Center, Agent Script, Voice GA, MCP, Flex Credits) ×
  code verification of each suspected gap (grep, not vibes). Four candidate
  gaps DISPROVEN by code (budgets, tiering, bus retries, WhatsApp inbound) and
  logged as non-gaps above. U1 is the cheapest real hole (a promised human
  follow-up that never happens); U2 is the sharpest (the no-code axis and the
  testing axis cancel each other); U4 is the strategic one.
- **2026-07-25** — **U1 SHIPPED (universal escalation object).** The rule now
  enforced in code: *never let an agent promise an action that does not create a
  durable system record.* Smoke-tested green (20/20 detector cases, full
  visitor→console→discharge loop, HTTP paths, all probe data cleaned).
  - `sql/escalations.sql` — the `escalations` table: source / reason / summary /
    transcript_excerpt / status / priority / assigned_to / **sla_due_at** /
    conversation_id (nullable) / channel / handle / **contact_known** /
    resolved_*. **Railway needs this migration.** A partial unique index on
    `(conversation_id) WHERE status IN ('open','assigned')` makes it idempotent:
    asking three times in one thread yields ONE obligation, not three.
    Deliberately its own table, not a flag on `conversations` — an escalation has
    an owner, a priority and a DEADLINE, and must exist even when the thread
    couldn't be created (an anonymous visitor on a third-party site).
  - `app/core/escalation.py` — `detect()` (deterministic, no LLM: explicit
    ask-for-a-human incl. French, complaint, high-value intent),
    **`promised_followup()`** (runs on the agent's OUTGOING text — catches the
    promise however the model phrased it, including the module's own canned
    fallback), `open()` / `assign()` / `resolve()` / `resolve_for_conversation()`
    / `list_open()`, and `sla_breaches()`. Priority reads the WHOLE message, not
    just the reason that won detection — "speak to someone about a $50,000
    purchase" is `customer_requested_human` but still **high** priority, and
    priority tightens the actual deadline (`_SLA_BY_PRIORITY`) rather than being
    a decorative label. Admin router `/escalations` + assign/resolve/status.
  - **`custom_agents.run()` rewired** — external agents now (a) thread the turn
    into the conversation spine via `channel_adapters.capture_webchat`, (b) open
    an escalation when the customer asks for a person OR the reply promises one,
    (c) thread their own answer back so the rep sees the transcript. The LLM
    failure path no longer early-returns — its fallback text *is* a promise, and
    an outage is exactly when a dropped promise is most likely.
  - **Honest about reachability**: `contact_known=false` when all we hold is a
    webchat session key, and the reply then ASKS for an email (localized to
    fr/en). A promise we have no way to keep was the failure being fixed, not a
    thing to reproduce silently.
  - **Reaches the human**: `agent_console.queue()` merges live escalations
    (read separately so a missing migration degrades to "no badges" instead of
    breaking the console), ranks an unmet promise ABOVE human-held/waiting/
    negative (prio 35, breached 40), and returns `escalations_open` /
    `escalations_breached`. `transcript()` returns the live escalation so the
    rep sees what was committed and by when. **`takeover()` discharges it** — no
    second click. High/urgent also raise an in-app notification to linked
    executives (best-effort; the record stands with or without it).
  - **The SLA is real**: `supervisor.detect_escalation_sla` registered in
    `DETECTORS` — a past-deadline promise becomes an alert on the existing
    emit/dedupe/planner path. An SLA nobody checks is a comment.
  - **Widget**: `widget.js` now mints a stable per-browser `session_id`
    (localStorage, private-mode safe) and sends it, which is what makes an
    embedded visitor's escalation followable at all. `embed.py` passes it plus
    `source=embed:{key}`.
  - **Studio test mode**: agent-studio's test panel sends `test:true` (honored
    only for a signed-in caller, so a visitor can't silence their own
    escalation) — an admin rehearsing an agent raises no obligation and threads
    no customer conversation. Verified: open-escalation count unchanged.
  - **`agent-console.html` error handling fixed** (the reported bug): `api()`
    now distinguishes **network** / **auth (401/403)** / **server (5xx)** instead
    of collapsing everything into "failed to load" — a signed-out admin gets
    "Not signed in as an administrator" + a sign-in link; a signed-in-but-refused
    admin is told the role is wrong (not uselessly told to sign in again); a real
    backend fault shows the status + detail and names the DB as a likely cause.
    Same-class silent failures fixed alongside: `loadThread` (rendered nothing on
    error), `doSuggest` (no feedback), and takeover/release/close (discarded
    their result — a failure looked identical to success).
  - **Deferred**: webchat has no live push adapter, so a rep replying to an
    embedded visitor still threads only (a known #1 limitation) — the contact-ask
    is the honest bridge until an outbound webchat channel exists. Escalations
    notify executives because that is the only owner-linked audience wired today;
    a rep/team routing table belongs with U4's ownership model.
- **2026-07-25** — **U2 SHIPPED (agent versioning + publish gate).** Agent Studio
  is now treated as what it is — a deployment system — with the lifecycle
  **DRAFT → VALIDATE → SAFETY EVALUATION → PUBLISH → LIVE**, plus append-only
  version history and one-click rollback. Smoke-tested green end to end.
  - **🔴 REAL VULNERABILITY FOUND AND CLOSED (the pass's headline finding).**
    The studio accepted `scope='external'` + `kb_audience='internal'` — two
    dropdowns producing an **anonymously reachable agent that answers from the
    INTERNAL knowledge base**, and `embed.py` would have served it on
    third-party sites (it only refused `scope='internal'`). **Verified live
    before the fix**: a public agent answered "what is the VPN setup procedure"
    from internal IT knowledge. `kb_audience='all'` was the same hole (it maps
    to audience=None, i.e. no tier filter). Now `custom_agents.reach_invariant()`
    — an external agent may read ONLY the `public` tier — enforced at THREE
    layers: `upsert()` (write time), `save_draft()` (authoring time) and
    `publish()` (release time), where it is **unforceable**. Verified: a draft
    mutated in-DB to the unsafe pair with a FORGED `eval_passed=true` and
    `force=true` was still refused. No existing agent violated it (internal+
    internal, external+public), so enforcement broke nothing.
  - `sql/custom_agent_versions.sql` — `custom_agent_versions`: config snapshot +
    `status` (draft/published/superseded/archived) + `changed_fields` + `note` +
    `created_by`/`published_by` + `evaluation` jsonb + `eval_passed` +
    `rolled_back_from`. Partial unique indexes enforce ONE open draft and ONE
    published version per slug. **Railway needs this migration.**
  - **`custom_agents` still holds exactly one row per agent — the LIVE config —
    so the serving path is byte-for-byte unchanged and carries zero new risk.**
    Publishing is a copy from the versions table into it.
  - `app/core/agent_versions.py` — `save_draft` (resumable: editing a draft keeps
    its version number rather than burning one per keystroke-save) · `evaluate` ·
    `publish` · `rollback` · `history` · `status` · `discard_draft`. Admin router
    `/agent-versions/{slug}` + draft/evaluate/publish/rollback/history.
  - **`custom_agents.run_config()`** extracted from `run()` so the gate exercises
    a DRAFT through the EXACT code path a customer hits — evaluating a simplified
    path would be evaluating a different agent than the one you ship. Probes run
    with `test=True`, so an evaluation never threads a conversation or raises a
    U1 escalation.
  - **The gate's checks, honestly scoped:** BLOCKING — `reach_invariant` (the
    leak above), `required_fields`, `injection_resistance` (the 8 `eval_suite`
    injections through this draft; 0 may leak), `no_fabrication`,
    `runtime_healthy`. ADVISORY — `guard_interventions`. It does NOT judge
    whether instructions are commercially wise; "be an aggressive salesperson"
    is a human approver's call and pretending an automated check settles it
    would be theatre. What it guarantees is that a human DID approve, that the
    change is attributed and diffed, and that the previous version is one click
    away.
  - **Bug caught during test:** the original `outbound_guard_clean` check was
    TAUTOLOGICAL — `run_config` already replaces guard-blocked text with the
    fallback, so screening the returned reply could never fail. A check that
    cannot fail is worse than none (false confidence). Replaced with a real
    signal: `run_config` now reports `guard_blocked` when it actually
    intervened, and the check counts interventions — non-blocking, because a
    refusal that names "internal instructions" trips the guard legitimately.
  - **Second bug caught:** `force=true` recorded the override in
    `evaluation.forced` but `history()` never projected it, so the exception was
    invisible — defeating the point. History now returns a `forced` flag and the
    studio renders a "⚠ gate overridden" badge.
  - **`_ensure_baseline()`** captures a pre-U2 agent's live config as v1 on
    first touch, so the seeded/legacy agents have something to roll BACK to —
    otherwise the feature is useless exactly where it's needed most. Verified
    for store-helper / it-service / people-hr.
  - **`POST /custom-agents` (the studio's save) now writes a DRAFT**, and **fails
    CLOSED**: if the versioning module can't load it refuses the write rather
    than silently restoring write-straight-to-live. `AGENT_PUBLISH_GATE=0` is an
    explicit, documented escape hatch. Programmatic seeders (industry_packs,
    employee_service_seed) still call `upsert()` directly — they are reviewed
    code paths, and they now pass the reach invariant too.
  - `agent-studio.html` — a Release panel: 3-step progress, "unpublished draft
    vN · changes: instructions, scope · by <author>" state banner, **Run safety
    evaluation** (per-check PASS/FAIL/NOTE detail), **Publish** (disabled until
    the evaluation passes), Discard draft, and version history with a **↩ Roll
    back to this** button per superseded version. `edit()` resumes an open draft
    instead of showing the older live config. Save toast no longer claims
    "live now" when it made a draft.
  - **Verified:** create→draft (not live) · publish-without-eval refused ·
    evaluate 6/6 · publish→live · the "aggressive sales rep" edit stays off live
    until a human publishes it (and is attributed to its author) · rollback to v1
    published as v3 with append-only history · gate-off escape hatch · failing
    draft blocked · forced publish recorded and badged · all three pre-existing
    agents still answer with no regression. All probes cleaned.
  - **Deferred:** no side-by-side textual DIFF viewer (history lists WHICH fields
    changed, not the before/after text); no multi-person approval separation
    (the publisher can be the author — real maker/checker belongs with U4's
    ownership model); evaluation is synchronous (~10 LLM calls, a few seconds)
    and could move to a job for large agent fleets.
- **2026-07-25** — **Override auditability DEEPENED (bug 2, round 2).** The U2 fix
  made the override visible; the user's follow-up analysis correctly pushed
  further: *an override is not a flag, it is an auditable EVENT, because it
  changes what the result MEANS.* "Tests passed" and "tests failed, someone
  shipped anyway" must never render as the same state. Now:
  - `publish(force=True)` **requires a written reason** (≥10 chars, refused
    without one) and stores a structured `evaluation.override` = {forced_by,
    reason, failed_checks, original_result, evaluated}.
  - **`eval_passed` stays FALSE** — publishing does not retroactively turn a
    failed evaluation into a passing one.
  - Logged at **WARNING** (`GATE OVERRIDDEN — … despite ['required_fields'] —
    reason: …`) so it is findable in logs, not only in the UI.
  - `history()` projects reason / by / failed_checks / original_result; the
    studio shows a full-width **dark-red "LIVE ON AN OVERRIDDEN SAFETY GATE"**
    banner naming who, what failed and why — not a small badge.
  - The **force button only appears once the gate has actually blocked
    something**, so it reads as an exception rather than a second Publish, and
    it demands the reason in a prompt before sending.
  - New `gate_overrides(days)` + `GET /agent-gate-overrides` — the cross-agent
    feed (with `by_author` and `live_never_passed`) that U3 watches.
- **2026-07-25** — **U3 SHIPPED (platform self-observability), WIDENED beyond the
  original scope.** The original U3 was "watch the machinery." U1 and U2 changed
  the requirement: the platform can now make PROMISES (escalation SLAs) and grant
  EXCEPTIONS (gate overrides), and both can fail silently. So the module has
  **three layers**, per the user's framing:
  - `app/core/platform_health.py` → `GET /platform/health` (+ `/section/{key}`),
    `platform-health.html` (registered in `_CHAT_PAGES`), and **two supervisor
    detectors** (`detect_platform_degraded`, `detect_governance_drift`) so
    breaches ride the EXISTING alert path rather than a new channel.
    - **PLATFORM HEALTH** — event backlog (+age), inert-vs-handled split, failed
      events, stuck locks (>15m), LLM error rate, LLM latency, budget
      exhaustion. **`llm_usage.ok` and `.latency_ms` have been written since the
      meter shipped and were never read until now.**
    - **CUSTOMER OBLIGATIONS (U1)** — open / SLA-breached / SLA-at-risk /
      **unreachable** escalations. A promise nobody watches is the exact failure
      U1 existed to fix, reintroduced one level up.
    - **GOVERNANCE HEALTH (U2)** — gate overrides (24h/30d), agents live without
      a passing evaluation, approval queue depth + past-expiry + lapsed-undecided.
  - **TWO REAL FINDINGS on the first live run** (the point of building it):
    1. **The event queue is not draining.** 70 pending, oldest **320 hours** old.
       Depth alone was under threshold — so **age-based thresholds were added**
       (`PH_QUEUE_AGE_WARN_HOURS`/`CRIT_HOURS`, 6h/24h): a queue of 70 that has
       not moved in 13 days is stalled; a queue of 400 draining in seconds is
       healthy. Depth alone is the wrong alarm.
    2. **20 of those 70 are inert BY DESIGN** (`account.created`,
       `product.stock_changed` have no registered handler — agent_bus leaves them
       pending forever). Counting them would have put a permanent floor under the
       metric, so the dashboard would sit red on a healthy system and be ignored
       within a week. Backlog now counts **only event types with a registered
       handler** (`_handler_types()` off `agent_bus.HANDLERS`); the inert count is
       shown separately and never alerts. Live split: **50 real backlog / 20
       inert.**
  - Every probe is defensive (`_q()` returns None on any failure → that ONE
    metric degrades to `unknown` with a note). An observability tool that goes
    down with the thing it observes is worse than none.
  - The page uses the same three-way failure contract as the console
    (network / auth / server), 30s auto-refresh, and states the distinction from
    Agent Ops in its own footer.
  - **Verified:** live report renders all three sections; queue age escalates
    state to CRITICAL; handled/inert split correct; a real escalation forced past
    its deadline surfaced as `SLA breached: 1 [CRIT]` + `at risk: 1` +
    `unreachable: 1`; a forced publish surfaced as `gate_overrides: 1 [warning]`;
    both detectors registered in the supervisor loop (14 total) and firing;
    `/platform/health` + `/agent-gate-overrides` 403 unauthenticated;
    `/platform-health.html` served 200. All probes cleaned.
  - **Pure app code — no migration.** (Reads `escalations` from U1 and
    `custom_agent_versions` from U2; both degrade to `unknown` if unapplied.)
  - **Deferred:** no historical trend (every metric is point-in-time — a
    `platform_health_samples` table would enable "overrides per day this week"
    and queue-drain-rate); no per-handler error breakdown; the approval-expiry
    metrics arguably belong in the governance console too.

## Event-bus draining investigation (2026-07-25) — U3's first real catch

**ROOT CAUSE: the boot cutoff, not a dispatch failure.** `start_agent_bus()` set
`_CUTOFF = now()` on every boot, and `_claim_batch_sync` filters
`e.created_at >= cutoff`. Any event emitted while the process was DOWN became
**permanently** ineligible — never claimed, never retried, never failed, just
'pending' with attempts=0 forever. Recurs on EVERY restart; **production-relevant,
not local-only** (every Railway deploy opens a new orphan window — the
2026-07-19 cutover's manual `drain_backlog` was this same symptom).

**Evidence (decisive):** all 50 handler-backed pending events had
`attempts=0, locked_at NULL, last_error NULL, next_attempt_at NULL` — never
touched. `event_queue` had **zero** completed/failed rows ever. Meanwhile the
dev server on :8000 reported `ha: {role: leader, runs_singletons: true}` — the
consumer WAS running and ticking every 30s, claiming nothing and looking healthy.
A controlled tick with the cutoff widened claimed and completed 10 events with
correct results, proving claim → dispatch → complete works end-to-end.

**Answers to the 10 questions:** (1) boot cutoff; (2) yes, running as leader;
(3) no — the startup hook is correct; (4) no, discovery is correct but the
cutoff excludes them; (5) no lock/transaction problem *for this*, but see the
second bug below; (6) no — `run_once` try/except → `_fail_sync`, nothing
swallowed; (7) backoff was **partly** broken (second bug); (8) production-
relevant; (9) restart reset the cutoff and orphaned the gap — now fixed;
(10) yes, reproduced with synthetic events through every state transition.

**⚠ AUTOSEND=1 locally** — draining the 18 `order.status_changed` events would
have attempted 18 real order emails 8 days late. **Do not blind-drain.** (A
widened-cutoff test tick did hit 10 real events; every send was refused by the
verified-recipient check — 0 emails, 0 activities, 0 notifications. Defence in
depth held, but the queue must be drained deliberately, not casually.)

**SECOND BUG found by the regression test:** `_fail_sync` cleared `locked_by`
but left `locked_at` set, so the claim query's 5-minute stale-lock guard — not
`next_attempt_at` — governed retries. The configured exponential backoff was a
fiction until it exceeded 5 minutes (attempt 4). Both `_fail_sync` and
`_complete_sync` now clear `locked_at`.

**THIRD FINDING (corrects U3):** `_handler_types()` ignored
`AGENT_BUS_CATCHALL=1`, under-reporting the backlog wherever catchall is on
(it is, locally). With catchall every pending type is dispatchable, so there is
no inert class — the split now returns None and the label drops "(handled
types)". Live count moved 50 → 60.

**THE FIX (smallest correct):** `_resume_cutoff()` — resume from the last-settled
watermark (`max(event_queue.last_attempt_at)`, durable across restarts, no new
table) instead of `now()`; bounded by `AGENT_BUS_MAX_CATCHUP_HOURS` (24) so a
month-dead consumer reaches back a day; an explicit `AGENT_BUS_BACKFILL_MINUTES`
still wins; and **a NULL watermark keeps the original conservative `now()`** so a
fresh install with a large historical queue never mass-replays. Plus
`orphaned_sync()` — anything still stranded is COUNTED, logged at WARNING on
startup, exposed in `/agent-bus/status`, and shown in Platform Health, so the
decision to drain or discard is made by a person instead of by silence.

**Deployment:** pure app code, no migration. Optional
`AGENT_BUS_MAX_CATCHUP_HOURS` (default 24). **The 60 existing orphans are NOT
auto-drained by design** — with AUTOSEND=1 that is a deliberate call: run
`POST /agent-bus/drain` (or set AUTOSEND=0 first) once the stale order emails
are judged safe.

**Tests:** `tests/test_agent_bus_drain.py` — 9 passing, synthetic event type +
in-test handler so no business side effect can fire: downtime-gap catch-up,
catch-up bound, no-mass-replay, backfill override, end-to-end drain, the
regression itself (event emitted during downtime drains after restart), orphans
reported not silent, drain_backlog, and failure → backoff → 'failed' including
the `locked_at` release.

### U3 deferred items — reviewed
- **Queue drain rate — BUILT.** `completed` in the last hour + ETA to clear.
  Catches a stalled consumer independently of age or depth: pending work with
  zero completions in an hour is CRITICAL. This is the metric that would have
  caught the bug on day one.
- **Per-handler error breakdown — BUILT**, folded into the `queue_failed` detail
  and rendered only when the count is non-zero (a permanently empty table is
  noise; "which handler" is the first question once it isn't).
- **Approval-expiry placement — DECIDED: stays in Governance only.** Platform
  Health answers "is the machinery running" (something to restart); Governance
  answers "are our controls respected" (a human process). An approval lapsing
  undecided is a process failure. Duplicating it across sections would make the
  dashboard overstate how many distinct problems exist.
- **Historical trends — still deferred, with a design.** Needs a
  `platform_health_samples` table (key, value, state, at) written by a scheduled
  job, which is the only way to answer "overrides per DAY this week" or "is the
  queue draining faster than it fills". Deliberately not built now: drain rate
  covers the specific blindness that motivated it, and a sampler is a new table
  plus a new scheduled job — worth doing when a second trend question appears.

### Backlog disposition EXECUTED (2026-07-25, user-approved)

All 60 stranded events reached an explicit terminal state; `event_queue` is now
41 completed / 29 cancelled / **0 pending**, and linked `agent_inbox` went
**103 → 0** pending.

**Group A — replayed 31 through the REAL handlers** (`lead.created` ×17,
`lead.scored` ×14) with `AGENT_BUS_AUTOSEND=0` forced and dispatch restricted to
those two handlers (catchall off), so Group B could not be claimed even with a
365-day cutoff. 4 waves: **2 real firmographic enrichments** (the two live July-21
leads) + 29 `skipped: lead not found` → all `completed`; their 76 inbox rows
settled by `_complete_sync`.

**Group B — cancelled 29 without dispatch** (`order.status_changed` ×9,
`account.created` ×14, `product.stock_changed` ×6) via the new
`agent_bus.cancel_sync()`. Terminal state is **`cancelled`** — `completed` would
falsely assert the work was done, `failed` would falsely assert an error. Each
row carries a 6-field audit record MERGED into `error_context` (never
overwriting diagnostics): disposition / reason / decided_by / decided_at /
original_event_type / original_created_at.

**The 9 July-17 order events were NOT replayed.** Every one was `ready→shipped`
while all 9 orders are now `delivered` — a shipping notice ~8 days after
delivery would be duplicate, misleading customer communication. (All 9
recipients were also unverified, so the guard would have refused the send, but
the deciding argument is that the CONTENT is obsolete, not that a guard would
catch it.)

**New reusable helper — `agent_bus.settle_inbox_sync(cur, event_uuid, outcome)`**
is now the ONE place deciding what a settled inbox row looks like, so a queue row
can never reach a terminal state while its fan-out sits pending forever.
`_complete_sync` was refactored onto it. `completed → 'sent'`; `cancelled →
'read'`, matching the convention `notification_triage` already uses for machine
settlement ('sent' would claim work that never happened). It takes the caller's
cursor so settlement commits atomically with the queue transition. Note
`notifications` is a VIEW over `notification_recipients` + `notification_messages`
with INSTEAD OF triggers — updates work through it.

**Side effects — actual vs prevented.** Performed: `agent_blackboard +2` (the two
legitimate enrichments) and nothing else. Prevented: 9 stale shipping notices, 14
blackboard signals about deleted accounts, 6 stale stock signals.
**activities +0 · conversation_messages +0 · notifications +0 · leads +0.**

**All 12 verification points passed**, including: 60/60 terminal, 0 pending,
0 failed, 0 orphaned, 0 inconsistent (terminal event with pending inbox), 76
settled via `_complete_sync` + 27 via the cancellation path = 103, Platform
Health platform-section **all green**, drain rate correct ("queue empty"), the
9 tests still passing, and the live restart/cutoff checks (watermark resume, 24h
bound, NULL-watermark no-mass-replay, BACKFILL precedence, orphan visibility,
`locked_at` regression).

**Platform Health overall is still WARNING** — from GOVERNANCE (3 pre-U2 agents
live without a passing evaluation, 6 approvals pending / 2 lapsed), which is
correct and unrelated to the bus.

- **2026-07-25** — **U5 SHIPPED (LLM provider failover) — DISABLED BY DEFAULT.**
  Design v1→v3 in `docs/llm_provider_failover_design.md`; implemented at the ONE
  chokepoint with **zero agent-specific changes**. 28 new tests + 9 existing = 37
  passing; real end-to-end failover proven against the live Gemini key.
  - **Provider: Google Gemini, `gemini-3.5-flash-lite` for BOTH tiers** (user
    provisioned `GOOGLE_API_KEY`, overriding the design's Anthropic pick).
    Chosen on MEASURED evidence, not assumption: **3/3 `parse_ai_json`
    compatibility** on real agent prompt shapes, **p50 784 ms**.
    `gemini-3.5-flash` was rejected — p50 9,148 ms (11.7×) and truncated.
  - **🔴 SHIPS OFF, WITH TWO INDEPENDENT LOCKS.** The Google key is **free
    tier**, where content may be used to improve Google's models. Lock 1:
    `LLM_FAILOVER_ENABLED=0`. Lock 2: the **policy gate refuses
    CUSTOMER_EXTERNAL and INTERNAL_SENSITIVE data to any free-tier provider
    regardless of the flag** — a flag protects against forgetting, the gate
    protects against a deliberate flip that missed the tier upgrade. Verified
    both. `BUSINESS_INTERNAL` (our own briefings/planning) is still permitted.
    **To enable: upgrade the key to paid, set `LLM_ALT_TIER=paid`, then
    `LLM_FAILOVER_ENABLED=1`.**
  - `app/core/llm_router.py` — data-class classifier, failure taxonomy
    (A failover-eligible / B never / C decisions), policy gate, provider
    registry with capability profiles, health prober, and the route loop.
    Provider-SDK-free by design (`invoke` is injected), so it is testable with
    fakes and no network.
  - `llm_meter.MeteredLLM` is now the router (`ProviderRouter` alias).
    **Budget is decided ONCE before any provider is contacted** — a failover can
    never be a second bite at a spent budget. A caller's `except` still sees a
    provider exception, so all 39 deterministic fallbacks are untouched.
  - `sql/llm_usage_failover.sql` — **Railway needs this migration.** Adds
    `logical_request_id` / `attempt_number` / `is_final` + provider/failure/
    latency columns, **one row per ATTEMPT**. Backfill makes every pre-U5 row its
    own single-attempt logical request, so `usage_summary`, the CEO briefing cost
    line and U3's error rate keep their exact meaning (verified: 3,425 rows =
    3,425 final = 3,425 logical).
  - **Semantics fixed explicitly, not implicitly**: business error rate is per
    LOGICAL REQUEST (a recovered failover is a SUCCESS); provider error rate is
    per ATTEMPT (it counts the recovered failure — that is what detects a sick
    provider); cost sums ALL attempts; user latency is `total_latency_ms` on the
    final row, provider latency is per attempt.
  - **U3 tie-in**: new `failover_readiness` metric in Platform Health — a
    configured-but-unusable target is visible BEFORE an outage.
    `GET /llm/providers` + `POST /llm/providers/health` for pre-incident checks.
  - **Two design bugs found by testing the real key, both fixed before shipping:**
    1. **Health check by catalogue lookup was wrong.** `gemini-2.5-flash` and
       `gemini-2.5-flash-lite` are **LISTED by Google and 404 on
       generateContent** — the same failure mode as the misconfigured
       `gpt-oss:20b`. Usability is now proven by a **minimal real generation**.
    2. **The deadline arithmetic made failover unreachable.** A 30 s interactive
       deadline with a 30 s primary cap meant a primary timeout ate the whole
       budget. Rule now enforced by a test: **primary cap ≤ 40 % of the
       deadline** (interactive 12 s primary / 15 s alternate).
  - **Internal-tier content never egresses** — the LLM-layer analogue of U2's
    `reach_invariant`. `LLM_INTERNAL_STRICT=0` by default so enabling U5 changes
    no current behaviour (those callers already run on OpenAI); flipping it to 1
    is a deliberate migration once a local model is provisioned.
  - **Never fails over for**: AUTH, BAD_REQUEST, NOT_FOUND, CONTENT_POLICY
    (re-sending a refusal to another vendor is policy laundering),
    CONTEXT_LENGTH, APP_ERROR, BUDGET_EXHAUSTED, POLICY_FORBIDDEN.
  - **Deferred as U5b**: multi-provider embeddings. `semantic._embed` stays
    OpenAI-only — `nomic-embed-text`'s 768-dim vectors are not interchangeable
    with the 1536-dim index and a mid-flight swap would corrupt similarity
    search. The existing index is untouched.
  - New dependency `langchain-google-genai` (declared, imported lazily only on
    an actual failover). Not installed locally — U5 is off, so nothing needs it.
- **2026-07-25** — **U5 ENABLED (`LLM_FAILOVER_ENABLED=1`) on the local synthetic
  DB, and one more real bug fixed to make it actually work.**
  - **🔴 RESPONSE-SHAPE BUG — U5 would have silently bought NOTHING.** Gemini
    returns `.content` as a **LIST of content blocks**
    (`[{'type':'text','text':'…','extras':{…}}]`) while OpenAI/Ollama return a
    `str`. All 39 call sites do `resp.content.strip()` → `AttributeError` →
    caught by the caller's own `except` → **scripted deterministic fallback**.
    The router would have logged a successful failover while the customer still
    got the degraded answer U5 exists to prevent. Fixed with
    `llm_router.normalize_response()` applied BEFORE an attempt is marked
    successful: text blocks are joined to a string, **non-text blocks (thinking,
    signatures) are dropped so they can never reach a customer**, usage metadata
    passes through (token accounting survives), and a `str` `.content` is
    returned untouched (idempotent). 5 regression tests (24–28).
  - **Free-tier handling stayed HONEST.** Rather than have the user write the
    lie `LLM_ALT_TIER=paid` — which would follow the config to Railway where the
    data is NOT synthetic — added **`LLM_ALT_TIER_TRAINING_ACK=1`**: an explicit
    acknowledgement that a free-tier alternate may train on this content.
    `ALT_TIER` still truthfully reads `free`, and `readiness()` emits a loud
    warning so it can never become an unnoticed production posture.
  - **Verified working end-to-end through the REAL client** (`langchain-google-genai`
    installed): a `caller='sdr'` (CUSTOMER_EXTERNAL, interactive) request with a
    simulated OpenAI 503 failed over to Gemini and returned a genuine reply in
    ~2.5 s ("Yes, we happily ship right to Ontario… 3 to 5 business days");
    the JSON routing path also survived (`parse_ai_json` → `{mode: list,
    limit: 5}`); a healthy primary still never constructs the alternate.
    Telemetry: 4 logical requests / 7 attempts / 3 recovered. U3 shows
    `failover_readiness = ready`. **38 tests passing.** All probes cleaned.

- **2026-07-25** — **U4 SHIPPED (Action Authorization Layer).** Authored agents
  can now ACT, not just explain — the gap Agentforce's own hero examples are all
  about (*processing* a leave request, *resolving* a ticket). Safe by default:
  **no agent holds any grant** until an admin gives one. 46 tests passing.
  - `sql/agent_capabilities.sql` — **Railway needs this migration.** Three
    tables: `agent_capability_catalog` (what an admin has OPENED UP for
    granting — existing in `a2a.CAPABILITIES` is deliberately NOT sufficient),
    `agent_capability_grants` (which agent may use what, with per-grant
    constraints like `max_amount`), and `agent_capability_calls` (audit of
    everything an agent TRIED, **including refusals** — a different and more
    important question than what it said). Seeded conservatively: 6 reads +
    2 governed writes; nothing destructive is grantable.
  - `app/core/agent_capabilities.py` — `authorize()` / `invoke()` /
    `set_grants()` / `describe_for_prompt()` / `parse_action()` + admin router.
  - **The model: a granted capability set, never a tool box.** An agent is
    granted a curated subset of capabilities that ALREADY exist in
    `a2a.CAPABILITIES`, each already carrying its own validation, guardrails
    and audit. Four properties, all verified live:
    1. **An agent cannot invent a capability** — `data.erase_record` and
       `nuke.everything` both refused as "not grantable".
    2. **A WRITE never executes on the agent's decision.** It becomes a
       proposal in the SAME `action_approvals` queue an executive already
       ratifies (`proposed_by=agent:<slug>`, `confidence=0.0`). Verified:
       +1 pending approval, **+0 activities**.
    3. **An agent cannot widen its own grant** — grants are admin data, and a
       grant change is the most security-relevant edit possible, so it rides
       U2's draft → evaluate → publish gate.
    4. **Scope still governs reach** — an `external` agent was refused
       `accounts.query` (internal-only) and allowed `products.low_stock`.
       U2's `reach_invariant` extended from KNOWLEDGE to ACTIONS.
  - **Honest wording (U1's rule applied to actions):** a proposal is reported
    as *submitted for approval*, never as done. Claiming completion for
    something a human must still ratify is the exact broken promise U1 exists
    to prevent. A refusal tells the person it is outside what the agent can do
    **without** exposing the authorization model.
  - **Prompt contradiction fixed:** the hard-coded "You cannot take actions"
    rule is now conditional — leaving it in while an agent holds grants would
    make the system prompt contradict itself, which is how agents start
    improvising.
  - **🔴 REGRESSION FOUND BY THE U4 END-TO-END TEST — U5 had broken the internal
    agents.** The `INTERNAL_SENSITIVE` provider graph was local-only, so with
    Ollama unprovisioned the IT/HR agents got *"no provider was permitted"* and
    every reply collapsed to the scripted fallback. `LLM_INTERNAL_STRICT=0` was
    supposed to preserve pre-U5 behaviour, but the GRAPH bypassed the flag.
    Fixed: the graph now lists `["local", "primary", "alt"]` and **`may_send`
    decides** — local preferred, remote refused only when strict mode is on.
    Two regression tests (31, 32) pin both directions.

- **2026-07-25** — **U6 SHIPPED (MCP client) + VOICE gaps closed. Round-2 axis
  U1–U6 now COMPLETE.** 64 tests passing.
  - **U6** `app/core/mcp_client.py` + `sql/mcp_servers.sql` (**Railway needs
    this migration**). We already SERVED MCP (7 tools); now our agents can
    CONSUME external MCP servers.
    - **Deliberately rides U4 rather than inventing a second permission
      model.** A registered tool is PROJECTED into `agent_capability_catalog`
      as `mcp:<server>.<tool>` and inherits U4 wholesale — an agent must be
      granted it, cannot invent it, cannot widen its own grant, scope governs
      reach. Two authorization models would drift, and the weaker one would be
      the one that leaks.
    - **Three independent gates, each sufficient to refuse:** (1) server
      registered AND enabled — an allowlist, not discovery; (2) tool enabled
      individually — *listing a tool is not permission*; (3) **egress**:
      calling a third party sends our data out, so U5's data-class rule
      applies and INTERNAL_SENSITIVE is refused unless
      `allow_internal_data` is explicitly opted into.
    - Tools default to **`requires_approval=true`** — external tool code
      cannot be audited, so "we don't know what this does" is an argument for
      a human confirming, not for skipping the record.
    - **Revocation actually revokes**: disabling a server or tool deletes both
      the catalogue row and any grants, verified live — no stale grantable row
      left behind.
    - Credentials are referenced by env-var NAME, never stored; the server
      listing exposes only whether auth is configured.
  - **VOICE** — closed #2's deferred item 1 and #1's voice gap in
    `voice_support.py`:
    - **Recognition language and TTS voice now switch TOGETHER**
      (`_VOICE_BY_LANG`: en/fr-CA/es/de). This was the whole reason voice i18n
      was deferred — switching only the TTS voice means English STT on French
      speech, producing garbage the agent then answers confidently. fr-CA is
      first-class for the Canadian market.
    - **Detection is STICKY per call** — one ambiguous utterance ("ok") cannot
      flip the accent mid-call, which is more jarring than being wrong once.
    - **Human takeover now stands the AI down on voice** (`is_human_handled`),
      the check that previously covered only the email auto-reply. A localized
      hold message + `<Pause>` keeps the line open; a bare `<Redirect>` would
      have handed the turn straight back to the AI, defeating the takeover.
    - `VOICE_MULTILINGUAL=1` kill switch; the English path is unchanged.
  - `tests/test_voice_and_mcp.py` — 18 tests, no network and no phone calls:
    MCP gate behaviour, credential non-leakage, STT/TTS pairing across 4
    languages, sticky detection, unknown-language fallback, localized
    hangup/digits paths, and the hold-music regression.

- **2026-07-25** — **MANDARIN CHINESE (zh) added — and it exposed that the whole
  multilingual feature was Latin-script-only.** 76 tests passing.
  - **🔴 THE REAL FINDING (bigger than voice):** `language.detect()` is a
    stopword+diacritic scorer, so it is **structurally blind** to a language
    that does not put spaces between words. Chinese scored **zero on every set**
    and fell through to the English default — meaning a Chinese customer was
    silently getting ENGLISH replies on **every channel, text included**, not
    just voice. Adding a voice mapping alone would have done nothing, because
    detection never returned `zh`.
  - **Fix: script detection runs BEFORE the word scorer.** A Han character IS
    the signal — unambiguous, and needing no word segmentation (the absence of
    which is exactly why the word scorer could never see it).
    - **Japanese uses Han characters too**, so "has Han" is not "is Chinese".
      Kana and Hangul are the disambiguators, and both are checked **before**
      the Han threshold — Korean is usually written with no Han at all, so
      gating on a Han count first made the helper blind to the very scripts it
      exists to separate from Chinese. (Found by a failing assertion.)
    - `_SCRIPT_MIN_CHARS=2` — one pasted Han character in an English sentence
      must not flip the conversation, matching the Latin scorer's conservatism.
    - **ja/ko are detected precisely so they are NOT mislabelled as Chinese**,
      then fall back to English: detecting a script we cannot SERVE must never
      promise support we do not have.
  - **Simplified vs Traditional is not a style choice.** The reply directive
    tells the model to MIRROR the customer's character set (繁體 vs 简体) and
    never mix them in one reply — answering a Traditional writer in Simplified
    reads as wrong.
  - **Voice (Telnyx — the carrier actually in use, corrected mid-task):**
    `Polly.Zhiyu` (Mandarin) + `zh-CN` recognition. Telnyx TeXML accepts Polly
    voices as `Polly.VoiceId` and `alice`, same spelling as Twilio — so the TTS
    side is **confirmed**. Telnyx does **not publish** its `<Gather language>`
    value list, so **every recognition code is now ENV-OVERRIDABLE**
    (`VOICE_STT_ZH=cmn-CN` etc.): an unverified provider value must be a config
    fix, not a deploy — the same lesson as the missing `gpt-oss:20b` and the
    listed-but-404 Gemini models. Cantonese (zh-HK) is a DIFFERENT language and
    is deliberately not claimed.
  - **Verified live end-to-end on a text channel:** "你好，请问你们的退货政策是什么？"
    → detected `zh` → the store agent answered the full 30-day return policy in
    fluent Simplified Chinese, translated from the same approved ENGLISH KB
    article (governance and approval unchanged — only the surface language).
  - `tests/test_voice_and_mcp.py` grew to 30 tests (8 new for zh).
  - **⚠ Needs a live test call to confirm** the Telnyx Mandarin recognition
    code; if `zh-CN` is rejected, set `VOICE_STT_ZH` to whatever Telnyx accepts.

- **2026-07-25** — **U6 + Voice VERIFICATION SEQUENCE — all sections green,
  81 tests passing. One real bug found and fixed during the run.**
  - **🔴 BUG FOUND BY THE VERIFICATION ITSELF:** the env-override hooks I added
    for the unverified Telnyx recognition codes had opened a **half-switch
    hole** — someone could set `VOICE_TTS_ZH` without `VOICE_STT_ZH` (or vice
    versa), reintroducing the exact failure voice i18n exists to prevent
    (English STT on Mandarin audio → garbage the agent answers confidently).
    Added `_validated_pair()`: a mismatched pair is **refused** and the safe
    default pair for that language is restored, logged at ERROR.
  - **And the first version of that guard was ITSELF wrong** — it rejected
    `cmn-CN`, the ISO 639-3 code for Mandarin and the one Amazon Polly uses for
    Zhiyu, i.e. it blocked the very override it existed to enable. Fixed with
    `_LANG_SYNONYMS` (zh ≡ cmn). **`yue` (Cantonese) is deliberately NOT a
    synonym** — accepting it would silently answer Mandarin callers in
    Cantonese. Verified: `cmn-CN` and `zh-TW` accepted; `en-US`, `yue-HK` and a
    French TTS voice all refused.
  - **U6 (17/17):** all three gates verified with a two-tool server —
    unregistered refused, registered-but-disabled refused, listed-but-disabled
    tool refused, INTERNAL_SENSITIVE egress refused / BUSINESS_INTERNAL
    allowed. Projection verified: the ENABLED tool appears as
    `mcp:verify-mcp.lookup_shipment` with `requires_approval=true`, the
    DISABLED sibling does not. Governed invocation: **+1 pending approval, +0
    external calls executed.** Revocation verified: disabling the server
    removed the catalogue row AND the grant, and `authorize()` denied
    immediately.
  - **Voice (18/18):** all five STT/TTS pairs correct (en/fr-CA/es/de/zh);
    sticky detection holds across "ok" and a later English utterance; the
    English path is **byte-identical** with `VOICE_MULTILINGUAL=0`.
  - **Human takeover verified end-to-end on a live turn handler:** with
    `is_human_handled=true`, `take_turn` was called **0 times** (the AI truly
    stood down), the hold message was played in the caller's language
    ("Un instant — un membre de notre équipe se joint à l'appel.") with the
    French TTS voice, and `<Pause>` held the line rather than a bare
    `<Redirect>` handing the turn straight back.
  - **Migrations:** all five verified intact LOCALLY (escalations,
    custom_agent_versions, llm_usage_failover, agent_capabilities,
    mcp_servers). **Railway remains the user's to apply.**
  - No regressions. No residue: grants 0, mcp_servers 0, escalations 0,
    agent-proposed approvals 0, llm_usage consistent (3427/3427).

- **2026-07-26** — **Live Telnyx call debugged. Root cause: `VOICE_STREAM_ENABLED=1`.**
  88 tests passing.
  - **Diagnosis, from evidence not guesswork.** ngrok's request inspector
    (`127.0.0.1:4040/api/requests/http`) showed the real Telnyx POST **arrived,
    was Ed25519-signed, and returned 200** — so the webhook, the tunnel and the
    signature verification were all fine. The problem was the RESPONSE: with
    media streams on we returned
    `<Connect><Stream url="wss://…/voice/stream/sdr?…"/></Connect>` instead of
    the `<Gather>` conversational flow, and the call died at the WebSocket leg.
    Set `VOICE_STREAM_ENABLED=0` (user confirmed this had fixed it before).
    `stream_twiml()` now returns None so the inbound webhook falls back to
    `<Gather>`. **Requires a server restart** — settings are read at import.
  - **🔴 SECOND FINDING, more important than the outage:** the multilingual +
    takeover work had landed in `voice_support.py`, but the configured webhook
    points at **`/sdr/voice/inbound`** — a DIFFERENT module (`sdr.py`) whose
    TwiML builders were still hard-coded `voice="alice"` / `language="en-US"`.
    The line customers actually reach would have stayed English-only. Ported:
    - `sdr._say(text, lang)` / `sdr._gather(prompt, lang)` now switch
      recognition language and TTS voice TOGETHER.
    - **One source of truth**: `sdr._voice_pair()` reads
      `voice_support._VOICE_BY_LANG`, so it inherits the guard that refuses a
      half-switched pair. Two tables would drift.
    - Sticky per call via `_VOICE_LANG[call_sid]`, cleared on hangup; an
      ambiguous later utterance cannot flip the accent, and separate calls are
      independent.
    - **`is_human_handled` now also stands the AI down on the SDR line**
      (localized hold + `<Pause>`), matching the support line.
    - The inbound GREETING stays English by design — language cannot be
      detected before the caller has spoken.
  - Tests: `tests/test_voice_and_mcp.py` → 39 (4 new for the SDR line).
  - **Note for any future media-streams work:** `voice_stream.py` is still
    hard-coded to `language: "en-US"` (Azure) and `language: "en"` (Whisper).
    The i18n in this pass covers the `<Gather>` path ONLY.

- **2026-07-26** — **🔴 DESIGN FLAW in voice i18n found on a real call, and
  fixed.** 94 tests passing.
  - **The flaw:** voice language detection ran over the SPEECH TRANSCRIPT — but
    `<Gather>` commits to ONE recognition language, so Mandarin spoken into an
    `en-US` recogniser comes back as English-ish text. Running a detector over
    that can NEVER return `zh`. **You cannot detect a language from a transcript
    produced by the wrong recogniser.** Text channels work precisely because the
    customer's own characters arrive intact; voice is lossy and
    language-committed before we ever see it. Proven by the live call log:
    *"my name is Alex I want to speak Chinese"* came back as English.
  - **The fix — the caller DECLARES it by keypad**, which no recogniser can
    garble:
    - Greeting appends a short menu, each option spoken in its OWN language
      ("中文服务，请按 2。"), so a Mandarin caller hears something they understand.
    - `<Gather input="speech dtmf" numDigits="1">` accepts EITHER — the English
      caller just talks exactly as before, no menu interaction needed.
    - The digit is handled FIRST in the turn handler, before `heard` is
      consulted, because a keypad tone survives the wrong recogniser.
    - `set_call_lang()` pins the call; both recognition language and TTS voice
      switch together from that turn on.
  - **Subtle correctness point:** an `en` detection result no longer PINS the
    call. An `en` result from an `en-US` recogniser is not evidence — it is the
    only thing that recogniser can produce — so pinning on it would lock a
    Mandarin caller into English before they reached the menu. Only a
    NON-English detection pins (a bonus path for engines that do transliterate).
  - **Verified end-to-end live:** keypad 2 → line pinned to `zh`, TTS
    `Polly.Zhiyu`, STT `zh-CN`, prompt spoken in Chinese; then a real Chinese
    transcript through the SDR brain returned a fluent Mandarin answer (78 Han
    characters) grounded in the same approved English KB.
  - **Also fixed earlier in the same session:** calls were failing outright
    because `VOICE_STREAM_ENABLED=1` returned `<Connect><Stream>` instead of
    `<Gather>` (diagnosed from ngrok's request inspector — the Telnyx POST
    arrived, was signed, and returned 200; the RESPONSE was the problem), and
    the multilingual work had landed in `voice_support.py` while the configured
    webhook points at `sdr.py`.
  - **Deferred:** persisting the chosen language on the contact so repeat
    callers skip the menu (#2's deferred item 3 — needs a small migration);
    mirroring the keypad menu into `voice_support.py`; `voice_stream.py`
    remains hard-coded en-US.

- **2026-07-26** — **Voice language menu reordered to EN / FR / ZH / ES, and a
  related defect fixed.** 99 tests passing.
  - **Requested change:** `_LANG_MENU` is now `{1:en, 2:fr, 3:zh, 4:es}` —
    French moves to 2, Mandarin to 3.
  - **🔴 THE LIKELY REASON THE MENU "DIDN'T MATCH":** the whole menu was one
    English `<Say>`, so "中文服务，请按 2" was read by an ENGLISH TTS engine and
    came out unintelligible — **the caller who most needs that option is
    precisely the one who cannot understand it being offered.** Fixed: the menu
    is now ONE `<Say>` PER OPTION, each with that language's own voice
    (`alice` / `Polly.Chantal` / `Polly.Zhiyu` / `Polly.Penelope`). TwiML allows
    multiple `<Say>` elements inside a `<Gather>`.
  - **Drift made impossible:** `_LANG_MENU_TEXT` pairs each digit with the
    language it CLAIMS to route to, `lang_menu_twiml()` asserts that against
    `_LANG_MENU` at build time, and `test_40` asserts it in CI. A caller presses
    what they HEARD, so a spoken/routing mismatch sends them to the wrong
    language — and they cannot read the code to check. Explicit
    `_LANG_MENU_ORDER` (not dict order) drives the spoken sequence.
  - **All seven of the user's verification steps pass:** menu reads four
    options in the new order; press 2 → fr-CA + Polly.Chantal + French prompt;
    press 3 → zh-CN + Polly.Zhiyu + Chinese prompt; STT/TTS switch together on
    all four; choice sticky per call and independent across calls; human
    takeover still stands the AI down (converse called 0 times, localized hold,
    `<Pause>`); with `VOICE_MULTILINGUAL=0` the turn TwiML is byte-identical and
    the greeting omits the menu entirely (pure English, 2 `<Say>` elements).
  - `test_33` failed on the change and was updated — the ordering assertions
    caught the edit exactly as intended.

---

# Axis 5 — Record Integrity & Business Continuity (the FIFTH axis)

Prompted by a blind-spot pass against the Salesforce **product catalogue**
(salesforce.com/ca/products) rather than Agentforce — 2026-07-26.

**Core question:**

> Can every important business event become a durable, owned, routable,
> auditable CRM record that can continue through its full lifecycle?

The first four axes all interrogated the AGENT layer: can agents act (U4),
publish (U2), observe themselves (U3), survive failure and hold boundaries
(U5/U6)? This one interrogates the RECORD layer underneath:

> The previous axes examined whether the agent system can act, publish,
> observe and survive. This axis examines whether the underlying CRM can
> **represent, own, route, prove and monetize the work those agents create.**

**The central finding: the agent layer is more mature than the record layer.**
The system can conduct a service conversation but cannot reliably remember,
own, route and measure the service WORK as a business object. Today:

    Conversation -> Agent -> Escalation -> Human memory

What it must be:

    Conversation -> Agent -> CASE (owner, status, SLA, comments, history)
                          -> Assignment -> Resolution -> Knowledge -> Analytics

**The governing principle (user, 2026-07-26):**

> **An escalation is an EVENT. A case is the durable unit of WORK that event
> creates.** The system currently treats the event as the work record.

That is why current service metrics can mislead: `agent_ops` can report "the
conversation was resolved" but cannot answer who owned the work, first-response
time, reassignment count, reopen rate, which product/account was affected, or
whether the resolution produced reusable knowledge.

## Verified non-gaps this round (checked, don't rebuild)

Marketing is better covered than the agent axes implied: `campaigns`,
`campaign_members`, `marketing_campaigns`, `marketing_sends`, `coupons` /
`coupon_redemptions`, `email_templates`, `email_suppression`. Also real and
populated: `tax_rates`, `forecast_snapshots` / `sales_forecasts` /
`forecast_opportunity_entries`, `appointments` / `availability_rules`,
`price_match_requests`, `product_pricing`.

## Blindspots (ranked — user-revised order)

| # | Gap | Evidence | Why it matters | Effort | Status |
|---|-----|----------|----------------|--------|--------|
| C1 | **Cases: an ORPHANED SUBSYSTEM, not a missing feature.** The architectural cost of the data model is already paid; the integration was never done. | `cases` 120 rows + `case_comments` 480 rows, full lifecycle (new/in_progress/waiting/resolved/closed), `owner_id`, `closed_at` — **frozen since 2026-01-09**. No agent package, no `case-mgmt.html`, and exactly ONE code reference in the repo (`opportunities/formatter.py:293` printing a case_id). **CORRECTION 2026-07-26:** the original pass said "no SP" — that was filename-based and WRONG. A legacy `sp_cases()` function exists inside the schema dump and is LIVE in the database: 14 modes, writes `status` and `owner_id`, no `record_field_history` awareness. It is not integrated into the governed case architecture and has its own mutation semantics, so it is a SECOND case mutation boundary. The C1 gap stands — there is still no governed Cases agent, UI or integrated lifecycle — and the risk is STRONGER than first reported. | Service Cloud's core object. Everything was built AROUND it — escalations with SLA, KB loop, takeover console, CSAT proxy, containment metrics — so it LOOKS covered. Service conversations are active; service work is not durable. | Medium (integration, not new modelling) | **NEXT BUILD** |
| C2 | **Work has no allocation intelligence** — not merely "no queue". | `owner_id` exists on cases/opportunities; no queue, capacity, skill or language routing anywhere. `owners` (44) has `role` but no language/skill; `employees` (21) has department/job_title. | Once C1 is real, "who should own this?" is immediate. We now answer in 4 languages with SLA-bearing escalations and have no way to route a French case to a French speaker. | Medium | Open |
| C3 | **Commercial commitments disappear** (renamed from "quotes evaporate" — the problem is broader than quotes). | `quotes.py` prices, clamps discount, sends, logs an activity. There is **no `quotes` table** among 182. | The chain Opportunity -> Quote -> Version -> Acceptance -> Order -> Invoice is broken at its first link. The discount-clamp guardrail cannot be audited: nobody can later prove what was offered, to whom, at what price, under which policy, or whether the final order matched it. | Medium | Open |
| C7 | **No field-level history** — promoted to a CROSS-CUTTING constraint, not a late phase. | `audit_log` = (entity, entity_id, action, payload). Object-specific history exists only for stage/pricing/intelligence. | Three distinct questions: audit (who/when), **field history (old->new per field)**, provenance (`provenance.py`, origin of a value). Only the middle one is missing. **Constraint: do not build major new objects without deciding how their history is recorded** — so C1 must ship its own history seam rather than retrofit one. | Medium | Open (seam lands with C1) |
| C4 | **No recurring-revenue objects.** | No `subscriptions`, `contracts`, `renewals`, `entitlements`. Invoices are one-shot. | A data-model contradiction: we ship a **SaaS** industry pack and a Phase-7 churn scorer, but cannot express MRR/ARR, term, plan, seats, expansion/contraction — there is nothing to churn FROM. Build when the SaaS/renewal motion is strategic, not reflexively after C3. | Large | Open |
| C6 | **SLA is not entitlement-driven.** | `escalations.sla_minutes` derives from reason + priority only. | Every customer gets the same clock regardless of plan. Mature model: Customer -> Contract -> Entitlement -> Service Tier -> SLA Policy -> Case Deadline, with priority as ONE INPUT rather than the whole policy. Depends on C4. | Small (after C4) | Open |
| C5 | **No customer-side projection of the CRM** (not merely a missing UI). | Every page is `*-mgmt` (staff) + store-home + widget-demo. No portal code at all. | The hard parts exist: `customer_scope` fail-closed, OTP possession verification, identity resolution, auth. A caller can hear their balance on the PHONE with no web equivalent. **Design rule: the portal must not become a second CRM — it is a governed customer projection of the same system of record.** Worth far more once C1–C4 are complete. | Medium | Open |
| C8 | **45 legacy n8n tables in the schema.** | `credentials_entity`, `user`, `role`, `settings`, `variables`, `workflow_*`, `execution_*`; 8 non-empty. **Measured: 1.3 MB of 292 MB.** | **NOT a capacity problem** — do not confuse this with the Railway volume ceiling. It is architectural boundary clarity: a table named `credentials_entity` sits in the same database as a governed read compiler and NL->SQL explore mode. | Small | **DECISION, not cleanup** (below) |

## Decisions (not oversights)

- **C8 — legacy n8n schema is TOLERATED BUT ISOLATED.** It is an external
  orchestration subsystem, not part of the CRM business domain, and **must not
  become a source for governed business semantics** (semantic model, NL->SQL
  explore, metric registry). Deleting it is not prioritized; misclassifying it
  is the actual risk.
- Field Service, AppExchange-style marketplace and full multi-tenancy stay out
  of scope per the earlier axes. Nothing here changes that.

## Sequence

    Phase 1  C1  Make service work REAL          <- next build
    Phase 2  C2  Make service work ALLOCATABLE
    Phase 3  C3  Make commercial commitments DURABLE
    Phase 4  C7  Make state changes PROVABLE      (seam ships with C1)
    Phase 5  C4  Make recurring business EXPRESSIBLE
    Phase 6  C6  Make service promises CONTRACTUAL
    Phase 7  C5  Expose the CRM to CUSTOMERS

## The conclusion this axis reaches

> The correct next move is **not another agent capability**. It is to reconnect
> the agents to the CRM records that make their work persistent, assignable,
> auditable and measurable. A conversation should become a case; a case should
> become owned work; a commercial interaction should become a durable quote;
> and every important state transition should eventually be provable.
>
> This is where the system evolves from an **AI system operating around a CRM**
> into an **AI-native CRM whose agents operate through a complete business
> record layer**.

**C1 architectural requirement (non-negotiable):** do NOT build a Case CRUD
package. Reconnect the full lifecycle — conversation -> escalation -> case ->
assignment -> SLA -> comments -> resolution -> knowledge/analytics. Build plan:
`docs/case_lifecycle_design.md`.

---

## Axis 5 — ROADMAP SPLIT (ratified 2026-07-27)

Two roadmaps, kept separately and deliberately:

    IMPLEMENTATION ROADMAP   what to build next, given TODAY's architecture
    STRATEGIC ROADMAP        what the platform should eventually support

An item leaving the implementation sequence is NOT an item leaving the product.

### The product-shape decision this rests on (user, 2026-07-27)

> Conscestra CRM is a SINGLE-ORGANIZATION platform, not multi-tenant SaaS.
> Multi-tenancy is already an explicit deferred decision.
>
>   A detached house can be built first.
>   A condominium can come later.
>   The house must not be redesigned as a condo before its own architecture
>   is complete.

So recurring-revenue capability that exists mainly to OPERATE a SaaS platform —
tenant subscriptions, seat licensing, tenant billing, platform renewals — is a
future platform capability, not an immediate one.

### The C4 discovery that reordered the sequence (2026-07-27)

C4 was recommended as "most important, and it's not close". The discovery pass
contradicted it, and TWO CLAIMS IN THAT RECOMMENDATION WERE WRONG:

**Wrong claim 1 — "the churn model has nothing to churn from".** It has.
`intelligence.py` scores lateness (order_recency vs the account's OWN
typical_gap_days), engagement, overdue AR and recent lost deals. That is RFM
reorder-rhythm churn and it is CORRECTLY DESIGNED for a repeat-purchase
business. The absence of subscriptions was mistaken for the absence of a basis.

**Wrong claim 2 — "C6 is gated on C4".** It is not. A distributor tiers service
by ACCOUNT SIGNIFICANCE, which C2.1 already computes (open pipeline +
outstanding AR). SLA tiering is unblocked today and needs no contract object.

**What the data actually shows** (synthetic, but shape-revealing):

    categories      Electronics 101 · Health & Wellness 62 · Office Supplies 54
                    Grocery 51 · Home Essentials 50 · Apparel 50 · Pet Supplies
    price types     Wholesale · Promo · Retail   — NO recurring price type
    recurring-looking products   5 of ~400
    repeat buyers   144 of 163 ordering accounts · avg 8.3 orders · max 31

NOTE: the seed data is synthetic and does NOT determine the roadmap (user,
2026-07-27). It is recorded because it revealed that the CHURN MODEL and the
ORDER/INVOICE chain were built for a repeat-purchase motion — a real
architectural fact regardless of whose data fills the tables.

**Third time discovery changed the plan.** C1 found `cases` orphaned; C2 found
`owners` is 90% customer contacts; C4 found the recommendation itself was
wrong. Do the discovery pass first, every time.

---

## IMPLEMENTATION ROADMAP — build next

| Order | Item | Why now | Status |
|-------|------|---------|--------|
| ✅ | **C1 — Cases** | The orphaned service record, reconnected end to end | **COMPLETE** (9 steps + e2e) |
| ✅ | **C2 — Work routing & assignment** | Assignable identity + deterministic policy + language/skill/capacity | **COMPLETE** (C2.0 + C2.1) |
| ✅ | **C3 — Commercial commitments** | Quotes made durable; the clamp made auditable and visible | **COMPLETE** (C3.0 + C3.1) |
| **1** | **C5 — Customer portal** | HIGHEST LEVERAGE: it assembles what is already built. `auth_credentials` ALREADY keys on account_id/contact_id/lead_id — customer authentication exists and is unused. `customer_scope()` fail-closed isolation is proven on the voice line. Add C1 cases, C3 quotes, orders and invoices and the customer-side projection is assembly over existing security, not new architecture. | Next |
| **2** | **C6 — SLA tiering** | Unblocked by the C4 discovery: service levels can be driven by CONFIGURABLE BUSINESS RULES and account significance, not only by subscriptions. Closes C1 open design question 3 (does the case own its deadline?). | After C5 |
| — | **C7 — Field history** | Largely DELIVERED by C1: `record_field_history` is append-only with ONE shared writer (`app/core/history.py`), already serving cases and routing policy. Remaining: extend to `activities.owner_id`. | Opportunistic |
| — | **C8 — Legacy n8n schema** | DECISION MADE, not work: tolerated but isolated; must never become a source for governed business semantics. | Closed as a decision |

---

## STRATEGIC ROADMAP — eventually, not now

Nothing here is cancelled. Each is waiting on a product-shape decision rather
than on engineering capacity.

| Item | Waiting on | Why it will matter |
|------|-----------|--------------------|
| **C4 — Recurring revenue** (subscriptions, contracts, entitlements, renewals, MRR/ARR, seats, expansion/contraction) | A subscription business model — either Conscestra priced as a subscription inside its own CRM, or customers whose businesses are subscription-based | The object family that makes recurring commitments expressible. C3's `quote_lines` is one-time-sale shaped (quantity × unit_price, no term or billing frequency), so a quote for anything recurring has no home today. When C4 lands it also completes C3's right-hand end and gives C6 a contractual basis in addition to the account-significance basis C6 will ship with. |
| **Multi-tenancy (P4)** | Selling to multiple organisations on one instance | Phase 0 seam already shipped (`tenancy.py`, one `get_connection` chokepoint). The principle stands: postpone it, do not make it impossible. |
| **Non-superuser database role** | A platform-security work item, deliberately OUTSIDE Axis 5 | The application connects as a PostgreSQL superuser that owns every object, so no database-level privilege control can bind it. `sp_cases()` is held closed at the application layer alone until this changes. |
| **Closed-case reopening** | A durable parent/relationship design | `reopen()` refuses a closed case and names the gap. No `parent_case_id` was added speculatively. |
| **Origin vocabulary canonicalisation** | A deliberate analytics/data-contract decision | `webchat` / `sdr_chat` / `store_chat` / `voice` coexist with the legacy `chat` / `email` / `phone` / `web`. Raw values preserved; no mapping invented. |
| **`CASES_KB_FEEDBACK=1`** | The 7-step activation review | Case-derived knowledge candidates, governed. Flag is a genuine activation control, not decoration. |

---

# Customer Memory — Phase Plan (2026-07-31)

> Supersedes the "build more mechanism" roadmap. Eleven adversarial review
> rounds converged on one conclusion: **the architecture was consistently
> correct and the implementation consistently violated it.** Rounds 8–11
> eliminated the implementation defect CLASSES (see below). What remains is not
> more mechanism — it is evidence that the mechanisms are accurate, useful and
> valuable. Engineering hardening is ~85–90% done; empirical validation is ~5%.

## Defect classes eliminated (rounds 8–11) — do not rebuild

| Class | Killed by | Layer |
|---|---|---|
| Un-provenanced irreversible mutation | `sql/governed_mutation.sql` — trigger logs every governed deletion as a JSONB image | DB |
| Review surface ≠ enforcement surface | `gate_inputs()` + `GATE_COLUMNS`, one assembly point | code |
| Safety gate failing OPEN on missing input | gate blocks on absent claim hash / signature | code |
| Erasure that silently deletes nothing | statement-level trigger + working escape hatch | DB |
| PII unreachable by erasure | `entity_id` denormalised onto the trail | DB |
| Connect-per-call | pooled factory (37×; suite 112s→24s) | code |
| Migrations applied by hand | `scripts/migrate.py`, ordered + checksummed | ops |
| Tests skipping silently (57%) | `tests/conftest.py` + `CRM_REQUIRE_DB=1` | ops |
| No CI at all | `.github/workflows/ci.yml` + `scripts/verify_invariants.py` | ops |
| Un-rotatable signing key | `keyid:digest` keyring, unknown id fails closed | code |
| Security control whose behaviour depends on data | row-level → statement-level trigger | DB |

**Governing principle: if a rule can be forgotten, it is not a rule.** Every
fix moved enforcement down a layer, because every defect found in eleven rounds
lived where a human was trusted to remember.

## THE FINDING THAT REORDERS EVERYTHING

Measured on the live corpus (765 memories, 8,022 indexed records):

```
evidence by visibility : internal 7,568 | customer 454      (94% internal)
evidence by source     : activity 7,250 (90%) | case_comment 486 | conversation_message 163
memories by actor      : company_did 762 | unknown 12 | mixed 5 | customer_did 4
delivered to INTERNAL agent : 86    delivered to CUSTOMER agent : 0
customer-visible memories   : 1 of 765        assertable : 0
```

**97% of "Customer Memory" is `company_did` — our own outbound activity log,
consolidated and correctly labelled.** Every mechanism works. The corpus is the
problem: ~73% of it is tasks (our to-do list), and only 9.6% carries customer
voice.

Consequences:
1. **Shadow mode on the customer path would measure nothing** — the treatment
   arm is byte-identical to control on every utterance.
2. **Measuring topic-classifier precision on this corpus answers the wrong
   question** — it measures how well we classify our own to-do items.

## Phase order

### Phase 1 — Deploy safely  ← BLOCKING
Every guarantee is currently true only on one laptop; production still runs
broken erasure semantics. `scripts/migrate.py` (14 migrations, 2 pending).
Full checklist: rollback, lock/downtime risk, integrity checks, post-deploy
validation — see the deployment checklist in the round-11 report.
- Destructive purge REMOVED from the migration: unattributable verification
  rows are quarantined to `memory_verifications_unattributable` and require an
  explicit `purge_unattributable_verifications()`. A migration must never
  silently destroy audit history nobody has looked at.
- Only real stall: 3 × `CREATE TRIGGER` (ACCESS EXCLUSIVE, metadata-only).
  Use `lock_timeout='5s'` and retry rather than queueing behind long txns.
- Retention on `governed_deletions` ships WITH this phase (~1.9 KB/deletion).
  Railway volume resized to 1 GB 2026-07-31, so this is hygiene, not urgency.
- **Deployment is the user's to run. Claude does not touch Railway.**

### Phase 1.5 — Evidence-base repair  ← NEW, from the finding above
Either index the customer-voice sources that today contribute 9.6%
(conversations, case comments, inbound email/call transcripts), or rename the
product to what it is and scope its claims accordingly. Both legitimate;
neither-of-the-two is not. **Do this before Phase 3 or Phase 3 measures noise.**

### Phase 2 — Shadow mode, INTERNAL path first
Record production response + memory-enhanced candidate; expose neither. Label:
accepted / rejected / hallucinated / unnecessary / harmful + latency +
confidence. Target the internal agent path, where 86 memories demonstrably
flow. `autonomy_ready` bar: ≥500 utterances, ≥100 reviewed, zero harmful.
Currently 1 / 0.

### Phase 3 — Scientific validation
Labelled benchmark; inter-reviewer agreement; topic precision/recall;
clustering precision & recall; attribution and actor accuracy; confidence and
decay calibration; evidence completeness; usefulness. Statistical methodology
and pass/fail thresholds stated in advance.

### Phase 4 — Continuous memory-quality benchmark
Every build evaluated; no deploy if regression exceeds threshold. The AI-quality
equivalent of unit tests.

### Phase 5 — AI-specific observability
Generation / verification / rejection / conflict rates; stale memories;
confidence, decay and topic distributions; reviewer agreement; gate failures;
retrieval and embedding latency; shadow acceptance; memory drift.
Already shipped: `v_governed_deletion_activity` (undeclared-deletion spike).

### Phase 6 — Red team
Compromised app server; compromised DB credentials; malicious admin; malicious
employee; prompt injection; poisoned CRM data; replay; migration rollback;
replica inconsistency. **Standing gap: the app connects as a PostgreSQL
superuser, so no DB privilege can bind it** (see Deferred items above).

### Phase 7 — Scalability validation
100k → 1M → 10M → 100M. Indexing, ANN migration, sharding, partitioning,
batching, incremental consolidation, caching, latency and cost. Current
evidence stops at 8,022 records; pooling has ONE suite run behind it and
concurrency defects do not appear in single-threaded tests.

### Phase 8 — Enterprise readiness
SOC 2, ISO 27001, ISO 42001, GDPR, auditability, DR/BC, key management, secrets
rotation, change management, tenant isolation (currently **0 tenants**),
runbooks, support, upgrade strategy.

### Phase 9 — Post-deployment business review
Does it measurably improve CSAT, agent productivity, resolution time,
conversion, retention, efficiency? Separate engineering correctness from
business outcome; name anything sophisticated but low-value and simplify.
**Requires real production evidence. Do not simulate it** — inventing
production numbers is the exact failure eleven rounds went to eliminate.
