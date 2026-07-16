"""Knowledge loop — resolved cases become governed, reusable answers
(advanced improvement #2, the service pillar's missing piece).

    mine     nightly (gated KB_DRAFT_ENABLED): pair an inbound
             support/complaint email with the human's outbound resolution on
             the same account/lead within 7 days
    draft    LLM turns the pair into {title, problem, answer, keywords} —
             drafting failure means SKIP, never junk
    propose  governance.propose('kb.publish', …) — the critic checks it
             (required fields, near-duplicates, source thread), an executive
             approves with one click
    publish  approval executes A2A `kb.publish` → knowledge_articles row;
             governance undo retires it
    serve    the autonomous auto-reply retrieves approved articles by
             Postgres full-text search (deterministic, no embeddings) and
             grounds its reply in them; `uses` counts what earns its keep

The KB grows through three governed inflows (all land in the SAME approval
queue; nothing publishes itself):

    email miner   _resolved_threads: inbound support email + the human's
                  outbound resolution (the original miner)
    voice miner   _resolved_calls: support-call transcripts logged by
                  voice_support (caller lines = the problem, agent lines =
                  the resolution) — every resolved call can feed the KB
    gap miner     gap_pass: questions the KB could NOT answer (logged by
                  rag_block per public channel into kb_gaps, PII-masked,
                  deduped by salient terms) — the owning module agent is
                  asked for the GENERAL answer over A2A (read-only), the
                  LLM generalizes it, and a kb.publish is proposed. Demand-
                  driven: the KB grows exactly where callers show demand.

CONFIG (env)
  KB_RAG_ENABLED     1   retrieval into the auto-reply (read-only kill switch)
  KB_DRAFT_ENABLED   0   ALL nightly mining on/off (LLM cost — opt-in): the
                         email miner, the voice-transcript miner AND the
                         gap pass share this one switch
  KB_DRAFT_CAP       3   max articles drafted per mining pass
  KB_GAP_CAP         3   max gap articles proposed per pass
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from app.core.database import get_connection

logger = logging.getLogger("knowledge")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


RAG_ENABLED = _flag("KB_RAG_ENABLED", "1")
# One switch for every miner (email threads, voice transcripts, gap pass) —
# they all cost LLM tokens and all end in the same governed proposal queue.
DRAFT_ENABLED = _flag("KB_DRAFT_ENABLED", "0")
DRAFT_CAP = int(os.getenv("KB_DRAFT_CAP", "3"))
GAP_CAP = int(os.getenv("KB_GAP_CAP", "3"))

MIN_RANK = 0.03          # FTS rank below this = not actually relevant
RAG_MIN_RANK = 0.01      # OR-mode noise floor only — precision comes from the
                         # matched-term count (ts_rank dilutes with query
                         # length, so an absolute floor can't separate a
                         # 5-of-8-terms hit from a 1-of-4 coincidence)
RAG_MIN_TERMS = 2        # distinct query terms an article must match
                         # (1 when the query itself has ≤2 salient terms)
MIN_ANSWER_CHARS = 40    # publish refuses answers shorter than this

_STOPWORDS = {"the", "and", "for", "with", "your", "from", "this", "that",
              "have", "has", "are", "was", "can", "cant", "cannot", "you",
              "our", "not", "how", "what", "why", "need", "help", "please",
              "hello", "thanks", "hi", "dear", "regards"}


# ============================================================================
# RETRIEVAL — deterministic full-text search
# ============================================================================

def search(query: str, limit: int = 3, min_rank: float = MIN_RANK,
           any_terms: bool = False) -> List[Dict[str, Any]]:
    """Ranked active articles for a query ([] on any failure).

    Default = AND semantics (plainto_tsquery — precise, used for API search
    and duplicate detection). any_terms=True = OR semantics over the query's
    salient words — what free-text emails need, where no article contains
    EVERY word of the message; the higher rank floor keeps single-word
    coincidences out."""
    q = (query or "").strip()
    if not q:
        return []
    if any_terms:
        import re as _re
        terms = [t for t in dict.fromkeys(_re.findall(r"[a-z0-9]{3,}", q.lower()))
                 if t not in _STOPWORDS][:10]
        if not terms:
            return []
        tsq_sql, tsq_arg = "to_tsquery('english', %s)", " | ".join(terms)
    else:
        tsq_sql, tsq_arg = "plainto_tsquery('english', %s)", q
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT article_uuid::text, title, problem, answer, keywords,
                           ts_rank(search_tsv, {tsq_sql}) AS rank
                    FROM knowledge_articles
                    WHERE status='active'
                      AND search_tsv @@ {tsq_sql}
                    ORDER BY rank DESC LIMIT %s""",
                (tsq_arg, tsq_arg, int(limit)))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return [r for r in rows if float(r["rank"]) >= min_rank]
    except Exception as exc:
        logger.debug(f"[knowledge] search skipped: {exc}")
        return []
    finally:
        conn.close()


