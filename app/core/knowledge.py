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
import time
from concurrent.futures import ThreadPoolExecutor
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

# Words that carry no topic. The second group is CONVERSATIONAL FILLER, and
# leaving it out was a real retrieval bug rather than a missed optimisation:
# `need` (how many query terms an article must match) is derived from the
# COUNT of salient terms, so filler inflated the count and raised the bar the
# answer had to clear. "company" retrieved the company article; "tell me about
# your company" retrieved nothing — Postgres ranked the right article first
# and our own filter then discarded it for matching "only" 1 of 3 terms.
# Politeness made the search stricter, which is exactly backwards, and it hit
# every language: a spoken question is almost always the padded form.
_STOPWORDS = {"the", "and", "for", "with", "your", "from", "this", "that",
              "have", "has", "are", "was", "can", "cant", "cannot", "you",
              "our", "not", "how", "what", "why", "need", "help", "please",
              "hello", "thanks", "hi", "dear", "regards",
              # conversational filler
              "tell", "about", "know", "like", "want", "give", "say", "said",
              "ask", "asking", "explain", "describe", "introduce", "some",
              "something", "anything", "more", "little", "bit", "could",
              "would", "should", "does", "did", "get", "got", "there",
              "here", "just", "really", "maybe", "wondering", "curious",
              # Non-English filler. The list was English-only, so the same
              # bug reappeared one language over: "parlez-moi de votre
              # entreprise" scored 4 salient terms, needed 2 matches, and the
              # company article matched only "entreprise" — discarded. Only
              # LATIN-script languages appear here; Chinese produces no ASCII
              # terms at all, so it never reaches this filter and depends
              # entirely on the semantic leg.
              "parlez", "parler", "moi", "votre", "vos", "une", "des", "les",
              "pouvez", "puis", "quel", "quelle", "quels", "quelles", "est",
              "sur", "pour", "avec", "dites", "dire", "peu", "plus", "bonjour",
              "merci", "que", "qui", "quoi", "comment", "pourquoi",
              "hablame", "háblame", "sobre", "puede", "puedo", "cual", "cuál",
              "como", "cómo", "que", "por", "para", "con", "los", "las",
              "una", "unos", "unas", "hola", "gracias", "decir", "digame",
              "sagen", "sie", "mir", "ihre", "ihr", "eine", "einen", "kann",
              "koennen", "können", "was", "wie", "warum", "ueber", "über",
              "hallo", "danke", "bitte", "etwas"}


# ============================================================================
# RETRIEVAL — deterministic full-text search
# ============================================================================

_AUD_CACHE: Dict[str, Any] = {"at": 0.0, "has": False}


def _has_audience() -> bool:
    """Whether the audience/category/review_after migration (kb_enrichment.sql)
    is applied — cached 5 min. False = filters are omitted and retrieval
    behaves exactly as before the migration (graceful on Railway)."""
    if time.time() - _AUD_CACHE["at"] < 300:
        return _AUD_CACHE["has"]
    has = False
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM information_schema.columns "
                            "WHERE table_name='knowledge_articles' "
                            "AND column_name='audience'")
                has = cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        pass
    _AUD_CACHE.update(at=time.time(), has=has)
    return has


