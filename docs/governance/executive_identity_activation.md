# Executive Identity Activation — CEO · CRO · CFO · CTO · COO

**Procedure, not a migration.** Every step here is a deliberate act by a person with
authority. None of it is automated, and none of it may become a side effect of a policy
change: an identity that can approve business actions and receive escalation mail is
created because somebody decided to create it, and the record must say who.

Companion to `docs/governance/activation_plan.md` (§5, §6, §20) and
`docs/governance/five_authority_readiness.md`. Baseline measurement: 2026-09-05.
Executed 2026-09-05 under the CEO's authorisation; last measured 2026-09-06.

---

## 1. What "identity" means here — five separate things

They are separate on purpose. Conflating any two of them is how a customer contact
became an owner of internal work on this corpus, and how one uuid came to name two
different people.

| # | Layer | Table / mechanism | Answers | Created by |
|---|---|---|---|---|
| 1 | **Authority record** | `executives` (`role_code`, `email`) | which of the five roles this person holds | Executives page, or SQL |
| 2 | **Owner (staff) identity** | `owners` row | the id business work and governance rows point at | `assignable.provision_owner()` |
| 3 | **Directory membership** | `assignable_identity` (`source='executive'`) | may this identity be given work at all | `assignable.grant()` |
| 4 | **Authenticated principal** | `auth_credentials` + `auth_sessions` | can this person prove they are themselves | `POST /auth/signup` |
| 5 | **Governance authority** | `governance_action_policies.approver_role` | which action classes they decide | already configured — no per-person step |

Eligibility (`fn_owner_eligible`) is the conjunction of 2 and 3 plus four refusals: not a
customer contact, not a service identity, not an identity collision, membership active.
**Layer 5 needs 1–4 to be real; nothing infers one from another.**

## 2. State as measured, 2026-09-06

| Role | 1 Authority | 2 Owner | 3 Membership | 4 Credential | Password set | Eligible | Attested real |
|---|---|---|---|---|---|---|---|
| CEO | ✓ Alan Qin | ✓ | ✓ | ✓ `viewer` | ✓ 09:35 | ✓ | — |
| CRO | ✓ Daping Qin | ✓ | ✓ | ✓ `viewer` | ✓ 09:40 | ✓ | — |
| CFO | ✓ Sherman Zhang | ✓ | ✓ | ✓ `viewer` | ✓ 09:37 | ✓ | — |
| CTO | ✓ Bill Wang | ✓ `585f003c…` | ✓ | ✓ `viewer` | ✓ 09:09 | ✓ | ✓ **attested by the CEO** |
| COO | ✓ Alex Zhou | ✓ `e4d99e38…` | ✓ | ✓ `viewer` | ✓ 09:39 | ✓ | ✓ **attested by the CEO** |

**MILESTONE — 2026-09-06 09:40. All five authorities are operational.** Each executive
holds an individual credential, has set their own password through the self-service reset
flow, and can sign in and decide. `GET /governance/authorities` reports an empty `missing`,
an empty `without_credential` **and** an empty `identity_mismatch` for the first time.

Verified against the database rather than from the console's own report: each credential
carries a `last_used_at` from the moment its owner set the password, and **zero outstanding
reset codes remain for any of them** — the sibling-retirement rule in §10 firing five times
in real use.

Two proposals are already queued on the CRO's desk, and no pending item carries
`ownership_exception`. Nothing is sitting on the CEO's queue because the system could not
work out who owned it.

**All five are live**, as of 2026-09-06. **No executive holds `access_role='admin'`, and none needs to.**
Verified on 2026-09-05 against the running application:

| Surface | Executive session | Meaning |
|---|---|---|
| `/governance/whoami` | 200, `role_code='CTO'`, `can_decide=true` | governance authority works |
| `/governance/queue` | 200 | they can see what they must decide |
| `/executives` · `/agent-bus/status` · `/supervisor/status` · `/corpus-provenance/summary` · `/demo/status` | **403** | authority did **not** confer platform administration |

