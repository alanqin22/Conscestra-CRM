"""C3.1 — making the clamp visible, and closing out stale offers.

TWO DELIBERATE NON-DECISIONS, both driven by evidence rather than principle:

  the quote is never GATED on approval
      a guardrail constrains; a gate blocks. Holding a customer-facing offer
      behind the queue trades a small governance win for a real commercial
      loss — the 15% offer is valid and should go out.

  the clamp does not PROPOSE
      the governance queue's dominant outcome is expiry (17 expired vs 5
      executed). Adding items to a backlog nobody finishes produces the
      appearance of governance, not the substance. Visibility first; a
      proposal path only once the visibility proves it is being acted on.
"""
import inspect
import pathlib
import re

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.core import ceo_briefing, quotes                       # noqa: E402
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
    return {
        "account_id": _sql("SELECT account_id::text FROM accounts LIMIT 1")[0][0],
        "product": _sql("""SELECT p.product_name FROM products p
                           JOIN product_pricing pp ON pp.product_id=p.product_id
                           WHERE p.is_active AND lower(pp.price_type)='retail'
                             AND pp.price_value IS NOT NULL LIMIT 1""")[0][0],
    }


@pytest.fixture
def quote(fixtures):
    made = []

    def _make(discount_pct=0, **kw):
        b = quotes.build_quote(fixtures["account_id"],
                               [{"product": fixtures["product"], "qty": 1}],
                               discount_pct=discount_pct)
        r = quotes.record_quote(b["quote"], created_by="test-visibility", **kw)
        made.append(r["quote_id"])
        return r["quote_id"]

    yield _make
    for qid in made:
        _sql("DELETE FROM quotes WHERE quote_id=%s::uuid", (qid,), fetch=False)


def _pressure():
    c = get_connection()
    try:
        with c.cursor() as cur:
            return ceo_briefing._discount_pressure(cur)
    finally:
        c.close()


# ── 1. the clamp becomes visible ─────────────────────────────────────────────

def test_01_a_clamped_quote_appears_in_the_briefing_signal(quote):
    quote(discount_pct=45)
    dp = _pressure()
    assert dp.get("clamped", 0) >= 1
    assert dp["worst_ask"] >= 45 and dp["cap"] > 0


def test_02_the_line_states_the_ask_and_the_cap(quote):
    quote(discount_pct=45)
    line = ceo_briefing._discount_lines(_pressure())[0]
    assert "45%" in line and "15%" in line
    assert "cut by policy" in line


def test_03_it_asks_for_a_judgement_not_a_reprimand(quote):
    """Somebody judged the deal needed more room than the brand allows. The
    line should read as a commercial fact, not a policy violation."""
    quote(discount_pct=45)
    line = ceo_briefing._discount_lines(_pressure())[0].lower()
    assert "deserved an exception" in line
    for blame in ("violation", "breach", "unauthorized", "illegal"):
        assert blame not in line


def test_04_an_unclamped_quote_produces_no_noise(quote):
    """A quote inside policy is not news."""
    before = _pressure().get("clamped", 0)
    quote(discount_pct=5)
    assert _pressure().get("clamped", 0) == before


def test_05_no_clamped_quotes_means_no_section():
    _sql("DELETE FROM quotes WHERE created_by='test-visibility'", fetch=False)
    assert ceo_briefing._discount_lines(_pressure()) == [] or \
        _pressure().get("clamped", 0) > 0   # other quotes may exist


def test_06_the_signal_reaches_both_briefing_renders():
    src = inspect.getsource(ceo_briefing)
    assert src.count("_discount_lines(") >= 3, (
        "the signal must reach gather, the flagship render and the role render")
    assert '"discount_pressure": discount_pressure' in src


def test_07_it_degrades_when_the_table_is_absent(monkeypatch):
    """A database without the C3.0 migration must brief exactly as before."""
    class _Cur:
        def execute(self, *a, **k):
            self._r = (None,)

        def fetchone(self):
            return self._r
    assert ceo_briefing._discount_pressure(_Cur()) == {}