def _mark_used(article_uuids: List[str]) -> None:
    if not article_uuids:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE knowledge_articles "
                "SET uses = uses + 1, last_used_at = now() "
                "WHERE article_uuid = ANY(%s::uuid[])", (article_uuids,))
        conn.commit()
    except Exception as exc:
        logger.debug(f"[knowledge] usage bump skipped: {exc}")
    finally:
        conn.close()


def _salient_terms(q: str) -> List[str]:
    import re as _re
    return [t for t in dict.fromkeys(_re.findall(r"[a-z0-9]{3,}", q.lower()))
            if t not in _STOPWORDS][:10]


def _matched_terms(terms: List[str], hit: Dict[str, Any]) -> int:
    """How many distinct query terms this article actually matches (prefix-
    tolerant both ways: 'pay'~'payment', 'cards'~'card')."""
    import re as _re
    text = " ".join([hit.get("title") or "", hit.get("problem") or "",
                     " ".join(hit.get("keywords") or [])]).lower()
    words = set(_re.findall(r"[a-z0-9]{3,}", text))
    n = 0
    for t in terms:
        if any(w.startswith(t) or t.startswith(w) for w in words):
            n += 1
    return n


def rag_block(subject: str, body: str,
              gap_channel: Optional[str] = None) -> str:
    """Prompt block with the best-matching approved answers ('' when none or
    the kill switch is off). Counts the retrieval as a use.

    Pass a REAL subject only (email). Fixed channel labels ('support call',
    'sms inquiry') must be '' — their words pollute term matching, letting a
    generic channel article outrank the actual answer.

    gap_channel: when set and NO article matches, the question is recorded
    as a KB gap for that channel (PII-masked, deduped) — the demand signal
    the nightly gap_pass turns into governed article proposals."""
    if not RAG_ENABLED:
        return ""
    query = f"{subject or ''} {str(body or '')[:200]}"
    terms = _salient_terms(query)
    need = 1 if len(terms) <= 2 else RAG_MIN_TERMS
    hits = [h for h in search(query, limit=4, min_rank=RAG_MIN_RANK,
                              any_terms=True)
            if _matched_terms(terms, h) >= need]
    # Precision order: how many of the ASKER's terms an article matches beats
    # raw ts_rank (which rewards repeated generic words like 'call'/'text').
    hits = sorted(hits, key=lambda h: (_matched_terms(terms, h),
                                       float(h["rank"])), reverse=True)[:2]
    if not hits:
        if gap_channel:
            log_gap(gap_channel, body)
        return ""
    _mark_used([h["article_uuid"] for h in hits])
    lines = ["[APPROVED KNOWLEDGE BASE]"]
    for h in hits:
        lines.append(f"Q: {h['title']}\nA: {h['answer'][:600]}")
    return "\n".join(lines)


# ============================================================================
# PUBLISH / RETIRE (the only writers)
# ============================================================================

