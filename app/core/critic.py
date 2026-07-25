"""Governance critic — an independent verifier on the approval queue.

The multi-agent "critique before commit" pattern, done cheaply: when an action
is PROPOSED for human approval, a second agent cross-checks it against live CRM
state and the shared blackboard and attaches a short, structured critique to
the approval row. The routed executive then decides with BOTH the proposal and
an independent second opinion in front of them — in the queue API, the
one-click approval email, and the CEO briefing.

Design rules
------------
  • Deterministic only — SQL + blackboard reads, no LLM, no network. Every
    finding names its check and the number it saw, so the critique is
    explainable and auditable.
  • Advice only — the critic NEVER approves, rejects, or blocks. stance:
        endorse  every check passed
        caution  at least one warning (proceed, but look at this first)
        object   at least one failing check (probably pointless/harmful)
  • Best-effort everywhere: a critic failure never breaks propose(); a missing
    critique column (migration not applied) downgrades to log-only.

ADD A CHECK: append `def _check_<name>(ap) -> [finding]` to the generic list
or the per-action registry in _ACTION_CHECKS. A finding is
{"check": name, "verdict": "ok"|"warn"|"fail", "note": short-sentence}.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.core.database import get_connection

logger = logging.getLogger("critic")

REVIEWER = "critic"


def _f(check: str, verdict: str, note: str) -> Dict[str, str]:
    return {"check": check, "verdict": verdict, "note": note}


def _rows(sql: str, params=None) -> List[tuple]:
    """Query that tolerates missing tables/columns (returns [])."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    except Exception as exc:
        logger.debug(f"[critic] query skipped: {exc}")
        return []
    finally:
        conn.close()


def _one(sql: str, params=None):
    r = _rows(sql, params)
    return r[0][0] if r else None


# ============================================================================
# GENERIC CHECKS — run for every proposed action
# ============================================================================

