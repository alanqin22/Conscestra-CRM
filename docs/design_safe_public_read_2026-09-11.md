# Design — public-read as a safe second option

**2026-09-11. ANALYSIS ONLY.** Nothing was changed in production. One reversible
experiment was run against the **local** database and its cleanup is verified
below.

Question: *can `API_SECURITY_MODE=public-read` return as a marketing option, with
the safety property actually enforced?*

**Answer: yes. The enforcement half is proven to work. The corpus half is a
decision you have not yet made, and two implementation traps would each ship as a
vulnerability if taken the obvious way.**

Context: D-01 retired public-read and P-01 is closed (`locked` is live and
verified at the deployed boundary). Nothing here is urgent; there is no standing
exposure while this is decided.

---

## 1. What must be true

> for every customer subject `s`: `PublicRead(s) → ProvenDemo(s)`

Retirement satisfies this vacuously. To return to public-read *safely*, two
things must both hold:

| | Status |
|---|---|
| **A gate** that shows only `ProvenDemo` rows to anonymous callers | **does not exist** — now proven buildable |
| **A corpus** where `ProvenDemo` is genuinely true for what you want shown | **12 of 352 today** |

The gate is the part that matters most, and for a reason worth stating: **with the
gate, safety stops being a property of the data and becomes a property of the
boundary.** Today public-read's safety is an assumption about database contents
that nothing re-checks, and it decays silently every time a seed adds rows
(P-02). With the gate, an unattested row is simply invisible. You no longer have
to trust the corpus — and nobody can make a real customer record visible by
mistake, because doing so would require attesting it as synthetic, which is not
something anyone can truthfully do for a real record.

## 2. The gate works — REPRODUCED, not inferred

Run against the **local** database as `crm_readonly` (`rolbypassrls = false`),
with the real `sp_contacts`, then dropped:

```
BASELINE (no RLS)              raw_count=182   SP_total=182   page_rows=5
GUC UNSET (must fail closed)   raw_count=6     SP_total=6     page_rows=5
GUC = 'off'  (authenticated)   raw_count=182   SP_total=182   page_rows=5
GUC = 'on'   (anonymous)       raw_count=6     SP_total=6     page_rows=5

[CLEANUP] policies on contacts=0   relrowsecurity=False
```

| Claim | Result |
|---|---|
| RLS filters **inside** the INVOKER-rights SP | **TRUE** — SP total 182 → 6 |
| **Pagination and `Total:` count the visible set** | **TRUE** — 6 == 6, with no SP rewrite |
| An unset GUC **fails closed** | **TRUE** — 6, not 182 |
| Authenticated access is unaffected | **TRUE** — 182 |

The pagination result is the one that makes this cheap. `sp_contacts`,
`sp_accounts` and `sp_leads` total ~238KB of source and compute their own counts;
because they run with invoker rights, those counts are computed over the filtered
set automatically. **No SP is modified.** RLS also covers the **50 modules** that
read these tables directly outside the SP path — by construction, not by
enumeration.

**A methodological note, because the first attempt was wrong.** The experiment
initially showed *no filtering at all*, and the cause was my test role:
`FORCE ROW LEVEL SECURITY` makes a policy apply to the table **owner**, but it
does **not** override the `BYPASSRLS` role attribute, and `postgres` holds it.
Re-run as `crm_readonly` it filtered correctly. A control tested as a superuser
tests nothing.

## 3. Two traps, each of which would ship as a vulnerability

### Trap 1 — a session-scoped GUC survives into the connection pool

The existing seam (`database.py:238-254`) stamps `app.correlation_id` with
`set_config(..., false)` — session-scoped — and early-`return`s when there is
nothing to stamp, leaving the previous value in place. Copying that shape for an
authorization GUC is fail-open. Demonstrated on one connection:

```
fresh connection                                     (unset)
request A (authenticated) sets 'off' session-scoped  off
after COMMIT (connection back in the pool)           off
request B (anonymous) inherits                       off
```

Request B is served as authenticated. Silent, intermittent, load-dependent, and
invisible to any single-request test.

**The fix is transaction-local** (`set_config(..., true)`), proven not to
survive:

```
inside the transaction                             off
after COMMIT                                       (empty)
next request on the same connection                (empty)
```

### Trap 2 — and the fix for Trap 1 creates it

A transaction-local GUC does not unwind to `NULL`. It unwinds to the **empty
string**, which is the normal state of a pooled connection after the safe
implementation has run once. The obvious predicate then fails open:

