# Self-service order status and cancellation

**Built 2026-08-21.** An Order Status button in the lifecycle emails, a public
order page it opens, an email-OTP cancellation for orders that have not shipped,
and the return policy for orders that have.

---

## What this is not

It is **not** a new cancellation feature. The cancellation gate was built for the
phone line in August 2026 and is unchanged:

| Rule | Where it lives | Not restated in |
|---|---|---|
| Cancellable: `pending` / `processing` / `ready` | `voice_support.CANCELLABLE_STATUSES`, and really the `WHERE` clause inside `cancel_order_sp` | `order_status.py` |
| Too late: `shipped` / `delivered` / `completed` | `voice_support.TOO_LATE_STATUSES` | `order_status.py` |
| Return policy text | a published KB article | `order_status.py`, `return-policy.html` |
| The customer's confirmation email | `order_notifications.notify(…, 'order.cancelled')` | — |
| The audit ledger row | `governance.record_preauthorized` | — |
| Telling a human | `voice_support._notify_employee_of_cancellation` | — |

A second copy of any of these would be a second policy, and the weaker of the two
would decide what a customer can do. `tests/test_order_status_self_service.py`
asserts this structurally: it greps `order_status.py` for status literals and
fails if one appears.

What is genuinely new is **an entry point** and **an authorization model**.

---

## The authorization split

```
                       VIEW                         CANCEL
  phone line     caller ID + order no.       4 record facts + SMS OTP
                                             to the number ON FILE

  this page      HMAC-signed link            the link  +  email OTP
                 (unforgeable, stateless)    to the address ON THE ORDER
```

**Why the link alone cannot cancel.** Possession of the link is not possession of
the account. Confirmation emails get forwarded; shared purchasing mailboxes are
normal. So the link opens a read-only page — a forwarded email lets someone
*look* — and the write requires a code sent to the address the **order** holds,
never to one the request supplies. A link holder who is not the customer can
request codes all day and every one lands in the customer's mailbox, where it is
evidence rather than access.

**The token.** `HMAC-SHA256(secret, "order-status:v1:" + order_id)`, truncated to
128 bits, compared with `compare_digest`. The `order-status:v1:` prefix is
domain separation: `order_status._secret()` falls back to `UNSUBSCRIBE_SECRET`
when `ORDER_LINK_SECRET` is unset, and without the prefix a token minted for one
purpose could be replayed as the other.

**No secret ⇒ no button.** `order_status_url()` returns `None`, and the email is
composed and sent without it. The alternatives were signing with a constant (any
order readable by anyone) or a per-process secret (every link dead at the next
restart).

---

## The flow

```
  order.created / shipped / delivered email
              │  [ Order Status ]
              ▼
  GET /order-status?o=…&t=…          ← page, carries no data
  GET /order-status/summary          ← verifies the HMAC, returns the order
              │
     disposition (server-side, from voice_support's sets)
              │
   ┌──────────┼─────────────────────────┐
   │          │                         │
 cancellable  too_late                other
   │          │                         │
   │          └─ return policy       a colleague follows up
   │             (KB article)
   │
   ├─ POST /order-status/cancel/request   → status checked FIRST, then a code
   │                                        is mailed to the address on record
   └─ POST /order-status/cancel/confirm   → code consumed under FOR UPDATE,
                                            then cancel_order_sp
```

The status is checked **before** a code is sent, so a shipped order never costs
the customer an email they cannot use — and the too-late answer arrives with the
return policy attached rather than as a bare refusal.

Between the code check and the write there is **no** Python status re-check. The
predicate is inside `cancel_order_sp`'s `WHERE` clause, evaluated by Postgres
under a row lock. A read-then-check-then-write would leave a window in which the
order ships between check and write, and the customer would then be told their
shipped order was cancelled.

---

## Why the verification is a table

A call is one process holding one conversation open, so the phone line keeps its
pending OTP in the call session. A web link cannot: "send me a code" and "here is
the code" are separate HTTP requests that may land on different replicas
(`HA_LEADER_ELECTION` implies more than one). An in-process dict would pass every
single-worker test and then fail in production as a wrong-code error for a
customer who typed the right code.

