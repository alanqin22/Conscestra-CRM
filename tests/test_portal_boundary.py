"""C5.1 boundary — the portal over REAL HTTP, not direct function calls.

The C5.0 suite called the endpoint functions with a fabricated context, so the
dependency that establishes the scope was never exercised. These tests go
through the ASGI stack, which is the only way to prove the things that actually
protect a customer:

    a bookmarked URL cannot bypass authorization
    an expired session loses scope IMMEDIATELY
    a customer id in a URL cannot reach another customer's records
    scope does not leak from one request to the next
    responses never carry internal-only fields

Written BEFORE the customer UI, deliberately: authorization is proved before
functionality is added.
"""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from fastapi.testclient import TestClient                       # noqa: E402

from app.core import cases as case_layer                        # noqa: E402
from app.core import write_guard                                # noqa: E402
from app.core.database import get_connection                    # noqa: E402
from app.main import app                                        # noqa: E402

client = TestClient(app)

PAGES = ["/portal/me", "/portal/orders", "/portal/invoices",
         "/portal/cases", "/portal/quotes"]


def _sql(q, a=(), fetch=True):
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute(q, a)
            r = cur.fetchall() if fetch else None
        c.commit()
        return r
    finally:
        c.close()


def _hash(tok):
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def _mint(account_id=None, contact_id=None, minutes=60, role="member",
          source_table="contacts"):
    """A real auth_sessions row — the same shape /auth/signin writes."""
    tok = f"portal-test-{uuid.uuid4()}"
    _sql("""INSERT INTO auth_sessions
              (token_hash, account_id, credential_id, identifier, lead_id,
               contact_id, first_name, last_name, source_table, role, expires_at)
            VALUES (%s,%s,NULL,%s,NULL,%s,'Test','Customer',%s,%s,%s)""",
         (_hash(tok), account_id or "", f"{uuid.uuid4()}@example.invalid",
          contact_id, source_table, role,
          datetime.now(timezone.utc) + timedelta(minutes=minutes)),
         fetch=False)
    return tok


def _drop(tok):
    _sql("DELETE FROM auth_sessions WHERE token_hash=%s", (_hash(tok),),
         fetch=False)


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def two_accounts():
    rows = _sql("""SELECT account_id::text FROM orders
                   GROUP BY 1 HAVING count(*) > 0 ORDER BY count(*) DESC LIMIT 2""")
    if len(rows) < 2:
        pytest.skip("need two accounts with orders")
    return rows[0][0], rows[1][0]


def _make_case(account_id, subject):
    """Through the governed C1 write layer, never a raw INSERT."""
    cid = case_layer.open_case(subject, actor="portal-test",
                               source="test", account_id=account_id)["case_id"]
    return cid


def _purge_case(cid):
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("ALTER TABLE record_field_history DISABLE TRIGGER "
                        "trg_rfh_append_only")
            cur.execute("DELETE FROM record_field_history WHERE entity='case' "
                        "AND entity_id=%s::uuid", (cid,))
            cur.execute("ALTER TABLE record_field_history ENABLE TRIGGER "
                        "trg_rfh_append_only")
            cur.execute("DELETE FROM case_comments WHERE case_id=%s::uuid", (cid,))
            cur.execute("DELETE FROM cases WHERE case_id=%s::uuid", (cid,))
        c.commit()
    finally:
        c.close()


@pytest.fixture
def foreign_case(two_accounts):
    cid = _make_case(two_accounts[1], "another customer's case")
    yield cid
    _purge_case(cid)


@pytest.fixture
def own_case(two_accounts):
    cid = _make_case(two_accounts[0], "my own case")
    yield cid
    _purge_case(cid)


@pytest.fixture
def customer(two_accounts):
    made = []

    def _make(account_id="A", **kw):
        acct = (two_accounts[0] if account_id == "A"
                else two_accounts[1] if account_id == "B" else account_id)
        tok = _mint(account_id=acct, **kw)
        made.append(tok)
        return tok, acct

    yield _make
    for t in made:
        _drop(t)


# ── 1. every page operates under customer scope ──────────────────────────────

