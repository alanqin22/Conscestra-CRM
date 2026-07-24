# 🪐 CRM Commerce View — Implementation Plan v5

A front-end storefront rendering Conscestra CRM **products** as retail items — checkout creates a CRM
**Order** that automatically fires a full invoice + payment + opportunity + notification cascade
via database triggers. This is a powerful client demo — *no real products are sold.*

> **v5 revision notes:** Updated after reviewing `activities`, `category`, `events`/`event_types`,
> `opportunities`, `opportunity_products`, `product_metadata` tables, and the actual source of
> `trgfn_order_items_update_order`. All prior corrections carry forward.
>
> **Six material corrections from v4:**
> 1. `trgfn_order_items_update_order` uses `SUM(line_subtotal)`, NOT `SUM(line_total)` — tax_amount
>    is never updated by this trigger; it stays 0. Invoice total = pre-discount subtotal for store orders.
> 2. Same trigger **auto-copies billing and shipping addresses** from the selected contact to the order
>    during Step B — the checkout address form is partially redundant for contacts with existing addresses.
> 3. **`product_metadata` table exists** as a separate 1:1 table (`product_id PK → products`).
>    v4 removed the specs section entirely; it should be conditionally restored.
> 4. **`category.is_active`** column exists — `sp_orders(get_category)` does NOT filter on it;
>    inactive categories will appear in the carousel without a frontend guard.
> 5. **`activities` has `trg_fill_activity_owner` BEFORE INSERT** — owner_id is auto-resolved
>    for all trigger-created activities even when passed NULL.
> 6. **`events` table fires `trg_events_after_insert`** — every checkout emits real-time
>    notification events into the CRM subscriber/workflow engine. No store-side handling needed,
>    but CRM operators will receive live order + invoice notifications during demos.

---

## Architecture Overview

```
Browser  ──►  store-*.html  (HTML/CSS/JS, galaxy theme)
                  │
                  ▼
            FastAPI  /store/*  (lightweight proxy routes)
                  │
                  ├── sp_products    (list, get_details)
                  ├── sp_orders      (get_category, contact_search,
                  │                   create, update/batch_update,
                  │                   update/change_status, get_detail)
                  └── sp_accounting  (get_invoice_360, account_balance)
            [optional] Stripe Test Mode API
```

---

## ⚠️ Critical: The Complete Trigger Stack (v5 — Fully Corrected)

Five triggers fire across a complete store checkout. The diagram below reflects actual function
source code, not documentation.