`order_cancel_verifications` therefore holds the pending code, and:

- only the **SHA-256 hash** is stored — a dump must not let anyone complete a
  cancellation, and support staff must not be able to read a live code;
- issuing a new code **closes the previous one** (`consumed_result='superseded'`),
  so `attempts` is a budget per *order*, not per *code*. Without that, three
  guesses becomes three per request and is unbounded by re-requesting;
- the row is **consumed inside the transaction that reads it**, under
  `FOR UPDATE`. A double-clicked form would otherwise pass verification twice and
  race into the cancellation;
- the row is **committed before the send**. A crash between the two leaves an
  unused code that expires harmlessly; the reverse ordering would mail a code
  that nothing can verify.

Rate limiting reuses `voice_verification_attempts` — the same durable, fail-closed
counter the phone line uses — keyed on `web:link:<sha256(order_id)>` and
`web:dest:<sha256(email)>`. Both are things the requester cannot rotate. There is
deliberately **no** counter on client IP: it is trivially rotated, so it would
constrain only the honest.

**Viewing is never rate-limited.** It is gated by an unforgeable 128-bit
signature, writes nothing and costs nothing to send. Failing it closed on a
limiter outage would break a real customer's link for no security at all. The
cancel path fails closed, exactly as the phone's does.

---

## The cancellation reason

The page asks **why** before it will send a code. The reason is required, and
`prefer_not_to_say` is why that is not a barrier: an optional field produces a
mostly-empty column no one can report on, while a mandatory one with no opt-out
would stop a customer cancelling their own order because they would not answer a
market-research question.

**The server owns both halves.** `order_status.CANCEL_REASONS` maps code to
label; the page *fetches* it from `GET /order-status/cancel/reasons` and posts
back a key. It never posts the label, and a key outside the dict is refused. Two
things go wrong otherwise: the analytics become a pile of near-duplicate strings
that no `GROUP BY` can add up, and unvalidated text reaches the staff
notification and the audit record, which are read by people.

Free text is accepted only with the `other` code, capped at 300 characters.
Detail sent alongside a fixed answer is dropped rather than rejected — the record
must not carry a comment attached to a canned reason.

**Validated before anything is spent.** A malformed reason is an incomplete form,
not a decision about the order, so it is refused before the status lookup and
before any verification email is sent.

**Written once, read from the row.** The reason is stored on
`order_cancel_verifications` at request time and read back at confirm time. The
browser could post a different one on the second request, and the audit record
must say what the customer chose on the page — there is a test for exactly that.

The carrier row is swept after 30 days. The durable copies go to
`audit_log.payload` (code + detail) and `action_approvals.params` (code, detail
and the rendered label), plus the staff notification. `v_order_cancel_reasons`
reports over the carrier and filters `consumed_result = 'verified'`: an abandoned
or locked-out verification carries a reason for a cancellation that never
happened, and counting those would overstate every category.

The phone line does not ask for a reason. `cancel_order_sp` writes `null` rather
than a blank — a "Reason: —" line reads as *the customer declined to say* when
the truth is *we never asked*.

To change the options, edit `CANCEL_REASONS` and nothing else. Keep existing keys
stable: they are what historical audit rows recorded, and renaming one silently
re-labels the past.

---

## The assistant

The page carries a free-text box. It **routes intent; it never performs the
write.** Recognising "I want to cancel" returns `action: "offer_cancel"`, and the
page walks the identical two-step OTP gate the button walks. If the assistant
could cancel there would be two gates, and a mis-parsed or injected sentence
would walk the weaker one.

Facts about the order — status, dates, totals — are **rendered from the record**,
never generated. Only the open-ended tail reaches a model, fenced by
`audience='public'` KB text (the reach invariant), with the order's own facts
injected so it never has to guess them.

---

## The return policy

`return-policy.html` contains **no policy text**. It fetches
`/return-policy/content`, which reads the published KB article — the same article
`voice_support._return_policy_answer` reads out on the phone. Editing it in
`knowledge-mgmt.html` moves the phone line, the chat, the order page and the
website together, because there is only one of them.

When the KB is unreachable, both the API and the page say so and route to a
human. Inventing terms on a page a customer may rely on to get their money back
is the one failure mode this page must not have.

