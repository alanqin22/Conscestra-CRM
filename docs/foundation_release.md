# Conscestra CRM — Foundation Release

Release-readiness assessment for **C1–C5**, prepared 2026-07-27.

**Verdict: GO.** Both blockers are now closed in code, and the configuration
that remains is enumerated in §5.3. No architectural defect was found.

**Fixed during release preparation**

| | |
|---|---|
| Calendar feed could start unsecured | `app/core/release_guard.py` — a deployed environment now **refuses to start** without `CALENDAR_FEED_TOKEN` |
| Unsafe auth posture was silent | The same guard reports it loudly at startup |
| `/home-index` disclosed business scale | Now carries the `_DATA` dependency |
| Free-tier training ack could travel | Blocks a deployed start |
| **The guard misreported that posture** (§2.8) | It re-derived the posture from the environment, so an application enforcing **no auth at all** passed its own check. Now reads the resolved posture |
| `.env` outranked the platform (§2.9) | `load_dotenv()` — a variable the operator set deliberately now wins |
| A doubly-expressed posture resolved silently | Startup names which setting won and which is inert |

740 tests passing, 1 environment-dependent skip.

---

## 1. What this release is

An AI-native CRM in which **agents operate through governed business objects
rather than around them**.

The distinction that makes it coherent: every other AI-CRM demonstration shows
an agent that can *converse* about the business. This one shows agents that
create durable records the business can be held to — and that a human can
audit afterwards.

```
   Customer ──▶ Portal / AI ──▶ ONE customer scope ──▶ CRM
                                                        │
   Agents ────▶ governed domain layer ──────────────────┤
                (state machine · owner validation ·      │
                 field history · one transaction)        │
                                                         ▼
                                              durable business records
```

Four claims, each backed by tests rather than assertion:

**CRM records have lifecycle integrity.** A case cannot skip or reverse its
lifecycle, an owner is a validated CRM identity or NULL, and every ownership
change is provable after the fact because the previous value was recorded
before it was overwritten.

**AI agents act through the same boundary as humans.** The cases agent has no
second write path; a button click and a model's decision converge on the same
`app/core/cases.py → _mutate()`. An LLM can choose *what* to do; it can never
choose *how* the write happens.

**Customers have secure self-service access.** There is exactly one customer
authorization implementation, shared by voice, chat and the portal.

**AI and UI share one authorization model.** The customer assistant calls the
same functions the pages call — literally the same function objects — so it
cannot see more than the page.

---

## 2. Security boundary review

### 2.1 No customer endpoint bypasses `customer_scope()` — VERIFIED

Every `/portal/*` data route depends on `customer_context`, which opens the one
scope and clears it in a `finally`. Proven over real HTTP, not by calling the
functions directly:

| Check | Test |
|---|---|
| No credential → 401, no data in the body | `test_portal_boundary::test_01` |
| Invalid token → 401 | `test_02` |
| Expired session loses scope on the very next request | `test_10`, `test_11` |
| Scope cleared after every request | `test_31` |
| Two customers interleaved keep their own data | `test_33` |

### 2.2 No portal endpoint reaches unrestricted CRM data — VERIFIED

Every portal query anchors on `%(account_id)s`, filled **from the verified
scope, never from the request**. A caller-supplied `account_id` is overwritten
(`test_11`). A foreign order or case id in the URL returns 404 (`test_20`,
`test_21`). A bookmarked URL is not a credential (`test_22`).

The transaction is opened `readonly=True`, so PostgreSQL itself refuses a write
(`test_30`), and while a customer scope is set `execute_sp` refuses **all**
stored-procedure access, fail-closed (`test_20` in `test_portal.py`).

### 2.3 No AI customer intent has a separate data path — VERIFIED

```python
portal._INTENTS["orders"][1] is portal.portal_orders   # the same object
```

