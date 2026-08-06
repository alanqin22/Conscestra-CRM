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
                   "email", "phone", "person_id", "subject_id", "user_email")

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
    "customers":                  ("customer_id", "account_id"),
    "accounts":                   ("account_id",),
    "channel_identities":         ("account_id",),
    # Their activity
    "activities":                 ("contact_id", "lead_id", "account_id"),
    "activity_participants":      ("contact_id",),
    "appointments":               ("customer_id",),
    "cases":                      ("contact_id", "account_id"),
    "conversations":              ("account_id",),
    "call_logs":                  ("customer_id",),
    "call_state":                 ("customer_id",),
    "invalid_phones_log":         ("customer_id",),
    # Commercial record
    "opportunities":              ("contact_id", "account_id"),
    "orders":                     ("contact_id", "account_id"),
    "quotes":                     ("contact_id", "account_id"),
    "invoices":                   ("contact_id", "account_id"),
    "payments":                   ("contact_id", "account_id"),
    "coupon_redemptions":         ("contact_id", "account_id"),
    "price_match_requests":       ("contact_id", "account_id"),
    # Marketing and consent
    "marketing_sends":            ("contact_id", "account_id", "email"),
    "email_suppression":          ("email",),
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
    # Their own request history. Art. 15 covers processing carried out ON the
    # subject, and answering their access requests is such processing — so the
    # register of those requests is disclosable to them. Found by the coverage
    # check the moment the register was created, which is the check earning its
    # keep on the very first table added after it was written.
    "dsar_requests":              ("subject_id",),
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
    "employees":    "Staff record, not customer data. A staff member's own DSAR "
                    "is a separate request against subject_type='employee'.",
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
    "n8n_chat_histories": "Legacy n8n chat log, retired 2026-08-05. Retained "
                          "read-only pending deletion; searched manually on "
                          "request because its session key has no subject link.",
}

# Never emit these column types/names even from an included table: a vector is
# not intelligible to a subject, and a credential hash is not their data in any
# useful sense — disclosing it only creates risk.
BINARY_TYPES = {"bytea", "vector", "USER-DEFINED"}
SECRET_COLUMNS = {"password_hash", "password", "secret", "token", "token_hash",
                  "api_key", "signature", "embedding", "vector"}


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


def coverage() -> Dict[str, Any]:
    """Which subject-linked tables the manifest accounts for, and which it does
    not. `undeclared` being non-empty means exports cannot be certified."""
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
        # customer_id is derived FROM THE ACCOUNT, so on a shared account it
        # may belong to a colleague. Resolving it unconditionally laundered an
        # account-scoped identifier into a subject-scoped one and slipped past
        # the Art. 15(4) withholding — a shared-account export carried another
        # contact's email address in the `customers` section. Withholding the
        # account is not enough if an id taken from it is still trusted.
        if others == 0:
            cur.execute("SELECT customer_id FROM customers WHERE account_id = %s "
                        "LIMIT 1", (ids["account_id"],))
            c = cur.fetchone()
            if c:
                ids["customer_id"] = c[0]
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
        print("\nmanifest covers every subject-linked table")
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
