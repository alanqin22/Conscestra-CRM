"""Deploy state — which migrations ran, and do all replicas agree on policy?

Two failures this codebase actually hit, neither detectable at the time:

MIGRATION ORDER. Three memory migrations (v1/v2/v3) shipped with no version
table and no ordering enforcement. v2 widened a primary key and silently
disabled the indexer; `reindex` reported "embedded: 0", which is
indistinguishable from "nothing was stale". The failure surfaced days later
through an unrelated test. Nothing recorded what had been applied.

CONFIG DIVERGENCE. Every safety parameter — the assertion floor, decay
half-lives, verify roles, the signing key — is read from per-process
environment. Two replicas can gate differently and nothing compares them. An
attacker who can set one replica's env can lower its floor; an operator who
forgets one replica creates the same effect by accident.

    applied_migrations()   what the database says has run
    check_migrations()     ordered list + what is missing
    safety_fingerprint()   hash of the parameters that decide what may be said
    attest()               record this replica's fingerprint; compare replicas

The fingerprint deliberately EXCLUDES secret values and includes only whether a
secret is present — an attestation endpoint must not become a key oracle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

import psycopg2

from app.core.database import get_connection

logger = logging.getLogger("deploy_state")

# Ordered. A later migration may depend on an earlier one; applying out of order
# is how the primary-key widening broke the indexer.
REQUIRED_MIGRATIONS: List[str] = [
    "metric_registry_migration.sql",
    "metric_registry.sql",
    "content_embeddings.sql",
    "memory_grounding.sql",
    "provenance_enrichment.sql",
    "customer_memories.sql",
    "customer_memories_v2.sql",
    "customer_memories_v3.sql",
    "data_sources.sql",
    "memory_invariants.sql",
    "activity_direction.sql",
    "activity_direction_revert.sql",
    "memory_audit_erasure.sql",
    "governed_mutation.sql",
    "activity_direction_enforcement.sql",
    "customer_memories_actor_key.sql",
    "shadow_paired_eval.sql",
    "memory_eval_labels.sql",
    "memory_eval_instrument.sql",
    "content_index_parent.sql",
    "theme_breadth.sql",
    "memory_observability.sql",
    "app_role.sql",
    "erasure_authorization.sql",
    "erasure_log_retention.sql",
    "executives_audit_and_touch.sql",
    # Applied to BOTH local and Railway on 2026-08-15, and declared in the same
    # change — which is the rule this list exists to enforce. Ordered: the
    # address trigger must be repaired before the backfill restores addresses,
    # or the backfill's work is undone by the next line-item edit.
    "fix_order_address_overwrite.sql",
    "backfill_contact_shipping_addresses.sql",
    "order_lifecycle_notifications.sql",
    "order_cancellation_voice.sql",
    # Must follow order_cancellation_voice.sql: it warns (not fails) when
    # 'order.cancelled' is not yet a registered event type, and the ordering
    # here is what stops that warning being the normal case on a fresh database.
    "order_status_self_service.sql",
    # Extends the file above. A separate migration rather than an edit to it:
    # that one is already recorded with a checksum everywhere, and migrate.py
    # reports a changed file as drifted instead of re-running it.
    "order_cancel_reason.sql",
    # verify_order_test_contacts.sql is DELIBERATELY NOT DECLARED, and this is
    # not the same reason as tier1 below. It is not a schema requirement at all
    # — it flips is_email_verified on a handful of contacts so live sends can be
    # exercised. Declaring it would assert that every database MUST have those
    # people emailable, which is false for a fresh environment and false for
    # production. It is also not portable: it names contact_ids, and on Railway
    # four of the five do not exist (see the file's own header).
    # tier1_audit_instrumentation.sql is DELIBERATELY NOT DECLARED. The file
    # exists and is validated, but applying it has not been authorized. This
    # list means "the schema must have this", so declaring an unapplied,
    # unauthorized migration turns `migrate --check` red for a decision nobody
    # has taken — it states a proposal as a requirement. Add the line in the
    # same change that applies the migration, not before.

    # Staff email (docs/employee_email_notifications_design.md). Declared
    # 2026-08-22, the day they were applied to BOTH local and Railway — which
    # is the rule above, honoured in the other direction. They were held
    # undeclared through four stages of local development precisely because
    # this list means "the schema must have this", and until Railway had them
    # that statement was false.
    #
    # Ordered: the ledger creates staff_email_ledger and adds
    # notification_messages.tier, and stage2's trigger writes that column.
    "staff_email_ledger.sql",
    "staff_email_stage2.sql",
    # Declared 2026-08-25, the day it was applied to BOTH databases — verified
    # by query, not by the apply command's own output: 2 of 2 columns, both
    # CHECK constraints and the partial index are present on each. Held
    # undeclared until then precisely because this list means "the schema MUST
    # have this", and while Railway had 0 of 2 columns that statement was false.
    #
    # It was applied through the out-of-band path, which records nothing, so
    # neither ledger has a row for it yet. Declaring it is what lets
    # `migrate.py` adopt it: the next run finds it missing from the ledger,
    # re-runs it (every statement is IF NOT EXISTS / guarded, so a no-op) and
    # records the row with the real checksum. That is provenance completed, not
    # invented — the file on disk is the file that was applied.
    "a2a_outcome_and_principal.sql",
    # APPROVED 2026-08-25. Self-contained: it creates coupons,
    # coupon_redemptions and price_match_requests plus their indexes, and its
    # only foreign-key prerequisite is a table it creates itself, so a clean
    # database can execute it truthfully. All three tables were verified present
    # on BOTH databases before declaring it.
    #
    # Its Railway ledger row is the one written by railway_catchup_20260805.sql
    # with checksum='' -- that row is historical fact and is NOT rewritten.
    # migrate.py reports it as CHECKSUM UNVERIFIABLE and leaves it alone, which
    # is the honest outcome: the file was applied by hand and nobody knows what
    # its bytes were that day.
    #
    # Declared late for the reason this whole list exists: its absence from
    # Railway once caused a fifteen-day outage in which every valid coupon was
    # refused, and nothing detected it. Now a clean deployment must have it.
    "promotions_coupons.sql",
    # PROMOTED 2026-08-28, and only after Railway had it. It sat in
    # OUT_OF_BAND_SQL marked PENDING DEPLOYMENT while it was applied
    # locally but not to production, because this list is a claim about
    # what production has RUN -- putting it here first would have made
    # migrate --check report a chain production had not executed.
    # Verified before promotion: all six canonical triggers present on
    # Railway, zero legacy triggers surviving.
    "touch_updated_at_convergence.sql",
    # PROMOTED 2026-08-28, after Railway was verified: convert_lead absent,
    # fn_update_opportunity_momentum absent, sp_leads intact. It removes a
    # function that could not execute -- zero callers across twelve surfaces,
    # four columns that no longer exist.
    "drop_convert_lead.sql",
    # PROMOTED 2026-08-28 after Railway verification: the fallback is present,
    # confined to the opportunities insert (not invoices or activities), the
    # trigger binding survived CREATE OR REPLACE and all three grants remain.
    "fix_order_opportunity_owner_inheritance.sql",
    # PROMOTED 2026-09-01, and only after Railway had both. They sat in
    # OUT_OF_BAND_SQL marked PENDING DEPLOYMENT while they were applied locally
    # but not to production, because this list is a claim about what production
    # has RUN. Verified on Railway before promotion: customers,
    # invalid_phones_log, appointments, call_logs and call_state all absent,
    # zero orphan functions remaining, and the disposition ledger holding
    # exactly two rows with rows_erased 38 and 8.
    #
    # ORDER IS LOAD-BEARING, not alphabetical. The erasure targets the RETIRED
    # names, so the rename must precede it; applied the other way round it
    # erases nothing while appearing to succeed. The file now REFUSES that
    # ordering rather than relying on this comment — a migration safety gate
    # found the silent no-op against Railway before it ran.
    "retire_customers_fossil_cluster.sql",
    "erasure_e8_retired_tables.sql",

    # ── Governance activation. PROMOTED 2026-09-06, after Railway executed
    #    every one of them and the objects were verified there directly.
    #
    #    They spent a day in OUT_OF_BAND_SQL marked PENDING DEPLOYMENT, which
    #    is the middle state this file names: authored and applied locally, but
    #    not yet claimable as run. REQUIRED_MIGRATIONS is a claim about what
    #    PRODUCTION has executed and `migrate --check` reads it as one, so
    #    entering earlier would have been the verifier asserting a state
    #    production was not in.
    #
    #    ORDER IS LOAD-BEARING for the first four. `governance_activation.sql`
    #    creates the policy table the other three amend; five_authorities
    #    widens the role CHECK to admit the COO before any row names one;
    #    reescalation adds counters to tables activation creates; and
    #    kb_publish_policy rewrites a row activation seeds. Applied out of
    #    order they fail loudly rather than silently, but they do fail.
    #
    #    All four seed with WHERE NOT EXISTS and create with IF NOT EXISTS, so
    #    replay on a database that already has them changes nothing — checked
    #    before promoting, because promotion means migrate.py may replay them.
    "governance_activation.sql",
    "governance_five_authorities.sql",
    "governance_reescalation.sql",
    "governance_kb_publish_policy.sql",
    #    Independent of the four above and safe in any order relative to them:
    #    CREATE OR REPLACE on one function so that consuming a password-reset
    #    token retires every sibling token for that credential.
    "reset_token_single_use.sql",

    # ── P0/P1 remediation. PROMOTED 2026-09-08, after Railway executed both and
    #    the objects were verified there directly:
    #      * GET /governance/decide went 500 -> 403 with the correct refusal,
    #        which is the _row() path that reads the new columns; and
    #      * undeclared_write_capabilities went ['policy.widen'] -> [], which is
    #        the policy row the first file seeds.
    #
    #    THE APP SHIPPED AHEAD OF BOTH on 2026-09-08 04:03 UTC and the
    #    governance decision path was down until they were applied at 04:2x.
    #    The startup log was entirely green throughout: release_guard passed
    #    every check and the capability registry reported 46 of 46, because the
    #    missing columns are only touched when somebody decides something. A
    #    clean boot is not evidence that the schema is present.
    #
    #    ORDER IS NOT LOAD-BEARING between these two -- they share no objects --
    #    but both are idempotent (verified by applying each twice) so migrate.py
    #    may replay them safely.
    "governance_decision_link_identity.sql",
    "workflow_owner_resolution.sql",
    # 48 -> 49 on 2026-09-13. Not a new deployment: the three triggers this
    # wires -- trg_contacts_touch, trg_leads_touch, trg_accounts_touch -- were
    # verified present on Railway read-only as crm_readonly, and locally, before
    # the promotion. What changes is the DECLARATION. The file was applied out
    # of band and filed in tri_fn/ among function definitions, so it sat outside
    # the census with no disposition and no schema_migrations row: applied, with
    # no record that it had been. Declaring it re-applies it idempotently
    # (DROP TRIGGER IF EXISTS + CREATE) and writes the ledger row that was never
    # created. It depends on trgfn_touch_updated_at from
    # 49 -> 48 on 2026-09-29. trg_fn_contacts_leads_accounts_touch.sql was
    # removed from this manifest and classified out-of-band instead. The three
    # triggers it declares are already created and owned by
    # touch_updated_at_convergence.sql, required at position 37, which carries
    # the Railway ledger entry dated 2026-08-28 16:13:52. The note previously
    # recorded here described the file as applied out of band. The object
    # history does not support that: the file was authored on 2026-09-13,
    # sixteen days after the triggers were created, and it declares no object
    # the convergence migration does not already own.
]



# ============================================================================
# SQL DISPOSITION -- every file in sql/ declares which path it belongs to
# ============================================================================
#
# THE GAP THIS CLOSES. Every integrity mechanism here was computed from the
# ledger and this manifest, so a SQL file that changed production without
# entering either was invisible to all of them -- and nothing required a file
# to enter either. `migrate.py --check` iterates REQUIRED_MIGRATIONS,
# `ledger_health()` divides by REQUIRED_MIGRATIONS, and `postdeploy_verify`
# compared tables only. A schema change applied through apply_sql.py passed all
# four in silence. It has already happened at least three times: the
# trg_fn_events_after_insert chain below.
#
# The fix is not a second ledger and not a new table. It is that "not declared"
# stops meaning "nobody thought about it" and becomes a STATEMENT. A file in
# neither list is now an ERROR, not the default.
#
# OUT-OF-BAND IS NOT A DEMOTION. It records a true fact -- this file is not
# replayed by migrate.py -- and for 101 of these files it is the only correct
# answer: a backfill repairs rows a clean database does not have, and replaying
# it would be actively wrong. Declaring everything a migration would turn
# one-time repairs into permanent obligations.
#
# WHY MOST HISTORICAL FILES ARE OUT-OF-BAND. Not judgement, observation: none
# of them is in the governed chain today. Adopting them would assert "a clean
# database should execute this", a stronger claim than "this once ran in
# production" and one the evidence does not support file by file. The six
# entries whose reason begins REVIEW are exactly where that claim may in fact
# be true, and they are named rather than quietly resolved.

# THE LIFECYCLE MARKER, kept after its first use rather than deleted.
# A governed schema change passes through three states, and the middle one
# had no name until 2026-08-28:
#
#   1. authored + classified here, applied locally  -> PENDING DEPLOYMENT
#   2. applied to production                        -> verified directly
#   3. moved into REQUIRED_MIGRATIONS               -> claimed as run
#
# Skipping straight to (3) is the failure this prevents: REQUIRED_MIGRATIONS
# is a claim about what PRODUCTION has executed, and migrate --check reads
# it as one. The next migration of this shape should reuse this marker.
_PENDING_DEPLOYMENT = (
    "PENDING DEPLOYMENT -- authored 2026-08-28 and applied to LOCAL; it is a "
    "governed schema change and belongs in REQUIRED_MIGRATIONS, but it is not "
    "there yet because Railway does not have it. Adding it earlier would make "
    "migrate --check report a chain this database has not run, which is the "
    "verifier claiming a state production is not in. Move it to "
    "REQUIRED_MIGRATIONS in the SAME change that records its Railway "
    "application, and not before. Binds the canonical trg_<table>_touch "
    "trigger on accounts, contacts, leads, customers, employees and "
    "product_pricing, retiring four legacy-named triggers.")

# Second use of the marker above, which is what it was kept for. Same three
# states, same rule: this is NOT a claim that production has run it.
_PENDING_CORRELATION = (
    "PENDING DEPLOYMENT -- authored 2026-08-31 and applied to LOCAL only. A "
    "governed schema change that belongs in REQUIRED_MIGRATIONS, and is not "
    "there yet because Railway does not have it; declaring it earlier would "
    "make migrate --check assert a chain production has not run. Move it in "
    "the SAME change that records its Railway application, and not before. "
    "Adds a BEFORE INSERT trigger on events that fills correlation_id from "
    "the app.correlation_id session GUC, and stops emit_event() inventing a "
    "random correlation id for events that have no play behind them -- 2.8% "
    "of 226k events carried a usable correlation, because eight of nine "
    "trigger functions INSERT INTO events directly and never reach "
    "emit_event().")

# Third use of the PENDING DEPLOYMENT marker. Same rule: not a claim that
# production has run it.
_PENDING_PGVECTOR = (
    "PENDING DEPLOYMENT -- authored 2026-08-31 and applied to LOCAL only. A "
    "governed schema change that belongs in REQUIRED_MIGRATIONS once Railway "
    "has it, and not before. It ADOPTS objects that were applied to local by "
    "hand and entered neither the manifest nor the ledger: a vector(512) "
    "column embedding_v and an HNSW index idx_ce_hnsw, 59% populated and 35 "
    "rows disagreeing with their authoritative bytea. Railway has neither. "
    "Idempotent by construction so it is correct on both. Creating the "
    "structure only -- coverage is filled by content_index.rebuild_vectors(), "
    "not claimed here.")

_PENDING_ALERT_DISPOSITION = (
    "PENDING DEPLOYMENT -- authored 2026-09-10 and applied to LOCAL only. "
    "Governed schema; promote to REQUIRED_MIGRATIONS in the same change that "
    "records its Railway application, and not before. Closes A-02 and A-06 of "
    "docs/architecture_reassessment_2026-09-09.md. Adds "
    "governance_alerts.ack_suppressed_until so an acknowledgement quiets the "
    "15-minute sweep WITHOUT moving due_at -- production acknowledged one "
    "alert three times and it re-escalated 82 seconds after the last one -- "
    "and governance_alerts.resolution_disposition (worked | no_action_needed "
    "| delegated), which entering status='resolved' now requires. Seven of "
    "the eight resolution notes in production were INSTRUCTIONS that never "
    "executed, and all four alerts open afterwards were re-raises of those "
    "same rules. NOT RETROACTIVE: the requirement is tested only on the "
    "transition INTO 'resolved', so the eight existing rows keep their NULL "
    "and can still move to 'closed'. "
    "PHASE 1 OF TWO, AND IT ENFORCES NOTHING -- the requirement lives in "
    "governance_alert_resolution_required.sql. Split because with the "
    "requirement in this file, EVERY deploy order has a window where Resolve "
    "is broken: schema-first makes the trigger demand a field the running app "
    "does not write, app-first makes the app write a column that does not "
    "exist. This file is safe to apply at any time against any app version, "
    "because nothing reads or requires the columns it adds.")

_PENDING_READONLY_ROLE = (
    "PENDING DEPLOYMENT -- authored 2026-09-11 and applied to LOCAL only. "
    "Governed schema; promote to REQUIRED_MIGRATIONS in the same change that "
    "records its Railway application, and not before. Creates crm_readonly for "
    "D-08: the production SUPERUSER DSN has been sitting in .env on the "
    "developer laptop since the first assessment, beside ADMIN_API_TOKEN and "
    "RAILWAY_ADMIN_API_TOKEN, and was used (read-only) to produce the two most "
    "recent audits. SELECT only, no function privileges -- so no SECURITY "
    "DEFINER path can write on its behalf -- and "
    "default_transaction_read_only=on, so a write is refused by the "
    "transaction rather than by a missing grant. "
    "A REPLACEMENT, NOT A DELETION: removing the line would close the exposure "
    "and the evidence channel together, and direct read-only SQL is the single "
    "thing that made the 2026-09-09 audit stronger than its predecessor. "
    "APPLYING THIS FILE DOES NOT CLOSE D-08. It makes the replacement "
    "available; D-08 closes when the postgres password is ROTATED, because "
    "until then the old DSN still works from everywhere it was ever copied. "
    "scripts/verify_readonly_role.py proves the role by ATTEMPTING the writes "
    "and requiring each to be refused, and reports the rotation separately as "
    "an outstanding item rather than folding it into a pass.")

_PENDING_A3_FINANCIAL_STATE = (
    "PENDING DEPLOYMENT -- authored 2026-09-13. The A3 financial-state "
    "implementation: order_invoiceability (mutable current decision), "
    "order_invoiceability_transitions and order_financial_assertions (both "
    "append-only via the existing trgfn_append_only), product_tax_treatment "
    "(created EMPTY by design), five nullable proposition columns on "
    "action_approvals, and seven guard functions. "
    "THE AUDIT PATH IS ENFORCED, NOT ASSERTED. order_invoiceability is a "
    "MUTABLE current decision, so three controls make it auditable rather "
    "than merely documented: an AFTER trigger records every eligibility "
    "change into order_invoiceability_transitions with the predecessor and "
    "successor read from OLD and NEW; a BEFORE DELETE trigger refuses "
    "deletion, because absence of a row means NO DECISION RECORDED and "
    "deleting one would silently restate a decision as an unrecorded one; "
    "and BEFORE TRUNCATE triggers close the same removal on all three A3 "
    "tables. A BEFORE INSERT trigger on the transition table enforces the "
    "CONVERSE, that every transition corresponds to a real eligibility "
    "change: the successor must equal the current decision, the predecessor "
    "must equal the end of the recorded chain, and the two must differ. "
    "Those conditions cannot all hold for any row written outside the "
    "recorder, so the table is recorder-owned by construction rather than by "
    "permission -- which matters because ALTER DEFAULT PRIVILEGES grants "
    "crm_app full DML on every new table, so a REVOKE here would neither be "
    "durable nor bind the superuser the application connects as locally. "
    "order_invoiceability_transitions also gains transition_seq "
    "(bigserial), the ordering authority, for the reason assertion_seq "
    "exists: `at` is transaction-scoped and the uuid primary key broke the "
    "tie by chance, which read a five-step history back in the wrong order "
    "in 12 of 12 runs. "
    "ADDITIVE AND IDEMPOTENT. It creates tables, adds nullable columns and "
    "attaches triggers; it reads no existing row, writes no existing row and "
    "backfills nothing. "
    "NO TABLE-WIDE CONSTRAINT IS ADDED, and that is the design rather than an "
    "omission: 2,463 of 2,521 orders carry no currency, all 415 products are "
    "unclassified for tax, and 2,032 orders have no fulfilment event, so a "
    "blanket NOT NULL would force a value onto historical rows that do not "
    "have one. Every invariant is enforced at the transition that requires it. "
    "Applying it therefore changes no existing behaviour: nothing writes to "
    "the new tables until the A3 application path does. "
    "Promote to REQUIRED_MIGRATIONS in the same change that records its "
    "Railway application, and not before.")

_PENDING_INVOICE_CANCELLATION = (
    "PENDING DEPLOYMENT -- authored 2026-09-17. The cancellation authority for "
    "decision D1: a table beside invoices whose row existence is the cancelled "
    "state, carrying an effective timestamp, the deciding actor and a reason, "
    "and made terminal by the generic append-only trigger. "
    "THE DEFECT IT CLOSES is that cancellation was carried by invoices.status, "
    "which trgfn_invoice_before recomputes from balance_due on every write. A "
    "fact that economics do not determine cannot survive in a column that "
    "economics recompute. Measured: INV-000023 was voided on 2026-02-25, the "
    "projector rewrote its status, and the immaterial-overdue sweep recorded a "
    "confirmed payment against it on 2026-06-15 -- the settlement gate could "
    "not fire because it read that same column. "
    "IT IS A TABLE AND NOT A COLUMN so that ordinary updates and settlement "
    "projections of the invoices row cannot erase it implicitly; changing "
    "cancellation must target the authority itself. This follows the precedent "
    "A3 set with order_invoiceability for a lifecycle fact economics cannot "
    "derive. "
    "APPROVAL IS DELIBERATELY NOT ENFORCED. The approval_uuid column is an "
    "extension point for decision D2, which is recorded as NOT ESTABLISHED; "
    "nothing requires it, and its presence must not be read as a requirement. "
    "Deploy AFTER a3_invoice_economic_integrity.sql and BEFORE the settlement "
    "authority, whose lifecycle precondition reads this table.")

_PENDING_OWNER_PERSONHOOD = (
    "PENDING DEPLOYMENT -- authored 2026-09-18. The owner personhood register: "
    "owner_personhood, an effective-dated declaration of whether an identity is "
    "a natural person or a service identity, with fn_personhood_of, "
    "fn_personhood_certification_report, fn_personhood_roster_certified, "
    "v_owner_principal and v_personhood_unclassified. "
    "THE GAP IT CLOSES is that E2 names personhood as one of six eligibility "
    "grounds, and it is the one ground SQL cannot evaluate: the rules live in "
    "the Python constants SERVICE_IDENTITY_ROLES and "
    "SERVICE_IDENTITY_EXCEPTIONS. dsar.staff_personhood's roster-level "
    "fail-closed condition now has a SQL analogue over the owner-principal "
    "population, but it is not a mirror of the staff roster: the certification "
    "universe is deliberately narrower. Nine database routines "
    "establish activity ownership inside trigger and procedure execution where "
    "Python is unreachable, so a database-resident eligibility boundary built "
    "before this register would execute a predicate missing one of E2's "
    "grounds -- a second interpretation of the contract in the deepest "
    "enforcement layer. "
    "IT IS EFFECTIVE-DATED RATHER THAN MUTABLE because a classification can "
    "legitimately change and the question 'what was this identity classified as "
    "when that assignment was made' must stay answerable. A declaration may be "
    "closed and succeeded; it may not be altered or deleted. "
    "CERTIFICATION IS GLOBAL AND FAILS CLOSED. It is measured over the active "
    "role-assignment principals represented by v_owner_principal -- the "
    "distinct minted owner_id values holding an active membership -- and not "
    "over employees, which is an upstream source of employee declarations "
    "rather than the certification universe. Certification requires a "
    "non-empty population, exactly one current declaration per principal, and "
    "exactly one active membership per principal; "
    "fn_personhood_certification_report exposes each condition separately and "
    "v_personhood_unclassified names the principals that block it. See "
    "docs/personhood_domain_contract_gate.md. "
    "Deploy BEFORE owner_eligibility_authority.sql, and populate it before "
    "deploying that file.")

_PENDING_OWNER_ELIGIBILITY_AUTHORITY = (
    "PENDING DEPLOYMENT -- authored 2026-09-18. The E2 contract as one "
    "authoritative executable definition: fn_owner_eligibility_state, seven "
    "states in strict precedence, with fn_owner_eligible redefined to derive "
    "from it rather than restate it. "
    "THE DEFECT IT CLOSES is two executable meanings for one ratified "
    "contract. Measured on 2026-09-17 the SQL predicate and the Python "
    "classifier agreed exactly, 516 eligible and 11,509 ineligible, while "
    "differing structurally in four places: the collision rule (Python refuses "
    "an identifier present in both employees and owners; SQL refused it only "
    "when the addresses also differed), membership multiplicity, personhood, "
    "and the not-granted/not-active distinction that a report needs because the "
    "two have different remedies. Agreement on one population is a property of "
    "that data, not of the definitions. "
    "CONDITION 3 IS DELIBERATELY ABSENT. Refusing a synthetic identity as the "
    "owner of attested-real work is not an E2 ground: it is a property of the "
    "pairing of an owner with particular work, and belongs with the selection "
    "engine, which has the work in hand. "
    "THIS FILE CHANGES THE BEHAVIOUR OF A DEPLOYED FUNCTION. Against an empty "
    "or uncertified personhood register it returns INELIGIBLE_NOT_HUMAN for "
    "every identity -- the correct reading of an uncertified roster, and one "
    "that would refuse all twelve currently eligible owners. Apply "
    "owner_personhood_register.sql first, populate it, confirm "
    "fn_personhood_roster_certified() returns true, and only then apply this.")

_PENDING_OWNERSHIP_POLICY = (
    "PENDING DEPLOYMENT -- authored 2026-09-18. The ownership selection "
    "carrier: ownership_policy and ownership_policy_history, versioned by the "
    "database and append-only, mirroring the deployed "
    "governance_action_policies pattern. "
    "WHY A SEPARATE CARRIER. governance_action_policies answers whether an "
    "action class may execute without a human, in what mode, owned by which "
    "authority -- and answers it well. It carries no conditions, no candidate "
    "population, no ordering and no tie-break, because it was never about "
    "selection. Its grain is one row per action type, which is the wrong grain "
    "for policies that compete for the same activity, and putting selection "
    "there would couple a routing-rule edit to an execution-authority row. "
    "Authority is therefore referenced rather than restated. "
    "PRECEDENCE IS A TOTAL ORDER over active policies, enforced by a unique "
    "partial index rather than resolved at execution time: whether two "
    "populations overlap is not decidable here, and a conflict settled by "
    "whichever row the planner returned first is not a governed decision. "
    "NO POLICY IS SEEDED. An empty carrier assigns nothing, which is the "
    "correct state until a policy is authored and approved. The selection "
    "engine that interprets these declarations is a later stage; this file "
    "changes no assignment behaviour.")

_PENDING_OWNERSHIP_SELECTION = (
    "PENDING DEPLOYMENT -- authored 2026-09-18. Stage 4 of the C1 ownership "
    "blueprint: the declared policy grammar and its validator, the decision "
    "evidence carriers ownership_decision and ownership_decision_candidate, "
    "deterministic sampling, and fn_select_activity_owner. "
    "THE GRAMMAR IS VALIDATED ON WRITE, by a check constraint calling "
    "fn_ownership_policy_defect, because a policy that failed only when the "
    "engine ran would fail during an assignment -- where the correct behaviour "
    "is to return NULL -- making a configuration error indistinguishable from "
    "the legitimate answer 'no eligible candidate'. The validator returns the "
    "reason rather than a boolean so the author is not left guessing which of "
    "four structures was wrong. "
    "EVERY CANDIDATE IS RECORDED, not only the winner. The existing router "
    "computes exactly this set and discards it; the ordering values it read are "
    "kept here because workload is counted live and is therefore not "
    "reproducible later. A policy version alone answers which rule ran, not why "
    "this person rather than that one. "
    "SAMPLING IS DETERMINISTIC in the activity and policy version, using md5 "
    "rather than hashtext: hashtext is an internal function with no "
    "cross-version stability guarantee, and a sample whose membership changes "
    "on an upgrade cannot be verified afterwards. "
    "THE ENGINE NEVER RAISES AND WRITES NO activities.owner_id. P3 requires the "
    "activity to survive a refused candidate, so a selection that could throw "
    "would turn an ownership failure into a work failure; and separating the "
    "decision from its application is what allows a shadow window to measure "
    "the engine without it changing anything. "
    "Deploy AFTER ownership_policy.sql. This file assigns nothing: it adds no "
    "trigger to activities and changes no existing write path.")

_PENDING_OWNERSHIP_SHADOW = (
    "PENDING DEPLOYMENT -- authored 2026-09-18. Stage 5 of the C1 ownership "
    "blueprint: an AFTER ROW observer on activities that records what the "
    "governed mechanism would have decided, and changes nothing. "
    "WHY A TRIGGER AND NOT AN APPLICATION CALL. The writer census found "
    "nineteen establishment-capable paths, nine of them resident in the "
    "database. An application-level observer cannot see any of those, so a "
    "shadow window built there would measure the two paths that already call "
    "the boundary and report the result as coverage. At the table, every "
    "writer passes through, including direct SQL. "
    "NON-MUTATION IS STRUCTURAL, NOT DISCIPLINARY. Postgres ignores what an "
    "AFTER trigger returns and gives it no way to alter the row that fired it, "
    "so the only way this could change ownership is by issuing its own UPDATE. "
    "It issues none, and the mutation suite adds one to prove that would be "
    "caught. An owner already recorded is preserved: an observer that "
    "corrected it would be an enforcement mechanism wearing a shadow's name, "
    "and reassignment is a separate authority. "
    "IT SWALLOWS ITS OWN FAILURES because the observation must never break the "
    "write it observes; a shadow that could abort an activity insert would "
    "turn a measurement into an outage. "
    "DEFAULT OFF. Deploying it changes nothing until app.ownership_shadow is "
    "set, matching the posture OWNER_ELIGIBILITY_ENFORCE sets for P3. "
    "Deploy AFTER ownership_selection.sql.")

_PENDING_OWNERSHIP_ATTRIBUTION = (
    "PENDING DEPLOYMENT -- authored 2026-09-18. Stage 6 of the C1 ownership "
    "blueprint: trusted writer attribution on the decision record, and the "
    "engine widened to capture it. "
    "THE GAP IT CLOSES is that the stage 5 observer proved an ownership event "
    "reached the governed boundary and could not say which writer caused it. "
    "The only context available was a free-form session setting any caller can "
    "write, and a decision attributed to crm_app names the role that executed "
    "the statement, not the business path that caused it. "
    "WRITER CLASS IS DERIVED, NOT DECLARED. pg_trigger_depth() distinguishes a "
    "statement issued directly from one issued inside another trigger, which is "
    "the distinction that matters: nine of the nineteen establishment paths are "
    "database-resident and an application cannot declare on their behalf. "
    "Neither value is settable by a caller. "
    "A SELF-REPORTED PATH IS RECORDED AND NEVER PROMOTED. writer_declared "
    "carries what the caller said; it never overrides writer_class, so a direct "
    "statement claiming to be a trigger is recorded as a direct statement that "
    "made the claim, and v_ownership_attribution_conflicts names it. The "
    "forgery becomes visible rather than effective. "
    "UNKNOWN IS A PERMITTED VALUE. Where attribution cannot be established it "
    "is recorded as unknown: an unknown writer is an evidence gap, a fabricated "
    "one is a false record that looks like evidence. "
    "Execution role, policy authority, human actor and activity owner are kept "
    "in separate columns because they are separate facts; one actor column is "
    "how a database role comes to stand for a human decision. "
    "Deploy AFTER ownership_shadow.sql.")


_APPLIED_LOCAL_CANCELLATION_REVERSAL = (
    "APPLIED TO LOCAL railwayl2 2026-10-05 22:28:16 -04 "
    "(schema_attestations id 171); NOT YET ON RAILWAY. "
    "Authored 2026-09-20. Reversing a cancellation, "
    "governed at the same boundary: fn_authorize_reversal, the reversal guard, "
    "and the operation discriminator that keeps the two apart. "
    "THE GAP IT CLOSES. The cancellation guard fires on a row ENTERING "
    "cancelled, so it said nothing about leaving. Measured: as the application "
    "role, a cancelled order could be moved to pending, processing, ready, "
    "shipped, delivered, completed or active by bare DML, with no authority, "
    "no principal, no reason and no record. "
    "WHY IT IS A DISTINCT OPERATION. A customer cancels their own order having "
    "proven possession; nobody self-serves an un-cancellation and no such flow "
    "exists, so reversal is staff authority. The operation is bound into the "
    "proposition hash, so a grant issued to cancel cannot be spent to "
    "un-cancel. "
    "A REVERSAL IS NOW AN EVENT. Nothing previously recorded un-cancelling: "
    "audit_log holds 5,024 cancel_by_agent rows and no reversal action, so the "
    "three reversals in the corpus are visible only as a contradiction between "
    "an order's status and its cancellation evidence. "
    "NO WINDOW IS DECIDED HERE. The 72 hours in undo() is an implementation "
    "artifact, not a ratified policy; governing who may reverse does not "
    "require deciding how long. Not applied to crmdb.")


_PENDING_CANCELLED_NOT_INVOICEABLE = (
    "PENDING DEPLOYMENT -- authored 2026-09-19. The invariant that a cancelled "
    "order must not become invoiceable, enforced at invoice creation. "
    "THE DEFECT IT CLOSES. An earlier guard refused the single transition "
    "cancelled -> Invoiced. That is not where invoices come from: "
    "trgfn_order_create_invoice fires when an order reaches 'shipped'. Reversal "
    "out of cancelled is ungoverned, so cancelled -> shipped created an invoice "
    "without meeting any guard, and cancelled -> pending -> Invoiced reached "
    "the invoiced status the same way. Four orders in the current corpus are "
    "cancelled and carry invoices totalling $2,594.21; those rows are "
    "historical and are not modified. "
    "WHY A STATUS PAIR CANNOT EXPRESS IT. OLD.status cannot say 'has ever been "
    "cancelled', because every intermediate state resets what OLD reports. The "
    "durable fact is a consumed cancellation authority record, which survives "
    "reversal; the current status is checked as well, as transitional cover for "
    "orders cancelled before this enforcement existed. "
    "ENFORCED AT THE CONSEQUENCE. A BEFORE INSERT trigger on invoices and on "
    "invoice_orders, so the order trigger that creates invoices on shipment, "
    "sp_accounting, and direct DML by the application role all meet it without "
    "any path being enumerated. "
    "REVERSAL IS NOT DECIDED HERE. An order may still leave the cancelled "
    "state exactly as before; only the invoice is refused. Not applied to crmdb.")


_APPLIED_LOCAL_CANCELLATION_ENFORCEMENT = (
    "APPLIED TO LOCAL railwayl2 2026-10-05 22:28:15 -04 "
    "(schema_attestations id 170); NOT YET ON RAILWAY. "
    "Authored 2026-09-19. The enforcement half of the "
    "cancellation boundary: cancellation_authorization, "
    "fn_authorize_cancellation, and the triggers that govern a row entering "
    "cancelled. "
    "THE DEFECT IT CLOSES. The application role holds arwd on orders on both "
    "the local database and production, with no row-level security and no "
    "SECURITY DEFINER routine in the cancellation surface. A bare UPDATE "
    "setting status to cancelled therefore succeeded, and revoking EXECUTE on "
    "any procedure did not change that, because the capability lives on the "
    "table rather than on the procedure. "
    "IT GOVERNS THE TRANSITION, NOT THE FUNCTION. The guard fires only when a "
    "row enters cancelled, so it reaches bare DML, the generic status writer, "
    "function indirection and any future caller, while leaving ordinary "
    "lifecycle progression, invoicing and total recalculation untouched. Six of "
    "the seven writers that touch order status never produce cancelled. "
    "THE CALLER CANNOT WRITE ITS OWN PROOF. The authorization table grants the "
    "application role SELECT only; the row is written by a SECURITY DEFINER "
    "function that records what fn_cancellation_authority returned, and a CHECK "
    "constraint independently refuses any verdict other than AUTHORIZED. "
    "Authority is scoped to the transaction that obtained it and is spent once. "
    "Installing this file changes cancellation behaviour: a caller that does "
    "not obtain authority can no longer cancel. It is not applied to crmdb.")


_APPLIED_LOCAL_CANCELLATION_AUTHORITY = (
    "APPLIED TO LOCAL railwayl2 2026-10-06 11:15:15 -04 "
    "(schema_attestations id 188); NOT YET ON RAILWAY. "
    "Authored 2026-09-19. The cancellation boundary: "
    "fn_cancellation_authority, the verdict and cancellable-state vocabularies, "
    "and the cancellation_path register. "
    "THE FINDING IT ANSWERS. A reconciliation of 298 governed cancellations "
    "established that verified_via is a field the action writes about itself "
    "and is not a reference to any verification record. Of 298 cancellations "
    "asserting OTP verification, 6 could be corroborated against "
    "order_cancel_verifications. "
    "EVIDENCE, NOT ASSERTION. Authority is established by a consumed, "
    "unexpired verification record bound to the exact order, over the stated "
    "channel, with the order still in a cancellable state at execution. "
    "Absence of evidence returns a refusal, never a pass. "
    "NO CALLER-SUPPLIED CLOCK. The evaluator takes no evaluation time, so a "
    "caller cannot ask what the verdict would have been at some other moment; "
    "execution-time revalidation is expressed as equality against current "
    "state rather than as a tolerance, because no staleness window has been "
    "decided. "
    "THE REGISTER CARRIES THE CENSUS. Six paths can reach or overwrite "
    "cancelled status; none binds a verification record, and three of them "
    "carry no cancellation control at all. Installing this file changes no "
    "cancellation behaviour -- nothing calls the function yet. Not applied to "
    "crmdb.")


_PENDING_SETTLEMENT_AUTHORITY = (
    "PENDING DEPLOYMENT -- authored 2026-09-14. Phase 1 of the ratified "
    "write-off financial control: settlement_events, an append-only ledger of "
    "named economic events, and the authority functions "
    "fn_settlement_record_payment and fn_settlement_record_write_off. "
    "THE DEFECT IT CLOSES is that receivable state is changed by writing a "
    "row: a caller that can insert into payments reduces a receivable, and "
    "trgfn_payment_before supplies 'credit card' and 'confirmed' when the row "
    "does not say otherwise, so an underspecified row becomes a confirmed card "
    "payment. Measured: on 2026-06-15 that mechanism recorded 57 relinquished "
    "residuals, $1,446.10 across 29 accounts, as money received. "
    "THE EVENT VOCABULARY IS CLOSED -- payment, write_off, recovery, reversal, "
    "refund -- with no general-purpose 'adjustment' member, because a "
    "general-purpose event reintroduces the ambiguity the vocabulary removes. "
    "One function per event rather than settle(type, ...): a generic entry "
    "point whose behaviour is chosen by a caller-supplied string is a "
    "privileged DML proxy, and whoever picks the string picks the economics. "
    "WRITE-OFF IS DEFINED BUT NOT EXECUTABLE. It fails closed, because the "
    "approved-proposition path and the collection-state model that separates "
    "the relinquished amount from the amount still collectible are later "
    "phases, and writing the balance now would execute a partial write-off as "
    "a full one. No threshold is consulted; the CFO has not set one and the "
    "fifty dollars in settle_immaterial_overdue.sql is not policy. "
    "PHASE 1 DOES NOT REVOKE DIRECT DML. Any role holding DML on payments can "
    "still settle a receivable without the authority. That exposure is left "
    "visible rather than partially closed, and closing it requires converting "
    "the remaining invoker-rights writers first: only 4 of 27 sp_* procedures "
    "are SECURITY DEFINER and sp_accounting is not, so a revoke that preceded "
    "the migration would take the application down."
)


_APPLIED_A3_INVOICE_ECONOMIC_INTEGRITY = (
    "APPLIED OUT-OF-BAND TO BOTH DATABASES. Railway 2026-09-14 13:41:00 UTC "
    "(schema_attestations id 23); local crmdb 2026-09-14 00:15:53 -04. "
    "Authored 2026-09-14. The A3 invoice economic "
    "integrity control: invoices.snapshot_sealed_at, an append-only "
    "invoice_economic_authorization table, and six triggers. "
    "THIS DECLARATION WAS STALE UNTIL 2026-09-23 and said PENDING DEPLOYMENT "
    "while the objects were live on both databases -- which is the precise "
    "failure this file exists to prevent, since every downstream check reads "
    "it as the truth about production. Corrected against a direct object "
    "probe of both databases: the column, the table, fn_invoice_line_set_hash "
    "and all five named triggers are present on each. "
    "THE INVARIANT is subtotal_amount = SUM(invoice_orders.line_total), "
    "evaluated at TRANSACTION COMMIT and not at INSERT -- measured, the "
    "invoice row is written first, a payment is created against it by "
    "trigger, and only then are its lines written; 446 payments exist that "
    "predate their invoice's lines, so an INSERT-time check would fail on "
    "every invoice ever produced. "
    "SEALING THE 2,102 EXISTING INVOICES IS DONE BY THE COLUMN DEFAULT, not "
    "by an UPDATE: ADD COLUMN ... DEFAULT now() gives pre-existing rows that "
    "value through the catalogue, and the default is dropped immediately so "
    "future invoices are born unsealed. NO STATEMENT WRITES AN ECONOMIC "
    "COLUMN OF ANY EXISTING RECORD. "
    "SEALED IS NOT A CERTIFICATE OF CORRECTNESS: ~188 historical invoices "
    "carry a generation defect corrected between May and June 2026 "
    "($35,108 overstated, $2,577 understated), two were issued at $0.00 "
    "against $398.75 of delivered goods, and one is a pytest artifact. Their "
    "financial disposition is open finance work and is not settled here. "
    "LIVE-PATH CHANGE: fn_recalc_order_totals, called by sp_orders, will be "
    "refused when it would rewrite a sealed invoice. Measured exposure is "
    "nil -- 0 of 4,310 invoice lines have drifted since May 2026. "
    "DELIBERATELY NOT PROMOTED to REQUIRED_MIGRATIONS, and the earlier "
    "instruction to promote on Railway application is superseded. That list "
    "is read by ledger_health(), which accounts a declared migration against "
    "a schema_migrations row; apply_sql records nothing there by design, so "
    "declaring this file would report a permanent shortfall for a migration "
    "that IS applied. A check that is permanently red teaches its reader to "
    "ignore it. The apply is evidenced by schema_attestations instead, which "
    "exists for exactly this path.")
_APPLIED_LOCAL_FINANCIAL_APPROVAL_INSERT_BOUNDARY = (
    "APPLIED TO LOCAL crmdb 2026-09-23 22:51:06 -04 "
    "(schema_attestations id 1470); NOT YET ON RAILWAY. "
    "Authored 2026-09-23. Re-binds two financial "
    "approval controls from BEFORE UPDATE to BEFORE INSERT OR UPDATE, and "
    "adds the TG_OP guard each one needs to survive an INSERT. "
    "THE DEFECT IT CLOSES: a row INSERTed already at status='executed' passed "
    "neither control. Measured on Railway 2026-09-23 -- five "
    "email.send_payment_reminder rows, policy class 'financial', "
    "created_at = decided_at = executed_at to the microsecond, every one with "
    "a null proposition_hash, a null assigned_executive_id and a null amount, "
    "the most recent dated that morning. Beside them, supervisor.emit_dunning "
    "rows DO name an executive, because they travel pending -> executed by "
    "UPDATE. One table, two populations, separated only by the verb. "
    "ONLY TWO OF THE THREE TRIGGERS MOVE. "
    "trgfn_approval_proposition_immutable stays UPDATE-only on purpose: it "
    "compares thirteen NEW fields to their OLD counterparts and there is no "
    "OLD on INSERT. It protects a decided row from being rewritten, which is "
    "a statement about change, not about state. "
    "IT CLOSES THE BYPASS AND NOT THE CONTROL. amount remains 0 on all 78 "
    "approvals that carry it, approval_authority_limit remains NULL for all "
    "five executives, and SAMPLED_REVIEW still has no completion record. "
    "EXPECT IT TO STOP A DAILY JOB. email.send_payment_reminder will fail on "
    "whichever database has this until it supplies a proposition and an "
    "eligible executive, or is reclassified on the evidence of what it "
    "actually does. That is the control working, and it is written down here "
    "so the first failure is recognised rather than diagnosed. "
    "VERIFIED ON LOCAL after apply: both triggers report BEFORE INSERT "
    "OR UPDATE and trg_approval_proposition_immutable is unchanged at "
    "BEFORE UPDATE; an INSERT-as-executed financial approval is refused "
    "by ck_financial_approval_requires_proposition, and a pending "
    "proposal naming an eligible owner is still accepted -- both probed "
    "in transactions that were rolled back, leaving no row behind. "
    "PRE-APPLY MEASUREMENT ON LOCAL: 1,523 email.send_payment_reminder "
    "rows born executed, of which 480 were written AFTER "
    "financial_proposition_binding.sql was applied on 2026-09-13. The "
    "control was deployed and the bypass kept writing through it. "
    "NOT PROMOTED TO REQUIRED_MIGRATIONS: apply_sql writes no "
    "schema_migrations row, so a declared out-of-band apply would leave "
    "ledger_health() permanently short by one. schema_attestations is the "
    "evidence path for this file.")


_APPLIED_FINANCIAL_PROPOSITION_BINDING = (
    "APPLIED OUT-OF-BAND TO BOTH DATABASES. Railway 2026-09-14 13:40:42 UTC "
    "(schema_attestations id 22); local crmdb first applied 2026-09-13 "
    "17:32:44 -04 and replayed five times while it was being developed. "
    "Authored 2026-09-13. Widens "
    "trgfn_approval_proposition_immutable from the five proposition* columns "
    "to the fields execution actually consumes -- params, amount, "
    "action_type, entity_type, entity_id -- and to the identity a decision is "
    "attributed to. Adds a delete guard for decided approvals, and requires a "
    "canonical proposition before an action whose policy class is 'financial' "
    "may be approved. "
    "THE MEASUREMENT: all five guarded columns were empty in all 1446 rows "
    "because nothing wrote them, while UPDATE of amount, params and "
    "decided_by on genuine executed approvals was accepted by direct SQL. The "
    "guarded set was narrower than the content it protected. "
    "NOT AN ACTIVATION. It constrains how an approval may change; it opens no "
    "execution path, and the financial-proposition requirement binds only "
    "actions a deployed policy already classifies 'financial'. "
    "THIS DECLARATION WAS STALE UNTIL 2026-09-23. Corrected against a direct "
    "object probe: all five proposition* columns, all three named triggers "
    "and governance_policy_changes are present on both databases. "
    "DELIBERATELY NOT PROMOTED to REQUIRED_MIGRATIONS, for the reason given "
    "on the A3 economic-integrity declaration above: apply_sql writes no "
    "schema_migrations row, so promoting an out-of-band apply would make "
    "ledger_health() permanently short by one. "
    "KNOWN GAP IT DOES NOT CLOSE: its three triggers are BEFORE UPDATE only, "
    "so a row INSERTed already at status='executed' passes none of them. "
    "Measured 2026-09-23: five email.send_payment_reminder rows, a financial "
    "action class, executed daily with a null proposition and no named "
    "executive. The remedy is a separate file, not an edit to this one.")
_APPLIED_LOCAL_SOFT_DELETED_AR = (
    "APPLIED TO LOCAL crmdb 2026-09-20; NOT YET ON RAILWAY. Adds the missing "
    "invoice-level is_deleted predicate to vw_invoices_ar and "
    "accounting_invoice_pipeline. "
    "THE DEFECT. Neither view filtered soft-deleted invoices. vw_invoices_ar "
    "ended FROM invoices i LEFT JOIN ... with no WHERE at all, while filtering "
    "is_deleted = false on PAYMENTS inside its own CTE -- honouring the flag for "
    "payments and ignoring it for invoices. Soft-deleting an invoice therefore "
    "removed it from nothing: 260 marked deleted, and both the AR Aging chart "
    "and the Accounting Summary continued to report $132,769.65, exactly "
    "sum(balance_due) over the view. "
    "LATENT UNTIL EXERCISED. No invoice had ever been soft-deleted before that "
    "date, so the omission had never cost anything. v_invoice_balance_drift "
    "already carried the predicate, which is why this is an omission rather "
    "than a decision. "
    "WHY THE VIEW AND NOT THE CONSUMERS: the reasoning in "
    "cancelled_invoices_are_not_receivable.sql, which fixed the neighbouring "
    "defect on the same view. Two consumer shapes exist and a consumer-side fix "
    "must find every one of them. "
    "PRODUCED BY READING THE LIVE VIEWS with pg_get_viewdef and re-emitting them "
    "with one predicate added, then diffing against the originals to confirm the "
    "predicate is the only change. Measured effect: outstanding $132,769.65 -> "
    "$71,994.65, paid rate 82.5% -> 91.5%, affecting exactly the 260 "
    "soft-deleted rows and nothing else.")


_PENDING_ACCOUNTING_INVOICE_PIPELINE = (
    "PENDING DEPLOYMENT -- authored 2026-09-29. The sole authoritative "
    "definition of the accounting_invoice_pipeline view, extracted from the "
    "accounting SP pack and reconciled with the soft-delete predicate. "
    "It exists as its own artifact because the statement is a CREATE OR REPLACE "
    "VIEW, so the artifact that executes last defines the view, and any artifact "
    "that restates all 145 lines to change one clause silently removes the "
    "clauses other artifacts contributed. Three sources carried a full "
    "definition and two of them were pending: the cancellation-authority "
    "reconciliation in sp/sp_accounting_v5e.sql and the invoice-level "
    "is_deleted predicate in soft_deleted_invoices_are_not_receivable.sql. "
    "Neither carried the other's clause, so both apply orders lost a "
    "correction and no order made both units correct. Ownership is now stated "
    "in one artifact instead of decided by execution sequence. "
    "The two historical artifacts are not amended. "
    "cancelled_invoices_are_not_receivable.sql and "
    "soft_deleted_invoices_are_not_receivable.sql remain byte-identical: each "
    "records a definition it did apply, and editing a record to tidy an "
    "architecture is the defect corrected here on 2026-09-29 for "
    "trg_fn_contacts_leads_accounts_touch.sql. They are superseded by "
    "application order, and neither is authoritative for future promotion. "
    "Apply it after invoice_cancellation.sql, which creates the table the view "
    "reads. The dependency is on that table alone; nothing in the cancellation "
    "set reads this view. "
    "It changes reported numbers. Cancelled invoices report a zero balance and "
    "a cancelled status, soft-deleted invoices leave the receivable "
    "population, and the GREATEST(1.00, one percent) settlement tolerance is "
    "removed under the T1 decision of 2026-09-15, so a residual within it is "
    "reported rather than rounded to settled. No column is removed. "
    "Promote to REQUIRED_MIGRATIONS in the same change that records its "
    "Railway application, and not before.")


_PENDING_GENERATE_INVOICE_POLICY = (
    "PENDING DEPLOYMENT -- authored 2026-09-12. One governance_action_policies "
    "row for accounting.generate_invoice: financial class, HUMAN_APPROVAL, CFO "
    "approves, CEO escalates, never auto-executes. Additive and idempotent; "
    "inserts only when the action_type is absent. "
    "IT DOES NOT CHANGE WHAT IS PERMITTED. An undeclared write capability "
    "already fails closed to the CEO with human approval, so this row moves the "
    "decision to the authority who owns the money and makes it explicit rather "
    "than making it safer. "
    "NO DEPLOY ORDER CONSTRAINT relative to the app: the capability is refused "
    "for want of its required identifiers whether or not this row exists, and "
    "the policy is read at decision time rather than at registration. "
    "Promote to REQUIRED_MIGRATIONS in the same change that records its Railway "
    "application, and not before.")

_PENDING_OWNER_REMIND_KIND = (
    "PENDING DEPLOYMENT -- authored 2026-09-12. Additive and idempotent: it "
    "widens the staff_email_ledger send vocabulary by one kind, "
    "alert_owner_remind, for the pre-deadline nudge to an alert's accountable "
    "owner. "
    "APPLY THIS BEFORE THE APP THAT EMITS THE KIND. The reverse order has "
    "already cost this system a record: on 2026-09-08 the owner-notice code "
    "shipped emitting alert_assigned before either EMAIL_KINDS or the CHECK "
    "admitted it, so the three owner emails raised on 2026-09-09 were SENT and "
    "NOT recorded -- begin_send falls open, which costs the send its ledger "
    "row and never the message. The absence of rows was then read for two days "
    "as the absence of mail. "
    "Promote to REQUIRED_MIGRATIONS in the same change that records its "
    "Railway application, and not before.")

_PENDING_ALERT_RESOLUTION_REQUIRED = (
    "PENDING DEPLOYMENT -- authored 2026-09-10 and applied to LOCAL only. "
    "Governed schema; promote to REQUIRED_MIGRATIONS in the same change that "
    "records its Railway application, and not before. PHASE 2 OF TWO and the "
    "LAST step of the sequence: it makes resolution_disposition REQUIRED to "
    "enter status='resolved', replacing trgfn_governance_alerts_lifecycle "
    "whole (CREATE OR REPLACE takes the entire body). "
    "APPLY ORDER, and it is not a preference: "
    "(1) governance_alert_ack_and_disposition.sql -- columns, no enforcement; "
    "(2) governance-mgmt.html -- starts SENDING the field, hand-deployed; "
    "(3) the app -- starts WRITING it; "
    "(4) THIS FILE -- starts REQUIRING it. "
    "Applying it before (3) makes every Resolve fail, which is the shape "
    "recorded above for 2026-09-08: the app shipped ahead of its schema, the "
    "governance decision path was down, and the startup log stayed green "
    "throughout. Nothing in CI can enforce the ordering because step (2) is "
    "untracked. NOT RETROACTIVE: the requirement is tested only on the "
    "transition INTO 'resolved', so rows resolved earlier keep their NULL and "
    "can still reach 'closed'.")

_PENDING_CORPUS_PROVENANCE = (
    "PENDING DEPLOYMENT -- authored 2026-08-31 and applied to LOCAL only. "
    "Governed schema; promote to REQUIRED_MIGRATIONS in the same change that "
    "records its Railway application. Creates corpus_provenance: which "
    "subjects are demonstration data and which are real. It exists because "
    "the question could not be answered -- seed_email_migration.sql rewrote "
    "EVERY address with no synthetic filter and kept no backup, so email "
    "domain proves nothing, and is_synthetic coverage runs 0%-54% by table. "
    "The rule CHECK is the control: the prohibited inferences (email domain, "
    "name similarity, is_email_verified, created_at clustering, model "
    "judgement) cannot be recorded at all.")

_PENDING_RETIRE_CUSTOMERS = (
    "PENDING DEPLOYMENT -- authored 2026-09-01 and applied to LOCAL only. "
    "Governed schema; promote to REQUIRED_MIGRATIONS in the same change that "
    "records its Railway application. Retires the legacy `customers` cluster "
    "-- remnants of a booking application that predates this CRM. Probed "
    "first, sp_cases-style: zero writers, zero FK dependents, zero views, and "
    "booking.py calls none of it. DROPS three zero-row tables and three orphan "
    "functions; RENAMES the two that hold data, because a rename is reversible "
    "in one statement and `customers` is not covered by governed_deletions. "
    "The disposition of the personal data it holds is deliberately NOT decided "
    "here -- see dsar.EXCLUDED, which records it as a holding state.")

_PENDING_SCHEMA_ATTEST = (
    "PENDING DEPLOYMENT -- authored 2026-09-01 and applied to LOCAL only. "
    "Governed schema; promote to REQUIRED_MIGRATIONS in the same change that "
    "records its Railway application. Creates schema_attestations, which "
    "closes the one gap every other integrity control here shares: they all "
    "live in the DECLARATION path and cannot see a change that never used the "
    "tooling. Proved 2026-08-31 by a hand-made vector column and HNSW index "
    "that passed every check. Deliberately a MIGRATION rather than a runtime "
    "ensure_table: a detector for undeclared schema changes must not make one.")

_PENDING_ERASURE_E8 = (
    "PENDING DEPLOYMENT -- authored 2026-09-01 and applied to LOCAL only. "
    "Governed schema; promote to REQUIRED_MIGRATIONS in the same change that "
    "records its Railway application. Executes disposition E8 (SETTLED: "
    "ERASE): permanently removes the personal data held in the two retired "
    "fossil tables, records the erasure in a new retired_table_dispositions "
    "ledger, and drops the emptied shells -- because both carry customer_id "
    "and account_id, so leaving them out of dsar.EXCLUDED while they exist "
    "would break export certification. IRREVERSIBLE by design: no restorable "
    "image, which is what distinguishes an erasure from a deletion.")

_PENDING_IDENTITY_CONFIRM = (
    "PENDING DEPLOYMENT -- authored 2026-09-01 and applied to LOCAL only. "
    "Governed schema; promote to REQUIRED_MIGRATIONS in the same change that "
    "records its Railway application. E6: separates match_method (how a pair "
    "was DISCOVERED -- name, email, phone are fine there) from confirm_method "
    "(what JUSTIFIES a merge -- only a deterministic FK or a named human). "
    "Found live: 20 candidates already recorded on normalized_name and email "
    "at confidence 0.85-0.99, with identity.materialize registered as an A2A "
    "capability. Nothing had merged; nothing prevented it. Also makes "
    "materialized_at unreachable without status='confirmed'.")

_PENDING_WORKFLOW_OWNER = (
    "PENDING DEPLOYMENT -- authored 2026-09-07 and applied to LOCAL only. "
    "Governed schema; promote to REQUIRED_MIGRATIONS in the same change that "
    "records its Railway application, and not before. Closes the P1 ownership "
    "defect: workflow_execute_action wrote activities with a NULL owner "
    "whenever the triggering entity had none (1,660 of 1,667 had no eligible "
    "accountable owner). Resolves through a DECLARED entity-type -> authority "
    "-> eligible-owner routing (owner decision C: at this scale authority is "
    "ownership) and FAILS CLOSED with a durable workflow_action_exceptions row "
    "when nothing resolves. Adds workflow_owner_routing, "
    "workflow_action_exceptions, fn_workflow_route_role/owner. The 153-line "
    "function body is carried over VERBATIM from the base schema; only the "
    "owner resolution differs.")

_PENDING_LINK_IDENTITY = (
    "PENDING DEPLOYMENT -- authored 2026-09-07 and applied to LOCAL only. "
    "Governed schema; promote to REQUIRED_MIGRATIONS in the same change that "
    "records its Railway application, and not before. N-01/N-02 of the "
    "2026-09-07 reassessment. Gives action_approvals the three facts a "
    "decision link must carry -- which executive it was minted for, when, and "
    "which issuance -- so an emailed token binds to a person instead of "
    "proving possession of a URL; adds the policy.widen action policy so that "
    "WEAKENING a governance control is itself a governed decision; and adds "
    "append-only governance_policy_changes, which records the authenticated "
    "actor, the value before and after, and the approval that authorised a "
    "widening. Historical email-link rows are NOT rewritten: a decision the "
    "system could not attribute stays unattributed.")

_PENDING_AUTH_OTP_TRUST_BOUNDARY = (
    "PENDING DEPLOYMENT -- authored 2026-10-04 and applied NOWHERE, not even "
    "locally. Operator-applied through apply_sql.py; promote to "
    "REQUIRED_MIGRATIONS in the same change that records its Railway "
    "application, and not before. "
    "WHAT IT IS. The one-time-password trust boundary. It creates "
    "auth_otp_challenges and auth_secrets and the two SECURITY DEFINER "
    "functions that are the only way to reach them, fn_otp_issue and "
    "fn_session_create_otp. crm_app is explicitly REVOKEd on both tables, "
    "because the default ACL grants it arwd on anything new, so the "
    "application holds no OTP state and can read neither a pending code nor "
    "the pepper that hashes it. "
    "THE PEPPER IS NOT IN THIS FILE and must never be. It is generated inside "
    "the database after this artifact is applied, so no plaintext exists in "
    "any repository file, .env, shell history or deployment configuration. "
    "Both functions fail closed until it exists. "
    "ORDER MATTERS AND IS NOT A PREFERENCE. This file must be applied BEFORE "
    "the router change that calls it ships, because that router calls "
    "fn_otp_issue and a deployment preceding this artifact breaks signup and "
    "verification for every user until it lands. The reverse order is inert: "
    "the objects exist and nothing calls them. "
    "IDEMPOTENT, tested by applying it twice to one database -- CREATE TABLE "
    "IF NOT EXISTS, CREATE UNIQUE INDEX IF NOT EXISTS, CREATE OR REPLACE "
    "FUNCTION and idempotent grants. It carries no outer transaction, so "
    "apply_sql.py owns the transaction and no statement can partially apply. "
    "sha256 a576382b0791b0a76e796582bafc2e7908880341f0f9e1d8738bfd0ff55cfffc, "
    "17444 bytes.")

_SCHEMA_OOB = (
    "Historical schema operation applied out-of-band; it never entered the "
    "governed chain. This records what is true, not that the objects are "
    "unimportant.")
_BACKFILL = (
    "Data backfill -- repairs rows that already exist. A clean database has "
    "nothing to repair, so replaying it would be wrong.")
_CORRECTION = (
    "One-time data correction -- targets rows that exist only in this "
    "database's history.")
_SEED = (
    "Data seed -- content, not schema. Seeding is an environment choice; a "
    "clean database is not incorrect without it.")
_DIAGNOSTIC = (
    "Diagnostic -- reads only and changes no state.")

# Dispositions normalised 2026-09-29. These entries stated their disposition
# in prose rather than naming one, which a structural check cannot read.
# The wording is carried across unchanged; only the reference is new.
#
# _DUPLICATE_DECLARATION names a file whose objects are already created and
# owned by another declared migration. It is not 'superseded': the owning
# migration was ledgered before this file was authored, so this file was
# never the source of those objects.
_APPLIED_EMPLOYEE_WORK_EMAIL_ACTIVATION = (
    "OUT OF BAND -- authorised activation applied to RAILWAY 2026-09-02. "
        "A clean database must not replay it: it would confer email on grants "
        "that environment does not have.")
_APPLIED_OWNERS_NO_IDENTITY_REUSE = (
    "APPLIED TO RAILWAY 2026-09-02. Promotable, not promoted -- see the "
        "note on activities_owner_no_fabrication.sql.")
_APPLIED_EMPLOYEES_PROVENANCE_ATTESTATION = (
    "APPLIED TO RAILWAY 2026-09-02. Stays out-of-band permanently: it "
        "records attestations about eight SPECIFIC identities, and a clean "
        "database has no such employees to attest about.")
_APPLIED_ACTIVITIES_OWNER_NO_FABRICATION = (
    "APPLIED TO RAILWAY 2026-09-02. Promotable to REQUIRED_MIGRATIONS, but "
        "NOT promoted here: apply_sql records no ledger row, so declaring "
        "it would make migrate --check report a chain the ledger cannot "
        "evidence. Answering that by writing a ledger row is exactly what "
        "the ledger exists to prevent, so promotion waits.")
_PENDING_POLICY_DELETION_LOG = (
    "PENDING DEPLOYMENT -- authored 2026-10-08 and applied to LOCAL railwayl2 "
    "only; NOT applied to Railway. Operator-applied; promote to "
    "REQUIRED_MIGRATIONS in the same change that records its Railway "
    "application and the read-back that proves it, and not before. "
    "WHAT IT IS. It registers governance_action_policies with the generic "
    "governed-deletion logger, so a deleted decision policy is archived into "
    "governed_deletions with its full row, actor and txid, and can be put back "
    "by restore_governed_deletion_row(). One trigger, no new function: "
    "log_governed_deletion takes the primary key column as TG_ARGV[0], and this "
    "table's key is the text column action_type. "
    "WHY IT IS NEEDED. action_class decides whether financial enforcement "
    "applies, and deleting the row removes it -- the proposition requirement, "
    "both executive requirements, and three application effects that resolve "
    "the class through _is_financial. Seven action types are classified "
    "financial in production, so the path is live. And the deletion left "
    "nothing behind: trg_gap_version_and_history fires BEFORE INSERT OR UPDATE "
    "only, and governance_action_policy_history cannot carry a deletion because "
    "after_state is NOT NULL. action_approvals has had a deletion log since "
    "2026-08-08; the table that decides how those approvals are enforced had "
    "none. "
    "NO DEPLOY-ORDER CONSTRAINT, unlike the vocabulary and console files "
    "elsewhere in this list. Nothing in the application reads this trigger, and "
    "it only observes a deletion, so applying it before or after any "
    "application change alters nothing the application does. "
    "AFTER DELETE, matching the four existing attachments: logging must never "
    "be able to block a write. trgfn_audit_row_history is the obvious-looking "
    "wrong choice here, because it requires a uuid primary key and raises "
    "otherwise. "
    "IDEMPOTENT, and guarded: DROP TRIGGER IF EXISTS before CREATE, a "
    "to_regclass check on the table, and an explicit check that "
    "log_governed_deletion exists so a fresh environment fails with a readable "
    "reason rather than inside CREATE TRIGGER. "
    "WHAT IT DOES NOT DO. It does not prevent a deletion, does not govern an "
    "UPDATE to action_class, and does not reduce the application principal's "
    "rights: crm_app retains INSERT, UPDATE and DELETE on the table.")


_PENDING_LOCAL_20260902 = (
    "PENDING DEPLOYMENT -- governed schema change applied locally "
        "2026-09-02, awaiting Railway. Promote to REQUIRED_MIGRATIONS after "
        "production has run it.")
_PENDING_LOCAL_20260909 = (
    "PENDING DEPLOYMENT -- applied locally 2026-09-09, not yet on Railway.")
_DUPLICATE_DECLARATION = (
    "Redundant declaration retained for the record. The three triggers it "
        "creates -- trg_contacts_touch, trg_leads_touch and trg_accounts_touch "
        "-- are already created and owned by touch_updated_at_convergence.sql, "
        "required at position 37 and ledgered on Railway 2026-08-28 16:13:52. "
        "This is a corpus classification decision. It does not assert that the "
        "triggers were applied outside the governed path, because they were "
        "not: that migration created them through scripts.migrate.")


# CORRECTED 2026-08-25 after re-examination. The earlier reason given here was
# "an incremental chain cannot be adopted one link at a time". That mechanism is
# WRONG: CREATE OR REPLACE FUNCTION is a TOTAL replacement, and
# notification_headline.sql carries a complete body byte-identical to the live
# function (4641 chars). Applying that file alone on a clean database would
# reproduce trg_fn_events_after_insert() exactly. These three files are a
# HISTORY, not an incremental dependency.
#
# THE REAL REASON THEY CANNOT BE ADOPTED is prerequisites. Between them they
# also define emit_event(), trgfn_events_emit_guard() and
# trg_events_before_insert -- and the tables they all attach to (events,
# event_queue, notifications, notification_messages, agent_event_subscriptions)
# are created by NO FILE IN THE CORPUS AT ALL, declared or otherwise. They exist
# only in the live databases. `CREATE TRIGGER ... ON events` cannot run where
# `events` does not exist, so declaring these would put a migration in the
# governed set that a clean database cannot execute.
#
# RESOLVED 2026-08-28. The paragraph above ends "adoption becomes possible only
# if the base schema enters the corpus", and it now has -- not in sql/, but as
# schema/00_base_schema.sql, the canonical baseline CI builds every run. It
# carries events, event_queue, emit_event() and the current
# trg_fn_events_after_insert body, so the prerequisite gap these three cited is
# gone and their REVIEW reason has expired.
#
# THE DISPOSITION IS STILL NOT "ADOPT", and the distinction matters. A clean
# database now receives those objects FROM THE BASELINE, already at their
# current bodies. Replaying these files on top would be redundant at best and
# conflicting at worst -- they are a record of how production reached that
# state, not a way to reproduce it. So they move to the historical category:
# not governed, not replayed, retained as evidence.
#
# WHAT THIS EPISODE SHOWS. The reason above named a checkable condition and
# nothing checked it; the condition changed and the text kept reading as
# current. A justification that can expire should be verified by something that
# runs, which is why the accompanying tests now assert the BASELINE contains
# these prerequisites rather than that sql/ does not.
_EVENT_HISTORY = (
    "Historical schema operation applied out-of-band; it never entered the "
    "governed chain. DISPOSITIONED 2026-08-28: the prerequisite gap that held "
    "the event trio in REVIEW is closed -- schema/00_base_schema.sql carries "
    "events, event_queue, emit_event() and the current "
    "trg_fn_events_after_insert body, so a clean database receives them from "
    "the baseline. Not adopted: replaying would be redundant or conflicting. "
    "Retained as historical record -- not governed, not replayed. ")
_CHAIN_1 = _EVENT_HISTORY + (
    "This file defines emit_event() and replaces trg_fn_events_after_insert().")
_CHAIN_2 = _EVENT_HISTORY + (
    "This file defines trgfn_events_emit_guard() and trg_events_before_insert "
    "ON events, and replaces trg_fn_events_after_insert().")
_CHAIN_3 = _EVENT_HISTORY + (
    "This file is the CURRENT production body of trg_fn_events_after_insert(), "
    "verified byte-identical on both databases and complete in itself.")
_ENCODING_REPAIR = (
    "Repairs seven functions whose non-ASCII literals were mangled on Railway "
    "-- five in executable literals, two in comments only. Out-of-band because "
    "it repairs damage on one database; a clean installation gets these "
    "functions from sp/. NOT a deploy-path fix: psql was tested through both "
    "shells and does not corrupt, so the damage is historical.")
_TRIGGER_BIND = (
    "Binds two business-rule triggers whose FUNCTIONS are already deployed on "
    "Railway but which were never attached there. Out-of-band because it "
    "repairs one database's missing bindings, and the base tables involved are "
    "not in this corpus at all.")

_CATCHUP = (
    "Catch-up operation -- a one-time reconciliation of Railway against local "
    "on 2026-08-05, by name and by purpose. A clean database must never replay "
    "it. Ledgered with an empty checksum, which stays as recorded.")

# filename -> why it is NOT a governed migration. Reasons beginning REVIEW are
# open questions for a human, not settled answers.
OUT_OF_BAND_SQL: Dict[str, str] = {
    "account_intelligence.sql": _SCHEMA_OOB,
    "accounting_invoice_pipeline.sql": (
        _PENDING_ACCOUNTING_INVOICE_PIPELINE),
    "accounts_enrichment_columns.sql": _SCHEMA_OOB,
    "accounts_firmographics_columns.sql": _SCHEMA_OOB,
    "activities_account_fk.sql": _SCHEMA_OOB,
    "addresses table.sql": _SCHEMA_OOB,
    "agent_bus_watermark.sql": _SCHEMA_OOB,
    "agent_capabilities.sql": _SCHEMA_OOB,
    "agent_console.sql": _SCHEMA_OOB,
    "agent_event_subscriptions_seed.sql": _SEED,
    "agent_playbooks.sql": _SCHEMA_OOB,
    "agent_sequences.sql": _SCHEMA_OOB,
    "agent_tuning.sql": _SCHEMA_OOB,
    "append_only_revokes.sql": _SCHEMA_OOB,
    "append_only_revokes_fix.sql": _SCHEMA_OOB,
    "ar_aging_realism.sql": _SCHEMA_OOB,
    "ar_collections_settle_88pct.sql": _CORRECTION,
    "assignable_identity.sql": _SCHEMA_OOB,
    "audit_log_immutability.sql": _SCHEMA_OOB,
    # E4 — the two structural guards on the membership and owner primitives
    # (one active membership per owner; one owner row per employee).
    #
    # PENDING DEPLOYMENT. Applied locally 2026-09-02, NOT yet on Railway. It
    # sits here rather than in REQUIRED_MIGRATIONS for the reason recorded
    # above: that list is a claim about what production has RUN, and declaring
    # it first would make `migrate --check` report a chain production has not
    # executed. Promote it only once Railway has it -- same path
    # promotions_coupons.sql and the touch triggers took.
    #
    # It is genuinely a governed schema definition: a clean database SHOULD
    # execute it, both indexes are idempotent, and neither depends on data a
    # fresh environment lacks. Nothing about it is a one-time repair.
    # Turns on work email for the seven granted employee identities and gives
    # them a Tier-2 route. AUTHORISED by the owner 2026-09-02.
    #
    # A DATA/CONFIG change, not schema, and RAILWAY-SCOPED: the seven grants
    # exist only there, so the UPDATE matches nothing on a local database.
    # Out-of-band is the correct disposition — a clean database must NOT
    # replay it, because a fresh environment has no such grants and should not
    # acquire email-enabled recipients by being created.
    "employee_work_email_activation.sql": (
        _APPLIED_EMPLOYEE_WORK_EMAIL_ACTIVATION),
    # An owner id may never equal the employee id it links to. Reusing one as
    # the other is exactly how the F1 collision was created, and it is the
    # shortest path to a working digest — so it is forbidden structurally
    # rather than by convention, before the first employee-linked owner exists.
    # 0 of 44 rows carry a link today, so nothing can violate it.
    #
    # PENDING DEPLOYMENT status is recorded at application time.
    "owners_no_identity_reuse.sql": (_APPLIED_OWNERS_NO_IDENTITY_REUSE),
    # P5 — the eight non-service employee identities attested SYNTHETIC by the
    # owner, 2026-09-02. corpus_provenance held ZERO rows for `employees`, so
    # they were unclassified; real-vs-synthetic cannot be reconstructed on this
    # corpus but it can be attested, and the table's CHECK already admits
    # rule='human_attested' for state='synthetic'.
    #
    # It is what keeps a P5 grant distinguishable from production
    # accountability: without it, eight demo personas would enter the eligible
    # population indistinguishably from real staff.
    #
    # A DECLARATION, not a repair — it changes no employee, owner or activity.
    # Eight uuids named individually so a future real hire cannot inherit it.
    "employees_provenance_attestation.sql": (_APPLIED_EMPLOYEES_PROVENANCE_ATTESTATION),
    # Removes trg_fill_activity_owner, the BEFORE INSERT OR UPDATE trigger
    # whose entire body fabricates activity ownership (contact -> account ->
    # created_by -> sentinel). It made the ratified P3 transition impossible:
    # a handler writing NULL got a sentinel-owned row back, visible on no
    # surface at all. Supersedes the intent recorded in
    # bind_missing_business_rule_triggers.sql, which bound it precisely to
    # prevent unowned activities -- the opposite of what P3 decided.
    #
    # Changes no existing row. The function is kept (unbound), so re-binding is
    # one statement.
    #
    # PENDING DEPLOYMENT. Applied locally 2026-09-02, not yet on Railway.
    "activities_owner_no_fabrication.sql": (_APPLIED_ACTIVITIES_OWNER_NO_FABRICATION),
    # E7 — the executive role-assignment link, under a truthful name.
    # Additive: adds owner_id, copies the four values across, constrains it to
    # owners. Does NOT drop employee_uuid (readers still on it) and does NOT
    # clear it (is_employee derives from it -- a separate decision).
    #
    # PENDING DEPLOYMENT. Applied locally 2026-09-02, not yet on Railway.
    "executives_owner_id_column.sql": (
        _PENDING_LOCAL_20260902),
    "owner_eligibility_guards.sql": (
        _PENDING_LOCAL_20260902),
    "auth_sessions.sql": _SCHEMA_OOB,
    "autocomplete_communication_activities.sql": _SCHEMA_OOB,
    "backfill_account_addresses.sql": _BACKFILL,
    "backfill_account_contact_info.sql": _BACKFILL,
    "backfill_account_firmographics.sql": _BACKFILL,
    "backfill_contact_addresses.sql": _BACKFILL,
    "backfill_contacts_data_quality.sql": _BACKFILL,
    "backfill_created_updated_by.sql": _BACKFILL,
    "backfill_lead_credentials.sql": _BACKFILL,
    "backfill_lead_firmographics.sql": _SCHEMA_OOB,
    "backfill_lead_owner_ids.sql": _BACKFILL,
    "backfill_missing_accounts.sql": _BACKFILL,
    "backfill_open_opp_margins.sql": _SCHEMA_OOB,
    "backfill_opportunity_amounts.sql": _BACKFILL,
    "backfill_order_totals.sql": _DIAGNOSTIC,
    "backfill_ownership.sql": _BACKFILL,
    "backfill_pending_orders_invoice.sql": _BACKFILL,
    "backfill_products_audit_seed.sql": _SEED,
    "backfill_reduce_ar_outstanding_90pct.sql": _BACKFILL,
    "backfill_reduce_ar_outstanding_round2.sql": _BACKFILL,
    "backfill_shipping_from_billing.sql": _BACKFILL,
    "backfill_synthetic_amounts.sql": _BACKFILL,
    "BACKFILL_zero_amount_orders_and_invoices.sql": _BACKFILL,
    "balance_account_statistics.sql": _CORRECTION,
    "breach_register.sql": _SCHEMA_OOB,
    "business_objectives.sql": _SCHEMA_OOB,
    "cancelled_invoices_are_not_receivable.sql": _SCHEMA_OOB,
    "case_escalation_bridge.sql": _SCHEMA_OOB,
    "case_lifecycle.sql": _SCHEMA_OOB,
    "ck_ar_digest_dedup_key.sql": _SCHEMA_OOB,
    "cleanup_20260725_migration_alert_flood.sql": _CORRECTION,
    "cleanup_order_statuses.sql": _CORRECTION,
    "cleanup_soft_deleted_payments.sql": _CORRECTION,
    "cleanup_status_case.sql": _CORRECTION,
    "consent_channels.sql": _SCHEMA_OOB,
    "create_sp_products_list_categories.sql": _SCHEMA_OOB,
    "crm_agent_memory.sql": _SCHEMA_OOB,
    "custom_agent_versions.sql": _SCHEMA_OOB,
    "custom_agents.sql": _SCHEMA_OOB,
    "custom_field_provenance.sql": _SCHEMA_OOB,
    "custom_field_typed.sql": _SCHEMA_OOB,
    "custom_fields.sql": _SCHEMA_OOB,
    "customer_memory.sql": _SCHEMA_OOB,
    "dedupe_accounts_by_name.sql": _CORRECTION,
    "dedupe_accounts_merge_ltd_variants.sql": _CORRECTION,
    "dedupe_addresses_by_parent_label.sql": _SCHEMA_OOB,
    "dedupe_contacts_by_name.sql": _CORRECTION,
    "delete_my_test_account.sql": _CORRECTION,
    "diag2_product_counts.sql": _DIAGNOSTIC,
    "diag_grocery_toys_categories.sql": _DIAGNOSTIC,
    "disable_lead_auto_credentials.sql": _SCHEMA_OOB,
    "dsar_requests.sql": _SCHEMA_OOB,
    "dsar_subject_requests.sql": _SCHEMA_OOB,
    "email_received_event.sql": _CORRECTION,
    "email_templates.sql": _SCHEMA_OOB,
    "embed_keys.sql": _SCHEMA_OOB,
    "employee_emails_to_emp_subdomain.sql": _CORRECTION,
    "employee_service_seed.sql": _SCHEMA_OOB,
    "content_embeddings_pgvector.sql": _PENDING_PGVECTOR,
    "corpus_provenance.sql": _PENDING_CORPUS_PROVENANCE,
    "governance_alert_ack_and_disposition.sql": _PENDING_ALERT_DISPOSITION,
    "governance_alert_resolution_required.sql": _PENDING_ALERT_RESOLUTION_REQUIRED,
    "staff_email_ledger_owner_remind_kind.sql": _PENDING_OWNER_REMIND_KIND,
    "governance_policy_generate_invoice.sql": _PENDING_GENERATE_INVOICE_POLICY,
    "a3_financial_state.sql": _PENDING_A3_FINANCIAL_STATE,
    "financial_approval_insert_boundary.sql": _APPLIED_LOCAL_FINANCIAL_APPROVAL_INSERT_BOUNDARY,
    "financial_proposition_binding.sql": _APPLIED_FINANCIAL_PROPOSITION_BINDING,
    "a3_invoice_economic_integrity.sql": _APPLIED_A3_INVOICE_ECONOMIC_INTEGRITY,
    "invoice_cancellation.sql": _PENDING_INVOICE_CANCELLATION,
    "owner_personhood_register.sql": _PENDING_OWNER_PERSONHOOD,
    "owner_eligibility_authority.sql": _PENDING_OWNER_ELIGIBILITY_AUTHORITY,
    "ownership_policy.sql": _PENDING_OWNERSHIP_POLICY,
    "ownership_selection.sql": _PENDING_OWNERSHIP_SELECTION,
    "ownership_shadow.sql": _PENDING_OWNERSHIP_SHADOW,
    "ownership_attribution.sql": _PENDING_OWNERSHIP_ATTRIBUTION,
    "cancellation_authority.sql": _APPLIED_LOCAL_CANCELLATION_AUTHORITY,
    "cancellation_enforcement.sql": _APPLIED_LOCAL_CANCELLATION_ENFORCEMENT,
    "cancelled_not_invoiceable.sql": _PENDING_CANCELLED_NOT_INVOICEABLE,
    "cancellation_reversal.sql": _APPLIED_LOCAL_CANCELLATION_REVERSAL,
    "soft_deleted_invoices_are_not_receivable.sql": _APPLIED_LOCAL_SOFT_DELETED_AR,
    "settlement_authority.sql": _PENDING_SETTLEMENT_AUTHORITY,
    "readonly_role.sql": _PENDING_READONLY_ROLE,
    "schema_attestations.sql": _PENDING_SCHEMA_ATTEST,
    "identity_confirm_evidence.sql": _PENDING_IDENTITY_CONFIRM,
    "auth_otp_trust_boundary.sql": _PENDING_AUTH_OTP_TRUST_BOUNDARY,
    "escalations.sql": _SCHEMA_OOB,
    "event_correlation_propagation.sql": _PENDING_CORRELATION,
    "event_types_voice_learning.sql": _CORRECTION,
    "executive_intelligence.sql": _SCHEMA_OOB,
    "expire_moot_courtesy_tasks.sql": _CORRECTION,
    "f915_contact_email_verified.sql": _SCHEMA_OOB,
    "f915_live_names.sql": _SCHEMA_OOB,
    "fix_all_mismatched_invoices.sql": _CORRECTION,
    "fix_bottleneck_backlog_2026_07.sql": _CORRECTION,
    "fix_category_assignments.sql": _CORRECTION,
    "fix_event_emit_guard.sql": _CHAIN_2,
    "fix_encoding_corrupted_functions.sql": _ENCODING_REPAIR,
    "bind_missing_business_rule_triggers.sql": _TRIGGER_BIND,
    "fix_event_queue_double_enqueue.sql": _CHAIN_1,
    "fix_image_urls_snacks_personal_pet.sql": _CORRECTION,
    "fix_inflated_invoices.sql": _CORRECTION,
    "fix_invoice_after_item_added.sql": _CORRECTION,
    "fix_lucas_tremblay_encoding.sql": _CORRECTION,
    "fix_mangled_dashes.sql": _SCHEMA_OOB,
    "fix_notification_lifecycle_trigger.sql": _SCHEMA_OOB,
    "fix_office_supplies_image_urls.sql": _CORRECTION,
    "fix_opportunity_closed_paid_status.sql": _CORRECTION,
    "fix_orphan_open_deals.sql": _CORRECTION,
    "fix_payment_received_event.sql": _SCHEMA_OOB,
    "fix_product_images.sql": _CORRECTION,
    "fix_relative_image_urls.sql": _CORRECTION,
    "governance_critic.sql": _SCHEMA_OOB,
    "governance_history_audit.sql": _SCHEMA_OOB,
    "governed_policy_deletion_log.sql": _PENDING_POLICY_DELETION_LOG,
    "governance_routing.sql": _SCHEMA_OOB,
    "guardrails_acl.sql": _SCHEMA_OOB,
    "identity_links.sql": _SCHEMA_OOB,
    "identity_trgm.sql": _SCHEMA_OOB,
    "improve_account_statistics.sql": _CORRECTION,
    "insert_30_electronics.sql": _CORRECTION,
    "insert_31_office_supplies.sql": _CORRECTION,
    "insert_32_grocery.sql": _CORRECTION,
    "insert_35_apparel.sql": _CORRECTION,
    "insert_35_health.sql": _CORRECTION,
    "insert_35_home.sql": _CORRECTION,
    "insert_50_electronics2.sql": _CORRECTION,
    "insert_electronics_images.sql": _CORRECTION,
    "insert_new_electronics.sql": _CORRECTION,
    "insert_product_images.sql": _CORRECTION,
    "insert_products.sql": _CORRECTION,
    "intelligence_v2.sql": _SCHEMA_OOB,
    "invoice_balance_drift_guard.sql": _SCHEMA_OOB,
    "job_ledger.sql": _SCHEMA_OOB,
    "kb_documents.sql": _SCHEMA_OOB,
    "kb_enrichment.sql": _SCHEMA_OOB,
    "kb_fix_automation_overreach.sql": _CORRECTION,
    "kb_fix_false_capability_claims.sql": _CORRECTION,
    "kb_gaps.sql": _SCHEMA_OOB,
    "kb_search_aliases.sql": _CORRECTION,
    "kb_seed_crm_product_docs.sql": _SEED,
    "kb_seed_crm_product_docs_round2.sql": _SEED,
    "kb_semantic.sql": _SCHEMA_OOB,
    "kb_update_cancel_policy.sql": _CORRECTION,
    "knowledge_base.sql": _SCHEMA_OOB,
    "lead_scoring_model.sql": _SCHEMA_OOB,
    "leads table.sql": _SCHEMA_OOB,
    "leads_enrichment_columns.sql": _SCHEMA_OOB,
    "leads_signup_consent.sql": _SCHEMA_OOB,
    "llm_usage.sql": _SCHEMA_OOB,
    "llm_usage_failover.sql": _SCHEMA_OOB,
    "lock_writes_to_admins.sql": _SCHEMA_OOB,
    "mark_old_notifications_read.sql": _CORRECTION,
    "mark_old_notifications_read_8k.sql": _CORRECTION,
    "marketing_ab.sql": _SCHEMA_OOB,
    "marketing_campaigns.sql": _SCHEMA_OOB,
    "mcp_servers.sql": _SCHEMA_OOB,
    "metric_decision_tz_fix.sql": _SCHEMA_OOB,
    "migration_add_role_to_leads.sql": _SCHEMA_OOB,
    "migration_auth_credentials_lead_id.sql": _SCHEMA_OOB,
    "migration_auth_credentials_nullable_account.sql": _SCHEMA_OOB,
    "migration_fix_endash_in_activities.sql": _CORRECTION,
    "migration_leads_soft_delete.sql": _SCHEMA_OOB,
    "migration_products_add_audit_columns.sql": _SCHEMA_OOB,
    "normalize_phones_to_e164.sql": _CORRECTION,
    "notification_headline.sql": _CHAIN_3,
    "opportunity_decided_at.sql": _SCHEMA_OOB,
    "owners_employee_link.sql": _SCHEMA_OOB,
    "product_image_table.sql": _SCHEMA_OOB,
    "promote_account_billing_address.sql": _CORRECTION,
    "provenance_expand.sql": _SCHEMA_OOB,
    "quotes.sql": _SCHEMA_OOB,
    "railway_catchup_20260805.sql": _CATCHUP,
    "railway_cutover_2026_07.sql": _SCHEMA_OOB,
    "railway_insert_ring_replacement.sql": _CORRECTION,
    "railway_insert_sony.sql": _CORRECTION,
    "rbac_roles.sql": _SCHEMA_OOB,
    "rebrand_email_templates_conscestra.sql": _CORRECTION,
    "redistribute_shipped_orders.sql": _CORRECTION,
    "registry_policies_trace.sql": _SCHEMA_OOB,
    "remove_personal_care_category.sql": _CORRECTION,
    "reorganize_personal_care.sql": _CORRECTION,
    "repair_stale_invoice_balances.sql": _CORRECTION,
    "replace_amazon_products.sql": _CORRECTION,
    "replace_ring_kindle.sql": _CORRECTION,
    "replace_synthetic_products.sql": _CORRECTION,
    "rescale_open_opportunity_amounts.sql": _CORRECTION,
    "reset_synthetic_email_verified.sql": _CORRECTION,
    "resolve_owner_id_fix.sql": _SCHEMA_OOB,
    "resolve_stale_overdue_events.sql": _CORRECTION,
    "restore_all_contacts_active.sql": _CORRECTION,
    "restore_synthetic_images.sql": _CORRECTION,
    "resync_lead_ratings.sql": _CORRECTION,
    "retire_n8n_legacy.sql": _SCHEMA_OOB,
    "retire_sp_admin_broken_modes.sql": _SCHEMA_OOB,
    "retire_sp_admin_data_cleanup.sql": _SCHEMA_OOB,
    "routing_rules.sql": _SCHEMA_OOB,
    "routing_signals.sql": _SCHEMA_OOB,
    "sdr_sessions.sql": _SCHEMA_OOB,
    "seed_account_activities.sql": _SEED,
    "seed_contact_activities.sql": _SEED,
    "seed_contact_activities_topup.sql": _SEED,
    "seed_email_migration.sql": _SCHEMA_OOB,
    "seed_kb_articles.sql": _SEED,
    "seed_kb_articles_round2.sql": _SEED,
    "session_memory.sql": _SCHEMA_OOB,
    # MIGRATION B, deliberately separate from the reminder allocation migration
    # below. Both touch `escalations` and they are not the same change: this one
    # closes an independent data-integrity leak that exists today, while that
    # one establishes state a future producer needs. Installed NOT VALID because
    # four historical test-origin rows violate it and their disposition is a
    # separate decision -- a capability migration must not quietly repair
    # history. PENDING DEPLOYMENT; applied locally 2026-09-09, not on Railway.
    "escalation_assigned_requires_claim_time.sql":
        _PENDING_LOCAL_20260909,
    # Escalation reminder allocation (docs/decision_escalation_reminder_state.md,
    # Option A). Two columns so that eligibility and ordinal allocation are ONE
    # atomic UPDATE -- the scheduling guarantee the ledger cannot provide,
    # because the ledger proves exactly-once for an ordinal it is GIVEN and
    # cannot decide which ordinal is next.
    #
    # PENDING DEPLOYMENT. Applied locally 2026-09-09, NOT on Railway. No
    # producer consumes these columns yet: `unclaimed_reminders()` does not
    # exist and is not authorised. Promote only after Railway has run it AND
    # the columns have been read back there directly.
    "escalation_reminder_allocation.sql":
        _PENDING_LOCAL_20260909,
    # Defect A (docs/remediation_plan_2026-09-09_defects_A_B_C.md). Widens the
    # ledger's send vocabulary to the canonical set so the identity boundary in
    # staff_email.py has nothing legitimate left to refuse.
    #
    # PENDING DEPLOYMENT. Applied locally 2026-09-09, NOT yet on Railway. It
    # sits here rather than in REQUIRED_MIGRATIONS for the reason recorded at
    # the top of that list: it is a claim about what production has RUN, and
    # `migrate --check` reads it as one. Promote it only after Railway has
    # executed it AND the constraint has been read back there directly --
    # revision 2 of the assessment could only INFER production's constraint
    # from the migration ledger, and promoting on an inference would repeat
    # exactly that gap.
    "staff_email_ledger_governance_kinds.sql":
        _PENDING_LOCAL_20260909,
    "settle_immaterial_overdue.sql": _CORRECTION,
    "telephony.sql": _CORRECTION,
    "tenants.sql": _SCHEMA_OOB,
    "tier1_audit_instrumentation.sql": _SCHEMA_OOB,
    # LOCAL-ONLY cleanup, so it never needs a Railway deployment: all five
    # functions are verified absent from production already. Dropping them
    # reduces drift rather than creating it. Not governed -- a clean database
    # built from the regenerated baseline never has them to drop.
    "drop_local_only_dead_functions.sql":
        _CORRECTION,
    # OUT-OF-BAND for the same structural reason as the drop above, but it is
    # NOT the same kind of change and the classification should not be read as
    # saying so. A clean database built from the regenerated baseline never
    # creates sp_cases, so there is nothing for a required migration to drop
    # -- that is why it stays out of REQUIRED_MIGRATIONS.
    #
    # The difference: Railway HAD it, so unlike the drop above this one needed
    # a production deployment. APPLIED TO RAILWAY 2026-08-28 and verified there
    # by reading pg_proc directly (0 rows) -- not by trusting deploy_sp.ps1's
    # own SUCCESS line. The stale-declaration check named the PENDING
    # DEPLOYMENT entry on the first run afterwards, which is the independent
    # signal; that entry is now deleted.
    "drop_sp_cases.sql":
        _CORRECTION,
    # BATCH A, same out-of-band reasoning: the regenerated baseline no longer
    # creates any of them, so a clean database has nothing to drop. Both DO
    # need a Railway apply, and each carries PENDING DEPLOYMENT entries in
    # postdeploy_verify.DECLARED_DRIFT until it lands there.
    #
    # TWO FILES, NOT ONE, and the split is the point. The seven in the first
    # cannot execute a statement. sp_ai_assist can -- update_lead_score and
    # update_case_summary were measured mutating leads.score, leads.rating and
    # cases.summary. Merging them would put a live write-path removal behind a
    # title that says cleanup, and reverting one would revert the other.
    "drop_dead_seed_fossils.sql":
        _CORRECTION,
    "drop_sp_ai_assist.sql":
        _CORRECTION,
    # A DATA BACKFILL, so it stays out-of-band permanently rather than being
    # promoted into REQUIRED_MIGRATIONS. I had planned to promote it; the
    # repository's own vocabulary says otherwise, and it is right: a clean
    # database built from the baseline has no unowned opportunities to repair,
    # so replaying this there would be a no-op pretending to be a migration.
    # 17 other files already carry exactly this disposition.
    #
    # Applied to both databases 2026-08-28. Kept SEPARATE from the write-path
    # fix so either can be audited or reverted alone. Restores the documented
    # opportunity invariant on OPEN rows only: 147 closed unowned
    # opportunities are a deliberate exclusion, because assigning an owner to
    # finished business rewrites who is recorded as having won or lost it.
    "backfill_open_opportunity_owner.sql": _BACKFILL,
    "trg_fn_contacts_leads_accounts_touch.sql": (
        _DUPLICATE_DECLARATION),
    "unified_comms_conversations.sql": _SCHEMA_OOB,
    "unified_comms_identity.sql": _SCHEMA_OOB,
    "update_product_images.sql": _CORRECTION,
    "update_product_images2.sql": _CORRECTION,
    "update_product_images_new.sql": _CORRECTION,
    "update_product_pricing.sql": _CORRECTION,
    "update_product_pricing_new.sql": _CORRECTION,
    "update_products.sql": _CORRECTION,
    "update_products_new.sql": _CORRECTION,
    "verify_contacts_with_orders.sql": _CORRECTION,
    "verify_order_test_contacts.sql": _CORRECTION,
    "voice_echo_probe.sql": _SCHEMA_OOB,
    "voice_flux_turn.sql": _SCHEMA_OOB,
    "voice_stt_shadow.sql": _SCHEMA_OOB,
    "welcome_letter_copy_v2.sql": _CORRECTION,
    "workflow_chain.sql": _SCHEMA_OOB,
    "workflow_idempotency.sql": _SCHEMA_OOB,
    "workflow_placeholders.sql": _SCHEMA_OOB,
    "workflow_revival.sql": _SCHEMA_OOB,
}

from app.core.artifact_paths import SQL_DIR as _SQL_DIR


def _dollar_quoted_spans(sql: str) -> "list[tuple[int, int]]":
    """Character ranges covered by $$...$$ / $tag$...$tag$ bodies.

    Needed because a function body or DO block may legitimately contain the
    words BEGIN and COMMIT -- PL/pgSQL's BEGIN is a block opener, not
    transaction control -- and rewriting those would corrupt the function."""
    spans, pos = [], 0
    tag_re = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
    while True:
        m = tag_re.search(sql, pos)
        if not m:
            return spans
        close = sql.find(m.group(0), m.end())
        if close == -1:                       # unterminated; treat as to-EOF
            spans.append((m.start(), len(sql)))
            return spans
        spans.append((m.start(), close + len(m.group(0))))
        pos = close + len(m.group(0))


def _outside(spans, i: int) -> bool:
    return not any(a <= i < b for a, b in spans)


_TXN_STMT = re.compile(r"^[ \t]*(BEGIN|COMMIT|END)[ \t]*;[ \t]*$",
                       re.IGNORECASE | re.MULTILINE)


def strip_outer_transaction(sql: str) -> "tuple[str, bool]":
    """Remove a file's OWN transaction control so the caller owns the transaction.

    SHARED BY BOTH APPLY PATHS ON PURPOSE. apply_sql.py discovered this first:
    our .sql files wrap themselves in BEGIN;...COMMIT; so they are atomic under
    psql and pgAdmin, which are autocommit by default. psycopg2 is NOT -- it has
    already opened a transaction, so the file's BEGIN is a no-op that merely
    warns and the file's COMMIT commits OUR transaction. A later rollback then
    warns 'no transaction in progress' and does nothing, so `--dry-run` printed
    "ROLLED BACK -- nothing changed" while having applied the file in full.

    IT REMOVES EVERY TOP-LEVEL STATEMENT, NOT JUST A MATCHED OUTER PAIR, and
    that widening was not cosmetic. The first version stripped a leading BEGIN
    and a COMMIT only at end-of-file. sql/metric_registry_migration.sql has
    BEGIN on line 31 and COMMIT on line 150 with 62 lines after it, so the
    BEGIN was removed and the COMMIT was left -- strictly worse than doing
    nothing, because the marker went and the early commit stayed. 15 of the 34
    declared migrations were in that shape, which means the atomicity guarantee
    migrate.py had just been given was false for nearly half of them.

    THIS WAS FOUND THE EXPENSIVE WAY. A mutation experiment routed that file
    through apply_sql.py with `--dry-run`; the mid-file COMMIT ended the
    transaction, the rollback covered only the tail, and a view silently
    reverted to an older definition -- caught by an unrelated metric test two
    steps later.

    Dollar-quoted bodies are skipped: PL/pgSQL BEGIN is a block opener, not
    transaction control, and rewriting a function body would corrupt it.

    Safe to merge into one transaction because every declared migration was
    checked for statements PostgreSQL forbids inside a transaction block
    (CREATE INDEX CONCURRENTLY, VACUUM, REINDEX, ALTER TYPE ADD VALUE): there
    are none. A future migration needing one must be applied deliberately
    outside the runner, not by loosening this.

    Returns (body, had_transaction_control). The file on disk is never modified
    and stays correct under psql."""
    spans = _dollar_quoted_spans(sql)
    out, last, had = [], 0, False
    for m in _TXN_STMT.finditer(sql):
        if not _outside(spans, m.start()):
            continue                          # inside a function body
        if m.group(1).upper() == "END":
            continue                          # END; is ambiguous -- leave it
        out.append(sql[last:m.start()])
        last = m.end()
        had = True
    out.append(sql[last:])
    return ("".join(out), had) if had else (sql, False)


def residual_transaction_control(sql: str) -> "list[str]":
    """Top-level BEGIN/COMMIT still present after stripping.

    The fail-closed companion. A caller promising atomicity must refuse a file
    that can still end its transaction mid-way, rather than promise something
    it cannot deliver -- an unverified guarantee is worse than none, because it
    stops people looking."""
    spans = _dollar_quoted_spans(sql)
    return [m.group(1).upper() for m in _TXN_STMT.finditer(sql)
            if _outside(spans, m.start()) and m.group(1).upper() != "END"]


class SqlDispositionError(RuntimeError):
    """A SQL file has no disposition, or two. Fail closed."""


def classify_sql_corpus(sql_dir: Optional[str] = None) -> Dict[str, Any]:
    """THE COMPLETENESS INVARIANT.

        set(sql/*.sql) == REQUIRED_MIGRATIONS union OUT_OF_BAND_SQL
        REQUIRED_MIGRATIONS intersect OUT_OF_BAND_SQL == empty

    Four ways to fail, each naming a different mistake:

      unclassified   a file nobody assigned a path -- the silent default this
                     mechanism exists to abolish
      both           a file claiming to be governed and not, which is not a
                     disposition but a contradiction
      missing_*      a name declared with no file behind it -- the manifest
                     describing something that does not exist

    Returns a report rather than raising, so each caller chooses its severity:
    `migrate.py --check` treats it as fatal, the release guard as advisory.

    THE DIRECTORY IS THE DENOMINATOR, deliberately. Computing this from the
    ledger would reproduce the blind spot, because the population at issue is
    exactly the files the ledger never saw.

    SKIPS CLEANLY WHERE sql/ IS ABSENT. /sql/ is gitignored, so a deployed
    container has no corpus. `present: False` means "not evaluated", which is
    not "clean" and must never be reported as passing."""
    # ---- the half that is checkable WITHOUT the corpus ---------------------
    # sql/ is deliberately not shipped and deliberately not in source control,
    # so a deployed container can never evaluate file presence. The MANIFEST
    # ships regardless, and two of the four failure modes are properties of the
    # manifest alone: a filename in both lists, or an entry with no usable
    # reason. Checking those in production turns a bare "not evaluated" line
    # into a real assertion -- a guard that only ever prints a skip is one
    # people stop reading.
    declared = set(REQUIRED_MIGRATIONS)
    out_of_band = set(OUT_OF_BAND_SQL)
    both = sorted(declared & out_of_band)
    dupes = sorted({m for m in REQUIRED_MIGRATIONS
                    if REQUIRED_MIGRATIONS.count(m) > 1})
    unreasoned = sorted(k for k, v in OUT_OF_BAND_SQL.items()
                        if not isinstance(v, str) or len(v.strip()) < 30)

    d = Path(sql_dir) if sql_dir else _SQL_DIR
    if not d.is_dir():
        return {"present": False,
                # None, never True: file presence was NOT evaluated, and an
                # absent denominator must not produce a confident pass.
                "ok": None,
                "manifest_ok": not (both or dupes or unreasoned),
                "declared": len(declared), "out_of_band": len(out_of_band),
                "both": both, "duplicates": dupes, "unreasoned": unreasoned,
                "needs_review": sorted(k for k, v in OUT_OF_BAND_SQL.items()
                                       if isinstance(v, str)
                                       and v.startswith("REVIEW")),
                "reason": f"no sql dir at {d} — file presence not evaluated"}

    on_disk = {p.name for p in d.glob("*.sql")}

    unclassified = sorted(on_disk - declared - out_of_band)
    missing_declared = sorted(declared - on_disk)
    missing_oob = sorted(out_of_band - on_disk)
    ok = not (unclassified or both or missing_declared or missing_oob
              or dupes or unreasoned)
    return {
        "present": True,
        "ok": ok,
        "on_disk": len(on_disk),
        "declared": len(declared),
        "out_of_band": len(out_of_band),
        "unclassified": unclassified,
        "both": both,
        "missing_declared": missing_declared,
        "missing_out_of_band": missing_oob,
        "duplicates": dupes,
        "unreasoned": unreasoned,
        # Named, not hidden: the open governance questions inside the
        # out-of-band set. Appearing here is not a failure.
        "needs_review": sorted(k for k, v in OUT_OF_BAND_SQL.items()
                               if v.startswith("REVIEW")),
    }


def disposition_of(filename: str) -> str:
    """'governed' | 'out_of_band' | 'unclassified' -- the whole vocabulary."""
    if filename in set(REQUIRED_MIGRATIONS):
        return "governed"
    if filename in OUT_OF_BAND_SQL:
        return "out_of_band"
    return "unclassified"


def require_disposition(filename: str, expected: str) -> None:
    """Refuse to apply a file down the wrong path. Raises SqlDispositionError.

    THIS IS THE BOUNDARY, not the manifest. A list nothing consults is
    documentation; the refusal is what makes the two paths real."""
    actual = disposition_of(filename)
    if actual == "unclassified":
        raise SqlDispositionError(
            f"{filename} has NO disposition. Add it to REQUIRED_MIGRATIONS "
            f"(governed schema definition, applied by migrate.py) or to "
            f"OUT_OF_BAND_SQL with the reason it is not one. Refusing to "
            f"guess -- guessing is how the trg_fn_events_after_insert chain "
            f"reached production unrecorded.")
    if actual != expected:
        other = "migrate.py" if actual == "governed" else "apply_sql.py"
        raise SqlDispositionError(
            f"{filename} is classified '{actual}' and this is the "
            f"'{expected}' path. Apply it with {other}, or change its "
            f"disposition deliberately -- in the same change that applies it.")


# The parameters that decide what an agent may SAY. A difference in any of these
# between two replicas is a policy difference, not a config nuance.
SAFETY_PARAMS: List[str] = [
    "MEMORY_ASSERT_FLOOR",
    "MEMORY_VERIFY_ROLES",
    "MEMORY_DUAL_APPROVALS",
    "MEMORY_HALF_LIFE_DAYS",
    "MEMORY_HL_STABLE",
    "MEMORY_HL_VOLATILE",
    "MEMORY_DORMANT_BELOW",
    "MEMORY_CLUSTER_SIM",
    "MEMORY_MAX_RECORDS",
    "CONTENT_INDEX_MIN_SIM",
    "PROVENANCE_TRUST_FLOOR",
    "EMBED_MODEL",
    "EMBED_DIMS",
    "METRICS_TZ",
]

# Presence-only: a fingerprint that embedded the key would leak it to anyone who
# can read the attestation.
SAFETY_SECRETS: List[str] = ["MEMORY_SIGNING_KEY"]


def ensure_table() -> bool:
    """Ensure the two deploy-state tables are USABLE. Returns True when they are.

    The original version conflated two outcomes that need opposite responses:

      * the tables exist and this role may not CREATE  -> perfectly fine
      * the tables are genuinely missing               -> a deployment fault

    Under the privilege separation `crm_app` has USAGE but not CREATE, and
    PostgreSQL checks CREATE permission BEFORE the IF NOT EXISTS short-circuit —
    so the statement fails with 'permission denied for schema public' even when
    the table is right there. The old code logged that at warning and returned
    False, which read as 'no deploy state available'. replica_attestations then
    silently recorded nothing from 2026-08-03 until it was noticed on 08-05.

    Returning False when the tables are present and writable is the bug. The
    inability to CREATE something that already exists is not a failure."""
    missing = _missing_objects()
    if not missing:
        return True                       # present and usable; CREATE not needed

    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS public.schema_migrations (
                        filename    text PRIMARY KEY,
                        applied_at  timestamptz NOT NULL DEFAULT now(),
                        applied_by  text,
                        checksum    text
                    );
                    CREATE TABLE IF NOT EXISTS public.replica_attestations (
                        replica       text PRIMARY KEY,
                        fingerprint   text NOT NULL,
                        params        jsonb NOT NULL DEFAULT '{}'::jsonb,
                        attested_at   timestamptz NOT NULL DEFAULT now()
                    );""")
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        # Genuinely absent AND uncreatable. This is a real deployment fault:
        # apply the migration. Named explicitly so the fix is obvious.
        logger.error(f"[deploy] MISSING and uncreatable by this role: "
                     f"{', '.join(missing)} — apply the migration that declares "
                     f"them. ({str(exc).splitlines()[0][:100]})")
        return False


def ledger_health() -> Dict[str, Any]:
    """Does the ledger account for every DECLARED migration?

    CORRECTED 2026-08-06. The first version compared the ledger against every
    *.sql file in sql/ — 196 of them — and reported 12.8% coverage and
    reliable=False. That was a false alarm from the wrong denominator: most of
    sql/ is stored procedures, seeds and one-off fixes, never meant to be
    tracked. The ledger tracks REQUIRED_MIGRATIONS, and both databases hold all
    25 of them.

    Two lessons, kept because they were expensive. A coverage metric is only as
    good as the set it divides by — a wrong denominator produces a confident
    number pointing at nothing. And I wrote that check while hunting misleading
    signals and made one, so this now names its own denominator in the output.

    What WAS real: migrations applied by hand in pgAdmin never call
    record_migration(), so the ledger can miss rows for migrations that ARE
    applied. That is a process gap, not a coverage gap, and the compensating
    control is the live schema comparison in scripts/postdeploy_verify.py."""
    recorded: set = set()
    files = set(REQUIRED_MIGRATIONS)
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT filename FROM public.schema_migrations")
                recorded = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception as exc:                                    # noqa: BLE001
        logger.warning(f"[deploy] ledger unreadable: {exc}")
        return {"readable": False, "reliable": False, "error": str(exc)[:120]}

    unrecorded = sorted(files - recorded)
    coverage = (len(recorded & files) / len(files)) if files else 0.0
    return {
        "readable": True,
        "denominator": "REQUIRED_MIGRATIONS (declared), not every file in sql/",
        "declared_migrations": len(files),
        "recorded_rows": len(recorded),
        "coverage": round(coverage, 3),
        "unrecorded": unrecorded,
        # Extra rows are fine and expected: a migration applied by hand and then
        # recorded appears here without being in the declared list.
        "reliable": not unrecorded,
        "authoritative_alternative": "compare live schemas — "
                                     "scripts/postdeploy_verify.py",
    }


def _missing_objects() -> List[str]:
    """Which of this module's tables are absent. Cheap catalog lookup."""
    out: List[str] = []
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                for t in ("schema_migrations", "replica_attestations"):
                    cur.execute("SELECT to_regclass(%s)", (f"public.{t}",))
                    if cur.fetchone()[0] is None:
                        out.append(t)
        finally:
            conn.close()
    except Exception as exc:                                    # noqa: BLE001
        logger.warning(f"[deploy] could not check state tables: {exc}")
    return out


def record_migration(filename: str, applied_by: str = "manual",
                     checksum: str = "") -> bool:
    """Record a manually-applied migration. A CHECKSUM IS NOW REQUIRED.

    THE SECOND WRITER. The empty-checksum defect was fixed in migrate.py, and
    this function was missed -- it took `checksum=""` as its DEFAULT and
    inserted it unguarded, so `record_migration("x.sql")` minted a brand new
    row carrying no integrity information at all. Two of the rows on Railway
    are in exactly that state, and nothing prevented a third.

    An empty string is the harmful middle of the vocabulary:

        NULL             never recorded, and reads honestly as absent
        'abc123...'      the content hash at apply time
        ''               satisfies NOT NULL while guaranteeing nothing --
                         the constraint looks enforced and is not

    So this refuses rather than writes. NULL is not the fallback either:
    Railway declares `checksum NOT NULL`, so "unknown" cannot be represented
    there at all, which is precisely why '' exists in its history. Refusing
    keeps a caller from minting more of them.

    A genuinely unknown checksum means the row should not be written by this
    function. Record the application out of band and leave the ledger silent,
    rather than adding a row that asserts nothing.

    The existing empty rows are NOT touched. Adopting today's hash for a
    2026-08-05 application would fabricate a historical claim -- see the
    specification's §8.6."""
    if not (checksum or "").strip():
        logger.error(
            f"[deploy] refusing to record {filename} with an empty checksum. "
            f"Pass the sha256 of the file as applied, or do not record the "
            f"row -- an entry that asserts nothing is worse than no entry.")
        return False
    ensure_table()
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO public.schema_migrations
                         (filename, applied_by, checksum)
                       VALUES (%s,%s,%s) ON CONFLICT (filename) DO NOTHING""",
                    (filename, applied_by, checksum.strip()))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[deploy] could not record migration: {exc}")
        return False


def applied_migrations() -> List[str]:
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT filename FROM schema_migrations ORDER BY applied_at")
                return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


def check_migrations() -> Dict[str, Any]:
    """Ordered status. `out_of_order` matters as much as `missing`: applying a
    later migration first is what silently disabled the indexer."""
    ensure_table()
    applied = applied_migrations()
    applied_set = set(applied)
    missing = [m for m in REQUIRED_MIGRATIONS if m not in applied_set]

    positions = {m: i for i, m in enumerate(REQUIRED_MIGRATIONS)}
    seen = [positions[m] for m in applied if m in positions]
    out_of_order = any(b < a for a, b in zip(seen, seen[1:]))

    return {"ok": not missing and not out_of_order,
            "required": REQUIRED_MIGRATIONS,
            "applied": applied,
            "missing": missing,
            "out_of_order": out_of_order,
            "note": ("apply the missing files in the order listed"
                     if missing else "schema is current")}


# ============================================================================
# SCHEMA ATTESTATION — did the schema move without a tool recording that it did?
# ============================================================================
#
# Every other integrity control in this module lives in the DECLARATION path.
# This one does not, because the gap it closes is precisely a change that never
# entered a declaration: on 2026-08-31 a vector column and an HNSW index were
# found on the local database, created by hand, in no migration and no ledger,
# absent from Railway, and every control passed.
#
# The question deliberately is NOT "is the schema correct" — that would require
# simulating 265 migration files and would report standing historical drift
# until somebody switched it off. It is "did the schema move, and does anything
# explain the movement".

def _schema_objects(cur) -> Dict[str, str]:
    """One hash per schema object. Named keys so a drift report can say WHAT.

    Function BODIES are included, not just signatures. The incident this
    detector exists for was three successive CREATE OR REPLACEs of one trigger
    function — same name, same arguments, different behaviour — which a
    signature-only fingerprint cannot see.

    Line endings are normalised because Railway stores prosrc with CRLF and
    local with LF. Within a single database that never changes, so this is
    defensive rather than necessary; it costs nothing and removes a whole class
    of spurious diff if a database is ever restored across platforms.
    """
    import hashlib as _h
    objs: Dict[str, str] = {}

    def _put(key: str, payload: str) -> None:
        objs[key] = _h.sha256(payload.replace("\r\n", "\n").encode("utf-8")
                              ).hexdigest()[:12]

    cur.execute("""
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_attribute a ON a.attrelid = c.oid
         WHERE n.nspname='public' AND c.relkind IN ('r','p')
           AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY c.relname, a.attname""")
    cols: Dict[str, List[str]] = {}
    for rel, col, typ, notnull in cur.fetchall():
        cols.setdefault(rel, []).append(f"{col} {typ}{' NN' if notnull else ''}")
    for rel, spec in cols.items():
        _put(f"table:{rel}", "|".join(spec))

    cur.execute("""
        SELECT p.oid::regprocedure::text, pg_get_functiondef(p.oid)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname='public' AND p.prokind IN ('f','p')""")
    for sig, body in cur.fetchall():
        _put(f"function:{sig}", body or "")

    cur.execute("""
        SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid)
          FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname='public' AND NOT t.tgisinternal""")
    for rel, tg, dfn in cur.fetchall():
        _put(f"trigger:{rel}.{tg}", dfn or "")

    cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public'")
    for name, dfn in cur.fetchall():
        _put(f"index:{name}", dfn or "")

    cur.execute("""
        SELECT c.conname, pg_get_constraintdef(c.oid)
          FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
         WHERE n.nspname='public'""")
    for name, dfn in cur.fetchall():
        _put(f"constraint:{name}", dfn or "")

    return objs


def _attestation_conn(dsn: Optional[str] = None):
    """Connect to the database being ATTESTED, not to whichever one this
    process happens to be configured for.

    THE DEFECT THIS CLOSES, in full because it was subtle and shipped:
    `apply_sql --target railway` applied a migration to production and then
    reported "schema attested" — having fingerprinted the LOCAL database,
    because this module resolved its own connection from DB_DSN. The apply was
    correct and the record was about the wrong database. Worse, Railway got no
    attestation at all, so the drift detector was blind on the one database it
    most needed to watch.

    A caller with a target DSN must pass it. Callers with none keep the old
    behaviour, which is right for the application itself.
    """
    if not dsn:
        return get_connection()
    conn = psycopg2.connect(dsn)
    conn.set_client_encoding("UTF8")
    return conn


def schema_fingerprint(dsn: Optional[str] = None) -> Dict[str, Any]:
    """Composite hash of every object in the public schema, plus the parts."""
    import hashlib as _h
    conn = _attestation_conn(dsn)
    try:
        with conn.cursor() as cur:
            objs = _schema_objects(cur)
            cur.execute("SELECT current_database()")
            db = cur.fetchone()[0]
    finally:
        conn.close()
    blob = "\n".join(f"{k}={v}" for k, v in sorted(objs.items()))
    return {"fingerprint": _h.sha256(blob.encode("utf-8")).hexdigest()[:16],
            "objects": objs, "object_count": len(objs), "database": db}


def read_only_refusal(dsn: Optional[str] = None,
                      label: str = "the target") -> Optional[str]:
    """A sentence explaining why this connection cannot apply DDL, or None.

    WHY THIS EXISTS. D-08 repointed RAILWAY_DB_URL at `crm_readonly`, which
    carries `default_transaction_read_only=on`. Every Railway-targeting tool
    reads that one variable, so `apply_sql --target railway` and
    `migrate --target railway` -- the sanctioned path for a governed migration
    -- now fail with libpq's

        cannot execute ALTER TABLE in a read-only transaction

    which describes the symptom and hides the cause. Nothing in it mentions
    roles, D-08, or that the DSN was downgraded deliberately, so an operator
    meets a puzzle instead of a stated policy. This says the policy instead.

    Best-effort and fail-open: if the check itself cannot run, it returns None
    and the apply proceeds to fail the old way. Refusing an apply because a
    diagnostic could not connect would be worse than the message it replaces.
    """
    try:
        conn = _attestation_conn(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user, "
                            "current_setting('default_transaction_read_only')")
                user, read_only = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:                                       # noqa: BLE001
        logger.debug(f"read-only preflight skipped: {exc}")
        return None
    if str(read_only).lower() not in ("on", "true", "yes"):
        return None
    nl = chr(10)
    return (f"{label} resolves to the role {user!r}, which is READ-ONLY "
            f"(default_transaction_read_only=on).{nl}"
            f"Nothing was applied.{nl}{nl}"
            f"This is D-08 working as intended: the default connection for "
            f"this environment cannot write, so an accidental apply is "
            f"impossible. A governed migration needs a privileged DSN supplied "
            f"deliberately for the single invocation -- set the target DSN in "
            f"the shell for this command only, and close the shell afterwards, "
            f"rather than writing a superuser DSN into .env.")


def record_schema_attestation(source: str, detail: str = "",
                              dsn: Optional[str] = None) -> Dict[str, Any]:
    """Record what the schema looks like now, because a TOOL just changed it.

    Called by migrate.py and apply_sql.py after a successful apply. Everything
    that shifts the fingerprint without leaving one of these is, by
    construction, a change that used neither door.

    Best-effort: a failure to attest must never fail the migration that
    succeeded. The consequence is one unexplained-looking drift on the next
    check, which is a false positive in the safe direction.
    """
    fp = schema_fingerprint(dsn)
    try:
        conn = _attestation_conn(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schema_attestations (source, fingerprint, "
                    "objects, detail, database) VALUES (%s,%s,%s,%s,%s)",
                    (source[:80], fp["fingerprint"], json.dumps(fp["objects"]),
                     detail[:500] or None, fp["database"]))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "fingerprint": fp["fingerprint"],
                "objects": fp["object_count"], "database": fp["database"]}
    except Exception as exc:
        # NAMES THE DATABASE IT FAILED ON. The mis-targeting this parameter
        # fixes was invisible partly because the message said only "could not
        # record" — an operator reading it had no way to notice it was talking
        # about the wrong database.
        logger.warning(f"could not record schema attestation for "
                       f"{fp.get('database')!r}: {exc}")
        return {"ok": False, "error": str(exc)[:200],
                "fingerprint": fp["fingerprint"], "database": fp.get("database")}


def schema_drift(dsn: Optional[str] = None) -> Dict[str, Any]:
    """Has the schema moved since the last time a tool recorded it?

    Returns the object names that were added, removed or altered — naming them
    is the difference between a detector somebody acts on and one they mute.

    NO ATTESTATION AT ALL is reported as `unknown`, never as clean. A database
    that has never been attested has not been shown to be undrifted; saying
    otherwise would be the absence of evidence dressed as evidence of absence,
    which is the failure this codebase's outcome model exists to forbid.
    """
    out: Dict[str, Any] = {"ok": True, "unexplained": False, "state": "unknown"}
    try:
        live = schema_fingerprint(dsn)
        conn = _attestation_conn(dsn)
        try:
            with conn.cursor() as cur:
                # FILTERED ON `database`, so a fingerprint taken against the
                # wrong database can never be read as drift in this one. The
                # targeting bug that motivated this is fixed above; the filter
                # stays because a detector whose correctness depends on every
                # caller passing the right DSN is one careless call site away
                # from silence. Rows written before the column existed carry
                # NULL and are accepted for this database — they were, by
                # construction, written by the only writer there was.
                cur.execute(
                    "SELECT source, fingerprint, objects, recorded_at "
                    "FROM schema_attestations "
                    "WHERE database = %s OR database IS NULL "
                    "ORDER BY recorded_at DESC, id DESC LIMIT 1",
                    (live["database"],))
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "state": "unknown", "unexplained": False,
                "error": str(exc)[:200]}

    out["live_fingerprint"] = live["fingerprint"]
    out["object_count"] = live["object_count"]
    out["database"] = live["database"]
    if not row:
        out["state"] = "never_attested"
        out["detail"] = ("no attestation recorded — run "
                         "deploy_state.record_schema_attestation('baseline') "
                         "once this schema is believed correct")
        return out

    source, fp, objs, at = row
    out["last_attested"] = {"source": source, "fingerprint": fp,
                            "at": at.isoformat() if at else None}
    if fp == live["fingerprint"]:
        out["state"] = "clean"
        return out

    prev = objs if isinstance(objs, dict) else json.loads(objs or "{}")
    now = live["objects"]
    out["state"] = "drifted"
    out["unexplained"] = True
    out["added"] = sorted(set(now) - set(prev))[:50]
    out["removed"] = sorted(set(prev) - set(now))[:50]
    out["altered"] = sorted(k for k in set(prev) & set(now)
                            if prev[k] != now[k])[:50]
    out["detail"] = (
        f"{len(out['added'])} added, {len(out['removed'])} removed, "
        f"{len(out['altered'])} altered since {source} attested at "
        f"{out['last_attested']['at']}. If these were applied by hand, apply "
        f"them through migrate.py or apply_sql.py instead; if they are correct "
        f"and reviewed, record a new attestation naming who reviewed them.")
    return out


def safety_fingerprint() -> Dict[str, Any]:
    """Hash of the parameters that decide what may be asserted."""
    params = {k: os.getenv(k, "") for k in SAFETY_PARAMS}
    # Secrets contribute PRESENCE only — never their value.
    for k in SAFETY_SECRETS:
        params[f"{k}__set"] = "1" if os.getenv(k, "").strip() else "0"
    blob = json.dumps(params, sort_keys=True)
    return {"fingerprint": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16],
            "params": params}


def attest(replica: Optional[str] = None) -> Dict[str, Any]:
    """Record this process's safety fingerprint so replicas can be compared."""
    ensure_table()
    fp = safety_fingerprint()
    name = replica or f"{socket.gethostname()}:{os.getpid()}"
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO replica_attestations (replica, fingerprint, params)
                       VALUES (%s,%s,%s::jsonb)
                       ON CONFLICT (replica) DO UPDATE SET
                         fingerprint=EXCLUDED.fingerprint,
                         params=EXCLUDED.params, attested_at=now()""",
                    (name, fp["fingerprint"], json.dumps(fp["params"])))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[deploy] attestation failed: {exc}")
    return {"replica": name, **fp}


def consensus(max_age_minutes: int = 60) -> Dict[str, Any]:
    """Do all recently-seen replicas agree on safety policy?

    Divergence is reported with the SPECIFIC parameters that differ, because
    "replicas disagree" is not actionable and "replica B has
    MEMORY_ASSERT_FLOOR=0.1" is."""
    ensure_table()
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT replica, fingerprint, params, attested_at
                         FROM replica_attestations
                        WHERE attested_at > now() - (%s || ' minutes')::interval
                        ORDER BY attested_at DESC""", (str(max_age_minutes),))
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    if not rows:
        return {"ok": True, "replicas": 0, "note": "no recent attestations"}

    fingerprints = {r[1] for r in rows}
    diverging: Dict[str, List[Any]] = {}
    if len(fingerprints) > 1:
        keys = set().union(*[set(r[2].keys()) for r in rows])
        for k in sorted(keys):
            vals = {json.dumps(r[2].get(k)) for r in rows}
            if len(vals) > 1:
                diverging[k] = sorted(vals)

    return {"ok": len(fingerprints) == 1,
            "replicas": len(rows),
            "fingerprints": sorted(fingerprints),
            "diverging_params": diverging,
            "detail": [{"replica": r[0], "fingerprint": r[1],
                        "attested_at": r[3].isoformat()} for r in rows]}


router = APIRouter(tags=["deploy-state"])


@router.get("/deploy/migrations")
def deploy_migrations():
    return check_migrations()


@router.get("/deploy/safety-fingerprint")
def deploy_fingerprint():
    return attest()


@router.get("/deploy/consensus")
def deploy_consensus(max_age_minutes: int = 60):
    """Do all live replicas apply the same safety policy?"""
    return consensus(max_age_minutes)