`test_portal_ui::test_31` asserts identity, `test_33` asserts
`ai["data"] == page` for every intent, and `test_32` fails if `portal_ask` ever
issues a query of its own. An unrecognised question does not fall back to a
broad search.

### 2.4 No stored-procedure path bypasses the governed layers — VERIFIED

`sp_cases()` — a 14-mode legacy procedure that writes `status` and `owner_id`
with no history awareness — is refused for **every** caller including admin and
the system context (`test_case_second_boundary`).

**Limitation CLOSED, 2026-08-28.** This previously read: *"the database half of
that defence is inert … the application guard is currently the only effective
control."* That is no longer true, and not because the ACL changed —
`sql/drop_sp_cases.sql` removed the object. There is no privilege left to
argue about.

The measurement that prompted it is worth recording: of the fourteen modes,
**five still executed**, and `assign` and `close` were verified writing
`cases.owner_id` and `cases.status` in a rolled-back transaction. "Legacy"
had been doing a lot of work in that sentence.

The application guard stays, and `test_20` now fails on *any*
`sp\_case%` procedure rather than the one known name.

### 2.5 ✅ FIXED — unauthenticated calendar feed

```
GET /calendar/activities.ics    →  200 OK, 903 KB, 2,000 events, no credential
```

Contents include **account names** ("Northwind Analytics Inc."), **commercial
margins** ("Margin is excellent at 26.33%"), **invoice numbers** (INV-000028)
and 2,000 email addresses.

The code is correct. `integrations.py` implements the secret-URL pattern —
calendar clients cannot send auth headers, so a token in the URL is the right
design — but the control only engages when the env var is set:

```python
required = (os.getenv("CALENDAR_FEED_TOKEN", "") or "").strip()
if required and token != required:
    raise HTTPException(403, "invalid or missing calendar token")
```

`CALENDAR_FEED_TOKEN` is **unset**, so the feed follows the documented "demo
public-read posture". Acceptable for a local demo; not acceptable for a public
release.

**Fixed.** `app/core/release_guard.py` runs before anything binds or serves and
**refuses to start a deployed environment** without the token:

```
UnsafeConfiguration: Refusing to start a deployed environment with an unsafe
configuration — calendar_feed: CALENDAR_FEED_TOKEN is not set…
```

A warning would have produced the same outcome as no control, since that is
exactly how this shipped unsecured. An operator who genuinely wants a public
feed sets `CALENDAR_FEED_PUBLIC=1` — an escape hatch that turns an accident
into a decision, and keeps a real deployment from being locked out.

Local development is untouched: the guard only fires when the environment looks
deployed (`APP_ENV`/`ENVIRONMENT`, Railway's own markers, or a non-local
`APP_URL`).

Verified: `403` without the token, `403` with a wrong one, `200` with the right
one, and a refusal leaks no event data (`test_release_guard::test_20`, `test_21`).

### 2.6 ✅ FIXED — unauthenticated dashboard aggregates

```
GET /home-index  →  200 OK, no credential
                    active_pipeline 106 · open_leads 97 · pending_orders 67
                    unread_alerts 371
```

Counts and totals only — no customer names, no individual records. It disclosed
business *scale* to anyone who could reach the URL.

**Fixed.** `home_router` now carries `dependencies=_DATA`, the same gate as every
other CRM read. Effective when the posture is `locked`; under `public-read` it
remains readable by design, which is the posture decision in §5.3.

### 2.7 ⚠️ MUST CONFIRM — security posture on Railway

Use the single switch, not the individual flags:

```
API_SECURITY_MODE=locked        every data call requires a login
API_SECURITY_MODE=public-read   anyone reads; writes need an Admin/writer login
```

`API_SECURITY_MODE` is resolved **before** the legacy `API_AUTH_ENABLED` /
`API_PUBLIC_READ` pair and cannot be half-set, which is why it is the reliable
control (`test_62`).

Setting **both** is the trap: the mode wins and the legacy flag does nothing.
That used to happen silently. Startup now names it —

