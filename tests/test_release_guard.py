"""Foundation Release — configuration controls that cannot be forgotten.

The calendar feed proved the failure mode: the secret-URL control was
implemented CORRECTLY and the feed still served 903 KB of account names,
commercial margins and 2,000 email addresses — because the environment variable
was unset and the code fell through to its documented demo posture.

A control that only engages when somebody remembers to set it is not a control.
So the posture is checked at startup, and a deployed environment refuses to
start rather than serving unsecured data.
"""
import importlib

import pytest

from app.core import release_guard


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every check reads os.getenv directly, so the test controls the whole
    environment rather than inheriting the developer's .env."""
    for k in ("APP_ENV", "ENVIRONMENT", "RAILWAY_ENVIRONMENT",
              "RAILWAY_PROJECT_ID", "APP_URL", "CALENDAR_FEED_TOKEN",
              "CALENDAR_FEED_PUBLIC", "API_AUTH_ENABLED", "ADMIN_API_TOKEN",
              "LLM_ALT_TIER_TRAINING_ACK", "LLM_ALT_TIER"):
        monkeypatch.delenv(k, raising=False)


# ── 1. what counts as deployed ───────────────────────────────────────────────

def test_01_a_laptop_is_not_deployed():
    assert release_guard.is_deployed() is False


@pytest.mark.parametrize("var,val", [
    ("APP_ENV", "production"), ("APP_ENV", "staging"),
    ("ENVIRONMENT", "prod"), ("RAILWAY_ENVIRONMENT", "production"),
    ("RAILWAY_PROJECT_ID", "abc123"),
    ("APP_URL", "https://orbitcrm-production.up.railway.app"),
])
def test_02_deployment_signals_are_recognised(monkeypatch, var, val):
    monkeypatch.setenv(var, val)
    assert release_guard.is_deployed() is True


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000"])
def test_03_a_local_app_url_is_not_deployment(monkeypatch, url):
    monkeypatch.setenv("APP_URL", url)
    assert release_guard.is_deployed() is False


def test_04_an_explicit_declaration_beats_the_url(monkeypatch):
    """A developer pointing APP_URL at production must not trip the guard if
    they have said this is development."""
    monkeypatch.setenv("APP_URL", "https://orbitcrm-production.up.railway.app")
    monkeypatch.setenv("APP_ENV", "development")
    assert release_guard.is_deployed() is False


# ── 2. the calendar feed cannot start unsecured in production ────────────────