---

## Bug found and fixed on the way

`cancel_order_sp` wrote `"channel": "voice-support"` as a **constant** into its
`audit_log` payload. It is now the single cancellation write for every channel,
so the first web cancellation filed itself as a phone call. `channel` is now a
parameter, defaulted to `voice-support` so existing voice callers are unchanged.
The same fix was applied to `_notify_employee_of_cancellation`, whose emitted
event and notification metadata were hardcoded the same way.

An audit trail that misreports the channel is worse than one that omits it.

---

## Where the link points — the API origin is not the customer's origin

The three deploy targets are independent, and this feature is the first one that
had to care:

```
  HTML   repo-root *.html  --SFTP-->  agentorc.ca      (static file server)
  API    app/**            --git-->   Railway
  DB     sql/, sp/         --psql-->  Railway Postgres
```

**Nothing under `*.html` is in git** — it is gitignored by policy, and
`git ls-files '*.html'` returns zero. So the backend physically cannot serve
these pages in production. Measured 2026-08-21:

```
  railway  /store-home.html   500      <- FileResponse on a file that isn't there
  agentorc /store-home.html   200
```

A link built from `APP_URL` would therefore land **every customer on a 500** —
and would show them an `orbitcrm-production.up.railway.app` hostname inside an
order confirmation, which reads as phishing whatever it actually does.

So there are two origins:

| | variable | used for |
|---|---|---|
| API | `APP_URL` | the endpoints, and the CASL unsubscribe link (an endpoint, not a page) |
| Site | `PUBLIC_SITE_URL` | the Order Status button in customer email |

`PUBLIC_SITE_URL` falls back to `APP_URL`, so local development — where one
process serves both — is unchanged and needs no second variable.

Three consequences:

1. The emailed URL ends in **`.html`**, because the production host resolves
   paths to files. The backend registers both spellings, so the same URL shape
   works locally and links already in inboxes keep working.
2. The pages fetch **`API_BASE + path`**, never a relative URL — the same
   localhost/Railway switch `store-home.html` uses. A relative fetch asks the
   static host for an endpoint that lives on Railway and gets its 404 page. A
   test greps both pages for `fetch('/…')` and fails on any.
3. The backend page routes **redirect** to `PUBLIC_SITE_URL` when the file is
   absent, instead of raising the 500 the existing `_CHAT_PAGES` routes produce.
   A customer who reaches the API origin by any route still arrives at their
   order.

`GET /order-status/health` reports `public_site`, `api_origin` and
`page_present_locally` so this is checkable from outside the container.

---

## Deploying

| | |
|---|---|
| **Migrations** | `sql/order_status_self_service.sql` then `sql/order_cancel_reason.sql`. Both declared in `REQUIRED_MIGRATIONS` and recorded locally via `python -m scripts.migrate`. Run the same command with `--target railway`. The reason columns are a SEPARATE file on purpose: the first migration is already recorded with a checksum, and `migrate.py` reports a changed file as drifted instead of re-running it — an applied migration is immutable. |
| **Prerequisite** | `sql/order_cancellation_voice.sql` (already on both). The migration warns if `order.cancelled` is not a registered event type. |
| **Env** | `ORDER_LINK_SECRET` — recommended, not required. Without it links are signed with `UNSUBSCRIBE_SECRET` and a warning is logged on every send; with neither, the button is omitted. Optional: `ORDER_CANCEL_OTP_TTL` (900), `ORDER_CANCEL_OTP_ATTEMPTS` (3), `ORDER_CANCEL_OTP_PER_HOUR` (5). |
| **Static** | `order-status.html` and `return-policy.html` deploy to **agentorc.ca** with the other pages (SFTP / `deploy_html.ps1`), NOT with the backend — they are gitignored and Railway 500s on them. |
| **Check** | `GET /order-status/health` reports `links_signable` and `verification_table` — the two things that silently disable the feature. |

`PUBLIC_SITE_URL` must be set on Railway to `https://agentorc.ca` **before the
first customer email goes out**. Left unset it falls back to `APP_URL`, and every
Order Status button points at a host that does not have the page.
