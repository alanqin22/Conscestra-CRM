"""Custom Fields — data-model extensibility, the LIGHT version (Platform P3).

Every business has fields a fixed schema can't model (real-estate MLS#, a piano
school's RCM level, a SaaS account's renewal date). This lets an admin declare
extra fields on the core entities WITHOUT a schema change and WITHOUT touching
the entity SPs — a sidecar defs+values store (sql/custom_fields.sql), mirroring
the data-defined `custom_agents` pattern.

The point (per the requirement) is that custom fields are NOT decorative metadata
— they are:
  • DB-aware   — the defs/values tables + this module,
  • UI-aware   — authored + edited from the frontend (setup.html + a values panel),
  • Agent-aware — injected into `context.hydrate()` and the entity ai_summary, so
                  an agent answering "show me our Enterprise accounts" SEES the
                  customer_tier field,
  • Analytics-aware — surfaced as dimensions/filters in the A2 semantic layer, so
                  "total pipeline by customer tier" works.

Values are stored as text and coerced/validated per the def's type. Reads
tolerate the tables being absent (feature simply returns empty).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection
from app.core import provenance as prov

logger = logging.getLogger("custom_fields")

# Provenance columns (P1) may not be migrated yet on every deployment. Detect
# once so custom-field writes/reads keep working on a pre-migration schema.
_HAS_PROV: Optional[bool] = None


def _has_provenance() -> bool:
    global _HAS_PROV
    if _HAS_PROV is not None:
        return _HAS_PROV
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.columns "
                        "WHERE table_name='custom_field_values' AND column_name='source_type'")
            _HAS_PROV = cur.fetchone() is not None
    except Exception:
        conn.rollback()
        _HAS_PROV = False
    finally:
        conn.close()
    return _HAS_PROV


_HAS_TYPED: Optional[bool] = None


def has_typed_columns() -> bool:
    """True once sql/custom_field_typed.sql is applied (value_number/date/bool).
    The semantic layer uses this to read the typed column instead of casting
    value_text — see semantic_model._cf_expr. Cached per process."""
    global _HAS_TYPED
    if _HAS_TYPED is not None:
        return _HAS_TYPED
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.columns "
                        "WHERE table_name='custom_field_values' "
                        "AND column_name='value_number'")
            _HAS_TYPED = cur.fetchone() is not None
    except Exception:
        conn.rollback()
        _HAS_TYPED = False
    finally:
        conn.close()
    return _HAS_TYPED


# Entities that can carry custom fields (must match a real uuid PK column).
ENTITIES = {
    "accounts":      "account_id",
    "contacts":      "contact_id",
    "leads":         "lead_id",
    "opportunities": "opportunity_id",
}
FIELD_TYPES = {"text", "number", "select", "date", "bool"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CustomFieldError(ValueError):
    """Raised on invalid defs or values (message is safe to show the caller)."""


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")
    return s[:40]


# ============================================================================
# Coercion / validation of a value against its def
# ============================================================================

def _coerce(value: Any, ftype: str, options: List[str]) -> Optional[str]:
    """Validate `value` for a field of `ftype`; return the TEXT to store (or
    None to clear). Raises CustomFieldError on invalid input."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if ftype == "number":
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise CustomFieldError(f"'{value}' is not a number")
        return str(int(f)) if f.is_integer() else str(f)
    if ftype == "bool":
        s = str(value).strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return "true"
        if s in ("false", "0", "no", "n", "off"):
            return "false"
        raise CustomFieldError(f"'{value}' is not a boolean")
    if ftype == "date":
        s = str(value).strip()
        if not _DATE_RE.match(s):
            raise CustomFieldError(f"'{value}' must be a date (YYYY-MM-DD)")
        return s
    if ftype == "select":
        s = str(value).strip()
        if options and s not in options:
            raise CustomFieldError(f"'{s}' is not an allowed option ({', '.join(options)})")
        return s
    return str(value).strip()   # text


def _decode(value_text: Optional[str], ftype: str) -> Any:
    if value_text is None:
        return None
    if ftype == "number":
        try:
            f = float(value_text)
            return int(f) if f.is_integer() else f
        except ValueError:
            return value_text
    if ftype == "bool":
        return value_text == "true"
    return value_text


# ============================================================================
# Definitions
# ============================================================================

def list_defs(entity: Optional[str] = None, active_only: bool = True) -> List[Dict[str, Any]]:
    where, params = [], []
    if entity:
        where.append("entity = %s"); params.append(entity)
    if active_only:
        where.append("is_active")
    sql = ("SELECT field_id::text, entity, field_key, label, field_type, options, "
           "required, is_active, sort_order FROM custom_field_defs")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY entity, sort_order, field_key"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            out = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.debug(f"[custom_fields] list_defs skipped: {exc}")
        return []
    finally:
        conn.close()
    for d in out:
        if isinstance(d.get("options"), str):
            try:
                d["options"] = json.loads(d["options"])
            except json.JSONDecodeError:
                d["options"] = []
    return out


