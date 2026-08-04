"""Semantic index over the CRM's unstructured text (audit finding #2).

WHAT WAS BROKEN: the only embedded corpus was 65 knowledge-base articles. Every
word the customer actually produced — ~6,500 activity notes, 480 case comments,
120 cases, 137 conversation messages — was reachable only by exact keyword or by
`ORDER BY created_at DESC`. `customer_memory.recall` was literally recency:

    SELECT ... FROM interaction_memories ORDER BY created_at DESC LIMIT 3

So "what has this customer complained about regarding pricing?" found nothing
unless it happened in the last three interactions. That is Recent Chat History,
not Customer Memory.

    sources ──extract()──▶ (text, scope, visibility) ──embed──▶ content_embeddings
                                                                      │
                                            search(query, scope, audience)
                                                                      ▼
                                             customer_memory.recall / /search/content

──────────────────────────────────────────────────────────────────────────────
THREE DESIGN DECISIONS, EACH FORCED BY A FACT
──────────────────────────────────────────────────────────────────────────────
1. NO pgvector. It is not installed and not guaranteed on the deploy target
   (pg_available_extensions lists only pg_trgm). Vectors are float32 bytea and
   ranking is a numpy matrix product in `embeddings.rank`. At this corpus size a
   scoped query ranks tens-to-hundreds of candidates — microseconds. If the
   corpus grows past ~100k rows, pgvector + HNSW becomes the right answer and
   only `search()` changes.

2. NO PROCESS-LOCAL VECTOR CACHE. semantic.py keeps the whole KB in a module
   global, which is fine for 65 rows but wrong here: N replicas × N copies, each
   with its own staleness clock, and a cold worker silently serving a partial
   index. Instead every search FETCHES its candidates scoped by the SQL indexes.
   Always fresh, identical across replicas, no invalidation logic to get wrong.

3. VISIBILITY IS FAIL-CLOSED. This index contains internal case comments and
   internal conversations. `search()` REQUIRES an explicit audience; 'customer'
   sees only visibility='customer' rows AND only within its own scope. An
   unrecognised audience is treated as the most restrictive, not the most
   permissive. Serving an internal note to a customer-facing agent would re-open
   the leak the reach_invariant closed in U1.

Everything degrades to empty rather than raising: no API key, no table, no
numpy — the caller falls back to keyword/recency retrieval and the product still
works.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core import embeddings as E
from app.core.database import get_connection

logger = logging.getLogger("content_index")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("CONTENT_INDEX_ENABLED", "1")
# Below this, hits are noise rather than weak signal. Tuned against the live
# corpus: genuine topical matches land ~0.40-0.55, while an off-topic query
# ("cancel my subscription" against a corpus with no cancellations) tops out
# ~0.34. Returning nothing is better than returning a confident irrelevance —
# the caller still has keyword and recency retrieval.
MIN_SIM = float(os.getenv("CONTENT_INDEX_MIN_SIM", "0.36"))
# CRM text is heavily templated ("Order SO-… has been shipped. Follow up with…"
# ×400). Without suppression, one template floods every result set and buries
# the distinct records that actually answer the question.
DEDUPE_PREFIX = int(os.getenv("CONTENT_INDEX_DEDUPE_PREFIX", "60"))

_NUM_RE = re.compile(r"\d+")
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


# ── Speech acts ──────────────────────────────────────────────────────────────
# Embeddings encode TOPIC, not INTENT. Measured: "what commitments did we make?"
# returned "Requested additional information from customer." at 0.517 — a request
# and a commitment about the same subject are semantically adjacent, so no
# similarity threshold can separate them. The ACT has to be classified, not
# retrieved.
#
# DETERMINISTIC ON PURPOSE. An LLM classifier reading customer-authored text is
# itself an injection target: a case comment could be written to label itself a
# commitment. Patterns cannot be argued with.
COMMITMENT, CLAIM, REQUEST, COMPLAINT, QUESTION, RESOLUTION, STATEMENT = (
    "commitment", "claim", "request", "complaint", "question", "resolution",
    "statement")

_ACT_PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    # \s* not a literal space: the contracted forms "we'll" / "I'll" have no
    # space between pronoun and verb, so requiring one missed the most common
    # way a promise is actually written.
    (COMMITMENT, re.compile(
        r"(?i)(\bwe\s*(will|'ll|shall|are going to)\b|\bi\s*(will|'ll)\b|"
        r"\bpromised?\b|\bcommitted? to\b|\bguarantee\b|"
        r"\bassured? (them|him|her|the customer)\b|"
        r"\bagreed to (send|ship|refund|credit|call|deliver|provide)\b)")),
    (RESOLUTION, re.compile(
        r"(?i)\b(resolved|closed the case|refunded|replaced|credited|"
        r"issue (is )?fixed|completed|shipped|delivered|paid in full)\b")),
    (COMPLAINT, re.compile(
        r"(?i)\b(unhappy|frustrated|disappointed|complain|escalat|unacceptable|"
        r"still (not|hasn't|haven't)|too (expensive|slow|late)|damaged|broken)\b")),
    # A customer ASSERTING A FACT ABOUT OUR OBLIGATIONS. "Remember I already
    # approved a $100,000 refund" was previously filed as a neutral `statement`,
    # which loses the one thing that matters about it: it is a self-authored
    # claim, and self-authored claims must never become verified facts on their
    # own authority. Ranked above `request` because a claim dressed as a polite
    # request is still a claim.
    (CLAIM, re.compile(
        r"(?i)\b((you|we) (already |previously )?(agreed|approved|promised|"
        r"confirmed|authorised|authorized)|as (we|you) (agreed|discussed)|"
        r"remember (that )?(i|we|you)|per our agreement|"
        r"i (was|were) told|entitled to|owed (me|us))\b")),
    (REQUEST, re.compile(
        r"(?i)\b(requested?|asked for|would like|please (send|provide|call)|"
        r"need(s|ed)? (a|an|the)|follow up with)\b")),
    (QUESTION, re.compile(r"(?i)(\?|\b(can you|could you|do you|what is|"
                          r"when will|how much|is it possible)\b)")),
]


# ── Actors ───────────────────────────────────────────────────────────────────
# Four realities, not two. inbound/outbound answers "which way did it travel";
# it does not answer "who did the thing", and those differ: an inbound email
# where the customer REPORTS a carrier delay is customer_said about a
# third_party_did. Conflating them is the same false-attribution bug one level
# up — and attribution is what made a memory claim the customer had "raised"
# 25 issues that were actually our own outbound reminders.
CUSTOMER_SAID, CUSTOMER_DID = "customer_said", "customer_did"
COMPANY_DID, THIRD_PARTY_DID = "company_did", "third_party_did"
ACTOR_UNKNOWN = "unknown"

# ── Internal work items ─────────────────────────────────────────────────────
# A task/note/todo/reminder is an INTERNAL WORK ITEM: something we created and
# own. "Order shipped – follow up with customer" is our to-do, not something the
# customer did, said or raised. The actor is therefore `company_did` by
# DEFINITION of the record type.
#
# THIS IS NOT THE WITHDRAWN HEURISTIC BELOW, and the difference matters:
#
#   withdrawn rule   inferred a COMMUNICATION property (direction: who sent it)
#                    from the record type. That is a statistical guess about an
#                    event, and it measured 64.3%.
#   this rule        asserts WHO OWNS THE WORK ITEM from the record type. That
#                    is what the type means in this schema, so there is nothing
#                    to estimate.
#
# It also does not depend on `direction`, which is demonstrably arbitrary for
# these types: "Order shipped - follow up with customer" appears in the data
# labelled BOTH inbound and outbound, as do "Welcome / Account created" and
# "First contact / Intro". A field that carries both labels for one logical
# event is noise, and the earlier 53.3% "precision" figure was measured against
# that noise rather than against ground truth.
#
# SCHEMA-DEPENDENT. If a task in this CRM can be created BY a customer action
# rather than FOR one, this rule is wrong and must be removed.
_INTERNAL_WORK_ITEM_TYPES = {"task", "note", "todo", "reminder"}

# WITHDRAWN. This rule claimed an activity's TYPE implies who acted, and it
# looked like free coverage: it attributed 5,595 records (70% of the corpus) that
# had no direction column. Held-out validation against the 1,888 rows that DO
# have an observed direction says otherwise —
#
#     agrees with observed direction   283  (15.0%)
#     disagrees                        157  ( 8.3%)
#     precision when it commits             64.3%
#
# 64% is barely better than guessing between two classes, and it was being
# applied at scale to produce statements about customers. Whether the fault is
# the rule or the seed data's direction column, the conclusion is the same and
# it is the project's own stated principle: an unattributed memory says less, a
# WRONGLY attributed one says something false about a person. So this abstains.
#
# To reinstate: label a sample by hand, and require >=95% precision on held-out
# rows before trusting it for anything an agent may state.
_COMPANY_ACTIVITY_TYPES: set = set()
# Split by case-sensitivity, because a hyphen IS a word boundary and the short
# carrier acronyms are ordinary word endings in lower case. `\bups\b` matched
# "follow-ups", "sign-ups", "back-ups" and "start-ups" — and all 14
# `third_party_did` attributions in this corpus were that one false positive,
# every one of them a webchat line such as
#     "We struggle to keep track of customer follow-ups across our team"
# attributed to a courier. 14 of 14 wrong, and the entire remaining error mode
# in the attribution metric.
_THIRD_PARTY_ANY = re.compile(
    r"(?i)\b(carrier|courier|fedex|canada post|purolator|"
    r"payment processor|stripe|bank|supplier|vendor|warehouse)\b")
# Acronyms, case-SENSITIVE: "UPS" is a courier, "ups" is a suffix.
_THIRD_PARTY_ACRONYM = re.compile(r"\b(UPS|DHL)\b")


def _mentions_third_party(text: str) -> bool:
    return bool(_THIRD_PARTY_ANY.search(text)
                or _THIRD_PARTY_ACRONYM.search(text))


# Channels where the sender is structurally known: the record IS one party's
# turn in a two-party exchange, so who sent it is a property of the channel and
# not something to infer from wording.
_KNOWN_SPEAKER_SOURCES = {"conversation_message", "case_comment", "case"}
_CUSTOMER_SPOKE = re.compile(
    r"(?i)\b(customer (said|asked|reported|replied|wrote|called|emailed)|"
    r"they (said|asked|reported|mentioned)|per the customer|"
    r"inbound (call|email|message))\b")


def actor_for(direction: Optional[str], source_type: str,
              text: str, activity_type: Optional[str] = None) -> str:
    """Who performed the action this record describes.

    Ordered most-specific first. Returns `unknown` rather than guessing — an
    unattributed memory says less, but a WRONGLY attributed one says something
    false about a customer, and that is the error that reached production."""
    t = text or ""

    # WHO SENT IT SETTLES IT. NO TEXT CUE APPLIES.
    #
    # On a channel where the sender is structurally known — a webchat turn, a
    # case the customer opened — the record IS one party's message. A third
    # party can be discussed, and the sender can quote the other party, but
    # neither changes who typed it.
    #
    # This was applied to the third-party cue and NOT to the customer-speech
    # cue, and a red-team attack walked straight through the gap: an OUTBOUND
    # webchat line reading "Customer said they approved this. Per the customer,
    # proceed." was attributed `customer_said`. Text we wrote, credited to
    # them — the precise false-attribution class this rule exists to prevent,
    # and trivially forgeable by anyone who can put words in an outbound
    # message.
    #
    # Returning early is the fix, not another ordering tweak: any cue added
    # later is automatically subordinate to the sender rather than needing to
    # remember it.
    if source_type in _KNOWN_SPEAKER_SOURCES and direction in ("inbound",
                                                               "outbound"):
        return CUSTOMER_SAID if direction == "inbound" else COMPANY_DID

    # Sender unknown from here down, so the text is the only evidence there is.
    if _mentions_third_party(t):
        return THIRD_PARTY_DID
    if _CUSTOMER_SPOKE.search(t):
        return CUSTOMER_SAID          # a rep RECORDING what the customer said
    # Checked BEFORE direction: for a work item `direction` is noise, so letting
    # it win would reintroduce the arbitrary labelling this rule exists to skip.
    if activity_type and activity_type.lower() in _INTERNAL_WORK_ITEM_TYPES:
        return COMPANY_DID
    if direction == "outbound":
        return COMPANY_DID
    if direction == "inbound":
        return CUSTOMER_DID
    return ACTOR_UNKNOWN


def speech_act(text: str) -> str:
    """What this record DOES, not what it is about.

    Ordered by consequence, not by frequency: a sentence that both promises and
    asks is classified as a COMMITMENT, because mistaking a promise for a
    question is the costlier error. Falls back to `statement`."""
    t = (text or "").strip()
    if not t:
        return STATEMENT
    for act, pattern in _ACT_PATTERNS:
        if pattern.search(t):
            return act
    return STATEMENT


def template_fingerprint(text: str, width: int = DEDUPE_PREFIX) -> str:
    """Collapse a record to the TEMPLATE it was generated from.

    CRM text is mass-produced ("Order SO-2026-100518 has been shipped. Follow up
    with…" × 400), and near-identical records crowd out the distinct ones that
    actually answer a question. Two normalizations, each earned from a real
    miss on the live corpus:
      • digit runs → '#'  — the record id sits INSIDE the compared prefix, so a
        raw comparison sees 400 different strings.
      • punctuation dropped — the same template exists with '-' and with '—',
        which otherwise reads as two distinct templates."""
    s = _PUNCT_RE.sub(" ", _NUM_RE.sub("#", (text or "").lower()))
    return _WS_RE.sub(" ", s).strip()[:width]
BATCH = int(os.getenv("CONTENT_INDEX_BATCH", "128"))      # embedded per pass
MIN_CHARS = int(os.getenv("CONTENT_INDEX_MIN_CHARS", "25"))
MAX_CANDIDATES = int(os.getenv("CONTENT_INDEX_MAX_CANDIDATES", "4000"))

# Last observed search coverage, for the observability surface. A search that
# ranked every matching row is complete; one that hit the cap ranked only the
# newest slice, and the ratio says how much of the corpus it could reach.
_LAST_COVERAGE: Dict[str, Any] = {"searches": 0, "truncated": 0,
                                  "last_ratio": None, "last_matched": None}


def _record_truncation(considered: int, matched: int) -> None:
    _LAST_COVERAGE["truncated"] += 1
    _LAST_COVERAGE["last_matched"] = matched
    _LAST_COVERAGE["last_ratio"] = round(considered / matched, 4) if matched else None
    if matched and considered / matched < 0.5:
        logger.warning(
            f"[content_index] search ranked {considered} of {matched} matching "
            f"records ({considered/matched:.0%}) — results are drawn from the "
            f"most recent slice only, and better matches may exist outside it")


def search_coverage() -> Dict[str, Any]:
    """What fraction of matching records recent searches could actually rank."""
    return dict(_LAST_COVERAGE)

INTERNAL, CUSTOMER = "internal", "customer"


# ============================================================================
# SOURCES
#
# Each source declares the SQL that produces indexable rows. Every query yields
# the SAME shape so the indexer is source-agnostic:
#
#   source_id, text, account_id, contact_id, opportunity_id, party_key,
#   visibility, occurred_at
#
# `visibility` is decided IN SQL, next to the column that determines it, so the
# classification can't drift away from the data it describes.
# ============================================================================

SOURCES: Dict[str, Dict[str, Any]] = {
    # Rep-authored call/meeting/email notes. The single largest corpus and the
    # densest record of what customers actually said. Internal: these are staff
    # notes about a customer, not text the customer may read back.
    "activity": {
        "label": "Activity note",
        "sql": """
            SELECT a.activity_id::text,
                   concat_ws(' — ', NULLIF(a.subject,''), NULLIF(a.description,'')),
                   a.account_id, a.contact_id, a.opportunity_id,
                   CASE WHEN a.contact_id IS NOT NULL THEN 'contact:'||a.contact_id::text
                        WHEN a.account_id IS NOT NULL THEN 'account:'||a.account_id::text END,
                   'internal',
                   COALESCE(a.completed_at, a.start_at, a.due_at),
                   a.direction,
                   a.type,
                   -- The business object this record is ABOUT.
                   -- Two records with the same wording about the
                   -- same order are one occasion logged twice.
                   CASE WHEN a.related_type IS NOT NULL
                         AND a.related_id IS NOT NULL
                        THEN concat(a.related_type, ':', a.related_id::text)
                   END
            FROM activities a
            WHERE length(concat_ws(' ', a.subject, a.description)) >= %(min_chars)s
              -- SYSTEM BOOKKEEPING IS NOT A CUSTOMER INTERACTION.
              --
              -- Found by reading evidence during the abandoned labelling round:
              -- the memory "General came up 2 times on 2026-01-06" was built
              -- from two rows reading
              --     "Lead imported: Ethan Wong - Lead created during legacy
              --      data import."
              -- That is a migration receipt. Nothing happened between us and
              -- the customer, and no rep would act on it.
              --
              -- These records will STILL BE HERE when real customers arrive,
              -- so this noise survives the switch to real data rather than
              -- being replaced by it.
              --
              -- Same class as _INTERNAL_WORK_ITEM_TYPES: a claim about what a
              -- record IS, not a statistical guess about what it probably
              -- means. Each pattern below is a system-generated string with no
              -- human author.
              AND a.description NOT LIKE 'Lead created during legacy data import%%'
              AND COALESCE(a.description, '') <> 'General activity logged in CRM'
              AND a.subject NOT LIKE 'Lead imported:%%'
              -- Lifecycle bookkeeping, written TWICE for one event (once
              -- against the lead, once against the account) and carrying no
              -- date. It produced the dateless "General came up 2 times."
              -- A conversion is a real business event; it is not an
              -- interaction with the customer, and no rep acts on the receipt.
              AND a.subject NOT LIKE 'Lead converted:%%'
              AND a.subject NOT LIKE 'Converted from lead:%%'
        """,
    },
    # The customer's own problem statement. Customer-visible: they wrote it and
    # can see it in the portal.
    "case": {
        "label": "Case",
        "sql": """
            SELECT c.case_id::text,
                   concat_ws(' — ', NULLIF(c.subject,''), NULLIF(c.description,'')),
                   c.account_id, c.contact_id, NULL::uuid,
                   CASE WHEN c.contact_id IS NOT NULL THEN 'contact:'||c.contact_id::text
                        WHEN c.account_id IS NOT NULL THEN 'account:'||c.account_id::text END,
                   'customer',
                   c.created_at,
                   'inbound',
                   NULL,
                   NULL
            FROM cases c
            WHERE length(concat_ws(' ', c.subject, c.description)) >= %(min_chars)s
        """,
    },
    # is_internal decides visibility — the flag already exists and already means
    # exactly this, so the index inherits the case module's own judgement rather
    # than inventing a second policy.
    "case_comment": {
        "label": "Case comment",
        "sql": """
            SELECT cc.case_comment_id::text,
                   cc.comment,
                   c.account_id, c.contact_id, NULL::uuid,
                   CASE WHEN c.contact_id IS NOT NULL THEN 'contact:'||c.contact_id::text
                        WHEN c.account_id IS NOT NULL THEN 'account:'||c.account_id::text END,
                   CASE WHEN COALESCE(cc.is_internal,true) THEN 'internal' ELSE 'customer' END,
                   cc.created_at,
                   NULL, NULL,
                   concat('case:', cc.case_id::text)
            FROM case_comments cc
            JOIN cases c ON c.case_id = cc.case_id
            WHERE length(COALESCE(cc.comment,'')) >= %(min_chars)s
        """,
    },
    # What was actually said, on any channel. An external conversation is
    # customer-visible (they were a party to it); internal-scope threads are not.
    "conversation_message": {
        "label": "Conversation message",
        "sql": """
            SELECT m.message_id::text,
                   m.body,
                   cv.account_id,
                   CASE WHEN cv.party_type='contact' THEN cv.party_id::uuid END,
                   NULL::uuid,
                   CASE WHEN cv.party_type IS NOT NULL AND cv.party_id IS NOT NULL
                        THEN cv.party_type||':'||cv.party_id::text END,
                   CASE WHEN cv.scope='external' THEN 'customer' ELSE 'internal' END,
                   m.created_at,
                   m.direction,
                   NULL,
                   NULL
            FROM conversation_messages m
            JOIN conversations cv ON cv.conversation_id = m.conversation_id
            WHERE length(COALESCE(m.body,'')) >= %(min_chars)s
        """,
    },
    # AI-distilled cross-channel summaries. Internal: an inference about the
    # customer, not their words — see the provenance envelope's reasoning.
    "interaction_memory": {
        "label": "Interaction memory",
        "sql": """
            SELECT im.memory_id::text,
                   concat_ws(' — ', NULLIF(im.intent,''), im.summary),
                   NULL::uuid,
                   CASE WHEN im.entity_type='contact' THEN im.entity_id::uuid END,
                   NULL::uuid,
                   im.entity_type||':'||im.entity_id::text,
                   'internal',
                   im.created_at,
                   'inbound',
                   NULL,
                   NULL
            FROM interaction_memories im
            WHERE length(COALESCE(im.summary,'')) >= %(min_chars)s
        """,
    },
}

_COLS = ("source_id", "text", "account_id", "contact_id", "opportunity_id",
         "party_key", "visibility", "occurred_at", "direction", "activity_type",
         "parent_key")


def _fetch_source(cur, source_type: str) -> List[Dict[str, Any]]:
    spec = SOURCES[source_type]
    try:
        cur.execute(spec["sql"], {"min_chars": MIN_CHARS})
    except Exception as exc:
        cur.connection.rollback()
        logger.info(f"[content_index] source '{source_type}' unavailable: "
                    f"{str(exc).splitlines()[0][:120]}")
        return []
    return [dict(zip(_COLS, r)) for r in cur.fetchall()]


# ============================================================================
# INDEXING
# ============================================================================

def reindex(source_types: Optional[List[str]] = None,
            limit: int = BATCH, force: bool = False) -> Dict[str, Any]:
    """Bring the index up to date, at most `limit` embeddings per call.

    Idempotent and resumable: a row is re-embedded only when its
    (content_hash, model, dims) differs from current, so re-running is cheap and
    a model change re-indexes everything without any manual step. Rows whose
    source record vanished are deleted."""
    if not ENABLED:
        return {"ok": False, "reason": "disabled"}

    types = [t for t in (source_types or list(SOURCES)) if t in SOURCES]
    out: Dict[str, Any] = {"ok": True, "model": E.MODEL, "dims": E.DIMS,
                           "embedded": 0, "deleted": 0, "pending": 0,
                           "by_source": {}}
    budget = max(int(limit), 1)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for st in types:
                rows = _fetch_source(cur, st)
                if not rows:
                    continue

                cur.execute("SELECT source_id, content_hash, model, dims, "
                            "       visibility, account_id, contact_id, "
                            "       opportunity_id, party_key "
                            "FROM content_embeddings WHERE source_type=%s", (st,))
                stored, stored_meta = {}, {}
                for r in cur.fetchall():
                    stored[r[0]] = (r[1], r[2], int(r[3]))
                    stored_meta[r[0]] = (r[4], r[5], r[6], r[7], r[8])

                live = {r["source_id"] for r in rows}
                gone = [sid for sid in stored if sid not in live]
                if gone:
                    # NAME THIS WORK. Pruning index entries whose source row is
                    # gone is a known operation, and leaving it in the
                    # 'undeclared' bucket is how that bucket stopped being a
                    # signal: it reached 14,162 rows in 24h, against which the
                    # 270-row silent deletion it exists to catch would have been
                    # invisible. Excluding the migration artefacts in N1 would
                    # have added 663 more on the next reindex.
                    cur.execute("SET LOCAL app.repair_key = 'index:prune'")
                    cur.execute("DELETE FROM content_embeddings "
                                "WHERE source_type=%s AND source_id = ANY(%s)",
                                (st, gone))
                    out["deleted"] += len(gone)

                todo, meta_fix = [], []
                for r in rows:
                    text = (r["text"] or "").strip()
                    if len(text) < MIN_CHARS:
                        continue
                    key = E.index_key(text)
                    sid = r["source_id"]
                    if force or stored.get(sid) != key:
                        todo.append((r, text, key))
                        continue
                    # GOVERNANCE METADATA CAN CHANGE WITHOUT THE TEXT CHANGING.
                    # Flipping case_comments.is_internal reclassifies a comment
                    # as staff-only but leaves its wording alone — so the
                    # (content_hash, model, dims) key is unchanged and the row
                    # was NEVER revisited. The index kept serving it as
                    # visibility='customer' permanently. Re-embedding is not
                    # needed (the vector is still correct); the denormalized
                    # columns just have to follow their source.
                    live_meta = (r["visibility"] or INTERNAL, r["account_id"],
                                 r["contact_id"], r["opportunity_id"],
                                 r["party_key"])
                    if stored_meta.get(sid) != live_meta:
                        meta_fix.append((sid, *live_meta))

                if meta_fix:
                    for sid, vis, acc, con, opp, pk in meta_fix:
                        cur.execute(
                            """UPDATE content_embeddings
                                  SET visibility=%s, account_id=%s, contact_id=%s,
                                      opportunity_id=%s, party_key=%s, updated_at=now()
                                WHERE source_type=%s AND source_id=%s""",
                            (vis, acc, con, opp, pk, st, sid))
                    out["metadata_synced"] = out.get("metadata_synced", 0) + len(meta_fix)
                    logger.info(f"[content_index] {st}: re-synced governance "
                                f"metadata on {len(meta_fix)} row(s)")

                out["by_source"][st] = {"live": len(rows), "stale": len(todo),
                                        "meta_synced": len(meta_fix)}
                if not todo:
                    continue

                take, todo = todo[:budget], todo[budget:]
                out["pending"] += len(todo)
                if not take:
                    continue

                vecs = E.embed([t for _r, t, _k in take])
                if not vecs:
                    logger.warning(f"[content_index] embedding failed for '{st}' "
                                   f"— leaving {len(take)} row(s) stale")
                    out["ok"] = False
                    continue

                for (r, text, key), vec in zip(take, vecs):
                    h, model, _asked = key
                    cur.execute(
                        """INSERT INTO content_embeddings
                             (source_type, source_id, content_hash, model, dims,
                              embedding, account_id, contact_id, opportunity_id,
                              party_key, visibility, occurred_at, snippet,
                              speech_act, direction, actor, parent_key, chunk_ix)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)
                           -- chunk_ix is part of the PK since schema v2. A
                           -- short record is chunk 0; long-form sources will
                           -- write 0..n. The conflict target MUST match the PK
                           -- or every upsert fails.
                           ON CONFLICT (source_type, source_id, chunk_ix) DO UPDATE SET
                             content_hash=EXCLUDED.content_hash,
                             model=EXCLUDED.model, dims=EXCLUDED.dims,
                             embedding=EXCLUDED.embedding,
                             account_id=EXCLUDED.account_id,
                             contact_id=EXCLUDED.contact_id,
                             opportunity_id=EXCLUDED.opportunity_id,
                             party_key=EXCLUDED.party_key,
                             parent_key=EXCLUDED.parent_key,
                             visibility=EXCLUDED.visibility,
                             occurred_at=EXCLUDED.occurred_at,
                             snippet=EXCLUDED.snippet,
                             speech_act=EXCLUDED.speech_act,
                             direction=EXCLUDED.direction,
                             actor=EXCLUDED.actor,
                             updated_at=now()""",
                        (st, r["source_id"], h, model, len(vec),
                         E.encode(vec), r["account_id"], r["contact_id"],
                         r["opportunity_id"], r["party_key"],
                         r["visibility"] or INTERNAL, r["occurred_at"],
                         text[:2000], speech_act(text), r.get("direction"),
                         actor_for(r.get("direction"), st, text,
                                   r.get("activity_type")),
                         r.get("parent_key")))
                    out["embedded"] += 1

                budget -= len(take)
                conn.commit()
                if budget <= 0:
                    break
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[content_index] reindex failed: {exc}")
        out.update(ok=False, error=str(exc)[:200])
    finally:
        conn.close()

    if out["embedded"]:
        logger.info(f"[content_index] embedded {out['embedded']} row(s), "
                    f"{out['pending']} pending")
    return out


# ============================================================================
# SEARCH  — audience-gated, scoped, fail-closed
# ============================================================================

# Sources whose visibility can change WITHOUT their text changing, plus the
# query that re-asserts "is this still customer-visible?" from the source of
# truth. The index is a CACHE of that judgement; for the customer audience we
# do not trust the cache. Sources absent here are fixed by type (activities and
# interaction memories are always internal, so they can never appear in a
# customer result set at all).
_VISIBILITY_RECHECK: Dict[str, str] = {
    "case_comment": """
        SELECT cc.case_comment_id::text FROM case_comments cc
        WHERE cc.case_comment_id::text = ANY(%s)
          AND COALESCE(cc.is_internal, true) = false
    """,
    "conversation_message": """
        SELECT m.message_id::text FROM conversation_messages m
        JOIN conversations cv ON cv.conversation_id = m.conversation_id
        WHERE m.message_id::text = ANY(%s) AND cv.scope = 'external'
    """,
    "case": """
        SELECT c.case_id::text FROM cases c WHERE c.case_id::text = ANY(%s)
    """,
}


def _still_customer_visible(cur, pairs: List[Tuple[str, str]]) -> set:
    """Re-assert customer visibility against the SOURCE tables.

    Defence in depth. The indexer now re-syncs governance metadata every pass,
    but that still leaves a window between staff reclassifying a comment and the
    next pass — and a window in which internal notes reach a customer is not an
    acceptable bound for this boundary. Result sets on this path are small
    (limit ~5, already scoped), so re-checking costs one indexed query per
    source type. Fail CLOSED: anything we cannot positively confirm is dropped."""
    allowed: set = set()
    by_type: Dict[str, List[str]] = {}
    for st, sid in pairs:
        by_type.setdefault(st, []).append(sid)
    for st, ids in by_type.items():
        sql = _VISIBILITY_RECHECK.get(st)
        if not sql:
            continue                      # unknown/always-internal → not allowed
        try:
            cur.execute(sql, (ids,))
            allowed.update((st, r[0]) for r in cur.fetchall())
        except Exception as exc:
            cur.connection.rollback()
            logger.warning(f"[content_index] visibility re-check failed for "
                           f"'{st}' — dropping those results: {exc}")
    return allowed


def search(query: str,
           audience: str,                       # REQUIRED — no default. See below.
           account_id: Optional[str] = None,
           contact_id: Optional[str] = None,
           party_key: Optional[str] = None,
           source_types: Optional[List[str]] = None,
           acts: Optional[List[str]] = None,
           limit: int = 5,
           min_sim: Optional[float] = None) -> List[Dict[str, Any]]:
    """Nearest records by MEANING, within a scope the caller is entitled to.

    audience:
      'internal' — staff/agent context; sees internal + customer rows.
      'customer' — a verified customer's own context. Sees ONLY
                   visibility='customer' rows, and ONLY within an explicit
                   account/contact/party scope. A customer-audience call with no
                   scope returns [] — it is never a whole-corpus search.

    Anything that is not exactly 'internal' is treated as 'customer'. The
    restrictive branch is the DEFAULT branch, so a typo or a new caller that
    passes something unexpected under-serves rather than leaks.

    `audience` HAS NO DEFAULT VALUE, deliberately. It previously defaulted to
    'internal' — the permissive branch — which meant the enforcement was
    fail-closed but the API surface was fail-OPEN: a caller that simply forgot
    the argument silently received internal notes. Default-deny has to hold at
    the signature, not only inside the function, so every caller is now forced
    to state which side of the boundary it is on."""
    if not ENABLED or not (query or "").strip():
        return []

    is_internal = (audience == INTERNAL)
    scoped = any([account_id, contact_id, party_key])
    if not is_internal and not scoped:
        logger.info("[content_index] refusing unscoped customer-audience search")
        return []

    where = ["dims = %s", "model = %s"]
    args: List[Any] = [E.DIMS, E.MODEL]

    if not is_internal:
        where.append("visibility = %s")
        args.append(CUSTOMER)

    scope_or, scope_args = [], []
    if account_id:
        scope_or.append("account_id = %s::uuid"); scope_args.append(account_id)
    if contact_id:
        scope_or.append("contact_id = %s::uuid"); scope_args.append(contact_id)
    if party_key:
        scope_or.append("party_key = %s"); scope_args.append(party_key)
    if scope_or:
        where.append("(" + " OR ".join(scope_or) + ")")
        args.extend(scope_args)

    if source_types:
        valid = [t for t in source_types if t in SOURCES]
        if not valid:
            return []
        where.append("source_type = ANY(%s)")
        args.append(valid)

    # Filter by what the record DOES, not only what it is about. This is the
    # fix for the measured false positive: "what commitments did we make?"
    # returned "Requested additional information from customer." at 0.517,
    # because a request and a commitment about one subject sit next to each
    # other in embedding space. No threshold separates them; the ACT does.
    if acts:
        where.append("speech_act = ANY(%s)")
        args.append([a for a in acts])

    qv = E.embed_one(query.strip())
    if not qv:
        return []
    _LAST_COVERAGE["searches"] += 1

    sql = (f"SELECT source_type, source_id, embedding, dims, snippet, "
           f"visibility, occurred_at, speech_act FROM content_embeddings "
           f"WHERE {' AND '.join(where)} "
           f"ORDER BY occurred_at DESC NULLS LAST LIMIT {int(MAX_CANDIDATES)}")

    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rows = cur.fetchall()
                # HOW MUCH OF THE CORPUS COULD THIS SEARCH EVEN SEE?
                #
                # Candidates are the MOST RECENT `MAX_CANDIDATES` rows matching
                # the filters, and relevance is then ranked inside that window.
                # While the window holds most of the corpus that is a search.
                # Once it does not, it quietly becomes "search recent", and the
                # failure is not gradual: measured on this corpus, a window
                # covering 59% still reached 4 of the 5 best results, and a
                # window covering 29% reached NONE of them. Results still come
                # back — worse ones — with nothing to say the best were never
                # candidates.
                #
                # The number is recorded so the degradation is observable
                # before it is severe, rather than inferred afterwards from
                # complaints that answers "got worse".
                if len(rows) >= MAX_CANDIDATES:
                    cur.execute("SELECT count(*) FROM content_embeddings "
                                f"WHERE {' AND '.join(where)}", args)
                    matched = cur.fetchone()[0]
                    _record_truncation(len(rows), matched)
        finally:
            conn.close()
    except Exception as exc:
        logger.info(f"[content_index] search skipped (table missing?): {exc}")
        return []

    if not rows:
        return []

    by_key = {(r[0], r[1]): r for r in rows}
    # Over-fetch, then suppress near-duplicates down to `limit`. Ranking a few
    # extra rows is free (one matrix product); losing the only distinct answer
    # to four copies of a template is not.
    ranked = E.rank(qv,
                    [((r[0], r[1]), bytes(r[2]), r[3]) for r in rows],
                    limit=int(limit) * 6,
                    min_sim=MIN_SIM if min_sim is None else float(min_sim))

    # Customer audience: re-assert visibility from the source before returning.
    if not is_internal and ranked:
        try:
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    allowed = _still_customer_visible(cur, [k for k, _ in ranked])
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(f"[content_index] visibility re-check unavailable — "
                           f"returning nothing on the customer path: {exc}")
            return []
        ranked = [(k, s) for k, s in ranked if k in allowed]

    out: List[Dict[str, Any]] = []
    seen_prefixes: set = set()
    for key, sim in ranked:
        if len(out) >= int(limit):
            break
        r = by_key[key]
        snippet = r[4] or ""
        fingerprint = template_fingerprint(snippet)
        if fingerprint and fingerprint in seen_prefixes:
            continue
        seen_prefixes.add(fingerprint)
        out.append({
            "source_type": r[0], "source_id": r[1],
            "label": SOURCES.get(r[0], {}).get("label", r[0]),
            "snippet": snippet, "visibility": r[5],
            "on_date": r[6].date().isoformat() if r[6] else None,
            "speech_act": r[7],
            "similarity": round(sim, 4),
        })

    # Audit trail. Recorded HERE, at the single point every retrieval passes
    # through, so no caller can obtain grounding without leaving a record. The
    # index is mutable — rows are added, reclassified and erased — so a
    # retrieval cannot be reconstructed after the fact; it has to be captured
    # when it happens. Never allowed to fail the search.
    try:
        from app.core import grounding
        grounding.record(query, audience, out,
                         entity_type=("contact" if contact_id else
                                      "account" if account_id else None),
                         entity_id=contact_id or account_id)
    except Exception as exc:
        logger.debug(f"[content_index] grounding not recorded: {exc}")
    return out


def status() -> Dict[str, Any]:
    """Index health: coverage per source and whether any rows are on a stale
    model — the signal that a re-index is owed."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT source_type, visibility, count(*), model, dims
                               FROM content_embeddings
                               GROUP BY source_type, visibility, model, dims
                               ORDER BY source_type, visibility""")
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    stale = sum(n for _s, _v, n, m, d in rows if (m, int(d)) != (E.MODEL, E.DIMS))
    return {
        "ok": True, "enabled": ENABLED,
        "model": E.MODEL, "dims": E.DIMS,
        "total": sum(r[2] for r in rows),
        "on_stale_model": stale,
        "by_source": [{"source_type": s, "visibility": v, "count": n,
                       "model": m, "dims": d} for s, v, n, m, d in rows],
    }


# ============================================================================
# ROUTER
# ============================================================================

router = APIRouter(tags=["semantic-content"])


@router.get("/content-index/status")
def content_index_status():
    return status()


@router.post("/content-index/reindex")
def content_index_reindex(body: Optional[Dict[str, Any]] = None):
    b = body or {}
    return reindex(source_types=b.get("source_types"),
                   limit=int(b.get("limit") or BATCH),
                   force=bool(b.get("force")))


@router.post("/content-index/search")
def content_index_search(body: Dict[str, Any]):
    """Internal-audience search. Customer-scoped retrieval goes through
    customer_memory.recall, which passes the verified customer's own scope —
    this endpoint is admin-gated at the router and never customer-reachable."""
    return {"results": search(
        query=str(body.get("query") or ""),
        audience=INTERNAL,
        account_id=body.get("account_id"),
        contact_id=body.get("contact_id"),
        party_key=body.get("party_key"),
        source_types=body.get("source_types"),
        limit=int(body.get("limit") or 5),
    )}


if __name__ == "__main__":       # python -m app.core.content_index  → full backfill
    import json as _json
    total = 0
    while True:
        r = reindex()
        total += r.get("embedded", 0)
        print(_json.dumps({k: v for k, v in r.items() if k != "by_source"}))
        if not r.get("ok") or not r.get("embedded"):
            break
    print(f"done — {total} embedded")
    print(_json.dumps(status(), indent=2, default=str))
