"""Check that every quote Claude gives as evidence really stands in the CV.

The Citations feature of the API cannot be combined with structured output, so the program checks quotes itself:
first as a substring after normalization, then by similarity over word windows of the source.
"""

import re
import unicodedata
from difflib import SequenceMatcher

from skillpose.core.models import CandidateSkill

_QUOTES = str.maketrans({c: '"' for c in "„“”‚‘’'»«"})
_DASHES = str.maketrans({c: "-" for c in "–—‐‑"})
_HYPHENATION = re.compile(r"(\w)-\s*\n\s*(\w)")


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("­", "")
    s = s.translate(_QUOTES).translate(_DASHES)
    s = _HYPHENATION.sub(r"\1\2", s)
    return " ".join(s.casefold().split())


def verify_quote(quote: str, source: str, threshold: float = 0.90) -> bool:
    q, s = normalize_text(quote), normalize_text(source)
    if not q:
        return False
    if q in s:
        return True
    words, n = s.split(" "), len(q.split(" "))
    matcher = SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(q)  # fixed side: its index is built once
    for size in sorted({max(1, n - 1), n, n + 1}):
        for i in range(max(1, len(words) - size + 1)):
            matcher.set_seq1(" ".join(words[i:i + size]))
            if (matcher.real_quick_ratio() >= threshold and matcher.quick_ratio() >= threshold
                    and matcher.ratio() >= threshold):
                return True
    return False


def apply_verification(skills: list[CandidateSkill], source: str) -> list[CandidateSkill]:
    """Mark each quote as verified or not; an unverified quote only counts as a claim."""
    checked = []
    for skill in skills:
        evidence = []
        for e in skill.evidence:
            if verify_quote(e.quote, source):
                evidence.append(e.model_copy(update={"verified": True}))
            else:
                evidence.append(e.model_copy(update={"verified": False, "strength": "claim"}))
        checked.append(skill.model_copy(update={"evidence": evidence}))
    return checked
