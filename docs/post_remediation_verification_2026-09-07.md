# Post-Remediation Verification Report — Stage: P0 Closure

**2026-09-07.** Baseline: `docs/architecture_reassessment_2026-09-07.md`
(N-01…N-12, R-1…R-6, D-01…D-22).

**Scope of this stage:** Phase 0–1 of the remediation mandate (the two P0
governance bypasses), plus the two cheapest Phase-4 items that protect them
(CI gating, and the tooling for the orphaned-event decision).

> **STATUS: IMPLEMENTED AND TESTED LOCALLY. NOT DEPLOYED. NOT PRODUCTION
> VERIFIED.**
>
> The mandate's standard is IMPLEMENTED → TESTED → DEPLOYED → PRODUCTION
> VERIFIED. This stage has reached step 2. Nothing below may be called closed
> until §6 and §9 are done by a person.

---

## 1. Human decisions taken, and by whom

Four decisions were put to the owner before implementation (mandate §25) and
answered on 2026-09-07:

| # | Decision | Answer | Where it lands |
|---|---|---|---|
| 1 | Email decision architecture | **Confirmation page + identity-bound token** (both) | §2 |
| 2 | Production posture (D-01) | **Lock + separate synthetic demo** | NOT STARTED — §7 |
| 3 | The 18 shipped orders | **Classify first, then decide** | §5 — classifier built, decision still open |
| 4 | `policy.widen` approver | **CEO** | §3, encoded in the migration |

No other decision was made on the owner's behalf. Decisions 2 and 3 remain open
and are listed in §7.

---

## 2. N-01 — the email approval bypass · CLOSED (local)

**What was wrong.** `GET /governance/decide?g=…&a=approve&t=…` was an
unauthenticated GET that executed the governed action immediately. Its token was
`HMAC(secret, "uuid:action")` — proof of possessing a URL, with no executive, no
expiry and no issuance. Every governance mail carrying one was BCC'd to the
shared `info@agentorc.ca` archive, and the escalation reminder re-sent the same
eternal token every 24 hours without bound. It was the channel making 100% of
production decisions, each recorded `decided_actor = NULL`.

**What changed.**

| Property | Before | After |
|---|---|---|
| Token binds | `uuid : action` | `uuid : action : executive_id : nonce` |
| Executive in URL | — | `&e=<executive_id>`, covered by the HMAC |
| GET | **executed the action** | renders a confirmation page; mutates nothing |
| Decide | — | `POST /governance/decide`, from the form |
| Expiry | none | `GOV_LINK_TTL_HOURS`, default 72, measured from the mint |
| Re-mint on reminder | same token forever | new nonce; the previous link dies |
| Eligibility re-check | **skipped entirely** | same check the session path makes |
| Authority check | `if via == "email-link": return ok` | the resolved executive goes through `_authority_check` like a signed-in one |
| Archive | BCC to shared `info@` | `bcc=NO_BCC` on every governance send |
| Audit | `decided_by='email-link'`, actor NULL | `decided_by` and `decided_actor` = the executive's email; `decided_via` still `email-link` |
| On decision | nonce left live | `clear_decision_links()` retires the issuance |

**The GET/POST split is the load-bearing change.** It needs no allow-list of
user agents: a scanner, prefetcher, unfurler or crawler follows links and does
not submit forms.

**Deliberately NOT done — and stated rather than glossed:** this still does not
prove possession of a *mailbox*. A token in the right inbox still decides. What
changed is that it decides **as a named person, within a window, for one
issuance, only while that person is an eligible authority for that row** — so
the audit answers "who", exposure is time-bounded, and revocation works.

**Historical rows were not touched.** The six production decisions recorded as
`email-link` with a NULL actor stay exactly as they are. Manufacturing
attribution for them would be the defect this stage exists to close.

**Files:** `app/core/governance.py`, `app/core/governance_policy.py`,
`app/agents/email/smtp_imap.py`, `governance/sql/governance_decision_link_identity.sql`.

