"""The language of a text, German or English, from its stop words.

The demo shows each posting also translated into the other language, so the program must know which language a
posting is written in. Counting a few frequent function words is enough for job postings and needs no call: it is
deterministic and free. Words that both languages use ("in", "an", "so", "also", "was") do not count.
"""

import re
from typing import Literal

Lang = Literal["de", "en"]

GERMAN = frozenset({
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer", "eines", "und", "oder", "mit",
    "für", "von", "zu", "zur", "zum", "im", "auf", "bei", "sowie", "nicht", "auch", "ist", "sind", "wir", "du", "dich",
    "dir", "dein", "deine", "deinen", "deiner", "ihr", "ihre", "ihren", "ihrem", "ihrer", "sie", "sich", "unser",
    "unsere", "unseren", "unserem", "unserer", "als", "nach", "aus", "über", "wie", "bis", "je", "ab",
})
ENGLISH = frozenset({
    "the", "a", "and", "or", "with", "for", "of", "to", "on", "at", "by", "from", "not", "as", "is", "are", "be",
    "will", "this", "that", "it", "we", "you", "your", "our", "us", "their", "they", "what", "who", "have", "has",
})
_WORD = re.compile(r"[^\W\d_]+")


def detect_language(text: str) -> Lang:
    """"en" if the text has more English than German stop words, else "de" (also for a text without any)."""
    words = _WORD.findall(text.lower())
    german = sum(w in GERMAN for w in words)
    english = sum(w in ENGLISH for w in words)
    return "en" if english > german else "de"


def other_language(lang: Lang) -> Lang:
    return "en" if lang == "de" else "de"
