"""External knowledge ingestion — documents become governed KB proposals.

The KB pipeline (draft → critic → human approval → kb.publish → undo) is
built; this module feeds it from OUTSIDE sources:

    POST /knowledge/ingest        upload a document (PDF, .txt, .md, .html)
    POST /knowledge/ingest-url    ingest a web page (manufacturer docs,
                                  regulations) via web_tools.fetch_page

Pipeline: extract text → chunk on paragraph boundaries (~2,500 chars) →
one LLM draft per chunk ({title, problem, answer, keywords}, with an
explicit SKIP escape for boilerplate/TOC/legal chunks) → PROPOSE each via
governance kb.publish. Nothing publishes itself — the same human approval
and critic apply as to every mined article.

IDEMPOTENT: source_ref = doc:<sha256[:12]>:<chunk#>. Re-uploading the same
file proposes nothing new (chunks already published or pending are skipped
BEFORE any LLM spend).

BOUNDED: at most KB_INGEST_CAP proposals per call — a 300-page manual is
ingested over several deliberate passes, not one runaway loop. The response
says how many chunks remain.

CONFIG (env)
  KB_INGEST_CAP   8   max article proposals per ingest call
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

logger = logging.getLogger("kb_ingest")

INGEST_CAP = int(os.getenv("KB_INGEST_CAP", "8"))

_CHUNK_CHARS = 2500          # target chunk size (paragraph-packed)
_MIN_CHUNK_CHARS = 200       # smaller fragments carry no article
_MAX_DOC_CHARS = 400_000     # hard ceiling per document
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


# ============================================================================
# EXTRACT — bytes → plain text (per format, best-effort)
# ============================================================================

def extract_text(filename: str, data: bytes) -> str:
    """Plain text from a document. Raises ValueError on unsupported/broken
    input — the endpoint turns that into a 422."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            import fitz                      # PyMuPDF
            with fitz.open(stream=data, filetype="pdf") as doc:
                text = "\n\n".join(page.get_text() for page in doc)
        except Exception as exc:
            raise ValueError(f"PDF extraction failed: {exc}")
    elif name.endswith((".txt", ".md", ".markdown", ".rst", ".csv")):
        text = data.decode("utf-8", errors="replace")
    elif name.endswith((".html", ".htm")):
        html = data.decode("utf-8", errors="replace")
        text = ""
        try:
            import trafilatura
            text = trafilatura.extract(html) or ""
        except Exception:
            pass
        if not text:
            stripped = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html,
                              flags=re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", stripped)
    else:
        raise ValueError(f"unsupported file type: {filename!r} "
                         "(pdf, txt, md, html supported)")
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) < _MIN_CHUNK_CHARS:
        raise ValueError("document yielded almost no text "
                         f"({len(text)} chars) — scanned image PDF?")
    return text[:_MAX_DOC_CHARS]


def chunk_text(text: str) -> List[str]:
    """Paragraph-packed chunks of ~_CHUNK_CHARS. Paragraph boundaries keep
    each chunk self-coherent — an article is drafted from ONE chunk."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > _CHUNK_CHARS:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(c) >= _MIN_CHUNK_CHARS]


# ============================================================================
# DRAFT — one governed article proposal per useful chunk
# ============================================================================

def _draft_doc_llm(doc_name: str, chunk: str) -> Optional[Dict[str, Any]]:
    """Chunk → article dict, or None (SKIP / failure). The escape hatch
    matters: manuals are full of TOCs, legal boilerplate and revision
    tables that must never become customer-facing articles."""
    try:
        from app.core.graph_utils import _get_llm
        from app.core.knowledge import MIN_ANSWER_CHARS
        resp = _get_llm(tier="lite").invoke([
            {"role": "system", "content":
                "You turn excerpts of company/product documentation into "
                "knowledge-base articles served VERBATIM to customers of "
                "Conscestra CRM. Write ONE article answering the most useful "
                "customer question this excerpt can answer. Stay strictly "
                "within the excerpt — never add facts, prices or promises it "
                "doesn't contain. If the excerpt is a table of contents, "
                "legal boilerplate, revision history, or otherwise carries "
                "no answerable customer question, reply with exactly SKIP."},
            {"role": "user", "content":
                f"Document: {doc_name}\n\nExcerpt:\n{chunk[:3000]}\n\n"
                'Return ONLY JSON: {"title": "<question a customer would '
                'ask, <=100 chars>", "problem": "<1-2 sentence problem>", '
                '"answer": "<the answer, 2-6 sentences, from the excerpt '
                'only>", "keywords": ["<3-6 search terms>"]} — or exactly '
                'SKIP.'},
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
        logger.warning(f"[ingest] draft failed: {exc}")
        return None


def _already_processed(source_ref: str) -> bool:
    """Published already, or a live/decided kb.publish proposal exists —
    checked BEFORE the LLM runs, so re-uploads cost nothing."""
    from app.core.database import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM knowledge_articles WHERE source_ref=%(r)s
                   UNION ALL
                   SELECT 1 FROM action_approvals
                   WHERE action_type='kb.publish'
                     AND params->>'source_ref'=%(r)s
                     AND status IN ('pending','approved','executed')
                   LIMIT 1""", {"r": source_ref})
            return cur.fetchone() is not None
    finally:
        conn.close()