Twelve historical approvals still read `decided_by = admin@conscestra.local` and were **not
rewritten**. They record what actually happened: a shared credential decided them and the
human behind each one is unrecoverable. That is the gap this procedure closes going
forward, not backwards.

## 3. CTO identity grant — DONE, 2026-09-05

Executed under the CEO's written authorisation, in this order. `provision_owner` UPDATEs
the membership row, so it does nothing useful if run before the grant.

```python
from app.core import assignable
assignable.grant("cto@agentorc.ca", display_name="Bill Wang",
                 source="executive", source_ref="CTO", added_by="Alan Qin (CEO)")
assignable.provision_owner("cto@agentorc.ca", display_name="Bill Wang",
                           by="Alan Qin (CEO)")
# -> owner_id 585f003c-679e-4101-a6f9-13c12310670d, is_synthetic=False
```

```sql
UPDATE executives SET owner_id = '585f003c-679e-4101-a6f9-13c12310670d',
                      employee_uuid = '585f003c-679e-4101-a6f9-13c12310670d',
                      updated_at = now()
 WHERE role_code = 'CTO' AND is_active;
```

Both columns carry the value during the E7 transition window: `owner_id` is the truthfully
named copy of the misnamed `employee_uuid`, and eligibility reads whichever exists.

**`is_synthetic = False` because a person attested it, not because the code defaults there.**
The CEO stated that Bill Wang is a real human, so that statement is recorded where the
system reads it rather than left in a chat log:

```sql
INSERT INTO corpus_provenance (entity_type, entity_id, state, rule, evidence, decided_by)
VALUES ('owners', '585f003c-679e-4101-a6f9-13c12310670d', 'real', 'human_attested',
        '{"attested_by": "Alan Qin (CEO)"}'::jsonb, 'Alan Qin (CEO)');
```

This moved `eligible_production` from 0 to 1 — the first owner on this corpus the system
can prove is a real person. Everything else in `owners` remains unattested and therefore
never counts as real.

Then the CTO's already-queued work, which had been routed to the CEO as an
`ownership_exception` while no CTO owner existed, was re-pointed to its policy owner.

**Acceptance criteria, met:** the three tests that were failing truthfully because of this
gap now pass without being modified —
`test_assignable_identity::test_31_no_executive_is_missing_from_the_directory`,
`test_work_ownership::test_J0_the_truthful_column_exists_and_carries_the_values`,
`::test_J1_every_value_resolves_where_the_new_name_says_it_does`.

**Railway has the CTO authority row but not the activation migrations**, so `owner_id` and
`fn_owner_eligible` do not exist there yet. Run the grant *after* the migrations, not before.

## 4. Executive credentials — DONE, all five

Created with `sp_signup_with_lead`, which is how staff sign in on this system. Each was
given a long random secret that the creating process **discarded unread**: nobody, this
system included, knows any of those passwords. Each executive sets their own through
**Reset password** on `auth.html`.

```
ceo@agentorc.ca   Alan Qin        access_role=viewer   active
cro@agentorc.ca   Daping Qin      access_role=viewer   active
cfo@agentorc.ca   Sherman Zhang   access_role=viewer   active
cto@agentorc.ca   Bill Wang       access_role=viewer   active
coo@agentorc.ca   Alex Zhou       access_role=viewer   active
```

`viewer` is deliberate and sufficient. `require_governance_actor` admits them to the
governance routers on the strength of the `executives` row alone, and to nothing else.
Do **not** run `POST /admin/users/role` on any of them.

Sign-in resolves through the **leads** path, so the `contacts` email-verification gate does
not apply and none of them can look like a customer to `fn_owner_eligible`, whose customer
test is the shared `contacts.contact_id = owner_id` primary key. Both checked on the data,
not assumed.

> **§8 is closed.** The password-reset endpoint handed a working reset token to anonymous
> callers. Fixed, deployed 2026-09-06 (commit `bfbfac7a37b9`), and verified against the
> live host: a real account and an invented one now return byte-identical replies with no
> token.

## 5. COO identity — a rename that had only half landed. DONE 2026-09-06