def publish(article: Dict[str, Any], created_by: str = "governance") -> Dict[str, Any]:
    """Insert an approved article. Validates required fields; refuses a
    duplicate source_ref (the miner's idempotency anchor)."""
    title = str(article.get("title") or "").strip()
    problem = str(article.get("problem") or "").strip()
    answer = str(article.get("answer") or "").strip()
    if not (title and problem and answer):
        raise ValueError("title, problem and answer are all required")
    if len(answer) < MIN_ANSWER_CHARS:
        raise ValueError(f"answer too short (<{MIN_ANSWER_CHARS} chars) to be useful")
    keywords = [str(k)[:40] for k in (article.get("keywords") or [])][:8]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO knowledge_articles
                     (title, problem, answer, keywords, source, source_ref,
                      created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (source_ref) WHERE source_ref IS NOT NULL
                   DO NOTHING
                   RETURNING article_uuid::text""",
                (title[:180], problem[:2000], answer[:4000], keywords,
                 article.get("source", "agent"), article.get("source_ref"),
                 created_by))
            r = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not r:
        return {"ok": False, "error": "an article from this source thread "
                                      "already exists"}
    logger.info(f"[knowledge] published '{title[:60]}' ({r[0][:8]})")
    return {"ok": True, "article_uuid": r[0], "title": title}


def retire(article_uuid: str, reason: str = "retired") -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE knowledge_articles SET status='retired', updated_at=now() "
                "WHERE article_uuid=%s::uuid AND status='active' "
                "RETURNING title", (article_uuid,))
            r = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not r:
        return {"ok": False, "error": "not found or already retired"}
    logger.info(f"[knowledge] retired '{r[0][:60]}' ({reason})")
    return {"ok": True, "article_uuid": article_uuid, "status": "retired"}


# ============================================================================
# MINING — resolved support threads → governed article proposals
# ============================================================================

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _draft_llm(inbound_subject: str, inbound_text: str,
               resolution_text: str) -> Optional[Dict[str, Any]]:
    """LLM: support thread → article dict. None on any failure (skip, no junk).
    NB parse_ai_json does not parse plain fenced JSON — use the regex+loads
    pattern (same as marketing)."""
    try:
        from app.core import privacy
        from app.core.graph_utils import _get_llm
        # PII minimization: the thread text is masked BEFORE it reaches the
        # LLM (the prompt additionally forbids personal data in the OUTPUT).
        inbound_subject = privacy.mask(inbound_subject)
        inbound_text = privacy.mask(inbound_text)
        resolution_text = privacy.mask(resolution_text)
        llm = _get_llm()
        resp = llm.invoke([
            {"role": "system", "content":
                "You distill resolved customer-support threads into reusable "
                "knowledge-base articles for Conscestra CRM. Write for the NEXT "
                "customer with the same problem. Never include personal data "
                "(names, emails, amounts, account details) — generalize."},
            {"role": "user", "content":
                f"Customer message (subject: {inbound_subject}):\n"
                f"{inbound_text[:800]}\n\n"
                f"How our team resolved it:\n{resolution_text[:800]}\n\n"
                'Return ONLY JSON: {"title": "<question as the customer would '
                'ask it, <=100 chars>", "problem": "<1-2 sentence generalized '
                'problem>", "answer": "<the reusable answer, 2-5 sentences>", '
                '"keywords": ["<3-6 search terms>"]}'},
        ])
        text = resp.content if hasattr(resp, "content") else str(resp)
        m = _JSON_RE.search(text)
        art = json.loads(m.group(0)) if m else None
        if not art or len(str(art.get("answer") or "")) < MIN_ANSWER_CHARS:
            return None
        return art
    except Exception as exc:
        logger.warning(f"[knowledge] LLM draft failed: {exc}")
        return None


def revise_article(article: Dict[str, Any],
                   findings: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """Critic→revise loop: one bounded LLM revision of a rejected article,
    guided by the critic's findings. None on any failure — the original
    objection then stands (never publish junk to fix junk)."""
    problems = "; ".join(f"{f.get('check')}: {f.get('note')}"
                         for f in (findings or [])
                         if f.get("verdict") in ("fail", "warn")) or "unspecified"
    try:
        from app.core import privacy
        from app.core.graph_utils import _get_llm
        resp = _get_llm().invoke([
            {"role": "system", "content":
                "You revise rejected knowledge-base articles for Conscestra "
                "CRM. Fix EXACTLY the reviewer's objections; keep everything "
                "that was fine. Generalize; never include personal data."},
            {"role": "user", "content":
                f"Draft article:\n{privacy.mask(json.dumps({k: article.get(k) for k in ('title', 'problem', 'answer', 'keywords')}))}\n\n"
                f"Reviewer objections:\n{problems}\n\n"
                'Return ONLY the corrected JSON: {"title": "...", "problem": '
                '"...", "answer": "<at least 3 helpful sentences>", '
                '"keywords": ["..."]}'},
        ])
        text = resp.content if hasattr(resp, "content") else str(resp)
        m = _JSON_RE.search(text)
        art = json.loads(m.group(0)) if m else None
        if not art or len(str(art.get("answer") or "")) < MIN_ANSWER_CHARS:
            return None
        # provenance fields survive the revision
        for k in ("source_ref", "source"):
            if article.get(k) is not None:
                art[k] = article[k]
        return art
    except Exception as exc:
        logger.warning(f"[knowledge] revise failed: {exc}")
        return None


def _resolved_threads(cap: int) -> List[Dict[str, Any]]:
    """Inbound support/complaint emails with a later outbound human touch on
    the same account/lead within 7 days, not yet mined or proposed."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.activity_id::text, a.subject, a.description,
                          o.subject AS resolution_subject,
                          COALESCE(o.description,'') AS resolution
                   FROM activities a
                   JOIN LATERAL (
                       SELECT o.subject, o.description
                       FROM activities o
                       WHERE ((a.account_id IS NOT NULL AND o.account_id = a.account_id)
                           OR (a.lead_id IS NOT NULL AND o.lead_id = a.lead_id))
                         AND o.direction = 'outbound'
                         AND o.type IN ('email','call')
                         AND o.created_at > a.created_at
                         AND o.created_at < a.created_at + interval '7 days'
                       ORDER BY o.created_at LIMIT 1
                   ) o ON true
                   WHERE a.direction = 'inbound' AND a.type = 'email'
                     AND (a.description LIKE '%%intent: support_request%%'
                       OR a.description LIKE '%%intent: complaint%%')
                     AND a.created_at > now() - interval '30 days'
                     AND NOT EXISTS (SELECT 1 FROM knowledge_articles k
                                     WHERE k.source_ref = a.activity_id::text)
                     AND NOT EXISTS (SELECT 1 FROM action_approvals ap
                                     WHERE ap.action_type = 'kb.publish'
                                       AND ap.params->>'source_ref' = a.activity_id::text
                                       AND ap.status IN ('pending','approved','executed'))
                   ORDER BY a.created_at DESC
                   LIMIT %s""", (int(cap),))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.debug(f"[knowledge] thread mining skipped: {exc}")
        return []
    finally:
        conn.close()


def _resolved_calls(cap: int) -> List[Dict[str, Any]]:
    """Support-call transcripts (logged by voice_support as one inbound
    'voice' activity) not yet mined. The transcript carries BOTH sides:
    'caller:' lines are the problem, 'agent:' lines are the resolution —
    so one activity is a complete minable thread."""
    if cap <= 0:
        return []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.activity_id::text, a.subject, a.description
                   FROM activities a
                   WHERE a.channel = 'voice' AND a.direction = 'inbound'
                     AND a.description LIKE 'Tier: %%'
                     AND length(COALESCE(a.description,'')) > 200
                     AND a.created_at > now() - interval '30 days'
                     AND NOT EXISTS (SELECT 1 FROM knowledge_articles k
                                     WHERE k.source_ref = a.activity_id::text)
                     AND NOT EXISTS (SELECT 1 FROM action_approvals ap
                                     WHERE ap.action_type = 'kb.publish'
                                       AND ap.params->>'source_ref' = a.activity_id::text
                                       AND ap.status IN ('pending','approved','executed'))
                   ORDER BY a.created_at DESC
                   LIMIT %s""", (int(cap),))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.debug(f"[knowledge] call mining skipped: {exc}")
        return []
    finally:
        conn.close()