def _defs_map(entity: str) -> Dict[str, Dict[str, Any]]:
    return {d["field_key"]: d for d in list_defs(entity)}


def create_def(body: Dict[str, Any]) -> Dict[str, Any]:
    entity = str(body.get("entity") or "").strip()
    if entity not in ENTITIES:
        raise CustomFieldError(f"unknown entity '{entity}'. Valid: {', '.join(ENTITIES)}")
    ftype = str(body.get("field_type") or "text").strip().lower()
    if ftype not in FIELD_TYPES:
        raise CustomFieldError(f"unknown type '{ftype}'. Valid: {', '.join(FIELD_TYPES)}")
    key = str(body.get("field_key") or "").strip() or _slug(str(body.get("label") or ""))
    if not _KEY_RE.match(key):
        raise CustomFieldError("field_key must be a slug: lowercase letter, then letters/digits/underscore")
    label = str(body.get("label") or key).strip()
    options = body.get("options") or []
    if ftype == "select" and not options:
        raise CustomFieldError("a 'select' field needs a non-empty options list")
    options = [str(o).strip() for o in options if str(o).strip()]

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO custom_field_defs (entity, field_key, label, field_type, options, required, sort_order)
                   VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)
                   ON CONFLICT (entity, field_key) DO UPDATE SET
                     label=EXCLUDED.label, field_type=EXCLUDED.field_type,
                     options=EXCLUDED.options, required=EXCLUDED.required,
                     is_active=true, updated_at=now()
                   RETURNING field_id::text""",
                (entity, key, label, ftype, json.dumps(options),
                 bool(body.get("required")), int(body.get("sort_order") or 0)))
            fid = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "field_id": fid, "entity": entity, "field_key": key}


def delete_def(entity: str, field_key: str, purge_values: bool = False) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE custom_field_defs SET is_active=false, updated_at=now() "
                        "WHERE entity=%s AND field_key=%s", (entity, field_key))
            n = cur.rowcount
            if purge_values:
                cur.execute("DELETE FROM custom_field_values WHERE entity=%s AND field_key=%s",
                            (entity, field_key))
        conn.commit()
    finally:
        conn.close()
    return {"ok": bool(n), "deactivated": n}


# ============================================================================
# Values
# ============================================================================

def get_values(entity: str, entity_id: str) -> Dict[str, Any]:
    """{field_key: coerced_value} for one record — only keys that have a value
    AND an active def. Best-effort (empty on any error / missing tables)."""
    if entity not in ENTITIES or not entity_id:
        return {}
    defs = _defs_map(entity)
    if not defs:
        return {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT field_key, value_text FROM custom_field_values "
                        "WHERE entity=%s AND entity_id=%s::uuid", (entity, entity_id))
            rows = cur.fetchall()
    except Exception as exc:
        logger.debug(f"[custom_fields] get_values skipped: {exc}")
        return {}
    finally:
        conn.close()
    out: Dict[str, Any] = {}
    for key, vt in rows:
        d = defs.get(key)
        if d and vt is not None:
            out[key] = _decode(vt, d["field_type"])
    return out


def _values_prov(entity: str, entity_id: str) -> Dict[str, Dict[str, Any]]:
    """{field_key: {source_type, source_id, confidence, observed_at}} — best-effort
    (empty if provenance isn't migrated / on any error)."""
    if not _has_provenance():
        return {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT field_key, source_type, source_id, confidence, observed_at "
                        "FROM custom_field_values WHERE entity=%s AND entity_id=%s::uuid",
                        (entity, entity_id))
            rows = cur.fetchall()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[custom_fields] provenance read skipped: {exc}")
        return {}
    finally:
        conn.close()
    out: Dict[str, Dict[str, Any]] = {}
    for k, st, sid, conf, obs in rows:
        out[k] = {"source_type": st, "source_id": sid,
                  "confidence": float(conf) if conf is not None else None,
                  "observed_at": obs.isoformat() if hasattr(obs, "isoformat") else obs}
    return out


def get_values_labeled(entity: str, entity_id: str) -> List[Dict[str, Any]]:
    """[{key,label,type,value,provenance,provenance_label}] in def order — for
    UI/context rendering. provenance keys are present only once the envelope is
    migrated and the value has a recorded source."""
    defs = list_defs(entity)
    vals = get_values(entity, entity_id)
    pmap = _values_prov(entity, entity_id)
    out = []
    for d in defs:
        k = d["field_key"]
        if k in vals:
            item = {"key": k, "label": d["label"], "type": d["field_type"], "value": vals[k]}
            p = pmap.get(k)
            if p and p.get("source_type"):
                item["provenance"] = p
                item["provenance_label"] = prov.describe(p.get("source_type"), p.get("confidence"))
            out.append(item)
    return out


def _typed_parts(ftype: str, value_text: str):
    """(value_number, value_date, value_bool) for an ALREADY-VALIDATED value —
    only the column matching `ftype` is populated, the rest are NULL."""
    if ftype == "number":
        try:
            return float(value_text), None, None
        except (TypeError, ValueError):
            return None, None, None
    if ftype == "date":
        return None, value_text, None
    if ftype == "bool":
        return None, None, value_text == "true"
    return None, None, None


def set_values(entity: str, entity_id: str, values: Dict[str, Any],
               source: "Optional[prov.Provenance]" = None) -> Dict[str, Any]:
    """Validate each value against its def and upsert with its PROVENANCE. Unknown
    keys are rejected. A None/empty value clears that field. `source` records who
    asserted these values (default: a HUMAN edit — the UI path); AI/import/enrich
    callers pass their own Provenance so the value's trust is recorded."""
    if entity not in ENTITIES:
        raise CustomFieldError(f"unknown entity '{entity}'")
    defs = _defs_map(entity)
    prepared = []   # (key, value_text_or_None)
    for key, raw in (values or {}).items():
        d = defs.get(key)
        if not d:
            raise CustomFieldError(f"unknown field '{key}' for {entity}")
        prepared.append((key, _coerce(raw, d["field_type"], d.get("options") or []),
                         d["field_type"]))

    pc = (source or prov.Provenance()).as_columns()
    has_prov = _has_provenance()
    has_typed = has_typed_columns()
    conn = get_connection()
    written = 0
    try:
        with conn.cursor() as cur:
            for key, vt, ftype in prepared:
                if vt is None:
                    cur.execute("DELETE FROM custom_field_values WHERE entity=%s AND entity_id=%s::uuid AND field_key=%s",
                                (entity, entity_id, key))
                elif has_prov:
                    cur.execute(
                        """INSERT INTO custom_field_values
                             (entity, entity_id, field_key, value_text,
                              source_type, source_id, confidence, observed_at, updated_at)
                           VALUES (%s,%s::uuid,%s,%s,%s,%s,%s,%s,now())
                           ON CONFLICT (entity, entity_id, field_key)
                           DO UPDATE SET value_text=EXCLUDED.value_text,
                             source_type=EXCLUDED.source_type, source_id=EXCLUDED.source_id,
                             confidence=EXCLUDED.confidence, observed_at=EXCLUDED.observed_at,
                             updated_at=now()""",
                        (entity, entity_id, key, vt,
                         pc["source_type"], pc["source_id"], pc["confidence"], pc["observed_at"]))
                else:
                    cur.execute(
                        """INSERT INTO custom_field_values (entity, entity_id, field_key, value_text, updated_at)
                           VALUES (%s,%s::uuid,%s,%s,now())
                           ON CONFLICT (entity, entity_id, field_key)
                           DO UPDATE SET value_text=EXCLUDED.value_text, updated_at=now()""",
                        (entity, entity_id, key, vt))
                # Keep the TYPED columns in step so the analytics layer never
                # casts value_text (blind spot #7). Values are already validated
                # by _coerce, so these can't fail.
                if has_typed and vt is not None:
                    num, dt, bl = _typed_parts(ftype, vt)
                    cur.execute(
                        "UPDATE custom_field_values SET value_number=%s, "
                        "value_date=%s, value_bool=%s "
                        "WHERE entity=%s AND entity_id=%s::uuid AND field_key=%s",
                        (num, dt, bl, entity, entity_id, key))
                written += 1
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "written": written, "values": get_values(entity, entity_id)}


