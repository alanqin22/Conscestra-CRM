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
1. NO pgvector — SUPERSEDED 2026-08-31, and the original reasoning is kept
   because the premise changed, not the logic. It read: "it is not installed
   and not guaranteed on the deploy target (pg_available_extensions lists only
   pg_trgm)". Measured on that date, pgvector 0.8.6 is INSTALLED on both the
   local and the Railway databases.

   The other half of the original claim also stopped holding, and this is the
   part that forced the change. "At this corpus size a scoped query ranks
   tens-to-hundreds of candidates" was true at ~7k rows. At 12,774 the
   MAX_CANDIDATES=4,000 recency window means **69% of the indexed corpus is
   unreachable by any query**, and which 31% is visible is chosen by recency
   rather than relevance — a correctness defect, not a latency one, and a
   silent one: the search returns confident, well-formed, incomplete answers.

   So `embedding_v vector(512)` + an HNSW index now exist and are kept exact
   (see `rebuild_vectors` and `vector_drift`). THE READ PATH IS STILL NUMPY:
   switching `search()` over is gated on measured ranking parity, because
   replacing one silent retrieval defect with another is the specific failure
   worth avoiding here. Both representations are written from one value in one
   statement — the 35 rows that once disagreed came from two writes of it.

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

# ── Retrieval mode ──────────────────────────────────────────────────────────
# The kill switch, because this changes which rows every grounded answer is
# drawn from.
#
#   recency_only  the pre-existing contract, exactly. DEFAULT.
#   hybrid        recency candidates UNION vector candidates, the whole union
#                 ranked by the exact NumPy ranker, then sliced.
#
# WHY NOT VECTOR-ONLY. It was tried, for about an hour, and measured on 40
# queries: replacing the recency pool with a vector pool GAINED 40 results and
# LOST 38 different ones. Not an upgrade — a trade. The cause is corpus
# redundancy: template groups run to 88 identical vectors, so the 4,000 most
# SIMILAR rows are far less diverse than the 4,000 most RECENT, and relevant
# records get crowded out by copies of one template that dedupe later collapses
# to a single line anyway.
#
# The union fixes that, and was chosen experimentally over two alternatives
# (a scaled budget; deduping before the rank slice). Across 60 queries at
# limits 1/3/5/10/20 it was the only one with ZERO true content loss — every
# baseline result it did not return was either displaced by something strictly
# more similar, or a representative swap inside the same template group.
RETRIEVAL_MODE = os.getenv("CONTENT_INDEX_RETRIEVAL_MODE", "recency_only").strip().lower()
HYBRID = RETRIEVAL_MODE == "hybrid"

# Vector candidate pool size. A TUNED OPERATIONAL PARAMETER, not an invariant.
#
# 500 captured 175 of 177 observed gains with zero measured true content loss
# on this corpus, while avoiding the materially higher vector-query cost seen
# at 4,000 (~30 ms of SQL against ~0.8 ms, dwarfing the ~6 ms the extra ranking
# costs). Two additional results were not worth six times the latency.
#
# Tuned on ONE corpus at ONE size. If the corpus composition changes
# materially — in particular its template redundancy — revalidate rather than
# assume. Do not silently retune.
N_VEC = int(os.getenv("CONTENT_INDEX_VECTOR_POOL", "500"))

# THE CORPUS N_VEC WAS VALIDATED AGAINST, recorded so "revalidate if the corpus
# changes materially" is a check rather than a hope. Measured 2026-09-01.
_NVEC_BASELINE = {"rows": 12976, "largest_template_group": 480,
                  "duplicate_share": 0.53, "validated": "2026-09-01"}

# Cached readiness. Re-checked periodically rather than per search: the answer
# changes only when a migration or a backfill runs.
_VEC_TTL = int(os.getenv("CONTENT_INDEX_VECTOR_TTL_SECS", "300"))
_vec_state: Dict[str, Any] = {"checked_at": 0.0, "ready": False, "why": "unchecked"}