def _split_transcript(description: str) -> tuple:
    """(caller_text, agent_text) from a voice_support transcript. Lines look
    like 'caller: …' / 'agent: …' after the 'Tier: …' header."""
    caller, agent = [], []
    for line in (description or "").splitlines():
        line = line.strip()
        if line.startswith("caller:"):
            caller.append(line[len("caller:"):].strip())
        elif line.startswith("agent:"):
            agent.append(line[len("agent:"):].strip())
    return " ".join(caller), " ".join(agent)


def draft_pass(force: bool = False) -> Dict[str, Any]:
    """Mine resolved support threads (email) AND support-call transcripts
    (voice) → LLM-draft articles → PROPOSE each via governance (kb.publish).
    Publishes nothing itself; the two sources share KB_DRAFT_CAP, email
    first. force=True runs even when KB_DRAFT_ENABLED=0 (endpoint/testing)."""
    if not DRAFT_ENABLED and not force:
        return {"enabled": False, "skipped": True}
    from app.core import governance

    threads = _resolved_threads(DRAFT_CAP)
    calls = _resolved_calls(DRAFT_CAP - len(threads))
    proposed, skipped = [], 0

    def _propose(art: Dict[str, Any], source_ref: str) -> None:
        art["source_ref"] = source_ref
        art["source"] = "agent"
        aid = governance.propose("kb.publish", "knowledge", art,
                                 confidence=0.6, severity="low")
        proposed.append({"approval_uuid": aid, "title": art.get("title"),
                         "source_ref": source_ref})
        logger.info(f"[knowledge] proposed article '{str(art.get('title'))[:60]}' "
                    f"({aid[:8]})")

    for t in threads:
        subject = re.sub(r"^Inbound:\s*", "", t["subject"] or "")
        art = _draft_llm(subject, t["description"] or "", t["resolution"])
        if art:
            _propose(art, t["activity_id"])
        else:
            skipped += 1
    for c in calls:
        caller_text, agent_text = _split_transcript(c["description"])
        if not (caller_text and agent_text):
            skipped += 1
            continue
        art = _draft_llm("support call", caller_text, agent_text)
        if art:
            _propose(art, c["activity_id"])
        else:
            skipped += 1
    return {"enabled": DRAFT_ENABLED, "threads": len(threads),
            "calls": len(calls), "proposed": proposed,
            "draft_failures": skipped}


