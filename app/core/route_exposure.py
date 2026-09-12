"""Declared inventory of routes that are reachable without authentication.

Every HTTP route in this application must either carry an authentication
dependency or be declared here with the reason it does not. A route that is
neither gated nor declared is an error, not a default.

Rationale. Enforcement of D-01 currently rests on an enumerated list of
customer-subject routers: a test asserts that six named routers carry the data
gate. That list is a statement about the routers that existed when it was
written, so a newly added customer-subject router is not covered by it. This is
not hypothetical. The 2026-09-09 reassessment examined the contacts, accounts
and leads surfaces and did not examine /order-chat, which disclosed contact_id,
account_id, email, phone and account_name across 2,462 orders to anonymous
callers. Per-endpoint reasoning omitted an endpoint during an audit whose
purpose was to find them.

This manifest inverts the question. Instead of listing the routes that must be
gated, it lists the routes that are permitted not to be, and requires every
other route to carry a gate. A new route is therefore covered on the day it is
added, and the author must either gate it or state why it does not need one.

A derived classification was attempted first and rejected. Inferring "this route
serves customer subjects" from the endpoint module's source flagged 35 routes,
of which approximately twenty were static pages, health endpoints and inbound
webhooks. The application serves 43 static page or asset routes and 48 health or
status routes, and a module-level signal cannot distinguish them from data
routes because app.main references customer tables elsewhere in the same file. A
control that reports twenty false positives is disabled within a week, which is
a worse outcome than the enumerated list it was intended to replace. The
manifest below is exact rather than inferred for that reason.

Relationship to the SQL manifest. This follows the model established by
`deploy_state.OUT_OF_BAND_SQL`, in which a file that enters neither the governed
chain nor the declared exceptions is refused. The discipline is the same:
absence of a declaration must be an error rather than a silent default.

What a declaration is not. Presence in this manifest is a statement that the
route does not require authentication, together with the control that makes that
acceptable. It is not an assertion that the route is harmless, and it does not
exempt the route from any other control.
"""
from __future__ import annotations

from typing import Any, Dict, List

# ── The dependencies that constitute a gate ──────────────────────────────────
#
# A route is gated when one of these appears among its resolved dependencies,
# whether applied at `include_router(...)` or at the `APIRouter(...)`
# construction. Both locations must be considered: `admin_users_router` and
# `executives_router` declare `dependencies=[Depends(require_admin)]` on the
# router itself and appear ungated if only the registration line is inspected.
GATE_DEPENDENCIES = frozenset({
    "require_data_access",
    "require_admin",
    "require_governance_actor",
    "require_session",
})

# ── Reasons ──────────────────────────────────────────────────────────────────

_STATIC = ("Static page or asset. The application serves the frontend, and a "
           "login cannot be presented to a caller who has not yet loaded the "
           "page that offers it. No database call and no subject data.")

_HEALTH = ("Liveness or status endpoint, consumed by the platform and by "
           "uptime monitoring, neither of which authenticates. Reports process "
           "state only.")

_AUTH = ("Authentication endpoint. It cannot require a session, because "
         "obtaining one is its purpose. Protected instead by per-IP and "
         "per-account rate limiting (20/900s and 3 failures/900s), and by "
         "single-use tokens on the reset path.")

_WEBHOOK = ("Inbound provider webhook. The sender is WhatsApp, Slack, Teams or "
            "Telnyx, none of which can present a CRM session. Authenticity is "
            "established by the provider's own signature or shared secret, not "
            "by this layer.")

_CUSTOMER_SCOPED = ("Customer self-service, scoped to the caller's own records "
                    "by `write_guard.set_customer_scope`. The caller is "
                    "identified by the portal token, and `execute_sp` refuses "
                    "general CRM queries outright while a customer scope is "
                    "set, so the scope cannot be widened from inside.")

_LINK_TOKEN = ("Customer self-service reached from a link in an email the "
               "customer already received. Authorisation is the signed token in "
               "the link, verified per request; an unsigned or expired request "
               "returns `invalid_link`.")

_PUBLIC_PRODUCT = ("Public product surface, deliberately anonymous. Serves "
                   "no customer subject: verified by probe on 2026-09-11, when "
                   "/sdr/chat declined a request to list contacts and returned "
                   "no subject identifier.")