**The office did not change hands.** The owner corrected the record on 2026-09-06: Alex
Zhou and Yongmei Qin are the same person, and the earlier reading of this section — that a
new officer had taken the post — was wrong. That selects the other branch, and it is the
safe one: one human, one identity, and history that never needed to move.

What had actually happened is narrower and more ordinary. The rename was applied to
`executives.full_name` and stopped there. The owner row and the directory membership still
read the old name, so the authority row and the identity it decided under disagreed.

| Fact | Before, 2026-09-06 | After |
|---|---|---|
| `executives` COO row | `Alex Zhou <coo@agentorc.ca>` | unchanged |
| …points at owner | `e4d99e38-de9d-4358-bdc8-ee3a86c1743f` | **the same uuid** |
| …which read | Yongmei Qin | **Alex Zhou** |
| Directory display name | Yongmei Qin | **Alex Zhou** |
| Work held by that owner | 48 activities, 2 opportunities | **48 activities, 2 opportunities** |
| `owners` / directory row counts | 45 / 5 | **45 / 5** |

The last two rows are the evidence that this was a rename and not a succession. No row was
created, no record changed hands, and the owner uuid every historical decision points at is
the one it always was. History stays valid because it always meant this person.

```bash
python -m scripts.provision_executive --role COO --name "Alex Zhou" \
    --email coo@agentorc.ca --by "Alan Qin (CEO)" \
    --i-am-renaming-the-same-person --credential --apply
```

**The guard fired first, and that is the point.** Run without
`--i-am-renaming-the-same-person`, the script refused, naming the 50 records it would have
reattributed. That refusal is what turned an assumption into a question, and the question
got the right answer from the only place it could come from — a person. A script that had
quietly done the "obvious" thing would have been right this time by luck, and silently
wrong the first time an office really does change hands.

Verified after applying: all five authorities eligible, all five hold a credential, no
identity mismatch, and a live round trip signed in as the COO, reached `can_decide=true`
under the name Alex Zhou, and was refused 403 on five admin surfaces.

**Still true, and still the reason the succession branch exists.** Role mailboxes are the
wrong identifier for all five. An office transfers between people and the mailbox transfers
with it, so `coo@agentorc.ca` cannot by itself distinguish two post-holders. That did not
bite here, because there is only one post-holder. It is waiting for the first office that
genuinely changes hands, and `scripts/provision_executive.py` is where it gets handled.

**The divergence is now detected rather than remembered.** `GET /governance/authorities`
reports `identity_mismatch` on every authority: whether the name on the authority row and
the name on the owner row it decides under describe the same person. While the rename was
half-applied it read:

```
authority row says 'Alex Zhou' but the owner it is attributed to
(e4d99e38-de9d-4358-bdc8-ee3a86c1743f) is 'Yongmei Qin' <coo@agentorc.ca>.
Decisions made under this authority would be stamped with the second person's identity.
```

It now reports nothing, for the right reason.

The check earns its place whatever the cause. Before it, `resolve_accountable_owner`
returned the label **"COO Alex Zhou"** over a row reading Yongmei Qin with
`exception=false` and `eligible=true`, and nothing was flagged — from the system's point of
view nothing was wrong: the role had an eligible owner and a name to show. A half-applied
rename and a mishandled succession produce the identical symptom, and only one of them is
harmless. The detector does not have to tell them apart; it only has to stop the system
from quietly asserting that a name and an identity agree when they do not.

Compared on a normalised name, so a change of case or spacing is not reported as a change
of person — a detector that cried at whitespace would be switched off within a week, and
then would not be there for the case that matters. Covered by
`governance/tests/test_executive_identity_mismatch.py`, which tests the **detector** rather
than pinning today's data, including a guard against the underlying join being dropped and
the detector silently becoming a function that always says yes.

**Attested 2026-09-06.** The CEO stated that Alex Zhou is a real person, and that
statement is recorded where the system reads it, in the same shape as the CTO's:
`state='real'`, `rule='human_attested'`, `decided_by='Alan Qin (CEO)'`. `eligible_production`
moved from 1 to 2.