# ============================================================================
# GAP MINING — unanswered questions → agent-grounded article proposals
# ============================================================================

def log_gap(channel: str, question: str) -> None:
    """Record a question the KB could not answer (called from rag_block).
    PII-masked, deduped by sorted salient terms (repeat asks bump `hits`).
    Best-effort: missing table = silent skip, same as the sdr_sessions
    pattern — losing telemetry must never break a live reply."""
    q = str(question or "").strip()[:500]
    # Dedup key: salient terms, crudely de-pluralized ('orders'→'order') so
    # rephrasings of the same ask land on one row and bump its hit count.
    terms = sorted({t[:-1] if len(t) > 3 and t.endswith("s") else t
                    for t in _salient_terms(q)})
    if not q or not terms:
        return
    try:
        from app.core import privacy
        q = privacy.mask(q)
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE kb_gaps SET hits = hits + 1, updated_at = now()
                       WHERE status = 'open' AND terms = %s::text[]
                       RETURNING gap_id""", (terms,))
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO kb_gaps (channel, question, terms) "
                        "VALUES (%s, %s, %s)", (channel[:20], q, terms))
            conn.commit()
        finally:
            conn.close()
        logger.info(f"[knowledge] KB gap logged ({channel}): {q[:80]}")
    except Exception as exc:
        logger.debug(f"[knowledge] gap log skipped (table missing?): {exc}")


def _open_gaps(cap: int) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT gap_id, channel, question, hits
                   FROM kb_gaps WHERE status = 'open'
                   ORDER BY hits DESC, created_at LIMIT %s""", (int(cap),))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        logger.debug(f"[knowledge] gap read skipped (table missing?): {exc}")
        return []
    finally:
        conn.close()


def _set_gap(gap_id: int, status: str, proposal_uuid: Optional[str] = None) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE kb_gaps SET status=%s, proposal_uuid=%s::uuid, "
                "updated_at=now() WHERE gap_id=%s",
                (status, proposal_uuid, gap_id))
        conn.commit()
    finally:
        conn.close()