```
POST /store/cart/checkout
  │
  │  Step A — sp_orders(create, p_status='Processing')  ← EXPLICIT, NEVER OMIT
  │  ┌──────────────────────────────────────────────────────────────┐
  │  │ trg_set_order_number  (BEFORE INSERT on orders)              │
  │  │  • Auto-generates order_number = 'SO-YYYY-XXXXXX'           │
  │  │  • status='Processing' → no invoice chain fires             │
  │  └──────────────────────────────────────────────────────────────┘
  │
  │  Step B — sp_orders(update/batch_update)  [all cart items in one call]
  │  ┌──────────────────────────────────────────────────────────────┐
  │  │ trgfn_order_items_update_order  (AFTER INSERT on order_items,│
  │  │                                  fires once per item row)    │
  │  │                                                              │
  │  │  Phase 1 — Address auto-population  [v5]                     │
  │  │   • Reads orders.contact_id                                  │
  │  │   • If orders.billing_address_id is NULL AND contact has a   │
  │  │     billing address → copies it into addresses table as      │
  │  │     parent_type='order' + sets orders.billing_address_id     │
  │  │   • Same for shipping address                                │
  │  │   • After first item: order has real billing/shipping addrs  │
  │  │                                                              │
  │  │  Phase 2 — Order total recalc  [v5 CORRECTED]               │
  │  │   • v_subtotal = SUM(order_items.line_subtotal)              │
  │  │     ← line_subtotal, NOT line_total                          │
  │  │     ← pre-discount amount; discounts NOT reflected here      │
  │  │   • orders.subtotal_amount = v_subtotal                      │
  │  │   • orders.total_amount    = v_subtotal  (no tax added)      │
  │  │   • orders.tax_amount stays 0  ← NEVER updated by trigger   │
  │  │   • By end of batch: total_amount = SUM(line_subtotal)       │
  │  └──────────────────────────────────────────────────────────────┘
  │
  │  Step C — sp_orders(update/change_status, p_status='Pending')
  │  ┌──────────────────────────────────────────────────────────────┐
  │  │ trgfn_order_create_invoice  (AFTER UPDATE on orders)         │
  │  │  • Reads NEW.total_amount = SUM(line_subtotal)               │
  │  │  • Reads NEW.subtotal_amount, NEW.tax_amount (=0)            │
  │  │  • Resolves or auto-creates Opportunity for account          │
  │  │    – New opp: is_synthetic=TRUE, stage='qualification',      │
  │  │      margin_health='unknown' (satisfies CHECK constraint)    │
  │  │    – Stamps opportunity_id back onto orders row              │
  │  │  • INSERTs invoices row:                                     │
  │  │    – invoice_number from invoice_number_seq → 'INV-XXXXXX'   │
  │  │    – status='issued', currency='CAD'  ← hardcoded in trigger │
  │  │    – total_amount = NEW.total_amount                         │
  │  │    – UNIQUE (order_id) enforced; second attempt → DB error   │
  │  │  • INSERTs invoice_orders (one row per order_item)           │
  │  │    – UNIQUE (order_item_id) enforced per row                 │
  │  │  • Creates activity: "Order confirmed – invoice gen'd"       │
  │  │    – trg_fill_activity_owner auto-resolves owner_id          │
  │  │  • emit_event('order.status_changed') → events table         │
  │  │  • emit_event('invoice.created') → events table              │
  │  │                                                              │
  │  │  Both events → trg_events_after_insert  [v5]                │
  │  │   • Fans out to CRM notification subscribers                 │
  │  │   • Queues for workflow engine                               │
  │  │   • Live CRM operators see order + invoice notifications     │
  │  │                                                              │
  │  │  └──► trgfn_invoice_create_payment  (AFTER INSERT on invoices)
  │  │        • Guard: status='issued' + total_amount > 0           │
  │  │        • INSERTs payments (invoice_id) — shell row only      │
  │  │                                                              │
  │  │        └──► trg_payment_before  (BEFORE INSERT on payments)  │
  │  │              • Sets amount = invoice.total_amount            │
  │  │              • Sets status = 'confirmed'                     │
  │  │              • Sets confirmed_at, net_amount, processing_fee │
  │  │                                                              │
  │  │        └──► trgfn_payment_update_invoice                     │
  │  │             (AFTER INSERT on payments)                       │
  │  │              • Recalculates balance_due → 0                  │
  │  │              • Sets invoice.status → 'paid', paid_at         │
  │  │              • Logs invoice_events + events table            │
  │  │              • events → trg_events_after_insert [v5]         │
  │  │              • opp stage → 'closed_paid'                     │
  │  │              • Creates activity: "Payment complete"          │
  │  │                – trg_fill_activity_owner auto-resolves owner  │
  │  │                                                              │
  │  │        • Creates activity: "Confirm receipt – INV-XXXX"      │
  │  │          – trg_fill_activity_owner auto-resolves owner        │
  │  └──────────────────────────────────────────────────────────────┘
  │
  │  Step D (backend raw SQL)
  │  SELECT invoice_id, invoice_number, total_amount
  │  FROM invoices WHERE order_id = $order_id   ← UNIQUE, exactly 1 row
  │
  └──► Returns { order_id, order_number, invoice_id, invoice_number, total_amount }
```

### Net result of a complete checkout

