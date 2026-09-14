"""The canonical financial proposition: what a named executive approved.

WHAT THIS IS FOR. An approval that records only "the CFO approved this" proves
a string. It does not prove which economic consequence was authorised, and it
does not prevent the system from executing a different one. The review on
2026-09-13 measured that gap: `proposition_hash` existed as a column with no
producer and no consumer, the executive's "What you are approving" block was
rendered from caller-supplied `params`, and a direct SQL writer could alter the
amount of an executed approval without any control refusing.

THE INVARIANT THIS SERVES. The authenticated, authorised executive approves the
exact economic proposition that the system executes. Two separate controls
carry it, and they are not interchangeable:

    integrity        this module: WHAT was approved, hashed
    authentication   governance.decision_token: WHO approved it

THE PARTITION IS THE DESIGN. Every field is either DERIVED from authoritative
state or DECLARED by the proposer, and which one it is travels inside the hash.
The alternative -- deriving everything -- was rejected because it is not true of
this system: sp_accounting accepts `p_adjustment_amount`, which replaces the
derived subtotal outright, and that is a real capability rather than a defect.
What the partition prevents is an override being presented as a derivation.

WHY DERIVED VALUES ARE NOT FROZEN. Approval stores the proposition; execution
REBUILDS it from current state and compares hashes. Freezing the derived half
would let an order line change after approval and still invoice the old amount,
which is the failure this design exists to catch -- and it is a failure that
needs no attacker.

CANONICALISATION FOLLOWS memory_consolidation.gate_fingerprint, which solved
the same problem for a non-financial dual approval and is in production. Its
specific lessons are kept: None is preserved rather than coerced to zero, bool
is tested before int because bool is a subclass of int, and Decimal and float
are rendered through one fixed-scale path so a value read back from psycopg2
fingerprints identically to the value that was written.

WHAT THIS MODULE DOES NOT DO. It does not decide invoiceability -- A3 does, and
this reads the decision. It does not determine tax -- `tax_rates` and
`product_tax_treatment` do. It never writes either. A proposition that cannot
establish a required fact is BLOCKED and says which fact is missing; it is
never completed by inference.
"""
from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Bumped when the field set or the canonical encoding changes. It travels
# inside the hash, so a proposition built under one version can never compare
# equal to one built under another -- an upgrade invalidates outstanding
# approvals rather than silently re-interpreting them.
PROPOSITION_VERSION = 1

# Domain separation. Without it a proposition digest could be presented where
# some other sha256 in this system is expected.
_DOMAIN = "conscestra.financial_proposition.v1"

# Money is carried to four decimal places because tax_rates.rate is
# numeric(5,4); totals are quantised to two. Both are FIXED so that 1, 1.0 and
# 1.00 cannot produce three different canonical forms.
_MONEY_SCALE = 2
_RATE_SCALE = 4

# THE CLOSED FIELD SET. A field outside this list cannot be approved, and a
# field inside it that execution does not consume is a decoration -- the
# standard memory_consolidation states as "a cryptographic control whose scope
# is narrower than the policy control it protects is not a control".
DECLARED_FIELDS: Tuple[str, ...] = (
    "account_id", "order_ids", "invoice_type", "due_date", "contact_id",
    "discount_amount", "shipping_amount", "adjustment_amount",
)
DERIVED_FIELDS: Tuple[str, ...] = (
    "currency", "line_composition_digest", "subtotal", "subtotal_is_override",
    "tax_jurisdiction", "tax_treatment_basis", "tax_rate", "tax_amount",
    "total_amount", "invoiceability_basis", "fulfilment_evidence_ref",
    "effective_financial_date", "policy_version",
)
PROPOSITION_FIELDS: Tuple[str, ...] = (
    ("approval_uuid", "action_type") + DECLARED_FIELDS + DERIVED_FIELDS)

