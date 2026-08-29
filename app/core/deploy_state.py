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
    "escalations.sql": _SCHEMA_OOB,
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
    "settle_immaterial_overdue.sql": _CORRECTION,
    "telephony.sql": _CORRECTION,
    "tenants.sql": _SCHEMA_OOB,
    "tier1_audit_instrumentation.sql": _SCHEMA_OOB,
    # LOCAL-ONLY cleanup, so it never needs a Railway deployment: all five
    # functions are verified absent from production already. Dropping them
    # reduces drift rather than creating it. Not governed -- a clean database
    # built from the regenerated baseline never has them to drop.
    "drop_local_only_dead_functions.sql":
        "One-time data correction -- targets rows that exist only in this "
        "database's history.",
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