def search(query: str, limit: int = 3, min_rank: float = MIN_RANK,
           any_terms: bool = False,
           audience: Optional[str] = None) -> List[Dict[str, Any]]:
    """Ranked active articles for a query ([] on any failure).

    Default = AND semantics (plainto_tsquery — precise, used for API search
    and duplicate detection). any_terms=True = OR semantics over the query's
    salient words — what free-text emails need, where no article contains
    EVERY word of the message; the higher rank floor keeps single-word
    coincidences out.

    audience: 'public' restricts to customer-facing articles (what every
    customer channel must pass); 'internal' = agent-only tier; None = all.
    Ignored (all rows) until kb_enrichment.sql is applied."""
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
    aud_sql, args = "", [tsq_arg, tsq_arg]
    if audience and _has_audience():
        aud_sql = "AND audience=%s"
        args.append(audience)
    args.append(int(limit))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT article_uuid::text, title, problem, answer, keywords,
                           ts_rank(search_tsv, {tsq_sql}) AS rank
                    FROM knowledge_articles
                    WHERE status='active'
                      AND search_tsv @@ {tsq_sql} {aud_sql}
                    ORDER BY rank DESC LIMIT %s""",
                tuple(args))
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


def _semantic_hits(query: str, limit: int = 4,
                   audience: Optional[str] = None) -> List[Dict[str, Any]]:
    """Articles the embedding index considers close, as full rows tagged with
    'sim'. [] whenever semantic is off/unavailable — rag_block then behaves
    exactly as the pure-FTS version did. The audience filter applies at the
    row fetch, so an internal article can never surface through the vector
    path either."""
    try:
        from app.core import semantic
        sims = semantic.search(query, limit=limit)
    except Exception as exc:
        logger.debug(f"[knowledge] semantic skipped: {exc}")
        return []
    if not sims:
        return []
    by_uuid = {h["article_uuid"]: h["sim"] for h in sims}
    aud_sql, args = "", [list(by_uuid)]
    if audience and _has_audience():
        aud_sql = "AND audience=%s"
        args.append(audience)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT article_uuid::text, title, problem, answer, keywords
                   FROM knowledge_articles
                   WHERE status='active' AND article_uuid = ANY(%s::uuid[])
                   {aud_sql}""",
                tuple(args))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        via = {h["article_uuid"]: h.get("via", "floor") for h in sims}
        for r in rows:
            r["sim"] = by_uuid[r["article_uuid"]]
            r["via"] = via.get(r["article_uuid"], "floor")
        return sorted(rows, key=lambda r: r["sim"], reverse=True)
    except Exception as exc:
        logger.debug(f"[knowledge] semantic fetch skipped: {exc}")
        return []
    finally:
        conn.close()


# ── Answerability gate ──────────────────────────────────────────────────────
# _fuse is reciprocal-rank FUSION: it ranks, it does not judge. RRF scores have
# no absolute meaning — every article on either input list receives one — so if
# a list is non-empty something always reaches the agent, labelled [APPROVED
# KNOWLEDGE BASE]. That is why out-of-scope questions were answered: the
# semantic floor of 0.33 sits far below where real answers live, and nothing
# downstream re-checks relevance.
#
# Measured on this KB (60 golden positives vs 10 out-of-scope controls):
#     clean positives      min 0.524   median 0.766
#     negative controls    max 0.534   median 0.387
# The distributions look separable, but raising the floor alone does NOT work —
# noisy real utterances reach down to 0.368 and non-English questions to 0.500,
# so any floor high enough to exclude 0.534 also discards genuine traffic.
#
# The discriminator that DOES separate them is agreement between the two
# retrieval halves: the top semantic hit also appearing in the keyword list.
#     positives 56/60 agree    negatives 0/10 agree
# So: accept a strong semantic match outright, or a moderate one that the
# keyword half independently corroborates.
#
# ANSWERABILITY_HI is deliberately below the 0.55 that scored perfectly on
# English, because non-Latin-script queries have no keyword half at all (the
# tsvector is English) and would fail the corroboration arm — Mandarin "how do
# I export contacts" measures 0.500. Excluding a correct multilingual answer to
# exclude one more control question is a bad trade.
ANSWERABILITY_GATE = _flag("KB_ANSWERABILITY_GATE", "1")
ANSWERABILITY_HI = float(os.getenv("KB_ANSWERABILITY_HI", "0.50"))
ANSWERABILITY_LO = float(os.getenv("KB_ANSWERABILITY_LO", "0.40"))


def _answerable(fts: List[Dict[str, Any]], sem: List[Dict[str, Any]]) -> bool:
    """Is ANY retrieved article plausibly an answer, in absolute terms?

    Returning False means refuse and log a gap — which is a better outcome than
    the nearest article, because a gap is recoverable and a confident wrong
    answer is not.
    """
    if not ANSWERABILITY_GATE:
        return bool(fts or sem)
    if not sem:
        # Keyword-only hits already passed term-count precision in _fts_hits;
        # they are corroborated by construction, so they stand on their own.
        return bool(fts)
    best = float(sem[0].get("sim") or 0.0)
    if best >= ANSWERABILITY_HI:
        return True
    fts_ids = {h["article_uuid"] for h in fts}
    corroborated = any(h["article_uuid"] in fts_ids for h in sem)
    return best >= ANSWERABILITY_LO and corroborated


