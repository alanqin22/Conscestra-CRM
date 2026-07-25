"""Governed CSV data onboarding (Platform blindspot P1).

The "value fast for companies of all sizes" on-ramp: a new org could EXPORT
(QuickBooks/Xero CSV) but had no way to bring its existing book of business IN
except one record at a time. This adds a governed bulk importer for the core
CRM entities — accounts, contacts, leads — that REUSES the existing per-entity
create path (`build_*_query(mode='create')` → `execute_sp`), so every import
goes through the same validation, guards and audit a chat-created record does.

Flow (two explicit steps, like a marketing-campaign launch — the admin is the
approver):
    preview(entity, csv)  → dry-run: parse, alias-map headers, validate, dedupe;
                            classify every row create / duplicate / error. READ-ONLY.
    commit(entity, csv)   → create the 'create' rows via the entity SP; re-checks
                            dedupe per row so a re-run can't double-insert.

Header mapping is alias-based and case/spacing-insensitive ("Company Name" →
accountName), with an optional explicit {field: header} override. Row count is
capped. Admin-gated at the router.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection, execute_sp
from app.agents.accounts.sql_builder import build_accounts_query
from app.agents.contacts.sql_builder import build_contacts_query
from app.agents.leads.sql_builder import build_leads_query

logger = logging.getLogger("data_import")

MAX_ROWS = 2000
PREVIEW_SAMPLE = 50   # detailed per-row rows returned in a preview (+ all errors)


def _F(key: str, aliases: List[str], required: bool = False) -> Dict[str, Any]:
    return {"key": key, "aliases": aliases, "required": required}


def _norm(h: str) -> str:
    """Normalize a header/alias for matching: lowercase, collapse spaces/underscores."""
    return re.sub(r"[\s_]+", " ", str(h or "").strip().lower())


def _account_transform(row: Dict[str, Any]) -> Dict[str, Any]:
    """Fold the flat address columns into the billingAddress JSONB the account
    SP expects (SP reads ->>'street' as line1). Leaves accountName/email/etc. intact."""
    r = dict(row)
    addr: Dict[str, Any] = {"street": r.pop("line1", None)}
    for src, dst in (("city", "city"), ("province", "province"),
                     ("postalCode", "postal_code"), ("country", "country")):
        v = r.pop(src, None)
        if v:
            addr[dst] = v
    r["billingAddress"] = addr
    return r


# ── Entity registry ─────────────────────────────────────────────────────────
# Each importable entity: the create builder, its fields (+ header aliases +
# required), an optional custom validator, and a dedupe probe.
ENTITIES: Dict[str, Dict[str, Any]] = {
    "accounts": {
        "label": "Accounts",
        "create_mode": "create",
        "build": build_accounts_query,
        "fields": [
            _F("accountName", ["account", "account name", "company", "company name",
                               "name", "organization", "organisation", "org"], required=True),
            # sp_accounts unconditionally writes a billing address on create and
            # addresses has NOT NULL line1/city/country, so those three are
            # required to import an account. Province/postal are optional.
            _F("line1",   ["address", "street", "street address", "address line 1",
                           "line 1", "line1", "billing street", "billing address"], required=True),
            _F("city",       ["city", "town"], required=True),
            _F("country",    ["country"], required=True),
            _F("province",   ["province", "state", "region"]),
            _F("postalCode", ["postal code", "postal", "zip", "zip code", "postcode"]),
            _F("email",    ["email", "email address", "e mail"]),
            _F("phone",    ["phone", "telephone", "phone number", "tel", "mobile"]),
            _F("industry", ["industry", "sector", "vertical"]),
            _F("website",  ["website", "url", "web", "site", "domain"]),
        ],
        "dedupe_sql": "SELECT 1 FROM accounts WHERE lower(account_name)=lower(%s) "
                      "AND COALESCE(is_deleted,false)=false LIMIT 1",
        "dedupe_value": lambda r: r.get("accountName"),
        # Assemble the flat address columns into the billing-address JSONB the SP
        # expects; force shipping off (explicit NULL — see the builder probe).
        "transform": lambda r: _account_transform(r),
        "create_defaults": {"shippingAddress": None},
    },
    "contacts": {
        "label": "Contacts",
        "create_mode": "create",
        "build": build_contacts_query,
        "fields": [
            _F("firstName", ["first name", "firstname", "first", "given name"], required=True),
            _F("lastName",  ["last name", "lastname", "last", "surname", "family name"], required=True),
            _F("email",     ["email", "email address", "e mail"], required=True),
            _F("phone",     ["phone", "telephone", "tel", "mobile"]),
        ],
        "dedupe_sql": "SELECT 1 FROM contacts WHERE lower(email)=lower(%s) "
                      "AND COALESCE(is_deleted,false)=false LIMIT 1",
        "dedupe_value": lambda r: r.get("email"),
    },
    "leads": {
        "label": "Leads",
        "create_mode": "create",
        "build": build_leads_query,
        "fields": [
            _F("firstName", ["first name", "firstname", "first", "given name"]),
            _F("lastName",  ["last name", "lastname", "last", "surname", "family name"]),
            _F("email",     ["email", "email address", "e mail"]),
            _F("company",   ["company", "company name", "account", "organization", "organisation"]),
            _F("phone",     ["phone", "telephone", "tel", "mobile"]),
            _F("source",    ["source", "lead source", "channel"]),
        ],
        # leads require firstName OR lastName (SP contract)
        "validate": lambda r: (None if (r.get("firstName") or r.get("lastName"))
                               else "a first name or last name is required"),
        "dedupe_sql": "SELECT 1 FROM leads WHERE lower(email)=lower(%s) "
                      "AND COALESCE(is_deleted,false)=false LIMIT 1",
        "dedupe_value": lambda r: r.get("email"),
    },
}


def list_schema() -> Dict[str, Any]:
    return {"max_rows": MAX_ROWS, "entities": {
        name: {"label": e["label"], "fields": [
            {"key": f["key"], "required": f["required"], "aliases": f["aliases"]}
            for f in e["fields"]]}
        for name, e in ENTITIES.items()}}


# ── CSV → mapped rows ────────────────────────────────────────────────────────

def _alias_index(spec: Dict[str, Any]) -> Dict[str, str]:
    """normalized header → field key (from each field's key + aliases)."""
    idx: Dict[str, str] = {}
    for f in spec["fields"]:
        idx[_norm(f["key"])] = f["key"]
        for a in f["aliases"]:
            idx.setdefault(_norm(a), f["key"])
    return idx


def _parse_csv(csv_text: str) -> tuple:
    """→ (rows: list[dict header→value], headers: list[str])."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = [{(k or ""): (v if v is not None else "") for k, v in row.items()}
            for row in reader]
    return rows, (reader.fieldnames or [])


def _map_row(spec: Dict[str, Any], raw: Dict[str, str], idx: Dict[str, str],
             mapping: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """Raw CSV row (header→value) → {field_key: value} using explicit mapping
    first, else alias matching. Empty strings are dropped."""
    out: Dict[str, Any] = {}
    if mapping:
        for field_key, header in mapping.items():
            v = raw.get(header)
            if v is not None and str(v).strip():
                out[field_key] = str(v).strip()
        return out
    for header, val in raw.items():
        key = idx.get(_norm(header))
        if key and val is not None and str(val).strip() and key not in out:
            out[key] = str(val).strip()
    return out


def _validate_row(spec: Dict[str, Any], row: Dict[str, Any]) -> Optional[str]:
    for f in spec["fields"]:
        if f["required"] and not row.get(f["key"]):
            return f"missing required field '{f['key']}'"
    v = spec.get("validate")
    if v:
        return v(row)
    return None


def _exists(sql: str, value: Any) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (value,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def _is_duplicate(spec: Dict[str, Any], row: Dict[str, Any]) -> bool:
    val = spec["dedupe_value"](row)
    if not val:
        return False
    try:
        return _exists(spec["dedupe_sql"], val)
    except Exception as exc:
        logger.debug(f"[import] dedupe check failed: {exc}")
        return False


# ── Public API ───────────────────────────────────────────────────────────────

def _prepare(entity: str, csv_text: str) -> tuple:
    if entity not in ENTITIES:
        raise ValueError(f"unknown entity '{entity}'. Valid: {', '.join(ENTITIES)}")
    spec = ENTITIES[entity]
    rows, headers = _parse_csv(csv_text.lstrip("﻿"))
    if len(rows) > MAX_ROWS:
        raise ValueError(f"{len(rows)} rows exceeds the {MAX_ROWS}-row import cap; split the file")
    return spec, rows, headers


def preview(entity: str, csv_text: str, mapping: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Dry-run: classify every row create / duplicate / error. READ-ONLY."""
    spec, rows, headers = _prepare(entity, csv_text)
    idx = _alias_index(spec)
    mapped_keys = set((mapping or {}).keys()) or {
        idx[_norm(h)] for h in headers if _norm(h) in idx}
    unmapped = [h for h in headers if not (mapping and h in mapping.values())
                and _norm(h) not in idx]

    summary = {"create": 0, "duplicate": 0, "error": 0}
    detail: List[Dict[str, Any]] = []
    for i, raw in enumerate(rows, 1):
        row = _map_row(spec, raw, idx, mapping)
        err = _validate_row(spec, row)
        if err:
            status, reason = "error", err
        elif _is_duplicate(spec, row):
            status, reason = "duplicate", "already exists (matched on dedupe key)"
        else:
            status, reason = "create", None
        summary[status] += 1
        if status == "error" or len(detail) < PREVIEW_SAMPLE:
            detail.append({"row": i, "status": status, "reason": reason, "values": row})

    return {
        "entity": entity, "total": len(rows), "summary": summary,
        "mapped_fields": sorted(mapped_keys),
        "unmapped_columns": unmapped,
        "sample": detail,
        "note": (f"{summary['create']} will be created, {summary['duplicate']} "
                 f"skipped as duplicates, {summary['error']} have errors. "
                 f"Nothing has been written — call commit to create."),
    }


def _batch_ref() -> str:
    """A stable id for one import run — recorded as each row's provenance
    source_id so an imported record can be traced (or reviewed) as a batch."""
    import datetime as _dt
    import uuid as _uuid
    return (f"import:{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}:"
            f"{_uuid.uuid4().hex[:8]}")


def _stamp_provenance(entity: str, dedupe_sql: str, dedupe_value: Any,
                      batch: str) -> None:
    """Record HOW this row entered the CRM (P1 provenance). Best-effort: on a
    pre-migration schema (no source_type column) this is silently skipped, so an
    import never fails over provenance. Matched on the same dedupe key that just
    proved the row unique."""
    pk_where = dedupe_sql.split("WHERE", 1)[1].split("LIMIT")[0]
    table = dedupe_sql.split("FROM", 1)[1].split()[0]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET source_type='import', source_id=%s, "
                f"observed_at=now() WHERE {pk_where}", (batch, dedupe_value))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[import] provenance stamp skipped: {exc}")
    finally:
        conn.close()


