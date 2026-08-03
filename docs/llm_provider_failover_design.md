# U5 — LLM Provider Failover: Design v3 (pre-implementation)

> Status: **DESIGN ONLY — awaiting review.** No code changed.
> v1: call-path inventory + taxonomy. v2: Ollama demoted to local-only.
> **v3 (2026-07-25): provider DECIDED — Google Gemini.** The user provisioned
> `GOOGLE_API_KEY`, selecting Gemini over the v2 Anthropic recommendation.
> Live validation against that key produced three corrections, two of which
> invalidate parts of v2. Supersedes v2 §1, §5, §8, §9, §10.

---

## v3 — DECISION + live validation against the provisioned key

**Remote alternate: Google Gemini — `gemini-3.5-flash-lite` for BOTH tiers.**

The key is live (50 models visible, 41 chat-capable). Validated against the real
workload shape rather than assumed:

| Check | Result |
|---|---|
| `parse_ai_json` compatibility (real agent prompt: JSON-in-prose, 3 routing cases) | **3/3 parsed, all modes correct** on `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` |
| Conversational CRM reply quality | Appropriate, concise, correctly declined to invent a return window |
| Latency, interactive path (n=3) | **`gemini-3.5-flash-lite` p50 = 784 ms** (min 763 / max 826) |

### 🔴 Correction 1 — the v2 health check was WRONG (found by testing)

v2 §8 specified: *"`GET /v1/models` → model id present."* **That check is
insufficient.** Both `gemini-2.5-flash-lite` and `gemini-2.5-flash` are **listed
by the models endpoint and return HTTP 404 on `generateContent`** with this key.

A list-membership check would have passed them and still failed at runtime —
reproducing the exact `gpt-oss:20b` failure mode U-2 exists to prevent, one level
deeper. **The health check must issue a minimal real generation** (`maxOutputTokens: 8`,
"reply OK"), not a catalogue lookup. Listing a model is not proof it is usable.

### 🔴 Correction 2 — the v2 deadline arithmetic could not fail over

v2 §5 set `interactive` deadline = 30 s **and** the OpenAI attempt cap = 30 s. A
full primary timeout therefore consumes the entire deadline and **the alternate
never runs** — the failover path would be unreachable in exactly the scenario it
exists for. Corrected in §5: **the primary attempt cap must be ≤ 40 % of the
logical deadline** so an alternate always has room.

### 🟡 Correction 3 — `gemini-3.5-flash` is the wrong model for interactive

p50 **9,148 ms** vs flash-lite's 784 ms (**11.7×**), and it truncated mid-sentence
at 400 output tokens. Use `flash-lite` for both tiers; `gemini-3.5-flash` is a
`background`-class option only.

### Why this doesn't contradict v2's reasoning

v2 ranked Gemini a **strong runner-up**, second only on two counts — the
free-tier training footgun and lineup churn. Both are addressable rather than
disqualifying, and the measured JSON-parse result (3/3) confirms the criterion
v2 called decisive is satisfied. Gemini also brings the one thing Anthropic
could not: **Vertex `northamerica-northeast1` (Montreal)** if Canadian residency
ever becomes contractual (U-9) — the same vendor, a config change, not a
re-selection.