def _fuse(fts: List[Dict[str, Any]], sem: List[Dict[str, Any]],
          top: int = 2, k: int = 60) -> List[Dict[str, Any]]:
    """Reciprocal-rank fusion of the two ranked lists. An article on BOTH
    lists sums both scores, so keyword+meaning agreement outranks either
    signal alone; k=60 is the standard damping constant."""
    scores: Dict[str, float] = {}
    rows: Dict[str, Dict[str, Any]] = {}
    for ranked in (fts, sem):
        for i, h in enumerate(ranked):
            u = h["article_uuid"]
            scores[u] = scores.get(u, 0.0) + 1.0 / (k + i + 1)
            rows.setdefault(u, h)
    order = sorted(scores, key=lambda u: scores[u], reverse=True)
    return [rows[u] for u in order[:top]]


def _fts_hits(query: str, audience: Optional[str]) -> List[Dict[str, Any]]:
    """The keyword half of hybrid retrieval, term-precision ordered."""
    terms = _salient_terms(query)
    need = 1 if len(terms) <= 2 else RAG_MIN_TERMS
    fts = [h for h in search(query, limit=4, min_rank=RAG_MIN_RANK,
                             any_terms=True, audience=audience)
           if _matched_terms(terms, h) >= need]
    # Precision order: how many of the ASKER's terms an article matches beats
    # raw ts_rank (which rewards repeated generic words like 'call'/'text').
    return sorted(fts, key=lambda h: (_matched_terms(terms, h),
                                      float(h["rank"])), reverse=True)


def retrieve(subject: str, body: str, audience: Optional[str] = "public",
             top: int = 2) -> List[Dict[str, Any]]:
    """Hybrid retrieval core (FTS term-precision + embedding similarity,
    rank-fused) — pure and side-effect free, so the evals can measure it.
    Default audience='public': the tier every customer channel must pass;
    audience=None lets internal callers see the agent-only tier too.

    The two halves run CONCURRENTLY. They are independent — one is a Postgres
    round trip, the other an OpenAI embedding round trip — but they used to run
    back to back, so every retrieval paid the SUM of two network latencies. The
    embedding call alone measured ~450-500ms, which on a phone call is dead air
    the caller hears. Fusing the results is unchanged, so ranking is identical;
    only the waiting overlaps. get_connection() is connect-per-call, so each
    thread gets its own connection and there is no shared-cursor hazard."""
    query = f"{subject or ''} {str(body or '')[:200]}"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            sem_future = pool.submit(_semantic_hits, query, 4, audience)
            fts = _fts_hits(query, audience)
            sem = sem_future.result()
    except Exception as exc:
        # A thread-pool failure must never cost us retrieval itself — fall back
        # to the original sequential path rather than returning nothing.
        logger.debug(f"[knowledge] parallel retrieve fell back to serial: {exc}")
        fts, sem = _fts_hits(query, audience), _semantic_hits(query, 4, audience)
    if not _answerable(fts, sem):
        return []
    return _fuse(fts, sem, top=top)


REWRITE_ENABLED = _flag("KB_REWRITE_ENABLED", "1")


def _rewrite_query(text: str) -> Optional[str]:
    """Corrective query rewrite — ONLY fires after a retrieval MISS on a real
    customer channel, so it costs nothing on hits. A lite LLM condenses the
    noisy message ('uh yeah so I was wondering about…') to its core question;
    None on any failure (the miss then just logs a gap as before)."""
    if not REWRITE_ENABLED:
        return None
    try:
        from app.core.graph_utils import _get_llm
        resp = _get_llm(tier="lite", caller="kb_rewrite").invoke([
            {"role": "system", "content":
                "Rewrite the customer's message as a short knowledge-base "
                "search query capturing its core question — at most 10 plain "
                "words, no punctuation. Reply with ONLY the query."},
            {"role": "user", "content": text[:400]}])
        q = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        return q[:120] if q and len(q.split()) <= 14 else None
    except Exception as exc:
        logger.debug(f"[knowledge] query rewrite skipped: {exc}")
        return None