@pytest.mark.parametrize("path", PAGES)
def test_01_no_credential_no_data(path):
    r = client.get(path)
    assert r.status_code == 401
    body = r.text.lower()
    for leak in ("order_number", "invoice_number", "account_name"):
        assert leak not in body


@pytest.mark.parametrize("path", PAGES)
def test_02_a_garbage_token_is_refused(path):
    r = client.get(path, headers=_hdr("not-a-real-token"))
    assert r.status_code == 401
    assert "sign in again" in r.json()["detail"].lower()


@pytest.mark.parametrize("path", PAGES)
def test_03_a_real_session_reaches_every_page(customer, path):
    tok, _ = customer("A")
    assert client.get(path, headers=_hdr(tok)).status_code == 200


# ── 2. expiry removes scope immediately ──────────────────────────────────────

def test_10_an_expired_session_loses_scope_at_once(customer):
    tok, _ = customer("A")
    assert client.get("/portal/orders", headers=_hdr(tok)).status_code == 200
    _sql("""UPDATE auth_sessions SET expires_at = now() - interval '1 second'
            WHERE token_hash=%s""", (_hash(tok),), fetch=False)
    r = client.get("/portal/orders", headers=_hdr(tok))
    assert r.status_code == 401
    assert "orders" not in r.text


def test_11_expiry_is_not_cached_across_requests(customer):
    tok, _ = customer("A")
    client.get("/portal/me", headers=_hdr(tok))
    _sql("DELETE FROM auth_sessions WHERE token_hash=%s", (_hash(tok),),
         fetch=False)
    assert client.get("/portal/me", headers=_hdr(tok)).status_code == 401


# ── 3. a URL cannot reach another customer's records ─────────────────────────

def test_20_a_foreign_order_id_in_the_url_returns_nothing(customer, two_accounts):
    tok, mine = customer("A")
    theirs = _sql("""SELECT order_id::text FROM orders
                     WHERE account_id = %s::uuid LIMIT 1""",
                  (two_accounts[1],))
    if not theirs:
        pytest.skip("no order on the other account")
    r = client.get(f"/portal/orders/{theirs[0][0]}", headers=_hdr(tok))
    assert r.status_code == 404, "another customer's order was reachable"


def test_21_a_foreign_case_id_in_the_url_returns_nothing(customer, foreign_case):
    """Creates the case rather than hunting for one: this is the strongest
    cross-customer assertion in the suite and it must not silently skip
    because the seed data happened not to have a case on that account."""
    tok, _ = customer("A")
    assert client.get(f"/portal/cases/{foreign_case}",
                      headers=_hdr(tok)).status_code == 404


def test_22_a_bookmarked_url_is_not_a_credential(customer, two_accounts):
    """The classic portal defect: a URL that worked while signed in keeps
    working when pasted by somebody else."""
    tok, mine = customer("A")
    mine_order = _sql("""SELECT order_id::text FROM orders
                         WHERE account_id=%s::uuid LIMIT 1""", (mine,))
    if not mine_order:
        pytest.skip("no order on this account")
    url = f"/portal/orders/{mine_order[0][0]}"
    assert client.get(url, headers=_hdr(tok)).status_code == 200
    assert client.get(url).status_code == 401           # no credential
    other, _ = customer("B")
    assert client.get(url, headers=_hdr(other)).status_code == 404


def test_23_a_uuid_shaped_guess_reaches_nothing(customer):
    tok, _ = customer("A")
    for path in ("/portal/orders/", "/portal/cases/"):
        assert client.get(path + str(uuid.uuid4()),
                          headers=_hdr(tok)).status_code == 404


# ── 4. scope never leaks between requests ────────────────────────────────────

def test_30_two_customers_in_sequence_see_their_own_data(customer):
    a_tok, a_acct = customer("A")
    b_tok, b_acct = customer("B")
    a = client.get("/portal/me", headers=_hdr(a_tok)).json()
    b = client.get("/portal/me", headers=_hdr(b_tok)).json()
    assert a["account_link"]["account_id"] == a_acct
    assert b["account_link"]["account_id"] == b_acct
    assert a["account_link"]["account_id"] != b["account_link"]["account_id"]