async def _agent_answer(question: str) -> Optional[str]:
    """Ask the OWNING module agent for the answer, over the typed A2A layer.
    Routed by the intent router (LLM, keyword fallback); runs on a read-only
    channel so a question-shaped write can never execute from here."""
    from app.core.a2a import A2ARequest, dispatch, query_intent_for_endpoint
    from app.core.intent_router import aroute
    from app.core.write_guard import set_readonly_channel

    set_readonly_channel("kb_gap")
    endpoint = (await aroute(question)).endpoint
    intent = query_intent_for_endpoint(endpoint)
    if not intent:
        return None
    res = await dispatch(A2ARequest(intent=intent, from_agent="knowledge",
                                    params={"message": question}))
    out = (res.output or "").strip()
    if not res.ok or not out or out.lstrip().startswith("### ERROR"):
        return None
    return out


def _web_answer_for_gap(question: str) -> Optional[str]:
    """Cited web material for a gap no module agent could answer. None when
    search is disabled/unreachable or nothing citable came back."""
    try:
        from app.core.web_tools import web_answer
        text = (web_answer(question) or "").strip()
        # web_answer degrades to apology/result-list strings — only material
        # that actually cites sources is usable as article grounding.
        if "Sources:" not in text and "http" not in text:
            return None
        return text
    except Exception as exc:
        logger.debug(f"[knowledge] web fallback skipped: {exc}")
        return None


def _draft_gap_llm(question: str, agent_answer: str,
                   channel: str) -> Optional[Dict[str, Any]]:
    """LLM: unanswered question + the module agent's answer → generalized
    article, or None. The agent's answer is often record-specific — the
    prompt demands a reusable generalization and allows an explicit SKIP,
    because 'no article' beats a data-flavored one."""
    try:
        from app.core import privacy
        from app.core.graph_utils import _get_llm
        resp = _get_llm().invoke([
            {"role": "system", "content":
                "You write knowledge-base articles for Conscestra CRM that "
                "will be served VERBATIM to anonymous customers. A customer "
                "asked a question our KB could not answer; a internal module "
                "agent has provided material. Write the general, reusable "
                "answer for ANY future customer. Never include personal data, "
                "record-specific names, amounts, dates or counts — those "
                "change daily and belong to other customers. If the material "
                "is only customer-specific data with no generalizable policy "
                "or process behind it, reply with exactly SKIP."},
            {"role": "user", "content":
                f"Customer question (via {channel}):\n"
                f"{privacy.mask(question)[:400]}\n\n"
                f"Module agent material:\n{privacy.mask(agent_answer)[:1200]}\n\n"
                'Return ONLY JSON: {"title": "<question as the customer would '
                'ask it, <=100 chars>", "problem": "<1-2 sentence generalized '
                'problem>", "answer": "<the reusable answer, 2-5 sentences>", '
                '"keywords": ["<3-6 search terms>"]} — or exactly SKIP.'},
        ])
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        if text.upper().startswith("SKIP"):
            return None
        m = _JSON_RE.search(text)
        art = json.loads(m.group(0)) if m else None
        if not art or len(str(art.get("answer") or "")) < MIN_ANSWER_CHARS:
            return None
        return art
    except Exception as exc:
        logger.warning(f"[knowledge] gap draft failed: {exc}")
        return None