### Tests (all passing)

Rewritten to the new contract — including `test_the_email_link_records_the_token_not_a_person`,
which asserted the *old* behaviour and was correct to at the time; it is now
`test_the_email_link_now_records_the_PERSON_who_clicked`. Added:

- `test_a_GET_never_decides_anything` — GET returns a form, row stays pending, nothing executes
- `test_one_executives_link_cannot_decide_another_executives_row` — CFO token vs CRO row → 403
- `test_renaming_the_executive_in_the_url_breaks_the_signature`
- `test_a_superseded_issuance_stops_working` — re-mint rotates; the old link 403s
- `test_an_expired_link_is_refused`
- `test_a_link_with_no_executive_is_refused` — every pre-change link fails closed
- `test_an_ineligible_executive_cannot_decide_by_link` — revoked executive
- `test_a_one_click_link_cannot_be_replayed`, `test_a_forged_one_click_token_is_refused`,
  `test_a_token_for_one_action_does_not_authorise_the_other`
- `test_a_decided_row_keeps_no_live_link`
- `test_governance_mail_is_never_archived_to_the_shared_mailbox` — a **source**
  assertion over every `send_email(` call site in both governance modules,
  because the failure was a *default being taken*, which no behavioural test sees

---

## 3. N-02 — governance governing governance · CLOSED (local)

**What was wrong.** Deciding one approval was bound to the signed-in executive.
Setting the policy that decides whether a human is needed *at all* was not: 13 of
16 mutating governance endpoints accepted the ops token, and
`PUT /governance/action-policies/{action_type}` took `updated_by` **from the
request body**.

**What changed.**

**a) Identity binding.** `bound_authority(request)` now lives in
`governance_policy.py` and is shared by all three governance modules — one
implementation, because three copies is three places for one to be forgotten,
which is the exact shape of this defect.

| | Before | After |
|---|---|---|
| Mutating governance endpoints bound to an executive | **3 of 16** | **10 of 17** |

The 7 unbound are each justified, not merely left: `/governance/sla-sweep`,
`/governance/alerts/sweep`, `/governance/expire` are the **scheduler's own jobs**
and decide nothing (binding them would mean the scheduler could not run them);
`/governance/critique` ×2 record a critic *opinion*; `/governance/alerts` *raises*
a signal rather than clearing one; `/governance/decide` is public by design and
token-bound. Verified by `scratchpad/audit_binding.py`, which parses the AST —
the first regex version reported `history/delete` as bound when it was not,
because a helper function between two endpoints fell inside its window.

Newly bound: `action-policies` PUT, `policies` PUT + DELETE, `undo`,
`alerts/{id}/resolve|close`, `history/delete`, `renotify`.

`renotify` is bound for a reason N-01 created: it now **re-mints** links, so an
unbound caller could repeatedly kill every outstanding link — a denial of service
against the decision path, delivered as a helpful reminder.

**b) `updated_by` is inert.** Kept in the request models so existing callers get
their change applied rather than a 422 they cannot interpret, and named in the
code as inert so the next reader is not left wondering. The actor is the session.

**c) Widening is a governed decision.** A change that **weakens** a control
becomes a `policy.widen` proposal (CEO, HUMAN_APPROVAL, `owner_required`) and
**changes nothing** until decided. HTTP **202**, not 403 — nothing was refused;
the change is in flight.

Detected as widening: `HUMAN_APPROVAL → SAMPLED_REVIEW/AUTO_EXECUTE`,
`auto_execute false → true`, longer `sla_hours`, lower `sample_rate`,
`delegation_allowed false → true`, `status active → anything`; and for the
tunables: raising `gov.hitl_amount` or `brand.max_discount_pct`, lowering
`gov.act_min` or `gov.propose_min`, raising `planner.max_steps`/`max_writes`.

**Narrowing applies directly.** A gate that also taxes the safe direction makes
the safe direction the expensive one.