def _check_duplicate_pending(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    n = _one(
        "SELECT count(*) FROM action_approvals "
        "WHERE action_type=%s AND status='pending' AND approval_uuid<>%s::uuid",
        (ap["action_type"], ap["approval_uuid"]))
    if n:
        return [_f("duplicate_pending", "warn",
                   f"{n} other pending approval(s) of the same type — "
                   f"decide together to avoid double execution")]
    return [_f("duplicate_pending", "ok", "no duplicate pending request")]


_ALIVE_SQL = {
    "lead": ("SELECT count(*) FROM leads "
             "WHERE lead_id=%s::uuid AND deleted_at IS NULL"),
    "account": ("SELECT count(*) FROM accounts "
                "WHERE account_id=%s::uuid AND COALESCE(is_deleted,false)=false"),
}


def _check_entity_alive(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    et, eid = ap.get("entity_type"), ap.get("entity_id")
    sql = _ALIVE_SQL.get(et or "")
    if not (sql and eid):
        return []
    n = _one(sql, (eid,))
    if n is None:          # query failed — skip rather than mis-report
        return []
    if not n:
        return [_f("entity_alive", "fail",
                   f"target {et} no longer exists (or is deleted) — action is moot")]
    return [_f("entity_alive", "ok", f"target {et} exists")]


_GENERIC_CHECKS = [_check_duplicate_pending, _check_entity_alive]


# ============================================================================
# PER-ACTION CHECKS
# ============================================================================

def _segment_accounts(params: Dict[str, Any]) -> List[str]:
    """Distinct account_ids a campaign segment resolves to (capped)."""
    try:
        from app.core import marketing
        recipients = marketing.resolve_segment(params.get("segment") or {}, limit=500)
        return sorted({r["account_id"] for r in recipients if r.get("account_id")})
    except Exception as exc:
        logger.debug(f"[critic] segment resolve failed: {exc}")
        return []


def _checks_campaign_winback(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    params = ap.get("params") or {}
    accounts = _segment_accounts(params)

    if not accounts:
        out.append(_f("segment_size", "fail",
                      "segment resolves to ZERO reachable recipients — "
                      "launching would do nothing"))
        return out
    out.append(_f("segment_size",
                  "warn" if len(accounts) > 200 else "ok",
                  f"segment reaches {len(accounts)} account(s)"
                  + (" — large blast, consider narrowing" if len(accounts) > 200 else "")))

    n = _one(
        "SELECT count(DISTINCT entity_id) FROM agent_blackboard "
        "WHERE entity_type='account' AND topic='complaint' "
        "  AND (expires_at IS NULL OR expires_at > now()) "
        "  AND entity_id = ANY(%s::uuid[])", (accounts,))
    if n:
        out.append(_f("open_complaints", "warn",
                      f"{n} target account(s) have an OPEN complaint — a discount "
                      f"on top of an unresolved grievance reads as tone-deaf; "
                      f"resolve or exclude them first"))
    else:
        out.append(_f("open_complaints", "ok", "no open complaints in the segment"))

    n = _one(
        "SELECT count(*) FROM account_intelligence "
        "WHERE overdue_invoices > 0 AND account_id = ANY(%s::uuid[])", (accounts,))
    if n:
        out.append(_f("overdue_ar", "warn",
                      f"{n} target account(s) carry OVERDUE invoices — offering a "
                      f"promotion while their balance is past due sends mixed "
                      f"signals (collect or reconcile first)"))
    else:
        out.append(_f("overdue_ar", "ok", "no overdue AR in the segment"))

    n = _one(
        "SELECT count(*) FROM marketing_campaigns "
        "WHERE name LIKE 'Win-back%%' AND created_at > now() - interval '7 days'")
    if n:
        out.append(_f("recent_winback", "warn",
                      f"{n} win-back campaign(s) already created in the last 7 days "
                      f"— same audience may be hit twice"))
    else:
        out.append(_f("recent_winback", "ok", "no win-back campaign in the last 7 days"))

    try:
        from app.core import agent_bus
        if agent_bus.AUTOSEND:
            out.append(_f("send_posture", "warn",
                          "AGENT_BUS_AUTOSEND=1 — approving LAUNCHES REAL EMAIL "
                          "to every eligible recipient (CASL-gated), not drafts"))
        else:
            out.append(_f("send_posture", "ok",
                          "drafts only (AUTOSEND off) — approval creates no outbound email"))
    except Exception:
        pass
    return out


def _checks_emit_dunning(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    n = int(_one("SELECT count(*) FROM invoices "
                 "WHERE status='overdue' AND deleted_at IS NULL") or 0)
    if not n:
        out.append(_f("overdue_exists", "fail",
                      "no overdue invoices right now — the dunning loop would "
                      "process nothing"))
        return out
    out.append(_f("overdue_exists", "ok", f"{n} overdue invoice(s) to work"))
    holds = _one(
        "SELECT count(DISTINCT entity_id) FROM agent_blackboard "
        "WHERE entity_type='account' AND topic='dunning_hold' "
        "  AND (expires_at IS NULL OR expires_at > now())")
    if holds:
        out.append(_f("dunning_holds", "warn",
                      f"{holds} account(s) have a Sales dunning_hold — the handler "
                      f"will skip them, so expect fewer reminders than invoices"))
    else:
        out.append(_f("dunning_holds", "ok", "no dunning holds on the blackboard"))
    return out


def _checks_emit_hot_leads(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    n = int(_one(
        "SELECT count(*) FROM leads WHERE deleted_at IS NULL AND score >= 70 "
        "AND status NOT IN ('converted','disqualified')") or 0)
    if not n:
        return [_f("hot_leads_exist", "fail",
                   "no hot leads (score ≥ 70) to schedule outreach for — "
                   "the loop would process nothing")]
    return [_f("hot_leads_exist", "ok", f"{n} hot lead(s) awaiting outreach")]


def _checks_tuning_adjust(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    """Sanity-check a self-tuning proposal: registered param, within hard
    bounds and band ordering, real change, evidence sample size, cooldown."""
    out: List[Dict[str, str]] = []
    from app.core import tuning
    params = ap.get("params") or {}
    param, value = params.get("param"), params.get("value")
    spec = tuning.TUNABLES.get(param or "")
    if not spec or value is None:
        return [_f("param_known", "fail",
                   f"unknown tunable {param!r} or missing value — refuse")]
    out.append(_f("param_known", "ok", f"{param} is a registered tunable"))

    try:
        tuning._validate(param, float(value))
        out.append(_f("within_bounds", "ok",
                      f"{value} within [{spec['min']}, {spec['max']}] and band order"))
    except ValueError as exc:
        out.append(_f("within_bounds", "fail", str(exc)))

    cur_v = tuning.current(param)
    if abs(float(value) - cur_v) < 1e-9:
        out.append(_f("real_change", "fail",
                      f"proposed value equals the current {cur_v} — a no-op"))
    else:
        out.append(_f("real_change", "ok", f"{cur_v} → {value}"))

    n = int((params.get("evidence") or {}).get("n") or 0)
    if n and n < tuning.MIN_SAMPLE:
        out.append(_f("evidence_sample", "warn",
                      f"only {n} accounts behind this evidence "
                      f"(< {tuning.MIN_SAMPLE}) — thin basis for retuning"))
    elif n:
        out.append(_f("evidence_sample", "ok", f"evidence from {n} account(s)"))

    if tuning._changed_recently(param, tuning.COOLDOWN_DAYS):
        out.append(_f("cooldown", "warn",
                      f"{param} already changed within the last "
                      f"{tuning.COOLDOWN_DAYS} days — let the previous change "
                      f"prove itself before another adjustment"))
    return out


def _checks_kb_publish(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    """Sanity-check a knowledge-article proposal: complete + useful content,
    no near-duplicate already answering the same question, traceable source."""
    out: List[Dict[str, str]] = []
    from app.core import knowledge
    p = ap.get("params") or {}
    title = str(p.get("title") or "").strip()
    answer = str(p.get("answer") or "").strip()
    if not title or not str(p.get("problem") or "").strip() or not answer:
        return [_f("complete", "fail",
                   "title, problem and answer are all required — refuse")]
    if len(answer) < knowledge.MIN_ANSWER_CHARS:
        out.append(_f("complete", "fail",
                      f"answer is {len(answer)} chars — too short to help "
                      f"the next customer"))
    else:
        out.append(_f("complete", "ok", "title/problem/answer present"))
    dup = knowledge.search(title, limit=1, min_rank=0.12)
    if dup:
        out.append(_f("near_duplicate", "warn",
                      f"an active article already covers this: "
                      f"\"{dup[0]['title'][:60]}\" — consider updating it instead"))
    else:
        out.append(_f("near_duplicate", "ok", "no similar active article"))
    if p.get("source_ref"):
        out.append(_f("traceable_source", "ok",
                      "mined from a recorded support thread"))
    else:
        out.append(_f("traceable_source", "warn",
                      "no source thread recorded — unverifiable provenance"))
    return out


def _checks_meeting_book(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    """Sanity-check a meeting-booking proposal: target reachable, requested
    slot free and within business hours (auto-slot is always fine)."""
    out: List[Dict[str, str]] = []
    from app.core import booking
    p = ap.get("params") or {}
    et = str(p.get("entity_type") or ("lead" if p.get("lead_id") else "account"))
    eid = str(p.get("entity_id") or p.get("lead_id") or p.get("account_id") or "")
    info = booking._entity_info(et, eid) if eid else None
    if not info:
        return [_f("target_exists", "fail",
                   f"{et} {eid or '(missing)'} not found — nobody to meet")]
    out.append(_f("target_exists", "ok", f"meeting with {info['display']}"))
    if not info.get("email"):
        out.append(_f("reachable", "warn",
                      "no email on file — the invite can only be delivered "
                      "manually (meeting still records internally)"))
    else:
        out.append(_f("reachable", "ok", "prospect has an email address"))
    start_iso = p.get("start")
    if start_iso:
        try:
            from datetime import datetime, timedelta
            start = datetime.fromisoformat(str(start_iso))
            if start.tzinfo is None:
                start = start.replace(tzinfo=booking.ET)
            dur = int(p.get("duration_min", booking.SLOT_MIN))
            if booking._conflicts(info["owner_id"], start,
                                  start + timedelta(minutes=dur)):
                out.append(_f("slot_free", "fail",
                              "requested slot conflicts with an existing "
                              "meeting on the owner's calendar"))
            else:
                out.append(_f("slot_free", "ok", "requested slot is free"))
            et_start = start.astimezone(booking.ET)
            if not (booking.BUSINESS_START <= et_start.hour < booking.BUSINESS_END
                    and et_start.weekday() < 5):
                out.append(_f("business_hours", "warn",
                              f"requested slot is outside ET business hours "
                              f"({et_start.strftime('%a %H:%M')})"))
        except (TypeError, ValueError):
            out.append(_f("slot_free", "fail",
                          f"unparseable start time {start_iso!r}"))
    else:
        out.append(_f("slot_free", "ok",
                      "auto-slot — engine picks the first free business-hour slot"))
    return out


def _checks_scoring_activate(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    """Sanity-check a model activation: candidate exists, evidence is thick
    enough, and it actually beats the base rate (and the incumbent)."""
    out: List[Dict[str, str]] = []
    from app.core import scoring
    p = ap.get("params") or {}
    version = p.get("version")
    row = scoring._model_row("WHERE version=%s", (int(version or 0),)) \
        if version else None
    if not row:
        return [_f("candidate_exists", "fail",
                   f"model v{version} not found — nothing to activate")]
    out.append(_f("candidate_exists", "ok", f"candidate v{version} on record"))
    m = row.get("metrics") or {}
    n = int(m.get("samples") or 0)
    if n < scoring.MIN_SAMPLES:
        out.append(_f("evidence_size", "fail",
                      f"trained on only {n} settled leads "
                      f"(< {scoring.MIN_SAMPLES}) — too thin to trust"))
    else:
        out.append(_f("evidence_size", "ok",
                      f"{n} settled leads ({m.get('positives')} converted)"))
    brier, base = m.get("brier"), m.get("baseline_brier")
    if brier is not None and base is not None:
        if float(brier) >= float(base):
            out.append(_f("beats_baseline", "fail",
                          f"holdout brier {brier} is not better than the "
                          f"predict-the-base-rate baseline {base} — the model "
                          f"adds nothing"))
        else:
            out.append(_f("beats_baseline", "ok",
                          f"brier {brier} vs baseline {base}"))
    cur = scoring.active_model()
    if cur and (cur.get("metrics") or {}).get("brier") is not None \
            and brier is not None \
            and float(brier) > float(cur["metrics"]["brier"]):
        out.append(_f("vs_incumbent", "warn",
                      f"candidate brier {brier} is worse than the active "
                      f"v{cur['version']}'s {cur['metrics']['brier']} — "
                      f"activating would be a downgrade"))
    return out


def _checks_sms_send(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    """Sanity-check an SMS proposal: usable number, sane length, real-send
    posture made explicit (an SMS cannot be unsent)."""
    out: List[Dict[str, str]] = []
    from app.core import telephony
    p = ap.get("params") or {}
    to = telephony.normalize_phone(str(p.get("to") or ""))
    if not to:
        return [_f("number_usable", "fail",
                   f"unusable phone number {p.get('to')!r}")]
    out.append(_f("number_usable", "ok", f"E.164 {to}"))
    body = str(p.get("body") or "")
    if not body.strip():
        out.append(_f("body_present", "fail", "empty message body"))
    elif len(body) > telephony.SMS_MAX_CHARS:
        out.append(_f("body_present", "warn",
                      f"{len(body)} chars (> {telephony.SMS_MAX_CHARS}) — "
                      f"will split into many segments; tighten it"))
    else:
        out.append(_f("body_present", "ok", f"{len(body)} chars"))
    if telephony.AUTOSEND:
        out.append(_f("send_posture", "warn",
                      "SMS_AUTOSEND=1 — approving SENDS A REAL SMS "
                      "immediately; it cannot be unsent"))
    else:
        out.append(_f("send_posture", "ok",
                      "drafts only (SMS_AUTOSEND off) — approval creates an "
                      "owner task, no outbound SMS"))
    return out


def _checks_data_fix(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    """Sanity-check a data-quality fix: the problem still exists live (a
    stale proposal must not run), the cap is honored, evidence attached."""
    out: List[Dict[str, str]] = []
    from app.core import data_quality
    p = ap.get("params") or {}
    detector = {"data.normalize_phones": "unnormalized_phones",
                "data.merge_contacts": "duplicate_contacts"}.get(
                    ap.get("action_type", ""))
    if detector:
        live = data_quality.DETECTORS[detector]().get("count", 0)
        if not live:
            out.append(_f("still_needed", "fail",
                          "nothing left to fix — the issue resolved since "
                          "this was proposed"))
        else:
            out.append(_f("still_needed", "ok", f"{live} item(s) still affected"))
    limit = int(p.get("limit", data_quality.FIX_LIMIT))
    if limit > data_quality.FIX_LIMIT:
        out.append(_f("bounded", "warn",
                      f"requested limit {limit} exceeds DQ_FIX_LIMIT "
                      f"({data_quality.FIX_LIMIT}) — the executor will cap it"))
    else:
        out.append(_f("bounded", "ok", f"capped at {limit} per run"))
    if (p.get("evidence") or {}).get("samples"):
        out.append(_f("evidence", "ok", "samples attached"))
    else:
        out.append(_f("evidence", "warn", "no samples attached"))
    return out


def _checks_data_erase(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    """Erasure is the one action with NO undo — surface that plainly, confirm the
    record still exists, and show what will actually be destroyed vs retained."""
    out: List[Dict[str, str]] = []
    p = ap.get("params") or {}
    entity, rid = str(p.get("entity") or ""), str(p.get("record_id") or "")
    out.append(_f("reversible", "warn",
                  "IRREVERSIBLE — this action has no undo; approve only against a "
                  "verified erasure request"))
    try:
        from app.core import lifecycle
        pv = lifecycle.preview(entity, rid)
    except Exception as exc:
        out.append(_f("still_needed", "fail", f"record not resolvable: {str(exc)[:80]}"))
        return out
    t = pv["totals"]
    out.append(_f("still_needed", "ok",
                  f"{t['rows_to_delete']} row(s) will be deleted, "
                  f"{t['rows_to_de_link']} de-linked, {t['rows_retained']} retained"))
    out.append(_f("evidence", "ok" if p.get("reason") else "warn",
                  p.get("reason") or "no erasure reason recorded"))
    return out


def _checks_identity_materialize(ap: Dict[str, Any]) -> List[Dict[str, str]]:
    """Sanity-check a duplicate merge: the link must STILL be confirmed and not
    already materialized (a stale proposal must not run), and the merge must be a
    human-confirmed one — never a bare detector guess."""
    out: List[Dict[str, str]] = []
    p = ap.get("params") or {}
    link_id = p.get("link_id")
    if not link_id:
        return [_f("still_needed", "fail", "no link_id in the proposal")]
    try:
        from app.core import identity_links
        lk = identity_links._link(str(link_id))
    except Exception as exc:
        return [_f("still_needed", "fail", f"link no longer resolvable: {str(exc)[:80]}")]
    if lk["status"] != "confirmed":
        out.append(_f("still_needed", "fail",
                      f"link is '{lk['status']}' — only a confirmed link may merge"))
    elif lk.get("materialized_at"):
        out.append(_f("still_needed", "fail", "already materialized"))
    else:
        out.append(_f("still_needed", "ok", "link is confirmed and not yet merged"))
    conf = float(lk.get("confidence") or 0)
    out.append(_f("evidence", "ok" if conf >= 0.90 else "warn",
                  f"match confidence {conf:.2f}"
                  + ("" if conf >= 0.90 else " — below 0.90, review the pair closely")))
    out.append(_f("reversible", "ok",
                  "every moved row is recorded; the merge is undoable from the audit"))
    return out


_ACTION_CHECKS: Dict[str, Callable[[Dict[str, Any]], List[Dict[str, str]]]] = {
    "campaign.winback":          _checks_campaign_winback,
    "supervisor.emit_dunning":   _checks_emit_dunning,
    "supervisor.emit_hot_leads": _checks_emit_hot_leads,
    "tuning.adjust":             _checks_tuning_adjust,
    "kb.publish":                _checks_kb_publish,
    "meeting.book":              _checks_meeting_book,
    "scoring.activate":          _checks_scoring_activate,
    "sms.send":                  _checks_sms_send,
    "data.normalize_phones":     _checks_data_fix,
    "data.merge_contacts":       _checks_data_fix,
    "identity.materialize_link": _checks_identity_materialize,
    "data.erase_record":         _checks_data_erase,
}


# ============================================================================
# REVIEW
# ============================================================================

def _stance(findings: List[Dict[str, str]]) -> str:
    verdicts = {f["verdict"] for f in findings}
    if "fail" in verdicts:
        return "object"
    if "warn" in verdicts:
        return "caution"
    return "endorse"


def _summary(stance: str, findings: List[Dict[str, str]]) -> str:
    flagged = [f for f in findings if f["verdict"] in ("fail", "warn")]
    if not flagged:
        return f"no objections — {len(findings)} check(s) passed"
    lead = next((f for f in flagged if f["verdict"] == "fail"), flagged[0])
    more = len(flagged) - 1
    return lead["note"] + (f" (+{more} more finding(s))" if more else "")


def review(approval_uuid: str, action_type: Optional[str] = None,
           params: Optional[Dict[str, Any]] = None,
           entity_type: Optional[str] = None,
           entity_id: Optional[str] = None) -> Dict[str, Any]:
    """Critique one approval. Loads missing fields from the row; persists the
    critique onto it (best-effort) and returns it either way."""
    if action_type is None:
        r = _rows("SELECT action_type, params, entity_type, entity_id::text "
                  "FROM action_approvals WHERE approval_uuid=%s::uuid",
                  (approval_uuid,))
        if not r:
            return {"stance": "caution", "summary": "approval not found",
                    "findings": [], "reviewer": REVIEWER}
        action_type, params, entity_type, entity_id = r[0]
        if isinstance(params, str):
            params = json.loads(params or "{}")

    ap = {"approval_uuid": approval_uuid, "action_type": action_type,
          "params": params or {}, "entity_type": entity_type,
          "entity_id": entity_id}

    findings: List[Dict[str, str]] = []
    for check in _GENERIC_CHECKS:
        try:
            findings.extend(check(ap))
        except Exception as exc:
            logger.warning(f"[critic] {check.__name__} failed: {exc}")
    action_checks = _ACTION_CHECKS.get(action_type)
    if action_checks:
        try:
            findings.extend(action_checks(ap))
        except Exception as exc:
            logger.warning(f"[critic] action checks for {action_type} failed: {exc}")

    stance = _stance(findings)
    critique = {"stance": stance, "summary": _summary(stance, findings),
                "findings": findings, "reviewer": REVIEWER,
                "checked_at": datetime.now(timezone.utc).isoformat()}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE action_approvals SET critique=%s::jsonb, critiqued_at=now() "
                "WHERE approval_uuid=%s::uuid",
                (json.dumps(critique), approval_uuid))
        conn.commit()
    except Exception as exc:
        logger.warning(f"[critic] critique not persisted (migration applied?): {exc}")
    finally:
        conn.close()

    logger.info(f"[critic] {approval_uuid[:8]} {action_type}: {stance} — "
                f"{critique['summary']}")
    return critique


def review_pending() -> Dict[str, Any]:
    """Backfill: critique every live pending approval that has none yet
    (rows proposed before the critic shipped, or while it errored)."""
    rows = _rows(
        "SELECT approval_uuid::text FROM action_approvals "
        "WHERE status='pending' AND critique IS NULL "
        "  AND (expires_at IS NULL OR expires_at > now())")
    done = []
    for (aid,) in rows:
        try:
            c = review(aid)
            done.append({"approval_uuid": aid, "stance": c["stance"]})
        except Exception as exc:
            logger.warning(f"[critic] backfill review of {aid[:8]} failed: {exc}")
    return {"reviewed": len(done), "items": done}
