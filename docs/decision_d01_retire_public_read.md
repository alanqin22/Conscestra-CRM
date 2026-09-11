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

**It was deliberately NOT exempted in this change.** The approved scope was
retirement; carving an exemption in the same change would mean the retirement
ships together with its first exception, and the exception would be reviewed as
part of a change whose headline is the opposite. It was recorded here as an
available, evidenced option and left as a separate decision.

### UPDATE 2026-09-11 — that decision has now been made: `/home-index` IS exempt

Taken as a separate change, on its own evidence, after retirement was live and
verified.

**It is not an exception to the invariant — it is outside it.** The invariant
governs customer SUBJECTS; `/home-index` exposes none, so there is no `s` for
`PublicRead(s) -> ProvenDemo(s)` to range over. Verified twice, because "no PII
in the sample I looked at" is an observation and not a property:

- **BY CONSTRUCTION** — the route declares `response_model=HomeIndexResponse`
  (four KPI objects plus metadata). FastAPI filters the response to the declared
  fields, so an SP that began returning a contact could not deliver one through
  this route. 30 declared fields, none naming a person, account or lead.
- **IN FACT** — the whole production payload of `sp_home_index` was read
  read-only on 2026-09-11 and probed for `contact_id`, `account_id`, `lead_id`,
  `email`, a bare `@`, `phone`, `first_name`, `last_name`, `street` and `name`.
  All absent. The two untyped `list` fields — the ones the response model does
  **not** bound — carry `[{count,status}]` and `[{day,count}]`.

**What it discloses, stated rather than glossed:** aggregate pipeline value and
lead / order / alert counts. Anyone may infer business scale. That is the point;
it is a marketing dashboard.

**Residual:** `owner_id` and `employee_uuid` query parameters scope the
aggregates, so a caller who already holds a valid owner UUID can learn that
owner's counts. They cannot enumerate UUIDs from here and the answer is still
only counts. If that stops being acceptable, restrict the parameters rather than
re-gating the route — re-gating takes the front page down again.

**A latent contradiction this surfaced.** The registration comment in `main.py`
already said *"PUBLIC ... so it is not session-gated"* while the code gated it
with `_DATA`. Under `public-read` the contradiction was invisible, because
anonymous reads passed the gate anyway. Retirement is what made it visible: the
front page went 401. The comment recorded the intent; the code had quietly
diverged from it, and only the stricter posture revealed which was true.

**A PRIOR DECISION THIS CHANGE REVERSES, and how it was honoured rather than
overridden.** `test_60_home_index_carries_the_data_dependency` already required
the gate on this route, for a reason that was **not** about customer data:

> *"Aggregate pipeline / leads / orders / alert counts. No customer records, but
> anonymous access lets anyone infer business scale."*

That reason is correct and was measured live before acting on it: **pipeline
$1,209,865.57, weighted $369,988.26**, 98 opportunities, 93 leads, 69 orders. The
front page's main card displays exactly those dollar figures.

The two requirements are not in conflict once separated:

| | |
|---|---|
| the route may be public | because it exposes no customer **subject** |
| the **money** must not be public | because business scale is nobody's by default |

So the route is ungated **and the pipeline value is redacted server-side** for
callers without a session. Counts to everyone; amounts only to a session.

- `total_amount` / `weighted_amount` are `Optional[float]`, `None` for
  anonymous. **Null, never `0.0`** — `0.0` asserts an empty pipeline, which is a
  false statement rather than a withheld one.
- `metadata.amounts_disclosed` says which state the payload is in, so a client
  never infers "withheld" from a null.
- Redaction is **server-side**. A client-side choice is not a control; the value
  would still travel in the payload.
- A trap caught on the way: `_kpi_pipeline` coerced with `float(x or 0)`, which
  silently turned the withheld `None` back into `0.0` and would have republished
  the redacted state as a false claim.

`test_60` was **inverted and re-justified, not deleted.** Deleting it would have
left the strongest argument for the old behaviour with nowhere to live, and the
next person would rediscover it the hard way.

**The front page needs a small edit to match:** Card 1 currently renders
`fmtK(P.total_amount)`. With the amount withheld it should show the opportunity
count. `index.html` / `index2.html` are hand-deployed and not in this repository.

**`/order-chat` remains un-exempt and must stay that way.** It discloses
`contact_id`, `account_id`, `email`, `phone` and `account_name`.

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