**d) `governance_policy_changes`** — append-only, enforced by trigger. Records
the authenticated actor, represented authority, field, **value before and after**,
whether it weakened the control, the reason, and a `CHECK` that a widening record
**names the approval that authorised it**.

**e) The recursion is closed.** `test_policy_widen_cannot_widen_itself`:
`policy.widen` cannot be made to auto-execute without a `policy.widen` approval.

**Files:** `app/core/governance_policy.py`, `app/core/governance.py`,
`app/core/governance_alerts.py`, `app/core/a2a.py` (the `policy.widen`
capability), the migration.

### Tests — `governance/tests/test_policy_governance.py`, 29 new, all passing

Machine token refused · admin-session-that-is-not-an-executive refused ·
body-supplied `updated_by` cannot establish attribution (CFO signs in, body
claims CEO, record says CFO) · each widening direction detected · each narrowing
direction not · widening creates a proposal and **changes nothing** · the
proposal names an accountable owner · `set_policy` refuses widening even when
called directly, not only through HTTP · money-floor and discount-ceiling cases ·
the audit record answers all of §10's questions · the record cannot be edited or
deleted (**and the refusal names the constraint**) · a widening record cannot
exist without its approval · `policy.widen` cannot widen itself · no write
capability is left undeclared.

---

## 4. Regression Register for this stage (mandate §21)

| Fix | Invariant protected | New risk introduced | Test | Production verification |
|---|---|---|---|---|
| Identity-bound token | decisions name a human | A link minted for exec A cannot be used by exec B — if routing assigns the wrong executive, **nobody can decide by email** and the row waits for the console. Chosen deliberately: failing closed on a decision is safer than failing open. | `test_one_executives_link_cannot_decide_another_executives_row` | `decisions_30d_by_decider` names people |
| GET/POST split | a machine cannot decide | One extra click. A mail client that strips forms shows a dead page — the console remains. | `test_a_GET_never_decides_anything` | a decision arrives with a named actor |
| Re-mint on reminder | old links die | An executive holding reminder *n−1* finds it dead; the email says so explicitly. | `test_a_superseded_issuance_stops_working` | reminders decided, not ignored |
| 72h expiry | bounded exposure | A row pending > 72h has no live link until the next reminder (24h cadence, so ≤ 24h uncovered). | `test_an_expired_link_is_refused` | no rise in undecided rows |
| `NO_BCC` on governance mail | no live token in a shared inbox | **The `info@` archive no longer holds governance mail**, so that record is thinner. Accepted: an archive of authorisation tokens is not an audit trail. `governance_policy_changes` + `action_approvals` are the record. | source-level call-site test | info@ shows no new decision links |
| `bound_authority` on 7 more endpoints | policy cannot be weakened by a machine | Any automation calling these with an ops token **now breaks with a 403**. None found in-repo; an external caller would be a runtime surprise. | the identity tests | watch for 403s after deploy |
| `policy.widen` | weakening requires the CEO | A **new dependency in the emergency path**: tightening still applies instantly, but loosening a policy during an incident now needs a decision. This is the intended trade and it should be understood before an incident, not during one. | `test_widening_creates_a_proposal_and_changes_nothing` | first widening reaches the CEO |
| Append-only `governance_policy_changes` | the record cannot be edited | A test **cannot clean up its own rows** — the fixture had to stop trying. Correct: a table a fixture can tidy is not append-only. | `test_the_change_record_cannot_be_edited_or_deleted` | — |
| Gating 4 governance suites in CI | a control ships with a gated test | CI now fails on governance regressions; it will be **slower and redder**, which is the point. | — | first CI run after merge |

**Two census pins converted or updated, deliberately:**

- `test_45_capabilities_without_a_schema_are_reported_not_hidden` was
  `assert len(writes) == 16` and broke because the population grew. **Converted
  to a ratchet**: a frozen legacy set of schema-less writes that may shrink and
  never grow, so a *new* write capability without a parameter contract fails.
  This is the antipattern the codebase already knows about; the pin was replaced
  rather than re-pinned.
