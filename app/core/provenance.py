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
  confidence   0..1 — meaningful for `ai`/`computed`; CERTAIN sources default 1.0
  observed_at  when the fact was observed/asserted (not when the row was written)
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


@dataclass(frozen=True)
class Provenance:
    source_type: str = HUMAN
    source_id: Optional[str] = None
    confidence: Optional[float] = None
    observed_at: Optional[str] = None

    def normalized(self) -> "Provenance":
        st = (self.source_type or HUMAN).strip().lower()
        if st not in SOURCE_TYPES:
            st = UNKNOWN
        conf = self.confidence
        if conf is not None:
            try:
                conf = max(0.0, min(1.0, float(conf)))
            except (TypeError, ValueError):
                conf = None
        if conf is None and st in CERTAIN:
            conf = 1.0
        obs = self.observed_at or (_dt.datetime.utcnow().isoformat() + "Z")
        return Provenance(st, self.source_id or None, conf, obs)

    def as_columns(self) -> Dict[str, Any]:
        p = self.normalized()
        return {"source_type": p.source_type, "source_id": p.source_id,
                "confidence": p.confidence, "observed_at": p.observed_at}


def from_input(body: Optional[Dict[str, Any]]) -> Provenance:
    """Build a Provenance from a request/dict; missing → a HUMAN default."""
    b = body or {}
    return Provenance(
        source_type=str(b.get("source_type") or HUMAN),
        source_id=b.get("source_id") or None,
        confidence=b.get("confidence"),
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
