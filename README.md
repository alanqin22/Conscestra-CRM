# Conscestra CRM — The Conscious Orchestration of Customer Intelligence

> ### 🚀 **Live Demo → [agentorc.ca](https://agentorc.ca/)**
>
> No signup required. Drop into any module and talk to the AI agents
> directly, or use the inline forms — same experience either way.

> **Where traditional CRMs record the past, Conscestra orchestrates what happens next.**

An AI-native CRM where a symphony of specialized agents — guided by an intelligent
Orchestrator — continuously responds to your business in real time.

- New sign-ups become verified customers.
- Orders automatically notify buyers.
- Overdue invoices compose and send their own reminders.
- Alert storms resolve themselves before they become crises.
- Leadership wakes to a daily briefing — and a live dashboard tracking every trend.

Simply ask in natural language, and every answer is delivered with a transparent,
verifiable audit trail.

**[Explore the Live Demo →](https://agentorc.ca/)** &nbsp;·&nbsp; See How the Agents Cooperate

_Safe by default · Opt-in outreach · Fully audited · No sign-up to explore_

---

## 🆕 What's New — July 2026

**Data security is the top priority at Conscestra CRM.** This release hardens
every layer of the platform — identity, authorization, transport, and session
governance — while keeping the live demo free to explore.

- **🔐 Defense-in-depth write protection** — every create / update / delete now
  requires an authorized sign-in, enforced **twice**: once at the API gate and
  again at the single database chokepoint. Even a free-text AI command
  ("delete invoice 1001…") from a read-only caller is stopped before any SQL
  runs. Financial operations — generate invoice, record payment, void
  invoice — are explicitly covered.
- **🛡️ One-switch security posture** — `API_SECURITY_MODE` = `open` ·
  `public-read` · `locked`. The frontend reads the live posture and adapts:
  read-only browsing with a polished sign-in dialog under *public-read*, a
  firm "Back to Home" under *locked* — and a blocked action never loses your
  in-progress form.
- **📧 Admin-only Email module** — reading and sending mail as the company
  mailbox always requires an administrator sign-in, in **every** posture. The
  public Contact Us form stays open.
- **🚦 Brute-force protection** — per-IP and per-account sign-in rate limiting
  with lockout, password-reset throttling, and `[security]`-tagged audit
  logging on every failure.
- **⏱️ Session governance** — DB-backed sessions that store only SHA-256 token
  hashes, a 15-minute sliding idle timeout with an 8-hour absolute cap, true
  server-side sign-out, and a signed-in chip with one-click **Sign out** on
  every page.
- **🌐 Locked-down CORS** — the API accepts browser calls only from
  agentorc.ca origins (environment-configurable).
- **👥 Role tiers (RBAC)** — admin / member / viewer resolved at sign-in and
  enforced end-to-end, including natural-language writes.

**Also new: every AI agent now reaches beyond the CRM — onto the live
internet, into your calendar and accounting tools, and out to any AI assistant
via MCP.**

- **🌐 Live Web Intelligence** — all 11 agents + the Orchestrator answer from
  the live internet with cited sources ("Search the web for…"): free-forever
  search (DuckDuckGo → Tavily fallback) with page synthesis, rendered as a
  formal *Web Intelligence Briefing* card. Every module page has a
  *Live web lookup* chip panel.
- **🔌 MCP Server** — connect Claude (or any MCP-capable assistant) directly to
  Conscestra: ask any agent, pull the company pulse, run AI summaries, dispatch
  A2A capabilities (dry-run by default, governance-gated writes).
  `app/mcp_server.py` + `.mcp.json`, docs in `docs/mcp_server.md`.
- **🧠 AI 360 Summaries** — one click on an account, contact, or deal produces a
  decision-grade synthesis (Snapshot · Momentum · Risks · Next actions) that
  folds in live cross-agent blackboard signals.
- **📨 Governed Autosend is live** — overdue invoices now *send* their own
  reminders: CASL consent + one-click unsubscribe + verified-recipient gate +
  a human **approval-queue console** (`governance-mgmt.html`) for
  medium-confidence actions. Company sender identity is managed in the
  Executives console.
- **📈 Executive briefings with live market intel** — the CEO/CFO/CRO/COO
  morning emails each carry a live-web section (market trends, rates & FX,
  competitive watch, supply chain) with cited sources.
- **⚡ Real-time everything** — Server-Sent-Events push for notifications (no
  polling), an every-2-hours triage sweep that digests/resolves alert noise,
  and an Orchestrator catch-all that settles *every* bus event with a proper
  mark — plus a root-level gate so noise never enters the work queue at all.
- **🔗 Open integrations** — subscribe to CRM activities from Google/Outlook
  Calendar (ICS feed) and export invoices/payments as QuickBooks-ready CSV
  (`docs/integrations.md`).

---

### Conscestra CRM — The Conscious Orchestration of Customer Intelligence

The name itself reveals the philosophy. **Conscestra** combines *conscious* and
*orchestra* — an AI-native CRM that does not simply collect customer data; it
**conducts** an intelligent symphony of business knowledge, relationships, and
decisions.

Traditional CRM systems are digital filing cabinets. Conscestra is the conductor. A
network of specialized AI Agents — each an expert in its own domain yet continuously
aware of the larger business context — performs in harmony, transforming isolated
records into coordinated intelligence.

At the center stands the **Orchestrator**, a master AI Agent that directs the Lead,
Contact, Account, Opportunity, Activity, Order, Product, Accounting, Analytics,
Notification, and Email Agents. These agents do not operate as independent silos;
they collaborate over a live **event bus** as one responsive system. When a visitor
signs up and verifies their email, the Lead, Account, and Contact Agents turn them
into a customer in the same motion. When an order is placed — and again when it
ships — the Order and Email Agents greet the buyer with confirmation and dispatch
notices. When an invoice falls overdue, the Accounting Agent drafts a tiered reminder
for the Email Agent to deliver; when the payment settles, the Activity Agent quietly
closes the loop. Every interaction flows through a governed memory architecture that
is transparent, traceable, and replayable — ensuring powerful AI collaboration never
becomes an invisible black box.

**Simply ask, and Conscestra responds.**

Executives can engage the Orchestrator in natural language and receive
**decision-grade insights** about sales pipelines, revenue forecasts, customer
relationships, outstanding invoices, operational performance, and team productivity —
the system fields hundreds of executive questions a day. No searching through
dashboards. No waiting for reports. Every answer is grounded in real-time business
events, structured knowledge, and a verifiable audit trail. When uncertainty exists,
the system acknowledges it — because trustworthy AI begins with intellectual honesty.

*This is the essence of conscious orchestration.*

As pioneers such as Nobel laureate Geoffrey Hinton continue to reshape our
understanding of artificial intelligence and machine awareness, Conscestra embraces a
practical vision: AI should understand the context, relationships, and evolving state
of a business — not merely execute isolated commands. **Intelligence without awareness
is automation; awareness without accountability is risk.** Conscestra unites both.

With role-based governance, OTP-verified identities, encrypted credentials, recorded
consent, immutable audit histories, and fully explainable agent interactions, every
action can be understood, verified, and trusted. Autonomous outreach is opt-in and
safe by default — agents draft before they send, and reach only customers who have
verified their address and chosen to hear from you.

Conscestra CRM is not merely a system of record.
It is a **system of understanding**.
A symphony of AI Agents conducting your business in perfect **resonance**.

---

Conscestra CRM is a full-stack AI CRM where every module is a **conversational
agent**: ask in plain English, get the answer (and the chart), no menus
to hunt through.

### What you can try in the live demo

_Listed in the same order as the launcher page on [agentorc.ca](https://agentorc.ca/) — start at Store Home, work clockwise through the planets, end at the Orchestrator._

| Module | What to ask / try |
|---|---|
| 🛒&nbsp;Store | **CRM Universe Hub** — the landing page. KPI tiles for Active Pipeline / Open Leads / Pending Orders / Unread Alerts, plus the module launcher |
| 🎯&nbsp;Leads | _"convert Maria's lead"_, _"score this lead"_ — deterministic Fit + Intent lead scoring (Cold/Warm/Hot), inline forms on vague intent, one-click conversion, qualify/disqualify, duplicate merge, archive & restore |
| 👤&nbsp;Accounts | _"find Apple"_, _"new prospect account"_ — voice typeahead, 360° timeline & financials, overdue-invoice and no-phone drill-downs |
| 📇&nbsp;Contacts | _"show me Bob Brown's contact details"_ — duplicate detection, account roll-up, archive & restore |
| 💼&nbsp;Opportunities | _"add product to opportunity"_ — stage pipeline, weighted forecast, win-rate analytics by owner & lead source |
| 📦&nbsp;Products | _"low stock under 70"_, _"bulk stock adjustment"_, _"price history for Lenovo"_ — wholesale-≤-retail enforced at DB level |
| 📑&nbsp;Orders | _"create order for ABC Corp"_ — line-item editor, status workflow, invoice generation |
| 💰&nbsp;Accounting | _"Accounting Summary"_ — AR aging, cashflow, account-margin analytics, product profitability, forecast accuracy — all real-time |
| 📝&nbsp;Activities | _"create task for Bob tomorrow"_ — typeahead Related-Name lookup across every entity |
| 📊&nbsp;Analytics | KPI dashboards driven by Postgres stored procedures, rendered with Chart.js |
| 🔔&nbsp;Notifications | Real-time activity stream with unread/read state |
| 📧&nbsp;Email | Outbound mail + inbox + autonomous inbound auto-reply (SMTP/IMAP + LangGraph), plus event-driven order-confirmation / shipped emails to verified customers |
| 🧭&nbsp;Orchestrator | _"daily briefing"_, _"pipeline health"_, _"company pulse"_ — symphonic workflows that fan out to multiple agents and weave the results into one report, plus a curated executive Q&A bank for CEO/CFO-style questions |

### Why it's interesting

- **11 conversational AI agents** (Accounts, Contacts, Leads, Opportunities, Orders, Products, Activities, Notifications, Accounting, Analytics, Orchestrator) — each a LangGraph state machine with deterministic pre-routing plus an LLM fallback.
- **Orchestrator agent** — symphonic multi-agent workflows (Daily Briefing, Pipeline Health, Revenue Snapshot, Weekly Report, Team Activity, New Business, Follow-ups Due, System Alerts, Company Pulse) plus an executive Q&A bank shared with every other agent for interrogative "executive question" phrasings.
- **4 supporting modules** — Email (SMTP/IMAP + LangGraph), Store (direct-SP catalogue), Auth (direct DB), Voice (Azure Speech token mint), plus a Home-Index KPI dashboard.
- **Agents that cooperate automatically** — an event-driven cooperation bus lets agents react to each other's work instead of waiting to be asked: a store order makes the Email agent send order-confirmation and shipped notices to the buyer (real SMTP, gated on a **verified, opted-in** address), overdue invoices auto-draft tiered dunning, hot leads auto-schedule outreach, and settled milestones self-complete — all idempotent and safe-by-default (`AGENT_BUS_AUTOSEND=0` keeps it draft-only until you flip it on).
- **Self-serve signup that actually converts** — a store signup creates a lead and sends an OTP verification email; on verify it **auto-converts to a verified account + contact** (authored by the owning AI agents), carrying the buyer's firmographics and **CASL/GDPR marketing consent** — so checkout then flows straight through with no manual data entry.
- **Hybrid routing** — common intents (search, list, update, delete) skip the LLM entirely for sub-second response; novel phrasings fall through to GPT-4o-mini.
- **Voice everywhere** — Azure Speech SDK (Bing-style) primary, Web Speech API fallback, with BFCache-safe cleanup across navigation.
- **Real analytics, not toys** — invoice-level cost/margin tracking, effective-dated wholesale/retail pricing, AR aging buckets, forecast attainment, data-quality badges, and a DB-level `wholesale ≤ retail` trigger.
- **Self-correcting financial data** — invoice ↔ payment triggers recalc balance and status with a rounding-tolerant settlement rule (near-paid invoices settle instead of lingering as "overdue"), the accounts→events trigger is idempotent (no duplicate auto-created deals), and the AR executive view reports outstanding / past-due on a materiality basis — so the receivables figures stay honest.
- **Deterministic lead scoring** — a transparent Fit + Intent model (`fn_score_lead`, `rule_based_v1`) replaces random scores with explainable, audited Cold/Warm/Hot ratings; the scoring function is a single swappable API, ready to drop in a predictive model later.
- **Executive Q&A bank** — every agent answers interrogative "executive questions" through a shared decision-grade layer (headline, confidence, drivers, recommended action + owner, drill-down link), with each capability chip routed to a distinct section set.
- **Nightly automation** — an APScheduler job set (pipeline progression, order-status advancement, daily seed data, activity sweep) runs at **10 PM US Eastern (DST-aware)** so the same wall-clock time fires on both the Railway and local databases.
- **Single FastAPI + LangGraph server** — zero duplicated config, one DB connection pool, shared session memory across all 15 modules.

---

## Project Structure

```
crm_agent/
├── main.py                        ← Top-level entry point
├── requirements.txt
├── .env                           ← Single config file for all agents
├── sp/                            ← PostgreSQL stored procedures
├── *-mgmt.html                    ← One frontend per agent (incl. orchestrator-mgmt.html)
└── app/
    ├── main.py                    ← FastAPI app — registers all routers
    ├── core/                      ← Shared utilities (zero duplication)
    │   ├── config.py              ← Conscestra Settings (pydantic-settings)
    │   ├── database.py            ← Generic execute_sp() + test_connection()
    │   ├── memory.py              ← Shared session memory (rolling window)
    │   └── graph_utils.py         ← AgentState, LLM factory, JSON parser,
    │                                 build_standard_graph()
    └── agents/
        │  ── 10 conversational AI agents (LangGraph + pre-router + LLM) ──
        ├── accounts/         prompt | pre_router | sql_builder | formatter | graph | router
        ├── contacts/
        ├── leads/
        ├── opportunities/
        ├── orders/
        ├── products/
        ├── activities/
        ├── notifications/
        ├── accounting/
        ├── analytics/
        ├── orchestrator/     ← symphony workflows + executive Q&A bank (router | executive)
        │  ── 4 supporting modules ──
        ├── home/                   ← sp_home_index dashboard (direct SP, no AI)
        ├── email/                  ← SMTP/IMAP + LangGraph + autonomous auto-reply poller
        ├── store/                  ← Commerce catalogue view (direct SP, no AI)
        ├── auth/                   ← Signup / signin / password-reset (direct DB, no AI)
        └── voice/                  ← Azure Cognitive Services token mint for browser STT
```

---

## Quick Start

```bash

python -m venv venv

.\venv\Scripts\activate

# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env .env.local   # then edit .env with your DB_DSN, LLM settings
# OR just edit .env directly

# 3. Run
python main.py
# or
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## API Endpoints

| Method | Path                         | Description                                    |
|--------|------------------------------|------------------------------------------------|
| GET    | `/`                          | Service info + endpoint catalogue              |
| GET    | `/health`                    | Aggregate health check (all modules + DB)      |
| GET    | `/home-index`                | Home dashboard KPIs (direct SP)                |
| POST   | `/account-chat`              | Accounts agent                                 |
| POST   | `/contact-chat`              | Contacts agent                                 |
| POST   | `/lead-chat` (`/leads-chat`) | Leads agent                                    |
| POST   | `/opportunity-chat`          | Opportunities agent                            |
| POST   | `/order-chat` (`/orders-chat`)| Orders agent                                  |
| POST   | `/prod-chat`                 | Products agent                                 |
| POST   | `/activity-chat`             | Activities agent                               |
| POST   | `/notifications-chat`        | Notifications agent                            |
| POST   | `/accounting-chat`           | Accounting agent                               |
| POST   | `/analytics-chat`            | Analytics agent                                |
| POST   | `/orchestrator-chat`         | Orchestrator agent (symphony workflows + executive Q&A) |
| POST   | `/email-chat`                | Email agent (SMTP/IMAP + LangGraph)            |
| POST   | `/store-chat`                | Store catalogue (direct SP)                    |
| POST   | `/auth/{signin\|signup\|signout\|...}` | Auth (direct DB, no AI)              |
| GET    | `/voice/azure-token`         | Azure Speech short-lived token for browser STT |
| GET    | `/sessions`                  | List active memory sessions                    |
| DELETE | `/sessions/{id}`             | Clear a session's memory                       |

Each AI agent also exposes a matching `GET /<name>-health` endpoint that
returns `graph_ready: true/false` so a load balancer can verify the agent's
LangGraph compiled cleanly on boot.

---

## Architecture

Every agent follows the same deterministic-first pattern:

```
Request
  └─► FastAPI router.py
        └─► graph.py
              └─► pre_router_node
                    ├─[ROUTED]──► db_node ──► formatter_node ──► Response
                    └─[PASSTHRU]► ai_agent_node
                                    └─► parse_output_node
                                          ├─[JSON found]──► db_node ──► formatter_node
                                          └─[no JSON]─────► formatter_node
```

Shared utilities in `app/core/`:
- **config.py** — one `Settings` class, one `.env` file
- **database.py** — one `execute_sp(query)` function works for any SP
- **memory.py** — one session store, all agents share it
- **graph_utils.py** — `AgentState`, LLM factory, JSON extractor,
  `build_standard_graph()` — each agent calls this with its own nodes

---

## Adding a New Agent

The 10 conversational agents are all in place. To add an 11th:

### 1. Create the agent package
```
app/agents/<name>/
    __init__.py
    prompt.py        ← Domain-specific system prompt
    pre_router.py    ← Deterministic intent matchers (form-marker pattern supported)
    sql_builder.py   ← build_<name>_query()  → sp_<name>(...) call
    formatter.py     ← format_response()     → markdown the frontend renders
    graph.py         ← LangGraph nodes; call build_standard_graph()
    router.py        ← FastAPI POST /<name>-chat + GET /<name>-health
```

### 2. Register in app/main.py
```python
from app.agents.<name>.router import router as <name>_router
app.include_router(<name>_router)
```

No changes to `app/core/` are ever needed — the shared utilities work for any agent.

---

## Shared Core — What Is Consolidated

All 15 modules pull from `app/core/` rather than duplicating boilerplate.

| Concern            | Per-agent duplicate (the old way)   | Consolidated in crm_agent              |
|--------------------|--------------------------------------|----------------------------------------|
| Settings           | `app/config.py` × 15                | `app/core/config.py` × 1              |
| DB connection      | `app/database.py` × 15              | `app/core/database.py` × 1            |
| Session memory     | `app/memory.py` × 15                | `app/core/memory.py` × 1              |
| LLM factory        | `_get_llm()` duplicated × 15        | `graph_utils._get_llm()` × 1          |
| Ollama direct call | duplicated × 15                     | `graph_utils._call_ollama_direct()` × 1 |
| JSON parser        | duplicated × 15                     | `graph_utils.parse_ai_json()` × 1     |
| Graph topology     | duplicated × 15                     | `graph_utils.build_standard_graph()` × 1 |
| Server entrypoint  | `main.py` × 15                      | `app/main.py` × 1                     |

Domain-specific code (prompt, pre_router, sql_builder, formatter, graph
nodes) remains **completely isolated** inside each agent's sub-package.
