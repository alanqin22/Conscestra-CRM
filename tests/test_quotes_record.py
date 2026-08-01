"""C3.0 — the commercial commitment made durable.

Before this, a quote left behind prose inside an activity description
TRUNCATED AT 2000 CHARACTERS. 77 of them. No line items as data, no unit
prices, no discount, no validity, no status, no version, no opportunity link.

Every other record in the chain — opportunity, order, invoice — is INTERNAL
STATE. The quote is the only one that is a PROMISE MADE TO A CUSTOMER, and it
was the only one with no record.

The two properties that could not be obtained from a join:

  SNAPSHOT PRICING   a quote must not re-price itself when the catalogue moves
  POLICY CAPTURE     "was this capped, and by what value at the time?" was
                     answerable only from application logs that rotate
"""
import inspect
import pathlib

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.core import quotes                                     # noqa: E402
from app.core.database import get_connection                    # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        c = get_connection()
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.quotes')")
            if cur.fetchone()[0] is None:
                pytest.skip("sql/quotes.sql not applied")
        c.close()
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


@pytest.fixture(scope="module")
def fixtures():
    acct = _sql("SELECT account_id::text FROM accounts LIMIT 1")[0][0]
    prod = _sql("""SELECT p.product_name FROM products p
                   JOIN product_pricing pp ON pp.product_id=p.product_id
                   WHERE p.is_active AND lower(pp.price_type)='retail'
                     AND pp.price_value IS NOT NULL LIMIT 1""")[0][0]
    opp = _sql("SELECT opportunity_id::text FROM opportunities LIMIT 1")[0][0]
    return {"account_id": acct, "product": prod, "opportunity_id": opp}


@pytest.fixture
def quote(fixtures):
    """A recorded quote, removed afterwards (lines cascade)."""
    made = []

    def _make(discount_pct=0, qty=2, **kw):
        b = quotes.build_quote(fixtures["account_id"],
                               [{"product": fixtures["product"], "qty": qty}],
                               discount_pct=discount_pct)
        assert b["ok"], b
        r = quotes.record_quote(b["quote"], created_by="test", **kw)
        assert r["ok"], r
        made.append(r["quote_id"])
        return r["quote_id"]

    yield _make
    for qid in made:
        _sql("DELETE FROM quotes WHERE quote_id=%s::uuid", (qid,), fetch=False)


# ── 1. the offer is a record ─────────────────────────────────────────────────

def test_01_a_quote_persists_its_lines_as_data(quote):
    q = quotes.get_quote(quote())
    assert q["lines"] and q["lines"][0]["product_name"]
    assert q["lines"][0]["unit_price"] > 0
    assert q["lines"][0]["quantity"] == 2


def test_02_totals_survive(quote, fixtures):
    qid = quote(discount_pct=10)
    q = quotes.get_quote(qid)
    assert q["subtotal"] > 0 and q["total"] > 0
    assert round(q["subtotal"] - q["discount_amount"], 2) == q["total"]


def test_03_validity_is_a_field_not_prose(quote):
    assert quotes.get_quote(quote())["valid_until"]


def test_04_the_generate_path_returns_the_record_id():
    src = inspect.getsource(quotes.generate_quote_sp)
    assert "record_quote(" in src and '"quote_id": recorded' in src


# ── 2. snapshot pricing — the property a join cannot give ───────────────────

def test_10_line_prices_are_copied_not_referenced(quote):
    """A quote that re-prices itself when the catalogue changes is not a
    commitment. Move the catalogue price and the offer must not move."""
    qid = quote()
    before = quotes.get_quote(qid)["lines"][0]
    pid = before["product_id"]
    original = _sql("""SELECT price_value FROM product_pricing
                       WHERE product_id=%s::uuid AND lower(price_type)='retail'
                       ORDER BY effective_from DESC NULLS LAST LIMIT 1""",
                    (pid,))[0][0]
    try:
        _sql("""UPDATE product_pricing SET price_value = price_value * 2
                WHERE product_id=%s::uuid AND lower(price_type)='retail'""",
             (pid,), fetch=False)
        after = quotes.get_quote(qid)["lines"][0]
        assert after["unit_price"] == before["unit_price"], (
            "the recorded offer re-priced itself from the catalogue")
        assert after["line_total"] == before["line_total"]
    finally:
        _sql("""UPDATE product_pricing SET price_value=%s
                WHERE product_id=%s::uuid AND lower(price_type)='retail'""",
             (original, pid), fetch=False)


