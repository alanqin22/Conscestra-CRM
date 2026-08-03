# Multi-Tenancy — decision & roadmap (Platform blindspot P4)

> Status doc, 2026-07-24. Conscestra CRM is a **single-organization product**
> today. Full SaaS multi-tenancy is **deferred**. This documents the ratified
> decision and the minimal seam we shipped so the door stays open — "single-company
> today, multi-tenant-*ready*."

## The decision (ratified 2026-07-24)

- **Current product:** single-organization CRM (agentorc.ca runs one org's CRM;
  `executives`, KB, `DATA_REGION`, compliance posture are all "the company's").
- **Business decision:** full SaaS multi-tenancy **DEFERRED** — it's a product
  pivot, not a feature.
- **Architecture decision:** introduce a **minimal tenant-routing seam** now, so
  future multi-tenancy is *populate a registry + provision schemas + flip a flag*,
  not a rewrite.
- **Recommended future model:** **schema-per-tenant** by default; **database-per-
  tenant** for enterprise / data-residency tenants. Both served by one seam.

## Why this model (grounded in how the code is built)

- **One chokepoint.** All ~360 data-access sites funnel through
  `database.get_connection()` / `execute_sp()`. Tenancy attaches in exactly one
  place.
- **Stored-procedure data layer.** Access is `SELECT sp_*(...)`; the SPs have no
  org awareness. A shared-schema `org_id`/RLS model would require rewriting **every
  SP** (against the "don't modify SPs" policy) and risks cross-tenant leaks through
  SECURITY DEFINER functions. **Rejected as the entry path.**
- Schema/DB-per-tenant makes tenancy a **routing** decision at `get_connection`,
  with **zero SP changes and zero rewrite** of the 360 sites.

## What shipped — Phase 0 (the seam)

`MULTI_TENANT_ENABLED=0` by default → **behaviourally identical to before.**

- `app/core/tenancy.py` — a request-scoped `tenant_context` (same pattern as
  `write_guard`'s role context) + `resolve(tenant_id) → (dsn, schema)` over a
  **`tenants` registry**. Explicit `"default"` fallback; an unknown/inactive tenant
  in multi-tenant mode **fails closed** (never a silent default). Schema names are
  validated (`^[a-z][a-z0-9_]{0,62}$`) before use.
- `sql/tenants.sql` — the `tenants` registry (control plane, `public`), seeded with
  one `default` row = today's org. No business table changed.
- `database.get_connection()` — resolves `(dsn, schema)` and applies the schema via
  the **`search_path` connection option on EVERY connection** (safe from a stale or
  pooled search_path); validated identifier, never raw-interpolated. `public` when
  off = today's exact behaviour.
- Sessions carry `tenant_id` (`auth/router.get_session`, default `default`);
  `auth_dep` stamps it into `tenant_context` next to the role.

### What Phase 0 proves — and what it does NOT

Phase 0 proves the **routing seam**: a request reaches the right schema and a write
is invisible across schemas, all through the single chokepoint, with existing
behaviour unchanged. It is **not** a hard SaaS isolation boundary and does **not**
establish background-worker, in-process-cache, rate-limit, billing, or full
application-level tenant isolation. Those are later phases. Hard isolation against
*untrusted* tenants favours **database-per-tenant** and/or RLS; schema-per-tenant is
the pragmatic default for *trusted, provisioned* tenants.

## Roadmap (gated on the business decision to go SaaS)

1. **Provisioning & routing** — a migration runner that creates a tenant schema and
   applies every `sql/` migration; request→tenant resolution (subdomain/header/
   session). Self-serve org creation ties into the P2 readiness + P6 demo flows.
2. **Background work per tenant** — the `leader` (see `docs/scaling_and_concurrency.md`)
   runs the scheduler / bus / IMAP **looping over active tenants** (set `search_path`
   per tenant per cycle). Per-tenant config/secrets (SMTP, telephony, `DATA_REGION`).
   *The single biggest hidden cost of multi-tenancy.*
3. **Commercial** — per-tenant billing/metering (extend `llm_meter` with a tenant
   dimension), per-tenant rate limits, a cross-tenant super-admin console.
4. **Hardening** — per-tenant connection pooling (or a pooler with per-tenant
   routing), noisy-neighbour isolation, per-tenant backup/residency.

## Gotchas (decide with eyes open)

- **In-process global state must become tenant-keyed** — the blackboard, rate-limit
  dicts, in-proc caches. Today they assume one org.
- **Background singletons × tenancy** — every scheduled job must run per tenant.
- **Connection multiplication** — N tenants × per-call connects; put a transaction
  pooler in front of Postgres first (already the scaling doc's #1 recommendation).
- **Baked-in single-org assumptions** — `executives`, KB, `DATA_REGION`, compliance,
  SMTP/telephony secrets all become per-tenant.