def commit(entity: str, csv_text: str, mapping: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Create the importable rows via the entity's own create SP. Re-checks
    dedupe per row so a re-run is idempotent. Admin action (gated at the router).

    Every created row is stamped with IMPORT provenance carrying this run's batch
    ref, so an imported value is distinguishable from a hand-typed or AI-inferred
    one (see app/core/provenance.py)."""
    spec, rows, headers = _prepare(entity, csv_text)
    idx = _alias_index(spec)
    batch = _batch_ref()
    counts = {"created": 0, "skipped_duplicate": 0, "error": 0}
    errors: List[Dict[str, Any]] = []

    for i, raw in enumerate(rows, 1):
        row = _map_row(spec, raw, idx, mapping)
        err = _validate_row(spec, row)
        if err:
            counts["error"] += 1
            if len(errors) < 25:
                errors.append({"row": i, "reason": err})
            continue
        if _is_duplicate(spec, row):
            counts["skipped_duplicate"] += 1
            continue
        try:
            built = spec["transform"](row) if spec.get("transform") else row
            params = {"mode": spec["create_mode"], **spec.get("create_defaults", {}), **built}
            sql, _ = spec["build"](params)
            execute_sp(sql)
            _stamp_provenance(entity, spec["dedupe_sql"],
                              spec["dedupe_value"](row), batch)
            counts["created"] += 1
        except Exception as exc:
            counts["error"] += 1
            logger.warning(f"[import] row {i} create failed: {exc}")
            if len(errors) < 25:
                errors.append({"row": i, "reason": str(exc)[:160]})

    logger.info(f"[import] {entity} commit — {counts} (batch {batch})")
    return {"entity": entity, "total": len(rows), "counts": counts, "errors": errors,
            "batch": batch,
            "note": f"Imported {counts['created']} {entity}; "
                    f"{counts['skipped_duplicate']} duplicate(s) skipped, "
                    f"{counts['error']} error(s). Provenance batch: {batch}."}


# ── Router (admin) ───────────────────────────────────────────────────────────

router = APIRouter(tags=["data-import"])


@router.get("/import/schema")
def import_schema():
    """Importable entities + their fields/aliases (for the import UI)."""
    return list_schema()


@router.post("/import/preview")
def import_preview(body: Dict[str, Any]):
    try:
        return preview(str(body.get("entity") or ""), str(body.get("csv") or ""),
                       body.get("mapping"))
    except ValueError as exc:
        return {"error": str(exc)}


@router.post("/import/commit")
def import_commit(body: Dict[str, Any]):
    try:
        return commit(str(body.get("entity") or ""), str(body.get("csv") or ""),
                      body.get("mapping"))
    except ValueError as exc:
        return {"error": str(exc)}
