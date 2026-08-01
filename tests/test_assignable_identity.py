"""C2.0 — assignable identity.

The finding this model exists because of: NOTHING in this database could answer
"who may receive work?".

    owners (44)          40 match a customer contact by email; roles are
                         Billing Contact / Decision Maker / Purchasing
    employees (21)       demo seed data — 9 on @company.com, usernames like
                         `sysadmin`, created in the same abandoned cohort as
                         the `cases` table
    auth_credentials (2) authenticates CUSTOMERS (account_id/contact_id/lead_id)
    executives (4)       the only non-synthetic internal identities

So membership is EXPLICIT and never derived — a view over any of the above
would inherit its untrustworthiness behind a confident-looking answer.
"""
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.core import assignable, cases                          # noqa: E402
from app.core.database import get_connection                    # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        c = get_connection()
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.assignable_identity')")
            if cur.fetchone()[0] is None:
                pytest.skip("sql/assignable_identity.sql not applied")
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


@pytest.fixture
def granted():
    made = []

    def _grant(email, **kw):
        r = assignable.grant(email, added_by="test", **kw)
        made.append(email)
        return r

    yield _grant
    for e in made:
        _sql("DELETE FROM assignable_identity WHERE lower(email)=lower(%s)",
             (e,), fetch=False)


# ── 1. assignability ─────────────────────────────────────────────────────────

def test_01_the_directory_is_the_four_executives():
    """The honest answer today is FOUR, not forty-four."""
    d = assignable.directory()
    assert len(d) == 4
    assert all(r["source"] == "executive" for r in d)
    assert all(r["email"].endswith("@agentorc.ca") for r in d)


def test_02_a_customer_contact_is_not_assignable():
    row = _sql("""SELECT o.email FROM owners o
                  JOIN contacts c ON lower(c.email)=lower(o.email) LIMIT 1""")
    assert row, "expected owners that are also customer contacts"
    assert assignable.resolve(row[0][0]) is None
    assert assignable.is_assignable(row[0][0]) is False


def test_03_presence_in_owners_confers_nothing():
    """The inference C2 discovery proved false."""
    for email, in _sql("SELECT email FROM owners WHERE email IS NOT NULL LIMIT 8"):
        if email.endswith("@agentorc.ca"):
            continue
        assert assignable.resolve(email) is None, email


def test_04_demo_employees_are_not_assignable():
    """9 of 21 are on the @company.com placeholder domain."""
    for email, in _sql("SELECT email FROM employees WHERE email IS NOT NULL"):
        assert assignable.resolve(email) is None, email


def test_05_an_executive_resolves_to_a_validated_owner_id():
    oid = assignable.resolve("ceo@agentorc.ca")
    assert oid is not None
    assert _sql("SELECT count(*) FROM owners WHERE owner_id=%s::uuid",
                (oid,))[0][0] == 1, "resolution must return a real owners row"


def test_06_revoked_membership_cannot_receive_work(granted):
    granted("temp.worker@agentorc.ca", owner_id=None)
    assignable.grant("temp.worker@agentorc.ca",
                     owner_id=assignable.resolve("ceo@agentorc.ca"),
                     added_by="test")
    assert assignable.is_assignable("temp.worker@agentorc.ca")
    assignable.revoke("temp.worker@agentorc.ca", by="test")
    assert assignable.is_assignable("temp.worker@agentorc.ca") is False


def test_07_a_revoked_row_is_kept_not_deleted(granted):
    """Who could receive work last quarter is a real question."""
    granted("gone@agentorc.ca")
    assignable.revoke("gone@agentorc.ca", by="test")
    assert _sql("SELECT is_active FROM assignable_identity "
                "WHERE lower(email)='gone@agentorc.ca'")[0][0] is False


# ── 2. identity resolution ───────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["agent", "Alan Qin", "David", "", "   ",
                                 None, "not-an-email"])
def test_10_display_names_are_never_identity_keys(bad):
    assert assignable.resolve(bad) is None


def test_11_unknown_email_is_rejected():
    assert assignable.resolve("nobody@example.invalid") is None