def rag_block(subject: str, body: str,
              gap_channel: Optional[str] = None,
              audience: Optional[str] = "public") -> str:
    """Prompt block with the best-matching approved answers ('' when none or
    the kill switch is off). Counts the retrieval as a use. HYBRID retrieval:
    FTS term-precision + embedding similarity (semantic.py), rank-fused — a
    caller's wording no longer has to share words with the article. On a miss
    from a real channel, ONE corrective query rewrite retries before the gap
    is logged.

    Pass a REAL subject only (email). Fixed channel labels ('support call',
    'sms inquiry') must be '' — their words pollute term matching, letting a
    generic channel article outrank the actual answer.

    gap_channel: when set and NO article matches, the question is recorded
    as a KB gap for that channel (PII-masked, deduped) — the demand signal
    the nightly gap_pass turns into governed article proposals."""
    if not RAG_ENABLED:
        return ""
    hits = retrieve(subject, body, audience=audience)
    if not hits and gap_channel:
        rq = _rewrite_query(f"{subject or ''} {body or ''}".strip())
        if rq:
            hits = retrieve("", rq, audience=audience)
    if not hits:
        if gap_channel:
            log_gap(gap_channel, body)
        return ""
    # THIRD STATE. Everything reaching here is served to the customer exactly
    # as before — this records WHY, it does not gate anything. A hit whose only
    # support is `via='decisive'` never cleared the similarity floor; it was
    # admitted for leading a weak field. That is correct for a question the
    # vector leg alone can answer (a Chinese query contributes no FTS terms at
    # all) and wrong for a question whose topic merely has one nearby article.
    # Since retrieval cannot tell those apart, the weak ones are recorded
    # instead of discarded, and a human reads the list.
    if gap_channel and hits and all(h.get("via") == "decisive" for h in hits):
        log_weak_match(gap_channel, body)
    _mark_used([h["article_uuid"] for h in hits])
    # The scope caution below is part of the block itself, not of any one
    # agent's prompt, because every channel consumes rag_block — auto_reply,
    # SDR, store chat, the console and authored agents. A caution added to one
    # prompt would leave the others exactly as they were.
    #
    # It exists because of a measured failure: after the KB was corrected to
    # say contact merge EXISTS, the assistant answered "why did the automatic
    # nightly de-duplication merge my contacts?" by explaining how merging
    # works — confirming a nightly job that does not exist. The article was
    # true; the inference drawn from it was not. Retrieval cannot catch this,
    # because the right article WAS retrieved.
    #
    # Three distinctions the model has to keep apart, none of which an article
    # about a capability states on its own:
    #     the capability exists  ≠  it runs automatically
    #     it runs automatically  ≠  it ran in THIS case
    #     it applies to contacts ≠  it applies to orders
    lines = [
        "[APPROVED KNOWLEDGE BASE]",
        "(Scope: these answers say what the product CAN do, for the record type "
        "named. A capability existing does NOT mean it runs automatically, on a "
        "schedule, in the background, or that it happened in any particular "
        "case; and it does NOT extend to record types the answer does not "
        "mention. If the question assumes automatic behaviour, a past event, or "
        "a different record type, correct that assumption plainly before "
        "answering the rest. Never describe the timing, frequency or history of "
        "something these answers do not establish.)",
    ]
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


