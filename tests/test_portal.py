"""C5.0 — the customer portal, read-only, on the proven security model.

The portal is not a new authorization system. The voice channel already proved
the model — possession-verified caller, account scope, fail-closed SP access,
explicitly account-scoped reads — and the portal adds an HTTP *entry* to it.

The invariants under test, in order of how badly they fail if wrong:

  ONE SCOPE          exactly one implementation, shared by voice and portal
  ACCOUNT BOUNDARY   the account_id comes from the SESSION, never the URL
  NO SP ACCESS       fail-closed while a customer scope is set
  READ-ONLY          the transaction itself refuses writes
  LEAD ONBOARDING    a lead never sees an empty Orders page
  NO INTERNAL NOTES  is_internal comments never reach a customer
"""
import inspect
import re
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.core import portal, write_guard                        # noqa: E402
from app.core.database import get_connection                    # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        get_connection().close()
    except Exception as exc:                                    # pragma: no cover
        pytest.skip(f"no database reachable: {exc}")


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


@pytest.fixture
def scoped():
    """Open a real customer scope on an account that has data, then clear it."""
    row = _sql("""SELECT o.account_id::text FROM orders o
                  GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""")
    if not row:
        pytest.skip("no account with orders")
    account_id = row[0][0]
    write_guard.set_customer_scope({"account_id": account_id,
                                    "contact_id": None})
    yield account_id
    write_guard.set_customer_scope(None)


def _ctx(account_id=None, **session):
    s = {"identifier": "cust@example.com", "first_name": "Cus",
         "last_name": "Tomer", "role": "viewer", "source_table": "leads",
         "account_id": account_id or "", "contact_id": None}
    s.update(session)
    return {"session": s, "account_id": account_id, "contact_id": None,
            "linked": bool(account_id)}


# ── 1. there is exactly ONE customer scope ───────────────────────────────────

def test_01_the_portal_adds_no_second_scope_implementation():
    src = inspect.getsource(portal)
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    assert "ContextVar" not in code, "the portal declared its own scope"
    assert "from app.core.write_guard import" in src


def test_02_voice_and_portal_share_the_same_read():
    from app.core import voice_support
    assert "write_guard import scoped_rows" in inspect.getsource(
        voice_support._scoped_rows)
    body = re.sub(r'"""[\s\S]*?"""', "",
                  inspect.getsource(voice_support._scoped_rows))
    assert "RealDictCursor" not in body, "voice kept a private copy"


def test_03_only_one_customer_scoped_read_exists_in_the_codebase():
    """Precisely: ONE place that decides whose rows a customer sees.

    Other modules legitimately open read-only transactions for other reasons —
    the read-only SMS channel, the NL→SQL explore layer. Those are different
    mechanisms, not duplicate customer boundaries. What must be unique is the
    code that fills account_id from customer_scope().

    C5.0 found a THIRD copy: voice_support and sdr each carried a
    character-identical one, so the duplication predated the portal. Three
    copies of a security boundary means the weakest copy decides."""
    import pathlib
    hits = [f.as_posix() for f in pathlib.Path("app").rglob("*.py")
            if "no verified customer scope" in
            f.read_text(encoding="utf-8", errors="ignore")]
    assert hits == ["app/core/write_guard.py"], hits


def test_03b_every_customer_channel_delegates_to_it():
    from app.core import sdr, voice_support
    for fn in (sdr._scoped_rows, voice_support._scoped_rows):
        assert "write_guard import scoped_rows" in inspect.getsource(fn)


# ── 2. the account boundary ──────────────────────────────────────────────────

def test_10_no_scope_means_no_data():
    write_guard.set_customer_scope(None)
    with pytest.raises(PermissionError):
        write_guard.scoped_rows("SELECT 1 AS x")


def test_11_the_account_id_comes_from_the_scope_not_the_caller(scoped):
    """A caller-supplied account_id must be OVERWRITTEN by the verified one."""
    other = _sql("""SELECT account_id::text FROM accounts
                    WHERE account_id <> %s::uuid LIMIT 1""", (scoped,))[0][0]
    rows = write_guard.scoped_rows(
        "SELECT %(account_id)s::text AS used", {"account_id": other})
    assert rows[0]["used"] == scoped, (
        "a caller-supplied account_id reached the query")


def test_12_asking_for_another_accounts_order_returns_nothing(scoped):
    foreign = _sql("""SELECT order_id::text FROM orders
                      WHERE account_id <> %s::uuid LIMIT 1""", (scoped,))
    if not foreign:
        pytest.skip("no foreign order")
    rows = write_guard.scoped_rows(
        """SELECT order_id::text FROM orders
           WHERE account_id = %(account_id)s::uuid
             AND order_id = %(oid)s::uuid""", {"oid": foreign[0][0]})
    assert rows == []


def test_13_the_portal_never_filters_on_a_caller_supplied_account(scoped):
    """Every WHERE clause anchors on %(account_id)s, which scoped_rows fills
    from the session."""
    src = inspect.getsource(portal)
    for q in re.findall(r'"""(\s*SELECT[\s\S]*?)"""', src):
        if "FROM" in q and "account_id" not in q:
            pytest.fail(f"a portal query is not account-scoped: {q[:90]}")


# ── 3. fail-closed against stored procedures ─────────────────────────────────

def test_20_a_customer_scope_refuses_all_sp_access(scoped):
    from app.core.database import execute_sp
    with pytest.raises(write_guard.WritePermissionError):
        execute_sp("SELECT sp_accounts(p_mode := 'list') AS result")