async def gap_pass(force: bool = False) -> Dict[str, Any]:
    """Top open gaps → covered-check → owning agent (A2A, read-only) →
    LLM-generalize → PROPOSE kb.publish. Publishes nothing itself; a gap
    whose draft fails stays open for the next pass. Shares the one
    KB_DRAFT_ENABLED switch with the thread/transcript miners."""
    if not DRAFT_ENABLED and not force:
        return {"enabled": False, "skipped": True}
    import asyncio as _aio

    from app.core import governance

    gaps = _open_gaps(GAP_CAP)
    proposed, covered, skipped = [], 0, 0
    for g in gaps:
        # A seed/mined article may have covered this gap since it was logged.
        if rag_block("", g["question"]):
            _set_gap(g["gap_id"], "covered")
            covered += 1
            continue
        answer = await _agent_answer(g["question"])
        if not answer:
            # Internet Agent fallback: when no module agent owns the answer
            # (shipping carriers, regulations, general product facts), ask
            # the live web — cited, and still only a governed PROPOSAL.
            answer = await _aio.to_thread(_web_answer_for_gap, g["question"])
        if not answer:
            skipped += 1
            continue
        art = await _aio.to_thread(_draft_gap_llm, g["question"], answer,
                                   g["channel"])
        if not art:
            skipped += 1
            continue
        art["source"] = "gap"
        art["source_ref"] = f"gap:{g['gap_id']}"
        aid = await _aio.to_thread(
            governance.propose, "kb.publish", "knowledge", art,
            confidence=0.6, severity="low")
        _set_gap(g["gap_id"], "proposed", aid)
        proposed.append({"approval_uuid": aid, "gap_id": g["gap_id"],
                         "title": art.get("title"), "hits": g["hits"]})
        logger.info(f"[knowledge] gap {g['gap_id']} (hits={g['hits']}) → "
                    f"proposed '{str(art.get('title'))[:60]}' ({aid[:8]})")
    return {"enabled": DRAFT_ENABLED, "gaps": len(gaps), "proposed": proposed,
            "covered": covered, "skipped": skipped}


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["knowledge"])


@router.get("/knowledge/list")
def knowledge_list(status: str = "active", limit: int = 50):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT article_uuid::text, title, status, source, uses,
                          last_used_at, created_by, created_at
                   FROM knowledge_articles WHERE status=%s
                   ORDER BY uses DESC, created_at DESC LIMIT %s""",
                (status, int(limit)))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            for r in rows:
                for k in ("last_used_at", "created_at"):
                    r[k] = r[k].isoformat() if r[k] else None
    finally:
        conn.close()
    return {"status": status, "count": len(rows), "articles": rows,
            "rag_enabled": RAG_ENABLED, "draft_enabled": DRAFT_ENABLED}


@router.get("/knowledge/search")
def knowledge_search(q: str, limit: int = 3):
    return {"query": q, "hits": search(q, limit)}


@router.post("/knowledge")
def knowledge_create(body: Dict[str, Any]):
    """Direct admin publish (bypasses governance — human-authored articles)."""
    try:
        return publish(body or {}, created_by=str(body.get("created_by", "admin")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/knowledge/{article_uuid}/retire")
def knowledge_retire(article_uuid: str, reason: str = "manual"):
    return retire(article_uuid, reason)


@router.post("/knowledge/draft-pass")
async def knowledge_draft_pass():
    """Run the mining→draft→propose pass now (forced; nightly job self-gates)."""
    import asyncio
    return await asyncio.to_thread(draft_pass, True)


@router.get("/knowledge/gaps")
def knowledge_gaps(status: str = "open", limit: int = 50):
    """The demand signal: what customers asked that the KB couldn't answer."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT gap_id, channel, question, hits, status,
                          proposal_uuid::text, created_at, updated_at
                   FROM kb_gaps WHERE status=%s
                   ORDER BY hits DESC, created_at LIMIT %s""",
                (status, int(limit)))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            for r in rows:
                for k in ("created_at", "updated_at"):
                    r[k] = r[k].isoformat() if r[k] else None
    except Exception as exc:
        return {"status": status, "count": 0, "gaps": [],
                "note": f"kb_gaps unavailable ({exc}) — apply sql/kb_gaps.sql"}
    finally:
        conn.close()
    return {"status": status, "count": len(rows), "gaps": rows,
            "draft_enabled": DRAFT_ENABLED}


@router.post("/knowledge/gaps/{gap_id}/dismiss")
def knowledge_gap_dismiss(gap_id: int):
    """Human triage: this gap should never become an article."""
    _set_gap(gap_id, "dismissed")
    return {"ok": True, "gap_id": gap_id, "status": "dismissed"}


@router.post("/knowledge/gap-pass")
async def knowledge_gap_pass():
    """Run the gap→agent→draft→propose pass now (forced; nightly self-gates)."""
    return await gap_pass(True)
