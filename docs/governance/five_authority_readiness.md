# Five-Authority Governance — Implementation Readiness Report

**2026-09-05.** Independent re-verification of the work in this stage, measured against
the repository, both databases and a running instance of the current code. Nothing has
been committed, pushed, deployed or applied to Railway.

Companion documents: `activation_plan.md` (the model),
`executive_identity_activation.md` (the identity procedure),
`railway_governance_verification.md` (the deployment gate).

---

## 1. Verdict

**Governance is implemented, tested, and locally OPERATIONAL. All five authorities can
sign in as themselves and decide. The only remaining step is deployment.**

Updated 2026-09-06 09:40. Every executive holds an individual credential, has set their own
password, and is refused platform administration. The CTO gap is closed, the COO identity is
reconciled, and `/governance/authorities` reports an empty `missing`, `without_credential`
and `identity_mismatch` for the first time.

**What this changes.** Governance stops being a mechanism that exists and becomes one that
operates. A decision taken in the console is now bound to an authenticated session and
attributed to a named person — the thing the twelve historical
`decided_by = admin@conscestra.local` approvals could never do. Two proposals are already
queued on the CRO's desk, and no pending item carries `ownership_exception`.

Two facts decide everything else:

1. ~~**There is one credential in the database and it belongs to nobody in particular.**~~
   **CLOSED 2026-09-05.** Four individual credentials now exist — CEO, CRO, CFO and CTO,
   each on their own address, each `access_role='viewer'`, none admin. Verified live: an
   executive session reaches `/governance/whoami` with `can_decide=true` and is refused
   403 on five separate admin surfaces. The passwords were generated at random and
   discarded unread, so each executive sets their own.

   It is also not a hypothetical: **twelve historical approvals record
   `decided_by = admin@conscestra.local` and nothing else** — an executed KB publication,
   a phone-normalisation run, a win-back campaign and a dunning emission among them. Which
   human authorised any of those cannot be recovered. That is the gap this model closes,
   and it closes it going forward only; §7 explains why the twelve stay as they are.

   Using one shared account for all five authorities would reproduce the gap exactly,
   and add a second one: revocation would be all-or-nothing. The remedy is five
   individual credentials, and under the governance gate none of them needs admin.
2. ~~**The CTO has no owner identity.**~~ **CLOSED 2026-09-05.** Membership, owner row
   (`585f003c…`) and credential all granted under the CEO's written authorisation, and
   the CEO attested that Bill Wang is a real person — recorded in `corpus_provenance`,
   which moved `eligible_production` from 0 to 1. The eleven policy classes route to the
   CTO, and the three controls that were failing truthfully now pass **unmodified**.

3. ~~**The COO office changed hands.**~~ **CORRECTED AND CLOSED 2026-09-06.** It did
   not. Alex Zhou and Yongmei Qin are the same person; the rename had been applied to
   `executives.full_name` and stopped there, leaving the owner row and the directory
   membership on the old name. Reconciled in place: one identity, the same uuid, and the
   48 activities and 2 opportunities never moved. `owners` and directory counts are
   unchanged at 45 and 5, which is the evidence that this was a rename and not a
   succession. The fifth credential followed.

   The guard refused the ambiguous command before any of this, naming the 50 records it
   would have reattributed. That refusal is what turned an assumption into a question.

4. **An unauthenticated password-reset token disclosure is live on the deployed host.**
   Found while verifying the new credentials. **Now fully fixed in code** — both the
   disclosure and the missing delivery that closing it would otherwise have exposed. See
   §12. **DEPLOYED 2026-09-06** (commit `bfbfac7a37b9`) and verified live: a real account
   and an invented one return byte-identical replies with no token.

Everything else in this stage verified. **Every action class is now decided** — the owner
settled `kb.publish` on 2026-09-05 (CRO, human approval, three a day), and
`decision_required` is empty.

## 2. Evidence matrix

Status vocabulary: **VERIFIED** (measured now) · **PARTIAL** · **BLOCKED** (on a human
act) · **NOT DEPLOYED**.