def test_12_resolution_is_case_insensitive():
    assert assignable.resolve("CEO@AgentOrc.CA") == \
        assignable.resolve("ceo@agentorc.ca")


def test_13_duplicate_email_is_impossible():
    """A duplicate would make resolution non-deterministic — the one thing an
    identity key may never be."""
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _sql("""INSERT INTO assignable_identity (email, source)
                VALUES ('CEO@agentorc.ca', 'manual')""", fetch=False)


def test_14_resolve_never_provisions():
    before = _sql("SELECT count(*) FROM assignable_identity")[0][0]
    for probe in ("ghost@agentorc.ca", "nobody@example.invalid", "agent"):
        assignable.resolve(probe)
    assert _sql("SELECT count(*) FROM assignable_identity")[0][0] == before


def test_15_provisioning_is_a_separate_explicit_operation():
    """Checked against the CODE, not the docstring — which necessarily names
    grant() while explaining that resolve() must never call it."""
    import inspect
    import re as _re
    src = inspect.getsource(assignable.resolve)
    body = _re.sub(r'""".*?"""', "", src, flags=_re.S)
    for verb in ("INSERT", "UPDATE", "grant("):
        assert verb not in body, "resolve() can write — routing could create staff"


def test_16_grant_requires_an_email_not_a_name():
    assert assignable.grant("David Smith", added_by="test")["ok"] is False


# ── 3. classification ────────────────────────────────────────────────────────

def test_20_an_executive_classifies_as_assignable():
    oid = assignable.resolve("ceo@agentorc.ca")
    assert assignable.identity_space(oid)["space"] == "assignable"


def test_21_a_customer_contact_owner_classifies_as_such():
    row = _sql("""SELECT o.owner_id::text FROM owners o
                  JOIN contacts c ON lower(c.email)=lower(o.email) LIMIT 1""")
    assert assignable.identity_space(row[0][0])["space"] == "customer_contact"


def test_22_a_demo_employee_classifies_as_demo():
    row = _sql("""SELECT employee_uuid::text FROM employees
                  WHERE employee_uuid IS NOT NULL
                    AND email LIKE '%%@company.com' LIMIT 1""")
    if not row:
        pytest.skip("no demo employee with a uuid")
    spaces = {f["space"] for f in assignable.identity_space(row[0][0])["spaces"]}
    assert "demo_employee" in spaces


def test_22b_a_colliding_uuid_reports_every_space_it_lives_in():
    """a1451ad6… is julia.martin@company.com in `employees` AND
    john.smith@example.com in `owners` — one uuid, two people — and it owns all
    120 historical cases. First-match-wins would answer with whichever table it
    tried first."""
    row = _sql("""SELECT e.employee_uuid::text FROM employees e
                  JOIN owners o ON o.owner_id = e.employee_uuid LIMIT 1""")
    if not row:
        pytest.skip("no colliding uuid in this database")
    r = assignable.identity_space(row[0][0])
    assert r["collision"] is True
    assert len(r["spaces"]) >= 2
    assert "DIFFERENT people" in r["reason"]


def test_22c_a_clean_uuid_reports_no_collision():
    oid = assignable.resolve("ceo@agentorc.ca")
    assert assignable.identity_space(oid)["collision"] is False


def test_23_an_unknown_uuid_is_unresolvable():
    assert assignable.identity_space(str(uuid.uuid4()))["space"] == "unresolvable"


def test_24_garbage_is_unresolvable():
    assert assignable.identity_space("agent")["space"] == "unresolvable"


# ── 4. inventory: report the gap, never guess it ─────────────────────────────

def test_30_employees_are_listed_as_candidates_not_imported():
    inv = assignable.inventory()
    assert inv["assignable"] == 4
    assert len(inv["candidates"]["employees_not_granted"]) >= 9
    assert "not imported" in inv["note"]


def test_31_no_executive_is_missing_from_the_directory():
    assert assignable.inventory()["candidates"]["executives_not_granted"] == []


def test_32_activity_provenance_is_counted_not_rewritten():
    p = assignable.owner_id_provenance("activities")
    assert p["ok"]
    assert p["total"] == p["unowned"] + p["assignable"] + p["legacy_owner"] \
        + p["demo_employee"] + p["unresolvable"]
    assert p["demo_employee"] > 0 and p["legacy_owner"] > 0
    assert "Nothing is rewritten" in p["note"]