| Table | New rows | Key values |
|---|---|---|
| `orders` | 1 | `status='Pending'`, `total_amount = SUM(line_subtotal)`, addresses auto-set |
| `order_items` | N | `line_subtotal` used for total; `line_total` stored but not summed for order total |
| `addresses` | 0–2 | billing + shipping copied from contact (if contact had them) |
| `invoices` | 1 | `status='paid'`, `currency='CAD'`, `invoice_number='INV-XXXXXX'` |
| `invoice_orders` | N | one per order_item; links pipeline to product_pricing |
| `payments` | 1 | `status='confirmed'`, `amount` = invoice total |
| `opportunities` | 0–1 | existing resolved or new `is_synthetic=TRUE`, `stage='closed_paid'` |
| `opportunity_products` | 0 | NOT populated by the trigger — opp is empty of line items |
| `activities` | 3 | owner auto-resolved; scores 20, 10, 10 |
| `events` | 2+ | `order.status_changed`, `invoice.created`, payment events |
| `audit_log` | 3 | create, batch_update, change_status |

---

## [v5] Key Schema Findings

### trgfn_order_items_update_order — Actual Behaviour (Two Jobs)

**Job 1 — Address auto-population (previously undocumented):**
When the first item is added in Step B, the trigger reads `orders.contact_id` and
automatically copies the contact's billing and shipping addresses into the `addresses` table
(as `parent_type='order'`, `parent_id=order_id`) then sets `orders.billing_address_id` and
`orders.shipping_address_id`. This means:
- If the selected contact has addresses on file, the order gets real addresses with zero extra API calls.
- The checkout Step 2 address fields are **pre-filled / redundant** for contacts with existing addresses.
- The confirmation page can show a real shipping address from the DB.
- The trigger only copies once (skips if address already exists on the order).

**Job 2 — Order total (corrected from v4):**
```
v_subtotal = SUM(order_items.line_subtotal)  ← NOT line_total
orders.subtotal_amount = v_subtotal
orders.total_amount    = v_subtotal          ← no tax is added
orders.tax_amount stays 0                   ← trigger never writes tax_amount
```
The invoice created in Step C will have `total_amount = SUM(line_subtotal)` = pre-discount
item totals. For the store demo — since we pass no discount values in `batch_update` — 
`line_subtotal` and `line_total` are equal (discount=0), so the invoice total matches
exactly what the customer sees in the cart. No discrepancy.

