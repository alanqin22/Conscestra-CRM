# Promotions, Coupons & Price-Match — Design

Status: **schema + read/validate layer built and live (local)**; checkout
redemption and the approval-decision UI are specified here but not yet built.

This is the "Price & Promotions" capability behind the Store Sales & Service
agent (category #2 of the in-store playbook). The guiding principle, unchanged
from the rest of the agent: **the AI answers from real data and never invents an
offer, price, or policy.**

---

## 1. What already existed (reused, not rebuilt)

Conscestra already stores prices in `product_pricing` as effective-dated rows
by `price_type` (`Retail`, `Promo`, `Cost`). A live `Promo` row within its
`effective_from`/`effective_to` window **is** a published promotion — it's the
sale price the PDP already shows (e.g. iPad $1,228.53 from $1,329.00). No new
table is needed for published promotions; the agent already states the sale
price and the computed saving.

## 2. What was missing (added — `sql/promotions_coupons.sql`)

| Table | Purpose |
|---|---|
| `coupons` | code-based discounts: percent/fixed, min-subtotal, scope (all/category/product), validity window, usage limits, `is_public` (advertisable), `requires_approval` |
| `coupon_redemptions` | usage ledger — enforces global `usage_limit` and `per_customer_limit` |
| `price_match_requests` | competitor price-match asks (approval-required); ties to a governance approval via `governance_ref` |

## 3. Authority tiers

The agent classifies every price/discount ask into one of three tiers:

### AUTOMATIC (the agent can act/answer directly)
- **Published Promo price** — already applied; the agent states it.
- **Valid public coupon** — the agent shares the code and terms
  (`promotions.summarize_for_agent`). At checkout the code is validated
  (`promotions.validate_coupon`) and, if valid and under the brand cap,
  applied + logged in `coupon_redemptions`.

### APPROVAL-REQUIRED (routed to a human via governance, never auto-applied)
- **Competitor price-match** → `promotions.create_price_match_request()` inserts
  a `pending` row **and** calls `governance.propose("price_match", …)`, so it
  lands in the existing approval queue (governance-mgmt.html) with the critic's
  second opinion. A human approves/declines; the row's `status`/`decided_by`
  close the loop.
- **Coupon over the brand cap** — `validate_coupon` returns
  `requires_approval=true` when the effective discount exceeds
  `governance.policy_value("brand.max_discount_pct", 15%)`, or when the coupon is
  flagged `requires_approval`. Checkout must hold it for approval, not apply it.
- Custom / large-volume / manager-override discounts — same path (a
  `price_match`-style or a dedicated `custom_discount` proposal).

### NEVER
- The agent arbitrarily changing a listed price. There is no code path for it;
  the price shown always comes from `product_pricing`.

## 4. Agent flow (built)

```
shopper: "do you have a coupon / can I get a better price?"
  → _DISCOUNT_RE matches (store agent branch, product page_context present)
  → promotions.summarize_for_agent(product)   # real public coupons in scope
  → injected as [ACTIVE PROMOTIONS] in the one grounded compose
  → agent shares the real codes, OR (if none) offers price-match / specialist
```

Verified live: *"Yes — code WELCOME15 for 15% off (up to $150) or SAVE10 for
10% off on orders over $100 …"* — pulled from the DB, not invented.

## 5. Checkout redemption (BUILT)

`promotions.apply_coupon_to_order()` runs inside the checkout node as **Step B2**
— after items are added (subtotal known) and *before* status→pending:

1. Read `orders.total_amount` (the gross subtotal the line trigger set).
2. `validate_coupon(code, subtotal, account_id)`.
3. AUTOMATIC tier only (valid + under the brand cap): reduce
   `orders.total_amount` by the discount, insert a `coupon_redemptions` row, and
   bump `coupons.times_redeemed` — one transaction.
4. Invalid / `requires_approval` → **not applied**; the order proceeds at full
   price and the reason is surfaced to the UI (`coupon_error`).

**Why reduce `orders.total_amount` and not touch line items:** the invoice is
created by `trgfn_order_create_invoice`, which copies `NEW.subtotal_amount /
tax_amount / total_amount` from the order row (`sp/crm_db.sql:25608-25611`).
Editing `order_items` would refire `trgfn_order_items_update_order`, which resets
`total_amount = SUM(line_subtotal)` and would erase the discount. Adjusting the
order row after the last line-item write is therefore the trigger-consistent
seam, and the invoice inherits the discounted total at status→pending.

Wiring: `StoreData.coupon_code` → pre_router `couponCode` → checkout Step B2.
Frontend: a coupon field on the review step; the confirmation shows `−$discount`
and the discounted total; an unusable code shows a toast (`coupon_error`).

**Scope note:** order-level redemption validates `all`-scope coupons. Category/
product-scoped coupons aren't matched against a multi-item cart here (conservative
— they simply don't apply); per-line scoping is a later refinement.

**Verified locally, end to end:** Bose $649.99 + `SAVE10` → order total $584.99
(−$65, 10% capped), redemption row written, `times_redeemed`++; a bogus code
leaves full price with no redemption. The order is placed `pending` (no invoice
yet — invoices are created on **ship**, see below); transitioning it to `shipped`
created invoice `INV-001481` at **$584.99**, confirming the discount flows all
the way to the invoice.

### Invoice timing (not a bug — Amazon-style)

The deployed `trgfn_order_create_invoice` creates the invoice when the order
becomes `'shipped'`, NOT at placement ("no money taken until goods leave the
warehouse; pending/processing/ready are pre-invoice states"). So a freshly
placed store order intentionally has no invoice until fulfilment — matching the
confirmation UI ("invoice generated when your order ships"). NOTE: the checked-in
`sp/crm_db.sql` is STALE here — it still shows an older invoice-at-`'Pending'`
rule; the live DB is the source of truth.

## 6. Seed data (local only)

`SAVE10` (10% off > $100, cap $200, public) and `WELCOME15` (15% off first
order, public) are seeded locally for testing. Production coupons are a business
decision — insert real rows before enabling advertisement.

## 7. Deploy

`sql/promotions_coupons.sql` is idempotent and additive. Apply locally via psql;
**Railway manually** (never `deploy_sp.ps1` — see the DB-deploy guardrails).
`app/core/promotions.py` degrades to "no coupons" when the tables are absent, so
shipping the code before the migration is safe.