def vector_pool_validity() -> Dict[str, Any]:
    """Is N_VEC still the size it was measured to be adequate at?

    N_VEC is a TUNED OPERATIONAL PARAMETER and the instruction attached to it
    was "revalidate if the corpus changes materially, do not silently retune".
    An instruction in a comment is one nobody runs, so this is the check.

    IT DOES NOT RETUNE ANYTHING. It reports that the evidence behind the number
    has expired, and names why. Choosing a new number requires re-running the
    end-to-end content-loss measurement, which is a decision, not a heuristic.

    Two properties matter, and only the second is obvious:

      SIZE          more rows means the pool covers a smaller share.
      REDUNDANCY    the pool is spent on copies. This is the sharper risk here:
                    the largest template group was measured at 480 against an
                    N_VEC of 500, so a single template can very nearly fill the
                    vector pool on a query that matches it. That is survivable
                    today only because the recency pool is unioned alongside.
    """
    out: Dict[str, Any] = {"ok": True, "revalidate": False, "reasons": [],
                           "baseline": _NVEC_BASELINE, "n_vec": N_VEC}
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM content_embeddings")
                rows = cur.fetchone()[0]
                cur.execute(
                    "SELECT coalesce(max(n), 0), coalesce(sum(n), 0) FROM ("
                    "  SELECT count(*) n FROM content_embeddings "
                    "  WHERE snippet <> '' GROUP BY left(snippet, 60) "
                    "  HAVING count(*) > 1) z")
                largest, dup_rows = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "revalidate": False, "error": str(exc)[:200]}

    share = (dup_rows / rows) if rows else 0.0
    out.update({"rows": rows, "largest_template_group": largest,
                "duplicate_share": round(share, 3)})

    base = _NVEC_BASELINE
    if rows > base["rows"] * 2 or rows < base["rows"] / 2:
        out["reasons"].append(
            f"corpus size moved from {base['rows']} to {rows}")
    if largest > N_VEC * 0.8:
        out["reasons"].append(
            f"largest template group ({largest}) is within 80% of N_VEC "
            f"({N_VEC}) — one template can crowd out the vector pool")
    if share > base["duplicate_share"] + 0.15:
        out["reasons"].append(
            f"duplicate share rose from {base['duplicate_share']:.0%} to "
            f"{share:.0%} — the pool buys less diversity per row")
    out["revalidate"] = bool(out["reasons"])
    return out


def _log_retrieval(diag: Dict[str, Any], empty_reason: Optional[str] = None) -> None:
    """One structured line per search. Counts and reasons; never content.

    The point is to make ONE distinction legible from a log: did retrieval find
    nothing, or did retrieval not happen? Those were the same observable
    outcome before this change, and the difference is what separates "we have
    no record of that" from "the index was unreachable".

    No snippets, no embeddings, no identifiers of the subject — a diagnostic
    that leaks the corpus to fix an observability gap trades one defect for a
    worse one.
    """
    if empty_reason:
        diag["empty_result_reason"] = empty_reason
    try:
        logger.info("[content_index] retrieval " + " ".join(
            f"{k}={v}" for k, v in diag.items() if v not in (None, False)))
    except Exception:                                       # pragma: no cover
        pass


def _vector_candidates_ready() -> bool:
    """Is `embedding_v` present AND complete on this database?

    BOTH CONDITIONS, and the second is the one that matters. Ordering by
    `embedding_v <=> …` silently drops every row whose vector is NULL — so a
    half-backfilled column would not degrade the search, it would HIDE part of
    the corpus while reporting nothing. That is the same failure this switch
    exists to remove, arriving by a different door: the column was found 59%
    populated on 2026-08-31.

    Falls back LOUDLY. A search quietly reverting to the recency window is
    indistinguishable from one that never switched, which is precisely how the
    original defect went unnoticed for months.
    """
    import time as _t
    now = _t.monotonic()
    if now - _vec_state["checked_at"] < _VEC_TTL:
        return _vec_state["ready"]
    ready, why = False, "unknown"
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.content_embeddings')")
                if not cur.fetchone()[0]:
                    why = "content_embeddings does not exist"
                else:
                    cur.execute(
                        "SELECT count(*) FROM pg_attribute a "
                        "WHERE a.attrelid='content_embeddings'::regclass "
                        "AND a.attname='embedding_v' AND NOT a.attisdropped")
                    if not cur.fetchone()[0]:
                        why = ("embedding_v column absent — apply "
                               "sql/content_embeddings_pgvector.sql")
                    else:
                        cur.execute("SELECT count(*) FROM content_embeddings "
                                    "WHERE embedding_v IS NULL")
                        missing = cur.fetchone()[0]
                        if missing:
                            why = (f"{missing} row(s) have no vector — run "
                                   f"content_index.rebuild_vectors()")
                        else:
                            ready, why = True, "ready"
        finally:
            conn.close()
    except Exception as exc:                                # pragma: no cover
        why = f"could not check: {str(exc)[:120]}"
    if ready != _vec_state["ready"] or _vec_state["why"] != why:
        (logger.info if ready else logger.warning)(
            f"[content_index] index-backed candidates {'ON' if ready else 'OFF'}"
            f" — {why}")
    _vec_state.update({"checked_at": now, "ready": ready, "why": why})
    return ready