However, the 10% promo code (`ORBIT10`) applied client-side in the cart does **not** flow
into the CRM order. The CRM invoice will show the undiscounted total. Document this clearly
on the confirmation page ("Promo discounts applied at time of purchase are reflected in your
cart total; CRM order total reflects list pricing.") or simply omit the promo code feature
to keep the demo clean.

### product_metadata — Exists as a Separate 1:1 Table

`product_metadata (product_id PK FK→products, metadata JSONB, updated_at)`

v4 incorrectly stated "no metadata JSONB" — the column is not on the `products` table directly,
but a dedicated companion table exists with a 1:1 relationship. Implications:
- `sp_products(get_details)` — needs verification of whether it JOINs `product_metadata`.
  If it does, `metadata` will appear in the detail response. If not, a second call or raw
  JOIN is required to get it.
- For the store: the product detail page can show a "Specifications" tab **conditionally** —
  only if `metadata` is non-null and non-empty `{}`. A graceful `if (metadata && Object.keys(metadata).length > 0)` guard is sufficient.
- Default to hidden; show only when data is present.

### category — is_active Filter Gap

`category (category_id, category_number, category_name varchar(100), description varchar(500), is_active DEFAULT true)`

`sp_orders(get_category)` returns **all** rows without filtering `is_active`. If any categories
are marked inactive, they will appear in the store carousel and filter sidebar.
- Backend `/store/categories` route should post-filter: return only items where `is_active=true`,
  or pass `p_search` guard. Since `sp_orders` doesn't expose this filter, apply it in the
  FastAPI route after receiving the SP response.
- Category `description varchar(500)` is available — use as tooltip or subcategory line under
  the category name in the carousel.

### opportunities — Auto-Created Record Details

Auto-created opportunities from `trgfn_order_create_invoice`:
- `is_synthetic=TRUE` — these are system-generated, not sales-rep-created
- `stage='qualification'` initially, then immediately updated to `closed_won` (Pending) then
  `closed_paid` (payment confirmed) all within the same transaction
- `margin_health='unknown'` — satisfies the CHECK constraint
  (`'excellent'|'good'|'fair'|'poor'|'negative'|'unknown'`)
- `opportunity_products` rows are **NOT** created — the auto-opp has no line items.
  `trgfn_opportunity_product_events` never fires for store checkouts. The opportunity amount
  field is set but the product breakdown is empty.

### activities — trg_fill_activity_owner Auto-Resolves Owner

All trigger-created activities pass through `trg_fill_activity_owner` (BEFORE INSERT/UPDATE),
which auto-fills `owner_id` even when the trigger passes NULL. No broken activities.

### events — Every Checkout Fires Real-Time Notifications

`trg_events_after_insert` fans out to CRM notification subscribers and queues for the workflow
engine after every INSERT on `events`. A store checkout emits at minimum:
`order.status_changed` and `invoice.created`. CRM operators with live notification subscriptions
will see these during demos — a feature, not a bug.

### invoices — currency Hardcoded to 'CAD'

`trgfn_order_create_invoice` inserts `currency='CAD'` hardcoded. The store catalog may show
prices in USD (products table `currency_code DEFAULT 'USD'`). This mismatch is pre-existing
and affects only the invoice display. On the confirmation page, display the invoice amount
without a currency assumption — use the value from `sp_accounting(get_invoice_360).invoice.currency`.

---

## Stored Procedure Reference

### sp_products v3e

**`list`**
- Params: `p_mode='list'`, `p_category_filter UUID`, `p_is_active_filter BOOLEAN`,
  `p_search TEXT`, `p_page_size INT`, `p_page_number INT`, `p_sort_field TEXT`, `p_sort_order TEXT`
- Valid `p_sort_field`: `name`, `sku`, `stock_quantity`, `created_at`, `updated_at`
- Returns: `product_id`, `product_number`, `sku`, `product_name`, `description varchar(500)`,
  `category_id`, `category_number`, `category_name`, `stock_quantity`, `is_active`,
  `wholesale_price`, `retail_price`, `promo_price`, `currency_code`, `stock_status`
- `stock_status`: `'In Stock'` / `'Low Stock'` / `'Out of Stock'` (threshold: `p_low_stock_threshold`, default 10)
- Price sort: client-side only. In Stock filter: client-side only (`stock_status !== 'Out of Stock'`).

**`get_details`**
- Identifier: `p_product_id` / `p_sku` / `p_product_number` (one required)
- Returns full record. Whether `product_metadata.metadata` JSONB is included depends on SP
  implementation — treat as conditionally present; guard before rendering.

### sp_orders v5e

**`get_category`** → `{ categories: [{ category_id, category_name }] }`
- No `is_active` filter in SP — post-filter in backend route.
- `category_name varchar(100)`, `description varchar(500)` available in DB but not returned
  by this SP mode. Carousel tooltip requires a second `/store/categories/details` call or
  can be omitted.

**`contact_search`** → `{ contacts: [{ contact_id, contact_name, email, phone, account_id, account_name }] }`
- Max 20 results. Used exclusively for checkout Step 1 typeahead.

**`create`** — Step A
- **Always** pass `p_status='Processing'`. Schema DEFAULT is `'Pending'`; SP COALESCE also
  defaults to `'Pending'`. Double default = double danger. Explicit is mandatory.
- Required: `p_account_id` (active account). Optional: `p_contact_id`, `p_created_by`.
- `order_number` auto-set by BEFORE trigger — do not include in call.

**`update / batch_update`** — Step B
- `p_payload`: `{ "header": {}, "items_to_add": [{"product_id","quantity","price_type":"Retail"}], "items_to_remove": [] }`
- After this call: `orders.total_amount = SUM(line_subtotal)` (addresses also auto-set).
- Send integer quantities as integers — `order_items.quantity` is `numeric(18,2)` and will
  accept decimals without error.

**`update / change_status`** — Step C (fires full cascade)
- `p_order_id`, `p_status='Pending'`, `p_updated_by`

**`get_detail`** — `p_order_id` or `p_order_number`. Returns items, totals, contact, account.

### sp_accounting v2r

**`get_invoice_360`** — Full invoice with `payments[]`, `orders[]`, owner, pipeline financials.
- Invoice `status='paid'`, `currency` = value from trigger (likely `'CAD'`).
- Retrieve `invoice_id` via Step D: `SELECT invoice_id FROM invoices WHERE order_id = $1`.

**`account_balance`** — `{ total_invoiced, total_paid, balance_due, invoice_count, overdue_count, recent_invoices[] }`

---

## Guest Checkout Strategy

`sp_orders create` requires `p_account_id` → active FK. No workaround at SP level.
- **Option A (recommended):** Mandatory contact typeahead. Contact carries `account_id`.
- **Option B:** Dedicated `Guest / Walk-in` account pre-created in DB.

---

## Proposed Pages

### store-home.html
1. Hero Banner — search, CTA
2. Category Carousel — `GET /store/categories` (post-filtered: `is_active=true` only)
   - **[v5]** Use `category.description` (varchar 500) as card subtitle or tooltip if available
3. Featured Products Grid — 8 active, non-synthetic products
   - Filter `is_synthetic=false` and `is_active=true` in route
4. Trust Signals Strip

### store-catalog.html
**Sidebar:** category checkboxes (active only), price range slider (client-side), In Stock toggle,
Sort dropdown (Name, Stock Level, Newest; Price client-side)

**Product cards:** `product_name` (≤100 chars), SKU, `retail_price`, `wholesale_price` (muted),
`promo_price` SALE badge (when `promo_price != null && promo_price < retail_price`), `stock_status`
badge, star rating (seeded from `product_number % 5 + 1`), "Add to Cart" (disabled if Out of Stock)

### store-product.html
**Right panel:** `product_name`, SKU, `category_name` breadcrumb, price block, `stock_status`,
qty selector (integers only, `min=1`, `step=1`), "Add to Cart" CTA

**Description:** `description varchar(500)` — no truncation needed.

**[v5] Specifications tab:** Conditionally render if `product_metadata.metadata` is non-null
and non-empty. Guard: `if (metadata && Object.keys(metadata).length > 0)`. Hide tab otherwise.
Whether `sp_products(get_details)` returns this needs runtime verification — if not, add a
`GET /store/products/{product_id}/metadata` route backed by:
`SELECT metadata FROM product_metadata WHERE product_id = $1`

**Bottom tabs:** Description | Specifications (conditional) | Reviews (3 static demo reviews)

### store-cart.html
**[v5] Promo code decision point:** The 10% promo code `ORBIT10` applied client-side does NOT
flow into the CRM order (which uses `SUM(line_subtotal)` with no discount). Two options:
- **Option A (recommended):** Remove promo code feature entirely to keep cart total = CRM total.
- **Option B:** Keep it, but add a note on the checkout page: "Promotional discounts are applied
  at your cart and are for display purposes; the CRM order reflects list pricing."

**Left:** items from localStorage; qty stepper integers only; line total = `qty × retail_price`.
**Right:** Subtotal, Shipping, Tax (demo rate), Grand Total, "Proceed to Checkout"

### store-checkout.html
**Step 1 — Contact (mandatory):**
Contact typeahead (3+ chars) → `sp_orders(contact_search)` → shows `contact_name + account_name`.
Captures `contact_id` + `account_id`.

**Step 2 — Order Details:**
**[v5]** Display a read-only "Shipping Address" block auto-populated from the contact record
(if contact has a shipping address — this will be set automatically after Step B fires
`trgfn_order_items_update_order`). Address fields are informational for demo; no manual entry
needed for contacts with existing addresses. Order notes textarea for any extras.

**Step 3 — Payment:**
"Place Demo Order" → `POST /store/cart/checkout` → 3-step SP sequence + Step D raw SQL.

### store-confirmation.html
- ✅ Checkmark + confetti
- **Order card:** `order_number`, `status='Pending'`, `order_date`, item count, `total_amount`
- **Invoice card:** `invoice_number` (INV-XXXXXX), `status='paid'` ✓, `currency` (from SP),
  `issue_date`, `due_date`
- **[v5] Shipping Address card:** If `orders.shipping_address_id` was populated by the trigger,
  display the auto-resolved shipping address from the confirmation response
- **Payment card:** `amount`, `payment_method`, `transaction_reference`, `confirmed_at`
- **[v5] Currency note:** Invoice currency from trigger may be 'CAD'; display `invoice.currency`
  dynamically rather than assuming USD
- **Optional:** Account balance widget via `sp_accounting(account_balance)`
- CTAs: "View Order in CRM" → `order-mgmt.html` | "Continue Shopping" → `store-catalog.html`

---

## Backend Routes — `app/router/store_router.py`

```python
# ── Products & Categories ──────────────────────────────────────────────────────
GET  /store/products
     # sp_products(p_mode='list', p_is_active_filter=True,
     #             p_search, p_category_filter, p_page_size, p_page_number,
     #             p_sort_field, p_sort_order)
     # Optionally add: is_synthetic=false filter at route level if needed

GET  /store/products/{product_id}
     # sp_products(p_mode='get_details', p_product_id)
     # If metadata not in SP response, also fetch from product_metadata table

GET  /store/categories
     # sp_orders(p_mode='get_category')
     # Post-filter: return only items where is_active=true
     # (SP does not filter is_active — must be done in route)

# ── Checkout Support ────────────────────────────────────────────────────────────
GET  /store/contacts?search=...
     # sp_orders(p_mode='contact_search', p_search)

POST /store/cart/checkout
     # Payload: { account_id, contact_id, items:[{product_id, quantity(int)}], notes? }
     # Validate: all quantities are positive integers before any SP calls.
     #
     # Step A:
     #   sp_orders(p_mode='create',
     #             p_status='Processing',   ← EXPLICIT, ALWAYS
     #             p_account_id, p_contact_id,
     #             p_order_date=NOW(), p_created_by)
     #   → order_id, order_number
     #
     # Step B:
     #   sp_orders(p_mode='update', p_action='batch_update',
     #             p_order_id,
     #             p_payload={
     #               "header": {},
     #               "items_to_add": [
     #                 {"product_id":"<uuid>","quantity":<int>,"price_type":"Retail"},
     #                 ...
     #               ],
     #               "items_to_remove": []
     #             })
     #   → trgfn_order_items_update_order fires per item:
     #     • copies contact billing/shipping addresses to order
     #     • sets orders.total_amount = SUM(line_subtotal), tax_amount stays 0
     #
     # Step C:
     #   sp_orders(p_mode='update', p_action='change_status',
     #             p_order_id, p_status='Pending', p_updated_by)
     #   → full cascade:
     #     trgfn_order_create_invoice (invoice, opp, activities, events)
     #     → trgfn_invoice_create_payment (payment shell)
     #     → trg_payment_before (fills payment fields)
     #     → trgfn_payment_update_invoice (invoice→paid, opp→closed_paid, events)
     #
     # Step D (raw SQL — safe due to UNIQUE order_id on invoices):
     #   SELECT invoice_id, invoice_number, total_amount, currency
     #   FROM invoices WHERE order_id = $order_id
     #
     # Returns:
     #   { order_id, order_number, invoice_id, invoice_number,
     #     total_amount, currency }

GET  /store/orders/{order_number}
     # sp_orders(p_mode='get_detail', p_order_number)

GET  /store/invoice/{invoice_id}
     # sp_accounting(p_mode='get_invoice_360', p_invoice_id)

GET  /store/account/{account_id}/balance
     # sp_accounting(p_mode='account_balance', p_account_id)

POST /store/stripe/intent
     # Stub — enabled via STRIPE_SECRET_KEY in .env
```

---

## Supabase / SQL Summary

No new stored procedures or tables required.

| Store action | SP / Trigger | Notes |
|---|---|---|
| Category carousel | `sp_orders(get_category)` | Post-filter `is_active=true` in route |
| Product catalog | `sp_products(list)` | Filter `is_synthetic=false` optionally |
| Product detail | `sp_products(get_details)` | Check if metadata JSONB included |
| Product specs | `product_metadata` table direct | Conditional; guard for empty `{}` |
| Contact typeahead | `sp_orders(contact_search)` | Returns `account_id` in same row |
| Step A — create order | `sp_orders(create)` | **Always** `p_status='Processing'` |
| ↳ Auto: order number | `trg_set_order_number` (BEFORE INSERT) | |
| Step B — add items | `sp_orders(update/batch_update)` | Integer quantities only |
| ↳ Auto: address copy | `trgfn_order_items_update_order` | Copies from contact; runs per item |
| ↳ Auto: order totals | `trgfn_order_items_update_order` | `SUM(line_subtotal)`, no tax |
| Step C — trigger chain | `sp_orders(update/change_status)` `status='Pending'` | |
| ↳ Auto: invoice | `trgfn_order_create_invoice` | `currency='CAD'` hardcoded |
| ↳ Auto: opportunity | `trgfn_order_create_invoice` | `is_synthetic=TRUE`, no opp_products |
| ↳ Auto: activities ×3 | all three invoice triggers | `trg_fill_activity_owner` auto-resolves |
| ↳ Auto: events | `emit_event` → `trg_events_after_insert` | Live CRM notifications fire |
| ↳ Auto: payment shell | `trgfn_invoice_create_payment` | |
| ↳ Auto: payment fill | `trg_payment_before` (BEFORE INSERT) | Sets amount, status, confirmed_at |
| ↳ Auto: invoice paid | `trgfn_payment_update_invoice` | Events fired again |
| Step D — capture invoice | Raw SQL: `SELECT … FROM invoices WHERE order_id=?` | UNIQUE constraint = safe |
| Confirmation: order | `sp_orders(get_detail)` | Includes shipping address if auto-set |
| Confirmation: invoice | `sp_accounting(get_invoice_360)` | Use `invoice.currency` dynamically |
| Confirmation: balance | `sp_accounting(account_balance)` | Optional |

**Test data seeding:**
```sql
-- Seed orders (status='Processing' — no invoices yet):
SELECT * FROM generate_random_orders(25, 90, 0.85, '<employee_uuid>');

-- Promote a subset to trigger full invoice+payment chains:
UPDATE orders
SET status = 'Pending'
WHERE status = 'Processing'
  AND order_date >= CURRENT_DATE - 31;
-- Note: trgfn_order_create_invoice fires for each UPDATE row,
-- but only if NOT EXISTS (invoice for order_id already).
-- Safe to run on already-promoted rows — trigger is guarded.
```

---

## Verification Plan

### Browser Tests

1. **Store Home**
   - Category carousel only shows active categories (Network tab: verify route post-filters)
   - Featured products show live prices; no synthetic products in grid

2. **Catalog** — search AND-match; sort fields valid; In Stock client-side; pagination from `metadata.*`

3. **Product Detail**
   - `description` renders (≤500 chars)
   - Specifications tab: hidden when `metadata` is absent or `{}`; shown when populated
   - SALE badge: only when `promo_price < retail_price` (both non-null)

4. **Cart**
   - Qty stepper: integers only; test that `1.5` is rejected/coerced
   - Line totals = `qty × retail_price` (no discount stored in CRM)
   - If promo code feature is retained: confirm page shows disclaimer about CRM pricing mismatch

5. **Checkout**
   - Step 1: contact typeahead shows `account_name` in result
   - Step 2: if contact has addresses, shipping address pre-displays as read-only
   - Network: 3 SP calls fire in order (create → batch_update → change_status)
   - **[v5]** Payload inspection: quantities are integers, not decimals

6. **Confirmation — Full Cascade Verification**
   - Invoice `status='paid'`, `invoice_number='INV-XXXXXX'`
   - **[v5]** Invoice `currency` displayed dynamically (likely 'CAD' from trigger)
   - **[v5]** Shipping address card shows auto-copied contact address (if contact had one)
   - In Supabase — verify:
     - `invoices`: 1 row, `status='paid'`, `total_amount = SUM(order_items.line_subtotal)`
     - `addresses`: 1–2 rows with `parent_type='order'` (billing + shipping from contact)
     - `invoice_orders`: N rows with `order_item_id` populated
     - `payments`: 1 row, `status='confirmed'`
     - `activities`: 3 rows linked to order, all with `owner_id` set (auto-resolved)
     - `opportunities`: 1 row `is_synthetic=TRUE`, `stage='closed_paid'`
     - `opportunity_products`: 0 rows (store checkout does not populate this)
     - `events`: at least 2 rows (`order.status_changed`, `invoice.created`)

7. **Idempotency test**
   - Submit checkout twice for the same `order_id` (simulate a double-submit)
   - Verify: `UNIQUE(order_id)` on invoices → second attempt returns DB error
   - Backend should catch this and return a user-friendly message

8. **Mobile** — 390px: 2-col grid, sidebar collapses, checkout stacks vertically

### Manual Verification Steps

1. `run_crm_agent.bat` → `http://localhost:18789/web%20r/store-home.html`
2. Browse catalog, add 2–3 items (integer quantities)
3. Checkout — search and select a CRM contact
4. Observe Step 2: auto-populated shipping address (if contact has one)
5. "Place Demo Order" → confirmation page
6. Confirm both **Order Number** (SO-…) and **Invoice Number** (INV-…) displayed
7. Confirm invoice status is `paid` and `currency` shown correctly
8. In `order-mgmt.html`: "Show order SO-…" → confirm items + totals match cart
9. In `accounting.html`: search account → confirm `paid` invoice in history
10. In Supabase Activities: confirm 3 new activities all have `owner_id` set

---

## Complete Constraints Reference

| Constraint | Source | Impact on Store |
|---|---|---|
| `orders.status DEFAULT 'Pending'` | Schema + SP | Always pass `p_status='Processing'` explicitly |
| `p_account_id` required (active) | `sp_orders` | No true guest checkout |
| `order_number` auto-generated | `trg_set_order_number` BEFORE INSERT | Never pass in create call |
| `order_items.quantity numeric(18,2)` | Schema | Enforce integers client-side + validate before API |
| `trgfn_order_items_update_order` auto-copies addresses | Trigger source | Step 2 addr form redundant for contacts with addresses |
| Order total = `SUM(line_subtotal)`, no tax | Trigger source | Invoice total is pre-discount; promo code creates mismatch |
| Invoice auto-created on `status='Pending'` | `trgfn_order_create_invoice` | Items must exist BEFORE status → Pending |
| `invoices.currency` hardcoded `'CAD'` in trigger | Trigger source | Display currency dynamically; do not assume USD |
| `UNIQUE (order_id)` on invoices | Schema | Double-submit protection; one invoice per order |
| `trg_payment_before` fills payment fields | Schema | Payment insert needs only `invoice_id` |
| Payment auto-confirmed | `trgfn_invoice_create_payment` | Invoice → `paid` immediately; void blocked |
| `void_invoice` blocked if confirmed payment | `sp_accounting` code -33 | No self-serve cancel from store UI |
| `category.is_active` not filtered by `sp_orders` | Schema + SP | Post-filter in `/store/categories` route |
| `product_metadata` is a separate table | Schema | Conditionally fetch; guard for empty `{}` |
| `products.description varchar(500)` | Schema | Max 500 chars; no truncation risk |
| `opportunities.margin_health CHECK` | Schema | Auto-created opps use `'unknown'` (valid) |
| `opportunity_products` NOT populated | Trigger | Auto-created opp has no line items |
| `trg_fill_activity_owner` auto-resolves | Schema | Activities always have owner_id set |
| `events → trg_events_after_insert` | Schema | Live CRM notifications fire during demo checkout |
| Valid `p_sort_field` for `sp_products` | SP | Price sort: client-side only |
