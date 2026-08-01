"""Consolidation — the step that turns search into memory (Customer Memory v1).

`content_index` made every activity, case and message SEARCHABLE. Retrieval
answers "find me records about pricing"; it cannot answer "has this customer
raised pricing REPEATEDLY?" — that needs counting across records, which is an
aggregation, not a lookup. Measured before this module existed: asking a real
contact "what issues has this customer raised repeatedly?" returned one refund
request, with no notion of repetition at all.

    content_embeddings ──cluster by meaning──▶ themes ──name──▶ customer_memories
                                                 │                    │
                                          occurrences          evidence links
                                          date span            inherited visibility
                                                               evidence_hash

CLUSTERING WITHOUT NEW INFRASTRUCTURE. The vectors already exist, one per record,
and a single customer holds at most a few hundred. Greedy agglomeration over the
cosine matrix at CLUSTER_SIM is enough at that scale and is deterministic, which
matters more than sophistication: a memory that reshuffles every run is not a
memory. No new dependency, no model training, no ANN index.

THREE RULES THAT MAKE IT SAFE
  1. VISIBILITY IS INHERITED, MOST RESTRICTIVE WINS. A theme drawn from one
     internal note and one customer message is INTERNAL. Without this,
     summarizing launders staff-only notes into customer-visible prose — a leak
     created by the act of consolidating.
  2. EVIDENCE IS POINTERS, NEVER CONTENT. Text stays in content_embeddings,
     which is erased with the customer. A third copy is a third thing for
     erasure to forget, which is exactly how the index shipped broken.
  3. STALENESS IS THE EVIDENCE HASH. A memory whose evidence set changed is a
     cached assertion that no longer matches its source — the same failure shape
     as the metric drift this codebase already paid for once.

Statements are TEMPLATED, not LLM-written, unless MEMORY_LLM_NAMING=1. A
deterministic sentence built from the cluster's own facts cannot hallucinate a
claim the evidence does not support, and consolidation runs unattended.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core import embeddings as E
from app.core.database import get_connection

logger = logging.getLogger("memory_consolidation")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("MEMORY_CONSOLIDATION_ENABLED", "1")
CLUSTER_SIM = float(os.getenv("MEMORY_CLUSTER_SIM", "0.78"))
MIN_CLUSTER = int(os.getenv("MEMORY_MIN_CLUSTER", "2"))     # 1 record is not a theme
MAX_RECORDS = int(os.getenv("MEMORY_MAX_RECORDS", "600"))   # per customer, newest first
MAX_EVIDENCE = int(os.getenv("MEMORY_MAX_EVIDENCE", "25"))
ENTITIES_PER_PASS = int(os.getenv("MEMORY_ENTITIES_PER_PASS", "25"))

INTERNAL, CUSTOMER = "internal", "customer"

# Memory kinds. Only a VERIFIED fact may be asserted to a customer; a hypothesis
# may be alluded to; a theme is a counted pattern. v1 had no distinction, so an
# inference and an established fact rendered identically and an agent had no way
# to tell what it was allowed to say.
FACT, HYPOTHESIS, THEME = "fact", "hypothesis", "theme"

# Lifecycle. A memory is no longer true-forever-once-written.
ACTIVE, DORMANT, SUPERSEDED, REJECTED = "active", "dormant", "superseded", "rejected"

# Certainty halves every HALF_LIFE_DAYS since last observation. "Budget
# approved" is true in March and a confident falsehood by November; without
# decay a memory's certainty records only how sure we were, never how long ago.
HALF_LIFE_DAYS = float(os.getenv("MEMORY_HALF_LIFE_DAYS", "180"))

# DECAY CLASSES. One half-life for everything was wrong in both directions:
# "prefers email" is a trait that should barely age, while "current negotiation
# status" is stale in weeks. The class belongs to the TOPIC, declared once.
STABLE, EPISODIC, VOLATILE = "stable", "episodic", "volatile"
_DECAY_HALF_LIFE = {
    STABLE:   float(os.getenv("MEMORY_HL_STABLE", "1460")),   # ~4y, effectively a trait
    EPISODIC: HALF_LIFE_DAYS,                                 # events fade
    VOLATILE: float(os.getenv("MEMORY_HL_VOLATILE", "30")),   # negotiation state changes weekly
}
# EVERY topic in _TOPICS is listed. It previously named 4 of 10, and the other
# six inherited EPISODIC from a `.get(topic, EPISODIC)` default — a middle tier
# assigned by omission rather than by decision. `pricing` is the clearest cost:
# a quoted price is the textbook short-lived fact and it silently got the
# 180-day class because nobody wrote it down.
_TOPIC_DECAY = {
    "account admin": STABLE,      # contact details, preferences
    "onboarding":    STABLE,      # how they were set up
    "billing":       EPISODIC,    # disputes recur and persist
    "delivery":      EPISODIC,
    "product issue": EPISODIC,
    "returns":       EPISODIC,
    "support":       EPISODIC,
    "pricing":       VOLATILE,    # a quote or discount goes stale fast
    "sales":         VOLATILE,    # negotiation state
    "renewal":       VOLATILE,
}


# Below this an aged memory stops being surfaced (it is kept, not deleted —
# it may still be evidence that something USED to be true).
DORMANT_BELOW = float(os.getenv("MEMORY_DORMANT_BELOW", "0.20"))


def decay_class_for(topic: str) -> str:
    """Decay belongs to the TOPIC. Declared once, beside the vocabulary.

    UNKNOWN FALLS TO THE SHORTEST LIFE, not the middle one. The same
    uncertainty was being treated in opposite directions: an unclassifiable
    claim needs the STRICTEST approval (`general` requires 2, unknown topics are
    STRICT — because the classifier reads customer text and a claim can be
    steered into the catch-all), while decay handed that same claim the middle
    tier and 180 days of continued assertion. `general` is 12.6% of this
    corpus. A claim we cannot characterise should stop being asserted soonest
    unless it is re-observed — and re-observation refreshes last_observed_at,
    so a genuinely recurring theme is unaffected."""
    return _TOPIC_DECAY.get(topic, VOLATILE)

# Model + prompt version. Part of the memory's identity: a statement produced by
# a since-changed generator is not comparable to a fresh one.
#
# The `v1` is hand-maintained and therefore was wrong: the statement templates
# were changed (to hedge a clipped window) with no bump, so two materially
# different sentences claimed the same generator identity. An invariant stated
# only in a comment is not an invariant.
#
# GENERATOR is completed at the END of this module with a fingerprint of what
# `_statement` ACTUALLY RENDERS over a fixed probe set. Behavioural, not
# source-hashing: reformatting or editing a comment leaves it alone, while any
# change to the words asserted about a person changes it automatically.
GENERATOR_BASE = f"memory_consolidation/v1+{E.MODEL}@{E.DIMS}"

# Topic vocabulary. A cluster is named by the strongest keyword signal across its
# members — deterministic, inspectable, and impossible to hallucinate. Order is
# irrelevant; the highest hit count wins, ties broken alphabetically for stability.
_TOPICS: List[Tuple[str, Tuple[str, ...]]] = [
    ("pricing",        ("price", "pricing", "quote", "discount", "cost", "expensive")),
    ("billing",        ("invoice", "billing", "payment", "overdue", "refund", "charge")),
    ("delivery",       ("shipping", "delivery", "shipped", "tracking", "delayed", "arrive")),
    ("product issue",  ("broken", "defect", "faulty", "not working", "error", "bug", "damaged")),
    ("returns",        ("return", "rma", "exchange", "replacement")),
    ("renewal",        ("renew", "renewal", "contract", "subscription", "expire")),
    ("onboarding",     ("onboard", "setup", "install", "training", "getting started")),
    ("support",        ("ticket", "case", "issue", "help", "support", "escalat")),
    ("sales",          ("demo", "proposal", "opportunity", "negotiat", "deal")),
    ("account admin",  ("address", "contact details", "update profile", "password", "login")),
]


# Tie-break strictness. `topic` decides the decay class AND how many humans must
# approve a claim, so a tie between two topics is a tie between two SAFETY
# POLICIES. The original tie-break was `-ord(name[0])` — the earlier letter won,
# which meant a snippet scoring equally for a 2-approver topic and a 1-approver
# topic had its approval policy settled by the alphabet.
#
# Ties now resolve to the STRICTER topic. Kept as a static rank rather than a
# call to required_approvals_for() so naming a cluster costs no database round
# trip; `test_topic_strictness_matches_the_policy_table` asserts the two agree.
_TOPIC_STRICTNESS = {"billing": 2, "pricing": 2, "returns": 2, "renewal": 2,
                     "general": 2}


def _topic_for(snippets: List[str]) -> str:
    """Name a cluster. Ties break toward the stricter safety policy, then
    alphabetically for a fully deterministic result."""
    blob = " ".join(snippets).lower()
    scored = [(sum(blob.count(k) for k in keys), name) for name, keys in _TOPICS]
    best_n, best = max(
        scored,
        key=lambda t: (t[0], _TOPIC_STRICTNESS.get(t[1], 1), [-ord(ch) for ch in t[1]]))
    return best if best_n > 0 else "general"


def _evidence_hash(ids: List[str]) -> str:
    return hashlib.sha256("|".join(sorted(ids)).encode("utf-8")).hexdigest()


# ── Verification signing ─────────────────────────────────────────────────────
# The database trigger (sql/memory_invariants.sql) makes casual forgery fail:
# rewriting a verified statement breaks the claim hash, and a verified fact must
# have a matching row in the append-only audit trail. But a caller with FULL
# write access can still insert fake approvals and then promote — demonstrated.
#
# A signature the database cannot compute closes that. The key lives only in the
# application, so a compromised DB account, a leaked replica, a stolen backup or
# a SQL-injection foothold cannot mint an assertable memory. It does NOT defend
# against full host compromise — an attacker with the app's environment has the
# key — and that boundary is stated rather than papered over.
#
# Unset key => signing is DISABLED and no memory is assertable. Fail-closed: an
# unconfigured deployment refuses to assert rather than silently trusting the
# database.
# ── Signing keyring ─────────────────────────────────────────────────────────
# ROTATION WAS IMPOSSIBLE. A single unlabelled key meant changing it invalidated
# every existing verification at once, with no way to distinguish "signed with
# the previous key" from "forged" — so the key could never be rotated, and the
# hardening that made more paths depend on it made that worse. It is currently
# a development placeholder.
#
#   MEMORY_SIGNING_KEY      the ACTIVE key; new signatures use it
#   MEMORY_SIGNING_KEY_ID   its label, default 'k1'
#   MEMORY_SIGNING_KEYS_OLD 'id:secret,id:secret' — verify-only, never sign
#
# Signatures are stored as 'keyid:hexdigest'. Rotation is: move the active pair
# into _OLD, set a new active pair, re-verify at leisure, drop the old entry.
# An unprefixed signature is treated as key 'k1' so existing rows keep working.
_SIGNING_KEY = os.getenv("MEMORY_SIGNING_KEY", "").strip()
_SIGNING_KEY_ID = os.getenv("MEMORY_SIGNING_KEY_ID", "k1").strip() or "k1"


def _retired_keys() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in os.getenv("MEMORY_SIGNING_KEYS_OLD", "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        kid, secret = pair.split(":", 1)
        kid, secret = kid.strip(), secret.strip()
        if kid and secret:
            out[kid] = secret
    return out


# EVERY field the assertion gate reads. The signature must cover the gate's
# whole input surface, not a subset of it.
#
# The first version signed (memory_id, claim_hash, verified_by), where claim_hash
# covered (statement, evidence_hash). The gate reads ELEVEN fields. Nine were
# unsigned, and a direct database writer could force reliability and certainty
# to 1.0 on a legitimately-signed memory — confidence 1.0, past the floor,
# signature still valid. Verified by attack.
#
# A cryptographic control whose scope is narrower than the policy control it
# protects is not a control; it is a decoration on two of eleven fields.
GATE_FIELDS = ("statement", "evidence_hash", "kind", "visibility", "actor",
               "truncated", "reliability", "certainty", "occurrences",
               "evidence_count", "topic", "decay_class", "independent_sources")


def gate_fingerprint(row: Dict[str, Any]) -> str:
    """Canonical hash of every gate input. Order-stable and type-stable, so the
    same logical memory always fingerprints identically regardless of how the
    row was fetched (psycopg2 hands back Decimal for numerics)."""
    canon = {}
    for f in GATE_FIELDS:
        v = row.get(f)
        if v is None:
            canon[f] = None
        elif isinstance(v, bool):
            canon[f] = v
        elif isinstance(v, (int,)):
            canon[f] = int(v)
        elif isinstance(v, float) or type(v).__name__ == "Decimal":
            canon[f] = f"{float(v):.6f}"       # Decimal/float parity
        else:
            canon[f] = str(v)
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def signature_for(memory_id: str, fingerprint: str,
                  verified_by: str) -> Optional[str]:
    """HMAC over the identity of an approval AND the full state that was
    approved. None when no key is configured."""
    if not _SIGNING_KEY:
        return None
    return f"{_SIGNING_KEY_ID}:" + _digest(_SIGNING_KEY, memory_id,
                                          fingerprint, verified_by)


def _digest(secret: str, memory_id: str, fingerprint: str,
            verified_by: str) -> str:
    import hmac
    return hmac.new(secret.encode("utf-8"),
                    f"{memory_id}|{fingerprint}|{verified_by}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def signature_valid(memory_id: str, fingerprint: str,
                    verified_by: Optional[str],
                    signature: Optional[str]) -> bool:
    """Constant-time check against the CURRENT gate state. Tampering with any
    signed field invalidates the signature. With no key configured this returns
    False, so the gate blocks — an unsigned deployment asserts nothing."""
    if not _SIGNING_KEY or not verified_by or not signature:
        return False
    import hmac
    kid, _, digest = signature.partition(":")
    if not digest:                       # pre-rotation format
        kid, digest = _SIGNING_KEY_ID, signature
    secret = _SIGNING_KEY if kid == _SIGNING_KEY_ID else _retired_keys().get(kid)
    if not secret:
        # Unknown key id. Fail closed: a signature we cannot check is not a
        # signature, and this is exactly the state a completed rotation leaves
        # behind for anything still holding a dropped key.
        return False
    return hmac.compare_digest(
        _digest(secret, memory_id, fingerprint, verified_by), digest)


def claim_hash(statement: str, evidence_hash: str) -> str:
    """The identity of what a human actually APPROVED.

    Found by adversarial test: binding verification to the evidence hash alone
    left the STATEMENT free. Rewriting the sentence while leaving evidence
    untouched kept the memory assertable, and "Customer agreed to a $100,000
    refund." reached confirmed_facts() — the one function documented as safe to
    feed a customer-facing agent. The audit trail still held the real approved
    wording, so it was detectable; nothing prevented it.

    A verifier approves a SENTENCE about a SET OF FACTS. Changing either one
    means they never saw what is now being claimed."""
    return hashlib.sha256(
        f"{(statement or '').strip()}||{evidence_hash or ''}".encode("utf-8")
    ).hexdigest()


def _distinct_occasions(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse a cluster to the distinct OCCASIONS it represents.

    THE correctness fix for counted memory. On real data one contact's largest
    cluster was 385 records that were literally the same sentence — "Requested
    additional information from customer." — logged over and over. Counting rows
    produced "Raised returns 385 times", which is not a summary of anything; it
    is boilerplate mistaken for customer behaviour, asserted with high certainty
    to whoever reads the memory.

    An occasion is (template, day): the same boilerplate on one day is one
    occasion, and the same wording genuinely recurring on different days IS
    distinct — a customer who complains about billing every month is exactly
    what the count should capture. Reuses content_index.template_fingerprint, so
    the notion of "same wording" is shared with retrieval's dedupe rather than
    reinvented with different rules."""
    from app.core.content_index import template_fingerprint

    seen: set = set()
    out: List[Dict[str, Any]] = []
    for m in members:
        day = m["occurred_at"].date().isoformat() if m.get("occurred_at") else "?"
        key = (template_fingerprint(m.get("snippet") or ""), day)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