class RetrievalUnavailable(RuntimeError):
    """Retrieval could not run — distinct from "retrieval found nothing".

    search() returned [] for both, so a caller could not tell "this customer has
    no matching history" from "the embedding provider is down". The comments in
    this module claim the caller falls back to keyword/recency retrieval; traced,
    no caller does, because none could see the difference to act on.

    Raising makes the failure decidable. It does NOT choose the policy — whether
    to degrade to keyword search, serve from cache, or refuse the request is a
    business decision. It makes that decision implementable, and until one is
    made it converts a silent wrong answer into a logged, visible one.
    """

# Last observed search coverage, for the observability surface. A search that
# ranked every matching row is complete; one that hit the cap ranked only the
# newest slice, and the ratio says how much of the corpus it could reach.
_LAST_COVERAGE: Dict[str, Any] = {"searches": 0, "truncated": 0,
                                  "last_ratio": None, "last_matched": None}


# One COUNT(*) per this many truncated searches.
TRUNCATION_SAMPLE = int(os.getenv("CONTENT_INDEX_TRUNCATION_SAMPLE", "20"))


def _record_truncation(considered: int, matched: int) -> None:
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
                              embedding, embedding_v, account_id, contact_id,
                              opportunity_id, party_key, visibility, occurred_at,
                              snippet, speech_act, direction, actor, parent_key,
                              chunk_ix)
                           -- embedding AND embedding_v are written from the SAME
                           -- `vec`, in the SAME statement. That is what keeps
                           -- them equal: the 35 rows that once disagreed were
                           -- produced by two separate writes of one value.
                           VALUES (%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)
                           -- chunk_ix is part of the PK since schema v2. A
                           -- short record is chunk 0; long-form sources will
                           -- write 0..n. The conflict target MUST match the PK
                           -- or every upsert fails.
                           ON CONFLICT (source_type, source_id, chunk_ix) DO UPDATE SET
                             content_hash=EXCLUDED.content_hash,
                             model=EXCLUDED.model, dims=EXCLUDED.dims,
                             embedding=EXCLUDED.embedding,
                             embedding_v=EXCLUDED.embedding_v,
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
                         E.encode(vec), E.to_pgvector(vec),
                         r["account_id"], r["contact_id"],
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
        # The embedding provider is the only unmitigated single point of failure
        # in the retrieval path: every search calls it, and unlike chat
        # completion there is no configured failover.
        raise RetrievalUnavailable(
            "could not embed the query — the embedding provider is unreachable "
            "or returned nothing. Semantic retrieval is unavailable; this is "
            "NOT an empty result set.")
    _LAST_COVERAGE["searches"] += 1

    # CANDIDATE SELECTION IS THE WHOLE FIX, and it is one clause.
    #
    # Everything downstream — the numpy re-rank, the template dedupe, MIN_SIM,
    # the speech-act filter — is unchanged and still decides the final order.
    # The index is not trusted to rank; it is trusted to CHOOSE WHAT TO RANK.
    # So the exact scores every caller sees are still computed here, over
    # candidates drawn from the whole corpus instead of from the last 4,000
    # rows by date.
    #
    # MEASURED BEFORE SWITCHING (60 sampled queries, recall@10 against an
    # exhaustive numpy ranking of all 12,971 rows):
    #
    #     recency window, BEST POSSIBLE   29.8%   ← the ceiling, not the score
    #     pgvector HNSW                   92.5%
    #     distance-equivalent queries     96.7%
    #     mean similarity shortfall       0.0095
    #     latency                         1.6 ms vs 1.7 ms
    #
    # The residual 7.5% is not a miss. This corpus is heavily templated, so the
    # true top-k contains ties, and a different member of a tie scores as a
    # miss by identity while being identical by distance — which is what the
    # 96.7% figure separates out.
    # Kept SEPARATE from the candidate query's parameters. The truncation
    # COUNT below reuses only the WHERE clause, and appending the query vector
    # to a shared list gave it one argument more than it had placeholders —
    # which psycopg2 raised, and the handler below swallowed as "no results".
    where_args = list(args)
    _cols = ("source_type, source_id, embedding, dims, snippet, "
             "visibility, occurred_at, speech_act")

    # RECENCY POOL — unchanged semantics, plus an explicit secondary key.
    # `occurred_at` ties were previously broken by whatever order PostgreSQL
    # returned, which is the other half of the determinism problem D2 fixes in
    # the ranker. Ordering by the primary key after the timestamp costs nothing
    # and makes the pool reproducible.
    sql = (f"SELECT {_cols} FROM content_embeddings "
           f"WHERE {' AND '.join(where)} "
           f"ORDER BY occurred_at DESC NULLS LAST, source_type, source_id "
           f"LIMIT {int(MAX_CANDIDATES)}")

    # VECTOR POOL — the same eligibility clause, so speech-act filtering and
    # visibility apply identically to both pools. Only the ordering differs.
    vec_sql = (f"SELECT {_cols} FROM content_embeddings "
               f"WHERE {' AND '.join(where)} AND embedding_v IS NOT NULL "
               f"ORDER BY embedding_v <=> %s::vector, source_type, source_id "
               f"LIMIT {int(N_VEC)}")

    # ── Diagnostics. Counts and reasons only; never content, never vectors. ──
    diag: Dict[str, Any] = {"retrieval_mode": "hybrid" if HYBRID else "recency_only",
                            "recency_candidate_count": 0, "vector_candidate_count": 0,
                            "union_candidate_count": 0, "overlap_count": 0,
                            "ranked_count": 0, "final_count": 0,
                            "fallback_reason": None, "vector_query_failure": False,
                            "recency_query_failure": False, "empty_result_reason": None}

    _vec = HYBRID and _vector_candidates_ready()
    if HYBRID and not _vec:
        diag["fallback_reason"] = _vec_state.get("why")

    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # RECENCY IS THE FLOOR. Its failure is a genuine retrieval
                # failure, not an empty result — the caller must be able to
                # tell "nothing matched" from "the index did not answer".
                cur.execute(sql, args)
                rows = cur.fetchall()
                diag["recency_candidate_count"] = len(rows)

                # VECTOR IS ADDITIVE, so its failure degrades rather than
                # fails. Caught SEPARATELY and reported LOUDLY: a vector query
                # that dies must not look like a corpus with nothing in it,
                # and it must not take the recency results down with it.
                if _vec:
                    try:
                        # ef_search MUST BE RAISED TO REACH N_VEC.
                        #
                        # HNSW returns at most `ef_search` rows regardless of
                        # LIMIT, and the default is 40 — so `LIMIT 500` was
                        # measured returning 41. The D1 experiment simulated
                        # the vector pool with exact numpy and therefore never
                        # saw this; without the line below, N_VEC is a number
                        # the index quietly ignores.
                        #
                        # The vector cast comes FIRST on purpose: pgvector
                        # registers hnsw.ef_search when its library loads, and
                        # PostgreSQL accepts `SET` for any unknown two-part
                        # name as a custom option — so setting it before the
                        # library is loaded silently sets a variable nothing
                        # reads. Verified after setting, for the same reason.
                        cur.execute("SELECT '[1,0]'::vector <=> '[0,1]'::vector")
                        cur.execute(f"SET LOCAL hnsw.ef_search = {max(int(N_VEC), 40)}")
                        cur.execute(vec_sql, where_args + [E.to_pgvector(qv)])
                        vrows = cur.fetchall()
                        diag["vector_candidate_count"] = len(vrows)
                        seen = {(r[0], r[1]) for r in rows}
                        overlap = sum(1 for r in vrows if (r[0], r[1]) in seen)
                        diag["overlap_count"] = overlap
                        rows = rows + [r for r in vrows if (r[0], r[1]) not in seen]
                    except Exception as vexc:
                        conn.rollback()
                        diag["vector_query_failure"] = True
                        diag["fallback_reason"] = f"vector query failed: {str(vexc)[:120]}"
                        logger.error(
                            f"[content_index] VECTOR candidate query failed — "
                            f"falling back to recency candidates only. This is "
                            f"a degraded search, not an empty corpus: {vexc}",
                            exc_info=True)
                diag["union_candidate_count"] = len(rows)
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
                # SAMPLED, not per request. Establishing how much was missed
                # needs a COUNT(*), which is a sequential scan — measured 0.9 ms
                # alone, and it contends with every other search doing the same
                # scan under concurrency. Instrumentation that degrades the path
                # it observes buys visibility with the thing it is watching.
                #
                # Coverage moves with corpus size, not per query, so one reading
                # every SAMPLE searches is the same signal at 1/20th the cost.
                # ONLY MEANINGFUL ON THE RECENCY PATH, and this guard is not a
                # convenience. The measurement above asks "how much of the
                # corpus could this search even see", which is a real question
                # when candidates are the most RECENT 4,000 rows. When they are
                # the most RELEVANT 4,000 the limit is always reached and never
                # indicates degradation — so counting it there would report
                # permanent truncation, drown the real signal, and teach
                # everyone to ignore the one number that detects this defect
                # coming back.
                if not _vec and len(rows) >= MAX_CANDIDATES:
                    _LAST_COVERAGE["truncated"] += 1
                    if _LAST_COVERAGE["truncated"] % TRUNCATION_SAMPLE == 1:
                        cur.execute("SELECT count(*) FROM content_embeddings "
                                    f"WHERE {' AND '.join(where)}", where_args)
                        _record_truncation(len(rows), cur.fetchone()[0])
        finally:
            conn.close()
    except Exception as exc:
        # THE RECENCY QUERY FAILED, and that is a retrieval failure.
        #
        # This used to catch everything, log at INFO as "table missing?", and
        # return [] — so an unreachable index, a malformed query and a corpus
        # with no matches were the same answer to the caller. It was not
        # hypothetical: a parameter-count bug surfaced as "0 results" and was
        # only found by reading the log line it was hiding behind.
        #
        # A missing table still degrades quietly, because a database that has
        # never been indexed is a legitimate state that must not raise on every
        # search. Anything else is now loud, and the caller is told.
        diag["recency_query_failure"] = True
        msg = str(exc)
        if "does not exist" in msg or "undefined table" in msg.lower():
            logger.info(f"[content_index] no content index on this database: {exc}")
            _log_retrieval(diag, "no_index")
            return []
        logger.error(f"[content_index] RECENCY candidate query FAILED — this is "
                     f"a retrieval failure, not an empty result: {exc}",
                     exc_info=True)
        raise RetrievalUnavailable(
            f"the content index could not be queried: {str(exc)[:200]}")

    if not rows:
        _log_retrieval(diag, "no_candidates")
        return []

    by_key = {(r[0], r[1]): r for r in rows}

    # CANONICAL ORDER BEFORE RANKING. The ranker's sort is stable (D2), so the
    # order candidates arrive in decides how exact ties are broken. Sorting by
    # the primary key here is what turns "stable" into "deterministic": the
    # same union produces the same result whichever pool contributed a row and
    # in whatever order the pools came back.
    cands = sorted(by_key.items(), key=lambda kv: kv[0])

    # RANK THE WHOLE UNION, SLICE LAST (D1-C).
    #
    # The budget used to be `limit * 6`, applied INSIDE rank() before template
    # dedupe ran. On a union that is measurably wrong: the pool is larger and
    # more redundant, so copies of one template consume the budget before a
    # distinct record is reached, and the search returns FEWER results than the
    # recency path it replaced. Measured across 60 queries at five limits, the
    # three candidate-budget strategies were compared end to end, and ranking
    # the entire union was the only one with zero true content loss.
    #
    # The cost is one larger matrix product — 4.1 ms to 10.2 ms measured at a
    # 4,500-row union — which is far less than the vector query it accompanies.
    # THE BUDGET IS MODE-DEPENDENT, and that is the kill switch's whole
    # promise. "Rank all" is part of D1-C, not a free improvement: because
    # rank() applies MIN_SIM AFTER its slice, widening the slice lets through
    # results that the fixed `limit * 6` budget had been silently consuming
    # slots for. Applied to the recency pool it changed recency_only's output —
    # 2, 4, 1, 1 results became 5, 5, 5, 5 on four sample queries.
    #
    # That is arguably better and is emphatically NOT what recency_only means.
    # The switch has to restore the prior contract exactly or it is not a
    # rollback, so the legacy path keeps the legacy budget.
    budget = len(cands) if _vec else int(limit) * 6
    ranked = E.rank(qv,
                    [(k, bytes(r[2]), r[3]) for k, r in cands],
                    limit=budget,
                    min_sim=MIN_SIM if min_sim is None else float(min_sim))
    diag["ranked_count"] = len(ranked)
    if not ranked:
        _log_retrieval(diag, "all_below_min_sim")
        return []

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

    # EVERY EMPTY RESULT NAMES ITS CAUSE. Reaching here with nothing means the
    # candidates ranked, cleared MIN_SIM, and were then removed — by template
    # dedupe or by the customer visibility re-check. Both are legitimate; being
    # unable to tell which is not.
    diag["final_count"] = len(out)
    diag["dedupe_count"] = len(ranked) - len(out)
    _log_retrieval(diag, None if out else
                   ("removed_by_dedupe_or_visibility" if ranked else "no_candidates"))

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


