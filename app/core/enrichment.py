"""Lead / company enrichment — the project's first OUTWARD 'function call'.

IBM's article notes agents "use function calling to connect with external tools —
APIs, data sources, web searches." Every agent here so far reads the CRM DB; this
module lets the Leads agent reach BEYOND it to fill knowledge gaps (firmographics
for a new lead). Stub by default (deterministic, no network, safe). Point
LEADS_ENRICH_API_URL at a real provider (Clearbit / Apollo / People Data Labs / …)
and adapt `_call_api()` to its response shape to go live.

CONFIG (env)
  LEADS_ENRICH_PROVIDER  ''   'apollo' | 'pdl' | 'web' | 'generic' | '' (stub)
                                apollo  — Apollo.io organizations/enrich (needs KEY)
                                pdl     — People Data Labs company/enrich (needs KEY)
                                web     — keyless: web_tools search (ddgs/Tavily) —
                                          real external data with NO api key
                                generic — legacy: LEADS_ENRICH_API_URL shape below
  LEADS_ENRICH_API_URL   ''   generic endpoint ('{domain}' substituted, else ?domain=)
  LEADS_ENRICH_API_KEY   ''   provider API key / bearer token
  LEADS_ENRICH_TIMEOUT   6    request timeout (seconds)

All providers fall back to the deterministic stub on failure — enrichment must
never break the bus. apply_to_lead() gap-fills only, so a low-confidence web
result can never overwrite data a human entered.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from app.core import provenance

logger = logging.getLogger("enrichment")

ENRICH_PROVIDER = os.getenv("LEADS_ENRICH_PROVIDER", "").strip().lower()
ENRICH_API_URL = os.getenv("LEADS_ENRICH_API_URL", "").strip()
ENRICH_API_KEY = os.getenv("LEADS_ENRICH_API_KEY", "").strip()
ENRICH_TIMEOUT = float(os.getenv("LEADS_ENRICH_TIMEOUT", "6"))

_INDUSTRIES = ["Software", "Manufacturing", "Healthcare", "Financial Services",
               "Retail", "Logistics", "Construction", "Education", "Hospitality",
               "Energy", "Professional Services", "Media & Marketing"]
_EMPLOYEES = ["1-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5000+"]
_REVENUE = ["<$1M", "$1M-$10M", "$10M-$50M", "$50M-$250M", "$250M-$1B", "$1B+"]
_LOCATIONS = ["Toronto, ON", "Vancouver, BC", "Montreal, QC", "Calgary, AB",
              "Ottawa, ON", "Waterloo, ON", "Halifax, NS", "Edmonton, AB"]

_SLUG = re.compile(r"[^a-z0-9]+")


def _domain(email: Optional[str], company: Optional[str]) -> str:
    if email and "@" in email:
        return email.split("@", 1)[1].strip().lower()
    if company:
        slug = _SLUG.sub("", company.strip().lower())
        return f"{slug}.com" if slug else ""
    return ""


def enrich_company(company: Optional[str] = None, email: Optional[str] = None,
                   domain: Optional[str] = None) -> Dict[str, Any]:
    """Return firmographics for a company. Tries the live API if configured, else a
    deterministic stub. Never raises — returns {'matched': False} when there's
    nothing to look up or the lookup fails."""
    seed = (domain or _domain(email, company) or (company or "")).strip().lower()
    if not seed:
        return {"matched": False, "source": "none", "reason": "no company/email/domain"}
    provider = ENRICH_PROVIDER or ("generic" if ENRICH_API_URL else "")
    try:
        if provider == "apollo" and ENRICH_API_KEY:
            return _call_apollo(seed)
        if provider == "pdl" and ENRICH_API_KEY:
            return _call_pdl(seed)
        if provider == "web":
            r = _call_web(seed, company)
            if r.get("matched"):
                return r
        if provider == "generic" and ENRICH_API_URL:
            return _call_api(seed)
    except Exception as exc:  # network/parse errors fall back to the stub
        logger.warning(f"[enrichment] provider {provider!r} failed ({exc}); using stub")
    return _stub(seed, company)


def _stub(seed: str, company: Optional[str]) -> Dict[str, Any]:
    """Deterministic pseudo-firmographics — stable per company, no network.

    THESE VALUES ARE INVENTED. They are a hash of the domain, not an observation
    of the company, and they exist so demo and dev environments have plausible
    firmographics. The old `confidence` here was 0.60-0.95, which asserted
    trustworthiness for fabricated data — worse than no confidence at all,
    because every downstream consumer read it as a real match quality. It is now
    STUB_CONFIDENCE, and `apply_to_lead` records source_type='computed' so a
    reader can tell invented values from observed ones."""
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)
    return {
        "matched": True,
        "source": "stub",
        "synthetic": True,
        "company": company,
        "domain": seed,
        "website": f"https://{seed}",
        "industry": _INDUSTRIES[h % len(_INDUSTRIES)],
        "employee_band": _EMPLOYEES[(h >> 5) % len(_EMPLOYEES)],
        "revenue_band": _REVENUE[(h >> 9) % len(_REVENUE)],
        "hq_location": _LOCATIONS[(h >> 13) % len(_LOCATIONS)],
        "confidence": STUB_CONFIDENCE,
    }


# Provenance mapping: enrichment `source` → the shared envelope's vocabulary.
# 'external' = an outside system asserted it; 'ai' = we inferred it from web
# text; 'computed' = we derived it ourselves, which is what a stub is.
STUB_CONFIDENCE = 0.15

# (source_type, RELIABILITY) per provider. Reliability is a property of the
# SOURCE — it does not vary per record. Whatever the provider says about a
# specific value is CERTAINTY, and the two multiply.
_SOURCE_KIND = {
    "apollo":  (provenance.EXTERNAL, 0.90),
    "pdl":     (provenance.EXTERNAL, 0.85),
    "generic": (provenance.EXTERNAL, 0.75),
    "web":     (provenance.AI,       0.55),
    # The stub is DETERMINISTIC — it returns the same answer every time, so its
    # certainty is 1.0 — while being disconnected from the actual company, so
    # its reliability is near zero. This row is why the two must be separate:
    # a single number cannot say "perfectly repeatable and completely made up".
    "stub":    (provenance.COMPUTED, STUB_CONFIDENCE),
}


def provenance_for(data: Dict[str, Any]) -> provenance.Provenance:
    """The provenance envelope for one enrichment result.

    A provider's stated match quality becomes CERTAINTY (clamped to 0..1); the
    source's RELIABILITY comes from the table above and is not negotiable by the
    provider — a vendor claiming 0.99 does not make a third-party guess as good
    as something a person looked up. The composite `confidence` is their
    product, so Apollo at a 92% match reads 0.90 × 0.92 = 0.828."""
    src = str(data.get("source") or "unknown").lower()
    kind, reliability = _SOURCE_KIND.get(src, (provenance.UNKNOWN, 0.30))
    certainty = data.get("confidence")
    if src == "stub":
        certainty = 1.0          # deterministic by construction
    return provenance.Provenance(
        source_type=kind, source_id=f"enrichment:{src}",
        reliability=reliability, certainty=certainty,
        observed_at=data.get("observed_at"),
    ).normalized()


def apply_to_lead(lead_id: str, data: Dict[str, Any]) -> int:
    """Write enrichment onto the lead row, GAP-FILL only (existing values are never
    overwritten). Maps hq_location 'City, PROV' -> city/province. Returns rows
    updated. Requires sql/leads_enrichment_columns.sql — raises if columns absent
    (callers treat enrichment-apply as best-effort)."""
    if not data or not data.get("matched"):
        return 0
    from app.core.database import get_connection
    p = provenance_for(data).as_columns()
    hq = (data.get("hq_location") or "")
    city = prov = None
    if "," in hq:
        city, prov = [x.strip() for x in hq.split(",", 1)]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE leads SET
                    industry      = COALESCE(NULLIF(industry, ''),      %(industry)s),
                    website       = COALESCE(NULLIF(website, ''),       %(website)s),
                    employee_band = COALESCE(NULLIF(employee_band, ''), %(emp)s),
                    revenue_band  = COALESCE(NULLIF(revenue_band, ''),  %(rev)s),
                    city          = COALESCE(NULLIF(city, ''),          %(city)s),
                    province      = COALESCE(NULLIF(province, ''),      %(prov)s),
                    -- Provenance travels WITH the values. Without it an Apollo
                    -- match, a web-scraped guess and a fabricated stub value are
                    -- the same string in the same column, and Explore segments
                    -- on employee_band / revenue_band as if a person typed them.
                    source_type   = %(src_type)s,
                    source_id     = %(src_id)s,
                    reliability   = %(rel)s,
                    certainty     = %(cert)s,
                    confidence    = %(conf)s,
                    observed_at   = %(observed)s::timestamptz,
                    enriched_at   = now(),
                    updated_at    = now()
                WHERE lead_id = %(id)s
                """,
                {"industry": data.get("industry"), "website": data.get("website"),
                 "emp": data.get("employee_band"), "rev": data.get("revenue_band"),
                 "city": city, "prov": prov, "id": lead_id,
                 "src_type": p["source_type"], "src_id": p["source_id"],
                 "rel": p["reliability"], "cert": p["certainty"],
                 "conf": p["confidence"], "observed": p["observed_at"]},
            )
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