def _register_document(sha: str, name: str, chunks: int,
                       uploaded_by: str = "admin") -> None:
    """Upsert the document registry row (kb_documents) — names and groups the
    doc's articles for the admin UI. Best-effort: a missing migration never
    blocks ingestion itself."""
    from app.core.database import get_connection
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO kb_documents (sha, name, chunks, uploaded_by)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (sha) DO UPDATE
                   SET name=EXCLUDED.name, chunks=EXCLUDED.chunks,
                       status='active', updated_at=now()""",
                (sha, name[:300], chunks, uploaded_by))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug(f"[ingest] registry skipped (table missing?): {exc}")


def ingest(doc_name: str, text: str, cap: int = 0) -> Dict[str, Any]:
    """Chunk → draft → PROPOSE (governed kb.publish). Idempotent per
    (document content, chunk index); bounded by cap (default KB_INGEST_CAP)."""
    from app.core import governance

    cap = cap or INGEST_CAP
    sha = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    chunks = chunk_text(text)
    _register_document(sha, doc_name, len(chunks))
    proposed, skipped_llm, already = [], 0, 0
    deferred: list = []          # held back by kb.publish's daily cap, not lost
    for i, chunk in enumerate(chunks):
        if len(proposed) >= cap:
            break
        ref = f"doc:{sha}:{i}"
        if _already_processed(ref):
            already += 1
            continue
        art = _draft_doc_llm(doc_name, chunk)
        if not art:
            skipped_llm += 1
            continue
        art["source"] = "doc"
        art["source_ref"] = ref
        try:
            aid = governance.propose("kb.publish", "kb-ingest", art,
                                     confidence=0.6, severity="low")
        except governance.ProposalCapReached as capped:
            # kb.publish is capped by policy (3 CRO decisions a day). An ingest
            # that raised here would abandon the rest of the document because
            # the CRO's day was full. The chunk is deferred; `source_ref` is the
            # idempotency anchor, so re-ingesting tomorrow proposes it once.
            logger.info(f"[kb_ingest] chunk {i} deferred — {capped}")
            deferred.append({"chunk": i, "title": art.get("title"),
                             "reason": str(capped)})
            continue
        proposed.append({"approval_uuid": aid, "chunk": i,
                         "title": art.get("title")})
        logger.info(f"[ingest] {doc_name} chunk {i} → proposed "
                    f"'{str(art.get('title'))[:60]}' ({aid[:8]})")
    done = already + skipped_llm + len(proposed)
    return {"document": doc_name, "sha": sha, "chunks": len(chunks),
            "proposed": proposed, "deferred_by_cap": deferred,
            "skipped_boilerplate": skipped_llm,
            "already_processed": already,
            "remaining_chunks": max(len(chunks) - done, 0),
            "note": ("re-run to continue — the cap bounds each pass"
                     if len(chunks) - done > 0 else "document fully processed")}


# ============================================================================
# Endpoints (admin — registered with _ADMIN in main.py)
# ============================================================================

router = APIRouter(tags=["kb-ingest"])


@router.post("/knowledge/ingest")
async def knowledge_ingest(file: UploadFile = File(...), cap: int = 0):
    """Upload a document (PDF/txt/md/html) → governed article proposals."""
    import asyncio
    data = await file.read()
    if len(data) > 25_000_000:
        raise HTTPException(status_code=413, detail="file over 25 MB")
    try:
        text = extract_text(file.filename or "upload", data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await asyncio.to_thread(ingest, file.filename or "upload", text, cap)


class _UrlBody(BaseModel):
    url: str
    cap: int = 0


@router.post("/knowledge/ingest-url")
async def knowledge_ingest_url(body: _UrlBody):
    """Ingest a web page (manufacturer docs, regulations) by URL."""
    import asyncio
    from app.core.web_tools import fetch_page
    text = await asyncio.to_thread(fetch_page, body.url, _MAX_DOC_CHARS)
    if len(text or "") < _MIN_CHUNK_CHARS:
        raise HTTPException(status_code=422,
                            detail=f"no usable text at {body.url}")
    return await asyncio.to_thread(ingest, body.url, text, body.cap)


# ============================================================================
# Document registry — list / remove / restore (the admin UI's data)
# ============================================================================

@router.get("/knowledge/documents")
def knowledge_documents():
    """Ingested documents with live per-document article/proposal counts."""
    from app.core.database import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT d.sha, d.name, d.chunks, d.status,
                          d.created_at, d.updated_at,
                          (SELECT count(*) FROM knowledge_articles k
                           WHERE k.source_ref LIKE 'doc:'||d.sha||':%'
                             AND k.status='active')  AS active_articles,
                          (SELECT count(*) FROM knowledge_articles k
                           WHERE k.source_ref LIKE 'doc:'||d.sha||':%'
                             AND k.status='retired') AS retired_articles,
                          (SELECT count(*) FROM action_approvals a
                           WHERE a.action_type='kb.publish'
                             AND a.params->>'source_ref' LIKE 'doc:'||d.sha||':%'
                             AND a.status='pending')  AS pending_proposals
                   FROM kb_documents d
                   ORDER BY d.created_at DESC""")
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            for r in rows:
                for k in ("created_at", "updated_at"):
                    r[k] = r[k].isoformat() if r[k] else None
    except Exception as exc:
        return {"count": 0, "documents": [],
                "note": f"kb_documents unavailable ({exc}) — apply "
                        "sql/kb_documents.sql"}
    finally:
        conn.close()
    return {"count": len(rows), "documents": rows, "cap": INGEST_CAP}


