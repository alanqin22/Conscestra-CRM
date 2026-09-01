"""Release configuration guard — Foundation Release (C1–C5).

A control that only engages when an environment variable is set is not a
control until somebody sets it. The calendar feed proved this: the secret-URL
pattern was implemented correctly and the feed was still serving 903 KB of
account names, commercial margins and 2,000 email addresses, because
CALENDAR_FEED_TOKEN was unset and the code fell through to its documented
"demo public-read posture".

So the posture itself is now checked at startup. In a deployed environment the
process REFUSES TO START rather than serving unsecured data — a failed deploy
is recoverable in minutes; a public feed of customer names is not.

WHY REFUSE RATHER THAN WARN: a warning in a log nobody reads is the same
outcome as no control at all. That is precisely how this shipped unsecured in
the first place.

THE ESCAPE HATCH IS DELIBERATE, NOT A LOOPHOLE. An operator who genuinely wants
a public calendar can set CALENDAR_FEED_PUBLIC=1. That turns an accident into a
decision, which is the entire point — and it keeps a real deployment from being
locked out by a control it disagrees with.

Local development is unaffected: nothing here fires unless the environment
looks deployed.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger("release_guard")


class UnsafeConfiguration(RuntimeError):
    """A deployed environment is missing a control that protects real data."""


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def is_deployed() -> bool:
    """Does this look like a deployed environment rather than a laptop?

    Checked in order of confidence. An explicit declaration wins; otherwise the
    platform's own markers; otherwise the app's public URL. Every signal is
    positive evidence of deployment — absence of evidence keeps the checks
    quiet, so a developer is never blocked by a guard meant for production.
    """
    declared = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    if declared:
        return declared in ("prod", "production", "staging", "live")
    # Railway sets these itself; their presence is not something a laptop fakes.
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
        return True
    url = (os.getenv("APP_URL") or "").strip().lower()
    if url and not any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0")):
        return True
    return False


# ── the checks ──────────────────────────────────────────────────────────────
# Each returns (ok, severity, message). BLOCKING failures stop startup in a
# deployed environment; ADVISORY ones are logged loudly and reported.

def _check_calendar_feed() -> Dict[str, Any]:
    token = (os.getenv("CALENDAR_FEED_TOKEN") or "").strip()
    if token:
        weak = len(token) < 24
        return {"control": "calendar_feed", "ok": True,
                "severity": "advisory" if weak else "ok",
                "message": ("CALENDAR_FEED_TOKEN is set but short — use at "
                            "least 24 random characters, it is the only thing "
                            "protecting the feed"
                            if weak else "calendar feed requires a token")}
    if _flag("CALENDAR_FEED_PUBLIC"):
        return {"control": "calendar_feed", "ok": True, "severity": "advisory",
                "message": "calendar feed is PUBLIC by explicit choice "
                           "(CALENDAR_FEED_PUBLIC=1) — it exposes account "
                           "names, margins and invoice references"}
    return {"control": "calendar_feed", "ok": False, "severity": "blocking",
            "message": "CALENDAR_FEED_TOKEN is not set. /calendar/activities.ics "
                       "would serve account names, commercial margins and email "
                       "addresses to anyone. Set CALENDAR_FEED_TOKEN to a long "
                       "random value, or set CALENDAR_FEED_PUBLIC=1 to accept "
                       "public access deliberately."}


def _check_api_auth() -> Dict[str, Any]:
    """Report the posture the application IS RUNNING ON.

    Deliberately reads auth_dep's RESOLVED values rather than re-deriving them
    from os.environ. The first version of this check read the env directly and
    reproduced the very trap the release review documented:

        API_SECURITY_MODE=locked  ->  effective 'locked'  ->  reported "not
                                      enforcing"          (false alarm)
        API_SECURITY_MODE=open    ->  effective 'open'    ->  reported "CLEAN"
                                      with API_AUTH_ENABLED=1 also set
                                                          (FALSE ALL-CLEAR)

    The second is why this matters: an application with no authentication at
    all passed its own security check. A guard that reports the wrong posture
    is worse than no guard, because it manufactures confidence.

    Softer than the calendar check for the postures somebody CHOSE — `locked`
    and `public-read` are staged-rollout decisions across many endpoints, and
    hard-stopping a deploy over one could take down a working system.

    NOT softer for `open`, as of 2026-08-31. That posture is reached by
    misspelling the mode or by configuring nothing, both of which are the
    absence of a decision rather than a decision, and it removes anonymous-write
    protection at all three layers at once. It blocks in a deployed environment
    unless API_ALLOW_OPEN=1 says otherwise. See the reasoning at the branch.
    """
    try:
        from app.core import auth_dep
        posture = auth_dep.SECURITY_POSTURE
        conflict = getattr(auth_dep, "POSTURE_CONFLICT", "")
        unrecognised = getattr(auth_dep, "SECURITY_MODE_UNRECOGNISED", "")
    except Exception as exc:
        return {"control": "api_auth", "ok": False, "severity": "advisory",
                "message": f"could not resolve the security posture: {exc}"}

    # Named because it is the trap, not a footnote: an operator who sets
    # API_AUTH_ENABLED=1 beside a stale API_SECURITY_MODE gets the mode's
    # answer silently, and would otherwise read this report as agreement.
    note = (f" ({conflict} is set but IGNORED — API_SECURITY_MODE wins.)"
            if conflict else "")
    # Reported wherever it appears, even when the resulting posture is safe: a
    # mode string nobody recognises is a misconfiguration whether or not it
    # happened to land somewhere harmless, and the operator should learn that
    # the word they wrote does nothing.
    if unrecognised:
        note += (f" (API_SECURITY_MODE={unrecognised!r} is not a recognised "
                 f"value and was IGNORED.)")

    if posture == "locked":
        return {"control": "api_auth", "ok": True,
                "severity": "advisory" if (conflict or unrecognised) else "ok",
                "message": "posture=locked — every data call requires a login."
                           + note}
    if posture == "public-read":
        return {"control": "api_auth", "ok": True, "severity": "advisory",
                "message": "posture=public-read — anyone may READ CRM data; "
                           "writes require an Admin/writer login. Deliberate "
                           "for a demo; use API_SECURITY_MODE=locked otherwise."
                           + note}

    # ── posture == open ─────────────────────────────────────────────────────
    # BLOCKING in a deployed environment, and this is a deliberate change from
    # the softer treatment this check used to give it.
    #
    # WHY THE OLD REASONING NO LONGER APPLIES. It said hard-stopping a deploy
    # "over a posture decision" could take down a working system. True of a
    # posture somebody CHOSE. `open` is almost never chosen: it is reached by
    # misspelling the mode (`blocked` reads like lockdown, matches nothing,
    # falls through to flags that are unset in any deployment using the single
    # switch) or by not configuring one at all. Both are the absence of a
    # decision, not the presence of one.
    #
    # WHY IT IS WORTH A FAILED DEPLOY. Measured, not assumed: under `open`,
    # require_data_access returns before it stamps the caller's role, and all
    # THREE write controls key off that role. So they fail together —
    #     HTTP gate           anonymous structured write ALLOWED
    #     write_guard._role   None, read as "system/background — never gated"
    #     readonly_context()  None, so no read-only transaction is opened
    #     guard_query()       write stored procedure ALLOWED
    # — leaving anonymous create/update/delete on every CRM record. That is a
    # different class of exposure from the deliberate public-read posture,
    # which protects writes at all three of those layers.
    #
    # THE ESCAPE HATCH IS THE POINT, exactly as CALENDAR_FEED_PUBLIC is for the
    # feed: an operator who genuinely wants an unauthenticated deployment sets
    # API_ALLOW_OPEN=1 and gets it. That turns an accident into a decision, and
    # keeps a real deployment from being locked out by a control it disagrees
    # with.
    if _flag("API_ALLOW_OPEN"):
        return {"control": "api_auth", "ok": True, "severity": "advisory",
                "message": "posture=open — CRM data endpoints enforce NOTHING, "
                           "reads AND writes, by explicit choice "
                           "(API_ALLOW_OPEN=1)." + note}

    cause = (f"API_SECURITY_MODE={unrecognised!r} is not a recognised value, so "
             f"it was ignored"
             if unrecognised else
             "no API_SECURITY_MODE is set and neither legacy flag is enabled")
    return {"control": "api_auth", "ok": False, "severity": "blocking",
            "message": (
                f"posture=open — CRM data endpoints enforce NOTHING: anonymous "
                f"callers may create, update and delete records, because all "
                f"three write controls key off a role this posture never "
                f"stamps. Cause: {cause}. Set API_SECURITY_MODE=locked (or "
                f"public-read for a demo); accepted values are "
                f"open|off · public-read|publicread|read · "
                f"locked|lockdown|full|strict. To run without authentication "
                f"deliberately, set API_ALLOW_OPEN=1." + note)}


def _check_public_read_corpus() -> Dict[str, Any]:
    """public-read is safe only while the corpus is demonstration data.

    The posture is a deliberate product decision — a prospective client must see
    the CRM working without a login — and it rests entirely on an assumption
    about WHAT IS IN THE DATABASE. Nothing checked that assumption, and nothing
    would have noticed the day it stopped holding. The marketing motion this
    posture exists to serve is the very thing that ends it: demos become
    clients, and clients bring real records.

    TWO SIGNALS, TWO SEVERITIES, and the split is the design.

      BLOCKING   a customer subject CLASSIFIED `real`. That classification can
                 only come from an external trace or a named person (the schema
                 forbids every heuristic), so it is definitive, and serving
                 definitively-real customer records anonymously is not a
                 posture anyone should reach by inaction.

      ADVISORY   an address outside the domains this deployment generates into.
                 Deliberately over-sensitive, therefore deliberately NOT
                 blocking: you do not hard-stop a deployment on a smoke
                 detector calibrated to catch toast. Measured when written, it
                 fires on eight rows in the legacy `customers` table that no
                 anonymous read path can reach.

    Quiet unless the posture is public-read: under `locked` there is nothing to
    protect against here, and under `open` the api_auth check has already said
    something louder.
    """
    try:
        from app.core import auth_dep
        if auth_dep.SECURITY_POSTURE != "public-read":
            return {"control": "public_read_corpus", "ok": True, "severity": "ok",
                    "message": f"posture={auth_dep.SECURITY_POSTURE} — the "
                               f"corpus assumption is not load-bearing here."}
        from app.core import corpus_provenance as cp
        trip = cp.tripwire()
        classified_real = [r for r in trip["reasons"] if "classified real" in r]
    except Exception as exc:
        # Cannot verify != verified safe. Advisory rather than blocking: an
        # unreadable corpus is usually a database that is not up yet, and
        # refusing to start on that would make this control the reason the
        # application cannot reach its own database.
        return {"control": "public_read_corpus", "ok": False,
                "severity": "advisory",
                "message": f"could not verify the corpus is demonstration "
                           f"data: {str(exc)[:160]}"}

    if classified_real and _flag("PUBLIC_READ_ACCEPT_REAL_DATA"):
        # Same shape as CALENDAR_FEED_PUBLIC: an operator who genuinely intends
        # to serve real customer records to anonymous callers can, and says so
        # in a variable whose name is hard to set by accident. Never silent —
        # an accepted risk that stops being reported stops being managed.
        return {"control": "public_read_corpus", "ok": True,
                "severity": "advisory",
                "message": ("posture=public-read is serving records CLASSIFIED "
                            "as real by explicit choice "
                            "(PUBLIC_READ_ACCEPT_REAL_DATA=1): " +
                            "; ".join(classified_real))}

    if classified_real:
        return {"control": "public_read_corpus", "ok": False,
                "severity": "blocking",
                "message": (
                    "posture=public-read while the corpus holds records "
                    "CLASSIFIED as real: " + "; ".join(classified_real) +
                    ". Anonymous callers can read them. Set "
                    "API_SECURITY_MODE=locked, or set "
                    "PUBLIC_READ_ACCEPT_REAL_DATA=1 to serve real customer "
                    "data publicly as a deliberate decision.")}

    if trip["tripped"]:
        return {"control": "public_read_corpus", "ok": True,
                "severity": "advisory",
                "message": ("posture=public-read and the corpus is not "
                            "uniformly demonstration data: " +
                            "; ".join(trip["reasons"]) +
                            ". Not blocking — the domain signal is an alarm, "
                            "not evidence. Classify them "
                            "(corpus_provenance.classify) so the question is "
                            "settled rather than re-guessed.")}

    return {"control": "public_read_corpus", "ok": True, "severity": "ok",
            "message": "posture=public-read and every customer subject is "
                       "demonstration data or unclassified-with-no-signal."}


def _check_admin_token() -> Dict[str, Any]:
    tok = (os.getenv("ADMIN_API_TOKEN") or "").strip()
    if not tok:
        return {"control": "admin_token", "ok": False, "severity": "advisory",
                "message": "ADMIN_API_TOKEN is unset — privileged command "
                           "endpoints rely solely on an admin session."}
    return {"control": "admin_token", "ok": True, "severity": "ok",
            "message": "ADMIN_API_TOKEN is set (rotate per environment)"}


def _check_training_ack() -> Dict[str, Any]:
    """LLM_ALT_TIER_TRAINING_ACK accepts that a FREE-tier provider may train on
    whatever is sent. That is defensible for synthetic local data. In a
    deployed environment the CONVERSATIONS are real people even when the CRM
    records are synthetic, so this must not travel."""
    if _flag("LLM_ALT_TIER_TRAINING_ACK") and \
            (os.getenv("LLM_ALT_TIER", "free").strip().lower() != "paid"):
        return {"control": "llm_training_ack", "ok": False,
                "severity": "blocking",
                "message": "LLM_ALT_TIER_TRAINING_ACK=1 with a non-paid tier in "
                           "a deployed environment. This accepts that a free-tier "
                           "provider may train on content — and deployed "
                           "conversations are real people. Remove it, or upgrade "
                           "the key and set LLM_ALT_TIER=paid."}
    return {"control": "llm_training_ack", "ok": True, "severity": "ok",
            "message": "no free-tier training acknowledgement in force"}


def _check_secret_strength() -> Dict[str, Any]:
    """A weak secret behaves exactly like a strong one until it is attacked.

    MEMORY_SIGNING_KEY sat on a development placeholder while the assertion
    gate, the verification trail and four red-team controls were all built on
    top of it — nothing failed, nothing logged, and every test stayed green. It
    is the only control here whose compromise silently converts "the attacker
    must forge an HMAC" into "the attacker types a string from the repo".

    BLOCKING on the signing key specifically, because a deployed environment
    running it with a guessable key can state fabricated claims to customers as
    verified fact. The rest are advisory: they are real problems, but refusing
    to boot over them would strand a running business.
    """
    from app.core.secret_health import report as _secret_report
    r = _secret_report()

    signing = next((f for f in r["secrets"]
                    if f["name"] == "MEMORY_SIGNING_KEY"), None)
    if signing and not signing["ok"]:
        return {"control": "secret_strength", "ok": False,
                "severity": "blocking",
                "message": f"MEMORY_SIGNING_KEY is unusable "
                           f"({'; '.join(signing['problems'])}). It authenticates "
                           f"every human verification; with a guessable key the "
                           f"assertion gate can be forged and the system will "
                           f"state invented claims to customers as verified fact."}

    weak = [n for n in r["weak"] if n != "MEMORY_SIGNING_KEY"]
    if weak or r["shared_values"]:
        parts = []
        if weak:
            parts.append("weak or unset: " + ", ".join(weak))
        parts.extend(r["shared_values"])
        return {"control": "secret_strength", "ok": False, "severity": "advisory",
                "message": "; ".join(parts)}

    fps = ", ".join(f"{f['name'].split('_')[0].lower()}:{f['fingerprint']}"
                    for f in r["secrets"] if f["fingerprint"])
    return {"control": "secret_strength", "ok": True, "severity": "ok",
            "message": f"guarded secrets configured and distinct ({fps})"}


def _check_configuration_integrity() -> Dict[str, Any]:
    """Configuration that changes CORRECTNESS, validated at startup.

    Three settings can each produce a silent, customer-visible failure while
    every other signal reports health. They were previously documented as
    deployment notes, which is the weakest possible control: a note cannot fail
    a deploy.

      WEB_CONCURRENCY      Leader election fails CLOSED under multiple workers
                           and OPEN under one. Unset while running several
                           workers means every worker assumes leadership on a
                           database blip — four schedulers, four IMAP pollers,
                           duplicate dunning email. MEASURED: 4 workers, DB
                           unreachable, 4 leaders.

      HA_LEADER_ELECTION   Set to 0, every worker runs the singletons
                           unconditionally. MEASURED: 4 workers, 4 leaders.

      MEMORY_ANN_EF_SEARCH hnsw.ef_search is per SESSION. At the pgvector
                           default of 40 this corpus returned 31.7% recall on
                           the CUSTOMER channel — degraded retrieval presented
                           as a normal answer. MEASURED: 100% at 100.

    Reported, not fatal, for the same reason the rest of this module reports:
    refusing to start a laptop over a deployment variable gets the check
    deleted. In a DEPLOYED environment the first two are blocking, because
    duplicate outbound email cannot be undone.
    """
    problems, blocking = [], False
    workers = 0
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        try:
            workers = max(workers, int(os.getenv(var, "") or 0))
        except ValueError:
            pass

    if not _flag("HA_LEADER_ELECTION", "1"):
        problems.append("HA_LEADER_ELECTION=0 — every worker runs the "
                        "scheduler, IMAP poller and agent-bus consumer")
        blocking = workers > 1

    if workers > 1 and not (os.getenv("WEB_CONCURRENCY") or "").strip():
        problems.append(f"{workers} workers configured but WEB_CONCURRENCY is "
                        f"unset — leader election cannot tell it is running "
                        f"multi-process and will fail OPEN on a database blip")
        blocking = True

    ef = (os.getenv("MEMORY_ANN_EF_SEARCH", "") or "").strip()
    if ef and int(ef or 0) < 100:
        problems.append(f"MEMORY_ANN_EF_SEARCH={ef} — below 100 this corpus "
                        f"measured 31.7% recall on the customer channel")

    if problems:
        return {"control": "configuration_integrity", "ok": False,
                "severity": "blocking" if blocking else "advisory",
                "message": "; ".join(problems)}
    return {"control": "configuration_integrity", "ok": True, "severity": "ok",
            "message": f"correctness-affecting configuration validated "
                       f"(workers={workers or 1}, leader_election=on)"}


def _check_public_url() -> Dict[str, Any]:
    """APP_URL is the host printed into every link we email a human.

    THE DEFECT THIS EXISTS TO CATCH. governance builds one-click approve/reject
    links as `os.getenv("APP_URL","") or "http://localhost:8000"`. That default
    is right on a laptop and silently wrong once deployed, and nothing said so.
    Measured on the info@ BCC archive: 19 of 19 approval emails carried
    `http://localhost:8000` decision links — including the ones production sent
    to the CFO and CRO. Every one-click approval ever emailed was unusable by
    anyone not running the local server, against the local database, holding
    the local signing secret. The buttons rendered, the mail sent, the recipient
    clicked, and got "Link not valid". Nothing in the system was wrong enough to
    report itself.

    A control whose failure looks exactly like success needs a startup check,
    not a comment. Advisory, not blocking: an unreachable link is a broken
    workflow, not an unsafe one, and refusing to boot over it would be worse.
    """
    def _is_local(u: str) -> bool:
        return any(h in u.lower() for h in ("localhost", "127.0.0.1", "0.0.0.0"))

    url = (os.getenv("APP_URL") or "").strip()
    # SINCE 2026-08-21 THERE ARE TWO EMAILED-LINK ORIGINS, and checking only one
    # reopens exactly the hole this control was built to close.
    #
    #   APP_URL          the API. Approval/verification links are ENDPOINTS and
    #                    must resolve here.
    #   PUBLIC_SITE_URL  the static host. The Order Status button in every order
    #                    email is a PAGE, and the app cannot serve pages in
    #                    production (`*.html` is gitignored, so Railway has none
    #                    of them). See order_status._public_site.
    #
    # If PUBLIC_SITE_URL were unset or local while APP_URL was fine, this check
    # would have printed "emailed links resolve to <api>" and been green while
    # every Order Status button led somewhere without the page -- a control
    # whose failure looks exactly like success, which is the whole reason the
    # paragraph above exists.
    site = (os.getenv("PUBLIC_SITE_URL") or "").strip()

    if not is_deployed():
        return {"control": "public_url", "ok": True, "severity": "ok",
                "message": f"APP_URL={url or '(unset → localhost)'} — local"}
    if not url:
        return {"control": "public_url", "ok": False, "severity": "advisory",
                "message": "APP_URL is UNSET in a deployed environment — every "
                           "emailed approval/verification link will point at "
                           "http://localhost:8000 and fail for the recipient"}
    if _is_local(url):
        return {"control": "public_url", "ok": False, "severity": "advisory",
                "message": f"APP_URL={url} in a deployed environment — emailed "
                           f"links point at the recipient's own machine"}
    if site and _is_local(site):
        return {"control": "public_url", "ok": False, "severity": "advisory",
                "message": f"PUBLIC_SITE_URL={site} in a deployed environment — "
                           f"the Order Status button in every order email points "
                           f"at the recipient's own machine"}
    if not site:
        # Not an error: one process CAN serve both, and the fallback is
        # deliberate. But on a deployment where it does not, every Order Status
        # button lands on a host that has no page, so say which case this is
        # rather than reporting a single origin as though it covered both.
        return {"control": "public_url", "ok": True, "severity": "advisory",
                "message": f"endpoint links resolve to {url}; PUBLIC_SITE_URL is "
                           f"unset so page links fall back to it — correct only "
                           f"if this host also serves the HTML"}
    return {"control": "public_url", "ok": True, "severity": "ok",
            "message": f"endpoint links resolve to {url}; page links to {site}"}


def _check_email_call_sites() -> Dict[str, Any]:
    """Every `send_email` caller must be declared, because staff mail has a
    governance layer and a direct call walks past it.

    Unlike every other check here this one inspects CODE, not configuration —
    and it belongs here for the same reason the others do. The alternative was
    a test, and `.github/workflows/ci.yml` says outright that "the tests/
    directory is also outside this repository by policy, so CI cannot run any
    test at all." A guard that only runs when somebody remembers to run pytest
    is a habit, not a control. This one runs on every boot.

    See app/core/email_call_sites.py for the allowlist and what to do when this
    fails."""
    from app.core.email_call_sites import check
    return check()


def _check_capability_registry() -> Dict[str, Any]:
    """An EMPTY capability registry is a permissive gate, and must say so.

    `dispatch` refuses any intent the registry does not name — but only once the
    registry has rows. With none, it falls through to the documented exception
    that treats an unseeded database as "the seed has not run yet", because
    refusing every capability on a fresh checkout would make a missing migration
    a total outage.

    That exception is defensible and it is also exactly where this control goes
    to die quietly: the previous audit found ZERO rows on both databases, so the
    kill switch and agent RBAC had never once fired in production. Reporting the
    state at startup is what stops "armed" and "unarmed" looking identical.

    ADVISORY, not blocking. A permissive capability mesh is the behaviour the
    system has always had; refusing to start over it would be a new outage in
    the name of a control that has never yet been needed. It becomes blocking
    the day agents stop being first-party — see the RBAC trigger in
    docs/deployment_gate_audit.md §14."""
    try:
        from app.core.a2a import registry_state
        st = registry_state()
    except Exception as exc:                                  # pragma: no cover
        return {"control": "capability_registry", "ok": False,
                "severity": "advisory",
                "message": f"could not read the capability registry: {exc}"}

    if not st["seeded"]:
        return {"control": "capability_registry", "ok": False,
                "severity": "advisory",
                "message": (f"capability registry is EMPTY — the closed-by-"
                            f"default gate and the operator kill switch are "
                            f"both INERT ({st['declared']} capabilities "
                            f"declared). Seed with POST /a2a/registry/sync.")}
    if st["unregistered"]:
        return {"control": "capability_registry", "ok": False,
                "severity": "advisory",
                "message": (f"{len(st['unregistered'])} declared capability(ies) "
                            f"have no registry row and will be REFUSED: "
                            f"{', '.join(st['unregistered'][:5])}. Run "
                            f"POST /a2a/registry/sync.")}
    disabled = f", {len(st['disabled'])} disabled by an operator" \
        if st["disabled"] else ""
    return {"control": "capability_registry", "ok": True, "severity": "ok",
            "message": (f"{st['registered']} of {st['declared']} capabilities "
                        f"registered; closed-by-default ACTIVE{disabled}")}


def _check_sql_disposition() -> Dict[str, Any]:
    """Is every SQL artifact classified as governed or out-of-band?

    THE FAILURE THIS REPORTS. Until now "not in REQUIRED_MIGRATIONS" was the
    default state of a SQL file, not a decision about it, so a schema change
    applied through apply_sql.py changed production and no mechanism noticed:
    `migrate --check` iterates the manifest, `ledger_health()` divides by the
    manifest, and the live-schema comparison looked only at tables. The
    trg_fn_events_after_insert chain went down that path three times.

    ADVISORY, NOT BLOCKING, and the reason is specific. The blocking gate is
    `migrate.py`, which refuses to apply anything while the corpus is
    unclassified -- that is where an unclassified file can still be stopped
    before it reaches a database. By the time this runs the database is already
    whatever it is, so refusing to boot would convert a bookkeeping gap into an
    outage without protecting anything.

    SKIPS ON A DEPLOYED HOST, HONESTLY. /sql/ is gitignored, so a Railway
    container has no corpus to classify. That is reported as 'not evaluated',
    never as clean -- an absent denominator producing a confident pass is the
    exact mistake `ledger_health()` already made once and documents."""
    try:
        from app.core.deploy_state import classify_sql_corpus
        c = classify_sql_corpus()
    except Exception as exc:                                  # pragma: no cover
        return {"control": "sql_disposition", "ok": False, "severity": "advisory",
                "message": f"could not classify the SQL corpus: {exc}"}

    if not c.get("present"):
        # THE CORPUS IS ABSENT BY DESIGN, so say what WAS checked rather than
        # only what was not. sql/ is neither shipped nor version-controlled, so
        # file presence can never be evaluated in a deployed container. The
        # MANIFEST ships regardless, and half the invariant is a property of the
        # manifest alone -- a filename in both lists, a duplicate, an entry with
        # no reason. A guard whose only output is "not evaluated" is one people
        # stop reading, which is how a real finding gets missed later.
        if not c.get("manifest_ok", True):
            bits = []
            if c.get("both"):
                bits.append(f"{len(c['both'])} in BOTH manifests")
            if c.get("duplicates"):
                bits.append(f"{len(c['duplicates'])} duplicated")
            if c.get("unreasoned"):
                bits.append(f"{len(c['unreasoned'])} with no reason")
            return {"control": "sql_disposition", "ok": False,
                    "severity": "advisory",
                    "message": ("SQL manifest is inconsistent: "
                                + "; ".join(bits))}
        review = f", {len(c.get('needs_review') or [])} awaiting a human "                  f"disposition" if c.get("needs_review") else ""
        return {"control": "sql_disposition", "ok": True, "severity": "ok",
                "message": (f"SQL manifest consistent "
                            f"({c.get('declared')} governed, "
                            f"{c.get('out_of_band')} out-of-band){review}; "
                            f"file presence NOT EVALUATED here — sql/ is not "
                            f"shipped, and the blocking gate is migrate.py, "
                            f"which runs where sql/ exists")}
    if not c["ok"]:
        bits = []
        if c["unclassified"]:
            bits.append(f"{len(c['unclassified'])} unclassified "
                        f"({', '.join(c['unclassified'][:3])})")
        if c["both"]:
            bits.append(f"{len(c['both'])} in BOTH manifests")
        if c["missing_declared"]:
            bits.append(f"{len(c['missing_declared'])} declared but absent")
        if c["missing_out_of_band"]:
            bits.append(f"{len(c['missing_out_of_band'])} classified but absent")
        return {"control": "sql_disposition", "ok": False, "severity": "advisory",
                "message": ("SQL corpus is not fully classified: "
                            + "; ".join(bits)
                            + ". migrate.py will refuse to run.")}
    review = f", {len(c['needs_review'])} awaiting a human disposition" \
        if c["needs_review"] else ""
    return {"control": "sql_disposition", "ok": True, "severity": "ok",
            "message": (f"{c['on_disk']} SQL files classified "
                        f"({c['declared']} governed, {c['out_of_band']} "
                        f"out-of-band){review}")}


def _check_write_call_sites() -> Dict[str, Any]:
    """Is every module that writes directly to the database declared?

    R-03. `execute_sp` is guarded — role, customer scope, forbidden procedures.
    `get_connection()` is not, and 75 modules use it for DML. That is not a
    defect on its own: the scheduler, the agent bus and governance execution
    must be able to write, and every public module that writes directly carries
    its own control (HMAC link, OTP, provider signature, a status predicate
    inside the UPDATE). What was missing is that nobody could ENUMERATE the
    set, so it could not be reviewed and could grow unnoticed.

    ADVISORY, not blocking, and deliberately so for now. The detector is a
    static scan of `.execute(<sql>)`, so it will produce a false positive
    before it produces none — and a control that stops a deployed start on its
    first false positive is a control somebody switches off. It earns
    'blocking' the way the write-mode coverage check did: by being right for a
    while first.
    """
    try:
        from app.core import write_call_sites
        a = write_call_sites.audit()
    except Exception as exc:
        return {"control": "write_call_sites", "ok": True, "severity": "advisory",
                "message": f"could not audit direct-write sites: {exc}"}

    if a["undeclared"]:
        return {
            "control": "write_call_sites", "ok": False, "severity": "advisory",
            "message": (f"{len(a['undeclared'])} module(s) write to the "
                        f"database directly without being declared in "
                        f"app/core/write_call_sites.py: "
                        f"{', '.join(a['undeclared'][:5])}"
                        f"{' …' if len(a['undeclared']) > 5 else ''}. "
                        f"Declare them with the control that protects them.")}
    return {"control": "write_call_sites", "ok": True, "severity": "ok",
            "message": (f"{a['modules_writing_directly']} modules write "
                        f"directly ({a['call_sites']} sites); all declared, "
                        f"{a['public_surface']} on a public surface")}


def _check_db_privileges() -> Dict[str, Any]:
    """Does the application actually connect as a non-superuser?

    WHY THIS IS A CHECK AND NOT A COMMENT. `write_guard.FORBIDDEN_PROCEDURES`
    carried a prose note reading "the application connects as `postgres`, which
    both OWNS the function and is a SUPERUSER … UNTIL THE APPLICATION RUNS AS A
    NON-SUPERUSER ROLE, THIS GUARD IS THE ONLY EFFECTIVE CONTROL."

    That stopped being true when privilege separation shipped — production runs
    as `crm_app` — and nobody updated the comment. A stale claim about a
    defence layer is dangerous in both directions: it told the next engineer to
    ignore a control that now works, and the same comment left unchanged after
    a rollback would invite reliance on one that had stopped working.

    So the question is asked of the database instead of asserted in prose. The
    answer moves on its own when the deployment does.

    ADVISORY, not blocking: a superuser connection is a weakened defence in
    depth, not an open door — `guard_query` still refuses the forbidden paths
    in the application, and refusing to start over it could take down a working
    system during a credential rollback.
    """
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user, "
                            "(SELECT usesuper FROM pg_user "
                            "  WHERE usename = current_user)")
                user, is_super = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return {"control": "db_privileges", "ok": True, "severity": "advisory",
                "message": f"could not determine the database role: {exc}"}

    if is_super:
        return {
            "control": "db_privileges", "ok": False, "severity": "advisory",
            "message": (f"the application connects as {user!r}, a SUPERUSER. "
                        f"PostgreSQL superusers bypass privilege checks, so "
                        f"REVOKEs on the forbidden legacy procedures are inert "
                        f"and write_guard is the only effective control. Run "
                        f"as a least-privilege role (crm_app).")}
    return {"control": "db_privileges", "ok": True, "severity": "ok",
            "message": f"connected as {user!r} (not a superuser) — the "
                       f"database-privilege layer is live"}


CHECKS = (_check_calendar_feed, _check_api_auth,
          _check_public_read_corpus, _check_admin_token,
          _check_training_ack, _check_secret_strength,
          _check_configuration_integrity, _check_public_url,
          _check_email_call_sites, _check_capability_registry,
          _check_sql_disposition, _check_db_privileges,
          _check_write_call_sites)


def audit() -> Dict[str, Any]:
    """Run every check. Safe to call anywhere — it never raises."""
    results: List[Dict[str, Any]] = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:              # a broken check must not mask others
            results.append({"control": getattr(check, "__name__", "?"),
                            "ok": False, "severity": "advisory",
                            "message": f"check failed: {exc}"})
    blocking = [r for r in results if r["severity"] == "blocking" and not r["ok"]]
    advisory = [r for r in results if r["severity"] == "advisory" and not r["ok"]]
    return {"deployed": is_deployed(), "checks": results,
            "blocking": blocking, "advisory": advisory,
            "safe_to_start": not (blocking and is_deployed())}


def enforce() -> Dict[str, Any]:
    """Called at startup. Refuses to start a DEPLOYED environment that is
    missing a blocking control; logs everything either way.

    On a laptop this only ever prints, so local development keeps working
    exactly as before — production safety must not be bought with developer
    friction, or it gets disabled."""
    report = audit()
    where = "DEPLOYED" if report["deployed"] else "local"

    for r in report["checks"]:
        if r["ok"] and r["severity"] == "ok":
            logger.info(f"[release-guard] {r['control']}: {r['message']}")
        elif r["severity"] == "blocking" and not r["ok"]:
            logger.critical(f"[release-guard] BLOCKING · {r['control']}: "
                            f"{r['message']}")
        else:
            logger.warning(f"[release-guard] {r['control']}: {r['message']}")

    if report["blocking"] and report["deployed"]:
        detail = "; ".join(f"{r['control']}: {r['message']}"
                           for r in report["blocking"])
        raise UnsafeConfiguration(
            f"Refusing to start a deployed environment with an unsafe "
            f"configuration — {detail}")

    if report["blocking"]:
        logger.warning(
            f"[release-guard] {len(report['blocking'])} blocking issue(s) "
            f"tolerated because this is a {where} environment. They WILL stop "
            f"a deployed start.")
    return report
