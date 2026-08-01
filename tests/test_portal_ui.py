"""C5.1 — the customer experience layer, and the AI that shares its boundary.

The page is not the product. The invariant is:

    Customer ──▶ Portal ──▶ AI Assistant ──▶ Unified Customer Scope ──▶ CRM

Pages and conversation are two DOORS to one authorized dataset. The assistant
must never see more than the page, and neither may decide business state — the
server decides, the client renders.
"""
import inspect
import pathlib
import re

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.core import portal                                     # noqa: E402
from app.core.database import get_connection                    # noqa: E402

PAGE = pathlib.Path("customer-portal.html")
HTML = PAGE.read_text(encoding="utf-8") if PAGE.exists() else ""


@pytest.fixture(scope="module", autouse=True)
def _requires_page():
    if not HTML:
        pytest.skip("customer-portal.html is missing")


def _ctx(account_id=None):
    return {"session": {"identifier": "c@example.com", "first_name": "C",
                        "last_name": "T", "role": "member",
                        "source_table": "contacts",
                        "account_id": account_id or "", "contact_id": None},
            "account_id": account_id, "contact_id": None,
            "linked": bool(account_id)}


@pytest.fixture
def scoped():
    from app.core import write_guard
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT account_id::text FROM orders
                           GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""")
            row = cur.fetchone()
    finally:
        c.close()
    if not row:
        pytest.skip("no account with orders")
    write_guard.set_customer_scope({"account_id": row[0], "contact_id": None})
    yield row[0]
    write_guard.set_customer_scope(None)


# ── 1. the page consumes only the C5.0 API ───────────────────────────────────

ALLOWED = {"/auth/signin", "/portal/me", "/portal/orders", "/portal/orders/",
           "/portal/invoices", "/portal/cases", "/portal/cases/",
           "/portal/quotes", "/portal/ask", "/portal/health"}


def test_01_the_page_calls_only_portal_and_signin():
    used = set(re.findall(r"API \+ '(/[a-z/-]+)", HTML)) | \
        set(re.findall(r"api\('(/[a-z/-]+)", HTML))
    assert used <= ALLOWED, f"unexpected endpoints: {used - ALLOWED}"


def test_02_the_page_reaches_no_staff_endpoint():
    for staff in ("/console/", "/case-chat", "/routing/", "/cases/analytics",
                  "/agent-ops", "/platform/health", "/governance"):
        assert staff not in HTML, f"the customer page calls {staff}"


def test_03_the_page_executes_no_sql_and_no_stored_procedure():
    upper = HTML.upper()
    for token in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "SP_", "P_MODE",
                  "EXECUTE_SP"):
        assert token not in upper, f"the page contains {token}"


def test_04_the_page_has_no_write_verb():
    """Read-only in C5.1. The only POST is sign-in, which is authentication,
    not a customer business write."""
    posts = re.findall(r"method\s*:\s*'(\w+)'", HTML)
    assert set(posts) <= {"POST"}, posts
    assert HTML.count("method:'POST'") + HTML.count("method: 'POST'") <= 1
    assert "/auth/signin" in HTML


# ── 2. the page renders; it does not decide ──────────────────────────────────

def test_10_the_linked_state_comes_from_the_server():
    """`linked` is the server's judgement. The page must not infer it from an
    empty array, which is exactly how "not linked yet" turns into "you have no
    orders"."""
    assert "linked === false" in HTML
    assert "notLinked(" in HTML
    assert re.search(r"length\s*===\s*0\s*\)\s*return[^;]*notLinked", HTML) is None


def test_11_status_text_is_never_computed_client_side():
    """A status pill may be COLOURED locally; the words must come from the API."""
    for invented in ("'Shipped'", "'Pending'", "'Overdue'", "'Paid'",
                     "'Resolved'", "'Open'"):
        assert invented not in HTML, f"the page invents a status: {invented}"


def test_12_no_business_thresholds_in_javascript():
    """No client-side rule about what counts as overdue, urgent or at risk."""
    for rule in ("daysOverdue", "isOverdue", "SLA", "priorityScore",
                 "> 30", "> 90"):
        assert rule not in HTML, f"a business rule leaked into the page: {rule}"