def _resolved_cases(cap: int) -> List[Dict[str, Any]]:
    """Resolved CASES as a third mining source — C1 Step 8.

    A resolved case is EVIDENCE THAT WORK WAS COMPLETED. It is not, by itself,
    verified knowledge, so this returns candidates for exactly the same
    governance path email threads and call transcripts already take: LLM draft
    → `governance.propose('kb.publish')` → human approval. Nothing here
    publishes, and this function is deliberately a SOURCE rather than a second
    pipeline — a parallel knowledge system would drift from the approval,
    dedupe and privacy handling that already work.

    DETERMINISTIC EXCLUSIONS, applied before any model sees the text:

      * `resolved_at IS NULL` — closure is not completion. A case closed
        without being resolved has no solution to teach.
      * `is_historical` — the 120 pre-C1 rows have no resolution context and no
        recorded history; there is nothing to mine and inventing it is exactly
        what the historical flag exists to prevent.
      * no substantive text — a resolution of "done" teaches nothing.
      * already mined — the same NOT EXISTS pair the other two sources use, so
        a case is proposed once and never re-proposed after approval OR
        rejection is pending/decided.

    Everything subtler than that — is this reusable or customer-specific, one
    time or general — is left to `_draft_llm` returning None (the existing
    classifier) and then to the human in the approval queue. A second
    classification vocabulary would have to be kept in agreement with the first.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.cases')")
            if cur.fetchone()[0] is None:
                return []
            cur.execute(
                """SELECT c.case_id::text, c.subject, c.description,
                          COALESCE(
                            (SELECT string_agg(cm.comment, E'\\n' ORDER BY cm.created_at)
                             FROM case_comments cm
                             WHERE cm.case_id = c.case_id), '') AS resolution
                   FROM cases c
                   WHERE c.resolved_at IS NOT NULL
                     AND c.is_historical = false
                     AND c.resolved_at > now() - interval '30 days'
                     AND length(coalesce(c.subject,'')) > 8
                     AND NOT EXISTS (SELECT 1 FROM knowledge_articles k
                                     WHERE k.source_ref = c.case_id::text)
                     AND NOT EXISTS (SELECT 1 FROM action_approvals ap
                                     WHERE ap.action_type = 'kb.publish'
                                       AND ap.params->>'source_ref' = c.case_id::text
                                       AND ap.status IN ('pending','approved','executed'))
                   ORDER BY c.resolved_at DESC
                   LIMIT %s""", (int(cap),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[knowledge] case mining skipped: {exc}")
        return []
    finally:
        conn.close()
    # A case whose whole record is a one-line subject has no solution in it.
    return [r for r in rows
            if len((r.get("resolution") or "") + (r.get("description") or "")) >= 40]


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
    # C1 Step 8 — resolved CASES as a third source, behind its own flag so the
    # knowledge loop's existing behaviour is byte-identical until it is turned
    # on. Shares the same cap, so adding cases cannot increase the review load
    # the queue already receives.
    cases_src = []
    try:
        from app.core import cases as _cases
        if _cases.ENABLED and _cases.KB_FEEDBACK:
            cases_src = _resolved_cases(DRAFT_CAP - len(threads) - len(calls))
    except Exception as exc:
        logger.debug(f"[knowledge] case source skipped: {exc}")
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
    for cs in cases_src:
        # A case-derived candidate is proposed as INTERNAL. Case text is
        # customer-specific by origin, and U2's reach_invariant means an
        # externally reachable agent may read only the `public` tier — so the
        # safe default keeps a fresh candidate away from customer-facing
        # agents until a human deliberately re-tiers it on approval.
        art = _draft_llm(cs["subject"] or "support case",
                         cs["description"] or "", cs["resolution"] or "")
        if art:
            art.setdefault("audience", "internal")
            _propose(art, cs["case_id"])
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
            "calls": len(calls), "cases": len(cases_src),
            "proposed": proposed, "draft_failures": skipped}


# ============================================================================
# GAP MINING — unanswered questions → agent-grounded article proposals
# ============================================================================

WEAK_MATCH_STATUS = "weak_match"


def log_weak_match(channel: str, question: str) -> None:
    """Record a question answered only from a hit the KB was not confident in.

    THE DEFECT THIS EXISTS TO KILL. rag_block had exactly two outcomes:

        hits == []   ->  log_gap()  ->  the nightly miner can propose an article
        hits != []   ->  covered    ->  nothing recorded, ever

    So "an article came back" was treated as "the question was answered", and a
    question the KB could not really answer left no trace at all. Measured on
    this KB: of 19 adversarial questions with no real answer, 11 still returned
    an article and none was recorded. Worse, promotion runs through
    `UPDATE kb_gaps SET hits = hits + 1` inside log_gap, so a falsely-covered
    question cannot accumulate hits — asking it a thousand times ranks it below
    a one-off gap. Frequency, the only prioritisation signal the miner has, is
    precisely what a false match destroys.

    A weak match is the third state: the customer still gets the answer the
    system would have given anyway — NOTHING about the reply changes — but the
    demand is no longer discarded. Written with status='weak_match' so
    `_open_gaps` (status='open') never picks it up: a weak match is evidence
    that retrieval was unconvincing, which is not the same claim as "an article
    is missing", and it should not auto-propose one. Read it with
    /knowledge/gaps?status=weak_match.

    Deliberately NOT a schema change: `status` carries no CHECK constraint, so
    a new value needs no migration.
    """
    _log_demand(channel, question, WEAK_MATCH_STATUS)


def log_gap(channel: str, question: str) -> None:
    """Record a question the KB could not answer (called from rag_block).
    PII-masked, deduped by sorted salient terms (repeat asks bump `hits`).
    Best-effort: missing table = silent skip, same as the sdr_sessions
    pattern — losing telemetry must never break a live reply."""
    _log_demand(channel, question, "open")


def _log_demand(channel: str, question: str, status: str) -> None:
    """One writer for both demand states, so they cannot drift apart.

    ONE ROW PER QUESTION, and the state only ever ratchets UP:

        weak_match  ──promote──▶  open        (a true miss supersedes)
        open        ──never───▶  weak_match   (a gap is the stronger claim)

    Scoping dedup to each status separately looked right and was wrong. Measured
    on a live Mandarin call: '你们提供API吗？有没有调用频率限制？' produced BOTH an
    `open` row and a `weak_match` row eight seconds apart, because rag_block
    retries a MISS through an LLM query rewrite, and a rewrite that happens to
    surface a decisive hit turns the same question into a weak match. One
    question then occupies two rows whose hit counts each understate the real
    demand — which is the same information loss this file exists to prevent,
    reintroduced one level down.

    Promotion (never absorption) keeps the original guarantee: a weak match can
    never swallow a later true miss. It is upgraded by it.
    """
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
                bump = ("""UPDATE kb_gaps SET hits = hits + 1, updated_at = now()
                            WHERE status = %s AND terms = %s::text[]
                            RETURNING gap_id""")
                hit = None
                if status == "open":
                    # A gap outranks a weak match, so try the gap row first,
                    # then PROMOTE a weak match rather than opening a rival row.
                    cur.execute(bump, ("open", terms))
                    hit = cur.fetchone()
                    if not hit:
                        cur.execute(
                            """UPDATE kb_gaps
                                  SET status = 'open', hits = hits + 1,
                                      updated_at = now()
                                WHERE status = %s AND terms = %s::text[]
                                RETURNING gap_id""", (WEAK_MATCH_STATUS, terms))
                        hit = cur.fetchone()
                        if hit:
                            logger.info(f"[knowledge] weak match promoted to "
                                        f"gap ({channel}): {q[:60]}")
                else:
                    # Ordering matters and cost a bug: checking "bump my own
                    # status" first let a weak match keep incrementing its own
                    # row while a gap row for the same question sat beside it.
                    # A known gap absorbs the observation; only if none exists
                    # does the weak-match row take it.
                    cur.execute(bump, ("open", terms))
                    hit = cur.fetchone()
                    if not hit:
                        cur.execute(bump, (WEAK_MATCH_STATUS, terms))
                        hit = cur.fetchone()
                if not hit:
                    cur.execute(
                        "INSERT INTO kb_gaps (channel, question, terms, status) "
                        "VALUES (%s, %s, %s, %s)",
                        (channel[:20], q, terms, status))
            conn.commit()
        finally:
            conn.close()
        label = "KB gap" if status == "open" else "KB weak match"
        logger.info(f"[knowledge] {label} logged ({channel}): {q[:80]}")
    except Exception as exc:
        logger.debug(f"[knowledge] demand log skipped (table missing?): {exc}")


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

# ============================================================================
# HYGIENE — weekly staleness pass ("flags outdated content")
# ============================================================================

HYGIENE_ENABLED = _flag("KB_HYGIENE_ENABLED", "1")
_ORCH_AGENT = "00000000-0000-0000-0000-000000000012"   # Orchestrator Agent
UNUSED_DAYS = 90


def hygiene_pass(force: bool = False) -> Dict[str, Any]:
    """Weekly: find articles past review_after or never earning their keep
    (90+ days old, unused), and upsert ONE consolidated Orchestrator
    notification (the bottleneck-pass pattern — a heartbeat, never a pile).
    Report-only: retiring stays a human decision via /knowledge/{id}/retire."""
    if not HYGIENE_ENABLED and not force:
        return {"enabled": False, "skipped": True}
    findings: List[Dict[str, Any]] = []
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if _has_audience():
                    cur.execute(
                        """SELECT title, source_ref FROM knowledge_articles
                           WHERE status='active' AND review_after < CURRENT_DATE
                           ORDER BY review_after LIMIT 10""")
                    due = cur.fetchall()
                    if due:
                        findings.append({"kind": "review_due", "count": len(due),
                                         "note": "article(s) past their review "
                                                 "date",
                                         "worst": [r[0] for r in due[:3]]})
                cur.execute(
                    """SELECT title, source_ref FROM knowledge_articles
                       WHERE status='active'
                         AND created_at < now() - interval '%s days'
                         AND uses = 0
                       ORDER BY created_at LIMIT 10""" % UNUSED_DAYS)
                unused = cur.fetchall()
                if unused:
                    findings.append({"kind": "never_used", "count": len(unused),
                                     "note": f"article(s) {UNUSED_DAYS}+ days "
                                             "old that no customer question "
                                             "ever matched",
                                     "worst": [r[0] for r in unused[:3]]})
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[knowledge] hygiene scan skipped: {exc}")
        return {"ok": False, "error": str(exc)[:200]}

    if not findings:
        logger.info("[knowledge] hygiene pass: all fresh")
        return {"ok": True, "findings": [], "notified": False}

    title = f"📚 KB hygiene: {sum(f['count'] for f in findings)} article(s) need review"
    lines = [f"• {f['count']} {f['note']} — e.g. "
             + "; ".join(f["worst"]) for f in findings]
    bodytext = ("Weekly knowledge-base staleness scan:\n" + "\n".join(lines)
                + "\n\nReview: GET /knowledge/list · retire via "
                  "POST /knowledge/{id}/retire · refresh by editing + "
                  "re-publishing")
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT notification_uuid FROM notifications
                   WHERE employee_uuid=%s::uuid
                     AND title LIKE '📚 KB hygiene%%'
                     AND status = ANY(%s) LIMIT 1""",
                (_ORCH_AGENT, ["pending", "sent", "unread"]))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE notifications SET title=%s, body=%s, created_at=now() "
                    "WHERE notification_uuid=%s", (title, bodytext, row[0]))
            else:
                # notifications is a VIEW — anchor with an event first, and
                # mark the row a digest so the trigger keeps our title/body
                # (same contract as learning.bottleneck_pass).
                cur.execute(
                    "SELECT emit_event(%s,%s,%s,%s,%s,%s)",
                    ("bottleneck.detected", "agent", _ORCH_AGENT,
                     json.dumps({"context": {"areas": ["kb_stale"]}}),
                     None, "knowledge"))
                event_uuid = cur.fetchone()[0]
                cur.execute(
                    """INSERT INTO notifications
                         (employee_uuid, event_uuid, channel, status, title,
                          body, metadata, created_at)
                       VALUES (%s::uuid, %s, 'in_app', 'pending', %s, %s,
                               %s::jsonb, now())""",
                    (_ORCH_AGENT, event_uuid, title, bodytext,
                     json.dumps({"kind": "kb_hygiene_digest",
                                 "areas": [f["kind"] for f in findings]})))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"[knowledge] hygiene notification skipped: {exc}")
        return {"ok": True, "findings": findings, "notified": False}
    logger.info(f"[knowledge] hygiene pass: {len(findings)} finding(s) "
                "→ notification upserted")
    return {"ok": True, "findings": findings, "notified": True}