def _doc_pending_proposals(sha: str) -> List[str]:
    from app.core.database import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT approval_uuid::text FROM action_approvals
                   WHERE action_type='kb.publish' AND status='pending'
                     AND params->>'source_ref' LIKE 'doc:'||%s||':%%'""",
                (sha,))
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


@router.post("/knowledge/documents/{sha}/remove")
def knowledge_document_remove(sha: str):
    """Remove a document from the KB as a unit: retire its active articles
    (they stop serving IMMEDIATELY) and reject its pending proposals
    (audited). The registry row stays, so a re-upload remains idempotent —
    use restore to bring the articles back."""
    from app.core import governance
    from app.core.database import get_connection
    rejected = 0
    for aid in _doc_pending_proposals(sha):
        res = governance.reject(aid, decided_by="knowledge-mgmt",
                                reason="document removed from knowledge base")
        rejected += 1 if res.get("ok") else 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE knowledge_articles
                   SET status='retired', updated_at=now()
                   WHERE source_ref LIKE 'doc:'||%s||':%%' AND status='active'
                   RETURNING 1""", (sha,))
            retired = len(cur.fetchall())
            cur.execute("UPDATE kb_documents SET status='removed', "
                        "updated_at=now() WHERE sha=%s RETURNING 1", (sha,))
            found = cur.fetchone() is not None
        conn.commit()
    finally:
        conn.close()
    if not found:
        raise HTTPException(status_code=404, detail=f"document {sha} not found")
    logger.info(f"[ingest] document {sha} removed: {retired} article(s) "
                f"retired, {rejected} proposal(s) rejected")
    return {"ok": True, "sha": sha, "articles_retired": retired,
            "proposals_rejected": rejected}


@router.post("/knowledge/documents/{sha}/restore")
def knowledge_document_restore(sha: str):
    """Undo a removal: un-retire the document's articles (rejected proposals
    stay rejected — re-upload the document to re-propose those chunks)."""
    from app.core.database import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE knowledge_articles
                   SET status='active', updated_at=now()
                   WHERE source_ref LIKE 'doc:'||%s||':%%' AND status='retired'
                   RETURNING 1""", (sha,))
            restored = len(cur.fetchall())
            cur.execute("UPDATE kb_documents SET status='active', "
                        "updated_at=now() WHERE sha=%s RETURNING 1", (sha,))
            found = cur.fetchone() is not None
        conn.commit()
    finally:
        conn.close()
    if not found:
        raise HTTPException(status_code=404, detail=f"document {sha} not found")
    logger.info(f"[ingest] document {sha} restored: {restored} article(s)")
    return {"ok": True, "sha": sha, "articles_restored": restored}