# WHICH FIELDS ARE MONEY, named rather than inferred from the incoming type.
# Inferring was wrong and a test caught it: `1` arrived as an int and
# canonicalised as 1, while the same amount as Decimal("1.00") canonicalised as
# "1.00", so two economically identical propositions produced two hashes. The
# type a value happens to arrive as is a property of how it was fetched, not of
# what it is -- which is the same lesson gate_fingerprint records for
# Decimal/float parity. policy_version stays a genuine integer and is not here.
MONEY_FIELDS: Tuple[str, ...] = (
    "discount_amount", "shipping_amount", "adjustment_amount", "subtotal",
    "tax_amount", "total_amount",
)
RATE_FIELDS: Tuple[str, ...] = ("tax_rate",)


class PropositionBlocked(Exception):
    """A required financial fact is not established. Carries every reason
    rather than the first, so an operator sees the whole gap in one pass
    instead of discovering it one refusal at a time."""

    def __init__(self, reasons: List[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# ── canonical form ──────────────────────────────────────────────────────────

def _money(v: Any, scale: int = _MONEY_SCALE) -> Optional[str]:
    """Fixed-scale decimal string, or None preserved as None.

    A string rather than a float: 0.1 + 0.2 is not 0.3 in binary floating
    point, and a hash over a float would depend on how the value happened to be
    computed rather than on what it is.
    """
    if v is None:
        return None
    q = Decimal(str(v)).quantize(Decimal(1).scaleb(-scale))
    return f"{q:f}"


def _canon_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        # Before the int branch: bool is a subclass of int, so True would
        # otherwise canonicalise as 1 and become indistinguishable from it.
        return v
    if isinstance(v, int):
        return int(v)
    if isinstance(v, (float, Decimal)):
        return _money(v)
    if isinstance(v, (list, tuple)):
        return [_canon_value(x) for x in v]
    return str(v)


def canonical(proposition: Dict[str, Any]) -> str:
    """The exact bytes that are hashed.

    Key order is fixed by sort_keys and the separators carry no whitespace, so
    two propositions that differ economically cannot share a canonical form and
    two that are economically identical cannot differ by formatting.
    """
    missing = [f for f in PROPOSITION_FIELDS if f not in proposition]
    if missing:
        raise ValueError(f"proposition is missing declared fields: {missing}")
    extra = [k for k in proposition if k not in PROPOSITION_FIELDS]
    if extra:
        # Refused rather than dropped: a field the proposer added is either
        # economically material -- in which case the field set is wrong -- or it
        # is noise that must not reach an approval record.
        raise ValueError(f"proposition carries undeclared fields: {extra}")
    canon = {}
    for f in PROPOSITION_FIELDS:
        v = proposition[f]
        if f in MONEY_FIELDS:
            canon[f] = _money(v)
        elif f in RATE_FIELDS:
            canon[f] = _money(v, _RATE_SCALE)
        else:
            canon[f] = _canon_value(v)
    canon["_v"] = PROPOSITION_VERSION
    return json.dumps(canon, sort_keys=True, separators=(",", ":"))


def proposition_hash(proposition: Dict[str, Any]) -> str:
    """SHA-256 over the domain-separated canonical form."""
    blob = _DOMAIN + "|" + canonical(proposition)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def hashes_match(a: Optional[str], b: Optional[str]) -> bool:
    """Constant-time, and False when either side is absent.

    A missing hash is never treated as agreement. The equivalent mistake is on
    record in memory_consolidation: a NULL signature was stored, the approver
    was told it worked, and the artifact could never be used.
    """
    import hmac as _hmac
    if not a or not b:
        return False
    return _hmac.compare_digest(str(a), str(b))


# ── construction from authoritative state ───────────────────────────────────

def _line_composition_digest(rows: List[Tuple[Any, Any, Any]]) -> str:
    """Digest of (order_id, order_item_id, line_total) over every line.

    Ordered, so the digest does not depend on how the rows were fetched. The
    total alone is not enough: two different line sets can sum to the same
    number, and an invoice whose total matches while its lines have changed is
    exactly the substitution this is here to detect.
    """
    canon = [[str(o), str(i), _money(t)] for o, i, t in rows]
    canon.sort()
    return hashlib.sha256(
        json.dumps(canon, separators=(",", ":")).encode("utf-8")).hexdigest()


def build(cur, approval_uuid: str, action_type: str,
          declared: Dict[str, Any]) -> Dict[str, Any]:
    """Construct the proposition from authoritative state plus declared input.

    `declared` may carry ONLY the fields in DECLARED_FIELDS. Anything else is
    refused rather than ignored, because a caller that can add a field to the
    proposition is a caller that authors the record approval is bound to.

    Raises PropositionBlocked when a required fact is not established. Under H1
    that is the correct outcome and not an error to be worked around: missing
    tax information does not become zero tax, and an unrecorded invoiceability
    decision does not become permission.
    """
    unknown = [k for k in declared if k not in DECLARED_FIELDS]
    if unknown:
        raise ValueError(
            f"declared input carries fields the proposer may not author: "
            f"{unknown}. Derived fields come from authoritative state.")

    order_ids = [str(o) for o in (declared.get("order_ids") or [])]
    account_id = declared.get("account_id")
    reasons: List[str] = []
    if not order_ids:
        raise PropositionBlocked(["no orders were declared"])
    if not account_id:
        raise PropositionBlocked(["no account was declared"])

    # Orders must exist, be live, and belong to the declared account. This
    # mirrors the precondition sp_accounting already enforces (-13) rather than
    # inventing a second rule.
    cur.execute("""SELECT order_id::text, currency, billing_address_id,
                          shipping_address_id
                     FROM orders
                    WHERE order_id = ANY(%s::uuid[]) AND deleted_at IS NULL
                      AND account_id = %s::uuid""", (order_ids, str(account_id)))
    orders = {r[0]: r for r in cur.fetchall()}
    absent = [o for o in order_ids if o not in orders]
    if absent:
        raise PropositionBlocked(
            [f"order {o} is absent, deleted, or belongs to another account"
             for o in absent])

    # (1) One explicit transaction currency across every order. A null currency
    # is unknown, never the local one.
    currencies = {r[1] for r in orders.values()}
    if any(c is None or not str(c).strip() for c in currencies):
        reasons.append("one or more orders carry no explicit transaction currency")
        currency = None
    elif len(currencies) > 1:
        reasons.append(f"orders span {len(currencies)} currencies; a multi-currency "
                       f"proposition has no single transaction amount")
        currency = None
    else:
        currency = currencies.pop()

    # (2) Line composition, and every line denominated in that currency.
    cur.execute("""SELECT oi.order_id::text, oi.order_item_id::text, oi.line_total,
                          p.currency_code
                     FROM order_items oi
                     JOIN products p ON p.product_id = oi.product_id
                    WHERE oi.order_id = ANY(%s::uuid[])""", (order_ids,))
    lines = cur.fetchall()
    if not lines:
        # A vacuous pass here would let a zero-line order satisfy currency
        # reconciliation and tax treatment by having nothing to check, and
        # produce a zero subtotal that an adjustment override could then
        # replace with any amount.
        reasons.append("the declared orders carry no order lines; there is no "
                       "line composition to invoice")
    if currency is not None:
        mismatched = [l for l in lines if (l[3] or "") != currency]
        if mismatched:
            reasons.append(f"{len(mismatched)} line(s) are not denominated in "
                           f"{currency}")
    digest = _line_composition_digest([(l[0], l[1], l[2]) for l in lines]) \
        if lines else None
    derived_subtotal = sum((Decimal(str(l[2] or 0)) for l in lines), Decimal(0))

    # (3) A3 is authoritative for invoiceability. This READS the decision and
    # never computes one; the absence of a row means no decision was recorded,
    # which is not permission.
    cur.execute("""SELECT i.eligibility, i.evidence_ref::text, t.transition_seq
                     FROM order_invoiceability i
                     LEFT JOIN LATERAL (
                          SELECT transition_seq FROM order_invoiceability_transitions
                           WHERE order_id = i.order_id
                           ORDER BY transition_seq DESC LIMIT 1) t ON true
                    WHERE i.order_id = ANY(%s::uuid[])""", (order_ids,))
    decisions = {}
    for elig, ref, seq in cur.fetchall():
        decisions.setdefault(elig, []).append(seq)
    cur.execute("""SELECT count(*) FROM order_invoiceability
                    WHERE order_id = ANY(%s::uuid[])
                      AND eligibility = 'invoiceable'""", (order_ids,))
    invoiceable_count = cur.fetchone()[0]
    if invoiceable_count != len(order_ids):
        reasons.append(
            f"{len(order_ids) - invoiceable_count} of {len(order_ids)} order(s) "
            f"are not recorded invoiceable under A3; an unrecorded decision is "
            f"not permission")

    # (4) Fulfilment provenance: only `established` supports invoicing. No
    # surviving evidence is not proof that fulfilment did not occur -- it is
    # proof that it is not established, and unestablished does not invoice.
    cur.execute("""SELECT count(*) FROM unnest(%s::uuid[]) AS o(order_id)
                    WHERE (SELECT a.state FROM order_financial_assertions a
                            WHERE a.order_id = o.order_id
                              AND a.assertion_type = 'fulfilment_provenance'
                            ORDER BY a.assertion_seq DESC LIMIT 1)
                          IS DISTINCT FROM 'established'""", (order_ids,))
    unestablished = cur.fetchone()[0]
    if unestablished:
        reasons.append(f"{unestablished} order(s) have no established fulfilment "
                       f"provenance")
    cur.execute("""SELECT a.evidence_ref::text FROM order_financial_assertions a
                    WHERE a.order_id = ANY(%s::uuid[])
                      AND a.assertion_type = 'fulfilment_provenance'
                    ORDER BY a.assertion_seq DESC LIMIT 1""", (order_ids,))
    row = cur.fetchone()
    evidence_ref = row[0] if row else None

    # (5) Tax jurisdiction, treatment and rate, each from its own authority.
    cur.execute("""SELECT DISTINCT ad.country, ad.province
                     FROM orders o
                     LEFT JOIN addresses ad ON ad.address_id =
                          coalesce(o.billing_address_id, o.shipping_address_id)
                    WHERE o.order_id = ANY(%s::uuid[])""", (order_ids,))
    juris = [r for r in cur.fetchall() if r[0] and str(r[0]).strip()]
    jurisdiction = tax_rate = tax_treatment = None
    if len(juris) != 1:
        reasons.append("the tax jurisdiction is not resolvable to exactly one "
                       "country; missing tax information does not become zero tax")
    else:
        country, province = juris[0]
        jurisdiction = f"{country}/{province}" if province else str(country)
        cur.execute("""SELECT count(*) FROM order_items oi
                        WHERE oi.order_id = ANY(%s::uuid[])
                          AND NOT EXISTS (
                              SELECT 1 FROM product_tax_treatment t
                               WHERE t.product_id = oi.product_id
                                 AND t.country = %s
                                 AND t.effective_from <= current_date
                                 AND (t.effective_to IS NULL
                                      OR t.effective_to >= current_date))""",
                    (order_ids, country))
        untreated = cur.fetchone()[0]
        if untreated or not lines:
            reasons.append(f"{untreated} line(s) have no established product tax "
                           f"treatment for {country}")
        else:
            cur.execute("""SELECT string_agg(DISTINCT t.treatment, ',' ORDER BY
                                             t.treatment)
                             FROM order_items oi
                             JOIN product_tax_treatment t
                               ON t.product_id = oi.product_id AND t.country = %s
                              AND t.effective_from <= current_date
                              AND (t.effective_to IS NULL
                                   OR t.effective_to >= current_date)
                            WHERE oi.order_id = ANY(%s::uuid[])""",
                        (country, order_ids))
            tax_treatment = cur.fetchone()[0]

        # tax_rates is the authority for the rate. A caller-supplied rate is
        # not accepted here at all: it is not in DECLARED_FIELDS.
        cur.execute("""SELECT rate FROM tax_rates
                        WHERE country = %s
                          AND (region IS NOT DISTINCT FROM %s OR region IS NULL)
                          AND valid_from <= current_date
                          AND (valid_to IS NULL OR valid_to >= current_date)
                     ORDER BY (region IS NOT NULL) DESC, valid_from DESC
                        LIMIT 1""", (country, province))
        r = cur.fetchone()
        if not r:
            reasons.append(f"no applicable tax rate is on file for {jurisdiction}")
        else:
            tax_rate = r[0]

    if reasons:
        raise PropositionBlocked(reasons)

    # Every required fact is established. Only now is the arithmetic done.
    adjustment = declared.get("adjustment_amount")
    is_override = adjustment is not None
    subtotal = Decimal(str(adjustment)) if is_override else derived_subtotal
    tax_amount = (subtotal * Decimal(str(tax_rate))).quantize(
        Decimal(1).scaleb(-_MONEY_SCALE))
    total = (subtotal + tax_amount
             + Decimal(str(declared.get("shipping_amount") or 0))
             - Decimal(str(declared.get("discount_amount") or 0)))

    cur.execute("""SELECT policy_version FROM governance_action_policies
                    WHERE action_type = %s""", (action_type,))
    r = cur.fetchone()
    cur.execute("SELECT current_date")
    effective_date = cur.fetchone()[0]

    return {
        "approval_uuid": str(approval_uuid),
        "action_type": str(action_type),
        "account_id": str(account_id),
        "order_ids": sorted(order_ids),
        "invoice_type": declared.get("invoice_type"),
        "due_date": declared.get("due_date"),
        "contact_id": declared.get("contact_id"),
        "discount_amount": _money(declared.get("discount_amount") or 0),
        "shipping_amount": _money(declared.get("shipping_amount") or 0),
        "adjustment_amount": _money(adjustment) if is_override else None,
        "currency": currency,
        "line_composition_digest": digest,
        "subtotal": _money(subtotal),
        "subtotal_is_override": is_override,
        "tax_jurisdiction": jurisdiction,
        "tax_treatment_basis": tax_treatment,
        "tax_rate": _money(tax_rate, _RATE_SCALE),
        "tax_amount": _money(tax_amount),
        "total_amount": _money(total),
        "invoiceability_basis": "order_invoiceability:invoiceable",
        "fulfilment_evidence_ref": evidence_ref,
        "effective_financial_date": effective_date.isoformat(),
        "policy_version": int(r[0]) if r and r[0] is not None else None,
    }


def rebuild_and_compare(cur, stored: Dict[str, Any],
                        stored_hash: str) -> Dict[str, Any]:
    """Rebuild the proposition from CURRENT state and compare to the approved
    hash. {ok, reason, rebuilt_hash}.

    This is the control that makes "approved equals executed" true rather than
    asserted, and it deliberately does not replay the stored derived values. An
    order line edited after approval changes the rebuilt hash, and that is a
    divergence the executive never authorised even though nobody tampered with
    the approval record.
    """
    declared = {f: stored.get(f) for f in DECLARED_FIELDS
                if stored.get(f) is not None}
    declared["order_ids"] = stored.get("order_ids") or []
    declared["account_id"] = stored.get("account_id")
    try:
        rebuilt = build(cur, stored["approval_uuid"], stored["action_type"],
                        declared)
    except PropositionBlocked as exc:
        return {"ok": False, "rebuilt_hash": None,
                "reason": f"the proposition can no longer be established: "
                          f"{'; '.join(exc.reasons)}"}
    rebuilt_hash = proposition_hash(rebuilt)
    if not hashes_match(rebuilt_hash, stored_hash):
        differing = [f for f in PROPOSITION_FIELDS
                     if _canon_value(rebuilt.get(f))
                     != _canon_value(stored.get(f))]
        return {"ok": False, "rebuilt_hash": rebuilt_hash,
                "reason": f"the proposition has diverged since approval "
                          f"({', '.join(differing) or 'encoding'}); the approved "
                          f"authorisation does not cover it"}
    return {"ok": True, "rebuilt_hash": rebuilt_hash, "reason": "unchanged"}