- The two SQL-disposition counts correctly detected the new migration file and
  were updated to 234 / 280 **with the reason recorded**, and `declared` was
  deliberately **not** moved — see §6.

---

## 5. The 18 shipped orders — classified, NOT actioned

Per decision 3, nothing was sent. `scripts/classify_orphaned_events.py` is new,
**read-only** (read-only transaction, no writes, no sends), and sorts every
orphaned event into REPLAY / REVIEW / WRITE_OFF **with the reason**, checking:
does an accepted notification already exist; is the order now cancelled/refunded/
returned (which would make the message *false*, not merely late); does the order's
current status agree with the event.

Run against **local**, where the same class of backlog exists:

```
orphaned events: 326   REPLAY 149 · REVIEW 5 · WRITE_OFF 172
```

**That result is itself the argument for the decision the owner made.** More than
half would have been wrong or duplicate. A blind `POST /agent-bus/drain` — the
remedy the alert itself recommends — would have sent them.

**HUMAN ACTION REQUIRED.** The 39 events that matter are on Railway, and direct
Railway SQL is blocked in this environment, so I could not classify *those*
rows. Run:

```
python -m scripts.classify_orphaned_events --target railway --json orphans.json
```

then decide per bucket. The `event_orphaned` alert (owner CTO Bill Wang) was
**4.3 hours from breaching its 24h SLA** when measured at 13:47 UTC on
2026-09-07; it has very likely breached and escalated to the CEO by now.

---

## 6. Migration state — the human merge point

`governance/sql/governance_decision_link_identity.sql` is **applied to LOCAL
only** and classified `OUT_OF_BAND_SQL` with the **PENDING DEPLOYMENT** marker —
the middle state this codebase already names:

```
1. authored + classified, applied locally   -> PENDING DEPLOYMENT   ← we are here
2. applied to production                    -> verified directly
3. moved into REQUIRED_MIGRATIONS           -> claimed as run
```

**It was deliberately NOT added to `REQUIRED_MIGRATIONS`.** That list is a claim
about what *production* has executed and `migrate --check` reads it as one.
Entering it now would be the verifier asserting a state production is not in.
Promote it in the same change that records its Railway application — exactly as
the four activation files were on 2026-09-06.

**Deploy order — every file, in sequence** (one file, but stated in full because
a previous handover named one migration of five and the app shipped ahead of its
schema):

1. `governance/sql/governance_decision_link_identity.sql` — apply to Railway
2. then deploy the application
3. then promote the file to `REQUIRED_MIGRATIONS` and update the two
   SQL-disposition counts by one in the same commit

**Order matters here.** The app calls `mint_decision_links`, which writes
`decision_link_nonce`. Deploying the code first means every governance email
fails to mint and goes out with no link at all until the migration lands.

`governance/` is the separate private repository: push it and update
`.governance-pin` before the public repo.

---

## 7. Findings: closed, partially closed, remaining

**Closed locally (not yet in production):** N-01, N-02, N-05 (the two
escalation-path `logger.debug` sites are now `WARNING`), N-09 (four governance
suites added to `verify_gate.py::CONTROL_TESTS`).

**Partially closed:** D-04 — the classifier exists; the events are still
orphaned. R-3 — the unbounded-token half is closed by re-minting; the *expiry
vs escalation* policy question is unchanged.

**Not started this stage** (mandate §11–§19, all still open): D-01 production
posture (decision taken: lock + synthetic demo), D-02 business-object ownership,
R-1 the 21 order-notification tests, R-2 the hybrid kill-switch contract, R-6 the
174-item work surface, D-10 migration truth, D-05 audit model, D-12/D-18
retrieval governance, D-08 the superuser DSN, N-08 alert quality, N-11 owner-id
portability.

---

## 8. Test results

| | Before this stage | After |
|---|---|---|
| Passing | 2,891 | **2,930** (+39) |
| Failing | 25 | **25** (unchanged) |
| Skipped | 2 | 2 |

