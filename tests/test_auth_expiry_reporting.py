"""An expired session must not be reported as a role problem.

Observed 2026-07-27: every admin page returned 403 on /console/queue and
/platform/health. The signed-in identity was admin@conscestra.local with
role='admin' — the role was correct. All three of its sessions had EXPIRED
(newest: 2026-07-23 19:45). require_admin() fell through to the same 403 it
uses for "wrong role", so the UI said "administrator role required" and sent an
admin hunting for a permission problem that did not exist.

    401  re-authenticate      (expired / invalid credential)
    403  you may not do this  (valid identity, insufficient role)

Different failures, different fixes.
"""
import pytest
from fastapi import HTTPException

from app.core import auth_dep


class _Req:
    """Minimal Request stand-in: headers plus a state object."""

    def __init__(self, bearer=None, admin_token=None):
        h = {}
        if bearer:
            h["authorization"] = f"Bearer {bearer}"
        if admin_token:
            h["x-admin-token"] = admin_token
        self.headers = h

        class _S:
            pass

        self.state = _S()


async def _call(req):
    return await auth_dep.require_admin(req)


def _run(req):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _call(req))


@pytest.fixture(autouse=True)
def _protection_on(monkeypatch):
    """The dev bypass must be off, or every case returns True."""
    monkeypatch.setattr(auth_dep, "ADMIN_API_TOKEN", "test-ops-token")
    monkeypatch.setattr(auth_dep, "API_AUTH_ENABLED", True)


def _no_session(monkeypatch):
    import app.agents.auth.router as ar
    monkeypatch.setattr(ar, "get_session", lambda t: None)


def _session(monkeypatch, role):
    import app.agents.auth.router as ar
    monkeypatch.setattr(ar, "get_session",
                        lambda t: {"role": role, "identifier": "x@y.z"})


def test_01_expired_session_reports_401_not_403(monkeypatch):
    """THE reported bug: an admin whose token lapsed was told their role was
    insufficient."""
    _no_session(monkeypatch)
    with pytest.raises(HTTPException) as e:
        _run(_Req(bearer="a-stale-token"))
    assert e.value.status_code == 401
    assert "sign in again" in e.value.detail.lower()


def test_02_the_message_does_not_blame_the_role(monkeypatch):
    _no_session(monkeypatch)
    with pytest.raises(HTTPException) as e:
        _run(_Req(bearer="a-stale-token"))
    assert "role" not in e.value.detail.lower()


def test_03_a_valid_non_admin_session_still_gets_403(monkeypatch):
    """Unchanged: the identity is fine, the permission is not."""
    _session(monkeypatch, "viewer")
    with pytest.raises(HTTPException) as e:
        _run(_Req(bearer="viewer-token"))
    assert e.value.status_code == 403
    assert "Admin authorization required" in e.value.detail


def test_04_no_credential_at_all_still_fails_closed_with_403(monkeypatch):
    """Deliberately unchanged — the documented fail-closed posture, and machine
    callers depend on it."""
    _no_session(monkeypatch)
    with pytest.raises(HTTPException) as e:
        _run(_Req())
    assert e.value.status_code == 403


def test_05_a_valid_admin_session_is_accepted(monkeypatch):
    _session(monkeypatch, "admin")
    assert _run(_Req(bearer="good-token")) is True


def test_06_the_ops_token_still_works(monkeypatch):
    _no_session(monkeypatch)
    assert _run(_Req(admin_token="test-ops-token")) is True


def test_07_a_wrong_ops_token_is_re_authentication_not_forbidden(monkeypatch):
    """A bad machine credential is an authentication failure too."""
    _no_session(monkeypatch)
    with pytest.raises(HTTPException) as e:
        _run(_Req(bearer="not-the-ops-token"))
    assert e.value.status_code == 401


def test_08_nothing_was_granted_that_was_not_before(monkeypatch):
    """The fix changes only the STATUS CODE of a refusal. Every path that
    refused before must still refuse."""
    _no_session(monkeypatch)
    for req in (_Req(), _Req(bearer="stale"), _Req(admin_token="wrong")):
        with pytest.raises(HTTPException):
            _run(req)