_AGGREGATE = ("Aggregate figures only, and no customer subject. Bounded by "
              "`response_model=HomeIndexResponse`, so the payload cannot carry "
              "a record even if the stored procedure returned one. Business "
              "scale is withheld separately: `total_amount` and "
              "`weighted_amount` are null unless the caller is authenticated. "
              "See docs/decision_d01_retire_public_read.md.")

_DECISION_LINK = ("Governance decision link. Authorisation is an HMAC bound to "
                  "the approval, the action, the executive and a per-issuance "
                  "nonce, valid for 72 hours. The GET renders a confirmation "
                  "and decides nothing; only the POST decides.")

_EMBED_KEY = ("Embedded widget surface, authorised by the embed key in the "
              "path. A CRM session is not available in a third-party page, "
              "which is the reason the key exists.")

_CONSENT = ("CASL consent surface. An unsubscribe link must work for a "
            "recipient who has no account, which is the point of it. Verified "
            "on 2026-09-11 not to be an existence oracle: a known address and "
            "an unknown address return identical responses.")

_DSAR = ("Data-subject request intake. A subject exercising Article 15 or 17 "
         "rights is by definition not a CRM user, so a session cannot be "
         "required. Intake only; fulfilment is governed separately.")

_CONTACT_FORM = ("Public contact form. Accepts a message and creates no "
                 "customer subject on the read side.")

_CALENDAR_TOKEN = (
    "Calendar subscription feed, consumed by a calendar client that cannot "
    "present a CRM session. Authorisation is the shared secret in "
    "CALENDAR_FEED_TOKEN, verified per request; production returned 403 to an "
    "unauthenticated probe on 2026-09-11. The route does disclose customer "
    "subjects, specifically account names, activity subjects and activity "
    "descriptions, so the token is load-bearing rather than incidental. The "
    "check is `if required and token != required`, which means an unset "
    "CALENDAR_FEED_TOKEN serves the feed to everyone. That configuration "
    "dependency is covered by the blocking `calendar_feed` control in "
    "release_guard, which refuses release unless the token is set or "
    "CALENDAR_FEED_PUBLIC=1 records the exposure as a deliberate choice.")

_IDENTIFIER_ONLY = (
    "Storefront checkout address lookup. It cannot require a session, because "
    "the guest checkout flow in store-home.html calls it before any account "
    "exists. Authorisation is knowledge of the record identifier alone. See "
    "UNRESOLVED_EXPOSURES below: this declaration records the current state "
    "and does not assert that the state is correct.")

# ── The manifest ─────────────────────────────────────────────────────────────
#
# Keyed "METHOD /path" so that adding a method to an existing path is also
# caught. Paths use the FastAPI template form, e.g. "{session_id}".