```
[security] API_SECURITY_MODE='open' is in force; API_AUTH_ENABLED is being
IGNORED. Remove the legacy flag(s), or clear API_SECURITY_MODE to use them.
```

— and the release guard downgrades even a *correct* posture from `ok` to
`advisory` while a conflict stands, so the report never reads as agreement with
a flag that is doing nothing (`test_36`, `test_37`).

### 2.8 FIXED — the release guard reported the wrong posture

Found while preparing this release, in the guard written for it. `_check_api_auth`
re-derived the posture from `os.environ` instead of reading what the application
had actually resolved, and was wrong in **both** directions:

| Environment | Application | Old guard said |
|---|---|---|
| `API_SECURITY_MODE=locked` | locked | "not enforcing" — **false alarm** |
| `API_SECURITY_MODE=open` + `API_AUTH_ENABLED=1` | **open** | "CLEAN" — **false all-clear** |

The second is why this mattered: an application enforcing **no authentication at
all** passed its own security check. A guard that reports the wrong posture is
worse than no guard, because it manufactures confidence.

**Fixed.** The check reads `auth_dep.SECURITY_POSTURE` — the value the running
process is actually using. `test_35` asserts structurally (via AST, not text)
that it never reads the environment again; `test_33` and `test_34` are the two
directions above. All three were mutation-tested against the old implementation
and fail on it.

### 2.9 FIXED — `.env` overrode the platform

`app/core/config.py` called `load_dotenv(override=True)`, so **a `.env` file beat
real environment variables** — the security posture, every secret, the database
DSN. That was safe only for as long as `.env` stayed untracked; committed or
baked into an image, it would have made Railway's dashboard settings stop taking
effect, silently.

**Fixed:** `load_dotenv()` — the standard semantic, where a variable the operator
set deliberately outranks a file. `.env` remains the development default. Verified
no local key was shadowed by this change (0 of 111 collide with the real
environment), and `test_63` now asserts *both* halves: the call has no `override`,
and `.env` stays untracked.

This also removes a local surprise: exporting `API_SECURITY_MODE` in a shell now
works, where before `.env` silently won.

---

## 3. Demo readiness

Three roles, one continuous story. Every step below runs against the current
build.

### 3.1 Customer

1. Open `customer-portal.html`, sign in.
2. **Overview** — account name, outstanding balance, open case count.
3. **Orders** → click one → line items and total.
4. **Invoices** → outstanding balance highlighted.
5. **Support** → a case → its public updates only.
6. **Ask** — type *"which invoices are overdue?"* The assistant answers and
   navigates to the same page, captioned *"Answered from /portal/invoices — the
   same data as the page."*

**The point to make:** the assistant and the page are two doors to one
authorized dataset, not two datasets.

### 3.2 Business user

1. A customer asks for a person on chat or the phone line → an **escalation**
   is created with an owner and an SLA clock.
2. `agent-console.html` — the rep takes over. The AI stands down. The
   obligation is **assigned, not discharged** — because no case records the
   work yet.
3. Click **"this produced durable work"** → a **case** is created, and *now*
   the escalation resolves.
4. `case-mgmt.html` — work the case: assign an owner, move it through the
   lifecycle, add a comment.
5. **Field history** — status and owner changes with their previous values.
6. **Routing policy** tab — the rule that matched, and if it names a tier
   nobody is granted in, it says so instead of guessing.

### 3.3 Executive

1. `GET /cases/analytics` — obligation / acceptance / work record / completion
   as four distinct moments.
2. `GET /cases/knowledge-signals` — repeated subjects; evidence, not
   conclusions.
3. The daily briefing — anomalies plus **discount pressure**: *"N quotes went
   out with the discount cut by policy (largest ask 45% vs 15% cap)."*

### 3.4 Demo caveats — state these, don't hide them