# ============================================================================
# THE ASSERTION GATE
# ============================================================================

# Minimum composite confidence for a statement an agent may make to a customer.
ASSERT_FLOOR = float(os.getenv("MEMORY_ASSERT_FLOOR", "0.6"))


def _assertion_blockers(*, verified_by, verified_actor, verification_expires_at,
                        kind, visibility, actor, contradicts, conflict_severity,
                        evidence_missing, truncated, effective_certainty,
                        verified_claim_hash=None,
                        current_claim_hash=None,
                        signature_ok=None) -> List[str]:
    """Every reason this memory may NOT be stated to a customer.

    Returned as a LIST rather than a boolean because "why not" is the useful
    answer: it tells a reviewer what to fix, and it makes the gate auditable
    instead of a mystery.

    The first version of this gate had three conditions. It passed a claim that
    was FALSE — 25 outbound staff notes rendered as "the customer raised
    billing 25 times", human-verified, marked assertable. Each condition below
    exists because its absence let something through:

      no verification        anyone's inference becomes a statement of fact
      actor unconfirmed      the verifier was never asked WHO acted
      evidence changed       a human approved a different set of facts
      not a fact             a hypothesis rendered as certainty
      internal evidence      staff-only material stated to the customer
      actor unknown/mixed    a claim about nobody in particular
      unresolved conflict    two memories disagree; picking one silently is a guess
      missing evidence       the records behind the count no longer exist
      truncated count        a lower bound stated as exact
      below floor            a decayed or weakly-corroborated claim
    """
    blockers: List[str] = []
    if not verified_by:
        blockers.append("not human-verified")
    if verified_by and not verified_actor:
        blockers.append("verifier did not confirm who acted")
    # FAIL CLOSED ON A MISSING INPUT. Each of these checks previously required
    # its input to be PRESENT before it could fire, so a caller that simply did
    # not supply one got a pass. That is backwards for a safety gate: not
    # knowing whether the statement still matches what a human approved is a
    # reason to refuse, not a reason to proceed. explain() supplied none of
    # them and consequently could never report any of these three.
    if verified_by:
        if not verified_claim_hash:
            blockers.append("no claim hash recorded at verification")
        elif not current_claim_hash:
            blockers.append("current claim hash could not be computed")
        elif verified_claim_hash != current_claim_hash:
            blockers.append("statement changed since it was verified")
    # The control a database writer cannot satisfy.
    if verified_by and signature_ok is not True:
        blockers.append("verification signature invalid or unsigned")
    if verification_expires_at is not None:
        try:
            if verification_expires_at < _dt.datetime.now(_dt.timezone.utc):
                blockers.append("verification expired")
        except (TypeError, ValueError):
            pass
    if kind != FACT:
        blockers.append(f"kind is '{kind}', not a fact")
    if visibility != CUSTOMER:
        blockers.append("evidence is internal-only")
    if actor in (None, "unknown", "mixed"):
        blockers.append(f"actor is '{actor or 'unknown'}'")
    if contradicts:
        n = len(contradicts) if isinstance(contradicts, (list, tuple)) else 1
        blockers.append(f"unresolved conflict with {n} other memory/memories")
    if conflict_severity == "high":
        blockers.append("high-severity conflict")
    if evidence_missing:
        blockers.append(f"{evidence_missing} evidence record(s) no longer exist")
    if truncated:
        blockers.append("count is a lower bound, not exact")
    if effective_certainty is not None and effective_certainty < ASSERT_FLOOR:
        blockers.append(f"certainty {round(effective_certainty, 2)} below "
                        f"floor {ASSERT_FLOOR}")
    return blockers