def test_10_a_deployed_start_without_the_token_is_refused(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(release_guard.UnsafeConfiguration) as e:
        release_guard.enforce()
    assert "CALENDAR_FEED_TOKEN" in str(e.value)


def test_11_a_token_permits_the_start(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CALENDAR_FEED_TOKEN", "x" * 40)
    monkeypatch.setenv("API_AUTH_ENABLED", "1")
    r = release_guard.enforce()
    assert r["blocking"] == [] and r["safe_to_start"] is True


def test_12_a_short_token_starts_but_is_flagged(monkeypatch):
    """It is the only thing protecting the feed, so length is not cosmetic."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CALENDAR_FEED_TOKEN", "short")
    r = release_guard.audit()
    cal = [c for c in r["checks"] if c["control"] == "calendar_feed"][0]
    assert cal["ok"] is True and cal["severity"] == "advisory"
    assert "at least 24" in cal["message"]


def test_13_public_access_is_possible_but_must_be_declared(monkeypatch):
    """An escape hatch, not a loophole: it turns an accident into a decision."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CALENDAR_FEED_PUBLIC", "1")
    r = release_guard.enforce()
    assert r["blocking"] == []
    cal = [c for c in r["checks"] if c["control"] == "calendar_feed"][0]
    assert "PUBLIC by explicit choice" in cal["message"]
    assert "account names" in cal["message"]


def test_14_local_development_is_never_blocked(monkeypatch):
    """Production safety bought with developer friction gets disabled."""
    r = release_guard.enforce()          # nothing set at all
    assert r["deployed"] is False and r["safe_to_start"] is True
    assert r["blocking"], "the issue should still be REPORTED locally"


# ── 3. the feed itself ───────────────────────────────────────────────────────

def test_20_the_token_actually_gates_the_feed(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("CALENDAR_FEED_TOKEN", "release-test-token")
    c = TestClient(app)
    assert c.get("/calendar/activities.ics").status_code == 403
    assert c.get("/calendar/activities.ics?token=wrong").status_code == 403
    ok = c.get("/calendar/activities.ics?token=release-test-token")
    assert ok.status_code == 200 and "BEGIN:VCALENDAR" in ok.text


def test_21_a_refused_feed_leaks_no_event_data(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("CALENDAR_FEED_TOKEN", "release-test-token")
    body = TestClient(app).get("/calendar/activities.ics?token=wrong").text
    for leak in ("BEGIN:VEVENT", "SUMMARY", "Margin", "INV-", "@"):
        assert leak not in body, f"the refusal leaked {leak}"


# ── 4. api auth is diagnosed, not hard-stopped ───────────────────────────────

def _posture(monkeypatch, posture, conflict=""):
    """Set the RESOLVED posture the application is running on.

    Deliberately not monkeypatch.setenv: auth_dep resolves the posture ONCE at
    import, so setting the environment inside a test changes nothing a running
    process would see. A test that pretended otherwise would be testing a
    fiction — and that fiction is exactly what these tests exist to catch."""
    from app.core import auth_dep
    monkeypatch.setattr(auth_dep, "SECURITY_POSTURE", posture)
    monkeypatch.setattr(auth_dep, "API_AUTH_ENABLED", posture != "open")
    monkeypatch.setattr(auth_dep, "API_PUBLIC_READ", posture == "public-read")
    monkeypatch.setattr(auth_dep, "POSTURE_CONFLICT", conflict)


def _api_check():
    return [c for c in release_guard.audit()["checks"]
            if c["control"] == "api_auth"][0]


def test_30_an_open_posture_is_advisory_not_blocking(monkeypatch):
    """Deliberately softer than the calendar check: the posture governs a staged
    rollout across many endpoints, and hard-stopping a deploy over it could take
    down a working system."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CALENDAR_FEED_TOKEN", "y" * 40)
    _posture(monkeypatch, "open")
    r = release_guard.enforce()
    api = [c for c in r["checks"] if c["control"] == "api_auth"][0]
    assert api["ok"] is False and api["severity"] == "advisory"
    assert r["safe_to_start"] is True


def test_31_a_locked_posture_is_reported_clean(monkeypatch):
    _posture(monkeypatch, "locked")
    assert _api_check()["ok"] is True


def test_32_public_read_is_permitted_but_never_silent(monkeypatch):
    _posture(monkeypatch, "public-read")
    api = _api_check()
    assert api["ok"] is True and api["severity"] == "advisory"
    assert "anyone may READ" in api["message"]


# ── 4b. the guard must report the posture the app is RUNNING on ──────────────
# The first version of this check re-derived the posture from os.environ and
# got it wrong in BOTH directions. These two tests are the regression.

def test_33_a_locked_deployment_is_not_a_false_alarm(monkeypatch):
    """API_SECURITY_MODE=locked resolves to a locked application. A guard that
    reads API_AUTH_ENABLED from the environment sees nothing set and reports
    'not enforcing' — a false alarm that trains operators to ignore it."""
    monkeypatch.delenv("API_AUTH_ENABLED", raising=False)
    monkeypatch.setenv("API_SECURITY_MODE", "locked")   # deliberately ignored
    _posture(monkeypatch, "locked")
    api = _api_check()
    assert api["ok"] is True, "a locked application was reported as unsecured"


def test_34_an_open_deployment_is_not_a_false_all_clear(monkeypatch):
    """The dangerous direction. API_SECURITY_MODE=open wins over
    API_AUTH_ENABLED=1, so the application enforces NOTHING — and the old check,
    reading API_AUTH_ENABLED from the environment, reported it CLEAN.

    An application with no authentication at all passed its own security check.
    A guard that reports the wrong posture is worse than no guard, because it
    manufactures confidence."""
    monkeypatch.setenv("API_AUTH_ENABLED", "1")         # deliberately ignored
    monkeypatch.setenv("API_SECURITY_MODE", "open")
    _posture(monkeypatch, "open", conflict="API_AUTH_ENABLED")
    api = _api_check()
    assert api["ok"] is False, "an unauthenticated application passed the audit"
    assert "enforce NOTHING" in api["message"]


def test_35_the_guard_reads_the_resolved_posture_not_the_environment():
    """Structural: re-deriving the posture is how it went wrong the first time.

    Inspects the AST for actual environment READS rather than scanning text.
    Text is the wrong instrument twice over here — the docstring explains the
    trap, and the advisory message legitimately tells the operator to set
    API_SECURITY_MODE. A scan cannot tell a mention from a read."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(release_guard._check_api_auth)))

    reads = []
    for node in ast.walk(tree):
        # os.getenv(...) / os.environ.get(...)
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in ("getenv", "get") \
                    and ast.unparse(f).startswith(("os.getenv", "os.environ")):
                reads.append(ast.unparse(node))
            # the module's own env helper — checked HERE, not in a later elif:
            # 'isinstance(node, ast.Call)' above already claims every Call, so
            # an elif for _flag would never run. Found by mutation-testing this
            # test against the old implementation, which it failed to catch.
            elif isinstance(f, ast.Name) and f.id == "_flag":
                reads.append(ast.unparse(node))
        # os.environ["X"]
        elif isinstance(node, ast.Subscript) and \
                ast.unparse(node.value) == "os.environ":
            reads.append(ast.unparse(node))

    assert reads == [], (
        f"_check_api_auth re-derives the posture from the environment "
        f"({reads}); it must read auth_dep's resolved values")


def test_36_a_conflicting_posture_is_named_not_silent(monkeypatch):
    """Two ways to express one posture, and the mode wins SILENTLY. An operator
    who set API_AUTH_ENABLED=1 on the platform must be told it did nothing."""
    _posture(monkeypatch, "locked", conflict="API_AUTH_ENABLED")
    api = _api_check()
    assert api["ok"] is True, "the posture itself is still correct"
    assert api["severity"] == "advisory", "but it must not read as all-clear"
    assert "IGNORED" in api["message"] and "API_AUTH_ENABLED" in api["message"]


def test_37_no_conflict_means_no_misleading_note(monkeypatch):
    """The note must not fire when no legacy flag is set — an unconditional
    'the mode wins' warning on an environment with no mode is noise, and noise
    is how a real warning gets ignored."""
    _posture(monkeypatch, "open")
    assert "IGNORED" not in _api_check()["message"]


# ── 5. the free-tier training acknowledgement must not travel ────────────────

def test_40_training_ack_blocks_a_deployed_start(monkeypatch):
    """Deployed CONVERSATIONS are real people even when the records are
    synthetic."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CALENDAR_FEED_TOKEN", "z" * 40)
    monkeypatch.setenv("LLM_ALT_TIER_TRAINING_ACK", "1")
    with pytest.raises(release_guard.UnsafeConfiguration) as e:
        release_guard.enforce()
    assert "real people" in str(e.value)


def test_41_a_paid_tier_makes_the_ack_moot(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CALENDAR_FEED_TOKEN", "z" * 40)
    monkeypatch.setenv("LLM_ALT_TIER_TRAINING_ACK", "1")
    monkeypatch.setenv("LLM_ALT_TIER", "paid")
    assert release_guard.enforce()["blocking"] == []


def test_42_the_ack_is_fine_locally(monkeypatch):
    monkeypatch.setenv("LLM_ALT_TIER_TRAINING_ACK", "1")
    assert release_guard.audit()["safe_to_start"] is True


# ── 6. the audit never raises ────────────────────────────────────────────────

def test_50_a_broken_check_does_not_mask_the_others(monkeypatch):
    def boom():
        raise RuntimeError("check exploded")

    monkeypatch.setattr(release_guard, "CHECKS", (boom,) + release_guard.CHECKS)
    r = release_guard.audit()
    assert any("check failed" in c["message"] for c in r["checks"])
    assert any(c["control"] == "calendar_feed" for c in r["checks"])


def test_51_the_guard_runs_at_startup():
    import pathlib
    main = pathlib.Path("app/main.py").read_text(encoding="utf-8")
    assert "release_guard.enforce()" in main
    i = main.index("release_guard.enforce()")
    assert i < main.index("test_connection()"), (
        "the guard must run before anything connects or serves")


# ── 7. /home-index no longer discloses business scale ────────────────────────

def test_60_home_index_carries_the_data_dependency():
    """Aggregate pipeline / leads / orders / alert counts. No customer records,
    but anonymous access lets anyone infer business scale."""
    import pathlib
    main = pathlib.Path("app/main.py").read_text(encoding="utf-8")
    assert "app.include_router(home_router, dependencies=_DATA)" in main


def test_61_a_locked_posture_refuses_an_anonymous_read(monkeypatch):
    """Tests the DEPENDENCY, not the env plumbing.

    An end-to-end attempt is misleading here for a reason that survives the
    dotenv fix: auth_dep resolves the posture ONCE at import, so no environment
    change inside a test reaches an already-imported module. Patch the resolved
    values, as the application itself sees them."""
    import asyncio

    from fastapi import HTTPException

    from app.core import auth_dep
    monkeypatch.setattr(auth_dep, "API_AUTH_ENABLED", True)
    monkeypatch.setattr(auth_dep, "API_PUBLIC_READ", False)

    class _Req:
        headers = {}
        method = "GET"

        class _S:
            pass
        state = _S()

        async def body(self):
            return b""

    loop = asyncio.get_event_loop_policy().new_event_loop()
    with pytest.raises(HTTPException) as e:
        loop.run_until_complete(auth_dep.require_data_access(_Req()))
    assert e.value.status_code in (401, 403)


def test_63_the_platform_environment_beats_a_dotenv_file():
    """config.py used load_dotenv(override=True), so a .env file BEAT the
    platform's own variables — the security posture, the secrets, the DSN. That
    was safe only for as long as .env stayed untracked; a .env committed or
    baked into an image would have made Railway's dashboard settings stop taking
    effect, silently.

    Both halves are asserted, because either alone permits the failure."""
    import pathlib
    import subprocess

    src = pathlib.Path("app/core/config.py").read_text(encoding="utf-8")
    calls = [ln.strip() for ln in src.splitlines()
             if ln.strip().startswith("load_dotenv(")]
    assert calls == ["load_dotenv()"], (
        f"a real environment variable must outrank .env; found {calls}")

    tracked = subprocess.run(["git", "ls-files", ".env"], capture_output=True,
                             text=True, cwd=".").stdout.strip()
    assert tracked == "", ".env is tracked — it would travel into a deployment"


def test_62_the_posture_switch_is_the_reliable_control():
    """API_SECURITY_MODE beats the individual flags: it is resolved first and
    cannot be half-set. Setting one legacy flag and forgetting the other is how
    an intended lockdown silently stays open."""
    from app.core import auth_dep
    import inspect
    src = inspect.getsource(auth_dep)
    i_mode = src.index('_MODE = os.getenv("API_SECURITY_MODE"')
    i_flag = src.index('API_AUTH_ENABLED = _flag(')
    assert i_mode < i_flag, "the single switch must be resolved first"


def test_64_the_conflict_is_computed_where_the_posture_is():
    """auth_dep owns the posture, so it owns knowing when the posture was
    expressed twice. Exporting it keeps the release guard from re-reading the
    environment — the bug test_35 forbids."""
    from app.core import auth_dep
    assert isinstance(auth_dep.POSTURE_CONFLICT, str)
