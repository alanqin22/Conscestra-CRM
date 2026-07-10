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

CONFIG (env)
  KB_RAG_ENABLED     1   retrieval into the auto-reply (read-only kill switch)
  KB_DRAFT_ENABLED   0   nightly mining job on/off (LLM cost — opt-in)
  KB_DRAFT_CAP       3   max articles drafted per pass
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
DRAFT_ENABLED = _flag("KB_DRAFT_ENABLED", "0")
DRAFT_CAP = int(os.getenv("KB_DRAFT_CAP", "3"))

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


def rag_block(subject: str, body: str) -> str:
    """Prompt block with the best-matching approved answers ('' when none or
    the kill switch is off). Counts the retrieval as a use."""
    if not RAG_ENABLED:
        return ""
    query = f"{subject or ''} {str(body or '')[:200]}"
    terms = _salient_terms(query)
    need = 1 if len(terms) <= 2 else RAG_MIN_TERMS
    hits = [h for h in search(query, limit=4, min_rank=RAG_MIN_RANK,
                              any_terms=True)
            if _matched_terms(terms, h) >= need][:2]
    if not hits:
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


def draft_pass(force: bool = False) -> Dict[str, Any]:
    """Mine resolved support threads → LLM-draft articles → PROPOSE each via
    governance (kb.publish). Publishes nothing itself. force=True runs even
    when KB_DRAFT_ENABLED=0 (endpoint/testing)."""
    if not DRAFT_ENABLED and not force:
        return {"enabled": False, "skipped": True}
    from app.core import governance

    threads = _resolved_threads(DRAFT_CAP)
    proposed, skipped = [], 0
    for t in threads:
        subject = re.sub(r"^Inbound:\s*", "", t["subject"] or "")
        art = _draft_llm(subject, t["description"] or "", t["resolution"])
        if not art:
            skipped += 1
            continue
        art["source_ref"] = t["activity_id"]
        art["source"] = "agent"
        aid = governance.propose("kb.publish", "knowledge", art,
                                 confidence=0.6, severity="low")
        proposed.append({"approval_uuid": aid, "title": art.get("title"),
                         "source_ref": t["activity_id"]})
        logger.info(f"[knowledge] proposed article '{str(art.get('title'))[:60]}' "
                    f"({aid[:8]})")
    return {"enabled": DRAFT_ENABLED, "threads": len(threads),
            "proposed": proposed, "draft_failures": skipped}


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
