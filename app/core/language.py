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
    # Farewells and other very short utterances. detect() needs TWO signals to
    # leave English, and a goodbye is often only two or three words long — so
    # "merci, au revoir" scored 1 (merci) and came back English, which then
    # picked the English farewell. The words that end a conversation have to
    # be in the table, or the conversation ends in the wrong language.
    "revoir", "bientôt", "salut", "désolé", "entreprise", "société",
}
_ES = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "que", "y",
    "en", "es", "por", "para", "con", "su", "mi", "hola", "gracias", "pedido",
    "factura", "cuenta", "ayuda", "necesito", "quiero", "dónde", "cuándo",
    "cómo", "porque", "está", "estoy", "muy", "pero", "señor", "buenos",
    "días", "puedo", "quisiera", "tengo",
    "adiós", "adios", "luego", "hasta", "chao", "empresa", "compañía",
}
_DE = {
    "der", "die", "das", "und", "ist", "ich", "sie", "ein", "eine", "nicht",
    "mit", "für", "auf", "haben", "hallo", "danke", "bestellung", "rechnung",
    "konto", "hilfe", "brauche", "möchte", "wann", "wo", "wie", "warum",
    "sehr", "aber", "bitte", "guten", "kann", "meine", "mein",
    "tschüss", "wiederhören", "auf", "unternehmen", "firma",
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
    # Written Chinese: the model must be told WHICH script to answer in, because
    # Simplified and Traditional are not interchangeable to a reader. Simplified
    # is the safer default for a general audience; a Traditional-writing customer
    # is handled by the mirroring instruction in directive().
    "zh": "Mandarin Chinese (中文, Simplified characters)",
    "en": "English",
}
_SHORT = {"fr": "French", "es": "Spanish", "de": "German",
          "zh": "Chinese", "en": "English"}

# Languages we can actually SERVE end to end (reply + voice). A script we can
# detect but not serve must fall back to English rather than promise support we
# don't have — ja/ko are detected precisely so they DON'T get mislabelled as
# Chinese, not because we support them.
_SUPPORTED = {"fr", "es", "de", "zh", "en"}

_WORD_RE = re.compile(r"[a-zà-ÿœæ']+", re.IGNORECASE)

# ── Non-Latin scripts ────────────────────────────────────────────────────────
# The stopword+diacritic scorer above is structurally blind to any language that
# doesn't use spaced Latin words: Chinese text scored ZERO on every set and fell
# through to the English default, so a Chinese customer silently got an English
# reply on every channel. Script detection fixes that far more reliably than
# stopwords ever could — a Han character IS the signal, with no ambiguity and no
# word segmentation needed (Chinese doesn't put spaces between words, which is
# precisely why the word-based scorer could never see it).
_HAN = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
# Japanese uses Han characters TOO, so "has Han" alone is not "is Chinese".
# Kana is the disambiguator: any kana means Japanese, not Chinese.
_KANA = re.compile(r"[぀-ゟ゠-ヿ]")
_HANGUL = re.compile(r"[가-힯ᄀ-ᇿ]")

# How much non-Latin script is needed before we switch languages. A single Han
# character inside an English sentence (a product name, a pasted brand) must not
# flip the whole conversation — the same conservatism the Latin scorer uses.
_SCRIPT_MIN_CHARS = 2


def _script_language(text: str) -> str | None:
    """Detect a non-Latin script language, or None to fall through to the
    word-based scorer. Deterministic, no dependency, no cost.

    Kana and Hangul are checked BEFORE the Han threshold, because Korean is
    usually written with no Han characters at all and Japanese can be — so
    gating on a Han count first would make this function silently blind to the
    very scripts it exists to tell apart from Chinese. Each script carries the
    same minimum-character conservatism, so one stray glyph in an English
    sentence never flips the conversation."""
    if len(_KANA.findall(text)) >= _SCRIPT_MIN_CHARS:
        return "ja"          # kana → Japanese (detected so it is NOT read as zh)
    if len(_HANGUL.findall(text)) >= _SCRIPT_MIN_CHARS:
        return "ko"          # Hangul → Korean (same reason)
    if len(_HAN.findall(text)) >= _SCRIPT_MIN_CHARS:
        # Han with no kana and no Hangul: Chinese.
        return "zh"
    return None


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("MULTILINGUAL_ENABLED", "1")


