# Compliance & Data Residency — posture (blindspot #8)

> Written 2026-07-23 as part of the Agentforce-parity blindspots pass
> (`skills.md`). **This is a self-attested control inventory and roadmap, not a
> third-party certification.** Where it states a control is "implemented", that
> reflects code in this repository; where "configurable" or "attested", it
> depends on how *you* deploy and contract. The live version of this inventory is
> served at `GET /compliance/posture` and rendered at `/trust.html`.

## Why this exists

We already had strong controls — they just weren't packaged as a compliance
*story* a security reviewer or prospect could read. #8 turns the existing
strengths into that story, and surfaces the one thing a US platform like
Agentforce can't claim as easily: **Canadian data residency**.

## Data residency

- **Model:** single-region — the FastAPI application and its PostgreSQL database
  are co-located in one region. There is no cross-region replication of customer
  data in the base architecture.
- **Canadian residency:** deploy both the app and the database in a Canadian
  region (e.g. `ca-central-1`) and customer data stays in Canada. Declare the
  region you run in via `DATA_REGION`; it appears on the Trust Center page and in
  the posture API so the claim is explicit and verifiable, not implied.
- **The one cross-border hop to be honest about:** LLM inference. Prompts go to
  the configured model provider, which may process them outside Canada. Two
  mitigations ship today: (1) **PII is masked before any prompt** (emails,
  phones, card-like runs), so identifiers don't leave in the clear; (2) set
  `LLM_ZERO_RETENTION=1` once your provider contract is zero-retention. For
  strict in-country inference, point the model factory at a Canadian-hosted
  model endpoint (roadmap note below).

## Control inventory (summary)

The authoritative, live list is the `/compliance/posture` payload. Categories:

| Area | Highlights | Status |
|------|-----------|--------|
| Data residency | single-region hosting; Canadian-region option | configurable |
| Data protection | PII masking before LLM; TLS in transit; hashed session tokens | implemented |
| AI data handling | per-call metering & budgets; model zero-retention | implemented / attested |
| Consent & privacy law | CASL/GDPR consent, suppression list, unsubscribe, verified-recipient gates | implemented |
| Access control | RBAC; admin-gated command APIs; **DB-enforced read-only channels**; write classification at the DB choke point (coverage-tested); rate-limit & lockout | implemented |
| Auditability | immutable attributable audit log; correlation-id tracing proposal→execution | implemented |
| AI governance | HITL amount floor; independent critic; deterministic outbound guard; four-layer guardrails | implemented |

Status legend: **implemented** (code in this repo) · **attested** (depends on
your contract/config, declared via env) · **configurable** (depends on how you
deploy) · **roadmap** (not yet built).

## Data subject rights

Access/export, correction (via governed profile updates), erasure/suppression
(unsubscribe + suppression list), and consent withdrawal at any time. Requests
go to the compliance contact (`COMPLIANCE_CONTACT`, default `privacy@agentorc.ca`).

## What is NOT claimed (honesty section)

- **No third-party certification is asserted.** SOC 2 / ISO 27001 / etc. are not
  held unless and until independently audited; this document and the Trust Center
  say so explicitly. Treat this as a controls inventory to *support* an audit, not
  a substitute for one.
- **Sub-processors** (model provider, hosting/database provider) must be listed
  and kept current by the operator; the posture payload carries a reminder, not a
  canonical list.

## Configuration

| Env | Purpose | Default |
|-----|---------|---------|
| `DATA_REGION` | The deployment region you attest (e.g. `ca-central-1`) | unset |
| `LLM_ZERO_RETENTION` | `1` if the model-provider contract is zero-retention | `0` |
| `COMPLIANCE_CONTACT` | Privacy/security contact shown on the Trust Center | `privacy@agentorc.ca` |

## Roadmap (to move items from configurable/roadmap → implemented)

1. **Declare and pin a Canadian region** in the deployment and set `DATA_REGION`.
2. **Zero-retention model contract** (or a Canadian-hosted inference endpoint for
   strict in-country processing), then `LLM_ZERO_RETENTION=1`.
3. **Formal audit** (SOC 2 Type II) using this inventory as the control map.
4. **DSAR workflow** endpoint to automate access/export/erasure requests.
5. **Signed sub-processor list** surfaced on the Trust Center page.