def rebuild_vectors(batch: int = 1000, limit: Optional[int] = None,
                    verify_only: bool = False) -> Dict[str, Any]:
    """Derive `embedding_v` from `embedding` — the whole column, not the gaps.

    WHY WHOLESALE. A hand-made backfill left this column 59% populated AND
    wrong in 35 places: rows whose vector disagreed with the bytea it was
    supposedly computed from, worst cosine 0.960217. Filling only the NULLs
    would have preserved every one of those, and a wrong vector does not fail —
    it ranks the wrong document, confidently, on a surface nobody re-checks.
    So every row is rewritten from the authoritative source and none of the
    existing values is trusted.

    DIRECTION IS FIXED: bytea -> vector. `embedding` is what every writer
    writes and every reader reads; `embedding_v` exists for the HNSW index.
    When they disagree the bytea is right by definition.

    No re-embedding, no API spend: the vectors already exist and this is a
    decode. Resumable and idempotent — re-running it changes nothing once the
    column agrees, which is also what makes it safe to schedule.

    `verify_only` reports the disagreement without writing, so the drift can be
    measured on production before anything is changed there.
    """
    out: Dict[str, Any] = {"ok": True, "checked": 0, "rewritten": 0,
                           "disagreed": 0, "missing": 0, "skipped_dims": 0,
                           "verify_only": verify_only, "worst_cosine": 1.0}
    try:
        import numpy as _np
    except Exception as exc:                                # pragma: no cover
        return {"ok": False, "error": f"numpy unavailable: {exc}"}

    conn = get_connection()
    try:
        last: Optional[Tuple[str, str, int]] = None
        while True:
            with conn.cursor() as cur:
                # Keyset pagination on the primary key. An OFFSET walk over a
                # table being written to skips rows, and the row it skips is
                # invisible rather than reported.
                if last is None:
                    cur.execute(
                        "SELECT source_type, source_id, chunk_ix, dims, "
                        "embedding, embedding_v IS NULL "
                        "FROM content_embeddings "
                        "ORDER BY source_type, source_id, chunk_ix LIMIT %s",
                        (batch,))
                else:
                    cur.execute(
                        "SELECT source_type, source_id, chunk_ix, dims, "
                        "embedding, embedding_v IS NULL "
                        "FROM content_embeddings "
                        "WHERE (source_type, source_id, chunk_ix) > (%s,%s,%s) "
                        "ORDER BY source_type, source_id, chunk_ix LIMIT %s",
                        (*last, batch))
                rows = cur.fetchall()
            if not rows:
                break

            for st, sid, ix, dims, blob, was_null in rows:
                last = (st, sid, ix)
                out["checked"] += 1
                if was_null:
                    out["missing"] += 1
                vec = E.decode(bytes(blob), expect_dims=int(dims)) if blob else None
                if vec is None:
                    # A width that does not match its own `dims` is not
                    # comparable to anything; writing it into a vector(512)
                    # would either fail or silently mean something else.
                    out["skipped_dims"] += 1
                    continue
                if not verify_only:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE content_embeddings SET embedding_v = %s::vector "
                            "WHERE source_type=%s AND source_id=%s AND chunk_ix=%s",
                            (E.to_pgvector(vec), st, sid, ix))
                        out["rewritten"] += cur.rowcount
            if not verify_only:
                conn.commit()
            if limit and out["checked"] >= limit:
                break

        # Measure what is left, from the database rather than from the counters
        # above — a report derived from the work it is reporting on cannot
        # detect the work being wrong.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(embedding_v) FROM content_embeddings")
            total, vectors = cur.fetchone()
        out["total"] = total
        out["with_vector"] = vectors
        out["still_missing"] = total - vectors
    except Exception as exc:
        # LOUD, not merely recorded. A returned {"ok": False} is only seen by a
        # caller that reads it, and the scheduled caller does not: a rebuild
        # that dies half-way would otherwise leave the column partly written
        # and say so nowhere — which is the state this whole routine exists to
        # repair, recreated by the repair itself.
        logger.error(f"[content_index] rebuild_vectors FAILED after "
                     f"{out['checked']} row(s), {out['rewritten']} rewritten: "
                     f"{exc}", exc_info=True)
        out["ok"] = False
        out["error"] = str(exc)[:300]
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    return out