def test_11_the_product_name_is_snapshotted_too(quote):
    cols = {r[0] for r in _sql("""SELECT column_name FROM information_schema.columns
                                  WHERE table_name='quote_lines'""")}
    assert "product_name" in cols and "unit_price" in cols


def test_12_a_line_survives_a_product_leaving_the_catalogue():
    """product_id is nullable on purpose: an offer must still read correctly
    when the thing offered is gone."""
    nullable = _sql("""SELECT is_nullable FROM information_schema.columns
                       WHERE table_name='quote_lines'
                         AND column_name='product_id'""")[0][0]
    assert nullable == "YES"


# ── 3. the guardrail becomes auditable ───────────────────────────────────────

def test_20_a_clamped_discount_is_recorded_not_merely_logged(quote):
    """Previously this fact existed only in the application log."""
    q = quotes.get_quote(quote(discount_pct=40))
    assert q["discount_clamped"] is True
    assert q["discount_pct_requested"] == 40
    assert q["discount_pct_granted"] < 40
    assert q["discount_cap_pct"] is not None


def test_21_an_unclamped_quote_records_the_policy_anyway(quote):
    """"The policy allowed 15% and they asked for 10%" is as much a fact about
    the offer as a clamp is."""
    q = quotes.get_quote(quote(discount_pct=5))
    assert q["discount_clamped"] is False
    assert q["discount_pct_requested"] == 5
    assert q["discount_cap_pct"] is not None, (
        "the cap in force was not recorded on an unclamped quote")


def test_22_the_policy_key_is_named(quote):
    key = _sql("SELECT discount_policy_key FROM quotes WHERE quote_id=%s::uuid",
               (quote(),))[0][0]
    assert key == "brand.max_discount_pct"


def test_23_requested_and_granted_are_separate_columns():
    cols = {r[0] for r in _sql("""SELECT column_name FROM information_schema.columns
                                  WHERE table_name='quotes'""")}
    assert {"discount_pct_requested", "discount_pct_granted",
            "discount_cap_pct", "discount_clamped"} <= cols


# ── 4. versioning: a superseded offer is never overwritten ──────────────────

def test_30_a_new_version_supersedes_rather_than_replaces(quote, fixtures):
    v1 = quote(discount_pct=5)
    b = quotes.build_quote(fixtures["account_id"],
                           [{"product": fixtures["product"], "qty": 4}],
                           discount_pct=10)
    r2 = quotes.record_quote(b["quote"], supersedes=v1, created_by="test")
    try:
        assert r2["version"] == 2
        old = quotes.get_quote(v1)
        assert old is not None, "the superseded offer was destroyed"
        assert old["status"] == "superseded"
        assert quotes.get_quote(r2["quote_id"])["supersedes_quote_id"] == v1
    finally:
        _sql("DELETE FROM quotes WHERE quote_id=%s::uuid", (r2["quote_id"],),
             fetch=False)


def test_31_the_old_offer_keeps_its_own_numbers(quote, fixtures):
    v1 = quote(discount_pct=5)
    before = quotes.get_quote(v1)["total"]
    b = quotes.build_quote(fixtures["account_id"],
                           [{"product": fixtures["product"], "qty": 9}],
                           discount_pct=12)
    r2 = quotes.record_quote(b["quote"], supersedes=v1, created_by="test")
    try:
        assert quotes.get_quote(v1)["total"] == before
    finally:
        _sql("DELETE FROM quotes WHERE quote_id=%s::uuid", (r2["quote_id"],),
             fetch=False)


# ── 5. lifecycle ─────────────────────────────────────────────────────────────

def test_40_status_moves_and_stamps(quote):
    qid = quote()
    assert quotes.get_quote(qid)["status"] == "draft"
    quotes.set_status(qid, "sent", by="test")
    quotes.set_status(qid, "accepted", by="test")
    q = quotes.get_quote(qid)
    assert q["status"] == "accepted" and q["accepted_at"]


def test_41_an_unknown_status_is_refused(quote):
    assert quotes.set_status(quote(), "banana")["ok"] is False