- The seed data is **synthetic**: a consumer-goods distributor.
- **Four people are assignable** (the executives). The 21 `employees` rows are
  demo seed data and are deliberately *not* granted work eligibility — the UI
  says so in a banner. A routing rule targeting the staff tier will correctly
  find nobody.
- `CASES_AUTO_OPEN=0`, so escalations do not create cases automatically. Press
  the button in the demo.
- Only one credential is linked to a customer account, so the portal shows the
  **not-linked onboarding state** until you link one.

---

## 4. Known limitations — roadmap, not defects

| Deferred | Why | Where it sits |
|---|---|---|
| **SaaS tenancy model** | Single-organization product by decision. The routing seam exists (`tenancy.py`, one `get_connection` chokepoint) so it stays possible | Strategic |
| **Subscription revenue model (C4)** | Requires a subscription business model. `quote_lines` is one-time-sale shaped, so recurring commitments have no home yet | Strategic |
| **Entitlement-driven SLA (C6)** | SLA derives from reason + priority. Unblocked — it can key on account significance without contracts | Implementation, next |
| **Advanced routing** | Deterministic ordered rules with language/skill/capacity. No scoring model, deliberately | Strategic |
| **Employee skill/capacity model** | Attributes exist on the assignable directory and are curated, never inferred | Strategic |
| **Closed-case reopening** | Needs a durable parent relationship; `reopen()` refuses and names the gap | Strategic |
| **Non-superuser database role** | The only structural security weakness found | Platform security |

---

## 5. Production readiness

### 5.1 Database migrations — MANUAL, in this order

`sql/` is gitignored, so these do **not** travel with a git deploy:

```
1. sql/escalations.sql              U1  obligations
2. sql/custom_agent_versions.sql    U2
3. sql/llm_usage_failover.sql       U5
4. sql/agent_capabilities.sql       U4
5. sql/mcp_servers.sql              U6
6. sql/case_lifecycle.sql           C1  cases + record_field_history
7. sql/case_escalation_bridge.sql   C1  source_assignee + unique index
8. sql/assignable_identity.sql      C2
9. sql/routing_rules.sql            C2
10. sql/routing_signals.sql         C2  languages/skills
11. sql/quotes.sql                  C3
```

All are idempotent and additive; each was applied twice locally to prove it.
**Order matters** — 7 depends on 6, 10 depends on 8 and 9.

### 5.2 HTML deployment — MANUAL

`*.html` is globally gitignored (**zero** tracked). All pages reach production
via `deploy_html.ps1` → agentorc.ca. New this release:
**`case-mgmt.html`**, **`customer-portal.html`**.

The `_CHAT_PAGES` routes in `app/main.py` serve files from the backend's working
directory — a local-development convenience so the Azure Speech SDK works over
`http://`. They 404 on Railway for every page, by design.

### 5.3 Environment configuration

**Must set before launch**

```
CALENDAR_FEED_TOKEN=<long random>     # §2.5 — release blocker
API_SECURITY_MODE=locked               # §2.7 — or public-read, deliberately
ADMIN_API_TOKEN=<rotate per env>       # never reuse the local value
```

Set `API_SECURITY_MODE` and **not** `API_AUTH_ENABLED` / `API_PUBLIC_READ`. The
mode wins over both; setting them together does nothing except log a conflict
(§2.7). If a legacy flag is already on Railway, remove it.

**Confirm intentional**

```
CASES_ENABLED=1        (code default)   case layer on
CASES_AUTO_OPEN=0                       escalations do not auto-create cases
CASES_KB_FEEDBACK=0                     case→knowledge mining off
ASSIGNABLE_STRICT=0                     only 4 assignable identities today
QUOTES_RECORD=1        (code default)   quotes persisted
LLM_FAILOVER_ENABLED=1                  Gemini standby, free tier
```

**Do not copy to production**

```
LLM_ALT_TIER_TRAINING_ACK=1   # accepts that a FREE-tier provider may train on
                              # content. Justified for synthetic local data; on
                              # Railway the CONVERSATIONS are real people even
                              # though the records are synthetic.
```