def test_31_the_scope_is_cleared_after_the_request(customer):
    tok, _ = customer("A")
    client.get("/portal/orders", headers=_hdr(tok))
    assert write_guard.customer_scope() is None, (
        "the customer scope survived the request")


def test_32_an_unauthenticated_request_after_a_customer_one_sees_nothing(customer):
    tok, _ = customer("A")
    assert client.get("/portal/orders", headers=_hdr(tok)).status_code == 200
    assert client.get("/portal/orders").status_code == 401


def test_33_refreshing_preserves_only_the_current_customer(customer):
    a_tok, a_acct = customer("A")
    b_tok, b_acct = customer("B")
    for _ in range(3):
        assert client.get("/portal/me", headers=_hdr(a_tok)).json()[
            "account_link"]["account_id"] == a_acct
        assert client.get("/portal/me", headers=_hdr(b_tok)).json()[
            "account_link"]["account_id"] == b_acct


# ── 5. responses carry no internal-only fields ───────────────────────────────

INTERNAL_FIELDS = (
    "owner_id", "source_assignee", "is_internal", "escalation_id",
    "discount_pct_requested", "discount_cap_pct", "discount_clamped",
    "discount_policy_key", "created_by", "updated_by", "reopen_count",
    "assigned_to", "internal", "routing", "sla_due_at",
)


@pytest.mark.parametrize("path", PAGES)
def test_40_no_internal_field_reaches_a_customer(customer, path):
    tok, _ = customer("A")
    body = client.get(path, headers=_hdr(tok)).text
    for field in INTERNAL_FIELDS:
        assert f'"{field}"' not in body, f"{path} leaked {field}"


def test_41_case_detail_carries_no_internal_field(customer, own_case):
    tok, _ = customer("A")
    body = client.get(f"/portal/cases/{own_case}", headers=_hdr(tok)).text
    for field in INTERNAL_FIELDS:
        assert f'"{field}"' not in body


def test_42_order_detail_carries_no_internal_field(customer, two_accounts):
    tok, mine = customer("A")
    row = _sql("SELECT order_id::text FROM orders WHERE account_id=%s::uuid LIMIT 1",
               (mine,))
    if not row:
        pytest.skip("no order on this account")
    body = client.get(f"/portal/orders/{row[0][0]}", headers=_hdr(tok)).text
    for field in INTERNAL_FIELDS:
        assert f'"{field}"' not in body


# ── 6. a lead-only session over HTTP ─────────────────────────────────────────

def test_50_a_lead_session_gets_onboarding_over_http():
    tok = _mint(account_id=None, source_table="leads", role="viewer")
    try:
        for path in ("/portal/orders", "/portal/invoices", "/portal/cases",
                     "/portal/quotes"):
            j = client.get(path, headers=_hdr(tok)).json()
            assert j["linked"] is False and j["state"] == "not_linked"
            assert path.rsplit("/", 1)[-1] not in j
        me = client.get("/portal/me", headers=_hdr(tok)).json()
        assert me["linked"] is False and "account" not in me
    finally:
        _drop(tok)


def test_51_a_lead_cannot_reach_a_detail_url():
    tok = _mint(account_id=None, source_table="leads")
    try:
        row = _sql("SELECT order_id::text FROM orders LIMIT 1")
        j = client.get(f"/portal/orders/{row[0][0]}", headers=_hdr(tok)).json()
        assert j.get("linked") is False and "order" not in j
    finally:
        _drop(tok)


# ── 7. the read-only promise, over HTTP ──────────────────────────────────────

@pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/portal/orders", "/portal/cases"])
def test_60_no_write_verb_is_accepted(customer, verb, path):
    tok, _ = customer("A")
    r = getattr(client, verb)(path, headers=_hdr(tok))
    assert r.status_code in (404, 405), f"{verb.upper()} {path} was accepted"


def test_61_health_needs_no_credential_and_carries_no_data():
    r = client.get("/portal/health")
    assert r.status_code == 200
    j = r.json()
    assert j["read_only"] is True
    assert not any(k in j for k in ("orders", "invoices", "cases", "account"))