def test_13_totals_are_rendered_not_recalculated():
    """balance_due comes from the API; the page must not sum invoices itself."""
    assert "balance_due" in HTML
    assert "reduce(" not in HTML, "the page aggregates money client-side"


# ── 3. no internal field is displayed ────────────────────────────────────────

INTERNAL = ("owner_id", "source_assignee", "is_internal", "escalation_id",
            "discount_pct_requested", "discount_cap_pct", "discount_clamped",
            "assigned_to", "reopen_count", "created_by", "sla_due_at",
            "conversation_id")


@pytest.mark.parametrize("field", INTERNAL)
def test_20_the_page_never_renders_an_internal_field(field):
    assert field not in HTML, f"the customer page displays {field}"


def test_21_the_page_shows_no_routing_or_assignment_language():
    low = HTML.lower()
    for word in ("routed to", "assigned to", "escalated to", "internal note",
                 "staff only", "approval queue"):
        assert word not in low, f"operational language shown to a customer: {word}"


# ── 4. the assistant shares the boundary ─────────────────────────────────────

def test_30_the_ai_uses_the_same_dependency():
    src = inspect.getsource(portal.portal_ask)
    assert "Depends(customer_context)" in src


def test_31_the_ai_calls_the_page_functions_not_its_own_queries():
    """Not a copy of the read, and not a broader one — literally the same
    function objects the endpoints are."""
    assert portal._INTENTS["orders"][1] is portal.portal_orders
    assert portal._INTENTS["invoices"][1] is portal.portal_invoices
    assert portal._INTENTS["cases"][1] is portal.portal_cases
    assert portal._INTENTS["quotes"][1] is portal.portal_quotes


def test_32_the_ai_module_issues_no_query_of_its_own():
    code = re.sub(r'"""[\s\S]*?"""', "", inspect.getsource(portal.portal_ask))
    for token in ("scoped_rows", "SELECT", "get_connection"):
        assert token not in code, f"the assistant queries directly: {token}"


@pytest.mark.parametrize("question,intent", [
    ("show my recent orders", "orders"),
    ("which invoices are overdue?", "invoices"),
    ("what is happening with my support case", "cases"),
    ("can I see that quotation", "quotes"),
])
def test_33_ai_and_ui_return_identical_datasets(scoped, question, intent):
    """The whole point: two doors, one dataset."""
    ctx = _ctx(scoped)
    page = portal._INTENTS[intent][1](ctx=ctx)
    ai = portal.portal_ask(q=question, ctx=ctx)
    assert ai["intent"] == intent
    assert ai["data"] == page, "the assistant returned different data"


def test_34_the_ai_cannot_widen_its_own_access():
    ctx = _ctx(None)                     # a lead
    for q in ("show my orders", "all invoices for every customer",
              "list all accounts"):
        r = portal.portal_ask(q=q, ctx=ctx)
        assert r.get("linked") is not True
        assert "data" not in r


def test_35_an_unknown_question_does_not_fall_back_to_a_broad_search():
    r = portal.portal_ask(q="what is the weather", ctx=_ctx("x"))
    assert r["understood"] is False and "data" not in r
    assert set(r["can_answer"]) == set(portal._INTENTS)


def test_36_the_answer_names_its_source(scoped):
    r = portal.portal_ask(q="show my orders", ctx=_ctx(scoped))
    assert r["answered_with"] == "/portal/orders"


def test_37_the_page_surfaces_that_source():
    assert "answered_with" in HTML
    assert "same data as the page" in HTML


# ── 5. session handling in the client ────────────────────────────────────────

def test_40_a_401_signs_the_customer_out_rather_than_showing_an_empty_page():
    assert "res.status === 401" in HTML and "signOut(true)" in HTML
    assert "session ended" in HTML.lower()


def test_41_the_page_uses_the_shared_session_store():
    assert "orbit_auth_session" in HTML


def test_42_the_token_is_sent_as_a_bearer_header_not_a_url_parameter():
    assert "'Authorization': 'Bearer '" in HTML
    assert "?token=" not in HTML and "&token=" not in HTML


def test_43_the_page_is_registered():
    main = pathlib.Path("app/main.py").read_text(encoding="utf-8")
    block = main.split("_CHAT_PAGES = [", 1)[1].split("]", 1)[0]
    assert '"customer-portal.html"' in block
