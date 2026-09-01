"""GDPR Art. 15 (access) and Art. 20 (portability) — data subject export.

The system could already ERASE a data subject (erase_verifications_for_entity,
audited, authorised) and could not GIVE THEM THEIR DATA. Erasure without access
is half of Chapter 3, and it is the half that is easier to build, because
deleting needs no completeness proof and disclosure does.

WHAT MAKES THIS HARD IS NOT THE QUERYING
----------------------------------------
An export that quietly misses a table looks exactly like a complete one. You
cannot tell by reading the output — it is a JSON file full of the subject's
data either way. So the risk here is not a broken query, it is a confident
answer built on an incomplete manifest, which is the same failure that let a
missing coupons table masquerade as "no such coupon" for fifteen days.

The defence is that the manifest is CHECKED AGAINST THE SCHEMA, not trusted.
Every public table carrying a subject-link column must be declared either
INCLUDED or EXCLUDED-with-a-reason. A table nobody declared is not silently
skipped: it makes the export refuse to certify itself (`strict=True`, the
default). Adding a table that holds personal data therefore breaks DSAR loudly
at the next request instead of quietly narrowing what a subject receives.

THIRD PARTIES (Art. 15(4))
--------------------------
"The right to obtain a copy shall not adversely affect the rights and freedoms
of others." A contact belongs to an account, and an account can hold several
contacts. Exporting everything under account_id for one of them would hand that
person their colleagues' data. So account-scoped sections are released only
when the subject is the ONLY contact on the account; otherwise they are
withheld, and the withholding is reported in the export rather than hidden.

    python -m app.core.dsar --contact <uuid> --requested-by dpo@example.com
    python -m app.core.dsar --email someone@example.com --out export.json
    python -m app.core.dsar --coverage        # manifest vs live schema
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from app.core.database import get_connection

logger = logging.getLogger("dsar")

# Columns that identify a data subject anywhere in the schema. The coverage
# check scans for these, so a new table holding one is detected automatically.
SUBJECT_COLUMNS = ("contact_id", "account_id", "lead_id", "customer_id",
                   "email", "phone", "person_id", "subject_id", "user_email",
                   # `recipient_email` added 2026-08-21. It was NOT here, and
                   # the consequence was silent: order_cancel_verifications
                   # stores the address a cancellation code was mailed to, and
                   # coverage() could not see the table at all -- so it was
                   # neither declared nor reported as undeclared. A blind spot
                   # is worse than a failure, because a failure gets fixed.
                   #
                   # This is the same shape as the `identifier` note on
                   # consent_state below: the matcher finds columns by NAME, so
                   # any new spelling of "an address belonging to a person" is
                   # invisible until it is listed here.
                   "recipient_email")

# account_id identifies an ORGANISATION, which may contain other people.
# Everything else identifies the individual.
ACCOUNT_SCOPED = {"account_id"}


# ============================================================================
# MANIFEST — every subject-linked table, declared
# ============================================================================
# DIRECT: table -> the subject columns to match on.
DIRECT: Dict[str, Tuple[str, ...]] = {
    # The subject's own records
    "contacts":                   ("contact_id", "email", "phone"),
    "leads":                      ("lead_id", "email", "phone"),
    "accounts":                   ("account_id",),
    "channel_identities":         ("account_id",),
    # Their activity
    "activities":                 ("contact_id", "lead_id", "account_id"),
    "activity_participants":      ("contact_id",),
    "cases":                      ("contact_id", "account_id"),
    "conversations":              ("account_id",),
    # RETIRED 2026-09-01: appointments, call_logs, call_state,
    # invalid_phones_log and customers were all keyed by `customer_id` — the
    # remains of a booking application that predates this CRM. Nothing wrote
    # any of them (the three call/appointment tables held zero rows;
    # invalid_phones_log's eight were all logged on 2025-10-22), and live
    # booking writes `activities`. They left the manifest together because
    # `customers` was the RESOLVER for the others' subject key: retiring it
    # alone would have left four sections in the manifest that no export could
    # ever reach — a coverage claim with nothing behind it.
    # Commercial record
    "opportunities":              ("contact_id", "account_id"),
    "orders":                     ("contact_id", "account_id"),
    "quotes":                     ("contact_id", "account_id"),
    "invoices":                   ("contact_id", "account_id"),
    "payments":                   ("contact_id", "account_id"),
    "coupon_redemptions":         ("contact_id", "account_id"),
    "price_match_requests":       ("contact_id", "account_id"),
    # What we sent them about their orders. Undeclared from 2026-08-15 (when the
    # table shipped) until 2026-08-21 -- caught by this check on both databases,
    # which is the check doing its job, just late. It records every lifecycle
    # email: which address, which template, whether the provider accepted it.
    # A subject asking what we hold about them is entitled to that.
    "order_notifications":        ("contact_id", "account_id", "recipient_email"),
    # Marketing and consent
    "marketing_sends":            ("contact_id", "account_id", "email"),
    "email_suppression":          ("email",),
    # Channel-aware consent (Axis 6 V1). `email`/`phone` are GENERATED from
    # `identifier`, which is what makes these matchable here at all — a bare
    # `identifier` column would be invisible to this matcher and the subject's
    # own consent record would be withheld from their Art. 15 export.
    "consent_state":              ("email", "phone"),
    "consent_log":                ("email", "phone"),
    # What the AI holds about them
    "account_intelligence":       ("account_id",),
    "account_intelligence_history": ("account_id",),
    "content_embeddings":         ("contact_id", "account_id"),
    # Authentication — metadata only; see BINARY_OR_SECRET below
    "auth_sessions":              ("contact_id", "lead_id", "account_id"),
    "auth_credentials":           ("contact_id", "lead_id", "account_id"),
    # Ad-hoc backup tables that still hold personal data. Exported because the
    # data IS there; flagged because a backup table is not a place personal
    # data should live indefinitely, and erasure routines do not know about it.
    "invoices_backup_v5e4":       ("contact_id", "account_id"),
    # NOT LISTED HERE, deliberately: accounts_email_backup_seed,
    # contacts_email_backup_seed, leads_email_backup_seed and
    # contacts_owner_backup_d4. They were migration safety nets from the
    # seed-email rename and the owner backfill, holding ~643 rows of stale
    # addresses and owner links, and they were DROPPED from both databases on
    # 2026-08-21 rather than declared.
    #
    # Dropping beat exporting, and the reason is worth keeping: erasure routines
    # did not know about them, so a subject's deletion would complete while
    # their old address survived in a backup nobody would think to look in. An
    # exported copy of that is a correct answer to the wrong question.
    #
    # Declaring them now would be a defect in the other direction --
    # phantom_manifest_entries, an entry whose export silently does nothing.
    # Their own request history. Art. 15 covers processing carried out ON the
    # subject, and answering their access requests is such processing — so the
    # register of those requests is disclosable to them. Found by the coverage
    # check the moment the register was created, which is the check earning its
    # keep on the very first table added after it was written.
    "dsar_requests":              ("subject_id",),
    # Their own requests, for the same reason as dsar_requests: answering an
    # access request is itself processing carried out on the subject. Caught by
    # the coverage check within minutes of the table existing — the second time
    # that check has stopped an export the moment a new subject-linked table
    # appeared, which is the whole argument for checking the manifest against
    # the schema rather than trusting it.
    "dsar_subject_requests":      ("subject_id",),
}

# Tables carrying an entity_id/memory_id that can hold ANY entity type. Matched
# against every one of the subject's resolved ids.
BY_ENTITY: Tuple[str, ...] = (
    "audit_log", "record_field_history", "governed_deletions",
    "customer_memories", "interaction_memories", "crm_agent_memory",
    "memory_retrievals", "memory_erasure_log", "memory_verifications",
    "memory_verifications_unattributable", "agent_blackboard",
    "agent_utterances", "action_approvals", "custom_field_values",
)

# CHILD: table -> (its fk column, parent table, parent's pk). Exported after
# the parent, keyed on the parent rows this subject actually owns.
CHILD: Dict[str, Tuple[str, str, str]] = {
    "conversation_messages":       ("conversation_id", "conversations", "conversation_id"),
    "agent_capability_calls":      ("conversation_id", "conversations", "conversation_id"),
    "escalations":                 ("conversation_id", "conversations", "conversation_id"),
    "case_comments":               ("case_id", "cases", "case_id"),
    "order_items":                 ("order_id", "orders", "order_id"),
    # Self-service cancellation verifications. CHILD rather than DIRECT: the
    # row carries recipient_email but no contact_id, and order_id is the only
    # honest link back to a person. Rows are swept after 30 days, so an export
    # legitimately shows only recent ones -- which is retention working, not
    # data being withheld.
    "order_cancel_verifications":  ("order_id", "orders", "order_id"),
    "order_items_backup_v5e4":     ("order_id", "orders", "order_id"),
    "invoice_orders":              ("invoice_id", "invoices", "invoice_id"),
    "opportunity_lines":           ("opportunity_id", "opportunities", "opportunity_id"),
    "opportunity_products":        ("opportunity_id", "opportunities", "opportunity_id"),
    "opportunity_stage_history":   ("opportunity_id", "opportunities", "opportunity_id"),
    "forecast_opportunity_entries": ("opportunity_id", "opportunities", "opportunity_id"),
    "quote_lines":                 ("quote_id", "quotes", "quote_id"),
    "memory_eval_labels":          ("memory_id", "customer_memories", "memory_id"),
}

# EXCLUDED: table -> why. A reason is mandatory. "We didn't think of it" is not
# one of these, which is the point of making the list explicit.
EXCLUDED: Dict[str, str] = {
    # customers_retired_20260901 and invalid_phones_log_retired_20260901 were
    # briefly listed here as a HOLDING STATE while their disposition was owed.
    #
    # DISPOSITION E8, SETTLED 2026-09-01: ERASE. 38 rows and 8 rows were
    # permanently removed (sql/erasure_e8_retired_tables.sql) and both tables
    # were dropped, so there is nothing left to exclude. The erasure itself is
    # recorded in `retired_table_dispositions`, which is now the ledger for the
    # standing invariant:
    #
    #     No retired table may retain personal data without an explicit
    #     disposition. Erasure is the default unless the table is restored
    #     to the manifest.
    #
    # The shells were dropped rather than emptied because both carried
    # `customer_id` and `account_id`: leaving them out of this dict while they
    # still existed would have made coverage() refuse to certify an export.
    "employees":    "Staff record, not customer data. KNOWN LIMITATION: the "
                    "staff-subject path this implies does NOT exist -- "
                    "_resolve() accepts only contact/lead/account/email. See "
                    "staff_personhood(), which answers the prerequisite "
                    "question (13 of 21 rows are software agents, not people) "
                    "and names the blocker: owners.employee_uuid is populated "
                    "on 0 of 44 rows, so one person's ownership and authorship "
                    "records cannot be assembled.",
    "owners":       "Staff record — see employees.",
    "executives":   "Staff record — see employees.",
    "professionals": "Staff record — see employees.",
    "assignable_identity": "Internal work-routing identity for staff, not a "
                           "customer-facing record.",
    "agent_session_memory": "Keyed only by agent session id with no subject "
                            "link. KNOWN LIMITATION: a session may contain the "
                            "subject's text and cannot be reliably located. "
                            "Recorded here so the gap is visible rather than "
                            "absent.",
    "sdr_sessions": "Keyed by session id with no subject link — see "
                    "agent_session_memory.",
    # STAFF NOTIFICATION LEDGER. Every row is an email sent TO A STAFF MEMBER,
    # and no column links one to a customer:
    #
    #   recipient_email / recipient_owner_id  the STAFF recipient. Third-party
    #     data relative to any customer, and disclosing it because a row is
    #     technically adjacent to their record is precisely the Art. 15(4)
    #     failure the account-scope logic exists to prevent.
    #   subject_ref_type / subject_ref_id     the INTERNAL WORK OBJECT the mail
    #     was about. Measured, not assumed: the only values the writers emit
    #     are 'approval', 'escalation' and 'digest' (governance.py:818,
    #     escalation.py:520, staff_email.py:1382), and the ledger holds only
    #     those. An approval id is not a person.
    #
    # So this is a staff record and belongs to a staff subject, exactly like
    # employees/owners/executives above.
    #
    # THE PROMISE THOSE ENTRIES MAKE IS NOT KEPT, and repeating it here would
    # be dishonest: they say "a separate request against subject_type='employee'",
    # but _resolve() accepts only contact/lead/account/email and raises
    # ValueError on anything else. There is no staff DSAR path. That is a real
    # gap, recorded here rather than implied — the same discipline
    # agent_session_memory gets.
    #
    # THE EXCLUSION RESTS ON A DATA PROPERTY, so it is verified against data:
    # see EXCLUSION_PREMISES. If subject_ref_type ever names a customer-shaped
    # entity, the premise is false and the exclusion must be reopened.
    "staff_email_ledger":
        "Staff notification ledger — every row is mail sent TO staff. "
        "recipient_email/recipient_owner_id identify the staff recipient "
        "(third-party data under Art. 15(4)); subject_ref_id names an internal "
        "work object (approval/escalation/digest), never a person. No column "
        "links a row to a customer, so nothing here belongs in a customer's "
        "Art. 15 or Art. 20 export. KNOWN LIMITATION: the staff-subject path "
        "these records would belong to is not implemented — _resolve() accepts "
        "only contact/lead/account/email. Premise verified by "
        "EXCLUSION_PREMISES.",
    "n8n_chat_histories": "Legacy n8n chat log, retired 2026-08-05. Retained "
                          "read-only pending deletion; searched manually on "
                          "request because its session key has no subject link.",
}

# ============================================================================
# EXCLUSIONS THAT REST ON DATA, not on schema alone
# ============================================================================
# coverage() is deliberately structural: it reads pg_attribute and never a row.
# That is right for "is this table declared", and useless for "is the REASON
# still true". Some exclusions are justified by a property of the CONTENT --
# staff_email_ledger is excluded because its subject_ref_type never names a
# person -- and a justification of that shape decays silently when the data
# changes.
#
# The event-trio disposition failed exactly this way: it cited a condition, the
# condition changed, and nothing re-checked it because the tripwire watched the
# wrong surface. So a premise stated here is CHECKED WHERE THE DATA LIVES, and
# checked in production, because that is where new values appear first.
#
# table -> {column, allowed values, why the exclusion depends on them}
EXCLUSION_PREMISES: Dict[str, Dict[str, Any]] = {
    "staff_email_ledger": {
        "column": "subject_ref_type",
        # The complete vocabulary the writers can emit, verified by grep across
        # app/: governance.py 'approval', escalation.py 'escalation',
        # staff_email.py 'digest'. NULL is permitted -- a row about nothing in
        # particular still links to no person.
        "allowed": ("approval", "escalation", "digest"),
        "why": "the table is EXCLUDED from customer exports because "
               "subject_ref_id names an internal work object rather than a "
               "data subject. A value outside this set may identify a person, "
               "which would make the exclusion an under-disclosure.",
    },
}


def exclusion_premises() -> Dict[str, Any]:
    """Are the data-dependent reasons behind EXCLUDED still true?

    Fail-closed and cheap: one DISTINCT per declared premise. A violation is
    not "tidy the list" -- it means a table currently withheld from subjects
    may now contain their data, so the exclusion has to be re-argued.
    """
    violations: List[Dict[str, Any]] = []
    conn = get_connection()
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            for table, spec in EXCLUSION_PREMISES.items():
                cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                if cur.fetchone()[0] is None:
                    continue            # phantom entries are coverage()'s job
                col = spec["column"]
                cur.execute(f'SELECT DISTINCT "{col}" FROM public."{table}" '
                            f'WHERE "{col}" IS NOT NULL')
                seen = {r[0] for r in cur.fetchall()}
                unexpected = sorted(seen - set(spec["allowed"]))
                if unexpected:
                    violations.append({"table": table, "column": col,
                                       "unexpected": unexpected,
                                       "allowed": list(spec["allowed"]),
                                       "why": spec["why"]})
    finally:
        conn.close()
    return {"checked": sorted(EXCLUSION_PREMISES),
            "violations": violations, "ok": not violations}


# Never emit these column types/names even from an included table: a vector is
# not intelligible to a subject, and a credential hash is not their data in any
# useful sense — disclosing it only creates risk.
BINARY_TYPES = {"bytea", "vector", "USER-DEFINED"}
SECRET_COLUMNS = {"password_hash", "password", "secret", "token", "token_hash",
                  "api_key", "signature", "embedding", "vector",
                  # `code_hash` added 2026-08-21, after an export of a real
                  # subject was read and found to contain it.
                  #
                  # It is the SHA-256 of a SIX-DIGIT order-cancellation code,
                  # and six digits is a million candidates — recovering the code
                  # from the hash takes milliseconds, which is how the test
                  # suite does it on purpose. A spent row is harmless, but a row
                  # still unconsumed and unexpired would let anyone holding the
                  # export file finish cancelling that order. Exports get
                  # emailed, stored and forwarded.
                  #
                  # The row itself stays in the export — that a code was sent,
                  # to which address, when, and whether it was used is genuinely
                  # the subject's data. Only the credential is withheld.
                  "code_hash"}


# ============================================================================
# WHICH STAFF ROWS ARE PEOPLE
# ============================================================================
# THE PREREQUISITE FOR A STAFF EXPORT, and the reason there is not one yet.
# `employees`/`owners`/`executives`/`professionals` are EXCLUDED from customer
# exports with the note "a staff member's own DSAR is a separate request
# against subject_type='employee'". _resolve() has never accepted that value,
# so the note describes a plan rather than a path. Before it can become one,
# the system has to answer a question it currently cannot:
#
#     WHICH OF THESE ROWS IS A NATURAL PERSON?
#
# Measured 2026-08-28: of 21 `employees`, 13 are SOFTWARE -- agent identities
# like agent.lead@system.internal. A software agent has no Art. 15 rights, and
# an "employee export" that returned one would be a category error; one that
# skipped a real person would be a breach. Neither available signal is
# sufficient on its own:
#
#   role='agent'                misses `System Admin`, whose role reads
#                               'Administrator' though it is a service account
#   email LIKE '%@system.internal'  catches it, but keys identity off a MUTABLE
#                               ATTRIBUTE -- the mistake that silently
#                               re-labelled 39 customers during a seed
#                               migration. An address is not an identity.
#
# So personhood is DECLARED, and anything the declaration cannot classify is a
# failure rather than a guess. Same shape as EXCLUDED: a reason is mandatory,
# and an unclassifiable row stops an export instead of being assumed one way.

# Roles that denote software rather than a person.
SERVICE_IDENTITY_ROLES: Tuple[str, ...] = ("agent",)

# Service accounts whose ROLE does not give them away, declared individually
# so each one is a decision somebody made. Keyed on employee_uuid -- the
# primary key -- and never on the address, which can change.
SERVICE_IDENTITY_EXCEPTIONS: Dict[str, str] = {
    "admin@system.internal":
        "the platform service account. Its role reads 'Administrator', which "
        "is indistinguishable from a human administrator by role alone, so it "
        "is named here instead. Resolved to a uuid at read time; the address "
        "is the human-readable key, not the identity.",
}


def staff_personhood() -> Dict[str, Any]:
    """Classify every staff row as a data subject or a service identity.

    Fail-closed: `unclassifiable` being non-empty means a staff export cannot
    be certified, exactly as `undeclared` does for the customer manifest.
    """
    people: List[Dict[str, Any]] = []
    services: List[Dict[str, Any]] = []
    unclassifiable: List[Dict[str, Any]] = []
    conn = get_connection()
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.employees')")
            if cur.fetchone()[0] is None:
                return {"people": [], "services": [], "unclassifiable": [],
                        "ok": True, "reason": "no employees table here"}
            cur.execute("""SELECT employee_uuid::text, employee_id::text,
                                  COALESCE(first_name,'') || ' ' ||
                                  COALESCE(last_name,''), email, role
                             FROM public.employees""")
            for uuid_, eid, name, email, role in cur.fetchall():
                row = {"employee_uuid": uuid_, "employee_id": eid,
                       "name": name.strip(), "email": email, "role": role}
                if (role or "").lower() in SERVICE_IDENTITY_ROLES:
                    row["why"] = f"role={role!r} denotes software"
                    services.append(row)
                elif (email or "").lower() in SERVICE_IDENTITY_EXCEPTIONS:
                    row["why"] = SERVICE_IDENTITY_EXCEPTIONS[(email or "").lower()]
                    services.append(row)
                elif not (name.strip() and email):
                    row["why"] = "no name or no address -- cannot be addressed as a subject"
                    unclassifiable.append(row)
                else:
                    people.append(row)
    finally:
        conn.close()
    return {"people": people, "services": services,
            "unclassifiable": unclassifiable, "ok": not unclassifiable}


# THE OTHER HALF OF THE PREREQUISITE, and it is not solved here.
#
# A staff person has TWO identities in this schema and they are not joined:
#
#   owners.owner_id        ownership -- accounts, contacts, opportunities,
#                          invoices. 39 owners own at least one account.
#   employees.employee_uuid  authorship -- created_by / updated_by /
#                          deleted_by on invoices, payments, products.
#
# `owners.employee_uuid` exists as a foreign key and is populated on 0 of 44
# rows, so nothing links the two. One person's ownership records and their
# authorship records currently cannot be assembled into a single export, and
# an export that silently returned half would be worse than none.
#
# Do NOT bridge this by name or address. The two sets share exactly one name
# and zero addresses (owners are @example.com seed identities, staff are
# @emp.agentorc.ca), so a join on either would invent links that do not exist.
# Populating owners.employee_uuid is a data decision for the product owner.
STAFF_IDENTITY_LINK_COLUMN = "owners.employee_uuid"


class IncompleteExport(RuntimeError):
    """The manifest does not cover the live schema, so no export from it can be
    certified complete. Raised instead of returning a partial file that looks
    whole."""


# ============================================================================
# COVERAGE — the manifest checked against the database, not trusted
# ============================================================================

def _schema_subject_tables(cur) -> Dict[str, List[str]]:
    cur.execute("""
        SELECT c.relname, array_agg(DISTINCT a.attname)
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0
                             AND NOT a.attisdropped
         WHERE n.nspname = 'public' AND c.relkind = 'r'
           AND a.attname = ANY(%s)
         GROUP BY c.relname""", (list(SUBJECT_COLUMNS),))
    return {t: sorted(cols) for t, cols in cur.fetchall()}


# ── content-level PII discovery ──────────────────────────────────────────────
# WHAT coverage() PROVES, EXACTLY: every table carrying a column NAMED in
# SUBJECT_COLUMNS is declared. It is a STRUCTURAL check — it reads pg_attribute,
# never a row. A table with no subject column is invisible to it no matter what
# it contains.
#
# Measured 2026-08-08, with coverage() reporting complete=true and undeclared=[]:
#   events              6,911 rows, no subject column, 52 rows carry an email
#                       address inside `payload` — including one contact.deleted
#   knowledge_articles  10 answers contain an email address
#   identity_links      7 rows, email inside `evidence`
#   email_sentiment     5 rows, `from_addr`
#   custom_agents / custom_agent_versions   an email inside `instructions`
# None of those are exported by a DSAR request and none are reached by erasure.
#
# DETECTION LIMITS — this is a smoke detector, not a guarantee:
#   * email-shaped text only. Names, postal addresses, free-text phone numbers
#     and government identifiers are NOT detected. Absence of a hit is NOT
#     evidence of absence of personal data.
#   * FALSE POSITIVES are expected and are not defects in themselves:
#     support@ourcompany.com in a KB article, a template's bcc address, a staff
#     address in an agent instruction. Each hit needs a human disposition, which
#     is why they are ACKNOWLEDGED in PII_CONTENT_ACK rather than auto-cleared.
#   * FALSE NEGATIVES: obfuscated ("name at example dot com"), base64, encrypted
#     or truncated values; anything in a binary column; anything in a table the
#     scan cannot read.
#   * It samples the CURRENT rows. A store that is empty today and PII-bearing
#     tomorrow passes today.
_EMAIL_RX = r"[[:alnum:]._%+-]+@[[:alnum:].-]+[.][[:alpha:]]{2,}"

# Obfuscated addresses — "alan [at] example [dot] com". Deliberate constructions,
# so precision is high and the pattern earns a place in the GATE.
# BRACKETED FORMS ONLY. The first version also allowed a bare " at ", which
# matched ordinary English — "meeting at 3pm", "products at the warehouse" —
# and produced 5 false findings against the live schema (event_types,
# executive_snapshot, memory_metrics_history, notification_messages, products).
# A bracketed [at]/(at) is a deliberate obfuscation and almost never prose.
_OBFUSCATED_RX = (r"[[:alnum:]._%+-]+[[:space:]]*(\[at\]|\(at\))"
                  r"[[:space:]]*[[:alnum:].-]+")

# Phone: a COARSE prefilter in SQL, confirmed in Python by the matcher already
# proven in job_ledger.scrub. Deliberately not a second phone regex — two
# independent definitions of "what a phone number looks like" drift apart, and
# this one has already been tuned against real false positives (SO-2026-101730,
# 2026-08-01 00:30:00). One definition, one place, reused.
_PHONE_PREFILTER = r"[0-9][0-9()+.[:space:]-]{7,}[0-9]"
_PHONE_SAMPLE = 200          # values pulled per column for confirmation

# ADVISORY ONLY — never gates. Street-suffix matching cannot distinguish a
# customer's home address from our own in an email footer, a product
# description, or a KB article. Reported as a count so the exposure is visible;
# making it a gate would produce failures nobody can action.
_ADDRESS_RX = (r"[0-9]{1,6}[[:space:]]+[[:alpha:].]+([[:space:]]+[[:alpha:].]+)*"
               r"[[:space:]]+(St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|"
               r"Drive|Lane|Ln|Way|Court|Ct|Cres|Crescent)\.?([[:space:]]|,|$)")

# Stores where email-shaped content has been seen, reviewed, and dispositioned.
# Presence here means A HUMAN DECIDED, not that the store is clean. A store that
# starts carrying PII and is NOT here fails content_coverage().
PII_CONTENT_ACK: Dict[str, str] = {
    "company_profile":  "our own company contact details, not a data subject's",
    "email_templates":  "our own from/bcc addresses and template boilerplate",
    "custom_agents":    "authored agent instructions may name a staff mailbox to "
                        "escalate to; staff data, see EXCLUDED['employees']",
    "custom_agent_versions": "version history of the above",
    # ── F-8.7 dispositions, 2026-08-08. Each was inspected, not assumed. ────
    "knowledge_articles":
        "OUR OWN addresses. Inspected: the only three are info@agentorc.ca, "
        "ithelp@agentorc.ca and security@agentorc.ca — published support "
        "contacts in article bodies, not data about a subject. No action.",
    "email_sentiment":
        "BOUNDED BY RETENTION. from_addr is a correspondent's address; mostly "
        "our own inbox, some external senders. A sentiment score has no "
        "independent basis for indefinite retention, so retention.POLICIES "
        "now expires it at 90 days rather than building an export path for "
        "operational exhaust.",
    "events":
        "BOUNDED BY RETENTION (180d). Payload carries subject identifiers and "
        "the table has no subject FK, so it is neither exported nor erasable. "
        "KNOWN LIMITATION, recorded rather than hidden: until a row expires, "
        "a subject's address can persist here after their erasure — including "
        "in the contact.deleted event itself. Declared so the gap is visible.",
    "identity_links":
        "ERASABLE, PARTIALLY. Measured: erasure DELETEs identity_links rows "
        "for contacts/leads/accounts, but matches on duplicate_id only — a "
        "row where the subject is the PRIMARY side survives with their "
        "identifier still in `evidence`. Follow-up F-9.5; recorded here so "
        "the acknowledgement does not overstate the coverage.",
}


def content_coverage(limit_tables: int = 0) -> Dict[str, Any]:
    """Find PII by CONTENT, in stores no structural check can see.

    Complements coverage(): that one asks "is every subject-linked table
    declared?", this one asks "is there personal data somewhere nothing
    declared?". A store is a FINDING when it carries email-shaped content and
    is neither in the export manifest, nor in EXCLUDED, nor acknowledged in
    PII_CONTENT_ACK.

    Read-only. Never mutates, never deletes, never redacts — a scanner that
    edits evidence is worse than no scanner.
    """
    from app.core import lifecycle

    exported = set(DIRECT) | set(BY_ENTITY) | set(CHILD)
    erasable = {s["table"] for p in lifecycle.PLANS.values()
                for s in p.get("satellites", [])} | set(lifecycle.PLANS)

    conn = get_connection()
    scanned = 0
    hits: Dict[str, Dict[str, int]] = {}
    phone_hits: Dict[str, Dict[str, int]] = {}
    address_hits: Dict[str, Dict[str, int]] = {}
    unreadable: List[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.relname, a.attname,
                       format_type(a.atttypid, a.atttypmod) AS typ
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0
                                     AND NOT a.attisdropped
                 WHERE n.nspname = 'public' AND c.relkind = 'r'
                   AND format_type(a.atttypid, a.atttypmod) ~
                       '^(text|character varying|json|jsonb)'
                 ORDER BY c.relname, a.attname""")
            cols = cur.fetchall()
            if limit_tables:
                keep = sorted({t for t, _, _ in cols})[:limit_tables]
                cols = [c for c in cols if c[0] in keep]
            for table, col, typ in cols:
                expr = (f'"{col}"::text' if typ.startswith(("json", "jsonb"))
                        else f'"{col}"')
                try:
                    # GATING detectors: email + obfuscated email.
                    cur.execute(
                        f'SELECT count(*) FROM "{table}" '
                        f'WHERE {expr} ~ %s OR {expr} ~ %s',
                        (_EMAIL_RX, _OBFUSCATED_RX))
                    n = cur.fetchone()[0]

                    # GATING: phone. Coarse SQL prefilter, then confirm each
                    # candidate with job_ledger's proven matcher, so a document
                    # number or a timestamp cannot be counted as a phone number.
                    cur.execute(
                        f'SELECT {expr} FROM "{table}" WHERE {expr} ~ %s '
                        f'LIMIT {int(_PHONE_SAMPLE)}', (_PHONE_PREFILTER,))
                    candidates = [r[0] for r in cur.fetchall() if r[0]]
                    if candidates:
                        from app.core.job_ledger import _PHONE_CAND, _phone_sub
                        confirmed = sum(
                            1 for v in candidates
                            if _PHONE_CAND.sub(_phone_sub, str(v)) != str(v))
                        if confirmed:
                            phone_hits.setdefault(table, {})[col] = confirmed
                            # ADVISORY, NOT GATING — demoted after measurement.
                            # Promoted to a gate it produced 9 new findings
                            # against the live schema, and inspection showed
                            # them to be false: product_image.image_url
                            # ('…/Office%20Supplies/Quartet%20Lined%…') is
                            # CONFIRMED as a phone number by the matcher,
                            # because percent-encoding makes long digit runs.
                            # SKUs and content hashes prefilter in too.
                            # The matcher was tuned against EXCEPTION TEXT in
                            # job_ledger, and that is the domain where it is
                            # accurate; URLs, hashes and identifiers are not.
                            # A gate that fires on image URLs teaches people
                            # to ignore the gate, which costs more than the
                            # detection is worth.

                    # ADVISORY: address-like. Counted, never gates.
                    cur.execute(
                        f'SELECT count(*) FROM "{table}" WHERE {expr} ~ %s',
                        (_ADDRESS_RX,))
                    a = cur.fetchone()[0]
                    if a:
                        address_hits.setdefault(table, {})[col] = a

                    scanned += 1
                except Exception:
                    conn.rollback()
                    unreadable.append(f"{table}.{col}")
                    continue
                if n:
                    hits.setdefault(table, {})[col] = n
    finally:
        conn.close()

    findings, acknowledged = [], []
    for table in sorted(hits):
        row = {"table": table, "columns": hits[table],
               "exported": table in exported,
               "excluded": table in EXCLUDED,
               "erasable": table in erasable}
        if table in exported or table in EXCLUDED:
            continue                      # governed already; content is in scope
        note = PII_CONTENT_ACK.get(table)
        # An entry marked UNRESOLVED is a PLACEHOLDER, not a disposition. It
        # must keep failing the gate, or writing the word "UNRESOLVED" into the
        # acknowledgement list would be enough to turn the check green — which
        # is the exact failure this check exists to prevent.
        settled = note is not None and not note.startswith("UNRESOLVED")
        (acknowledged if settled else findings).append({**row, "note": note})

    return {
        "columns_scanned": scanned,
        "columns_unreadable": unreadable,
        "stores_with_email_content": len(hits),
        "findings": findings,
        "acknowledged": acknowledged,
        "phone_hits": phone_hits,
        # ADVISORY. Reported, never gating — see _ADDRESS_RX. A count here does
        # NOT mean a finding; most matches are our own address in a footer or a
        # street name inside a product description.
        "address_advisory": address_hits,
        "detector": "GATING: email-shaped text and obfuscated addresses "
                    "([at]/[dot]). ADVISORY ONLY (counted, never gates): "
                    "phone-shaped and address-shaped text — both were measured "
                    "as too noisy to gate on (a percent-encoded image URL "
                    "confirms as a phone number). Names, prose addresses, "
                    "encoded and truncated values are NOT detected at all. "
                    "Broader coverage has NOT made this complete — a clean "
                    "result is NOT proof that no personal data exists",
        "complete": not findings,
    }


