"""External integrations — calendar feed + ERP accounting exports.

1. CALENDAR (public, token-guarded):
     GET /calendar/activities.ics[?token=…]
   RFC 5545 iCalendar feed of CRM activities (meetings, calls, tasks) that
   Google Calendar / Outlook can SUBSCRIBE to ("from URL"). Calendar clients
   can't send auth headers, so access uses the secret-address pattern: set
   CALENDAR_FEED_TOKEN in env and include ?token=<value> in the URL. When the
   env var is unset the feed follows the demo public-read posture.

2. ERP BRIDGE (admin-gated):
     GET /erp/export/invoices.csv?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
     GET /erp/export/payments.csv?date_from=…&date_to=…
   QuickBooks/Xero-importable CSV of invoices and payments. The
   invoices/payments tables already carry external_id/external_source
   columns, so a future two-way OAuth sync can build on this bridge.
"""
from __future__ import annotations

import csv
import io
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.core.database import get_connection

logger = logging.getLogger("integrations")

router_public = APIRouter(tags=["integrations"])
router_admin = APIRouter(tags=["integrations"])


# ============================================================================
# Calendar — iCalendar (ICS) feed of activities
# ============================================================================

def _ics_escape(s: str) -> str:
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """RFC 5545 §3.1 — lines longer than 75 octets are folded."""
    out, cur = [], line
    while len(cur.encode("utf-8")) > 74:
        cut = 74
        while cut > 1 and len(cur[:cut].encode("utf-8")) > 74:
            cut -= 1
        out.append(cur[:cut])
        cur = " " + cur[cut:]
    out.append(cur)
    return "\r\n".join(out)


def _dt(ts) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_TYPE_ICON = {"meeting": "📅", "call": "📞", "task": "✅", "email": "✉️"}


def _fetch_activities() -> List[tuple]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.activity_id::text, a.type, a.status, a.subject, a.description,
                       COALESCE(a.start_at, a.due_at) AS starts,
                       a.end_at, a.is_all_day,
                       COALESCE(ac.account_name, '') AS account_name,
                       a.updated_at
                FROM activities a
                LEFT JOIN accounts ac ON ac.account_id = a.account_id
                WHERE COALESCE(a.start_at, a.due_at) IS NOT NULL
                  AND COALESCE(a.start_at, a.due_at)
                      BETWEEN now() - interval '30 days' AND now() + interval '180 days'
                ORDER BY 6
                LIMIT 2000""")
            return cur.fetchall()
    finally:
        conn.close()


@router_public.get("/calendar/activities.ics")
def calendar_feed(token: Optional[str] = None):
    required = (os.getenv("CALENDAR_FEED_TOKEN", "") or "").strip()
    if required and token != required:
        raise HTTPException(403, "invalid or missing calendar token")

    rows = _fetch_activities()
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Conscestra CRM//Activities//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _ics_fold("X-WR-CALNAME:Conscestra CRM — Activities"),
        "X-WR-TIMEZONE:UTC",
    ]
    for (aid, typ, status, subject, desc, starts, ends, all_day,
         account, updated) in rows:
        icon = _TYPE_ICON.get((typ or "").lower(), "📌")
        summary = f"{icon} {subject or typ or 'CRM activity'}"
        if status == "completed":
            summary += " (done)"
        body_bits = [b for b in [
            (desc or "").strip(),
            f"Account: {account}" if account else "",
            f"Type: {typ} · Status: {status}",
        ] if b]
        lines.append("BEGIN:VEVENT")
        lines.append(_ics_fold(f"UID:{aid}@conscestra-crm"))
        lines.append(f"DTSTAMP:{_dt(updated or now)}")
        if all_day:
            lines.append(f"DTSTART;VALUE=DATE:{starts.strftime('%Y%m%d')}")
        else:
            lines.append(f"DTSTART:{_dt(starts)}")
            lines.append(f"DTEND:{_dt(ends or (starts + timedelta(minutes=30)))}")
        lines.append(_ics_fold(f"SUMMARY:{_ics_escape(summary)}"))
        if body_bits:
            lines.append(_ics_fold(f"DESCRIPTION:{_ics_escape(chr(10).join(body_bits))}"))
        lines.append(f"STATUS:{'CANCELLED' if status == 'cancelled' else 'CONFIRMED'}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")

    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar",
                    headers={"Content-Disposition": 'inline; filename="conscestra-activities.ics"'})


# ============================================================================
# ERP bridge — QuickBooks/Xero-importable CSV exports
# ============================================================================

def _csv_response(header: List[str], rows: List[tuple], filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _range(date_from: Optional[str], date_to: Optional[str]) -> tuple:
    try:
        f = datetime.fromisoformat(date_from).date() if date_from else None
        t = datetime.fromisoformat(date_to).date() if date_to else None
    except ValueError:
        raise HTTPException(400, "date_from/date_to must be YYYY-MM-DD")
    return f, t


@router_admin.get("/erp/export/invoices.csv")
def export_invoices(date_from: Optional[str] = None, date_to: Optional[str] = None):
    f, t = _range(date_from, date_to)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.invoice_number, COALESCE(a.account_name,''),
                       i.issue_date::date, i.due_date::date, i.status,
                       ROUND(i.subtotal_amount::numeric,2), ROUND(i.tax_amount::numeric,2),
                       ROUND(i.total_amount::numeric,2), ROUND(i.balance_due::numeric,2),
                       COALESCE(i.currency,'CAD'), COALESCE(i.external_id,'')
                FROM invoices i
                LEFT JOIN accounts a ON a.account_id = i.account_id
                WHERE (i.is_deleted IS NULL OR i.is_deleted = false)
                  AND (%s::date IS NULL OR i.issue_date::date >= %s::date)
                  AND (%s::date IS NULL OR i.issue_date::date <= %s::date)
                ORDER BY i.issue_date, i.invoice_number""", (f, f, t, t))
            rows = cur.fetchall()
    finally:
        conn.close()
    logger.info(f"[erp] invoices export: {len(rows)} rows ({date_from}..{date_to})")
    return _csv_response(
        ["InvoiceNo", "Customer", "InvoiceDate", "DueDate", "Status",
         "Subtotal", "Tax", "Total", "BalanceDue", "Currency", "ExternalId"],
        rows, "conscestra-invoices.csv")


@router_admin.get("/erp/export/payments.csv")
def export_payments(date_from: Optional[str] = None, date_to: Optional[str] = None):
    f, t = _range(date_from, date_to)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.payment_date::date, COALESCE(a.account_name,''),
                       COALESCE(i.invoice_number,''), ROUND(p.amount::numeric,2),
                       COALESCE(p.payment_method,''),
                       COALESCE(p.reference_number, p.transaction_reference, ''),
                       p.status, COALESCE(p.currency,'CAD'), COALESCE(p.external_id,'')
                FROM payments p
                LEFT JOIN accounts a ON a.account_id = p.account_id
                LEFT JOIN invoices i ON i.invoice_id = p.invoice_id
                WHERE (p.is_deleted IS NULL OR p.is_deleted = false)
                  AND (%s::date IS NULL OR p.payment_date::date >= %s::date)
                  AND (%s::date IS NULL OR p.payment_date::date <= %s::date)
                ORDER BY p.payment_date""", (f, f, t, t))
            rows = cur.fetchall()
    finally:
        conn.close()
    logger.info(f"[erp] payments export: {len(rows)} rows ({date_from}..{date_to})")
    return _csv_response(
        ["PaymentDate", "Customer", "InvoiceNo", "Amount", "Method",
         "RefNumber", "Status", "Currency", "ExternalId"],
        rows, "conscestra-payments.csv")