Worth noting what did **not** move it. The COO had been an eligible owner throughout, was
renamed, and was issued a credential — none of that counted. Only the sentence "this is a
real human", written down and attributed to the person who said it, counted. Three of the
five remain unattested and are therefore not production-accountable.

**One correction worth keeping visible.** This section previously recorded, as fact, that
the COO office had changed hands, and a good deal of analysis was built on top of that. It
came from a misreading, and the owner corrected it on 2026-09-06. The refusal in
`provision_executive.py` is what forced the question into the open rather than letting an
assumption become an UPDATE statement. The lesson is not that the guard was unnecessary —
it is that a guard which makes somebody stop and answer is worth more than one that decides
for them.

## 6. Rollback

| Step | Reversal | Note |
|---|---|---|
| 4 credential | `POST /admin/users/active` (deactivate) or delete the `auth_credentials` row | Sessions expire on their own; deactivating is immediate for new sign-ins |
| 3 link | `UPDATE executives SET owner_id = NULL, employee_uuid = NULL WHERE role_code='CTO'` | CTO items fall back to the CEO as exceptions — the pre-activation behaviour |
| 3 attestation | `DELETE FROM corpus_provenance WHERE entity_type='owners' AND entity_id='585f003c…'` | `eligible_production` returns to 0; do this only if the attestation was wrong |
| 3 owner | `assignable.revoke()` unlinks membership; the `owners` row is left in place | An owner row that has held work must not be deleted — history points at it |
| 3 membership | `assignable.revoke("cto@agentorc.ca", by="<you>")` | Eligibility is time-varying: revocation takes effect on the next check |
| Authority row | `UPDATE executives SET is_active=false WHERE role_code='CTO'` | Policies stay; their items route to the CEO as exceptions again |

Nothing here is destructive, and none of it needs a migration. Reversing an identity grant
is itself an act worth recording — say why in the same place you say who.

## 7. Definition of done

- [x] CTO membership, owner, link and eligibility — granted 2026-09-05, `added_by` names the CEO
- [x] the CEO's attestation that Bill Wang is real is recorded where the system reads it
- [x] the three identity tests pass **without being modified**
- [x] four individual credentials exist, no two people share one
- [x] **no executive holds `access_role='admin'`** — verified live that none is needed
- [x] an executive session is refused on five admin surfaces and admitted on governance
- [x] no pending approval carries `ownership_exception=true`
- [x] historical admin-attributed approvals left intact
- [x] **COO identity reconciled** 2026-09-06 — same person, one identity, 50 records untouched
- [x] all five credentials exist; `/governance/authorities` reports empty `missing`, `without_credential` and `identity_mismatch`
- [x] **all five executives have set their own password** and can sign in — 2026-09-06
- [x] COO attested real by the CEO, 2026-09-06; `eligible_production` is 2
- [x] a consumed reset code retires every sibling (§10)
- [ ] §8 and §10 deployed together
- [ ] CEO, CRO and CFO attestations — three of five owners remain unvouched-for
- [ ] any of this repeated on Railway (migrations not applied there yet)

## 8. Security finding — unauthenticated password-reset token disclosure

**Found 2026-09-05 while verifying that these credentials work. Fixed, and DEPLOYED
2026-09-06.**

Verified on the deployed host after the release, not inferred from the commit: posting a
**real** identifier and an **invented** one both return
`"If that account exists, a reset link has been sent"` with `reset_token: null`. The
takeover and the account-enumeration oracle are closed together.

`POST /auth/password-reset/request` takes no authentication and returned the freshly minted
`reset_token` in its response body, under a comment reading `# DEMO ONLY — remove in
production`. Nothing enforced the comment.

| Evidence | Classification |
|---|---|
| Local round trip: anonymous POST for `cto@agentorc.ca` returned a token; consuming it set a password; that password signed in and reached `can_decide=true` | **VERIFIED** |
| Anonymous POST for `admin@conscestra.local` returned a token (**not** consumed) | **VERIFIED** |
| A nonexistent identifier returns no token — so the endpoint is also an account-existence oracle | **VERIFIED** |
| The commit Railway reports as deployed, `cfaef1776d54`, contains the same disclosure line | **VERIFIED** (read at that commit) |
| The deployed endpoint answers unauthenticated with HTTP 200 | **VERIFIED** (probed with an invalid identifier only) |
| Therefore a real identifier posted anonymously to the deployed host returns a working token | **INFERRED** — deliberately not exercised against a live account |