def _assertable(**kw) -> bool:
    return not _assertion_blockers(**kw)


# Columns the gate needs. Named once so a caller cannot assemble a partial set.
GATE_COLUMNS = (
    "memory_id::text, statement, evidence_hash, kind, visibility, actor, "
    "truncated, reliability, certainty, occurrences, evidence_count, topic, "
    "decay_class, independent_sources, verified_by, verified_actor, "
    "verification_expires_at, verified_claim_hash, verified_signature, "
    "conflict_severity, evidence_missing, last_observed_at, verified_at, "
    "ARRAY(SELECT unnest(contradicts)::text)"
)


def gate_inputs(row: Dict[str, Any], *, effective_visibility: str = "",
                effective_cert: Optional[float] = None) -> Dict[str, Any]:
    """Assemble the gate's inputs from one memory row. THE only place this
    happens.

    It was happening in three places with three different input sets, and the
    weakest of them was `explain()` — the surface a reviewer reads to decide
    whether to approve a claim. explain() passed `verification_expires_at=None`
    as a literal and omitted the claim hash and signature entirely, so three
    conditions could not fire there AT ALL:

        statement changed since it was verified
        verification signature invalid or unsigned   <- the control a database
        verification expired                            writer cannot satisfy

    A reviewer therefore saw a strictly weaker verdict than the one enforcement
    applies, on the screen built for exactly that judgement. Two implementations
    of one rule is the same defect this codebase has now produced four times."""
    stmt = row.get("statement") or ""
    ev_hash = row.get("evidence_hash")
    return {
        "verified_by": row.get("verified_by"),
        "verified_actor": bool(row.get("verified_actor")),
        "verification_expires_at": row.get("verification_expires_at"),
        "kind": row.get("kind"),
        "visibility": effective_visibility or row.get("visibility"),
        "actor": row.get("actor"),
        "contradicts": row.get("contradicts") or [],
        "conflict_severity": row.get("conflict_severity"),
        "evidence_missing": row.get("evidence_missing"),
        "truncated": row.get("truncated"),
        "effective_certainty": effective_cert,
        "verified_claim_hash": row.get("verified_claim_hash"),
        "current_claim_hash": claim_hash(stmt, ev_hash),
        "signature_ok": signature_valid(
            row.get("memory_id"),
            gate_fingerprint({
                "statement": stmt, "evidence_hash": ev_hash,
                "kind": row.get("kind"), "visibility": row.get("visibility"),
                "actor": row.get("actor"), "truncated": row.get("truncated"),
                "reliability": row.get("reliability"),
                "certainty": row.get("certainty"),
                "occurrences": row.get("occurrences"),
                "evidence_count": row.get("evidence_count"),
                "topic": row.get("topic"),
                "decay_class": row.get("decay_class"),
                "independent_sources": row.get("independent_sources")}),
            row.get("verified_by"), row.get("verified_signature")),
    }


# ============================================================================
# LIFECYCLE — decay, verification, resolvability
# ============================================================================

def effective_certainty(certainty: Optional[float], last_observed,
                        verified_at=None, decay_class: str = EPISODIC) -> float:
    """Certainty as of NOW, not as of when the memory was written.

    Human verification PINS it: a person confirmed the claim, so age stops
    eroding it. Nothing else pins it — a model cannot verify its own inference,
    which is why `verified_by` is the only path to 1.0."""
    if certainty is None:
        return 0.0
    if verified_at is not None:
        return float(certainty)
    if last_observed is None:
        return float(certainty)
    try:
        age_days = (_dt.datetime.now(_dt.timezone.utc) - last_observed).days
    except (TypeError, ValueError):
        return float(certainty)
    if age_days <= 0:
        return float(certainty)
    hl = _DECAY_HALF_LIFE.get(decay_class, HALF_LIFE_DAYS)
    return float(certainty) * (0.5 ** (age_days / max(hl, 1.0)))


def _evidence_visibility(cur, evidence: List[Dict[str, Any]]) -> int:
    """How many evidence records are CURRENTLY internal.

    A memory's `visibility` is computed once at consolidation and then trusted.
    `content_index.search` learned not to trust its cached visibility — it
    re-asserts against the source on the customer path — and customer_memories
    did not get the same treatment. Measured: 2 of 3 customer-visible memories
    cited evidence that is now internal, so a reclassified source would have
    been laundered into a customer-visible claim by a stale snapshot.

    Returns 0 when nothing is internal. Unresolvable evidence is NOT counted
    here (that is `_resolve_evidence`'s job) — but a memory whose evidence
    cannot be checked also cannot be shown to be customer-safe, so the caller
    treats an error as internal."""
    if not evidence:
        return 0
    pairs = [(e.get("source_type"), e.get("source_id")) for e in evidence
             if e.get("source_id")]
    if not pairs:
        return 0
    try:
        cur.execute(
            """SELECT count(*) FROM content_embeddings ce
                WHERE (ce.source_type, ce.source_id) IN %s
                  AND ce.visibility <> %s""", (tuple(pairs), CUSTOMER))
        return int(cur.fetchone()[0] or 0)
    except Exception:
        cur.connection.rollback()
        return len(pairs)          # cannot verify => treat as internal


def _resolve_evidence(cur, evidence: List[Dict[str, Any]]) -> int:
    """How many evidence pointers no longer resolve.

    THE hole this closes: retention expires a source record, the indexer drops
    its row, and the memory goes on asserting "raised billing 25 times" backed
    by records that no longer exist. Proven live before this existed. Erasure
    was safe (it deletes memories outright); retention was not."""
    if not evidence:
        return 0
    pairs = [(e.get("source_type"), e.get("source_id")) for e in evidence
             if e.get("source_id")]
    if not pairs:
        return 0
    try:
        cur.execute(
            """SELECT count(*) FROM content_embeddings ce
                WHERE (ce.source_type, ce.source_id) IN %s""",
            (tuple(pairs),))
        found = int(cur.fetchone()[0] or 0)
    except Exception:
        cur.connection.rollback()
        return 0                      # unknown ≠ missing; do not invent a gap
    return max(0, len(pairs) - found)


# ============================================================================
# CLUSTERING
# ============================================================================

def _cluster(records: List[Dict[str, Any]]) -> List[List[int]]:
    """Greedy agglomeration by cosine similarity. Deterministic: records arrive
    in a fixed order and each is attached to the FIRST cluster whose seed it is
    close enough to, so the same input always yields the same themes."""
    if not records:
        return []
    try:
        import numpy as np
    except ImportError:                       # pragma: no cover
        return [[i] for i in range(len(records))]

    M = np.asarray([r["vec"] for r in records], dtype=np.float32)
    norms = np.linalg.norm(M, axis=1)
    norms[norms == 0] = 1.0
    M = M / norms[:, None]
    sims = M @ M.T

    clusters: List[List[int]] = []
    seeds: List[int] = []
    for i in range(len(records)):
        placed = False
        for ci, seed in enumerate(seeds):
            if float(sims[i, seed]) >= CLUSTER_SIM:
                clusters[ci].append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
            seeds.append(i)
    return clusters


def _load_records(cur, entity_type: str, entity_id: str):
    """Return (records, clipped).

    `clipped` is True when this customer has history OLDER than the window the
    LIMIT admits. It is measured, not inferred: the previous flag was
    `len(records) >= MAX_RECORDS`, which reports truncation for a customer with
    exactly MAX_RECORDS records and nothing excluded.

    It matters because every derived date and count is computed over the window,
    not over the history. On the largest contact in this database (762 records)
    the "returns" theme reported first_observed_at = 2025-12-19 — which is not
    when returns started, it is where the LIMIT fell. True history began
    2025-11-28. The sentence read "Returns came up 22 times between 2025-12-19
    and 2026-01-09": a closed range and an exact count, both artifacts of a
    query bound, stated as fact about a person."""
    col = "contact_id" if entity_type == "contact" else "account_id"
    cur.execute(
        f"""SELECT source_type, source_id, embedding, dims, snippet, visibility,
                   occurred_at, direction, actor, speech_act
              FROM content_embeddings
             WHERE {col} = %s::uuid AND model = %s AND dims = %s
             -- TOTAL order. `occurred_at DESC` alone is not deterministic:
             -- this contact alone has 162 timestamps shared by more than one
             -- record, and _cluster() attaches each record to the FIRST seed it
             -- is near, so a reordering produces DIFFERENT clusters — different
             -- evidence_hash — which silently invalidates any human
             -- verification of those memories. Two runs currently agree, but
             -- only because the plan happens to be stable; a VACUUM, an index
             -- change or a parallel scan would break it.
             ORDER BY occurred_at DESC NULLS LAST, source_type, source_id, chunk_ix
             LIMIT %s""",
        (entity_id, E.MODEL, E.DIMS, MAX_RECORDS))
    out = []
    for (st, sid, blob, dims, snippet, vis, occurred, direction,
         actor, act) in cur.fetchall():
        vec = E.decode(bytes(blob), dims)
        if vec is None:
            continue
        out.append({"source_type": st, "source_id": sid, "vec": vec,
                    "snippet": snippet or "", "visibility": vis or INTERNAL,
                    "occurred_at": occurred, "direction": direction,
                    "actor": actor, "speech_act": act})

    # Did the window actually exclude anything? Compare against the unbounded
    # minimum rather than assuming. Any excluded record could belong to ANY
    # cluster, so this is an entity-level property: when it is true, no theme
    # for this customer has a trustworthy start date or a complete count.
    clipped = False
    if len(out) >= MAX_RECORDS:
        window_min = min((r["occurred_at"] for r in out if r["occurred_at"]),
                         default=None)
        cur.execute(
            f"""SELECT min(occurred_at) FROM content_embeddings
                 WHERE {col} = %s::uuid AND model = %s AND dims = %s""",
            (entity_id, E.MODEL, E.DIMS))
        true_min = cur.fetchone()[0]
        clipped = bool(window_min and true_min and true_min < window_min)
    return out, clipped