router = APIRouter(tags=["knowledge"])


@router.post("/knowledge/hygiene")
def knowledge_hygiene():
    """Run the staleness scan now (weekly job runs it automatically)."""
    return hygiene_pass(force=True)


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
            # `count` is len(rows) and therefore capped by `limit` — the admin
            # page was rendering it as "Active articles" and so displayed the
            # page size, not the library size. It read exactly 50 with 50 as
            # the limit. Kept for compatibility; `total` is the real number.
            cur.execute(
                "SELECT count(*) FROM knowledge_articles WHERE status=%s",
                (status,))
            total = cur.fetchone()[0]
    finally:
        conn.close()
    return {"status": status, "count": len(rows), "total": total,
            "articles": rows,
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


# ── read and edit one article ────────────────────────────────────────────────
# /knowledge/list returns metadata only — title, source, uses, created_at — so
# until now the ANSWER TEXT the assistants read out to customers could not be
# seen anywhere in the product, let alone corrected. The only available action
# was Retire: delete the article and re-ingest the document. These two routes
# exist so a wrong sentence can be fixed as a wrong sentence.
#
# Path is /knowledge/article/{uuid} rather than /knowledge/{uuid} so it can
# never shadow /knowledge/list, /search, /gaps or /documents.

_AUDIENCES = ("public", "internal")
_EDITABLE = ("title", "problem", "answer", "keywords", "audience", "category",
             "review_after")


def _article_row(cur, article_uuid: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """SELECT article_uuid::text, title, problem, answer, keywords,
                  source, source_ref, status, uses, last_used_at,
                  created_by, created_at, updated_at, audience, category,
                  review_after
           FROM knowledge_articles WHERE article_uuid=%s::uuid""",
        (article_uuid,))
    r = cur.fetchone()
    if not r:
        return None
    row = dict(zip([d[0] for d in cur.description], r))
    for k in ("last_used_at", "created_at", "updated_at", "review_after"):
        row[k] = row[k].isoformat() if row[k] else None
    return row


@router.get("/knowledge/article/{article_uuid}")
def knowledge_get(article_uuid: str):
    """One article in full, including the answer body."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            row = _article_row(cur, article_uuid)
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="no such article")
    return row


@router.patch("/knowledge/article/{article_uuid}")
def knowledge_update(article_uuid: str, body: Dict[str, Any]):
    """Edit an article in place. PATCH semantics — absent keys are untouched.

    Validation deliberately mirrors publish(). An edit path looser than the
    create path is a hole, not a convenience: it would let an article that
    publish() would have refused (empty answer, answer under MIN_ANSWER_CHARS)
    exist anyway, and these articles are read out to customers.

    status is NOT editable here — retire/restore own that transition, and
    folding it in would give two routes the power to change what is served.
    uses, created_at and provenance are not editable at all.
    """
    body = body or {}
    unknown = [k for k in body if k not in _EDITABLE]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"not editable: {', '.join(sorted(unknown))}. "
                   f"Editable fields are {', '.join(_EDITABLE)}")

    sets: List[str] = []
    args: List[Any] = []

    for field in ("title", "problem", "answer"):
        if field not in body:
            continue
        val = str(body.get(field) or "").strip()
        if not val:
            raise HTTPException(status_code=422,
                                detail=f"{field} cannot be empty")
        if field == "answer" and len(val) < MIN_ANSWER_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"answer too short (<{MIN_ANSWER_CHARS} chars) to be "
                       f"useful — the same floor publish() enforces")
        cap = {"title": 180, "problem": 2000, "answer": 4000}[field]
        sets.append(f"{field}=%s")
        args.append(val[:cap])

    if "keywords" in body:
        kws = body.get("keywords")
        if isinstance(kws, str):        # "a, b, c" from a text input
            kws = [k for k in (p.strip() for p in kws.split(",")) if k]
        # publish() caps a NEW draft at 8 — a sensible leash on an LLM writing
        # its own keywords. Applying that cap to an EDIT would be destructive:
        # 5 of the seeded articles carry more, up to 29, because they hold the
        # French, Spanish and Chinese phrasings that make cross-lingual
        # retrieval work. Truncating to 8 on an unrelated edit would silently
        # delete them and quietly break non-English search. The edit ceiling
        # is therefore generous and exists only to bound the column.
        sets.append("keywords=%s")
        args.append([str(k)[:60] for k in (kws or [])][:40])

    if "audience" in body:
        aud = str(body.get("audience") or "").strip()
        # Checked here, not left to the CHECK constraint, so a bad value is a
        # 422 naming the allowed set rather than a 500 from psycopg2. This
        # field decides whether an article reaches customers at all.
        if aud not in _AUDIENCES:
            raise HTTPException(
                status_code=422,
                detail=f"audience must be one of {', '.join(_AUDIENCES)}")
        sets.append("audience=%s")
        args.append(aud)

    if "category" in body:
        cat = str(body.get("category") or "").strip()
        sets.append("category=%s")
        args.append(cat[:60] or None)

    if "review_after" in body:
        ra = str(body.get("review_after") or "").strip()
        sets.append("review_after=%s")
        args.append(ra or None)

    if not sets:
        raise HTTPException(status_code=422, detail="nothing to update")

    sets.append("updated_at=now()")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE knowledge_articles SET {', '.join(sets)} "
                f"WHERE article_uuid=%s::uuid",
                (*args, article_uuid))
            if not cur.rowcount:
                conn.rollback()
                raise HTTPException(status_code=404, detail="no such article")
            row = _article_row(cur, article_uuid)
        conn.commit()
    finally:
        conn.close()

    # search_tsv is maintained by trg_knowledge_articles_tsv, so full-text
    # follows the edit on its own. The VECTOR index does not: kb_embeddings is
    # keyed by a content hash, so until it is refreshed semantic search keeps
    # matching the text that was replaced. Forced here, and never allowed to
    # fail the edit — the article is already saved and FTS already sees it.
    try:
        from app.core import semantic
        semantic.ensure_index(force=True)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning(f"[knowledge] reindex after edit failed: {exc}")

    logger.info(f"[knowledge] edited {article_uuid[:8]} "
                f"({', '.join(k for k in body if k in _EDITABLE)})")
    return {"ok": True, "article": row}


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
