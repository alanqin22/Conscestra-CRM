"""Store catalog search — the read side behind the agent's product-discovery
answers (recommendations, comparisons, "best X under $Y", "do you have a Z").

Powers the case the single-product Store agent can't: a shopper on the grid or
home page asking to be pointed at the right product. Returns REAL in-stock
products with effective prices (published Promo price, else Retail), so the
agent recommends only from live inventory — never an invented product.

Prices come from product_pricing effective-dated rows (same source as the PDP).
Never raises — returns [] on any failure so a chat turn is never broken.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from app.core.database import get_connection

logger = logging.getLogger("store_catalog")

# Words that carry no product signal — stripped before keyword matching.
_STOP = {
    "best", "good", "great", "cheap", "cheapest", "under", "over", "below",
    "above", "less", "than", "up", "to", "max", "maximum", "min", "minimum",
    "the", "a", "an", "for", "what", "whats", "is", "are", "do", "does", "you",
    "your", "i", "im", "need", "want", "looking", "look", "me", "my", "mine",
    "buy", "purchase", "get", "find", "recommend", "suggest", "which", "one",
    "should", "and", "or", "with", "of", "price", "cost", "budget", "around",
    "about", "some", "any", "have", "sell", "carry", "stock", "in", "on",
    "help", "choose", "pick", "option", "options", "model", "models", "please",
    "can", "could", "would", "there", "that", "this", "it", "something", "device",
}

# Category synonyms — a common noun expands to the words that actually appear in
# product names/descriptions, so "computer" reaches laptops, "phone" reaches
# iPhones, etc.
_SYN = {
    "computer": ["laptop", "macbook", "desktop", "notebook", "chromebook", "pc", "computer"],
    "laptop":   ["laptop", "macbook", "notebook", "chromebook", "thinkpad"],
    "desktop":  ["desktop", "pc", "imac", "tower"],
    "pc":       ["pc", "desktop", "laptop", "computer"],
    "phone":    ["phone", "iphone", "smartphone", "galaxy", "pixel"],
    "smartphone": ["iphone", "smartphone", "galaxy", "pixel", "phone"],
    "tablet":   ["tablet", "ipad", "galaxy tab"],
    "headphones": ["headphone", "headphones", "earbud", "earbuds", "airpods", "earphone"],
    "earbuds":  ["earbud", "earbuds", "airpods", "earphone"],
    "watch":    ["watch", "smartwatch"],
    "tv":       ["tv", "television"],
    "monitor":  ["monitor", "display"],
    "keyboard": ["keyboard"],
    "mouse":    ["mouse"],
    "camera":   ["camera"],
    "speaker":  ["speaker", "soundbar"],
    "console":  ["console", "playstation", "xbox", "nintendo", "switch"],
    "gaming":   ["gaming", "rog", "nvidia", "rtx", "geforce"],
}

_UNDER_RE = re.compile(
    r"(?:under|below|less than|up to|max(?:imum)?|within|<=?)\s*\$?\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE)
_OVER_RE = re.compile(
    r"(?:over|above|more than|at least|min(?:imum)?|>=?|starting at)\s*\$?\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE)
_BARE_PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")


def parse_budget(text: str) -> Dict[str, Optional[float]]:
    """Extract a price ceiling/floor from free text. 'under $2000' → max 2000."""
    def _num(m):
        try:
            return float(m.group(1).replace(",", "")) if m else None
        except (TypeError, ValueError):
            return None
    mx = _num(_UNDER_RE.search(text))
    mn = _num(_OVER_RE.search(text))
    if mx is None and mn is None:
        # A bare "$2000" with no over/under usually means a ceiling ("a $2000 laptop").
        b = _BARE_PRICE_RE.search(text)
        if b:
            mx = _num(b)
    return {"max_price": mx, "min_price": mn}


def _keywords(text: str) -> List[str]:
    toks = [t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _STOP and len(t) > 2 and not t.isdigit()]
    out: List[str] = []
    for t in toks:
        for w in _SYN.get(t, [t]):
            if w not in out:
                out.append(w)
    return out[:10]


def search_products(text: str, max_price: Optional[float] = None,
                    min_price: Optional[float] = None,
                    limit: int = 6) -> List[Dict[str, Any]]:
    """Live in-stock products matching the shopper's terms + budget, priced by
    the same effective (Promo→Retail) rule as the storefront. Never raises."""
    kws = _keywords(text)
    patterns = [f"%{k}%" for k in kws]
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.product_id::text, p.product_name, p.sku,
                           p.stock_quantity, cat.category_name,
                           retail.pv AS retail, promo.pv AS promo,
                           left(coalesce(p.description,''), 200) AS descr
                      FROM products p
                      LEFT JOIN category cat ON cat.category_id = p.category_id
                      LEFT JOIN LATERAL (
                          SELECT price_value pv FROM product_pricing pr
                           WHERE pr.product_id = p.product_id AND pr.price_type='Retail'
                             AND pr.effective_from <= now()
                             AND (pr.effective_to IS NULL OR pr.effective_to > now())
                           ORDER BY pr.effective_from DESC LIMIT 1) retail ON true
                      LEFT JOIN LATERAL (
                          SELECT price_value pv FROM product_pricing pr
                           WHERE pr.product_id = p.product_id AND pr.price_type='Promo'
                             AND pr.effective_from <= now()
                             AND (pr.effective_to IS NULL OR pr.effective_to > now())
                           ORDER BY pr.effective_from DESC LIMIT 1) promo ON true
                     WHERE p.is_active AND p.stock_quantity > 0
                       AND COALESCE(promo.pv, retail.pv) IS NOT NULL
                       AND (%(has_kw)s = false OR
                            p.product_name ILIKE ANY(%(pats)s) OR
                            p.description  ILIKE ANY(%(pats)s) OR
                            cat.category_name ILIKE ANY(%(pats)s))
                       AND (%(maxp)s IS NULL OR COALESCE(promo.pv, retail.pv) <= %(maxp)s)
                       AND (%(minp)s IS NULL OR COALESCE(promo.pv, retail.pv) >= %(minp)s)
                     ORDER BY COALESCE(promo.pv, retail.pv) DESC
                     LIMIT %(lim)s
                    """,
                    {"has_kw": bool(patterns), "pats": patterns or [""],
                     "maxp": max_price, "minp": min_price, "lim": limit})
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[store_catalog] search failed: {exc}")
        return []

    out = []
    for r in rows:
        retail = float(r["retail"]) if r["retail"] is not None else None
        promo = float(r["promo"]) if r["promo"] is not None else None
        price = promo if promo is not None else retail
        out.append({
            "product_id": r["product_id"], "name": r["product_name"],
            "sku": r["sku"], "stock": r["stock_quantity"],
            "category": r["category_name"], "price": price,
            "list_price": retail, "on_sale": bool(promo is not None and retail and promo < retail),
            "description": (r["descr"] or "").strip(),
        })
    return out


def candidates_block(products: List[Dict[str, Any]]) -> str:
    """Compact, grounded product list for the recommendation prompt."""
    lines = []
    for p in products:
        price = f"${p['price']:,.2f}" if p.get("price") is not None else "—"
        sale = (f" (on sale from ${p['list_price']:,.2f})"
                if p.get("on_sale") and p.get("list_price") else "")
        stk = f"{p['stock']} in stock" if isinstance(p.get("stock"), int) else ""
        lines.append(f"- {p['name']} [{p.get('sku','')}] — {price}{sale}"
                     + (f", {stk}" if stk else "")
                     + (f", {p['category']}" if p.get("category") else "")
                     + (f". {p['description'][:120]}" if p.get("description") else ""))
    return "\n".join(lines)