def test_33_the_unresolvable_rows_are_quarantined_not_repaired():
    before = _sql("""SELECT count(*) FROM activities a
                     WHERE a.owner_id IS NOT NULL
                       AND NOT EXISTS (SELECT 1 FROM owners o
                                       WHERE o.owner_id=a.owner_id)""")[0][0]
    assignable.owner_id_provenance("activities")
    after = _sql("""SELECT count(*) FROM activities a
                    WHERE a.owner_id IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM owners o
                                      WHERE o.owner_id=a.owner_id)""")[0][0]
    assert before == after, "the inventory modified activity ownership"


def test_34_no_mass_creation_occurred():
    """21 employees + 44 owners exist; the directory holds 4."""
    assert _sql("SELECT count(*) FROM assignable_identity")[0][0] == 4
    assert _sql("SELECT count(*) FROM owners")[0][0] == 44


# ── 5. C1 is untouched while strict mode is off ──────────────────────────────

def test_40_strict_is_off_by_default():
    assert assignable.STRICT is False


def test_41_c1_resolution_is_byte_identical_with_strict_off():
    row = _sql("""SELECT owner_id::text, email FROM owners
                  WHERE email IS NOT NULL AND coalesce(is_active,true) LIMIT 1""")
    oid, email = row[0]
    assert cases.resolve_owner(email) == oid


def test_42_strict_mode_makes_membership_the_authority(monkeypatch):
    """The behaviour ASSIGNABLE_STRICT=1 buys — proved without shipping it."""
    monkeypatch.setattr(assignable, "STRICT", True)
    row = _sql("""SELECT o.email FROM owners o
                  JOIN contacts c ON lower(c.email)=lower(o.email) LIMIT 1""")
    assert cases.resolve_owner(row[0][0]) is None, (
        "a customer contact resolved as an assignable worker")
    assert cases.resolve_owner("ceo@agentorc.ca") is not None


def test_43_strict_mode_still_returns_a_validated_owner_id(monkeypatch):
    """C1's contract: assignment takes an owners.owner_id, strict or not."""
    monkeypatch.setattr(assignable, "STRICT", True)
    oid = cases.resolve_owner("ceo@agentorc.ca")
    assert _sql("SELECT count(*) FROM owners WHERE owner_id=%s::uuid",
                (oid,))[0][0] == 1


def test_44_the_case_mutation_boundary_is_unchanged():
    import inspect
    src = inspect.getsource(cases.assign)
    assert "_owner_exists" in src and "resolve_owner()" in src


# ── 6. no derivation, no inference ───────────────────────────────────────────

def test_50_assignability_is_stored_not_derived():
    import inspect
    src = inspect.getsource(assignable)
    assert "FROM assignable_identity" in src


def test_51_no_email_domain_heuristic_exists():
    """Two internal domains exist (@company.com demo, @agentorc.ca real) and
    they identify DIFFERENT populations, so a domain rule is not durable.

    Checked against executable code only: the module documents the domains at
    length, and a naive scan would flag the very prose warning against them."""
    import inspect
    import re as _re
    # Scoped to the functions that DECIDE. inventory() legitimately names the
    # demo domain in a report explaining why those rows were not imported —
    # that is prose for a human, not a rule the code applies.
    deciders = (assignable.resolve, assignable.is_assignable, assignable.grant,
                assignable.identity_space)
    code = "".join(_re.sub(r'""".*?"""', "", inspect.getsource(f), flags=_re.S)
                   for f in deciders)
    code = _re.sub(r"(?m)^\s*#.*$", "", code)
    for heuristic in ("agentorc.ca", "company.com", "endswith("):
        assert heuristic not in code, f"a domain heuristic leaked in: {heuristic}"


def test_52_no_job_title_or_department_heuristic():
    import inspect
    src = inspect.getsource(assignable.resolve) + \
        inspect.getsource(assignable.is_assignable)
    for h in ("job_title", "department", "role"):
        assert h not in src