def vector_drift() -> Dict[str, Any]:
    """How far `embedding_v` has drifted from the authoritative `embedding`.

    The detector for the failure this column already had once. Cheap enough to
    schedule: it decodes in numpy and compares direction, so it costs no model
    calls and touches no external service.
    """
    try:
        import numpy as _np
    except Exception as exc:                                # pragma: no cover
        return {"ok": False, "error": f"numpy unavailable: {exc}"}
    out: Dict[str, Any] = {"ok": True, "compared": 0, "disagreed": 0,
                           "worst_cosine": 1.0, "examples": []}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source_type, source_id, chunk_ix, dims, embedding, "
                        "embedding_v::text FROM content_embeddings "
                        "WHERE embedding_v IS NOT NULL")
            rows = cur.fetchall()
        for st, sid, ix, dims, blob, vtxt in rows:
            a = E.decode(bytes(blob), expect_dims=int(dims)) if blob else None
            if a is None or not vtxt:
                continue
            av = _np.asarray(a, dtype=_np.float64)
            # not np.fromstring: deprecated, and it fails silently on a
            # malformed literal by returning a short array rather than raising.
            bv = _np.array(vtxt.strip("[]").split(","), dtype=_np.float64)
            if av.shape != bv.shape:
                out["disagreed"] += 1
                continue
            out["compared"] += 1
            na, nb = _np.linalg.norm(av), _np.linalg.norm(bv)
            if na == 0 or nb == 0:
                continue
            cos = float(av @ bv / (na * nb))
            if cos < 0.9999:
                out["disagreed"] += 1
                out["worst_cosine"] = min(out["worst_cosine"], cos)
                if len(out["examples"]) < 10:
                    out["examples"].append(
                        {"source_type": st, "source_id": sid, "chunk_ix": ix,
                         "cosine": round(cos, 6)})
    except Exception as exc:
        logger.error(f"[content_index] vector_drift FAILED after "
                     f"{out['compared']} comparison(s): {exc}", exc_info=True)
        out["ok"] = False
        out["error"] = str(exc)[:300]
    finally:
        conn.close()
    return out


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