def test_21_the_portal_calls_no_stored_procedure():
    """Checked against executable code: the module header explains AT LENGTH
    why execute_sp is not widened, and a naive scan flags that explanation."""
    code = re.sub(r'"""[\s\S]*?"""', "", inspect.getsource(portal))
    code = re.sub(r"(?m)^\s*#.*$", "", code)
    for token in ("execute_sp", "sp_accounts", "sp_orders", "p_mode"):
        assert token not in code, f"the portal reached a stored procedure: {token}"


# ── 4. read-only, enforced by the database ───────────────────────────────────

def test_30_the_transaction_itself_refuses_a_write(scoped):
    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        write_guard.scoped_rows(
            """UPDATE orders SET status='hacked'
               WHERE account_id = %(account_id)s::uuid""")


def test_31_the_portal_exposes_no_write_endpoint():
    from app.main import app
    for r in app.routes:
        if getattr(r, "path", "").startswith("/portal"):
            assert set(getattr(r, "methods", set())) <= {"GET", "HEAD"}, \
                f"{r.path} exposes a write method"


def test_32_no_mutation_verbs_in_the_module():
    code = re.sub(r'"""[\s\S]*?"""', "", inspect.getsource(portal))
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "propose(", "assign("):
        assert verb not in code


# ── 5. lead-only sessions get onboarding, never an empty list ────────────────

def test_40_a_lead_gets_an_explicit_state_not_an_empty_orders_page():
    r = portal.portal_orders(ctx=_ctx(None))
    assert r["linked"] is False
    assert "orders" not in r, "a lead received an orders list"
    assert r["state"] == "not_linked"
    assert "not an empty" in r["message"]


@pytest.mark.parametrize("fn,what", [
    (portal.portal_orders, "orders"),
    (portal.portal_invoices, "invoices"),
    (portal.portal_cases, "cases"),
    (portal.portal_quotes, "quotes"),
])
def test_41_every_business_endpoint_onboards_a_lead(fn, what):
    r = fn(ctx=_ctx(None))
    assert r["linked"] is False and r["what"] == what
    assert what not in r


def test_42_a_lead_still_gets_a_profile_and_link_status():
    r = portal.portal_me(ctx=_ctx(None))
    assert r["ok"] and r["linked"] is False
    assert r["profile"]["identifier"]
    assert r["account_link"]["linked"] is False
    assert "not yet linked" in r["account_link"]["note"]
    assert "account" not in r, "a lead received account data"


def test_43_a_linked_session_gets_the_account(scoped):
    r = portal.portal_me(ctx=_ctx(scoped))
    assert r["linked"] is True and r["account"] is not None


# ── 6. contact_id is attribution, not authorization ──────────────────────────

def test_50_the_boundary_is_the_account_not_the_contact(scoped):
    """Two contacts on one account must see the same records — contact-level
    filtering becomes unworkable the moment an account has several people."""
    write_guard.set_customer_scope({"account_id": scoped,
                                    "contact_id": str(uuid.uuid4())})
    a = write_guard.scoped_rows(
        """SELECT count(*) AS n FROM orders
           WHERE account_id = %(account_id)s::uuid""")[0]["n"]
    write_guard.set_customer_scope({"account_id": scoped, "contact_id": None})
    b = write_guard.scoped_rows(
        """SELECT count(*) AS n FROM orders
           WHERE account_id = %(account_id)s::uuid""")[0]["n"]
    assert a == b


def test_51_the_api_states_that_contact_is_attribution(scoped):
    r = portal.portal_me(ctx=_ctx(scoped))
    assert "contact_id" in r["account_link"]
    assert "ATTRIBUTION" in inspect.getsource(portal)


# ── 7. internal notes never reach a customer ─────────────────────────────────

def test_60_case_comments_are_filtered_in_sql_not_afterwards():
    """Enforced in the WHERE clause so a later refactor cannot drop it."""
    src = inspect.getsource(portal.portal_case)
    assert "cm.is_internal = false" in src


def test_61_an_internal_comment_is_not_returned(scoped):
    row = _sql("""SELECT c.case_id::text FROM cases c
                  WHERE c.account_id = %s::uuid LIMIT 1""", (scoped,))
    if not row:
        pytest.skip("no case on this account")
    cid = row[0][0]
    _sql("""INSERT INTO case_comments (case_id, comment, is_internal)
            VALUES (%s::uuid, 'STAFF ONLY probe', true)""", (cid,), fetch=False)
    try:
        r = portal.portal_case(cid, ctx=_ctx(scoped))
        assert all("STAFF ONLY" not in c["comment"] for c in r["case"]["comments"])
    finally:
        _sql("DELETE FROM case_comments WHERE comment='STAFF ONLY probe'",
             fetch=False)


def test_62_quotes_hide_the_discount_negotiation():
    """requested-vs-granted and the cap are internal commercial facts."""
    src = inspect.getsource(portal.portal_quotes)
    for internal in ("discount_pct_requested", "discount_cap_pct",
                     "discount_clamped"):
        assert internal not in src


# ── 8. the scope never leaks between requests ────────────────────────────────

def test_70_the_dependency_clears_the_scope_afterwards():
    src = inspect.getsource(portal.customer_context)
    assert "finally:" in src and "set_customer_scope(None)" in src


def test_71_an_unauthenticated_call_is_401():
    from fastapi import HTTPException

    class _R:
        headers = {}

    gen = portal.customer_context(_R())
    with pytest.raises(HTTPException) as e:
        import asyncio
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            gen.__anext__())
    assert e.value.status_code == 401


def test_72_health_exposes_no_data():
    r = portal.portal_health()
    assert r["ok"] and r["read_only"] is True
    assert not any(k in r for k in ("orders", "invoices", "cases", "account"))
