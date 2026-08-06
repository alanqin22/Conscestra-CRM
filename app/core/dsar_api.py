"""HTTP for data-subject requests — two endpoints with deliberately different powers.

    POST /dsar/export     OPERATOR. Returns the data. Admin-gated in main.py.
    POST /dsar/request    SUBJECT.  Records that they asked. Never returns data.
    GET  /dsar/requests   OPERATOR. Open requests and time remaining.

WHY THE SUBJECT ENDPOINT DOES NOT RETURN DATA

The asymmetry is the design, not an omission. This export reaches
`customer_memories`, `audit_log` and `agent_blackboard`. A subject is entitled to
their personal data in all three — and those tables also hold internal working
notes and, in places, other people's identifiers. Returning them automatically on
presentation of a session cookie would turn one stolen session into a bulk
disclosure.

It would also skip a judgement the code cannot make. `dsar.export_subject`
withholds account-scoped sections when an account has other contacts, which
handles the STRUCTURAL Art. 15(4) cases. It cannot read a free-text note and tell
whose life it describes. That last step needs a person, which is why the statute
gives one a month rather than requiring an instant answer.

So the subject gets a receipt and a clock; an operator runs the export.

WHY THE SUBJECT IS TAKEN ONLY FROM THE SESSION

`/dsar/request` ignores any identifier in the request body. Accepting one would
let anyone holding any valid session open a request against anyone else — and an
operator later acts on that record, so a forged request becomes a disclosure with
a paper trail that looks legitimate.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from app.core.database import get_connection
from app.core.dsar import (IncompleteExport, export_subject, to_json)

logger = logging.getLogger("dsar.api")

router = APIRouter(tags=["dsar"])


def _record_subject_request(subject_type: str, subject_id: str,
                            verified_via: str, kind: str,
                            note: Optional[str], channel: str) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO dsar_subject_requests
                     (subject_type, subject_id, verified_via, request_kind,
                      subject_note, channel)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   RETURNING request_id, received_at, due_at""",
                (subject_type, str(subject_id), verified_via, kind,
                 (note or "")[:2000] or None, channel))
            rid, received, due = cur.fetchone()
        conn.commit()
        return {"request_id": rid, "received_at": received, "due_at": due}
    finally:
        conn.close()


def _session_from(request: Request) -> Optional[Dict[str, Any]]:
    token = None
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    token = token or request.headers.get("x-session-token")
    if not token:
        return None
    try:
        from app.agents.auth.router import get_session
        return get_session(token)
    except Exception as exc:                                    # noqa: BLE001
        logger.debug(f"[dsar] session verify failed: {exc}")
        return None


@router.post("/dsar/export")
def api_export(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Operator export.

    POST, not GET, deliberately: an export is a disclosure event recorded in
    `dsar_requests`, and a GET invites it into browser history, proxy logs and
    link prefetchers."""
    stype = str(payload.get("subject_type") or "").strip()
    sid = str(payload.get("subject_id") or "").strip()
    if stype not in ("contact", "lead", "account", "email") or not sid:
        raise HTTPException(
            400, "subject_type must be contact|lead|account|email, "
                 "and subject_id is required")
    try:
        exp = export_subject(
            stype, sid,
            requested_by=str(payload.get("requested_by") or "api:admin"),
            purpose=str(payload.get("purpose") or "Art. 15 access request"),
            strict=not bool(payload.get("allow_incomplete")))
    except IncompleteExport as exc:
        # 409, not 500. The request was valid; the system refused on purpose
        # because the manifest no longer covers the schema.
        raise HTTPException(409, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))

    rid = payload.get("fulfils_request_id")
    if rid:
        try:
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE dsar_subject_requests
                              SET status='fulfilled', fulfilled_at=now(),
                                  fulfilled_export_id=%s::uuid, handled_by=%s
                            WHERE request_id=%s AND status='received'""",
                        (exp["meta"]["export_id"],
                         str(payload.get("requested_by") or "api:admin"), rid))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:                                # noqa: BLE001
            # The disclosure happened; only the bookkeeping failed. Say so
            # rather than failing the response and inviting a second export.
            logger.warning(f"[dsar] export {exp['meta']['export_id']} did not "
                           f"close request {rid}: {exc}")
    return json.loads(to_json(exp))


@router.post("/dsar/request")
def api_subject_request(request: Request,
                        payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """A signed-in subject asks for their data. Returns a reference, not data."""
    sess = _session_from(request)
    if not sess:
        # Identical wording whether the token was absent, expired or forged —
        # a different message for each is an enumeration oracle.
        raise HTTPException(401, "sign in to request your data")

    if sess.get("contact_id"):
        stype, sid = "contact", sess["contact_id"]
    elif sess.get("lead_id"):
        stype, sid = "lead", sess["lead_id"]
    elif sess.get("account_id"):
        stype, sid = "account", sess["account_id"]
    else:
        raise HTTPException(403, "this session is not linked to a person")

    kind = str(payload.get("kind") or "access").strip().lower()
    if kind not in ("access", "portability", "erasure"):
        kind = "access"

    try:
        rec = _record_subject_request(
            stype, sid, "auth_session", kind,
            payload.get("note"), str(payload.get("channel") or "portal"))
    except Exception as exc:                                    # noqa: BLE001
        # A request we failed to record is a request that will be missed, and
        # the statutory clock runs whether or not we wrote it down. ERROR, and
        # tell the subject a route that does not depend on this code path.
        logger.error(f"[dsar] FAILED to record a subject request: {exc}")
        raise HTTPException(
            503, "we could not record your request — please email "
                 "privacy@agentorc.ca so that it is not lost")

    logger.warning(f"[dsar] subject request {rec['request_id']} received "
                   f"({kind}, {stype}) — respond by {rec['due_at']}")
    return {"ok": True,
            "request_id": rec["request_id"],
            "received_at": str(rec["received_at"]),
            "respond_by": str(rec["due_at"]),
            "message": "We have recorded your request and will respond within "
                       "30 days. Quote your reference number if you contact "
                       "privacy@agentorc.ca."}


@router.get("/dsar/requests")
def api_open_requests() -> Dict[str, Any]:
    """Open requests and time remaining.

    Exists because a request recorded and never looked at is worse than one
    never recorded: it proves the clock started."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT request_id, subject_type, subject_id, request_kind,
                          received_at, due_at, status,
                          EXTRACT(EPOCH FROM (due_at - now())) / 86400.0
                     FROM dsar_subject_requests
                    WHERE status = 'received'
                    ORDER BY due_at""")
            rows = [{"request_id": r[0], "subject_type": r[1],
                     "subject_id": r[2], "kind": r[3],
                     "received_at": str(r[4]), "due_at": str(r[5]),
                     "status": r[6], "days_remaining": round(float(r[7]), 1),
                     "overdue": float(r[7]) < 0} for r in cur.fetchall()]
    finally:
        conn.close()
    return {"open": len(rows),
            "overdue": sum(1 for r in rows if r["overdue"]),
            "requests": rows}