| GUC state | `coalesce(current_setting(...), 'on') <> 'on'` | `coalesce(nullif(current_setting(...), ''), 'on') <> 'on'` |
|---|---|---|
| NULL (never set) | False | False |
| **`''` (unwound)** | **TRUE — every row visible** | False |
| `'off'` (authenticated) | True | True |
| `'on'` (anonymous) | False | False |

**The two obvious choices combine into a hole.** Scope the GUC safely and the
predicate leaks; write the predicate naively and the safe scoping is what
triggers it. The correct form is `nullif(current_setting(...), '')`, and it needs
a test that asserts the empty-string state specifically — `NULL` and `''` are not
the same thing here, and only one of them arises in production.

## 4. The corpus half — the part that is a decision, not a build

Only two writers into `corpus_provenance` exist: `classify()`, which records
`synthetic` **only** where the row's own `is_synthetic IS TRUE` (rule
`flagged_synthetic`), and the executive-provisioning path for `owners`. **There
is no human-attestation endpoint for customer subjects.**

| Route | What it means | Honest assessment |
|---|---|---|
| **(a) Provenance at birth** | the demo-data generator writes `corpus_provenance` with `rule='seed_generator'` as it creates each row | **The only route where the evidence is real rather than recalled.** Needs a generator hook + a write path |
| **(b) Attest existing rows** | a person declares the 340 synthetic via `human_attested` | **Defensible only for rows you genuinely created and remember.** The seed-email migration destroyed the distinction and `is_synthetic` is `false` *by column default* on 123/129 contacts. Attesting all 340 would be fabricating the evidence this whole model exists to prevent |
| **(c) Curated demo subset** | generate and attest ~40 records properly; the rest stay invisible | **Recommended.** With the gate this is automatic — nothing is deleted or moved, the other 312 simply do not appear |
| **(d) Separate demo deployment** | own database, synthetic by construction | Safest; most infrastructure |

**(c) is the one that actually meets the marketing goal.** A prospect sees ~40
realistic records across contacts, accounts and leads; the invariant is genuinely
enforced rather than assumed; and no claim is made about the 312.

## 5. Scope

| Piece | Notes |
|---|---|
| RLS policies on `contacts`, `accounts`, `leads` | one governed migration; `nullif` predicate |
| GUC stamped **transaction-locally**, unconditionally, on every borrow | the `database.py` seam exists; must not early-return |
| Attestation write path | `human_attested` / `seed_generator`, admin-gated |
| Generator hook | writes provenance at row creation |
| Tests | the 14 from the Phase-1 contract, **plus** the empty-string predicate case and the pooled-connection inheritance case |
| Deployed-boundary verification | anonymous list returns only attested rows; `Total:` equals the attested count |

Bounded — roughly the size of the A-05 and D-08 pieces combined. Two of the three
riskiest unknowns are now closed by the experiments above.

## 6. Decisions required

1. **Which corpus route** — (a), (b), (c) or (d). This is the real decision;
   everything else follows from it.
2. **How many subjects should the demo show**, which under (c) is simply how many
   you choose to generate and attest.
3. **Whether `/home-index` is exempted** — it returns only aggregates and was
   verified to contain no PII, so it could be made public independently of all of
   this and would restore the marketing homepage on its own.
4. **Whether `/order-chat` is in scope** — it discloses `contact_id`,
   `account_id`, `email`, `phone`, `account_name`. Under the gate it would be
   filtered like the rest, but only if orders are joined to attested subjects;
   that needs its own predicate and is not covered by an RLS policy on the three
   subject tables.

Item 4 is the one most likely to be missed. It was missed once already.

## 7. Recommendation

**Do not rebuild public-read as a general posture.** Take route (c): a curated,
attested demo subset behind the RLS gate, and turn public-read back on only when
the deployed-boundary probe shows anonymous callers receiving *exactly* the
attested set and nothing else.

If the marketing homepage is the actual need rather than the full CRM demo,
**decision 3 alone may be sufficient and costs almost nothing**: `/home-index` is
aggregates-only, exposes no subject, and would restore the live dashboard without
reopening any part of P-01.

## 8. No-change conclusion

No production code, schema, configuration, guard, data or deployment was changed.
The single experiment ran on the **local** database and its cleanup is verified:
`policies on contacts=0`, `relrowsecurity=False`. Production remains `locked`;
P-01 remains closed.
