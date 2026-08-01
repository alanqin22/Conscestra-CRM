"""Provenance envelope — where did this value come from, and can we trust it?

P1. A stored value ("customer_tier = Enterprise") is indistinguishable from a
hand-typed one, a CSV import, or an AI inference — yet an agent needs to know
which before it leans on it. This is the ONE canonical envelope every
provenance-carrying write uses, so `source_type / source_id / confidence /
observed_at` mean the same thing everywhere (custom_field_values first;
interaction memories, imports, enrichment later).

  source_type  who/what asserted it (see vocabulary below)
  source_id    the specific actor: a user id, an agent slug, an import batch ref,
               an external system+record id
  reliability  0..1 — how much this SOURCE reflects reality, at all
  certainty    0..1 — how sure the source is about THIS particular assertion
  confidence   0..1 — the composite (reliability × certainty), derived
  observed_at  when the fact was observed/asserted (not when the row was written)

WHY RELIABILITY AND CERTAINTY ARE SEPARATE (2026-07-30)
-------------------------------------------------------
A single `confidence` conflated two independent things, and the codebase already
used the same word for both: enrichment meant SOURCE RELIABILITY (Apollo 0.90),
while `scoring.predict_for` and the memory distiller meant PREDICTION PROBABILITY
(0.7). One column name, two incompatible scales — the same class of drift that
made `win_rate` mean three things, arriving in a new place.

They are genuinely orthogonal:

    Apollo says "500 employees, 92% match"   reliability .90  certainty .92
    A web scrape guesses the industry        reliability .55  certainty .40
    The stub HASHES a domain into a band     reliability .15  certainty 1.00
    A person typed it                        reliability 1.0  certainty 1.00

The stub is the case that proves the split: it is perfectly deterministic — it
will say the same thing every time, so its CERTAINTY is 1.0 — while being
completely disconnected from reality, so its RELIABILITY is near zero. Collapsed
into one number, "deterministic" and "true" become indistinguishable.

`confidence` remains as the composite so every existing reader keeps working;
it is derived, never stored independently.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Dict, Optional

# ── source_type vocabulary ───────────────────────────────────────────────────
HUMAN = "human"        # a person typed/edited it (the UI path)
AI = "ai"              # an agent inferred it — confidence + evidence advised
IMPORT = "import"      # bulk import (CSV / QuickBooks / ...) — source_id = batch
EXTERNAL = "external"  # synced from an external system — source_id = system+record
COMPUTED = "computed"  # derived by a rule/aggregate
UNKNOWN = "unknown"    # provenance not recorded (pre-envelope rows)

SOURCE_TYPES = {HUMAN, AI, IMPORT, EXTERNAL, COMPUTED, UNKNOWN}
# Authoritative-by-definition sources — confidence defaults to 1.0.
CERTAIN = {HUMAN, IMPORT, EXTERNAL}


# Default reliability per source kind — how much a value from this KIND of
# source reflects reality before anything specific is known about the assertion.
DEFAULT_RELIABILITY = {
    HUMAN: 1.0,       # a person looked and typed it
    IMPORT: 0.95,     # the customer's own book of business
    EXTERNAL: 0.80,   # a third-party system; per-provider overrides refine this
    COMPUTED: 0.90,   # our own deterministic rule over data we hold
    AI: 0.70,         # a model's inference
    UNKNOWN: 0.30,    # unrecorded origin — assume little
}


def _clamp(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Provenance:
    source_type: str = HUMAN
    source_id: Optional[str] = None
    reliability: Optional[float] = None   # trust in the SOURCE
    certainty: Optional[float] = None     # the source's sureness about THIS value
    observed_at: Optional[str] = None
    # Legacy single-number input. Accepted so existing callers keep working; it
    # is interpreted as CERTAINTY (what a caller passing one number almost
    # always means: "how sure am I of this value"), with reliability taken from
    # the source kind.
    confidence_in: Optional[float] = None

    @property
    def confidence(self) -> Optional[float]:
        """The composite — what a single-number consumer should read.

        Multiplicative because the factors are independent: a certain assertion
        from an unreliable source is not trustworthy, and neither is a hesitant
        one from a good source."""
        r, c = self.reliability, self.certainty
        if r is None and c is None:
            return None
        return round((1.0 if r is None else r) * (1.0 if c is None else c), 4)

    def normalized(self) -> "Provenance":
        st = (self.source_type or HUMAN).strip().lower()
        if st not in SOURCE_TYPES:
            st = UNKNOWN
        rel = _clamp(self.reliability)
        cer = _clamp(self.certainty)
        if cer is None:
            cer = _clamp(self.confidence_in)
        if rel is None:
            rel = DEFAULT_RELIABILITY.get(st, 0.5)
        if cer is None:
            cer = 1.0 if st in CERTAIN else 0.7
        obs = self.observed_at or (_dt.datetime.utcnow().isoformat() + "Z")
        return Provenance(st, self.source_id or None, rel, cer, obs)

    def as_columns(self) -> Dict[str, Any]:
        p = self.normalized()
        return {"source_type": p.source_type, "source_id": p.source_id,
                "reliability": p.reliability, "certainty": p.certainty,
                "confidence": p.confidence, "observed_at": p.observed_at}

    def describe(self) -> str:
        """Plain phrase naming BOTH factors, so a reader can tell 'a good source
        that is unsure' from 'a bad source that is certain' — which is precisely
        what a single number hides."""
        p = self.normalized()
        base = describe(p.source_type, None)
        return (f"{base} (source reliability {round(p.reliability * 100)}%, "
                f"stated certainty {round(p.certainty * 100)}%)")


def from_input(body: Optional[Dict[str, Any]]) -> Provenance:
    """Build a Provenance from a request/dict; missing → a HUMAN default."""
    b = body or {}
    return Provenance(
        source_type=str(b.get("source_type") or HUMAN),
        source_id=b.get("source_id") or None,
        reliability=b.get("reliability"),
        certainty=b.get("certainty"),
        # A caller sending only `confidence` means "how sure am I of this value"
        # — that is CERTAINTY. Reliability then comes from the source kind.
        confidence_in=b.get("confidence"),
        observed_at=b.get("observed_at"),
    ).normalized()


def describe(source_type: Optional[str], confidence: Optional[float]) -> str:
    """Short human phrase, e.g. 'AI-inferred (78% confidence)'."""
    st = (source_type or UNKNOWN).lower()
    label = {HUMAN: "entered by a person", AI: "AI-inferred", IMPORT: "imported",
             EXTERNAL: "synced from an external system", COMPUTED: "system-computed",
             UNKNOWN: "unknown provenance"}.get(st, st)
    if st in (AI, COMPUTED) and confidence is not None:
        return f"{label} ({round(confidence * 100)}% confidence)"
    return label