### 5.4 Authentication setup

Two credentials exist: one admin (`admin@conscestra.local`), one viewer. Before
a customer demo, link a credential to a real `account_id` or the portal shows
the not-linked state. Sessions last 8 hours; an expired one now returns **401
"sign in again"** rather than a misleading 403 about roles.

### 5.5 Seed / demo data

`POST /demo/seed` builds a sample business and returns an intelligence
headline; `/demo/clear` removes it. `GET /setup/readiness` gives a prioritized
next-step checklist for an empty org.

### 5.6 Test suite

**740 passing, 1 skipped.** The skip is environment-dependent (needs two
accounts with orders). Suites added this release: cases (9 steps + e2e),
routing, assignable identity, quotes, portal boundary, portal UI/AI, and the
release-guard posture regressions (§2.8).

---

## 6. Recommendation

**Ship C1–C5 as the Foundation Release**, after:

1. Setting `CALENDAR_FEED_TOKEN` — the one true blocker.
2. Setting `API_SECURITY_MODE` on Railway, and removing any legacy
   `API_AUTH_ENABLED` / `API_PUBLIC_READ` beside it.
3. Applying the 11 migrations in order.
4. Deploying both new HTML pages.

Then gate `/home-index` (§2.6) in the first patch.

The architecture is coherent and the boundaries are tested rather than
asserted. What makes it demonstrable is not the feature count — it is that
every claim in §1 can be shown failing correctly when probed: an illegal
transition refused with a business reason, a foreign case id returning 404, a
routing rule finding nobody and saying why, a clamped discount surfacing in the
briefing.

---

## 7. Release demonstration script

The demo proves **governance**, not features. Each step is chosen because the
system can be shown *refusing correctly* — which is harder to fake than a happy
path, and is the actual product claim.

### Demo 1 — an illegal case transition is refused with a business reason

```
POST /case-chat  {"chatInput":{"mode":"transition","caseId":"…","toStatus":"closed"}}
→ "That isn't permitted: new -> closed is not a permitted transition
   (from 'new' only: in_progress)"
```

The case does not move, and **no history row is written** — a refusal leaves
nothing behind. Say out loud: the rule lives in one place, and the UI renders
only the transitions the server offered. There is no second copy in JavaScript
that could drift.

### Demo 2 — customer isolation

Sign in to `customer-portal.html` as customer A. Copy an order URL. Then:

```
same URL, no credential      → 401
same URL, customer B's token → 404
```

The account id comes from the session, never the URL — a caller-supplied one is
overwritten before the query runs. A bookmarked URL is not a credential.

### Demo 3 — routing that refuses to guess

`case-mgmt.html` → **Routing policy** → Preview.

```
Rule 'Everything else → staff' matched, but it names the employee tier,
and nobody there is currently assignable.
```

Then add a French-language requirement:

```
candidates: []
excluded:   Alan Qin — does not work in fr (has en)
            Daping Qin — does not work in fr (has en) …
```

The point: unknown is not "everything". The system routes nowhere loudly rather
than to someone who cannot help the customer, and it names who was considered.

### Demo 4 — the discount guardrail is visible and explainable

Generate a quote asking for 40% against a 15% cap:

```
requested 40.0 · granted 15.0 · cap 15.0 · clamped true · brand.max_discount_pct
```

The offer still goes out — a guardrail constrains, a gate blocks — and the fact
appears in the morning briefing:

> 💸 N quote(s) worth $X went out with the discount cut by policy (largest ask
> 45% vs 15% cap) — review whether any deserved an exception.

Before this release that fact existed only in an application log that rotates.

### Demo 5 — the assistant and the page are one dataset

In the portal, ask *"which invoices are overdue?"* The answer appears, the page
navigates, and the caption reads:

> Answered from /portal/invoices — the same data as the page.