UNGATED_ROUTES: Dict[str, str] = {
    # Static pages and assets
    "GET /": _STATIC,
    "GET /favicon.ico": _STATIC,
    "GET /index.html": _STATIC,
    "GET /setup.html": _STATIC,
    "GET /trust.html": _STATIC,
    "GET /widget.js": _STATIC,
    "GET /widget-demo.html": _STATIC,
    "GET /store-home.html": _STATIC,
    "GET /order-status.html": _STATIC,
    "GET /return-policy.html": _STATIC,
    "GET /platform-health.html": _STATIC,
    "GET /governance-mgmt.html": _STATIC,
    "GET /executives-mgmt.html": _STATIC,
    "GET /knowledge-mgmt.html": _STATIC,
    "GET /lead-chat.html": _STATIC,
    "GET /lead-mgmt.html": _STATIC,
    "GET /notifications-chat.html": _STATIC,
    "GET /notifications-mgmt.html": _STATIC,
    "GET /opportunity-chat.html": _STATIC,
    "GET /opportunity-mgmt.html": _STATIC,
    "GET /orchestrator-chat.html": _STATIC,
    "GET /orchestrator-mgmt.html": _STATIC,
    "GET /order-chat.html": _STATIC,
    "GET /order-mgmt.html": _STATIC,
    "GET /product-chat.html": _STATIC,
    "GET /product-mgmt.html": _STATIC,
    "GET /account-chat.html": _STATIC,
    "GET /account-mgmt.html": _STATIC,
    "GET /accounting-chat.html": _STATIC,
    "GET /accounting-mgmt.html": _STATIC,
    "GET /activity-chat.html": _STATIC,
    "GET /activity-mgmt.html": _STATIC,
    "GET /admin-users.html": _STATIC,
    "GET /agent-console.html": _STATIC,
    "GET /agent-ops.html": _STATIC,
    "GET /agent-studio.html": _STATIC,
    "GET /analytics-chat.html": _STATIC,
    "GET /analytics-mgmt.html": _STATIC,
    "GET /auth.html": _STATIC,
    "GET /case-mgmt.html": _STATIC,
    "GET /contact-chat.html": _STATIC,
    "GET /contact-mgmt.html": _STATIC,
    "GET /customer-portal.html": _STATIC,
    "GET /email-chat.html": _STATIC,
    "GET /email-mgmt.html": _STATIC,

    # Health and status
    "GET /health": _HEALTH,
    "GET /store-health": _HEALTH,
    "GET /portal/health": _HEALTH,
    "GET /order-status/health": _HEALTH,
    "GET /transports/status": _HEALTH,
    "GET /auth-health": _HEALTH,

    # Authentication
    "POST /auth/signup": _AUTH,
    "POST /auth/signin": _AUTH,
    "POST /auth/signout": _AUTH,
    "POST /auth/change-password": _AUTH,
    "POST /auth/password-reset/request": _AUTH,
    "POST /auth/password-reset/confirm": _AUTH,
    "POST /auth/verify-code": _AUTH,
    "POST /auth/verify-email": _AUTH,
    "POST /auth/resend-verification": _AUTH,

    # Inbound provider webhooks
    "GET /whatsapp/inbound": _WEBHOOK,
    "POST /whatsapp/inbound": _WEBHOOK,
    "POST /slack/events": _WEBHOOK,
    "POST /slack/interactive": _WEBHOOK,
    "POST /teams/messages": _WEBHOOK,
    "POST /telephony/sms/inbound": _WEBHOOK,
    "GET /telephony/texml/say": _WEBHOOK,
    "POST /telephony/texml/say": _WEBHOOK,
    "POST /voice/support/inbound": _WEBHOOK,
    "POST /voice/support/turn": _WEBHOOK,
    "POST /voice/support/verify": _WEBHOOK,
    "POST /voice/support/transfer-result": _WEBHOOK,
    "POST /sdr/voice/inbound": _WEBHOOK,
    "POST /sdr/voice/turn": _WEBHOOK,
    "POST /sdr/voice/whisper": _WEBHOOK,
    "POST /sdr/voice/transfer-result": _WEBHOOK,

    # Customer self-service, scoped to the caller's own records
    "GET /portal/me": _CUSTOMER_SCOPED,
    "GET /portal/ask": _CUSTOMER_SCOPED,
    "GET /portal/cases": _CUSTOMER_SCOPED,
    "GET /portal/cases/{case_id}": _CUSTOMER_SCOPED,
    "GET /portal/orders": _CUSTOMER_SCOPED,
    "GET /portal/orders/{order_id}": _CUSTOMER_SCOPED,
    "GET /portal/invoices": _CUSTOMER_SCOPED,
    "GET /portal/quotes": _CUSTOMER_SCOPED,

    # Customer self-service reached from a signed link
    "GET /order-status": _LINK_TOKEN,
    "GET /order-status/summary": _LINK_TOKEN,
    "GET /order-status/cancel/reasons": _LINK_TOKEN,
    "POST /order-status/ask": _LINK_TOKEN,
    "POST /order-status/cancel/request": _LINK_TOKEN,
    "POST /order-status/cancel/confirm": _LINK_TOKEN,
    "GET /booking/invite": _LINK_TOKEN,
    "GET /return-policy": _LINK_TOKEN,
    "GET /return-policy/content": _LINK_TOKEN,

    # Public product surfaces
    "POST /store-chat": _PUBLIC_PRODUCT,
    "POST /sdr/chat": _PUBLIC_PRODUCT,
    "POST /agents/custom/{slug}/chat": _PUBLIC_PRODUCT,
    "GET /home-index": _AGGREGATE,
    "POST /contact": _CONTACT_FORM,
    "GET /consent/status": _CONSENT,
    "GET /email/unsubscribe": _CONSENT,
    "POST /dsar/request": _DSAR,
    "GET /calendar/activities.ics": _CALENDAR_TOKEN,
    "GET /auth/address": _IDENTIFIER_ONLY,
    "GET /compliance/posture": _PUBLIC_PRODUCT,
    "GET /compliance/summary": _PUBLIC_PRODUCT,

    # Governance decision link
    "GET /governance/decide": _DECISION_LINK,
    "POST /governance/decide": _DECISION_LINK,

    # Embedded widget
    "GET /embed/v1/{embed_key}/config": _EMBED_KEY,
    "POST /embed/v1/{embed_key}/chat": _EMBED_KEY,
    "OPTIONS /embed/v1/{embed_key}/{rest:path}": _EMBED_KEY,
}


