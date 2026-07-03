"""Free internet access tools shared by all CRM agents.

Search:   ddgs (DuckDuckGo scrape) — free forever, no API key.
Fallback: Tavily free tier (1,000 searches/month) — used automatically when
          ddgs fails/rate-limits AND settings.tavily_api_key is set.
Fetch:    httpx + trafilatura — clean article text from any URL, free.
Answer:   web_answer() — search → fetch top pages → LLM synthesis with
          sources.  Falls back to a plain result list if the LLM step fails,
          so the user always gets something useful.

Adding web access to an agent
-----------------------------
1. Register a ``web_search`` mode in the agent's system prompt
   (required param: query; optional: url).
2. In the agent's db_node, branch on mode == "web_search" and return::

       from app.core.web_tools import web_answer
       text = web_answer(parsed_json.get("query") or user_input,
                         url=parsed_json.get("url"))
       return {**state, "db_rows": [{"result": {
           "metadata": {"status": "success", "code": 0, "mode": "web_search"},
           "web_markdown": text,
       }}]}

3. In the agent's formatter, render ``web_markdown`` when mode == "web_search"
   (same pattern as the executive_question / exec_markdown flow).
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)

# Plain-browser UA — some sites return empty/blocked pages to python-httpx UA.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_FETCH_TIMEOUT = 15.0


# ============================================================================
# SEARCH — ddgs primary, Tavily free-tier fallback
# ============================================================================

def _ddgs_search(query: str, max_results: int) -> List[Dict[str, str]]:
    """DuckDuckGo search via ddgs — free, no key. Raises on failure."""
    from ddgs import DDGS
    with DDGS() as ddgs:
        raw = list(ddgs.text(query, max_results=max_results))
    return [
        {
            "title":   r.get("title", ""),
            "url":     r.get("href")  or r.get("url", ""),
            "snippet": r.get("body")  or r.get("description", ""),
        }
        for r in raw if (r.get("href") or r.get("url"))
    ]


def _tavily_search(query: str, max_results: int) -> List[Dict[str, str]]:
    """Tavily search — free tier 1,000 requests/month. Needs tavily_api_key."""
    settings = get_settings()
    if not settings.tavily_api_key:
        return []
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key":     settings.tavily_api_key,
            "query":       query,
            "max_results": max_results,
        },
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        {
            "title":   r.get("title", ""),
            "url":     r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in resp.json().get("results", []) if r.get("url")
    ]


def web_search(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """
    Search the web. Returns [{title, url, snippet}, ...] — possibly empty.

    Order: ddgs (free, no key) → Tavily free tier (if key configured).
    Never raises — an empty list means both engines failed.
    """
    settings = get_settings()
    if not settings.web_search_enabled:
        logger.info("web_search disabled via settings")
        return []

    try:
        results = _ddgs_search(query, max_results)
        if results:
            logger.info(f"web_search (ddgs): {len(results)} results for {query!r}")
            return results
        logger.warning(f"web_search (ddgs): 0 results for {query!r} — trying Tavily")
    except Exception as e:
        logger.warning(f"web_search (ddgs) failed for {query!r}: {e} — trying Tavily")

    try:
        results = _tavily_search(query, max_results)
        if results:
            logger.info(f"web_search (tavily): {len(results)} results for {query!r}")
        return results
    except Exception as e:
        logger.error(f"web_search (tavily) failed for {query!r}: {e}")
        return []


# ============================================================================
# FETCH — httpx + trafilatura clean-text extraction
# ============================================================================

def fetch_page(url: str, max_chars: Optional[int] = None) -> str:
    """
    Fetch a URL and return clean readable text, trimmed to max_chars
    (defaults to settings.web_fetch_max_chars). Never raises — returns ""
    when the page cannot be fetched or yields no text.
    """
    settings = get_settings()
    limit = max_chars or settings.web_fetch_max_chars

    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _UA},
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning(f"fetch_page: GET {url} failed: {e}")
        return ""

    text = ""
    try:
        import trafilatura
        text = trafilatura.extract(html) or ""
    except Exception as e:
        logger.warning(f"fetch_page: trafilatura failed for {url}: {e}")

    if not text:
        # Crude fallback: strip tags/scripts so we still return something.
        stripped = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.IGNORECASE)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        text = re.sub(r"\s+", " ", stripped).strip()

    if len(text) > limit:
        text = text[:limit] + " …[truncated]"
    logger.info(f"fetch_page: {url} → {len(text)} chars")
    return text


# ============================================================================
# ANSWER — search + fetch + LLM synthesis with sources
# ============================================================================

_SYNTHESIS_PROMPT = """You are a research assistant inside a CRM application.
Answer the user's question using ONLY the web material below.
Rules:
- Be concise and factual; use short markdown (a few sentences or bullets).
- End with a "Sources:" line listing the URLs you actually used.
- If the material does not answer the question, say so plainly.
"""


def _format_results_markdown(query: str, results: List[Dict[str, str]]) -> str:
    """Plain markdown result list — fallback when LLM synthesis is unavailable."""
    lines = [f"**Web results for:** {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **[{r['title']}]({r['url']})**")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)


def web_answer(query: str, url: Optional[str] = None, max_results: int = 5) -> str:
    """
    Answer a question from the live internet. Returns markdown, never raises.

    - With ``url``: fetch that page and answer from its content.
    - Without: search (ddgs → Tavily), fetch the top 2 pages, synthesize an
      answer with the configured LLM, citing sources.
    - If the LLM step fails, returns the raw search-result list instead.
    """
    query = (query or "").strip()
    if not query and not url:
        return "I need a search query or a URL to look something up on the web."

    results: List[Dict[str, str]] = []
    material: List[str] = []

    if url:
        page = fetch_page(url)
        if page:
            material.append(f"[Source: {url}]\n{page}")
        results = [{"title": url, "url": url, "snippet": ""}]
    else:
        results = web_search(query, max_results=max_results)
        if not results:
            return (f"I couldn't reach any web search engine for \"{query}\" right now. "
                    "Please try again in a moment.")
        snippets = "\n".join(f"- {r['title']}: {r['snippet']} ({r['url']})" for r in results)
        material.append(f"[Search result snippets]\n{snippets}")
        for r in results[:2]:
            page = fetch_page(r["url"])
            if page:
                material.append(f"[Source: {r['url']}]\n{page}")

    if not material:
        return _format_results_markdown(query, results)

    try:
        # Imported lazily — graph_utils imports config, web_tools imports
        # graph_utils only here, so module import stays cycle-free.
        from .graph_utils import _get_llm
        llm = _get_llm()
        response = llm.invoke([
            {"role": "system", "content": _SYNTHESIS_PROMPT},
            {"role": "user",
             "content": f"Question: {query or 'Summarize this page.'}\n\n"
                        + "\n\n---\n\n".join(material)},
        ])
        answer = response.content if hasattr(response, "content") else str(response)
        if answer and answer.strip():
            return answer.strip()
    except Exception as e:
        logger.error(f"web_answer: LLM synthesis failed: {e}")

    return _format_results_markdown(query, results)