def coverage() -> Dict[str, Any]:
    """Which subject-linked tables the manifest accounts for, and which it does
    not. `undeclared` being non-empty means exports cannot be certified.

    STRUCTURAL ONLY. This reads pg_attribute and never looks at a row, so it
    cannot see personal data sitting in a JSON payload on a table with no
    subject column — see content_coverage() for that half.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            live = _schema_subject_tables(cur)
            cur.execute("""SELECT c.relname FROM pg_class c
                             JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname='public' AND c.relkind='r'""")
            all_tables = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()

    declared = set(DIRECT) | set(BY_ENTITY) | set(CHILD) | set(EXCLUDED)
    undeclared = sorted(set(live) - declared)
    # A manifest entry for a table that no longer exists is also a defect: it
    # means the export is silently doing nothing for that entry.
    phantom = sorted(declared - all_tables)
    return {
        "subject_linked_tables": len(live),
        "declared": len(declared & (all_tables | set(live))),
        "included_direct": len(DIRECT), "included_by_entity": len(BY_ENTITY),
        "included_child": len(CHILD), "excluded": len(EXCLUDED),
        "undeclared": undeclared,
        "phantom_manifest_entries": phantom,
        "complete": not undeclared and not phantom,
    }


# ============================================================================
# SUBJECT RESOLUTION
# ============================================================================

def _resolve(cur, subject_type: str, subject_id: str) -> Dict[str, Any]:
    """Every identifier this person is known by, plus whether account-scoped
    data can be released without exposing someone else."""
    ids: Dict[str, Any] = {"contact_id": None, "account_id": None,
                           "lead_id": None, "customer_id": None,
                           "email": None, "phone": None,
                           # the identifier as the requester gave it, so the
                           # DSAR register can be matched on its own key
                           "subject_id": str(subject_id)}
    if subject_type == "contact":
        cur.execute("SELECT contact_id, account_id, email, phone FROM contacts "
                    "WHERE contact_id = %s::uuid", (subject_id,))
    elif subject_type == "lead":
        cur.execute("SELECT NULL::uuid, NULL::uuid, email, phone FROM leads "
                    "WHERE lead_id = %s::uuid", (subject_id,))
    elif subject_type == "account":
        cur.execute("SELECT NULL::uuid, account_id, email, phone FROM accounts "
                    "WHERE account_id = %s::uuid", (subject_id,))
    elif subject_type == "email":
        cur.execute("SELECT contact_id, account_id, email, phone FROM contacts "
                    "WHERE lower(email) = lower(%s) LIMIT 1", (subject_id,))
    else:
        raise ValueError(f"unknown subject_type {subject_type!r}")
    row = cur.fetchone()
    if not row:
        raise LookupError(f"no {subject_type} matching {subject_id!r}")
    ids["contact_id"], ids["account_id"], ids["email"], ids["phone"] = row
    if subject_type == "lead":
        ids["lead_id"] = subject_id
    if subject_type == "account":
        ids["account_id"] = subject_id

    # Art. 15(4): is this person alone on the account?
    others = 0
    if ids["account_id"]:
        cur.execute("SELECT count(*) FROM contacts WHERE account_id = %s "
                    "AND (contact_id IS DISTINCT FROM %s)",
                    (ids["account_id"], ids["contact_id"]))
        others = cur.fetchone()[0]
        # RETIRED 2026-09-01 with the `customers` table, and the defect it
        # guarded is kept written down because the SHAPE recurs even though
        # this instance is gone:
        #
        #   customer_id was derived FROM THE ACCOUNT, so on a shared account it
        #   could belong to a colleague. Resolving it unconditionally laundered
        #   an account-scoped identifier into a subject-scoped one and slipped
        #   past the Art. 15(4) withholding — a shared-account export carried
        #   another contact's email address in the `customers` section.
        #   Withholding the account is not enough if an id taken from it is
        #   still trusted.
        #
        # `others` is still computed below and still gates `_account_scope_
        # released`. Any FUTURE account-derived identifier must re-earn the
        # `others == 0` guard that used to stand here.
    ids["_account_shared_with"] = others
    ids["_account_scope_released"] = (others == 0)
    return ids


# ============================================================================
# EXPORT
# ============================================================================

def _columns(cur, table: str) -> List[str]:
    cur.execute("""
        SELECT a.attname, format_type(a.atttypid, NULL)
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0
                             AND NOT a.attisdropped
         WHERE n.nspname='public' AND c.relname=%s ORDER BY a.attnum""", (table,))
    out = []
    for name, typ in cur.fetchall():
        if name.lower() in SECRET_COLUMNS or typ in BINARY_TYPES:
            continue
        out.append(name)
    return out


def _rows(cur, table: str, where: str, params: Dict[str, Any]) -> List[Dict]:
    cols = _columns(cur, table)
    if not cols:
        return []
    sel = ", ".join(f'"{c}"' for c in cols)
    cur.execute(f'SELECT {sel} FROM public."{table}" WHERE {where}', params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def export_subject(subject_type: str, subject_id: str,
                   requested_by: str = "unspecified",
                   purpose: str = "Art. 15 access request",
                   strict: bool = True) -> Dict[str, Any]:
    """Everything the system holds about one person, as portable JSON.

    strict=True (the default) refuses when the manifest does not cover the live
    schema. An export that cannot be certified complete must not be handed to a
    subject as though it were."""
    cov = coverage()
    if strict and not cov["complete"]:
        raise IncompleteExport(
            f"manifest does not cover the schema: undeclared={cov['undeclared']} "
            f"phantom={cov['phantom_manifest_entries']}. Declare each table in "
            f"DIRECT / BY_ENTITY / CHILD / EXCLUDED, then retry.")

    conn = get_connection()
    sections: Dict[str, Any] = {}
    withheld: Dict[str, str] = {}
    try:
        conn.set_session(readonly=True)          # the DB refuses a write
        with conn.cursor() as cur:
            ids = _resolve(cur, subject_type, subject_id)
            release_account = ids["_account_scope_released"]
            live = _schema_subject_tables(cur)
            subject_values = {k: v for k, v in ids.items()
                              if not k.startswith("_") and v is not None}

            # ---- direct ----
            for table, cols in DIRECT.items():
                if table not in live:
                    continue
                clauses, params, skipped_acct = [], {}, False
                for col in cols:
                    if col not in live[table] or ids.get(col) in (None, ""):
                        continue
                    if col in ACCOUNT_SCOPED and not release_account:
                        skipped_acct = True
                        continue
                    # Both sides as text. The same logical id is uuid in most
                    # tables and text in others (auth_sessions.contact_id), and
                    # a DSAR that skipped a table because of a type mismatch
                    # would under-disclose silently. Casting costs index use,
                    # which is the right trade for a handful of admin-run
                    # exports a year over a subject receiving a partial file.
                    key = f"p_{col}"
                    lhs = f'lower("{col}"::text)' if col == "email" else f'"{col}"::text'
                    rhs = f"lower(%({key})s)" if col == "email" else f"%({key})s"
                    clauses.append(f"{lhs} = {rhs}")
                    params[key] = str(ids[col])
                if not clauses:
                    if skipped_acct:
                        withheld[table] = (
                            f"account-scoped only, and this account has "
                            f"{ids['_account_shared_with']} other contact(s) — "
                            f"releasing it would disclose third-party data "
                            f"(Art. 15(4))")
                    continue
                rows = _rows(cur, table, " OR ".join(clauses), params)
                if rows:
                    sections[table] = rows
                if skipped_acct:
                    withheld[table + " (account-scoped rows)"] = (
                        f"withheld: account shared with "
                        f"{ids['_account_shared_with']} other contact(s)")

            # ---- entity_id tables ----
            # Same trap as customer_id: entity_id tables hold records ABOUT an
            # id, and an account-level memory or audit entry can describe the
            # whole organisation. If the account is withheld from the direct
            # sections it must be withheld here too, or the entity join becomes
            # a side door back to the data we just declined to release.
            entity_ids = [str(v) for k, v in subject_values.items()
                          if k.endswith("_id")
                          and not (k in ACCOUNT_SCOPED and not release_account)]
            if not release_account and ids.get("account_id"):
                withheld["(entity-keyed tables, account rows)"] = (
                    f"records keyed on the account id were not searched: "
                    f"account shared with {ids['_account_shared_with']} other "
                    f"contact(s) (Art. 15(4))")
            if entity_ids:
                for table in BY_ENTITY:
                    if table not in live and table not in live:
                        pass
                    try:
                        rows = _rows(cur, table, "entity_id::text = ANY(%(ids)s)",
                                     {"ids": entity_ids})
                    except Exception as exc:                    # noqa: BLE001
                        conn.rollback()
                        withheld[table] = f"query failed: {type(exc).__name__}"
                        continue
                    if rows:
                        sections[table] = rows

            # ---- children of what we exported ----
            for table, (fk, parent, ppk) in CHILD.items():
                parent_rows = sections.get(parent) or []
                pids = [str(r[ppk]) for r in parent_rows if r.get(ppk) is not None]
                if not pids:
                    continue
                try:
                    rows = _rows(cur, table, f'"{fk}"::text = ANY(%(ids)s)',
                                 {"ids": pids})
                except Exception as exc:                        # noqa: BLE001
                    conn.rollback()
                    withheld[table] = f"query failed: {type(exc).__name__}"
                    continue
                if rows:
                    sections[table] = rows
    finally:
        conn.close()

    total = sum(len(v) for v in sections.values())
    export = {
        "meta": {
            "export_id": str(uuid.uuid4()),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "subject_type": subject_type,
            "subject_id": str(subject_id),
            "requested_by": requested_by,
            "purpose": purpose,
            "legal_basis": "GDPR Art. 15 (access) / Art. 20 (portability)",
            "controller": os.getenv("DATA_CONTROLLER_NAME", "Conscestra CRM"),
            "identifiers_matched": {k: str(v) for k, v in
                                    (("contact_id", ids.get("contact_id")),
                                     ("account_id", ids.get("account_id")),
                                     ("lead_id", ids.get("lead_id")),
                                     ("customer_id", ids.get("customer_id")),
                                     ("email", ids.get("email")))
                                    if v is not None},
            "tables_with_data": len(sections),
            "total_rows": total,
            "certified_complete": cov["complete"],
            "manifest_coverage": cov,
        },
        "withheld": withheld,
        "excluded_by_policy": EXCLUDED,
        "data": sections,
    }
    _audit(export)
    return export


def _json_default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, (bytes, memoryview)):
        return f"<{len(bytes(o))} bytes omitted>"
    return str(o)


def to_json(export: Dict[str, Any], indent: int = 2) -> str:
    return json.dumps(export, indent=indent, default=_json_default,
                      ensure_ascii=False)


# ============================================================================
# AUDIT — a disclosure of personal data is itself an event worth recording
# ============================================================================

def _audit(export: Dict[str, Any]) -> None:
    m = export["meta"]
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO dsar_requests
                         (export_id, subject_type, subject_id, requested_by,
                          purpose, tables_exported, rows_exported,
                          certified_complete, withheld_sections)
                       VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                    (m["export_id"], m["subject_type"], m["subject_id"],
                     m["requested_by"], m["purpose"], m["tables_with_data"],
                     m["total_rows"], m["certified_complete"],
                     json.dumps(export["withheld"])))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:                                    # noqa: BLE001
        # WARNING, not debug: an unlogged disclosure is an accountability gap
        # under Art. 5(2), and the export still went out.
        logger.warning(f"[dsar] export {m['export_id']} was NOT audited: {exc}")


# ============================================================================
# CLI
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="GDPR data subject export")
    ap.add_argument("--contact"); ap.add_argument("--lead")
    ap.add_argument("--account"); ap.add_argument("--email")
    ap.add_argument("--requested-by", default="cli")
    ap.add_argument("--purpose", default="Art. 15 access request")
    ap.add_argument("--out")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--content-coverage", action="store_true",
                    dest="content_coverage",
                    help="scan row CONTENT for PII in stores no structural "
                         "check can see (email-shaped text only)")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="export even though the manifest is out of date; the "
                         "result is marked certified_complete=false")
    a = ap.parse_args()

    if a.coverage:
        c = coverage()
        print(json.dumps(c, indent=2))
        if not c["complete"]:
            print("\nNOT COMPLETE — declare these before exporting:")
            for t in c["undeclared"]:
                print(f"  undeclared table: {t}")
            for t in c["phantom_manifest_entries"]:
                print(f"  manifest entry with no table: {t}")
            return 1
        # A declared manifest is not a correct one. Some exclusions are
        # justified by a property of the CONTENT, and structural coverage
        # cannot see content -- so the premises are checked here, against
        # the database being verified, which in the deployed case is
        # production, where a new value appears first.
        prem = exclusion_premises()
        print(json.dumps({"exclusion_premises": prem}, indent=2))
        if not prem["ok"]:
            print("")
            print("EXCLUSION PREMISE VIOLATED - a table withheld from "
                  "subjects may now hold their data:")
            for v in prem["violations"]:
                print(f"  {v['table']}.{v['column']} has unexpected "
                      f"value(s) {v['unexpected']}; declared: {v['allowed']}")
                print(f"    {v['why']}")
            print("Re-argue the exclusion, or link the table into the "
                  "manifest. Do not widen `allowed` to make this pass.")
            return 1
        print("")
        print("manifest covers every subject-linked table, and every "
              "data-dependent exclusion premise still holds")
        return 0

    if a.content_coverage:
        c = content_coverage()
        print(json.dumps(c, indent=2))
        if not c["complete"]:
            print("\nPII CONTENT FOUND IN UNGOVERNED STORES:")
            for f in c["findings"]:
                print(f"  {f['table']}: {f['columns']} "
                      f"(exported={f['exported']} erasable={f['erasable']})")
            print("\nEach needs a disposition: add it to the export manifest, "
                  "to EXCLUDED with a justification, to an erasure plan, or to "
                  "PII_CONTENT_ACK if the match is our own data.")
            return 1
        print("\nno unacknowledged PII content found — NOTE: email-shaped text "
              "only; this is not proof that no personal data exists")
        return 0

    pairs = [("contact", a.contact), ("lead", a.lead),
             ("account", a.account), ("email", a.email)]
    given = [(t, v) for t, v in pairs if v]
    if len(given) != 1:
        ap.error("give exactly one of --contact/--lead/--account/--email")
    stype, sid = given[0]
    try:
        exp = export_subject(stype, sid, requested_by=a.requested_by,
                             purpose=a.purpose, strict=not a.allow_incomplete)
    except IncompleteExport as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except LookupError as exc:
        print(f"NOT FOUND: {exc}", file=sys.stderr)
        return 3

    text = to_json(exp)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        m = exp["meta"]
        print(f"wrote {a.out}: {m['total_rows']} rows across "
              f"{m['tables_with_data']} tables, "
              f"certified_complete={m['certified_complete']}")
        if exp["withheld"]:
            print(f"withheld {len(exp['withheld'])} section(s) — see 'withheld'")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