# ── Declared, but not thereby accepted ───────────────────────────────────────
#
# A declaration above records that a route is reachable without authentication
# and states the control that makes that so. It is not a finding that the
# arrangement is correct. Where the two differ, the difference is recorded here
# rather than resolved by the wording of the reason, because a reason written
# to sound acceptable is how an exposure becomes permanent.
#
# Entries here are open questions carried forward for decision. They must also
# appear in UNGATED_ROUTES, so that the manifest stays complete and the route
# does not fail the undeclared check while the question is open.

UNRESOLVED_EXPOSURES: Dict[str, str] = {
    "GET /auth/address": (
        "Returns the postal address held for a lead, contact or account to an "
        "unauthenticated caller who supplies the identifier. Three properties "
        "are worth separating. First, the identifier is a UUID and is "
        "therefore not enumerable, so this is not a bulk disclosure. Second, "
        "the identifier is nonetheless not a credential: it is not secret, not "
        "rotatable and not revocable, and it appears in ordinary application "
        "traffic. Third, and independently of whether the caller is "
        "authenticated, the handler does not verify that the identifier "
        "belongs to the caller. In the signed-in storefront path the "
        "identifiers are read from localStorage and sent by the browser, so a "
        "signed-in customer who substitutes another identifier receives that "
        "other party's address. Adding a session requirement would therefore "
        "not close the second half of this, and removing the route would break "
        "guest checkout, which has no session by design. The remedy is scope "
        "verification rather than a gate, and it is a change to the checkout "
        "flow rather than to the route alone. Raised 2026-09-11 by the "
        "construction of this manifest; not remediated in that change, and "
        "carried here so that it is not lost."),
}


def route_key(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


def gates_on(route: Any) -> set:
    """The gate dependencies in force for one route.

    Reads both the route's own `dependencies` and its resolved `dependant`,
    because a gate applied at `APIRouter(...)` construction appears only in the
    latter.
    """
    found = set()
    for dep in getattr(route, "dependencies", []) or []:
        call = getattr(dep, "dependency", None)
        if call is not None:
            found.add(getattr(call, "__name__", str(call)))
    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        for sub in getattr(dependant, "dependencies", []) or []:
            call = getattr(sub, "call", None)
            if call is not None:
                found.add(getattr(call, "__name__", str(call)))
    return found & GATE_DEPENDENCIES


def classify(app: Any) -> Dict[str, List[str]]:
    """Compare the application's live routes against this manifest.

    Returns `gated`, `declared`, `undeclared` and `stale`.

    `undeclared` is the failure this module exists to produce: a route that is
    reachable without authentication and that nobody has stated a reason for.

    `stale` is the opposite and matters for a different reason. A declaration
    that no longer matches a live route is a permission granted to nothing, and
    it will be read by the next person as evidence that an exposure was
    considered and accepted when in fact the route has been renamed or removed.
    """
    from fastapi.routing import APIRoute

    gated: List[str] = []
    declared: List[str] = []
    undeclared: List[str] = []
    seen: set = set()

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods or []):
            if method == "HEAD":
                continue
            key = route_key(method, route.path)
            seen.add(key)
            if gates_on(route):
                gated.append(key)
            elif key in UNGATED_ROUTES:
                declared.append(key)
            else:
                undeclared.append(key)

    stale = [k for k in UNGATED_ROUTES if k not in seen]
    return {"gated": sorted(gated), "declared": sorted(declared),
            "undeclared": sorted(undeclared), "stale": sorted(stale)}