def test_42_expiry_closes_offers_the_business_no_longer_has(quote):
    qid = quote()
    _sql("""UPDATE quotes SET valid_until = current_date - 1
            WHERE quote_id=%s::uuid""", (qid,), fetch=False)
    assert quotes.expire_due()["ok"]
    assert quotes.get_quote(qid)["status"] == "expired"


def test_43_expiry_leaves_settled_quotes_alone(quote):
    qid = quote()
    quotes.set_status(qid, "accepted", by="test")
    _sql("""UPDATE quotes SET valid_until = current_date - 1
            WHERE quote_id=%s::uuid""", (qid,), fetch=False)
    quotes.expire_due()
    assert quotes.get_quote(qid)["status"] == "accepted"


# ── 6. offered vs ordered ────────────────────────────────────────────────────

def test_50_without_an_opportunity_the_comparison_refuses_to_guess(quote):
    r = quotes.offered_vs_ordered(quote())
    assert r["ok"] and r["comparable"] is False
    assert "without guessing" in r["reason"]


def test_51_with_an_opportunity_the_comparison_works(quote, fixtures):
    qid = quote(opportunity_id=fixtures["opportunity_id"])
    r = quotes.offered_vs_ordered(qid)
    assert r["comparable"] is True
    assert "quoted_total" in r and "ordered_total" in r and "variance" in r


def test_52_the_attribution_caveat_travels_with_the_answer(quote, fixtures):
    r = quotes.offered_vs_ordered(quote(opportunity_id=fixtures["opportunity_id"]))
    assert "not a direct quote->order link" in r["note"]


# ── 7. failure isolation and flags ───────────────────────────────────────────

def test_60_recording_never_raises(monkeypatch):
    """The offer has already been priced and possibly sent; a recording failure
    must not turn a successful quote into an error."""
    monkeypatch.setattr(quotes, "get_connection",
                        lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
    r = quotes.record_quote({"account_id": "x", "lines": []}, created_by="test")
    assert r["ok"] is False and "error" in r


def test_61_the_kill_switch_skips_recording(monkeypatch, fixtures):
    monkeypatch.setattr(quotes, "RECORD_ENABLED", False)
    before = _sql("SELECT count(*) FROM quotes")[0][0]
    b = quotes.build_quote(fixtures["account_id"],
                           [{"product": fixtures["product"], "qty": 1}])
    r = quotes.record_quote(b["quote"], created_by="test")
    assert r["ok"] is False and "disabled" in r["skipped"]
    assert _sql("SELECT count(*) FROM quotes")[0][0] == before


def test_62_the_existing_activity_record_is_still_written():
    """The 77 prose activities are complemented, not replaced — and the new row
    keeps a pointer back to them."""
    src = inspect.getsource(quotes.generate_quote_sp)
    assert "_log_quote_activity(" in src
    cols = {r[0] for r in _sql("""SELECT column_name FROM information_schema.columns
                                  WHERE table_name='quotes'""")}
    assert "activity_id" in cols


def test_63_deleting_a_quote_takes_its_lines(quote):
    qid = quote()
    assert _sql("SELECT count(*) FROM quote_lines WHERE quote_id=%s::uuid",
                (qid,))[0][0] > 0
    _sql("DELETE FROM quotes WHERE quote_id=%s::uuid", (qid,), fetch=False)
    assert _sql("SELECT count(*) FROM quote_lines WHERE quote_id=%s::uuid",
                (qid,))[0][0] == 0


def test_64_the_migration_is_additive_only():
    """Checked against EXECUTABLE SQL. The header describes the problem it
    solves — prose containing "TRUNCATED AT 2000 CHARACTERS" is not a TRUNCATE,
    and the rollback note naming DROP TABLE is documentation, not a statement."""
    import re
    raw = pathlib.Path("sql/quotes.sql").read_text(encoding="utf-8")
    code = re.sub(r"(?m)^\s*--.*$", "", raw).upper()
    for destructive in ("DROP TABLE", "TRUNCATE", "DELETE FROM",
                        "ALTER TABLE ACTIVITIES", "ALTER TABLE ORDERS",
                        "ALTER TABLE OPPORTUNITIES"):
        assert destructive not in code, f"the migration is not additive: {destructive}"