**No new test failures.** All 25 remaining failures are the pre-existing R-1
(23 — the `is_deployed` guard below the test seam), R-2 (1) and R-6 (1), which
are Phase-4 work and were not touched.

`python -m scripts.verify_invariants` — **all invariants hold** (one standing
TODO: the local application DSN still names an owner account, D-08).

---

## 9. Production verification — NOT DONE

| Control | Local | CI | Deployed | Production | Evidence |
|---|---|---|---|---|---|
| Identity-bound decision link | ✅ | pending merge | ❌ | ❌ | `decisions_30d_by_decider` naming people |
| GET does not mutate | ✅ | pending merge | ❌ | ❌ | GET the confirm URL, row stays pending |
| Governance mail not archived | ✅ (source) | pending merge | ❌ | ❌ | no new decision link in `info@` |
| Policy mutation bound | ✅ | pending merge | ❌ | ❌ | ops-token PUT → 403 |
| `policy.widen` | ✅ | pending merge | ❌ | ❌ | a widening attempt reaches the CEO |
| Orphaned events resolved | ❌ | — | ❌ | ❌ | alert closed with closure evidence |

**Nothing in this stage may be described as closed until this table is green.**

---

## 10. Rollback

- **Schema** — additive only (three nullable columns, one new table, one policy
  row). Rolling back the code leaves them unused; no rollback SQL needed.
- **N-01** — the previous behaviour is not restorable by a flag, deliberately.
  Reverting means reverting the commit. Old-format links already fail closed
  (`test_a_link_with_no_executive_is_refused`), so a revert would re-open the
  bypass rather than restore service.
- **N-02** — reverting `bound_authority` on an endpoint restores machine access
  to it. `policy.widen` can be neutralised by setting its policy to
  `AUTO_EXECUTE` — **which itself now requires a `policy.widen` approval**. That
  is intentional and is the one place a rollback needs a human.
- **BCC** — `bcc=NO_BCC` reverts to `bcc=None` per call site to restore archiving.

---

## 11. Human actions required

1. **Apply the migration to Railway, then deploy, then promote** (§6, in order).
2. **Classify and decide the 39 orphaned events** (§5) — the CTO's alert has
   likely breached.
3. **Decide the production posture** (D-01). The decision is made
   (lock + synthetic demo); the work is not started, and the corpus is
   unprovenanced, so the demo needs a *fresh* synthetic corpus.
4. **Rotate `GOV_LINK_SECRET`** — now hygiene rather than an emergency: every
   pre-change link carries no executive and is refused by the first check.
5. **Remove the production superuser DSN from the laptop** (D-08 — standing
   since 2026-09-05).
6. **Push `governance/` and update `.governance-pin`** before the public repo.

---

## 12. Readiness verdict for this stage

> **The two P0 architectural bypasses are closed in code and covered by tests
> that fail without the fix. The stage is NOT complete: it is not deployed, not
> production-verified, and the operational failure that motivated the whole
> reassessment — 18 customers who were never told their orders shipped — is
> still true today.**

The overall verdict of the reassessment stands unchanged and is not softened by
this stage:

> **GOVERNED AI-AGENT CRM — NOT YET WORLD-CLASS**

What changed is which sentence blocks it. It is no longer "an unauthenticated
GET can execute a governed action" or "an administrator can abolish approval for
an action class". It is now, in order: nothing is deployed; the production
posture still serves contact PII to anonymous callers; 97.3% of orders have no
accountable owner; and 18 customers are still waiting.


---

## 13. Classification of the 25 remaining test failures

Requested by the owner (deployment mandate §3). **None was made green by
weakening an invariant**; three are recorded as needing a human decision because
resolving them either way changes real behaviour.

