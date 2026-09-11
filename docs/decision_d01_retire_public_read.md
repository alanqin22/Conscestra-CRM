# Decision D-01 — retire anonymous public-read

**2026-09-11.** Closes **P-01** of `docs/reassessment_public_read_corpus_2026-09-11.md`.
Analysis: `docs/phase1_public_read_enforcement_analysis_2026-09-11.md`.

**Decision: `API_SECURITY_MODE=locked`. Anonymous callers may no longer read CRM
customer data.**

---

## The invariant, and how this satisfies it

> for every customer subject `s`:  `PublicRead(s) → ProvenDemo(s)`

Under `locked`, `PublicRead(s)` is **false for every `s`**, so the implication
holds **vacuously**. This is the strongest available form of the property: a
subject that cannot be read anonymously cannot be read anonymously without
proven provenance.

**No provenance was reconstructed, no subject relabelled, and nothing unknown was
treated as synthetic.** The 340 unproven subjects remain exactly as unproven as
they were. The invariant is satisfied by removing the exposure, not by
manufacturing certainty about the corpus.

## Why not enforce the invariant and keep public-read

Measured (DATABASE-VERIFIED, 2026-09-11):

| Subject | Live rows | Proven demo | Visible if the invariant were enforced with public-read retained |
|---|---|---|---|
| `contacts` | 129 | 6 | 6 |
| `accounts` | 118 | 6 | 6 |
| `leads` | 105 | 0 | **0** |
| **Total** | **352** | **12** | **12 (3.4%)** |

The posture exists so a prospect can see the CRM working without a login. A demo
of 6 contacts, 6 accounts and no leads does not serve that purpose. **The control
that would have made the risk acceptable also removes the reason for accepting
it.**

The alternative — enforcing `ProvenDemo(s)` at the row via RLS — was analysed in
full (Phase 1 §3) and is viable: the SPs are INVOKER-rights, `crm_app` lacks
`BYPASSRLS`, and pagination would stay truthful because the SP computes it over
the filtered set. It is not being built, because it would deliver the 12 rows
above.

## What this costs — measured, not estimated

**These break, and they are a product loss, not a bug:**

| Surface | Depends on | Effect |
|---|---|---|
| `index.html` / `index2.html` (agentorc.ca homepage) | `GET /home-index` | live dashboard shows its error state |
| `store-home.html` (public storefront) | `POST /order-chat` | order lookup fails |
| `store-home.html` | `POST /voice/azure-token` | voice input fails |

**These are unaffected** — verified: they do not carry the `_DATA` gate:
`store_router`, `auth_router`, `portal_router`, `embed_public_router`,
`sdr_public_router`.

**The operator consoles are unaffected once signed in.** `*-mgmt.html` carry a
fetch shim that attaches `Authorization: Bearer <token>` when a session exists,
the same mechanism `governance-mgmt.html` already uses in production.

## A finding this work surfaced: `/order-chat`

**DIRECTLY OBSERVED.** The 2026-09-11 reassessment measured contacts, accounts
and leads. It missed `/order-chat`, which in list mode returns a table with
**Account | Contact | Email** columns and discloses `contact_id`, `account_id`,
`email`, `phone` and `account_name` across **2,462 orders, 99 pages** — to an
anonymous caller.

It is a fourth anonymous subject-disclosure path, reached through a different
router. It changes no conclusion — retirement closes it along with the rest — but
it is direct evidence for *why* the gate is the right enforcement point:
per-endpoint reasoning missed an endpoint, in an audit specifically looking for
them.

## What must NOT be done to restore the broken surfaces

**`/order-chat` must not be exempted from the data gate.** It discloses customer
subjects; exempting it re-opens P-01 through the same door under a different
name. If the storefront needs order lookup for anonymous shoppers, that is a
scoped-to-the-caller feature (the portal's `set_customer_scope` model), not a
posture exemption.

**`/home-index` is the one that could legitimately be exempted.** It returns only
aggregates — `active_pipeline`, `open_leads`, `pending_orders`, `unread_alerts`
— and DIRECTLY OBSERVED contains no `email`, `phone`, `first_name`,
`contact_id`, `full_name` or address field. Exempting it would not expose a
subject and would restore the marketing homepage.

**It is deliberately NOT exempted in this change.** The approved scope was
retirement; carving an exemption in the same change would mean the retirement
ships together with its first exception, and the exception would be reviewed as
part of a change whose headline is the opposite. It is recorded here as an
available, evidenced option and left as a separate decision.

## Verification required

Retirement is a Railway environment variable, so **tests cannot prove the
deployed posture**. The required evidence is a probe at the deployed boundary:

1. anonymous `POST /contact-chat` → **401**, not 200;
2. anonymous `POST /account-chat`, `/lead-chat`, `/order-chat` → **401**;
3. `/health` still healthy, scheduler and leader unaffected;
4. `release_guard` no longer warns `api_auth: posture=public-read`;
5. `public_read_corpus` reports `posture=locked — the corpus assumption is not
   load-bearing here`;
6. an authenticated operator console still reads data.

Item 1 is the one that closes P-01: the same probe that returned *"25 contacts,
23 unclassified"* must return 401.

## Status

| | |
|---|---|
| D-01 | **DECIDED — retire** |
| P-01 | closed by retirement, pending the deployed-boundary probe |
| P-02 (coverage reporting) | still open; unaffected by this decision |
| P-03 (`ambiguous` unused) | still open; consequence is remediation tracking only |
| Provenance of the 340 | **unchanged and still NOT PROVABLE** |
| Code changed | **none** — the enforcement already exists in `require_data_access` |