**Why it matters more than it used to.** While the only credential was one shared
administrator and the console decided nothing, this was a bad demo shortcut. The five
individual credentials change its meaning: governed decisions are now bound to an
authenticated executive session *precisely so an approval can be attributed to a person*.
This endpoint made that attribution forgeable from outside with nothing but an email
address. The identity work is worth exactly what its weakest sign-in path is worth.

The 5-per-hour per-identifier throttle is not a mitigation. It rations attempts at a
takeover that succeeds on the first.

**The fix** gates disclosure on an explicit opt-in **and** the absence of deployment
evidence, and returns the same sentence for real and invented accounts so the oracle closes
with it. It does not gate on `is_deployed()` alone: that helper documents itself as
treating absence of evidence as "not deployed" so a developer is never blocked, which is
the right bias for a warning and the wrong one for a secret. Unset anywhere — including a
misconfigured production host — no token is returned.

`AUTH_RESET_TOKEN_IN_RESPONSE=1` is set in the local `.env` so `auth.html`'s reset tab keeps
working on the laptop. **It must never be set on Railway.**

`governance/tests/test_reset_token_disclosure.py` covers the gate, the oracle and the
structure. Mutation-checked: restoring the unconditional return fails 10 of its 11 tests,
including the structural one, so a future rewrite that reintroduces the defect goes red.

**The delivery half, built 2026-09-06.** This endpoint never emailed the token; its own
docstring said it should, and that was never implemented. The flow appeared to work only
because the token came back in the response, which is precisely what made it a takeover.
Closing the disclosure alone would have left the deployed system with no password reset at
all.

`_send_reset_email` now delivers the code, and sits deliberately in the branch where the
token was **not** returned. Exactly one channel carries it, never two: on a laptop the
response contains it and no mail leaves the machine; on a deployed host the response
contains nothing and the inbox is the only route. Send failures are logged and swallowed,
because this reply must be byte-identical whether or not the account exists — an error here
would re-open the account-existence oracle that the shared message closes.

Three decisions worth stating:

- **`commercial=False`.** A reset marked commercial would be checked against the marketing
  opt-out list, so unsubscribing from a newsletter could lock somebody out of their own
  account.
- **Not routed through `staff_email`.** It reaches staff, including all five authorities, so
  the allowlist doctrine says it should be. It is exempted with the reason recorded in
  `email_call_sites.py`: that path applies tier, preference and an attention budget, all
  correct for work notices and wrong here. A person who muted digests, or whose budget is
  spent, must still be able to recover their account. The abuse concern is the opposite one
  — an unauthenticated caller mailbombing an executive — and that is answered by the
  five-per-identifier-per-hour throttle on the endpoint.
- **The link is built from `PUBLIC_SITE_URL`, not `APP_URL`.** No `*.html` is in git, so the
  backend cannot serve `auth.html` in production and an `APP_URL` link would land on a 500.
  `release_guard._check_public_url` already checks both origins on a deployed host; its
  message now names the reset link alongside the Order Status button, because a bad origin
  there is not a broken button, it is a locked door.

**HUMAN ACTION REQUIRED:** decide whether to deploy this ahead of the governance migrations.
It is a small, self-contained change to one endpoint and it closes a live takeover path on
the deployed host. Nothing has been pushed.

## 9. Where an executive sets or resets their password

There is no separate "set initial password" flow, and deliberately so. Each credential was
created with a random secret that was discarded unread, so **reset is the set-password
path** for all five. Three routes, in the order they are useful:

**1. Self-service — the normal path.** `auth.html` → the **Reset password** tab → enter
their own address → **Send Reset Link**. Where the token is disclosed locally the page
pre-fills it; otherwise the code arrives by email. Paste it into *Reset token*, choose a new
password, then sign in.