| # | Test(s) | Root cause | Class | Blocks deploy? |
|---|---|---|---|---|
| 1 | `test_order_lifecycle_notifications.py` — **21 tests** | `release_guard.is_deployed()` returns False on a laptop, so `order_notifications.notify()` returns `queued` where the tests assert `accepted`. The guard was added to stop the laptop becoming a second live sender (the email-quota-leak fix) and sits **below the test seam**. | **ENVIRONMENTAL** | **No** — but see the risk below |
| 2 | `test_email_send_sp.py::test_the_seed_catchall_is_deliverable_when_verified` | Two deliberate decisions collide. The test pins "`@seed.agentorc.ca` is NOT filtered — verification is the gate". The code later added `seed.agentorc.ca` to `_PLACEHOLDER_EMAIL_DOMAINS` because **112 of 129 seed contacts were flagged verified in production**, so verification was *not* in fact the gate and the domain set was the only thing between synthetic rows and the provider. | **INTENTIONAL / OBSOLETE** + **DECISION REQUIRED** | **No** |
| 3 | `test_email_send_sp.py::test_the_recipient_is_addressed_by_name` | Same domain change, plus a test double whose cursor has no `.description`, which makes `authorities()` fail and raises `GovernanceConfigError`. Two causes in one test. | **TEST DEFECT** (the double) + consequence of #2 | **No** |
| 4 | `test_hybrid_retrieval.py::test_recency_only_output_is_frozen` | Pinned `{2, 4, 1, 1}`; observed `{2, 2, 1, 1}`. **Three of four still match** — only `payment reminder` dropped 4 → 2. That is not the widened-budget failure the test was written to catch (which would raise all four toward 5); it is two rows no longer matching one query on a local corpus that months of test runs have mutated. | **ENVIRONMENTAL** *(probable)* | **No** |
| 5 | `test_work_ownership.py::test_K_H_closed_history_is_not_resurrected_as_work` | `unassigned_work()` returns **177 open unassigned items** against a pinned `< 100`. The test's own docstring says "~5 are open". The live unassigned surface has grown **~35×**. | **REAL DEFECT — in the data, not the code** | **No**, but it is D-02 surfacing |

### The three that need a decision or further work

**#1 — the 21 order-notification tests (R-1).** The remedy is a **test-scoped
deployment override** so the subsystem can be exercised without pretending to be
deployed, then each of the 21 diagnosed on its own merits. **Do not** relax the
assertion from `accepted` to `queued`: that would make the suite pass by
agreeing that notifications are not sent, which is the exact business failure
still open in production. **RISK:** until this is fixed, the subsystem behind the
18 unnotified shipped orders has no working local verification — the deployment
does not depend on it, and the confidence in that subsystem does.

**#2 — the seed catch-all. DECISION REQUIRED.** Both positions are reasoned and
the code's is the later and better-informed one. Resolving it either way changes
behaviour:

- *Update the test* (recommended): confirms the seed domain is blocked, and
  records that the pin was superseded by measured production evidence. **Cost:**
  the pin protected "the only end-to-end send test that does not involve a real
  customer" — that capability needs a replacement subject (a single real,
  owned mailbox on a non-seed domain), or it is lost quietly, which is what the
  pin existed to prevent.
- *Update the code*: re-opens the quota leak in which ~91% of real send volume
  went to an address we invented.

**#5 — 177 unassigned open work items.** The failing assertion is **telling the
truth**. This is D-02 (97.3% of orders unowned, `owners` 87% customer contacts)
appearing in a control rather than in a report. Two separate actions: the
*business* one is Phase 2 ownership work; the *test* one is that `< 100` is
another census pin — it should measure the property (is the surface growing? is
each item eligible-owned?) rather than compare a live population to a constant.
That is the same defect class as `assert len(writes) == 16`, already converted
in this stage.

### What this classification does not claim

**FACT:** the failure count is unchanged at 25 and no new failure was introduced.
**NOT ESTABLISHED:** that these 25 are acceptable indefinitely. #1 and #5 are
real gaps with named owners' work behind them; #2 is a live decision; #4 is
probable-not-proven, and the discriminator is whether the two `payment reminder`
rows still exist in the local index — a check, not a guess.