⚠️ **Open governance item (was U-9's footgun, now LIVE — see U-11):** free-tier
Gemini content may be used to improve Google's products; paid-tier is not. This
key's tier must be confirmed before any customer data flows through it.

### Model choice — pin GA, never `-latest`

`gemini-flash-latest` / `gemini-flash-lite-latest` are floating aliases. A
failover path exercised twice a year must not silently change model between
outages. **Pin `gemini-3.5-flash-lite`** (GA, 1M input / 65K output).
Fallback-of-the-fallback if it is ever retired: `gemini-3.1-flash-lite`
(also GA, also 3/3 on the parse test).

---

## 0. What v1 established (unchanged, carried forward)

- **One chat chokepoint**: 39 call sites → `_get_llm(tier, caller)` → `ChatOpenAI`
  | `ChatOllama` → `MeteredLLM.invoke/ainvoke`. Provider selection is static.
- **The interface actually in use is `messages → text`.** Zero uses of
  `with_structured_output`, `bind_tools`, `.stream()`, or JSON mode. Every
  JSON-producing agent prompts for JSON in prose and parses it with
  `graph_utils.parse_ai_json` (5 fallback strategies).
- **No provider failure escapes.** All 39 sites catch every exception and drop
  to a deterministic fallback, so an outage causes *silent fleet-wide quality
  collapse*, not errors. **U5's goal is continuity of intelligence.**
- **Hidden retries**: the OpenAI SDK already retries twice (`max_retries=2`).
  No timeouts are configured anywhere; `ChatOllama` can hang indefinitely.
- **Out of scope**: `_call_ollama_direct` (Ollama-specific by design) and
  `semantic._embed` (embeddings — see U5b, §5).

---

## 1. Recommended remote failover provider

**Anthropic Claude API**, tiered to mirror our own tiers:

| Our tier | Primary (unchanged) | Remote alternate |
|---|---|---|
| `lite` | `gpt-4o-mini` | **`claude-haiku-4-5`** — $1 / $5 per MTok, 200K context |
| `standard` | `gpt-4o-mini` (currently same) | **`claude-sonnet-5`** — $3 / $15 per MTok ($2 / $10 introductory through 2026-08-31), 1M context |

**Best low-latency emergency provider: Groq** — but **deferred, not adopted**
(§2.4). It is worth exactly one thing to us: sub-100 ms time-to-first-token on
the real-time voice path. Voice is off by default (`VOICE_STREAM_ENABLED=0`), so
adopting a third provider today buys nothing and adds a key, a dependency and a
policy row. Revisit **with** voice.

**Best local optional provider: Ollama** — retained as an *opt-in third hop*,
gated on the runtime actually being reachable **and** the configured model
actually being installed (§8). It is the **only** permitted provider for
local-only content (§3), which is the role it is genuinely good at.

### Why Claude over the alternatives

**It wins on the criterion that actually governs this workload: instruction
adherence on prose-embedded JSON.** Our entire structured path is "ask for JSON
in the prompt, parse it out of free text." A provider that wraps JSON in
commentary, or drifts from a requested shape, degrades `parse_ai_json` into its
regex fallbacks. Claude's instruction-following is its strongest documented
characteristic, and it does not require us to adopt a structured-output API we
don't use.

**Independent infrastructure.** Anthropic is a distinct vendor on distinct
infrastructure from OpenAI — the requirement that disqualifies "OpenAI with a
different model."

**Cleanest fit behind the existing interface.** `langchain-anthropic`'s
`ChatAnthropic` exposes the same `.invoke(messages)` / `.ainvoke(messages)`
surface, so `MeteredLLM.__getattr__` passthrough and all 39 call sites are
untouched. One new dependency, one construction branch in `_get_llm`.

**Data governance is contractible.** Zero-data-retention is available on the
models we would use. ⚠️ Note for the future: **Claude Fable 5 requires 30-day
retention and is *not* available under ZDR** — if `LLM_REQUIRE_ZERO_RETENTION=1`
we must not select it. Haiku 4.5 and Sonnet 5 carry no such restriction.

**Best forward path for the roadmap.** U4 (governed agent actions) and U6 (MCP
client) are already ranked. Anthropic's tool-use and MCP surface is the most
mature of the candidates, so the failover provider and the future capability
provider are the same vendor — one contract, one policy row, one key.

### The honest costs

- **~6–8× the token price of `gpt-4o-mini`** on the lite tier. Acceptable
  because failover is rare and bounded (§6), but it means "cheap failover" is
  not a selling point — continuity is.
- **Replies will read differently.** Different house style, different length
  calibration, different latency. This is recorded explicitly in the capability
  profile (§9) rather than pretended away.

---

## 2. Provider comparison

Scored **for this workload as a failover target**, not as a primary.

| Criterion | **Anthropic Claude** | Google Gemini | Mistral | Groq |
|---|---|---|---|---|
| 1. Independent of OpenAI | ✅ own infra | ✅ Google Cloud | ✅ EU infra | ✅ own LPU hardware |
| 2. CRM conversational quality | **Excellent** | Excellent | Good | Adequate (Llama-class) |
| 3. JSON-in-prose for `parse_ai_json` | **Strongest** — instruction adherence is its defining trait | Strong | Good; `json_object` enforces shape, not schema | Weakest of the four |
| 4. Latency | Good (Haiku is fast) | Good (Flash) | Occasional EU-hours spikes | **Best** — sub-100 ms TTFT, 500+ tok/s |
| 5. Model stability | Versioned IDs, published deprecations | Fast-moving lineup | Stable | Tracks upstream OSS releases |
| 6. Rate limits | Per-org pools, separate per model tier | Tiered; free tier heavily capped | Plan-based | Enterprise tier has a 99.9% SLA |
| 7. Cost (lite class) | $1 / $5 | **$0.25–$0.50 / $1.50–$3** | **$0.10 / $0.30** | ~$0.59 / $0.79 |
| 8. Privacy / data processing | ZDR available on our models | ⚠️ **free tier may train on prompts — paid tier only** | ZDR on Scale plan | Least mature |
| 9. CA / enterprise governance | Enterprise DPA; US processor | ⚠️/✅ **Vertex `northamerica-northeast1` (Montreal) is the only true Canadian-region option** | EU residency by default — *wrong region for a Canadian claim* | Thin |
| 10. Future agentic fit (U4/U6) | **Best** — mature tool use + MCP | Good | Moderate | Inference only |
| 11. LangChain integration | `langchain-anthropic`, same interface | `langchain-google-genai` | `langchain-mistralai` | `langchain-groq` |
| 12. Emergency-failover suitability | **Best overall** | Strong runner-up | Regionally mismatched | Narrow (latency only) |

### 2.1 Why not Gemini (the closest call)

Materially cheaper and genuinely strong. Two things put it second:

1. **The free-tier training footgun.** Free-tier content may be used to improve
   Google's products; paid-tier is not. A failover key provisioned casually —
   exactly how emergency keys get provisioned — could route CRM data into a
   training-eligible tier. That risk is structural, not hypothetical.
2. **Lineup churn.** A failover path exercised twice a year must not need model
   maintenance between outages.

**But it wins one decisive scenario**, and this is a recommendation, not a
dismissal: **if Canadian data residency becomes a contractual requirement,
Gemini on Vertex AI pinned to `northamerica-northeast1` (Montreal) is the only
candidate that keeps failover traffic inside Canada.** Neither Anthropic's
first-party API nor Mistral can offer that. This is recorded as the documented
alternate in §3, selectable by configuration without a code change.

### 2.2 Why not Mistral

Cheapest and technically capable, but **EU-residency-by-default is the wrong
region for us.** We are a Toronto-HQ company whose compliance story (#8,
`DATA_REGION`) is built on Canadian residency. Failing over into the EU replaces
one residency question with a different one. Mistral is the right answer for an
EU-regulated deployment; that isn't this one.

### 2.3 Why not Groq (as the default)

Groq is an *inference* provider for open-weight models, not an independent
frontier-model vendor. Llama-class quality is adequate for lite-tier wording and
weakest of the four on prose-embedded JSON — the one thing we most depend on.

### 2.4 Groq's actual role — deferred

Its speed advantage is real and matters for exactly one surface: **real-time
voice**, where TTFT dominates perceived quality. The router's provider graph is
designed so adding it later is a config row plus one construction branch. **Do
not adopt it until voice ships.**

---

## 3. Provider-policy matrix

The router answers **"may this specific request go to this specific
provider?"** before *every* attempt, including the first. Policy is evaluated on
the **data class** of the request, derived from the caller — never on the
failure.

| Data class | Callers | OpenAI (primary) | Claude (remote alt) | Ollama (local) | Deterministic fallback |
|---|---|---|---|---|---|
| **INTERNAL_SENSITIVE** | internal custom agents (`it-service`, `people-hr`, any `scope='internal'`), employee Slack/Teams orchestrator | ❌ **never** | ❌ **never** | ✅ **only permitted provider** | ✅ |
| **CUSTOMER_EXTERNAL** | sdr, email auto_reply, store/catalog, agent_console suggest, external custom agents, embed widget | ✅ | ✅ | ✅ (opt-in, 3rd hop) | ✅ |
| **BUSINESS_INTERNAL** | planner, supervisor, ceo_briefing, analytics, kb, evals, simulator | ✅ | ✅ | ✅ (opt-in, 3rd hop) | ✅ |

**The provider graph is per data class — not one linear chain:**

```
CUSTOMER_EXTERNAL / BUSINESS_INTERNAL
    OpenAI  →  Claude  →  [Ollama if available & enabled]  →  deterministic fallback

INTERNAL_SENSITIVE
    Ollama (if available)  →  deterministic fallback
        └─ NO external hop, ever. Not on outage, not on force, not on empty config.
```

**This is the U2 `reach_invariant` at the LLM layer, and it is deliberate.** U2
closed a hole where an anonymously-reachable agent could read the internal KB. A
generic failover chain would reopen the same class of leak from the other
direction — internal HR/IT content egressing to a third party because OpenAI had
an outage. The policy gate is *unforceable*, exactly like `reach_invariant`.

**Note the consequence, stated plainly:** with `LLM_PROVIDER=openai` today, the
internal agents' *primary* is already OpenAI. Classifying them
INTERNAL_SENSITIVE would change current behaviour, not just failover behaviour.
**Decision required — see U-7 in §11.**

### Policy gate inputs

`may_send(data_class, provider) → (allow, reason)`:

1. **Class rule** — the matrix above. INTERNAL_SENSITIVE + external ⇒ deny.
2. **Residency** — `DATA_REGION` set to a Canada-only value + provider not
   attested (`LLM_EXTERNAL_ALLOWED=0`) ⇒ deny external.
3. **Retention** — `LLM_REQUIRE_ZERO_RETENTION=1` + provider/model not attested
   ⇒ deny. (Blocks Claude Fable 5 specifically; Haiku 4.5 / Sonnet 5 pass.)
4. **Egress opt-in (U-1, confirmed)** — a **local→external** hop requires
   `LLM_ALLOW_EGRESS_FAILOVER=1`, **default 0**. Moot while OpenAI is primary;
   preserved for any future Ollama-primary deployment, exactly as instructed.
5. **Tenant override** (forward-compatible with P4) — a tenant row may pin
   `allowed_providers`; unknown ⇒ deny external.

Deny is **not** an error. It yields `POLICY_FORBIDDEN`, the router moves to the
next *allowed* provider, and if none remain the caller's existing deterministic
fallback runs — today's exact behaviour.

---

## 4. Failover state machine

```
                    ┌─────────────────────────┐
   logical request  │ 1. BUDGET DECISION      │  check_budget(caller) — ONCE
   (new logical_id) │    LLMBudgetExceeded ───┼──▶ raise (class C) · 0 attempts
                    └───────────┬─────────────┘
                                ▼
                    ┌─────────────────────────┐
                    │ 2. CLASSIFY DATA         │  caller → data_class
                    └───────────┬─────────────┘
                                ▼
                    ┌─────────────────────────┐
                    │ 3. ALLOWED PROVIDER SET  │  graph for this class,
                    │    ∩ healthy (§8)        │  minus unhealthy targets
                    └───────────┬─────────────┘
                                ▼
             ┌──────────────────────────────────────────┐
             │ FOR each provider in the ordered set:     │
             │   a. may_send()?  ── no ─▶ record         │
             │        POLICY_FORBIDDEN, next provider    │
             │   b. capability gate ── mismatch ─▶ stop  │
             │   c. deadline remaining? ── no ─▶ break   │
             │   d. ATTEMPT (provider-specific timeouts) │
             │        ├─ success ─▶ record is_final ─▶ RETURN
             │        └─ exception ─▶ classify()         │
             │             ├─ class B (our fault) ─▶ RAISE immediately
             │             ├─ class C ─────────────▶ RAISE immediately
             │             └─ class A (transient) ─▶ record, next provider
             └──────────────────────┬───────────────────┘
                                    ▼
                    ┌─────────────────────────┐
                    │ EXHAUSTED                │  re-raise the LAST provider
                    │  → caller's try/except   │  exception → deterministic
                    │  → deterministic fallback│  fallback (unchanged)
                    └─────────────────────────┘
```

**Failover NEVER occurs for** (all raise immediately, no second provider):
`AUTH` · `BAD_REQUEST` · `NOT_FOUND` · `CONTENT_POLICY` · `CONTEXT_LENGTH` ·
`APP_ERROR` · `BUDGET_EXHAUSTED` · `POLICY_FORBIDDEN` · `CAPABILITY_MISMATCH`.

**Failover occurs ONLY for** classified transient provider failures:
`NETWORK` · `TIMEOUT` · `PROVIDER_5XX` · `OVERLOADED` · `RATE_LIMIT`.

**A content-policy refusal is never re-sent to another provider.** Re-routing a
refused prompt to obtain a different answer is policy laundering, and it is
forbidden regardless of configuration.

**Attempts per provider: exactly 1 router attempt each.** The OpenAI SDK's
internal `max_retries=2` already covers same-provider retry; a router-level
retry on top would multiply latency, not add to it. No backoff between
providers — a different provider is not a busy provider.

---

## 5. Timeout model

Timeouts are an **availability policy**, not an exception path. Three distinct
timers per attempt, with **per-provider profiles**, bounded by a **per-caller
logical deadline**.

### 5.1 Per-provider profiles — **v3 CORRECTED**

**The governing rule (v3): the primary attempt cap must be ≤ 40 % of the logical
deadline.** v2 set both to 30 s for `interactive`, so a full primary timeout ate
the whole budget and the alternate could never run — the failover path was
unreachable in the one scenario it exists for.

| Provider | Connect | First response | Attempt cap by latency class | Rationale |
|---|---|---|---|---|
| OpenAI (primary) | 5 s | — | **12 s** interactive · 30 s async · 60 s background | Already SDK-retried twice inside the window; capped so an alternate always has room |
| **Gemini** (alternate) | 5 s | — | **15 s** interactive · 25 s async · 45 s background | Measured p50 **784 ms** on `flash-lite`; 15 s is ~19× headroom |
| Ollama (local, opt-in) | **2 s** | **90 s** | **120 s** (background only) | Connect is instant or dead; a 31B model on a long KB prompt legitimately takes a minute+ — never on the interactive path |

**Worked example (`interactive`, 30 s):** OpenAI hangs → capped at 12 s → Gemini
attempted with 18 s remaining → answers in ~0.8 s → **total ≈ 13 s, well inside
the deadline.** Under v2's numbers this same case produced a deterministic
fallback and no failover at all.

The connect/response split is what makes a local model workable: a **2 s connect
timeout** detects "Ollama isn't running" almost immediately, while the **90 s
first-response timeout** doesn't punish a slow local generation. A single flat
30 s timeout would have been wrong in both directions.

### 5.2 Per-caller logical deadline (latency class)

A single global deadline is the wrong shape — an SDR chat reply and a nightly
briefing have different tolerances.

| Latency class | Callers | Logical deadline |
|---|---|---|
| `interactive` | sdr, store, agent_console.suggest, custom_agents, embed | **30 s** |
| `async_conversational` | email auto_reply, telephony SMS, transports | **75 s** |
| `background` | planner, supervisor, ceo_briefing, analytics, kb, evals, simulator | **240 s** |

The router takes `min(provider attempt cap, deadline remaining)`. If the
remainder is below `LLM_MIN_ATTEMPT_SECS` (default 8 s) it **skips** that
provider rather than starting an attempt it cannot finish.

⚠️ **This is a real behaviour change** (U-2 v1): calls that today hang and
eventually succeed will now fail over or fall back. `interactive` at 30 s is the
tightest and the most likely to bite a slow local model — which is precisely why
Ollama is the *last* hop and never the only one for customer traffic.

---

## 6. Budget-accounting model

**Ratified: one logical request = one budget decision.**

| Rule | Behaviour |
|---|---|
| Budget **decision** | `check_budget(caller)` runs **once**, before any provider attempt. Unchanged: raises `LLMBudgetExceeded` and never reaches a provider. |
| Budget **accounting** | **Every attempt is metered** — a failed attempt still spent tokens and still costs money. |
| Failover attempts | Do **not** re-check budget. A caller near its cap must not be stranded mid-failover with a half-answered customer. |
| Bounded overshoot | A logical request can exceed the daily cap by at most the remaining hops — **at most 2 extra attempts**. Bounded, recorded, and visible in telemetry. |
| **External alternate** | An external hop **does** re-run the **policy** check (§3) — the user's stated requirement. Policy ≠ budget: policy asks *may this data go there*, budget asks *can we afford it*. |
| Budget exhaustion is class C | Never a provider failure, never triggers failover, never counted in provider error rates. |

---

## 7. Telemetry model — logical request vs provider attempt

`llm_usage` gains these columns (all nullable, additive; old rows stay valid):

| Column | Meaning |
|---|---|
| `logical_request_id` | uuid — groups all attempts of one logical request |
| `attempt_number` | 1 = primary, 2 = remote alt, 3 = local |
| **`is_final`** | **true on exactly one row per logical request** — the row that determines the outcome |
| `requested_provider` / `selected_provider` | policy+config choice vs who actually answered |
| `data_class` | INTERNAL_SENSITIVE / CUSTOMER_EXTERNAL / BUSINESS_INTERNAL |
| `failover` | this row is a failover attempt |
| `failure_class` | from the taxonomy (`NETWORK`, `AUTH`, `POLICY_FORBIDDEN`, …) |
| `failure_reason` | **redacted**: exception class + status + ≤120 chars, scrubbed |
| `policy_reason` | why an alternate was refused |
| `attempt_latency_ms` | this attempt (existing `latency_ms` keeps its meaning) |
| `total_latency_ms` | whole logical request — written on the `is_final` row only |
| `outcome` | `ok` / `failed_over_ok` / `fallback` / `budget` / `policy` |
| `deterministic_fallback` | the caller's scripted path ran |

### 7.1 Explicit semantics — nothing changes implicitly

| Question | Definition | Query |
|---|---|---|
| **Error rate (business)** | Per **logical request**. A request that failed over and succeeded is a **SUCCESS**. | `count(*) FILTER (WHERE is_final AND NOT ok) / count(*) FILTER (WHERE is_final)` |
| **Provider error rate (health)** | Per **attempt** — this is what detects a sick provider. | `count(*) FILTER (WHERE NOT ok) / count(*)` grouped by `selected_provider` |
| **Cost** | Summed across **all attempts** — a failed attempt still burned tokens. | `SUM(...)` over all rows, no `is_final` filter |
| **Calls** | Per **logical request**. | `count(*) FILTER (WHERE is_final)` |
| **Latency (user-perceived)** | `total_latency_ms` on the `is_final` row. | — |
| **Latency (provider health)** | `attempt_latency_ms` per attempt. | — |
| **Failover success** | Counts as **success** at the logical level **and** as a provider-level failure for the primary. Both are true and both are recorded. | — |

### 7.2 Existing-consumer migration (no silent corruption)

| Consumer | Change |
|---|---|
| `llm_meter.usage_summary` | Add `WHERE is_final` to call counts and failures; **leave token/cost sums unfiltered** |
| `llm_meter.spend_lines` → CEO briefing | Cost unfiltered (correct as-is); call count gains `is_final` |
| U3 `llm_error_rate` | **Split into two metrics**: logical failure rate + provider attempt error rate |
| Backfill | Existing rows: `is_final=true`, `attempt_number=1`, `logical_request_id=gen_random_uuid()` — every historical row is its own single-attempt logical request, so all existing queries remain exactly correct |

**Never logged:** prompts, message content, customer data, API keys, base URLs
with credentials. `failure_reason` passes a scrubber dropping key patterns
(`sk-`, `ek_`, bearer tokens), email addresses, and long digit runs.

---

## 8. Startup provider/model health check

**The U-2 requirement: a failover target that is configured but unavailable must
be visible BEFORE an outage.** Today's live config proves the need —
`ollama_model=gpt-oss:20b` is configured but **not installed** (`gemma4:31b` is),
so a failover would have hit `NOT_FOUND` and been correctly refused as class B.
U5 would have bought exactly nothing, silently.

### Design

`provider_health(provider) → {reachable, model_present, usable, checked_at, detail}`

### ⚠️ v3 CORRECTION — probe, don't just list

A catalogue lookup is **not** a usability check. Proven live: `gemini-2.5-flash`
and `gemini-2.5-flash-lite` are both **listed** by `GET /v1beta/models` and both
**404 on `generateContent`** with our key. The health check must issue a
**minimal real generation**.

| Provider | Credential check | Usability check (**must generate**) | Cost |
|---|---|---|---|
| OpenAI | `OPENAI_API_KEY` present | 1-token completion, `max_tokens=1` | negligible / TTL |
| **Gemini** | `GOOGLE_API_KEY` present | `:generateContent` "reply OK", `maxOutputTokens: 8` — **a 200 here is the only proof the model is usable** | negligible / TTL |
| Ollama | base URL set | `GET /api/tags` → model name present (local install is authoritative) | local, free |

- **Cached with a TTL** (`LLM_HEALTH_TTL_SECS`, default 900) — never in the hot
  request path.
- **Runs at startup**, logged at WARNING when a configured alternate is unusable:
  `[llm_router] failover target claude/claude-haiku-4-5 UNUSABLE: no API key`.
- **Surfaced in U3 Platform Health** as a new `failover_readiness` metric in the
  **platform** section: `ok` when every configured alternate is usable, `warning`
  when a configured alternate is unusable (the silent-misconfiguration case),
  `ok` with a note when no alternate is configured at all (U5 simply off).
- **Feeds the router**: an unusable provider is skipped with `TIER_UNAVAILABLE`
  rather than burning deadline on a doomed attempt.
- **Exposed** at `GET /llm/providers` (admin) for pre-incident inspection.

---

## 9. Provider / model capability profile

A static registry — **the router never assumes providers are equivalent.**

```python
PROVIDERS = {
  "openai": {
    "kind": "remote", "vendor": "OpenAI", "egress": True,
    "models": {"standard": "gpt-4o-mini", "lite": "gpt-4o-mini"},
    "supports": {"messages_text": True, "structured_output": True,
                 "tool_calling": True, "streaming": True},
    "timeouts": {"connect": 5, "first_response": 20, "attempt": 30},
    "profile": {"json_in_prose": "high", "style": "baseline",
                "relative_cost": 1.0, "relative_latency": 1.0},
  },
  "anthropic": {
    "kind": "remote", "vendor": "Anthropic", "egress": True,
    "models": {"standard": "claude-sonnet-5", "lite": "claude-haiku-4-5"},
    "supports": {"messages_text": True, "structured_output": True,
                 "tool_calling": True, "streaming": True},
    "timeouts": {"connect": 5, "first_response": 25, "attempt": 35},
    "profile": {"json_in_prose": "high", "style": "differs — more measured, "
                "different length calibration", "relative_cost": 6.5,
                "relative_latency": 1.1},
    "notes": "ZDR available on these models. Fable 5 requires 30-day "
             "retention — NEVER select it when LLM_REQUIRE_ZERO_RETENTION=1.",
  },
  "ollama": {
    "kind": "local", "vendor": "self-hosted", "egress": False,
    "models": {"standard": None, "lite": None},   # MUST be set explicitly
    "supports": {"messages_text": True, "structured_output": False,
                 "tool_calling": False, "streaming": True},
    "timeouts": {"connect": 2, "first_response": 90, "attempt": 120},
    "profile": {"json_in_prose": "medium", "style": "differs markedly",
                "relative_cost": 0.0, "relative_latency": 6.0},
  },
}
```

**Recorded explicitly** so a failover is never mistaken for an equivalent
answer: reasoning quality, response style, latency, token usage and JSON
reliability all differ across providers. `outcome='failed_over_ok'` in telemetry
is the queryable marker for "this reply came from a different brain."

---

## 10. Configuration variables

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `openai` | Primary. **Unchanged.** |
| `LLM_FAILOVER_ENABLED` | `0` | **Master switch — U5 is OFF until explicitly enabled** |
| `LLM_ALT_PROVIDER` | **`gemini`** | Remote alternate (**v3: decided**) |
| `GOOGLE_API_KEY` | — | **Already provisioned in `.env`** (validated live) |
| `LLM_ALT_MODEL` | **`gemini-3.5-flash-lite`** | Alternate, standard tier |
| `LLM_ALT_MODEL_LITE` | **`gemini-3.5-flash-lite`** | Alternate, lite tier — same model: measured 784 ms p50 and 3/3 JSON parse; `gemini-3.5-flash` is 11.7× slower |
| `LLM_LOCAL_FALLBACK_ENABLED` | `0` | Opt-in third hop (Ollama) |
| `LLM_LOCAL_MODEL` / `_LITE` | — | **No default** — must be set and health-checked (§8). Local install has `gemma4:31b` |
| `LLM_ALLOW_EGRESS_FAILOVER` | **`0`** | **U-1 confirmed** — local→external hop is opt-in |
| `LLM_LOCAL_ONLY_CALLERS` | `it-service,people-hr,custom_agents_internal` | INTERNAL_SENSITIVE roster |
| `LLM_EXTERNAL_ALLOWED` | `1` | Residency gate against `DATA_REGION` |
| `LLM_REQUIRE_ZERO_RETENTION` | `0` | Deny providers/models without attested ZDR |
| `LLM_DEADLINE_INTERACTIVE` | `30` | Logical deadline (s) |
| `LLM_DEADLINE_ASYNC` | `75` | Logical deadline (s) |
| `LLM_DEADLINE_BACKGROUND` | `240` | Logical deadline (s) |
| `LLM_PRIMARY_CAP_INTERACTIVE` | **`12`** | **v3**: primary cap ≤ 40 % of deadline so the alternate can run |
| `LLM_ALT_CAP_INTERACTIVE` | **`15`** | Alternate cap (measured need: <1 s) |
| `LLM_MIN_ATTEMPT_SECS` | `6` | Skip a provider if less remains |
| `LLM_HEALTH_TTL_SECS` | `900` | Provider health cache TTL |

**New dependency:** `langchain-google-genai` (verified: none of
`langchain_google_genai`, `langchain_anthropic`, `langchain_mistralai`,
`langchain_groq` is installed). Add to `requirements.txt`.

> Validation used raw HTTPS against `generativelanguage.googleapis.com` — no new
> dependency was installed to run these tests, and none is needed until
> implementation.

**Migration:** `sql/llm_usage_failover.sql` — additive nullable columns +
backfill (`is_final=true, attempt_number=1`).

---

## 11. Test plan

`tests/test_llm_failover.py` — **injected fake providers only, no network.**

| # | Test | Asserts |
|---|---|---|
| 1 | OpenAI succeeds | 1 attempt, `outcome=ok`, alternate never constructed |
| 2 | Transient 5xx → Claude succeeds | 2 rows, `failover=true`, `outcome=failed_over_ok`, one `is_final` |
| 3 | OpenAI timeout → Claude succeeds | `failure_class=TIMEOUT`, deadline respected |
| 4 | Remote alt fails → local Ollama (when enabled+healthy) | 3 attempts, local answers |
| 5 | Both remote fail, no local | last exception re-raised → deterministic fallback |
| 6 | **INTERNAL_SENSITIVE never egresses** | 0 external attempts; `POLICY_FORBIDDEN`; local-or-fallback only |
| 7 | **Egress failover disabled by default** | local-primary config does **not** reach external with default env |
| 8 | Auth failure | **no** failover; raises; `failure_class=AUTH` |
| 9 | Content-policy refusal | **no** failover — never re-sent to another provider |
| 10 | Invalid request (400) | **no** failover; raises |
| 11 | Budget exhausted | raises **before** any provider; **0** attempt rows |
| 12 | **Configured model unavailable detected pre-outage** | health check flags it; router skips with `TIER_UNAVAILABLE`; U3 shows `failover_readiness=warning` |
| 13 | Provider-specific timeouts | Ollama gets 90 s first-response, OpenAI 20 s; `interactive` deadline caps the total |
| 14 | Logical/attempt telemetry consistency | exactly one `is_final` per `logical_request_id`; attempts numbered 1..n; `total_latency_ms` only on final |
| 15 | Cost & error-rate correctness | cost = Σ all attempts; logical error rate excludes recovered failovers; provider error rate includes them |
| 16 | Redaction | an exception carrying `sk-…`, an email and a prompt fragment never reaches `failure_reason` or the log |
| 17 | Existing suites green | `tests/test_agent_bus_drain.py` (9) + `python -m app.core.eval_suite` |

---

## 12. Implementation location (approved shape)

**One layer at the existing chokepoint. Zero agent-specific changes.**
`llm_meter.MeteredLLM` becomes `ProviderRouter` (alias kept). `_get_llm` gains
lazy alternate construction. All 39 call sites and their `try/except`
deterministic fallbacks are untouched; a successful primary call adds two dict
lookups and one cached health read.

---

## 13. Unresolved decisions — need your call

**U-7 · Are the internal IT/HR agents INTERNAL_SENSITIVE today?** The matrix
says internal content never egresses. But with `LLM_PROVIDER=openai`, their
*primary* is already OpenAI — so enforcing the rule changes **current**
behaviour, not just failover behaviour, and makes those agents depend on a local
Ollama being installed and healthy. Three options: **(a)** enforce it — most
consistent with U2's `reach_invariant`, but internal agents stop working without
a healthy local model; **(b)** classify them CUSTOMER_EXTERNAL for now and
enforce only when a local model is provisioned; **(c)** add a
`LLM_INTERNAL_STRICT` flag defaulting to (b) with (a) available. **My
recommendation: (c)** — it makes the intent explicit and the migration
deliberate.

**U-8 · RESOLVED (v3).** Tier mapping is `gemini-3.5-flash-lite` for both tiers,
matching our own `standard`/`lite` both resolving to `gpt-4o-mini`. Measured, not
assumed: 3/3 JSON parse, 784 ms p50.

**U-9 · Is Canadian residency contractual?** Still open, but **no longer changes
the vendor** — Gemini is now the alternate, so residency is a *deployment* choice
within the same vendor: first-party `generativelanguage.googleapis.com` (current,
US-operated) vs **Vertex AI `northamerica-northeast1` (Montreal)**. A config and
client change, not a re-selection.

**U-11 · 🔴 NEW AND BLOCKING: is this Google key on a PAID tier?** Free-tier
Gemini content **may be used to improve Google's products**; paid-tier content is
not. This is the exact footgun v2 flagged when ranking Gemini second, and
provisioning the key has made it live. **Confirm the key's billing tier in the
Google Cloud console before any customer data is routed through it.** If it is
free-tier, U5 must stay disabled (`LLM_FAILOVER_ENABLED=0`) until it is upgraded
— otherwise an outage would silently route CRM conversations into a
training-eligible tier, which would also contradict the Trust Center posture
(#8). I cannot determine the tier from the API.

**U-10 · Confirm interactive deadline = 30 s.** The tightest number in §5 and the
one most likely to convert a slow-but-successful call into a fallback.

**U-5 (embeddings) — CONFIRMED DEFERRED as U5b.** `semantic._embed` stays
OpenAI-only. Documented rationale: `nomic-embed-text` is installed locally but
produces 768-dim vectors against an index built on OpenAI's 1536-dim
`text-embedding-3-small` — they are **not interchangeable**, and a mid-flight
swap would corrupt similarity search. Multi-provider embeddings require a second
index and a migration, not a failover. **The existing index is not touched.**
Residual exposure: during an OpenAI outage, semantic KB retrieval degrades to
FTS-only even with chat failover working.
