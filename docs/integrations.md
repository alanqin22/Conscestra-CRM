# Conscestra CRM — External Integrations

## 1. Calendar feed (Google Calendar / Outlook)

Subscribe to CRM activities as a live calendar:

```
https://<backend>/calendar/activities.ics?token=<CALENDAR_FEED_TOKEN>
```

- Google Calendar → Other calendars → **From URL** → paste the address.
- Outlook → Add calendar → **Subscribe from web**.
- Feed window: activities from 30 days back to 180 days ahead (max 2000),
  meetings/calls/tasks with account context; all-day events supported.
- **Security:** calendar clients can't send auth headers, so access uses the
  secret-address pattern. Set `CALENDAR_FEED_TOKEN` in env (any long random
  string) and include it as `?token=`. Unset = open (demo posture only).
- Refresh cadence is controlled by the calendar provider (Google: every few
  hours). One-way, read-only — CRM remains the source of truth.

## 2. ERP bridge (QuickBooks / Xero) — CSV export

Admin-gated exports, importable by QuickBooks/Xero and any accounting tool:

```
GET /erp/export/invoices.csv?date_from=2026-01-01&date_to=2026-06-30
GET /erp/export/payments.csv?date_from=2026-01-01
Header: X-Admin-Token: <ADMIN_API_TOKEN>   (or an admin session)
```

Columns:
- invoices: InvoiceNo, Customer, InvoiceDate, DueDate, Status, Subtotal, Tax,
  Total, BalanceDue, Currency, ExternalId
- payments: PaymentDate, Customer, InvoiceNo, Amount, Method, RefNumber,
  Status, Currency, ExternalId

**Roadmap note:** the `invoices` / `payments` tables already carry
`external_id` + `external_source` columns and payments have `reconciled`
flags — a future two-way QuickBooks OAuth sync can key on those without
schema changes. This CSV bridge is the deliberate first step.

## 3. SSE notification push

`GET /notifications/stream` — Server-Sent Events stream (`hello` with unread
count, `notification` per new row, `ping` keep-alive). notifications-mgmt.html
subscribes via EventSource: live "Live · N unread" badge + toast on arrival,
no client polling. Server-side it's a 3-second indexed delta query per
connection; reconnect is native EventSource behaviour (`?since=` backfills).

## 4. MCP server

See `docs/mcp_server.md` — the CRM's agents + A2A registry as MCP tools for
external AI assistants.