def detect(text: str) -> str:
    """Best-guess ISO code for the language of `text` — one of fr/es/de/en.
    Defaults to 'en' on weak or ambiguous signal (the safe fallback). Never
    raises; deterministic; no cost."""
    if not ENABLED or not text:
        return "en"
    # Script check FIRST: a non-Latin script is unambiguous evidence, and the
    # word scorer below cannot see it at all (no spaced Latin words to score).
    script = _script_language(text)
    if script:
        return script if script in _SUPPORTED else "en"
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
    #
    # The bar SCALES with length, because a fixed bar of 2 is unreachable for
    # the short utterances that matter most: "au revoir" and "adios" are two
    # words and one word, can never score 2, and so were answered in English —
    # the goodbye is exactly where a wrong language is most visible. A long
    # sentence still needs two independent signals; parity with English is
    # required either way, so a short ENGLISH message cannot be flipped by one
    # shared word.
    need = 1 if len(words) <= 3 else 2
    if best_score >= need and best_score >= en_score:
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
    extra = ""
    if code == "zh":
        # Simplified vs Traditional is not a style choice — mixing them, or
        # answering a Traditional writer in Simplified, reads as wrong to the
        # customer. Mirror what they used.
        extra = (" Match the customer's character set: reply in Traditional "
                 "characters (繁體) if they wrote in Traditional, otherwise "
                 "Simplified (简体). Do not mix the two in one reply.")
    return (
        f"\n\nLANGUAGE: The customer wrote in {name}. Reply ENTIRELY in {name}, "
        f"naturally and fluently, as a native speaker would. Any approved "
        f"knowledge or facts provided to you may be in English — translate their "
        f"SUBSTANCE into {name}; keep product names, prices, numbers, dates and "
        f"URLs exactly as given. Do not mention translation or that you switched "
        f"languages." + extra
    )


def intent_re(en_src: str, latin=(), cjk=()) -> "re.Pattern":
    """Build an intent regex that fires in every language we answer in.

    Lives here, not in a voice module, because BOTH phone lines (sdr.py and
    voice_support.py) need it and a second copy of an intent list is a second
    thing to forget to update — the way "au revoir" ended a call on one line
    and ran it to the turn limit on the other.

    Two mechanisms, because two writing systems:
      latin  fr/es/de alternates, kept \\b-anchored like the English source.
      cjk    plain substrings. Chinese is written WITHOUT spaces, so \\b never
             matches between Han characters — a \\b-anchored Chinese
             alternative does not merely match less, it never fires at all,
             and it fails silently. That is how this class of bug survives.

    Speech transcripts vary the apostrophe, so every ' also accepts ’."""
    parts = [f"(?:{en_src})"]
    if latin:
        alts = "|".join(re.escape(t).replace("'", "['’]") for t in latin)
        parts.append(r"\b(?:" + alts + r")\b")
    if cjk:
        parts.append("(?:" + "|".join(re.escape(t) for t in cjk) + ")")
    return re.compile("|".join(parts), re.IGNORECASE | re.UNICODE)


# "Stop talking" is a request to END THE CALL, and it was in neither line's
# goodbye pattern — a caller who said it got a cheerful "of course!" and then
# more talking. Shared so both lines honour it identically.
STOP_LATIN = ["au revoir", "c'est tout", "rien d'autre", "j'ai terminé",
              "raccrocher", "arrêtez", "arrête", "taisez-vous", "ça suffit",
              "adiós", "adios", "eso es todo", "nada más", "nada mas",
              "hasta luego", "colgar", "basta", "ya basta", "cállate",
              "cállese", "auf wiederhören", "tschüss", "das war's",
              "das wars", "sonst nichts", "hör auf", "aufhören", "sei still"]
STOP_CJK = ["再见", "再見", "拜拜", "就这样", "就這樣", "没有了", "沒有了",
            "没别的", "沒別的", "挂了", "掛了", "结束通话", "結束通話",
            "别说了", "別說了", "闭嘴", "閉嘴", "够了", "夠了", "不用了"]
# English phrases that mean "stop", added to both lines' goodbye patterns.
STOP_EN = (r"stop talking|please stop|stop it|shut up|be quiet|"
           r"that'?s enough|never ?mind")


def respond_in(text: str) -> str:
    """One-liner for call sites: the reply-language directive for whatever
    language the customer's `text` is in ('' for English). Append to a
    customer-facing system prompt."""
    return directive(detect(text))