# ── Provider adapters ────────────────────────────────────────────────────────

def _emp_band(n) -> Optional[str]:
    """Raw headcount → the CRM's employee_band buckets."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    for cap, band in ((10, "1-10"), (50, "11-50"), (200, "51-200"),
                      (500, "201-500"), (1000, "501-1000"), (5000, "1001-5000")):
        if n <= cap:
            return band
    return "5000+"


def _http_json(url: str, headers: Dict[str, str],
               payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    if data is not None:
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=ENRICH_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _call_apollo(seed: str) -> Dict[str, Any]:
    """Apollo.io organizations/enrich — set LEADS_ENRICH_PROVIDER=apollo +
    LEADS_ENRICH_API_KEY. Docs: api.apollo.io/api/v1/organizations/enrich."""
    raw = _http_json(
        f"https://api.apollo.io/api/v1/organizations/enrich"
        f"?domain={urllib.parse.quote(seed)}",
        {"Accept": "application/json", "X-Api-Key": ENRICH_API_KEY})
    org = raw.get("organization") or {}
    if not org:
        return {"matched": False, "source": "apollo", "domain": seed}
    city, state = org.get("city"), org.get("state")
    return {
        "matched": True, "source": "apollo", "domain": seed,
        "company": org.get("name"),
        "website": org.get("website_url") or f"https://{seed}",
        "industry": (org.get("industry") or "").title() or None,
        "employee_band": _emp_band(org.get("estimated_num_employees")),
        "revenue_band": org.get("annual_revenue_printed"),
        "hq_location": f"{city}, {state}" if city and state else city or None,
        "confidence": 0.9, "raw": org,
    }


def _call_pdl(seed: str) -> Dict[str, Any]:
    """People Data Labs company/enrich — set LEADS_ENRICH_PROVIDER=pdl +
    LEADS_ENRICH_API_KEY. Docs: api.peopledatalabs.com/v5/company/enrich."""
    raw = _http_json(
        f"https://api.peopledatalabs.com/v5/company/enrich"
        f"?website={urllib.parse.quote(seed)}",
        {"Accept": "application/json", "X-Api-Key": ENRICH_API_KEY})
    if raw.get("status") not in (None, 200) or not raw.get("name"):
        return {"matched": False, "source": "pdl", "domain": seed}
    loc = raw.get("location") or {}
    city, region = loc.get("locality"), loc.get("region")
    return {
        "matched": True, "source": "pdl", "domain": seed,
        "company": (raw.get("name") or "").title() or None,
        "website": raw.get("website") or f"https://{seed}",
        "industry": (raw.get("industry") or "").title() or None,
        "employee_band": raw.get("size") or _emp_band(raw.get("employee_count")),
        "revenue_band": raw.get("inferred_revenue"),
        "hq_location": f"{city.title()}, {region.title()}" if city and region else None,
        "confidence": 0.85, "raw": {k: raw.get(k) for k in
                                    ("name", "industry", "size", "website")},
    }


def _call_web(seed: str, company: Optional[str]) -> Dict[str, Any]:
    """Keyless provider: the project's own web_tools (ddgs → Tavily). Real
    external data, no API key — scans result snippets for an industry keyword
    and takes the top result as the website. Low confidence by design; the
    gap-fill contract means it can never overwrite human-entered data."""
    from app.core.web_tools import web_search
    name = company or seed
    results = web_search(f'"{name}" company industry headquarters', max_results=5)
    if not results:
        return {"matched": False, "source": "web", "domain": seed,
                "reason": "no search results (or WEB_SEARCH_ENABLED=0)"}
    text = " ".join(f"{r.get('title', '')} {r.get('snippet', '')}"
                    for r in results).lower()
    industry = next((i for i in _INDUSTRIES if i.lower() in text), None)
    website = None
    for r in results:
        url = r.get("url") or ""
        host = urllib.parse.urlparse(url).netloc.lower()
        # prefer the company's own site over directories/social profiles
        if host and not any(d in host for d in
                            ("linkedin.", "facebook.", "wikipedia.", "crunchbase.",
                             "glassdoor.", "indeed.", "yelp.", "instagram.")):
            website = f"https://{host}"
            break
    return {
        "matched": bool(industry or website),
        "source": "web", "domain": seed, "company": company,
        "website": website,
        "industry": industry,
        "employee_band": None, "revenue_band": None, "hq_location": None,
        "confidence": 0.5,
        "evidence": [{"title": r.get("title"), "url": r.get("url")}
                     for r in results[:3]],
    }


def _call_api(seed: str) -> Dict[str, Any]:
    """Legacy generic adapter (LEADS_ENRICH_PROVIDER=generic or just
    LEADS_ENRICH_API_URL set). ADAPT the response mapping to your provider."""
    q = urllib.parse.quote(seed)
    url = (ENRICH_API_URL.replace("{domain}", q) if "{domain}" in ENRICH_API_URL
           else f"{ENRICH_API_URL}{'&' if '?' in ENRICH_API_URL else '?'}domain={q}")
    headers = {"Accept": "application/json"}
    if ENRICH_API_KEY:
        headers["Authorization"] = f"Bearer {ENRICH_API_KEY}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=ENRICH_TIMEOUT) as r:
        raw = json.loads(r.read().decode("utf-8"))
    return {
        "matched": True,
        "source": "api",
        "domain": seed,
        "website": raw.get("website") or f"https://{seed}",
        "industry": raw.get("industry"),
        "employee_band": raw.get("employee_band") or raw.get("employees"),
        "revenue_band": raw.get("revenue_band") or raw.get("revenue"),
        "hq_location": raw.get("hq_location") or raw.get("location"),
        "confidence": raw.get("confidence", 0.9),
        "raw": raw,
    }