`portal._INTENTS["invoices"][1] is portal.portal_invoices` — the same function
object. The assistant cannot see more than the page because it is not asking a
different question of the database; it is calling the page's own read.

### Demo 6 (optional) — the deployment refuses to start unsafely

```
APP_ENV=production python main.py
→ UnsafeConfiguration: Refusing to start a deployed environment …
  CALENDAR_FEED_TOKEN is not set …
```

The strongest single statement about the product's posture: a control that
depends on someone remembering is not a control.

---

## 8. Final deployment checklist

### 8.1 Environment — Railway

```bash
CALENDAR_FEED_TOKEN=<48+ random chars>     # REQUIRED — start fails without it
API_SECURITY_MODE=locked                   # or public-read, deliberately
ADMIN_API_TOKEN=<rotate — never the local value>
APP_ENV=production                         # makes the guard authoritative
DATABASE_URL=<Railway Postgres>
OPENAI_API_KEY / GOOGLE_API_KEY
```

**Remove `API_AUTH_ENABLED` and `API_PUBLIC_READ` if either is set.**
`API_SECURITY_MODE` overrides them; leaving one behind reads like a second
control that is in fact inert (§2.7).

**Must NOT be set in production**

```
LLM_ALT_TIER_TRAINING_ACK=1     # blocks a deployed start, by design
```

**Confirm intentional**

```
CASES_ENABLED=1        (default)   CASES_AUTO_OPEN=0     CASES_KB_FEEDBACK=0
QUOTES_RECORD=1        (default)   ASSIGNABLE_STRICT=0   VOICE_STREAM_ENABLED=0
```

**Never deploy a `.env` file.** It is gitignored and untracked. Since §2.9 it no
longer outranks a real environment variable, so a stray one is no longer
catastrophic — but it still supplies anything Railway leaves unset, which is its
own quiet way to be wrong.

### 8.2 Database — apply in this order

```
 1  sql/escalations.sql               6  sql/case_lifecycle.sql
 2  sql/custom_agent_versions.sql     7  sql/case_escalation_bridge.sql
 3  sql/llm_usage_failover.sql        8  sql/assignable_identity.sql
 4  sql/agent_capabilities.sql        9  sql/routing_rules.sql
 5  sql/mcp_servers.sql              10  sql/routing_signals.sql
                                     11  sql/quotes.sql
```

Order matters: 7 needs 6; 10 needs 8 and 9. Every file is idempotent and
additive — each was applied twice locally to prove re-running is safe.

**Rollback:** each migration is additive, so rollback is dropping what it added
(`DROP TABLE quotes, quote_lines` etc.). No migration rewrites or deletes an
existing row, so a failed deploy loses no data. The one irreversible-by-design
element is `record_field_history`, which is append-only on purpose.

### 8.3 Frontend

```powershell
powershell -ExecutionPolicy Bypass -File deploy_html.ps1
```

New this release: **`customer-portal.html`**, **`case-mgmt.html`**. `*.html` is
globally gitignored, so a git deploy never carries them.

### 8.4 Post-deploy verification

```bash
curl -s -o /dev/null -w "%{http_code}\n" $APP/calendar/activities.ics        # 403
curl -s -o /dev/null -w "%{http_code}\n" $APP/portal/orders                  # 401
curl -s -o /dev/null -w "%{http_code}\n" $APP/home-index                     # 401 if locked
curl -s $APP/portal/health                                                   # ok, no data
```

---

## 9. GO / NO-GO

**GO.**

| | |
|---|---|
| Blocking defects | **0** — both closed in code |
| Tests | 733 passing, 1 environment-dependent skip |
| Architecture changes needed | none |
| Accepted limitations | superuser database role; documented, not hidden |

The release is not "a feature-complete CRM". It is a **Foundation Release**:
governed AI agents, trusted business records, secure customer intelligence and
explainable automation — each demonstrable by showing the system refuse
correctly, which is the part that cannot be faked.