| # | Control | Status | Evidence (2026-09-05) | Remaining gap |
|---|---|---|---|---|
| 1 | Five authorities admissible | VERIFIED | DB CHECKs list CEO/CRO/CFO/CTO/COO on `approver_role`, `escalation_role` (both tables); a sixth role is refused by the database | — |
| 2 | Technology + data → CTO | VERIFIED | 5 action policies + 6 alert rules, all `active`; `data.erase_record` deliberately stays CEO | CTO identity (#9) |
| 3 | Operations → COO | VERIFIED | 3 alert rules, `active` | — |
| 4 | Policy versioned with history | VERIFIED | 16 rows written by `owner-decision-2026-09-05`; history is append-only (delete refused) | — |
| 5 | Confidence grants no authority | VERIFIED | `/governance/status.confidence_grants_authority=false`; test proves `act_min=0.0` still proposes | — |
| 6 | Decision bound to session | VERIFIED live | machine token: `whoami.can_decide=false`, approve → **403**, row stays pending | — |
| 7 | Body cannot choose identity | VERIFIED | `_Decision.decided_by` unused by the endpoint; test asserts it by source inspection | — |
| 8 | Acting person recorded | VERIFIED | `decided_actor` written on approve, reject and the email link; test asserts all three | — |
| 9 | Owner eligibility enforced | PARTIAL | trigger refuses a customer-contact owner (constraint named in the test); **CTO ineligible** | identity grant |
| 10 | Governance ≠ administration | VERIFIED live | an executive session with `role='viewer'` reached `whoami` and `my-approvals`, and was **403 on `/deploy/migrations`** | credentials (#20) |
| 11 | Atomic approval | VERIFIED | six concurrent approvals → one execution; approve/reject, approve/delegate and delegate/delegate races each leave one coherent outcome | — |
| 12 | Stranded execution recovered | VERIFIED | lease sweep flips to `failed` and opens an owned alert | — |
| 13 | 48h SLA durable | VERIFIED | `due_at` written at proposal, backfilled for live rows, independent of process memory | — |
| 14 | Breach ≠ decision | VERIFIED | breached row stays `pending`; nothing has been set to `expired` by the new code | the old local server still runs the retired job (#22) |
| 15 | CEO escalation | VERIFIED | breach stamps `escalated_at`, notifies both, opens a CEO-owned `approval_breach` alert | — |
| 16 | Escalation email | VERIFIED (code + ledger) | ledgered per (approval, role); sender declared in the allowlist, without which `release_guard` refuses to start | delivery evidence needs the info@ archive after deploy |
| 17 | Escalation repeats | VERIFIED | reminder pass numbers and stamps each notice; immediate re-run reminds nothing; only a decision stops it | — |
| 18 | One-click link safe | VERIFIED | replay says "Already decided" and does not execute twice; a forged token and a cross-action token are both refused |— |
| 19 | Orphaned events owned | VERIFIED | durable `orphaned` state, CEO-owned alert, audited replay | 39 rows waiting on Railway |
| 20 | Executive can authenticate | **VERIFIED (local)** | 4 individual credentials, all `viewer`; a live round trip signed in and reached `can_decide=true`; refused 403 on 5 admin surfaces | §5.1 |
| 21 | CTO identity | **VERIFIED (local)** | membership + owner `585f003c…` + credential + `fn_owner_eligible` true; CEO-attested real, the corpus's only `eligible_production` owner | §5.2 |
| 21b | COO identity | **VERIFIED (local)** | rename reconciled across owner + directory + authority on one uuid; 50 records untouched; credential issued; live round trip signed in as Alex Zhou with `can_decide=true` and 403 on five admin surfaces | §5.3 |
| 31b | Authority names the person it decides as | **VERIFIED (detector)** | `identity_mismatch` on every authority row; reports the COO divergence today and nothing else | §5.3 |
| 32 | Reset-token disclosure | **CLOSED (verified in production)** | was: anonymous callers received a working token for any real account. Now: identical replies, no token, verified against the deployed host 2026-09-06 | §12 |
| 23 | Every action class decided | VERIFIED | `decision_required` is empty; `kb.publish` v2 → CRO, HUMAN_APPROVAL, cap 3, history records CEO→CRO | — |
| 24 | Daily cap enforced and visible | VERIFIED | `propose()` raises `ProposalCapReached`; six producers defer; `a2a.dispatch` returns structured `REJECTED`; first refusal each day opens a CRO-owned work item | — |
| 25 | Executive reaches governance without admin | VERIFIED live | a CEO session with `role='viewer'` returned 200 on all 12 governance and audit endpoints | credentials (#20) |
| 26 | Executive gains no admin surface | VERIFIED live | the same session: 403 on `/deploy/migrations`, `/platform/health`, `/agent-bus/status`, `/a2a/registry`, `/simulate` | — |
| 27 | **Platform admin cannot decide** | VERIFIED live | an `admin@conscestra.local` session reached every governance READ and was **403 on reject** | — |
| 28 | Machine token cannot decide | VERIFIED live | ops token: reads 200, decide 403 | — |
| 29 | One identity resolves to one authority | VERIFIED | parametrised over all five; the CTO case skips honestly because it is not yet eligible | CTO identity |
| 30 | Independent revocation | VERIFIED | deactivating one executive removes exactly that authority; a live session for the revoked executive is refused at the **next request**, not at the next restart | — |
| 31 | Historical attribution preserved | VERIFIED | all 288 decided rows are pre-activation (`decided_actor IS NULL`); a test fails if any admin-attributed row ever acquires an actor | — |
| 22 | Deployed | **DEPLOYED 2026-09-06** | commit `bfbfac7a37b9`; all five SQL files applied to Railway; 37 action policies, 4 of 5 authorities eligible, 10 of 10 approval columns present | §5.8 |
| 22b | Executive sign-ins on Railway | **NOT PRESENT** | 5 authority rows, 0 executive credentials (1 platform admin). Nobody can decide in the production console yet | §5.9 |
| 22c | CTO owner identity on Railway | **NOT PRESENT** | `fn_owner_eligible` is false for the CTO there; its classes route to the CEO as ownership exceptions, exactly as they did locally before the grant | §3 |

## 3. Test evidence

| Suite | Result |
|---|---|
| Full governance suite, 2026-09-06 (4 documented pre-existing failures deselected) | **2,713 passed, 2 skipped, 0 failed** |
| `test_reset_token_disclosure.py` (new) | **17 passed**; mutation-checked twice — restoring the unconditional return fails 10 of 11 on the gate, and removing delivery fails 3 more |
| `test_executive_identity_mismatch.py` (new) | **9 passed** — tests the detector, not today's data, so it survives the COO being fixed |

The three CTO-gap failures (`test_assignable_identity::test_31`,
`test_work_ownership::test_J0`/`::test_J1`) now pass **without having been modified**.
They were the gap's alarm and they went quiet when the gap closed, which is the only
acceptable way for an alarm to stop.

**Five tests went red as a consequence of the grant, and none was silenced.** Four were
census pins written as `== 4` — the directory's size on the day they were written — and
the authorised fifth identity broke them. Three are now derived from the directory, since
"routing recommends everybody eligible" was always the claim and the number never was; two
remain explicit counts with the reason recorded beside them.

The fifth was not a pin and mattered more. `assignable.environment()` decided whether this
is a real staffed organisation by comparing the linked count to the constant `4`. Adding
the CTO made five greater than four, so the system began reporting a real staffed
organisation on the strength of one row — and `case-mgmt.html` renders its warning banner
`if (env.synthetic)`, so the "Synthetic organisation" notice silently disappeared from the
UI. A control whose stated purpose is stopping the product looking staffed had switched
itself off by being staffed one person further. It now measures attestation instead:
synthetic while **any** authorised identity is unvouched-for, which no amount of adding
people can clear. Today it reports 1 of 5 attested, and the banner is back.

New adversarial coverage in this stage: approve/reject, approve/delegate and
delegate/delegate races; one-click replay, forgery and cross-action token reuse; a legacy
`expired` row can never execute; an escalated proposal stays decidable by both authorities;
proposal creation fails closed when no eligible owner exists; the governance gate admits an
executive and refuses a non-executive viewer; the admin gate still stamps the session that
bound identity depends on; reminder cadence fires after the window, once, and stops on a
decision. Cap coverage: `kb.publish` is owned by the CRO with a cap of three; the cap
refuses rather than dropping; hitting it opens exactly one owned work item per class per
day, not one per refusal; an uncapped class is unaffected; a cap cannot be placed on an
AUTO_EXECUTE class (the database refuses it); a capped dispatch is a structured `REJECTED`
result; and a structural control enumerates every producer that can reach a capped class,
so the next one somebody writes has to handle it.

## 4. Changes in this stage

| Area | Change |
|---|---|
| `governance/sql/governance_five_authorities.sql` | five-role CHECKs, class routing to CTO/COO, confirms the two standing policies, `decided_actor` |
| `governance/sql/governance_reescalation.sql` | `escalation_notices` + `last_escalation_notice_at` on approvals and alerts, with indexes |
| `app/core/governance_policy.py` | five roles; `has_credential`; `session_authority`; `executive_for_identifier`; `email_authority`; `GET /governance/whoami` |
| `app/core/governance.py` | `_bound_authority`; endpoints take identity from the session; `decided_actor` threaded; breach emails both authorities; reminder pass in `sla_sweep` |
| `app/core/governance_alerts.py` | escalation email; `remind_escalated` |
| `app/core/auth_dep.py` | `require_governance_actor` — admin **or** an active executive, for the governance routers only |
| `app/main.py` | governance routers move from `_ADMIN` to `_GOVERNANCE` |
| `app/core/email_call_sites.py` | the new sender declared, with its reason |
| `governance/sql/governance_kb_publish_policy.sql` | the owner's `kb.publish` decision, and `daily_proposal_cap` with the two CHECKs that keep it meaningful |
| `app/core/governance.py` | + `ProposalCapReached`, the cap check inside the insert, and the once-a-day refusal work item |
| producers made cap-aware | `knowledge` (miner + gap pass), `kb_ingest`, `planner`, `data_quality`, `supervisor`, `agent_capabilities`; `a2a.dispatch` converts the refusal to a structured `REJECTED` |
| `governance-mgmt.html` | whoami bar; role buttons choose a desk, not an identity; decision controls disabled unless the session may decide; the policy table shows and edits the daily cap |

## 5. Human actions, in order

1. ~~Create five executive credentials~~ — **four done 2026-09-05**, all `viewer`, none
   admin. Each executive sets their own password via **Reset password** on `auth.html`.
   The fifth is withheld; see 3.
2. ~~Grant the CTO an identity~~ — **done 2026-09-05**, with the CEO's attestation
   recorded. 11 policy classes moved off the CEO's exception queue.
3. ~~Supply Alex Zhou's own email address~~ — **not needed; done 2026-09-06.** The
   premise was wrong: it is the same person under a new name, so the rename was applied
   in place with `--i-am-renaming-the-same-person` and the fifth credential issued. No
   record changed hands.
3b. **Decide whether to deploy the §12 authentication fix ahead of the migrations.**
   It is one endpoint and it closes a live takeover path on the deployed host.
3c. ~~Reconcile the COO name~~ — **fully done 2026-09-06.** The 2026-09-05 edit touched
   only `executives.full_name`; the owner row and directory membership kept the old name,
   and that half-applied rename is what the new `identity_mismatch` check detected.
4. ~~Decide `kb.publish`~~ — **done 2026-09-05**: CRO, human approval, 3 per day.
5. **Confirm the paging destination** beyond email — §7.
6. **Restart the local dev server on :8000.** It is running pre-activation code from
   13:45 and still holds the retired nightly `governance_expiry` job, which last ran at
   21:45 on 2026-09-04 and set seven proposals to `expired`. Until it restarts it will do
   so again tonight. Local only, and a precise illustration of why deployment is the gate.
7. **Rotate the Railway superuser credential** (carried over; assessment D-08).
8. **Authorise the Railway deployment**, then follow
   `railway_governance_verification.md`.

## 6. `kb.publish` — DECIDED 2026-09-05 (owner)

**CRO · HUMAN_APPROVAL · escalation CEO · 48h · daily cap 3 · reversible.** Policy v2;
the history records the move from the CEO fallback. Five pending items were re-routed to
the CRO, and no action class carries `decision_required` any more.

The cap is enforced in `propose()`, which raises `ProposalCapReached`. Every producer that
can reach a capped class now defers instead of aborting its pass — the nightly miner,
document ingest, the planner, data quality, the supervisor, and authored agents — and
`a2a.dispatch` returns a structured `REJECTED` result. The first refusal each day opens a
low-severity work item owned by the CRO, so a deferred backlog is visible rather than
inferred from an absence. A structural test enumerates that boundary, so the next producer
someone adds has to handle it.

The reasoning that led to the decision is kept below, because a decision without its
evidence is just an assertion.

### Reasoning (as presented before the decision)

**Measured:** 201 active public articles and 11 internal; 2,270 recorded uses. 18
approvals executed, 5 pending, **42 expired unactioned**. Decided by: `system` 42 (the
retired expiry), `email-link` 10, an admin 8.

- **Consequential: yes.** A published article enters the retrieval corpus that grounds
  what the AI tells customers on the storefront, the support line and the widget.
- **Changes AI behaviour: directly.** It is the behaviour.
- **Reversible: yes.** `_undo_kb_publish` retires the article inside the undo window.
- **Accountable for the content:** whoever owns what customers are told.

**Recommendation: `HUMAN_APPROVAL`, approver CRO, escalation CEO, 48h.** The article is
customer-facing commercial copy that the platform will assert as fact; the CRO owns that.
`SAMPLED_REVIEW` is wrong here at this maturity — it would auto-publish customer-facing
assertions and review a tenth of them afterwards, and the 42 expired proposals say the
review capacity is not yet demonstrated. The CTO is defensible for the *retrieval tier*
(public vs internal), which is already enforced separately by `reach_invariant`, but not
for the content.

Until you decide, the row stays `decision_required` with the CEO as approver and
`HUMAN_APPROVAL` — so the invariant already holds: no `kb.publish` executes without a named
policy and an accountable human. What is missing is your choice, not a control.

## 7. Paging beyond email — recommendation

Email is now real: sent to the breached authority and the CEO, ledgered, idempotent, and
**repeating every 24 hours until a decision exists**. That closes "silently ignored" for
the demo stage, and the semantics are explicit:

- **Acknowledgement of an approval is the decision.** There is deliberately no "seen"
  control — a reminder that can be dismissed without deciding rebuilds the silence.
- **Acknowledgement of an alert** pauses reminders (a human has picked it up); resolving
  and closing it, with evidence, ends the obligation.

**Minimum next step, when you want one:** point `ESCALATION_EMAIL_TO`-style delivery at an
SMS or push destination for `severity='critical'` and CEO escalations only, reusing
`email_authority`'s ledger shape so idempotency and the attention budget still apply. This
needs no new platform — the transports module already carries SMS. It is a delivery
choice, and it is yours (§15).

## 8. Controlled temporary conditions

Both are recorded rather than hidden, and each has a gate that ends it.

| Condition | Risk while it stands | Ends when |
|---|---|---|
| The three identity tests fail | none to production — they fail *because* the guard works | the CTO grant is done |
| `governance_action_policies` allows five roles while only four have an eligible owner | CTO work accumulates on the CEO's desk, counted as exceptions | the CTO grant is done |
| The old dev server keeps expiring local proposals | local data only; no production effect | that process is restarted |
| Executive credentials will be lead-backed | consistent with existing staff sign-in on this system; no `contacts` row is created, so eligibility is unaffected | a narrower staff identity model, if ever wanted |

## 9. Railway deployment readiness

Per the five areas required before deployment is considered. **Not ready**, and the
blockers are identity, not code.

### Identity

| Item | State |
|---|---|
| Five executive credentials | **0 of 5.** Only `admin@conscestra.local` exists |
| Five authority bindings | 4 of 5 eligible; **CTO not eligible** |
| Directory membership | 4 of 5; CTO absent from `assignable_identity` |
| Owner identities | 4 of 5; no CTO owner row |
| CTO identity | authority record on both databases; owner, membership and credential missing |
| COO consistency | `executives.full_name` = Alex Zhou on both; **the owner row still reads Yongmei Qin** — unresolved, see §5.3 |

### Authorization

| Item | State |
|---|---|
| Executive governance access without admin | **VERIFIED live** — 12 endpoints, `viewer` role |
| No executive admin privilege | **VERIFIED live** — 5 admin surfaces refused |
| Admin cannot impersonate an executive | **VERIFIED live** — admin session, 403 on decide |
| Machine credentials cannot decide | **VERIFIED live** |
| Remaining admin dependency in a governance function | **fixed**: the correlation trace moved to the governance gate. The capability registry and what-if simulator stay admin deliberately — an operator kill switch and a business tool, neither needed to decide |

### Attribution

| Item | State |
|---|---|
| Actual actor | `decided_actor` on every new decision |
| Represented authority | `decided_by` + `authority_role` |
| Delegation | both roles recorded, clock not restarted, reason mandatory |
| Timestamps | `decided_at`, `executing_at`, `executed_at`, `breached_at`, `escalated_at` |
| Historical limitation | pre-activation rows carry `decided_actor IS NULL`; the twelve admin-attributed approvals are untouched and a test enforces that |

### Security

| Item | State |
|---|---|
| Credential revocation | per-executive; `executives.is_active` and `auth_credentials.is_active` are both per row |
| Independent lifecycle | **VERIFIED** — revoking one leaves the other four deciding |
| Session behaviour | eligibility read per request, so revocation bites immediately; sessions expire and idle out unchanged |
| Privilege boundaries | governance · administration · application role · data access are four separate things, and the probe shows the first two no longer imply each other |

### Governance

Approval, rejection, delegation, the 48-hour SLA, CEO escalation with repeating notices,
and the audit trail are all covered by the 86-test activation suite and were exercised
live. What has never happened is a decision by a real executive, because no executive can
sign in yet.

## 10. What this report does not claim

- Not deployed, therefore **not production verified**. No line above may be quoted as
  evidence about Railway.
- Local traffic is a handful of requests; concurrency evidence is the race tests, not load.
- Email **acceptance** is proven; **delivery** requires the info@ archive after deploy.
- Ninety days of met SLA is the evidence that governance is *operated*. This stage can
  only make that measurable.
- **No executive has ever made a decision in this system.** Every verification above uses
  a session constructed for the test. That is the strongest evidence available before
  credentials exist, and it is not the same as a person signing in and deciding.

## 11. A correction, recorded rather than quietly fixed

During the authorization probe on 2026-09-05 an automated test rejected a **real pending**
`kb.publish` proposal and recorded it as a decision by the CEO. That is exactly the
falsehood this stage exists to prevent, produced by the verification of it.

What was done: the row was **not** deleted and `decided_by` was **not** rewritten, because
§7's rule against manufacturing attribution applies to correcting one's own error too. Its
`decision_reason` now states plainly that the rejection came from an automated probe and is
not a CEO judgement. The probe was then changed to raise its own throwaway proposal and
delete it afterwards.

It is recorded here because a governance report that hides its own attribution error is
not evidence of anything.

## 12. Security finding — unauthenticated password-reset token disclosure

**Severity: critical. Found 2026-09-05 while verifying that the new executive credentials
work. Fixed, pushed, and DEPLOYED 2026-09-06 (commit `bfbfac7a37b9`).**

`POST /auth/password-reset/request` takes no authentication and returned the freshly
minted `reset_token` in its response body, under a comment reading `# DEMO ONLY — remove
in production`. Nothing enforced the comment. An anonymous caller who knows an email
address could take over that account.

### Evidence, classified

| Observation | Classification |
|---|---|
| Anonymous POST for `cto@agentorc.ca` returned a token; consuming it set a password; that password signed in and reached `can_decide=true` | **VERIFIED** — full local round trip |
| Anonymous POST for `admin@conscestra.local` returned a token | **VERIFIED** — token deliberately **not** consumed |
| A nonexistent identifier returns no token and a different message, so the endpoint also enumerates valid accounts | **VERIFIED** |
| The commit Railway reports as deployed, `cfaef1776d54`, contains the same disclosure at the same line | **VERIFIED** — read at that commit, not inferred from `master` |
| The deployed endpoint answers unauthenticated with HTTP 200 | **VERIFIED** — probed with an invalid identifier only |
| A real identifier posted anonymously to the deployed host returns a working token | **INFERRED** — not exercised against a live production account, by choice |

The last row is deliberately left as an inference. Proving it would have meant minting a
live reset token for a real account on the deployed system, and the finding does not need
it: the deployed artefact contains the code, and the endpoint answers.

### Why this outranks the migrations

While the only credential was one shared administrator and the console decided nothing,
this was a bad demo shortcut. The five individual credentials change what it means.
Governed decisions are now bound to an authenticated executive session *precisely so an
approval can be attributed to a person*. This endpoint made that attribution forgeable
from outside with nothing but an email address — no impersonation, no admin token, no
request-body authority selection, none of the routes this design spent its effort closing.
The identity work is worth exactly what its weakest sign-in path is worth.

Note also what it does to the record: a forged reset produces a **genuine** session for a
genuine executive, so the resulting approval is indistinguishable in the audit trail from
one the executive made. There is no anomaly to detect afterwards.

The 5-per-hour per-identifier throttle is not a mitigation. It rations attempts at a
takeover that succeeds on the first.

### The fix

Disclosure now requires an explicit affirmative opt-in **and** the absence of deployment
evidence, and the withheld path returns the identical sentence for real and invented
accounts, closing the enumeration with it.

It deliberately does **not** gate on `is_deployed()` alone. That helper documents itself as
treating absence of evidence as "not deployed" so a developer is never blocked — the right
bias for a warning and the wrong one for a secret. A host that set neither `APP_ENV` nor
the `RAILWAY_*` markers would have kept handing out tokens. Unset anywhere, including a
misconfigured production box, no token is returned.

`AUTH_RESET_TOKEN_IN_RESPONSE=1` is set in the local `.env` so the `auth.html` reset tab
keeps working on the laptop. **It must never be set on Railway.**

`governance/tests/test_reset_token_disclosure.py` — 11 tests covering the gate, the
oracle, and the structure. The structural test parses the handler and asserts the token is
returned only from a guarded branch, because the original defect *was* a correct comment
sitting beside unconditional code. Mutation-checked: restoring the unconditional return
fails 10 of the 11, including the structural one.

### The second half: nothing ever emailed the token

`password_reset_request` **sent no mail**. Its own docstring said "Production note: email
the returned token to the user", and that was never implemented — the flow appeared to work
only because the token came back in the response and `auth.html` pre-filled it. Closing the
disclosure on its own would therefore have left the deployed system with **no password
reset at all**: nothing in the response, nothing in an inbox.

Built 2026-09-06. `_send_reset_email` sits in the branch where the token was *not* returned,
so exactly one channel ever carries it. Failures are logged and swallowed, because the reply
must stay identical whether or not the account exists — an error would re-open the
enumeration oracle the shared message closes.

The decision that needed making was **not** whether to use `email_authority`'s ledger. It
was whether this belongs in `staff_email` at all, which the allowlist doctrine says it does:
it reaches staff, including all five authorities. It is exempted, with the reason recorded
in `email_call_sites.py`. That path applies tier, preference and an attention budget — all
correct for work notices and all wrong here. **A person who muted digests, or whose budget
is spent, must still be able to recover their own account.** Nor is it attention-competing
mail; the recipient asked for it seconds earlier. The real abuse concern is the opposite
one, an unauthenticated caller mailbombing an executive, and the five-per-identifier-per-hour
throttle answers that.

Two supporting details, both verified rather than assumed:

- `commercial=False`, so the marketing opt-out list is not consulted. Unsubscribing from a
  newsletter must never lock somebody out of their account.
- The link is built from `PUBLIC_SITE_URL`, because no `*.html` is in git and an `APP_URL`
  link would land on a 500. `release_guard._check_public_url` already checked both origins
  on a deployed host; its message now names the reset link too. Exercised in all three
  states: local, deployed-with-it-unset, deployed-with-it-set.

### What this finding says about the review that missed it

The assessment reviewed governance, ownership, provenance and deployment. It did not
review the authentication surface, because at the time there was one shared credential and
nothing consequential behind it. The credentials created on 2026-09-05 changed that
surface's importance without changing the surface, and the defect had been sitting in the
deployed code throughout. **Adding an identity layer re-scopes every path that can mint an
identity**, and that re-scoping was not on any checklist here. It is now.

**HUMAN ACTION REQUIRED:** authorise deployment of this fix, independently of the four
governance migrations. It touches one endpoint.

## 13. Production deployment — 2026-09-06

Application commit `bfbfac7a37b9`. All five SQL files applied to Railway, in order.

### What was verified, and how

| Claim | Evidence | Classification |
|---|---|---|
| The reset-token disclosure is closed | Posted a **real** identifier and an **invented** one to the deployed host; both returned `"If that account exists, a reset link has been sent"` with `reset_token: null` | **VERIFIED (production)** |
| A consumed code retires its siblings | `consume_password_reset_token` on the production database carries the sibling-retirement clause | **VERIFIED (production)** |
| Reset links reach the site, not the API | Release guard logged `page links to https://agentorc.ca` | **VERIFIED (production)** |
| The new mail call site is declared | Release guard logged `25 send_email call sites, all declared` — matching local exactly | **VERIFIED (production)** |
| The SQL manifest shipped intact | Release guard logged `41 governed, 238 out-of-band` — matching local exactly | **VERIFIED (production)** |
| Governed proposals can be created | `resolve_accountable_owner` now finds four eligible authorities where the query previously failed outright | **PARTIALLY VERIFIED** — the underlying query was exercised; no proposal was raised on production |

### The gap this release opened, and closed

The application shipped before its schema. For roughly two hours Railway ran governance
code against a database with none of its tables, and the failure was measured rather than
assumed: `column e.owner_id does not exist` and `relation "governance_action_policies" does
not exist`.

The consequences split cleanly, and the split is worth keeping.

- **Policy lookup failed safe.** No policy row means `may_auto_execute` returns false, so
  nothing could execute without a human. The fail-closed default did its job.
- **Proposal creation failed hard.** `resolve_accountable_owner` raises when no eligible
  authority can be resolved, and `propose()` calls it first, so a governed write could not
  even queue. Nothing was lost — there were zero pending approvals throughout — but the
  window is real and it is the reason the four files belong in the same change as the code
  that reads them.

**The lesson is about how it was communicated, not about the files.** All five were declared
`PENDING DEPLOYMENT` together, but the handover named the reset SQL and left the other four
in a commit body. A deployment instruction is part of the deliverable; if only one of five
files gets applied, the instruction was wrong regardless of what the declarations said.

### Production state after the migrations

| Measure | Value |
|---|---|
| Action policies | 37 (35 human-approval) |
| Active executives | 5 |
| Eligible owners | 4 of 5 |
| Executive credentials | **0 of 5** (plus 1 platform admin) |
| Pending approvals | 0 |
| Legacy `expired` approvals | 60 |
| Governance alerts | 1 (opened by the machinery itself) |

### What is still outstanding on Railway

1. **No executive can sign in.** The five credentials exist on the local database only, so
   the production console can decide nothing. This is D5 holding rather than a fault, and it
   is the next step.
2. **The CTO has no owner identity there.** `fn_owner_eligible` is false for that role, so
   its classes route to the CEO flagged `ownership_exception` — the same designed
   degradation that applied locally before the grant. `docs/governance/executive_identity_activation.md` §3
   is the procedure, and it must run **after** the migrations, which it now can.
3. **60 legacy `expired` approvals remain.** They are historical record and are deliberately
   left alone; the retired expiry sweep can no longer add to them.