def test_08_a_gather_failure_never_breaks_the_briefing():
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

        def fetchone(self):
            raise RuntimeError("db gone")
    assert ceo_briefing._discount_pressure(_Boom()) == {}


# ── 2. it reports; it does not govern ────────────────────────────────────────

def test_10_the_signal_creates_no_proposal(quote):
    before = _sql("SELECT count(*) FROM action_approvals")[0][0]
    quote(discount_pct=45)
    _pressure()
    ceo_briefing._discount_lines(_pressure())
    assert _sql("SELECT count(*) FROM action_approvals")[0][0] == before


def test_11_quotes_still_do_not_propose_on_clamp():
    """Deliberate: the queue's dominant outcome is expiry, so adding to it
    would be noise wearing the costume of governance."""
    code = re.sub(r'"""[\s\S]*?"""', "", inspect.getsource(quotes))
    assert "governance.propose" not in code
    assert "discount.apply" not in code


def test_12_the_quote_is_never_gated_on_approval(quote):
    """A guardrail constrains; a gate blocks. The clamped offer is valid and
    goes out immediately."""
    qid = quote(discount_pct=45)
    q = quotes.get_quote(qid)
    assert q["status"] in ("draft", "sent")
    assert q["total"] > 0 and q["discount_clamped"] is True


# ── 3. stale offers are closed out ───────────────────────────────────────────

def test_20_the_nightly_job_is_registered():
    m = pathlib.Path("app/main.py").read_text(encoding="utf-8")
    assert 'id="expire_quotes"' in m
    assert "def _run_expire_quotes" in m


def test_21_it_runs_after_the_existing_chain():
    """22:00 pipeline -> 22:05 orders -> 22:10 activities -> 22:12 milestones
    -> 22:15/22:20 seeds -> 22:25 quote expiry."""
    m = pathlib.Path("app/main.py").read_text(encoding="utf-8")
    block = m[m.index("def _register_jobs") if "def _register_jobs" in m else 0:]
    seg = m[m.index('id="expire_quotes"') - 400:m.index('id="expire_quotes"')]
    assert "minute=25" in seg


def test_22_the_job_never_raises():
    import app.main as main
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(quotes, "expire_due",
                   lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        main._run_expire_quotes()          # must not raise


def test_23_expiry_is_narrow(quote):
    """Only draft/sent, only past validity. Accepted and declined are settled
    commercial facts and must not be rewritten."""
    stale_accepted = quote()
    quotes.set_status(stale_accepted, "accepted", by="test")
    stale_open = quote()
    for qid in (stale_accepted, stale_open):
        _sql("UPDATE quotes SET valid_until = current_date - 1 "
             "WHERE quote_id=%s::uuid", (qid,), fetch=False)
    import app.main as main
    main._run_expire_quotes()
    assert quotes.get_quote(stale_accepted)["status"] == "accepted"
    assert quotes.get_quote(stale_open)["status"] == "expired"


def test_24_a_quote_valid_through_today_survives_the_2225_run(quote):
    """The predicate is `valid_until < current_date`, so the 22:25 slot does
    not clip the last two hours of a quote's final day."""
    qid = quote()
    _sql("UPDATE quotes SET valid_until = current_date WHERE quote_id=%s::uuid",
         (qid,), fetch=False)
    import app.main as main
    main._run_expire_quotes()
    assert quotes.get_quote(qid)["status"] in ("draft", "sent")


def test_25_expiry_is_idempotent(quote):
    qid = quote()
    _sql("UPDATE quotes SET valid_until = current_date - 5 "
         "WHERE quote_id=%s::uuid", (qid,), fetch=False)
    import app.main as main
    main._run_expire_quotes()
    first = quotes.get_quote(qid)["closed_at"]
    main._run_expire_quotes()
    assert quotes.get_quote(qid)["closed_at"] == first