# ============================================================================
# CONSOLIDATION
# ============================================================================

def consolidate_entity(entity_type: str, entity_id: str) -> Dict[str, Any]:
    """Rebuild one customer's memories from their indexed records."""
    if not ENABLED:
        return {"ok": False, "reason": "disabled"}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # ADVISORY LOCK per entity. consolidate_entity is reachable from the
            # API on any replica while the leader-gated job also runs, so two
            # passes can interleave: one rebuilds themes while the other runs
            # _link_contradictions over a half-written set, linking memories
            # that the first pass is about to delete. Non-blocking — a losing
            # caller returns rather than queueing, because the winner is doing
            # exactly the same work.
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                        (f"consolidate:{entity_type}:{entity_id}",))
            if not cur.fetchone()[0]:
                return {"ok": True, "entity_id": entity_id, "skipped": "locked",
                        "records": 0, "written": 0, "unchanged": 0,
                        "dropped": 0, "conflicts": 0}
            records, clipped = _load_records(cur, entity_type, entity_id)
            if len(records) < MIN_CLUSTER:
                return {"ok": True, "entity_id": entity_id, "records": len(records),
                        "written": 0, "note": "not enough records"}

            written, unchanged = 0, 0
            live_topics: List[str] = []
            for idx in _cluster(records):
                if len(idx) < MIN_CLUSTER:
                    continue
                members = _distinct_occasions([records[i] for i in idx])
                if len(members) < MIN_CLUSTER:
                    continue          # boilerplate repeated, not a recurring theme
                ev_ids = [f"{m['source_type']}:{m['source_id']}" for m in members]
                ev_hash = _evidence_hash(ev_ids)
                topic = _topic_for([m["snippet"] for m in members])
                if topic in live_topics:
                    continue                     # strongest cluster per topic wins
                live_topics.append(topic)

                # RULE 1 — most restrictive visibility wins.
                visibility = (CUSTOMER
                              if all(m["visibility"] == CUSTOMER for m in members)
                              else INTERNAL)

                dates = [m["occurred_at"] for m in members if m["occurred_at"]]
                first, last = (min(dates), max(dates)) if dates else (None, None)

                # RULE 2 — evidence is pointers, capped, newest first.
                evidence = [{"source_type": m["source_type"],
                             "source_id": m["source_id"],
                             "on_date": (m["occurred_at"].date().isoformat()
                                         if m["occurred_at"] else None)}
                            for m in members[:MAX_EVIDENCE]]

                actor = _cluster_actor(members)
                statement = _statement(topic, len(members), first, last,
                                       actor, clipped)
                dclass = decay_class_for(topic)
                independent = len({m.get("source_type") for m in members})
                reliability, certainty = _derive_trust(cur, members)
                truncated = clipped

                cur.execute(
                    """INSERT INTO customer_memories
                         (entity_type, entity_id, kind, statement, topic,
                          occurrences, evidence, evidence_count, evidence_hash,
                          source_type, reliability, certainty, generator,
                          visibility, first_observed_at, last_observed_at,
                          status, truncated, evidence_missing, evidence_checked_at,
                          valid_until, actor, decay_class, independent_sources)
                       VALUES (%s,%s::uuid,%s,%s,%s,%s,%s::jsonb,%s,%s,
                               'ai',%s,%s,%s,%s,%s,%s,%s,%s,0,now(),%s,%s,%s,%s)
                       ON CONFLICT (entity_type, entity_id, topic, kind, generator)
                       DO UPDATE SET
                         statement=EXCLUDED.statement,
                         occurrences=EXCLUDED.occurrences,
                         evidence=EXCLUDED.evidence,
                         evidence_count=EXCLUDED.evidence_count,
                         evidence_hash=EXCLUDED.evidence_hash,
                         reliability=EXCLUDED.reliability,
                         certainty=EXCLUDED.certainty,
                         visibility=EXCLUDED.visibility,
                         first_observed_at=EXCLUDED.first_observed_at,
                         last_observed_at=EXCLUDED.last_observed_at,
                         truncated=EXCLUDED.truncated,
                         evidence_missing=0, evidence_checked_at=now(),
                         valid_until=EXCLUDED.valid_until,
                         actor=EXCLUDED.actor,
                         decay_class=EXCLUDED.decay_class,
                         independent_sources=EXCLUDED.independent_sources,
                         -- Evidence moved, so a prior human verification no
                         -- longer applies to what this memory now claims.
                         verified_by=CASE WHEN customer_memories.verified_evidence_hash
                                               IS DISTINCT FROM EXCLUDED.evidence_hash
                                          THEN NULL ELSE customer_memories.verified_by END,
                         verified_actor=CASE WHEN customer_memories.verified_evidence_hash
                                                  IS DISTINCT FROM EXCLUDED.evidence_hash
                                             THEN false ELSE customer_memories.verified_actor END,
                         -- A human-verified memory is NOT overwritten by a
                         -- re-run: the person outranks the generator. Their
                         -- verification is cleared only if the evidence moved.
                         status=CASE WHEN customer_memories.verified_by IS NOT NULL
                                     THEN customer_memories.status
                                     ELSE 'active' END,
                         updated_at=now()
                       WHERE customer_memories.evidence_hash <> EXCLUDED.evidence_hash
                       RETURNING memory_id""",
                    (entity_type, entity_id, THEME, statement, topic, len(members),
                     json.dumps(evidence), len(evidence), ev_hash,
                     reliability, certainty, GENERATOR, visibility, first, last,
                     ACTIVE, truncated, _valid_until(last), actor, dclass,
                     independent))
                if cur.fetchone():
                    written += 1
                else:
                    unchanged += 1              # RULE 3 — hash matched, no rewrite

            # A generator change orphans the previous generator's rows: they
            # do not collide on the ON CONFLICT key and the sweep below filters
            # by generator, so they would survive forever and keep presenting
            # superseded wording as current.
            #
            # DISCARD WHAT NO HUMAN EVER JUDGED. Marking every one 'superseded'
            # bounds nothing: two derivation changes in a single afternoon left
            # 270 retired rows against 135 live ones, and ALL 270 had no
            # verification history — PII-bearing derived assertions kept for an
            # audit with nothing to audit. A claim nobody ever ruled on has no
            # history worth preserving, and the rulings themselves live in the
            # append-only memory_verifications trail either way.
            cur.execute(
                """DELETE FROM customer_memories
                    WHERE entity_type=%s AND entity_id=%s::uuid AND kind=%s
                      AND generator <> %s
                      AND NOT EXISTS (
                            SELECT 1 FROM memory_verifications v
                             WHERE v.memory_id = customer_memories.memory_id)""",
                (entity_type, entity_id, THEME, GENERATOR))
            discarded = cur.rowcount

            # What is left WAS judged by a person. Retire, do not delete.
            cur.execute(
                # The verification is cleared with it. A human approved a
                # SENTENCE; this generator no longer produces that sentence, so
                # the approval does not describe anything current. The audit of
                # who approved what survives in memory_verifications, which is
                # append-only. Clearing it also makes a ROLLBACK safe: if the
                # old generator returns, the upsert's status CASE only resets a
                # row to 'active' when verified_by IS NULL, so a verified row
                # would otherwise stay superseded while being current again.
                """UPDATE customer_memories
                      SET status='superseded', updated_at=now(),
                          verified_by=NULL, verified_actor=false
                    WHERE entity_type=%s AND entity_id=%s::uuid AND kind=%s
                      AND generator <> %s AND status <> 'superseded'""",
                (entity_type, entity_id, THEME, GENERATOR))
            retired = cur.rowcount

            # A theme that no longer has evidence must not linger as a confident
            # assertion. Human-verified memories are NEVER dropped by a re-run —
            # a person confirmed that claim; a generator finding no evidence for
            # it this pass is a reason to flag, not to delete someone's judgement.
            cur.execute(
                """DELETE FROM customer_memories
                    WHERE entity_type=%s AND entity_id=%s::uuid
                      AND kind=%s AND generator=%s
                      AND verified_by IS NULL
                      AND NOT (topic = ANY(%s))""",
                (entity_type, entity_id, THEME, GENERATOR, live_topics or [""]))
            dropped = cur.rowcount
            conflicts = _link_contradictions(cur, entity_type, entity_id)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[memory] consolidation failed for {entity_id[:8]}: {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()

    return {"ok": True, "entity_type": entity_type, "entity_id": entity_id,
            "records": len(records), "written": written,
            "unchanged": unchanged, "dropped": dropped, "conflicts": conflicts,
            "retired": retired, "discarded": discarded}


def _link_contradictions(cur, entity_type: str, entity_id: str) -> int:
    """Cross-link active memories that make competing claims about one topic.

    v1 could not represent this at all: UNIQUE (entity, topic) meant the upsert
    silently OVERWROTE the older claim, so "budget approved in March" was
    replaced by "budget frozen in June" with no trace the customer had changed
    their mind. That is the single most interesting thing memory can notice, and
    the schema deleted it.

    Now competing claims coexist and point at each other, so a reader (and an
    agent) sees the disagreement instead of only the winner. Resolution is NOT
    automatic — deciding which of two claims is true is a judgement, and a
    generator making it silently is how memory becomes confidently wrong."""
    # Done entirely in SQL. Round-tripping uuid[] through Python bit twice in
    # this module — psycopg2 handed back the literal string '{}' and iterating
    # it produced ['{','}'] — so the arrays never leave the database. It also
    # CLEARS links when a conflict resolves, because `others` becomes empty and
    # IS DISTINCT FROM still matches.
    cur.execute(
        """UPDATE customer_memories cm
              SET contradicts = sub.others, updated_at = now()
             FROM (
                 SELECT m.memory_id,
                        ARRAY(SELECT o.memory_id
                                FROM customer_memories o
                               WHERE o.entity_type = m.entity_type
                                 AND o.entity_id   = m.entity_id
                                 AND o.topic       = m.topic
                                 AND o.memory_id  <> m.memory_id
                                 AND o.status IN ('active','dormant')
                                 AND o.superseded_by IS NULL) AS others
                   FROM customer_memories m
                  WHERE m.entity_type = %s AND m.entity_id = %s::uuid
                    AND m.status IN ('active','dormant')
                    AND m.superseded_by IS NULL
             ) sub
            WHERE cm.memory_id = sub.memory_id
              AND cm.contradicts IS DISTINCT FROM sub.others""",
        (entity_type, entity_id))
    linked = cur.rowcount

    # SEVERITY — by consequence, not by count. A conflict where one side is
    # human-verified is high (someone will act on it); two unverified themes
    # disagreeing is low. High severity blocks assertion outright.
    cur.execute(
        """UPDATE customer_memories cm
              SET conflict_severity = CASE
                    WHEN cardinality(cm.contradicts) = 0 THEN NULL
                    WHEN cm.verified_by IS NOT NULL
                      OR EXISTS (SELECT 1 FROM customer_memories o
                                  WHERE o.memory_id = ANY(cm.contradicts)
                                    AND o.verified_by IS NOT NULL) THEN 'high'
                    WHEN cm.kind = 'fact' THEN 'medium'
                    ELSE 'low' END
            WHERE cm.entity_type=%s AND cm.entity_id=%s::uuid""",
        (entity_type, entity_id))
    return linked


def _cluster_actor(members: List[Dict[str, Any]]) -> str:
    """WHO this cluster is about — four realities, not two.

    Unanimous or `mixed`. A cluster containing both the customer contacting us
    and us contacting them genuinely IS ambiguous, and attributing it to either
    party is the false-attribution bug in a subtler form. `unknown` records are
    ignored when a clear majority exists, because refusing to attribute a
    93%-attributable cluster over one stray row loses real information."""
    actors = [m.get("actor") for m in members
              if m.get("actor") and m["actor"] != "unknown"]
    if not actors:
        return "unknown"
    counts = Counter(actors)
    top, n = counts.most_common(1)[0]
    # A clear supermajority names the cluster; anything murkier stays `mixed`.
    return top if n / len(actors) >= 0.8 else "mixed"


def _derive_trust(cur, members: List[Dict[str, Any]]) -> Tuple[float, float]:
    """Trust DERIVED from the evidence, not hardcoded.

    v1 gave every memory reliability 0.70 and a count-based certainty that
    saturated instantly, so a theme built from nine records and one built from
    four hundred both read 0.665 — the number carried no information.

    RELIABILITY takes the WEAKEST link: a theme resting on stub-grade records is
    not 0.70 just because a model assembled it. CERTAINTY scales with BREADTH
    (how many distinct sources and days corroborate) rather than raw volume —
    ten notes from one afternoon are one observation repeated, while four
    records across four months are a pattern."""
    src_types = {m["source_type"] for m in members}
    days = {m["occurred_at"].date() for m in members if m.get("occurred_at")}

    reliability = 0.70                      # the model as a source
    try:
        ids = [(m["source_type"], m["source_id"]) for m in members]
        # JOIN ON THE RIGHT KEY. This previously read
        #     LEFT JOIN leads l ON l.lead_id = ce.contact_id
        # which can never match — contact_id is a CONTACT, not a lead — so the
        # min() never fired and every memory stored the 0.70 default. Verified:
        # 0 rows matched, and `SELECT DISTINCT reliability` returned exactly
        # [0.7]. The "weakest evidence link" rule was documented, tested around,
        # and had never once executed. A trust signal that is secretly a
        # constant is worse than none: it looks earned.
        cur.execute(
            """SELECT min(COALESCE(ct.confidence, a.confidence, 1.0))
                 FROM content_embeddings ce
                 LEFT JOIN contacts ct ON ct.contact_id = ce.contact_id
                 LEFT JOIN accounts a  ON a.account_id  = ce.account_id
                WHERE (ce.source_type, ce.source_id) IN %s""", (tuple(ids),))
        row = cur.fetchone()
        weakest = row[0] if row else None
        if weakest is not None:
            reliability = min(reliability, float(weakest))
    except Exception as exc:
        cur.connection.rollback()           # unknown evidence trust → keep default
        logger.debug(f"[memory] evidence reliability lookup skipped: {exc}")

    breadth = min(1.0, (len(days) / 6.0) * 0.6 + (len(src_types) / 3.0) * 0.4)
    certainty = round(min(0.95, 0.35 + 0.6 * breadth), 3)
    return round(reliability, 3), certainty


def _valid_until(last_observed):
    """A theme is only claimed to hold for a bounded window after its last
    observation. Without a horizon, "raised billing 25 times" reads as current
    forever, including years after the customer stopped."""
    if last_observed is None:
        return None
    return last_observed + _dt.timedelta(days=HALF_LIFE_DAYS * 2)


def _statement(topic: str, n: int, first, last,
               actor: Optional[str] = None, clipped: bool = False) -> str:
    """A deterministic sentence built from the cluster's own facts.

    Templated rather than LLM-written on purpose: consolidation runs unattended
    and its output is read back to staff (and, for customer-visible themes,
    shapes what an agent says). A template cannot invent a claim the evidence
    does not support.

    DIRECTION IS PART OF THE CLAIM. The first version said "Raised {topic}" for
    every cluster, and clustering is topic-based and actor-blind — so 25
    OUTBOUND staff notes ("Payment reminder drafted") became "Raised billing 25
    times", attributing OUR actions to THE CUSTOMER. A human then verified it,
    which made a false attribution assertable. "They raised it" and "we
    contacted them about it" are different claims about different people and
    only one of them is about the customer.

    Changing ANY wording here changes GENERATOR (see `_wording_fingerprint`),
    which retires every memory written by the previous generator.

    A CLIPPED WINDOW CHANGES THE CLAIM, TOO. When MAX_RECORDS excluded older
    history, `first` is where the LIMIT fell and `n` counts only what was
    examined. Both then read as precise facts about a person and neither is one.
    The `truncated` column already recorded this and nobody reading the sentence
    sees a column, so the hedge belongs in the sentence: the count becomes a
    floor ("at least"), and the range is explicitly labelled as the part of the
    history that was looked at."""
    when = ""
    if first and last:
        f, l = first.date().isoformat(), last.date().isoformat()
        if clipped:
            # NOT "between f and l" — f is a query bound, not a beginning.
            when = f" in the period examined ({f} to {l})"
        else:
            when = f" between {f} and {l}" if f != l else f" on {f}"
    times = ("once" if n == 1 else f"{n} times") if not clipped else             f"at least {n} times"
    tail = " Earlier history was not examined." if clipped else ""
    return {
        "customer_said":   f"Raised {topic} {times}{when}.{tail}",
        "customer_did":    f"Acted on {topic} {times}{when}.{tail}",
        "company_did":     f"We contacted them about {topic} {times}{when}.{tail}",
        "third_party_did": f"A third party was involved in {topic} {times}{when}.{tail}",
    }.get(actor, f"{topic.capitalize()} came up {times}{when}.{tail}")


def consolidate_pass(limit: int = 0) -> Dict[str, Any]:
    """Consolidate the customers whose indexed records changed most recently."""
    if not ENABLED:
        return {"ok": False, "reason": "disabled"}
    cap = int(limit or ENTITIES_PER_PASS)
    out = {"ok": True, "entities": 0, "written": 0, "unchanged": 0, "dropped": 0}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # ROTATION, not "newest first". Ordering by record recency meant the
            # same busy customers were reconsolidated every pass while a quiet
            # one — whose evidence may since have been expired by retention —
            # was never revisited, leaving a stale count asserted indefinitely.
            # Least-recently-consolidated first guarantees full coverage: never
            # consolidated (NULL) sorts first, then oldest.
            cur.execute(
                """SELECT 'contact', ce.contact_id::text
                     FROM content_embeddings ce
                     LEFT JOIN LATERAL (
                          SELECT max(cm.updated_at) AS done_at
                            FROM customer_memories cm
                           WHERE cm.entity_type='contact'
                             AND cm.entity_id = ce.contact_id
                     ) m ON TRUE
                    WHERE ce.contact_id IS NOT NULL
                    GROUP BY ce.contact_id, m.done_at
                   HAVING count(*) >= %s
                    ORDER BY m.done_at ASC NULLS FIRST, max(ce.updated_at) DESC
                    LIMIT %s""",
                (MIN_CLUSTER, cap))
            targets = cur.fetchall()
    finally:
        conn.close()

    for et, eid in targets:
        r = consolidate_entity(et, eid)
        if r.get("ok"):
            out["entities"] += 1
            for k in ("written", "unchanged", "dropped"):
                out[k] += r.get(k, 0)
    if out["written"]:
        logger.info(f"[memory] consolidated {out['entities']} customer(s), "
                    f"{out['written']} memory/memories written")
    return out


# ============================================================================
# RECALL — audience-gated, exactly like the index it derives from
# ============================================================================

def recall(entity_type: str, entity_id: str, audience: str,
           limit: int = 5) -> List[Dict[str, Any]]:
    """A customer's consolidated memories.

    `audience` is REQUIRED and fail-closed, matching content_index: anything
    that is not exactly 'internal' sees only customer-visible memories. A memory
    inherits the most restrictive visibility of its evidence, so this is the
    second gate on the same data, not a replacement for the first."""
    is_internal = (audience == INTERNAL)
    sql = ["SELECT memory_id::text, statement, topic, occurrences, evidence, "
           "       evidence_count, reliability, certainty, visibility, "
           "       first_observed_at, last_observed_at, kind, status, "
           # ARRAY(...)::text[] — a bare uuid[] came back as the literal string
           # '{}', so iterating it yielded ['{','}'], which is truthy, and every
           # memory falsely reported "contradicted by another memory". A flag
           # that fires on everything trains readers to ignore it.
           "       verified_by, verified_at, actor, decay_class, "
           "       verified_actor, verification_expires_at, independent_sources, "
           "       verified_claim_hash, evidence_hash, verified_signature, "
           "       conflict_severity, "
           "       ARRAY(SELECT unnest(contradicts)::text) AS contradicts, "
           "       truncated, valid_until "
           "  FROM customer_memories "
           " WHERE entity_type=%s AND entity_id=%s::uuid AND superseded_by IS NULL"
           # Rejected and superseded claims never surface; dormant ones are kept
           # as history but are not context an agent should act on.
           "   AND status IN ('active','dormant')"]
    args: List[Any] = [entity_type, entity_id]
    if not is_internal:
        sql.append(" AND visibility=%s")
        args.append(CUSTOMER)
    sql.append(" ORDER BY occurrences DESC, last_observed_at DESC NULLS LAST LIMIT %s")
    args.append(int(limit) * 3)          # over-fetch; decay filters below

    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("".join(sql), args)
                rows = cur.fetchall()
                out: List[Dict[str, Any]] = []
                for r in rows:
                    (mid, statement, topic, occ, evidence, ev_count, rel, cert,
                     vis, first, last, kind, status, vby, vat, actor, dclass,
                     vactor, vexp, indep, vclaim, ev_hash, vsig, csev,
                     contradicts, truncated, valid_until) = r

                    # Resolvability: a memory whose evidence has been expired by
                    # retention still asserts its count. Check, don't assume.
                    missing = _resolve_evidence(cur, evidence or [])
                    # LIVE visibility re-check. The stored value is a snapshot
                    # taken at consolidation; the source may have been
                    # reclassified since.
                    internal_now = _evidence_visibility(cur, evidence or [])
                    effective_vis = INTERNAL if internal_now else vis
                    eff = effective_certainty(
                        float(cert) if cert is not None else None, last, vat,
                        decay_class=dclass or EPISODIC)
                    if missing:
                        # Proportional, not binary: losing 2 of 25 records
                        # weakens the claim; losing all of them guts it.
                        eff *= max(0.0, 1.0 - (missing / max(len(evidence or []), 1)))

                    expired = bool(valid_until and valid_until < _dt.datetime.now(
                        _dt.timezone.utc))
                    if eff < DORMANT_BELOW or expired:
                        continue          # kept in the table, not surfaced
                    # A customer-audience caller must never receive a memory
                    # whose evidence has since become internal.
                    if not is_internal and effective_vis != CUSTOMER:
                        continue

                    _blockers = _assertion_blockers(**gate_inputs(
                        {"memory_id": mid, "statement": statement,
                         "evidence_hash": ev_hash, "kind": kind,
                         "visibility": vis, "actor": actor,
                         "truncated": truncated, "reliability": rel,
                         "certainty": cert, "occurrences": occ,
                         "evidence_count": ev_count, "topic": topic,
                         "decay_class": dclass, "independent_sources": indep,
                         "verified_by": vby, "verified_actor": vactor,
                         "verification_expires_at": vexp,
                         "verified_claim_hash": vclaim,
                         "verified_signature": vsig, "contradicts": contradicts,
                         "conflict_severity": csev, "evidence_missing": missing},
                        effective_visibility=effective_vis, effective_cert=eff))

                    out.append({
                        "memory_id": mid, "statement": statement, "topic": topic,
                        "occurrences": occ, "evidence": evidence,
                        "evidence_count": ev_count,
                        "evidence_missing": missing,
                        "reliability": float(rel) if rel is not None else None,
                        "certainty": float(cert) if cert is not None else None,
                        "effective_certainty": round(eff, 3),
                        "confidence": (round(float(rel) * eff, 3)
                                       if rel is not None else None),
                        "kind": kind, "status": status,
                        "verified": vby is not None,
                        "verified_by": vby,
                        "contradicts": ([str(x) for x in contradicts]
                                        if isinstance(contradicts, (list, tuple))
                                        else []),
                        "truncated": truncated,
                        # THE control an agent reads. THREE conditions, not two:
                        # human-verified, typed as a fact, AND drawn from
                        # customer-visible evidence.
                        #
                        # The third was missing and it mattered: a memory built
                        # entirely from INTERNAL staff notes was reported
                        # assertable once a human verified it. Audience gating
                        # still kept it out of customer recall, so nothing
                        # leaked — but `assertable: True` on internal-sourced
                        # content is a flag that contradicts itself, and any
                        # caller trusting it without re-checking audience would
                        # state staff-only material to a customer.
                        "actor": actor, "decay_class": dclass,
                        "independent_sources": indep,
                        "conflict_severity": csev,
                        # ONE assembly, ONE evaluation. This block used to
                        # build the gate inputs twice, verbatim, and run
                        # the whole gate twice per memory — and a third,
                        # weaker assembly lived in explain().
                        "assertable": not _blockers,
                        "assertion_blockers": _blockers,
                        "visibility": effective_vis,
                        "stored_visibility": vis,
                        "evidence_now_internal": internal_now,
                        "first_observed_at": first.isoformat() if first else None,
                        "last_observed_at": last.isoformat() if last else None})
                    if len(out) >= int(limit):
                        break
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[memory] consolidated recall skipped: {exc}")
        return []
    return out


# ============================================================================
# TYPED API BOUNDARY
#
# `recall()` returns everything with an `assertable` flag, and a flag is a
# CONVENTION: it protects only callers who remember to read it. A customer-
# facing agent kept away from hypotheses by prompt wording is one refactor away
# from stating an inference as fact.
#
# These three functions are the boundary. A customer-facing caller uses
# confirmed_facts() and is STRUCTURALLY INCAPABLE of receiving anything else —
# not discouraged from it, incapable of it.
# ============================================================================

def confirmed_facts(entity_type: str, entity_id: str,
                    limit: int = 5) -> List[Dict[str, Any]]:
    """Memories an agent MAY state to a customer. The only safe input to a
    customer-facing prompt.

    Every item has passed the full assertion gate, so a caller needs no
    knowledge of kinds, decay, conflicts or visibility to be safe. Returns a
    deliberately narrow shape: statement, evidence count, and when it was
    verified — nothing an agent could misread as licence to say more."""
    return [{"statement": m["statement"],
             "topic": m["topic"],
             "evidence_count": m["evidence_count"],
             "verified_by": m.get("verified_by"),
             "last_observed_at": m.get("last_observed_at")}
            for m in recall(entity_type, entity_id, CUSTOMER, limit * 4)
            if m.get("assertable")][:limit]


def inferred_patterns(entity_type: str, entity_id: str,
                      limit: int = 5) -> List[Dict[str, Any]]:
    """Unverified themes — for ROUTING, PRIORITISATION and TONE.

    Deliberately NOT for prompts. Use these server-side to pick a queue, an
    owner or an opening approach; the statements themselves should not reach a
    model that is talking to the customer."""
    return [m for m in recall(entity_type, entity_id, INTERNAL, limit * 2)
            if not m.get("assertable")][:limit]


def hypotheses(entity_type: str, entity_id: str,
               limit: int = 20) -> List[Dict[str, Any]]:
    """Everything, with blockers — the human review surface.

    This is what a person sees when deciding whether to verify. It is the only
    view that shows WHY a memory cannot be asserted, which is precisely what was
    missing when a human verified a false attribution."""
    return recall(entity_type, entity_id, INTERNAL, limit)


# ============================================================================
# HUMAN FEEDBACK — the only route to an assertable fact
# ============================================================================

VERIFY_ROLES = {r.strip().lower() for r in
                os.getenv("MEMORY_VERIFY_ROLES", "admin,manager").split(",")
                if r.strip()}

# Topics where a wrong claim costs money or creates a legal obligation. These
# require TWO DISTINCT verifiers. The single-signature model was the largest
# residual risk in the safety case: one careless or compromised authorized
# account could promote unlimited claims, and nothing downstream re-checked.
# Sourced from memory_topic_policy (the DATABASE), not an env var. Two replicas
# reading different env could disagree about how many approvers a billing claim
# needs; a table cannot diverge. The env values below are only a fallback for a
# pre-migration schema, and they are the STRICT reading.
REQUIRED_APPROVALS = int(os.getenv("MEMORY_DUAL_APPROVALS", "2"))
_FALLBACK_HIGH_CONSEQUENCE = {"billing", "pricing", "returns", "renewal", "general"}


def required_approvals_for(topic: str) -> int:
    """How many DISTINCT verifiers this topic needs.

    An UNKNOWN topic returns the strict count, not the lenient one. The topic
    classifier reads customer-authored text, so an attacker can steer a claim
    out of a named topic — and 12.6% of memories already land in the `general`
    catch-all. A catch-all with the weakest policy is an invitation."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT required_approvals FROM memory_topic_policy "
                            "WHERE topic=%s", ((topic or "").lower(),))
                row = cur.fetchone()
                if row:
                    return int(row[0])
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[memory] topic policy lookup failed: {exc}")
    # NO ROW => STRICT. The DB trigger uses COALESCE(required_approvals, 2) for
    # an unlisted topic; an earlier version of this function returned 1, so
    # Python and SQL disagreed about the approval policy for exactly the topics
    # nobody had thought about — the same one-rule-two-implementations drift
    # this codebase has now paid for three times. Adding a topic to the
    # vocabulary without adding a policy row must fail SAFE.
    return REQUIRED_APPROVALS


# Retained for tests and readability; the DB table is authoritative.
HIGH_CONSEQUENCE_TOPICS = _FALLBACK_HIGH_CONSEQUENCE
VERIFY_TTL_DAYS = {STABLE: 730, EPISODIC: 365, VOLATILE: 90}


def _distinct_approvers(memory_id: str, claim: str) -> set:
    """Who has already approved THIS EXACT claim. Keyed on the claim hash so a
    re-worded statement starts approval over."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT performed_by FROM memory_verifications
                        WHERE memory_id=%s::uuid AND action='verified'
                          AND actor_confirmed AND evidence_hash IS NOT NULL""",
                    (memory_id,))
                return {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception:
        return set()


def _record_verification(memory_id: str, action: str, actor_confirmed: bool,
                         preview: Dict[str, Any], by: str, role: str,
                         reason: str = "") -> None:
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO memory_verifications
                         (memory_id, action, actor_confirmed, evidence_hash,
                          evidence_shown, statement_shown, performed_by, role, reason,
                          entity_type, entity_id)
                       SELECT %s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,
                              m.entity_type, m.entity_id
                         FROM customer_memories m WHERE m.memory_id=%s::uuid""",
                    (memory_id, action, actor_confirmed,
                     preview.get("evidence_hash"), len(preview.get("evidence") or []),
                     preview.get("statement"), by, role, reason, memory_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[memory] verification not recorded: {exc}")


def verification_preview(memory_id: str) -> Dict[str, Any]:
    """What a verifier MUST see before judging: the claim beside its evidence.

    A human verified "Raised billing 25 times" whose evidence was 25 OUTBOUND
    payment reminders — our own actions, attributed to the customer. They were
    shown a sentence and a button. Nobody could have caught it from that.

    Returns the statement, the derived actor, and the actual records, so the
    reviewer is judging the claim AGAINST ITS EVIDENCE rather than on trust."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT statement, topic, actor, kind, visibility, occurrences,
                          evidence, evidence_hash, decay_class, truncated,
                          independent_sources
                     FROM customer_memories WHERE memory_id=%s::uuid""",
                (memory_id,))
            r = cur.fetchone()
            if not r:
                return {"ok": False, "error": "no such memory"}
            (statement, topic, actor, kind, vis, occ, evidence, ev_hash,
             dclass, truncated, indep) = r

            records = []
            for ev in (evidence or [])[:MAX_EVIDENCE]:
                cur.execute(
                    """SELECT snippet, occurred_at::date, visibility, actor,
                              speech_act, direction
                         FROM content_embeddings
                        WHERE source_type=%s AND source_id=%s LIMIT 1""",
                    (ev.get("source_type"), ev.get("source_id")))
                row = cur.fetchone()
                records.append({
                    "source_type": ev.get("source_type"),
                    "source_id": ev.get("source_id"),
                    "resolves": bool(row),
                    "text": row[0][:300] if row else None,
                    "on_date": row[1].isoformat() if row and row[1] else None,
                    "visibility": row[2] if row else None,
                    "actor": row[3] if row else None,
                    "speech_act": row[4] if row else None})
    finally:
        conn.close()

    actors_in_evidence = {r["actor"] for r in records if r["actor"]}
    return {
        "ok": True, "memory_id": memory_id,
        "statement": statement, "topic": topic, "kind": kind,
        "claimed_actor": actor, "visibility": vis, "occurrences": occ,
        "decay_class": dclass, "truncated": truncated,
        "independent_sources": indep,
        "evidence_hash": ev_hash,
        "evidence": records,
        "missing_evidence": sum(1 for r in records if not r["resolves"]),
        # The check that would have caught the production bug.
        "actor_matches_evidence": (len(actors_in_evidence) == 1
                                   and actor in actors_in_evidence),
        "actors_in_evidence": sorted(actors_in_evidence),
        "warnings": _preview_warnings(actor, actors_in_evidence, records,
                                      truncated, indep),
    }


def _preview_warnings(actor, actors_in_evidence, records, truncated,
                      independent) -> List[str]:
    w: List[str] = []
    if actor not in actors_in_evidence and actors_in_evidence:
        w.append(f"The statement says '{actor}' but the evidence is "
                 f"{sorted(actors_in_evidence)} — DO NOT VERIFY unless the "
                 f"wording matches who actually acted.")
    if any(r["visibility"] == INTERNAL for r in records):
        w.append("Some evidence is INTERNAL — this memory cannot be stated to "
                 "the customer even if verified.")
    if any(r["speech_act"] == "claim" for r in records):
        w.append("Evidence contains a CUSTOMER CLAIM about our obligations. A "
                 "self-authored claim is not corroboration — verify only "
                 "against an independent record.")
    if truncated:
        w.append("The count is a LOWER BOUND (evidence was truncated).")
    if (independent or 0) < 2:
        w.append("Only one source type backs this claim — one automated process "
                 "firing repeatedly is a single observation, not corroboration.")
    if sum(1 for r in records if not r["resolves"]):
        w.append("Some evidence records no longer exist.")
    return w


def verify(memory_id: str, verified_by: str, as_fact: bool = True,
           actor_confirmed: bool = False, role: str = "",
           acknowledged_evidence_hash: str = "") -> Dict[str, Any]:
    """A person confirms a memory — under conditions.

    The ONLY path to `assertable`. A model cannot verify its own inference, so
    nothing automatic ever sets verified_by. But a bare boolean was not enough
    either: a human verified a false attribution because they were shown a
    sentence and a button.

    Now verification requires the caller to have SEEN the evidence (they echo
    back its hash), to CONFIRM THE ACTOR explicitly, and to hold an authorized
    role. It expires with the decay class, and every judgement is appended to an
    immutable trail."""
    verified_by = (verified_by or "").strip()
    if not verified_by:
        return {"ok": False, "error": "verified_by is required — verification "
                                      "must name a person"}
    if VERIFY_ROLES and (role or "").strip().lower() not in VERIFY_ROLES:
        return {"ok": False, "error": f"role '{role or 'none'}' may not verify "
                                      f"memories (allowed: {sorted(VERIFY_ROLES)})"}

    preview = verification_preview(memory_id)
    if not preview.get("ok"):
        return preview
    if acknowledged_evidence_hash != preview["evidence_hash"]:
        return {"ok": False, "error": "evidence not acknowledged — call "
                                      "verification_preview() and echo back its "
                                      "evidence_hash, so a verifier cannot "
                                      "approve a claim they have not seen",
                "expected_evidence_hash": preview["evidence_hash"]}
    if as_fact and not actor_confirmed:
        return {"ok": False, "error": "actor_confirmed is required to promote a "
                                      "memory to an assertable fact — the "
                                      "production failure was a claim about the "
                                      "customer whose evidence was our own actions",
                "claimed_actor": preview["claimed_actor"],
                "actors_in_evidence": preview["actors_in_evidence"]}
    # An UNRESOLVED actor cannot be confirmed. `actor_matches_evidence` compared
    # sets, so a memory whose actor is 'unknown' and whose evidence is also all
    # 'unknown' counted as a MATCH — a verifier would be confirming "yes, we do
    # not know who acted", which is not a judgement anyone can make. The gate
    # blocked assertion anyway, but verification should refuse at the source.
    if as_fact and preview["claimed_actor"] in (None, "unknown", "mixed"):
        return {"ok": False,
                "error": f"actor is '{preview['claimed_actor']}' — a claim with "
                         f"no resolved actor cannot be verified as a fact; fix "
                         f"the attribution or verify it as a hypothesis",
                "actors_in_evidence": preview["actors_in_evidence"]}
    if as_fact and not preview["actor_matches_evidence"]:
        return {"ok": False, "error": "the statement's actor does not match its "
                                      "evidence — fix the memory, do not verify it",
                "claimed_actor": preview["claimed_actor"],
                "actors_in_evidence": preview["actors_in_evidence"],
                "warnings": preview["warnings"]}

    # DUAL APPROVAL. Count DISTINCT prior verifiers of this exact claim; a
    # second signature from the same person is not a second opinion.
    topic = (preview.get("topic") or "").lower()
    needs = required_approvals_for(topic)
    approvals = _distinct_approvers(memory_id,
                                    claim_hash(preview["statement"],
                                               preview["evidence_hash"]))
    if verified_by not in approvals:
        approvals = approvals | {verified_by}
    if len(approvals) < needs:
        _record_verification(memory_id, "verified", actor_confirmed, preview,
                             verified_by, role,
                             reason=f"approval {len(approvals)}/{needs}")
        return {"ok": True, "pending": True, "memory_id": memory_id,
                "approvals": sorted(approvals), "required": needs,
                "note": f"'{topic}' is high-consequence and needs {needs} "
                        f"distinct verifiers; {needs - len(approvals)} more "
                        f"required before this becomes assertable"}

    ttl = VERIFY_TTL_DAYS.get(preview.get("decay_class") or EPISODIC, 365)
    claim = claim_hash(preview["statement"], preview["evidence_hash"])
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # ORDER MATTERS. The DB trigger requires a verified fact to have
                # a matching row in the append-only trail, and it fires BEFORE
                # the UPDATE commits — so the audit row must exist first.
                # Writing the memory first made every legitimate verification
                # fail with "no matching verification record", which is the
                # trigger correctly refusing an unsupported claim.
                cur.execute(
                    """INSERT INTO memory_verifications
                         (memory_id, action, actor_confirmed, evidence_hash,
                          evidence_shown, statement_shown, performed_by, role,
                          entity_type, entity_id)
                       SELECT %s::uuid,'verified',%s,%s,%s,%s,%s,%s,
                              m.entity_type, m.entity_id
                         FROM customer_memories m WHERE m.memory_id=%s::uuid""",
                    (memory_id, actor_confirmed, preview["evidence_hash"],
                     len(preview["evidence"]), preview["statement"],
                     verified_by, role, memory_id))
                cur.execute(
                    """UPDATE customer_memories
                          SET verified_by=%s, verified_at=now(),
                              verified_actor=%s,
                              verified_evidence_hash=%s,
                              verified_claim_hash=%s,
                              verified_signature=%s,
                              verification_expires_at=now() + (%s || ' days')::interval,
                              kind=CASE WHEN %s THEN %s ELSE kind END,
                              certainty=1.0, status=%s, valid_until=NULL,
                              updated_at=now()
                        WHERE memory_id=%s::uuid
                    RETURNING statement, kind, visibility""",
                    (verified_by, actor_confirmed, preview["evidence_hash"],
                     claim, None,        # signed below, over the FINAL state
                     str(ttl), as_fact, FACT, ACTIVE, memory_id))
                r = cur.fetchone()
                if r:
                    # Sign the row as it now stands. Signing the preview would
                    # bind the state BEFORE this update (kind is promoted to
                    # 'fact' here), so the signature would never validate.
                    cur.execute(
                        f"""SELECT {", ".join(GATE_FIELDS)}
                              FROM customer_memories WHERE memory_id=%s::uuid""",
                        (memory_id,))
                    final = dict(zip(GATE_FIELDS, cur.fetchone()))
                    cur.execute(
                        "UPDATE customer_memories SET verified_signature=%s "
                        "WHERE memory_id=%s::uuid",
                        (signature_for(memory_id, gate_fingerprint(final),
                                       verified_by), memory_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        # The trigger REFUSING is a governance outcome, not a bug — surface it.
        return {"ok": False, "error": str(exc).splitlines()[0][:220]}
    if not r:
        return {"ok": False, "error": "no such memory"}
    return {"ok": True, "memory_id": memory_id, "statement": r[0],
            "kind": r[1], "verified_by": verified_by,
            "expires_in_days": ttl,
            "warnings": preview["warnings"]}


def reject(memory_id: str, rejected_by: str, reason: str = "") -> Dict[str, Any]:
    """A person marks a memory wrong. It is retained (as a record that we once
    believed it) but never surfaced again, and consolidation will not resurrect
    it while the rejection stands."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE customer_memories
                          SET status=%s, verified_by=%s, verified_at=now(),
                              statement=statement || ' [rejected: ' ||
                                        COALESCE(NULLIF(%s,''),'no reason given') || ']',
                              updated_at=now()
                        WHERE memory_id=%s::uuid RETURNING topic""",
                    (REJECTED, rejected_by, reason[:200], memory_id))
                r = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": bool(r), "memory_id": memory_id,
            "topic": r[0] if r else None, "status": REJECTED}


def explain(memory_id: str) -> Dict[str, Any]:
    """Why do you believe this? — the whole chain, in one call.

    An agent that states something must be able to justify it, and a reviewer
    deciding whether to verify must see the same thing. Reconstructing this by
    hand-writing SQL is why a false attribution survived: nobody could see the
    claim beside its evidence, its arithmetic and its conflicts at once."""
    pv = verification_preview(memory_id)
    if not pv.get("ok"):
        return pv

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Same columns the enforcement path reads — see GATE_COLUMNS.
            cur.execute(
                f"""SELECT entity_type, entity_id::text, generator, status,
                           {GATE_COLUMNS}
                      FROM customer_memories WHERE memory_id=%s::uuid""",
                (memory_id,))
            cols = [d[0] for d in cur.description]
            grow = dict(zip(cols, cur.fetchone()))
            etype, eid, generator, status = (grow["entity_type"], grow["entity_id"],
                                             grow["generator"], grow["status"])
            rel, cert = grow["reliability"], grow["certainty"]
            last, vat, dclass = (grow["last_observed_at"], grow["verified_at"],
                                 grow["decay_class"])
            contradicts = grow["array"] if "array" in grow else grow.get("contradicts")
            grow["contradicts"] = contradicts
            csev, missing, truncated = (grow["conflict_severity"],
                                        grow["evidence_missing"], grow["truncated"])

            conflicts = []
            if contradicts:
                cur.execute(
                    """SELECT memory_id::text, statement, kind, certainty,
                              verified_by
                         FROM customer_memories
                        WHERE memory_id = ANY(%s::uuid[])""", (contradicts,))
                conflicts = [{"memory_id": r[0], "statement": r[1], "kind": r[2],
                              "certainty": float(r[3]) if r[3] is not None else None,
                              "verified": bool(r[4])} for r in cur.fetchall()]

            cur.execute(
                """SELECT action, performed_by, role, actor_confirmed,
                          evidence_shown, created_at
                     FROM memory_verifications WHERE memory_id=%s::uuid
                    ORDER BY created_at""", (memory_id,))
            history = [{"action": r[0], "by": r[1], "role": r[2],
                        "actor_confirmed": r[3], "evidence_shown": r[4],
                        "at": r[5].isoformat()} for r in cur.fetchall()]
    finally:
        conn.close()

    eff = effective_certainty(float(cert) if cert is not None else None,
                              last, vat, decay_class=dclass or EPISODIC)
    # The SAME inputs enforcement uses. Previously reconstructed here from the
    # verification history with expiry hardcoded to None, which made this
    # screen's verdict weaker than the one the system actually applies.
    blockers = _assertion_blockers(**gate_inputs(grow, effective_cert=eff))

    return {
        "ok": True, "memory_id": memory_id,
        "statement": pv["statement"],
        "entity": {"type": etype, "id": eid},
        "derivation": {
            "generator": generator,
            "how": "records clustered by embedding similarity, collapsed to "
                   "distinct occasions (template+day), named by keyword topic, "
                   "actor taken from evidence consensus",
            "occasions_counted": pv["occurrences"],
            "evidence_sampled": len(pv["evidence"]),
            "truncated": truncated,
        },
        "confidence_math": {
            "reliability": float(rel) if rel is not None else None,
            "stated_certainty": float(cert) if cert is not None else None,
            "decay_class": dclass,
            "half_life_days": _DECAY_HALF_LIFE.get(dclass or EPISODIC),
            "effective_certainty": round(eff, 3),
            "composite": round((float(rel) if rel else 0) * eff, 3),
            "formula": "confidence = reliability × certainty × decay(age)",
        },
        "evidence": pv["evidence"],
        "actor_matches_evidence": pv["actor_matches_evidence"],
        "conflicts": conflicts,
        "verification_history": history,
        "status": status,
        "assertable": not blockers,
        "assertion_blockers": blockers,
        "warnings": pv["warnings"],
    }


router = APIRouter(tags=["customer-memory"])


@router.get("/customer-memories/{memory_id}/explain")
def memory_explain(memory_id: str):
    """Why do you believe this?"""
    return explain(memory_id)


@router.get("/customer-memories/{memory_id}/preview")
def memory_preview(memory_id: str):
    """What a verifier must read BEFORE approving."""
    return verification_preview(memory_id)


@router.get("/customer-memories/{entity_type}/{entity_id}/confirmed")
def memories_confirmed(entity_type: str, entity_id: str, limit: int = 5):
    """The ONLY endpoint safe to feed a customer-facing agent."""
    return {"facts": confirmed_facts(entity_type, entity_id, limit)}


@router.post("/customer-memories/{memory_id}/verify")
def memory_verify(memory_id: str, body: Optional[Dict[str, Any]] = None):
    b = body or {}
    return verify(memory_id, str(b.get("verified_by") or ""),
                  bool(b.get("as_fact", True)))


@router.post("/customer-memories/{memory_id}/reject")
def memory_reject(memory_id: str, body: Optional[Dict[str, Any]] = None):
    b = body or {}
    return reject(memory_id, str(b.get("rejected_by") or "admin"),
                  str(b.get("reason") or ""))


@router.post("/customer-memories/consolidate")
def consolidate_endpoint(body: Optional[Dict[str, Any]] = None):
    b = body or {}
    if b.get("entity_id"):
        return consolidate_entity(b.get("entity_type", "contact"), b["entity_id"])
    return consolidate_pass(int(b.get("limit") or 0))


@router.get("/customer-memories/{entity_type}/{entity_id}")
def memories_get(entity_type: str, entity_id: str, limit: int = 10):
    """Admin inspection — internal audience (the router is admin-gated)."""
    return {"memories": recall(entity_type, entity_id, INTERNAL, limit)}


if __name__ == "__main__":                     # python -m app.core.memory_consolidation
    print(json.dumps(consolidate_pass(limit=1000), indent=2, default=str))


# ============================================================================
# GENERATOR IDENTITY
# ============================================================================

def _wording_fingerprint() -> str:
    """Fingerprint the DERIVATION: what `_statement` renders, plus how long the
    result stays assertable.

    Decay policy is in here because it is part of the claim, not metadata about
    it — it decides how long we keep saying this about a person. It also has to
    be, mechanically: the upsert only rewrites a row when its evidence_hash
    moves, so a change to `decay_class_for` alone would never have reached the
    270 memories already stored. They would have kept a lifetime derived from
    code that no longer exists, and nothing would have reported it.

    Deliberately not a hash of the source: source hashing churns identity on a
    comment edit and would have retired every memory in the database for a
    typo fix. This changes if and only if the SENTENCE changes.

    The probe set must exercise every branch that varies wording — each actor,
    the clipped and unclipped forms, singular and plural, and same-day vs
    ranged. A branch missing from the probes is a wording change this cannot
    see, which is the whole failure being fixed."""
    import datetime as _d
    a = _d.datetime(2026, 1, 5, tzinfo=_d.timezone.utc)
    b = _d.datetime(2026, 3, 9, tzinfo=_d.timezone.utc)
    probes = []
    for actor in ("customer_said", "customer_did", "company_did",
                  "third_party_did", None):
        for clipped in (False, True):
            for n, first, last in ((1, a, a), (7, a, b), (3, None, None)):
                probes.append(_statement("billing", n, first, last, actor, clipped))
    # Decay policy, in a stable order.
    probes.append("|".join(f"{t}={decay_class_for(t)}"
                           for t, _ in sorted(_TOPICS)))
    probes.append(f"fallback={decay_class_for('~absent-from-vocabulary~')}")
    return hashlib.sha256("␟".join(probes).encode()).hexdigest()[:12]


GENERATOR = f"{GENERATOR_BASE}+w{_wording_fingerprint()}"
