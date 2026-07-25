"""One-command demo / sample-data seed (Platform blindspot P6).

The "Show me intelligence" half of the Time-to-Value Engine (P1 import → P2
readiness → **P6 see it work**). An empty CRM can't demonstrate its agents; this
takes a fresh org to a living sample business in one call, deliberately shaped so
the intelligence surfaces light up immediately — stalled deals, slipped pipeline,
a real win rate, leads to work.

Design — reuse, self-contained, removable:
  * The sample BOOK OF BUSINESS (accounts / contacts / leads) is loaded through
    the SAME governed P1 importer (`data_import.commit`) — dedupe-safe, so
    re-seeding never duplicates. This also demos the import path itself.
  * Sample OPPORTUNITIES are inserted directly on the demo accounts with varied
    stages/dates (some stalled, some slipped, some won/lost) so anomaly
    detection + win-rate + pipeline all have something to show. Tagged
    `is_synthetic=true`.
  * `seed()` returns an intelligence HEADLINE computed from the demo data — the
    "this understands my business" moment.
  * Everything demo is identifiable (accounts by the fictional name set, people
    by the @demo domain, opps by is_synthetic on demo accounts) so `clear()`
    fully removes it. Admin-gated.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("demo")

DEMO_DOMAIN = "demo.conscestra.local"

# Classic fictional company names — recognizable as sample data, collision-safe.
DEMO_ACCOUNTS = [
    {"name": "Northwind Traders",         "industry": "Retail",        "city": "Toronto",   "street": "120 King St W"},
    {"name": "Contoso Manufacturing",     "industry": "Manufacturing", "city": "Hamilton",  "street": "88 Steel Rd"},
    {"name": "Fabrikam Logistics",        "industry": "Logistics",     "city": "Mississauga","street": "5 Airport Blvd"},
    {"name": "Adventure Works Retail",    "industry": "Retail",        "city": "Vancouver", "street": "700 Robson St"},
    {"name": "Tailspin Toys",             "industry": "Consumer Goods","city": "Calgary",   "street": "22 Stephen Ave"},
    {"name": "Wingtip Software",          "industry": "Technology",    "city": "Ottawa",    "street": "150 Elgin St"},
    {"name": "Proseware Health",          "industry": "Healthcare",    "city": "Toronto",   "street": "400 University Ave"},
    {"name": "Litware Financial",         "industry": "Finance",       "city": "Toronto",   "street": "200 Bay St"},
    {"name": "Fourth Coffee",             "industry": "Hospitality",   "city": "Montreal",  "street": "1200 Rue Sainte-Catherine"},
    {"name": "Graphic Design Institute",  "industry": "Education",     "city": "Toronto",   "street": "60 Front St"},
]
_DEMO_NAMES = [a["name"] for a in DEMO_ACCOUNTS]

DEMO_CONTACTS = [
    ("Maria", "Nguyen"), ("James", "Okafor"), ("Priya", "Sharma"), ("Liam", "Tremblay"),
    ("Sofia", "Rossi"), ("Chen", "Wei"), ("Aisha", "Khan"), ("Noah", "Bergeron"),
    ("Elena", "Petrov"), ("David", "Kim"), ("Fatima", "Hassan"), ("Lucas", "Silva"),
    ("Hana", "Sato"), ("Omar", "Farah"),
]

DEMO_LEADS = [
    ("Grace", "Adeyemi", "Brightway Dental",   "Website"),
    ("Marcus", "Bauer",  "Summit Contracting",  "Referral"),
    ("Yuki", "Tanaka",   "Harbour Freight Co",  "Trade Show"),
    ("Isabella", "Moreau","Lakeside Realty",    "Google Ads"),
    ("Kwame", "Mensah",  "Apex Fitness",        "LinkedIn"),
    ("Nina", "Kowalski", "Verdant Landscaping", "Cold Call"),
    ("Raj", "Patel",     "Nimbus Cloud Svcs",   "Website"),
    ("Amara", "Diallo",  "Copper Kettle Cafe",  "Newsletter"),
    ("Tomas", "Novak",   "Ironclad Security",   "Referral"),
    ("Leila", "Ahmadi",  "Petal & Stem Florist","Website"),
    ("Sven", "Larsson",  "Northstar Freight",   "Trade Show"),
    ("Rosa", "Gomez",    "Sunrise Bakery",      "Walk-in"),
]


def _conn():
    return get_connection()


# ── Sample opportunities: shaped to light up the intelligence surfaces ───────
def _opportunity_specs() -> List[Dict[str, Any]]:
    today = date.today()
    now = datetime.now()
    S: List[Dict[str, Any]] = []

    def add(name, amount, stage, status, prob, close_off, updated_off):
        S.append({"name": name, "amount": amount, "stage": stage, "status": status,
                  "probability": prob, "close_date": today + timedelta(days=close_off),
                  "updated_at": now - timedelta(days=updated_off)})

    # Stalled: open but untouched ≥ 20 days (trips the stalled-deals anomaly)
    add("Enterprise rollout — Phase 2", 82000, "proposal",    "open", 60,  30,  22)
    add("Annual renewal expansion",     45000, "negotiation", "open", 70,  20,  27)
    add("Managed services upgrade",     61000, "proposal",    "open", 55,  40,  24)
    add("Multi-site deployment",        120000,"qualification","open",40,  50,  31)
    # Slipped: open but past close date (revenue at risk)
    add("Q-close hardware refresh",     38000, "negotiation", "open", 75, -12,   3)
    add("Support contract (lapsed)",    27000, "proposal",    "open", 65, -25,   5)
    add("Pilot-to-production",          54000, "negotiation", "open", 80,  -6,   2)
    # Healthy open pipeline
    add("New logo — platform license",  95000, "qualification","open",35,  45,   1)
    add("Add-on modules",               22000, "proposal",    "open", 60,  25,   2)
    add("Onboarding services",          18000, "prospecting", "open", 25,  60,   1)
    add("Cross-sell — analytics",       33000, "qualification","open",30,  38,   4)
    # Recently won (win rate + booked revenue)
    add("Starter package",              15000, "closed_won",  "closed_won", 100, -3, 3)
    add("Team expansion seats",         28000, "closed_won",  "closed_won", 100, -5, 5)
    add("Renewal — 2yr",                72000, "closed_won",  "closed_won", 100, -2, 2)
    # Recently lost (win rate denominator)
    add("Competitive replacement",      40000, "closed_lost", "closed_lost", 0, -4, 4)
    add("Budget freeze",                31000, "closed_lost", "closed_lost", 0, -6, 6)
    return S


# ── Status / detection helpers ───────────────────────────────────────────────
def _demo_account_ids(cur) -> List[str]:
    cur.execute("SELECT account_id FROM accounts WHERE account_name = ANY(%s)", (_DEMO_NAMES,))
    return [r[0] for r in cur.fetchall()]


def status() -> Dict[str, Any]:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            ids = _demo_account_ids(cur)
            cur.execute("SELECT count(*) FROM contacts WHERE lower(email) LIKE %s", (f"%@{DEMO_DOMAIN}",))
            contacts = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM leads WHERE lower(email) LIKE %s", (f"%@{DEMO_DOMAIN}",))
            leads = cur.fetchone()[0]
            opps = 0
            if ids:
                cur.execute("SELECT count(*) FROM opportunities WHERE account_id = ANY(%s::uuid[]) AND is_synthetic", (ids,))
                opps = cur.fetchone()[0]
    finally:
        conn.close()
    return {"present": bool(ids or contacts or leads),
            "accounts": len(ids), "contacts": contacts, "leads": leads, "opportunities": opps}


def _headline(cur, ids: List[str]) -> str:
    if not ids:
        return "No demo opportunities."
    cur.execute(
        """SELECT
             COALESCE(SUM(amount) FILTER (WHERE status='open'),0)                                  AS open_amt,
             count(*) FILTER (WHERE status='open')                                                 AS open_n,
             count(*) FILTER (WHERE status='open' AND updated_at < now()-interval '5 days')        AS stalled,
             count(*) FILTER (WHERE status='open' AND close_date < CURRENT_DATE)                    AS slipped,
             count(*) FILTER (WHERE status='closed_won')                                            AS won,
             count(*) FILTER (WHERE status IN ('closed_won','closed_lost'))                         AS decided
           FROM opportunities WHERE account_id = ANY(%s::uuid[]) AND is_synthetic""", (ids,))
    r = cur.fetchone()
    open_amt, open_n, stalled, slipped, won, decided = r
    wr = round(100.0 * won / decided) if decided else 0
    cur.execute("SELECT count(*) FROM leads WHERE lower(email) LIKE %s", (f"%@{DEMO_DOMAIN}",))
    leads = cur.fetchone()[0]
    return (f"Your demo company has ${float(open_amt):,.0f} in open pipeline across "
            f"{open_n} deals — {stalled} stalled with no movement, {slipped} slipped "
            f"past their close date, a {wr}% win rate, and {leads} new leads to work.")


# ── Seed / clear ─────────────────────────────────────────────────────────────
def seed() -> Dict[str, Any]:
    """Load the sample book via the governed importer + add shaped opportunities.
    Idempotent: dedupe-safe imports; opportunities only added once."""
    from app.core import data_import

    # 1) Book of business through the P1 governed importer (dedupe-safe).
    acc_csv = "Company Name,Street,City,Country,Industry,Email\n" + "\n".join(
        f'{a["name"]},{a["street"]},{a["city"]},Canada,{a["industry"]},'
        f'info@{a["name"].lower().replace(" ", "").replace("&","and")[:18]}.{DEMO_DOMAIN}'
        for a in DEMO_ACCOUNTS)
    con_csv = "First Name,Last Name,Email\n" + "\n".join(
        f"{f},{l},{f.lower()}.{l.lower()}@{DEMO_DOMAIN}" for f, l in DEMO_CONTACTS)
    led_csv = "First,Last,Email,Company,Source\n" + "\n".join(
        f"{f},{l},{f.lower()}.{l.lower()}@{DEMO_DOMAIN},{co},{src}" for f, l, co, src in DEMO_LEADS)

    imported = {
        "accounts": data_import.commit("accounts", acc_csv)["counts"],
        "contacts": data_import.commit("contacts", con_csv)["counts"],
        "leads":    data_import.commit("leads", led_csv)["counts"],
    }

    # 2) Opportunities on the demo accounts (only if not already seeded).
    conn = _conn()
    opp_created = 0
    try:
        with conn.cursor() as cur:
            ids = _demo_account_ids(cur)
            if not ids:
                return {"ok": False, "error": "demo accounts did not import — check the importer"}
            cur.execute("SELECT count(*) FROM opportunities WHERE account_id = ANY(%s::uuid[]) AND is_synthetic", (ids,))
            already = cur.fetchone()[0]
            if not already:
                specs = _opportunity_specs()
                for i, s in enumerate(specs):
                    aid = ids[i % len(ids)]
                    cur.execute(
                        """INSERT INTO opportunities
                             (account_id, name, amount, stage, status, probability,
                              close_date, created_at, updated_at, is_synthetic)
                           VALUES (%s,%s,%s,%s,%s,%s,%s, now()-interval '45 days', %s, true)""",
                        (aid, s["name"], s["amount"], s["stage"], s["status"],
                         s["probability"], s["close_date"], s["updated_at"]))
                    opp_created += 1
            conn.commit()
            headline = _headline(cur, ids)
    finally:
        conn.close()

    return {"ok": True, "imported": imported, "opportunities_created": opp_created,
            "headline": headline,
            "note": ("Demo business seeded. Open the Analytics anomalies, the executive "
                     "briefing, or ask \"any anomalies?\" to see it come alive. "
                     "Call POST /demo/clear to remove it.")}


def clear() -> Dict[str, Any]:
    """Remove all demo data (opps → people → addresses → accounts). FK-safe order."""
    conn = _conn()
    counts = {"opportunities": 0, "contacts": 0, "leads": 0, "accounts": 0}
    try:
        with conn.cursor() as cur:
            ids = _demo_account_ids(cur)
            if ids:
                cur.execute("DELETE FROM opportunities WHERE account_id = ANY(%s::uuid[]) AND is_synthetic", (ids,))
                counts["opportunities"] = cur.rowcount
            cur.execute("DELETE FROM contacts WHERE lower(email) LIKE %s", (f"%@{DEMO_DOMAIN}",))
            counts["contacts"] = cur.rowcount
            cur.execute("DELETE FROM leads WHERE lower(email) LIKE %s", (f"%@{DEMO_DOMAIN}",))
            counts["leads"] = cur.rowcount
            if ids:
                cur.execute("DELETE FROM addresses WHERE parent_type='account' AND parent_id = ANY(%s::uuid[])", (ids,))
                cur.execute("DELETE FROM accounts WHERE account_id = ANY(%s::uuid[])", (ids,))
                counts["accounts"] = cur.rowcount
            conn.commit()
    finally:
        conn.close()
    return {"ok": True, "removed": counts}


# ── Router (admin) ───────────────────────────────────────────────────────────
router = APIRouter(tags=["demo"])


@router.get("/demo/status")
def demo_status():
    return status()


@router.post("/demo/seed")
def demo_seed():
    """Populate a realistic sample business + shaped opportunities (idempotent)."""
    return seed()


@router.post("/demo/clear")
def demo_clear():
    """Remove all demo/sample data."""
    return clear()
