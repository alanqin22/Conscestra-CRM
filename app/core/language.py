"""Language detection + reply-language directive — multilingual customer service.

Blindspot #2, and the one uniquely OURS to own: Conscestra is a Canadian,
bilingual product, and Agentforce can't claim French as a differentiator the way
a Canadian platform can. Every customer-facing responder now detects the language
the CUSTOMER wrote in and is instructed to reply in it — grounded in the SAME
approved (English) knowledge, so governance and approval stay intact and only the
surface language changes. A French shopper gets a French answer; the audit copy,
the KB, and the guardrails are unchanged.

Deterministic and free: a stopword/diacritic scorer — no new dependency, no LLM
call, idempotent, instant kill switch. French (fr-CA) is first-class; Spanish and
German are recognized too; everything else (and any weak signal) defaults to
English, the safe fallback.

Scope note: this covers the TEXT channels (email, web chat, store agent, the
agent-console co-pilot draft). Voice is deliberately left English for now — a
French reply spoken by an English TTS voice sounds broken; real voice i18n means
switching the STT recognition language and the TTS voice, a separate pass.

CONFIG (env)
  MULTILINGUAL_ENABLED  1   kill switch (0 → always English, directive is '')
"""

from __future__ import annotations

import os
import re

# ── Function-word signatures. Function words are the strongest cheap signal —
#    a sentence's grammar words betray its language even when content words are
#    shared (product names, brands). Overlaps between sets are fine; the margin
#    rule below resolves them. ──────────────────────────────────────────────
_FR = {
    "le", "la", "les", "un", "une", "des", "du", "de", "je", "tu", "il", "elle",
    "nous", "vous", "ils", "elles", "est", "es", "suis", "sommes", "êtes",
    "être", "avez", "avons", "ai", "avec", "pour", "pas", "ne", "que", "qui",
    "quoi", "où", "quand", "comment", "pourquoi", "mon", "ma", "mes", "votre",
    "vos", "notre", "nos", "bonjour", "merci", "oui", "non", "plaît",
    "commande", "facture", "livraison", "compte", "aide", "problème", "besoin",
    "voudrais", "pouvez", "puis", "cette", "ce", "cet", "sur", "dans", "mais",
    "donc", "très", "bien", "jour", "à", "aussi", "faire", "quel", "quelle",
}
_ES = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "que", "y",
    "en", "es", "por", "para", "con", "su", "mi", "hola", "gracias", "pedido",
    "factura", "cuenta", "ayuda", "necesito", "quiero", "dónde", "cuándo",
    "cómo", "porque", "está", "estoy", "muy", "pero", "señor", "buenos",
    "días", "puedo", "quisiera", "tengo",
}
_DE = {
    "der", "die", "das", "und", "ist", "ich", "sie", "ein", "eine", "nicht",
    "mit", "für", "auf", "haben", "hallo", "danke", "bestellung", "rechnung",
    "konto", "hilfe", "brauche", "möchte", "wann", "wo", "wie", "warum",
    "sehr", "aber", "bitte", "guten", "kann", "meine", "mein",
}
_EN = {
    "the", "a", "an", "and", "is", "are", "to", "of", "you", "your", "i", "we",
    "he", "she", "it", "for", "with", "on", "in", "my", "me", "hello", "hi",
    "thanks", "thank", "please", "order", "invoice", "account", "help", "need",
    "want", "where", "when", "how", "why", "but", "very", "can", "could",
    "would", "have", "this", "that", "do", "does", "get", "there",
}
_SETS = {"fr": _FR, "es": _ES, "de": _DE, "en": _EN}

# Diacritics that strongly favour one language (English uses none natively).
_DIA = {
    "fr": set("éèêëàâîïôûùüÿçœæ"),
    "es": set("ñáíóú¿¡"),
    "de": set("äößü"),   # 'ü'/'ä'/'ö' overlap FR/ES rarely; ß is unmistakably DE
}

_NAMES = {
    "fr": "French (Canadian French / français canadien)",
    "es": "Spanish (español)",
    "de": "German (Deutsch)",
    "en": "English",
}
_SHORT = {"fr": "French", "es": "Spanish", "de": "German", "en": "English"}

_WORD_RE = re.compile(r"[a-zà-ÿœæ']+", re.IGNORECASE)


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("MULTILINGUAL_ENABLED", "1")


def detect(text: str) -> str:
    """Best-guess ISO code for the language of `text` — one of fr/es/de/en.
    Defaults to 'en' on weak or ambiguous signal (the safe fallback). Never
    raises; deterministic; no cost."""
    if not ENABLED or not text:
        return "en"
    low = text.lower()
    words = _WORD_RE.findall(low)
    if not words:
        return "en"
    scores = {code: sum(1 for w in words if w in s) for code, s in _SETS.items()}
    # Diacritic evidence: each language-specific accented char adds weight
    # (capped) — a single 'ç' or 'ñ' is a very strong non-English tell.
    for code, chars in _DIA.items():
        d = sum(1 for ch in low if ch in chars)
        if d:
            scores[code] = scores.get(code, 0) + min(d, 4) + 1

    best = max(("fr", "es", "de"), key=lambda c: scores.get(c, 0))
    best_score = scores.get(best, 0)
    en_score = scores.get("en", 0)
    # Require real signal AND at least parity with English to leave 'en'. This
    # keeps an English sentence that happens to contain 'a'/'de'/'la' English.
    if best_score >= 2 and best_score >= en_score:
        return best
    return "en"


def language_name(code: str, short: bool = False) -> str:
    return (_SHORT if short else _NAMES).get(code, "English")


def directive(code: str) -> str:
    """A system-prompt fragment instructing the model to reply in `code`.
    Empty for English (or when disabled) so the default path is untouched."""
    if not ENABLED or code == "en" or code not in _NAMES:
        return ""
    name = _NAMES[code]
    return (
        f"\n\nLANGUAGE: The customer wrote in {name}. Reply ENTIRELY in {name}, "
        f"naturally and fluently, as a native speaker would. Any approved "
        f"knowledge or facts provided to you may be in English — translate their "
        f"SUBSTANCE into {name}; keep product names, prices, numbers, dates and "
        f"URLs exactly as given. Do not mention translation or that you switched "
        f"languages."
    )


def respond_in(text: str) -> str:
    """One-liner for call sites: the reply-language directive for whatever
    language the customer's `text` is in ('' for English). Append to a
    customer-facing system prompt."""
    return directive(detect(text))