| Environment | What happens | Works today |
|---|---|---|
| Local, `AUTH_RESET_TOKEN_IN_RESPONSE=1` | code returned in the response and pre-filled on the page | **yes** |
| Local, flag unset | code emailed | yes, if SMTP is configured |
| Railway, after the fix deploys | code emailed; nothing in the response | after deploy |
| Railway, today | **nothing** — the token is returned to whoever asked, which is the defect | do not use |

**2. Admin-initiated — useful for onboarding.** The CEO can push a code to any of them
without their involvement:

```bash
curl -X POST $APP/admin/users/reset-password \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"identifier":"cfo@agentorc.ca"}'
```

Verified 2026-09-06 with delivery captured rather than sent: `200`, `emailed: true`, and the
message carries a working code. This route is **already in the deployed commit**
`cfaef1776d54`, so it needs no deployment. It is the fastest way to get five people signed
in once the credentials exist on Railway.

**3. Not a route: an administrator setting a password on someone's behalf.** No endpoint
does this, and none should. The whole point of individual credentials is that only one
person can present each one; a password an administrator chose is a password two people
know, and it puts the shared-credential problem back with extra steps.

**A caveat on all three.** The five credentials exist on the **local** database only. Railway
has neither the executive credentials nor the four governance migrations, so nobody signs in
there yet whatever route they use.

## 10. A used reset code retires every sibling

**Found 2026-09-06, immediately after the first executive completed a reset. Fixed in
`governance/sql/reset_token_single_use.sql`, applied locally and to Railway 2026-09-06;
the function on the production database now carries the sibling-retirement clause.**

`consume_password_reset_token` marked exactly one row used: the row whose hash was
presented. Every other outstanding code for that credential stayed valid until its own
expiry. Measured on the local database moments after the CTO had finished securing the
account:

| identifier | live unused | consumed |
|---|---|---|
| cto@agentorc.ca | **64** | 23 |

Sixty-four working keys to an account whose owner had just finished locking it. The reset
had retired none of them.

**Why it outranks its own severity.** A reset code is not a convenience, it is a takeover
primitive: whoever holds one can set the password and inherit the session. The five
credentials exist so that a governed approval can be attributed to a person, so anything
letting two parties reach the same account attacks the attribution and not merely the
account.

The realistic path is mundane rather than exotic. Somebody requests a code, mistypes it,
requests another, uses the second — and the first sits live in their inbox for the rest of
the hour. Mail is forwarded, archived, synced to phones, and role mailboxes are open on
more than one screen. The person did everything right and still left a spare key in
circulation.

**The fix** retires every outstanding token for the credential when one is consumed. Rows
are marked `used`, never deleted: the table is the record of who asked for what and when,
and a reset nobody can account for afterwards is its own problem. `CREATE OR REPLACE` on a
single function, no schema change, idempotent.

**Deployment order matters.** This ships **with or before** the §8 router change, never
after. The router change stops tokens being handed to anonymous callers; this stops the
ones already issued from outliving the reset that should have retired them.

### The suite was minting live keys against a real executive

Sixty-three of those sixty-four came from the test suite, which exercised the reset flow
against `cto@agentorc.ca` — a real person's credential. Two problems, and the second was
about to cost somebody their afternoon:

- every run left dozens of live account-recovery keys against that account, the same class
  of mistake as leaving proposals on a real executive's desk, except these were credentials
  rather than work items;
- one test consumes a code and sets a password, so **the next full run would have silently
  changed the password the CTO had just chosen**, with nothing to connect the two events.

The tests now create and destroy a throwaway `reset-probe-<hex>@seed.agentorc.ca` account
per module, on the catch-all domain this project already uses for synthetic addresses.
`test_00_the_suite_leaves_no_live_key_on_a_real_account` fails on the residue rather than
trusting the next contributor to remember. Confirmed after a full run: the four codes then
in flight were untouched, and no throwaway account was left behind.

**Verified in real use.** All five executives completed a reset on 2026-09-06 and every one
shows zero outstanding codes afterwards.