def enrich(record: Dict[str, Any], entity: str, entity_id: str) -> Dict[str, Any]:
    """Attach a `custom_fields` dict to a record read (agent-aware helper)."""
    try:
        cf = get_values(entity, entity_id)
        if cf:
            record["custom_fields"] = cf
    except Exception as exc:
        logger.debug(f"[custom_fields] enrich skipped: {exc}")
    return record


# ============================================================================
# Router (admin)
# ============================================================================

router = APIRouter(tags=["custom-fields"])


@router.get("/custom-fields/defs")
def cf_list_defs(entity: Optional[str] = None):
    return {"entities": list(ENTITIES), "types": sorted(FIELD_TYPES),
            "defs": list_defs(entity, active_only=False)}


@router.post("/custom-fields/defs")
def cf_create_def(body: Dict[str, Any]):
    try:
        return create_def(body)
    except CustomFieldError as e:
        return {"ok": False, "error": str(e)}


@router.delete("/custom-fields/defs/{entity}/{field_key}")
def cf_delete_def(entity: str, field_key: str, purge_values: bool = False):
    return delete_def(entity, field_key, purge_values)


@router.get("/custom-fields/values/{entity}/{entity_id}")
def cf_get_values(entity: str, entity_id: str):
    return {"entity": entity, "entity_id": entity_id,
            "fields": get_values_labeled(entity, entity_id)}


@router.put("/custom-fields/values/{entity}/{entity_id}")
def cf_set_values(entity: str, entity_id: str, body: Dict[str, Any]):
    try:
        # Optional {"provenance": {source_type, source_id, confidence}}; default HUMAN.
        source = prov.from_input(body.get("provenance")) if isinstance(body, dict) else None
        return set_values(entity, entity_id, body.get("values") or body, source=source)
    except CustomFieldError as e:
        return {"ok": False, "error": str(e)}
