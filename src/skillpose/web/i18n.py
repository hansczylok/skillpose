"""Texts of the web pages in German and English.

The texts live in i18n/<lang>.yaml, nested for the translator and flattened to dotted keys ("nav.profile") here. The
matcher writes its reasons in English (they are stored with each match); reason_text shows them with the texts of
the language, English included: a few English texts use the words of the questions of the conditions form, not the
stored ones.
"""

import functools
import re
from pathlib import Path

import yaml

LANGS = ("de", "en")
I18N_DIR = Path(__file__).parent / "i18n"

# the reasons of core.matching, one pattern per part; kind tells how to show the captured values
REASONS: list[tuple[re.Pattern[str], str, str | None]] = [
    (re.compile(r"evidenced"), "reason.evidenced", None),
    (re.compile(r"not in the CV"), "reason.not_in_cv", None),
    (re.compile(r"only listed, no evidence of use"), "reason.only_listed", None),
    (re.compile(r"last used (?P<year>\d+)"), "reason.last_used", None),
    (re.compile(r"level (?P<have>\w+), (?P<need>\w+) required"), "reason.level", "level"),
    (re.compile(r"(?P<have>[\d.]+) of (?P<need>[\d.]+) years required"), "reason.years_required", "number"),
    (re.compile(r"(?P<have>[\d.]+) of (?P<need>[\d.]+) years"), "reason.years", "number"),
    (re.compile(r"years of use unknown"), "reason.years_unknown", None),
    (re.compile(r"related: (?P<id>\S+)"), "reason.related", None),
    (re.compile(r"higher degree: (?P<id>\S+)"), "reason.higher_degree", None),
    (re.compile(r"higher degree, other field: (?P<id>\S+)"), "reason.higher_degree_other_field", None),
    (re.compile(r"soft skill: not verifiable from a CV"), "reason.soft_unverifiable", None),
    (re.compile(r"not answered in the profile"), "reason.unanswered", None),
    (re.compile(r"the profile asks nothing about this condition"), "reason.no_question", None),
    (re.compile(r"working time of the posting not recognised"), "reason.working_time_unknown", None),
    (re.compile(r"travel share of the posting not recognised"), "reason.travel_unknown", None),
    (re.compile(r"profile: driving licence B"), "reason.licence", None),
    (re.compile(r"profile: no driving licence B"), "reason.no_licence", None),
    # the share of the posting can have decimals ("12.5 %"; German "12,5 %")
    (re.compile(r"profile: travel up to (?P<have>\d+) %, posting asks (?P<need>[\d.]+) %"), "reason.travel", "number"),
    (re.compile(r"profile: shift work"), "reason.shift_work", None),
    (re.compile(r"profile: no shift work"), "reason.no_shift_work", None),
    (re.compile(r"profile: remote only"), "reason.remote_only", None),
    (re.compile(r"profile: not only remote"), "reason.not_remote_only", None),
    (re.compile(r"profile: commute up to (?P<km>\d+) km"), "reason.commute", None),
    (re.compile(r"profile: full-time"), "reason.full_time", None),
    (re.compile(r"profile: no full-time"), "reason.no_full_time", None),
    (re.compile(r"posting asks part-time"), "reason.part_time", None),
]


def _flatten(tree: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = "" if value is None else str(value)
    return flat


def load_texts() -> dict[str, dict[str, str]]:
    """{lang: {dotted key: text}} for every language in LANGS."""
    return {lang: _flatten(yaml.safe_load((I18N_DIR / f"{lang}.yaml").read_text(encoding="utf-8")) or {})
            for lang in LANGS}


@functools.cache
def _texts() -> dict[str, dict[str, str]]:
    return load_texts()


class _KeepMissing(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def translate(texts: dict[str, dict[str, str]], lang: str, key: str, **params) -> str:
    """The text for key in lang, else in English, else the key itself; {name} placeholders filled from params. A text
    with a singular has the keys <key>.one and <key>.other: the parameter count picks one ("1" or 1: .one)."""
    if "count" in params and any(f"{key}.one" in texts.get(name, {}) for name in (lang, "en")):
        key = f"{key}.one" if str(params["count"]) == "1" else f"{key}.other"
    text = texts.get(lang, {}).get(key) or texts.get("en", {}).get(key) or key
    return text.format_map(_KeepMissing(params)) if params else text


def _accepted(accept_language: str) -> list[str]:
    """Primary language tags of an Accept-Language header, best first; q=0 and broken q values are dropped."""
    ranked = []
    for item in accept_language.split(","):
        tag, _, params = item.strip().partition(";")
        quality = 1.0
        if params.strip().startswith("q="):
            try:
                quality = float(params.strip()[2:])
            except ValueError:
                quality = 0.0
        if tag and quality > 0:
            ranked.append((quality, tag.split("-")[0].strip().lower()))
    return [tag for _, tag in sorted(ranked, key=lambda r: -r[0])]


def pick_lang(query: str | None, cookie: str | None, accept_language: str | None) -> str:
    """?lang= beats the lang cookie beats the browser's Accept-Language beats English."""
    for choice in (query, cookie):
        if choice in LANGS:
            return choice
    return next((tag for tag in _accepted(accept_language or "") if tag in LANGS), "en")


def _show(texts: dict[str, dict[str, str]], lang: str, kind: str | None, value: str) -> str:
    """A captured value as shown in lang. English keeps the stored form ("working", "12.5")."""
    if lang != "de":
        return value
    if kind == "level" and f"level.{value}" in texts[lang]:
        return texts[lang][f"level.{value}"]
    if kind == "number":
        return value.replace(".", ",")
    return value


def reason_text(reason: str, lang: str) -> str:
    """A matcher reason in lang, part by part; a part without a known pattern stays as it is. The English texts equal
    the stored reasons except for the place of work and the working time (the words of the form)."""
    texts = _texts()
    parts = []
    for part in reason.split("; "):
        for pattern, key, kind in REASONS:
            if match := pattern.fullmatch(part):
                values = {name: _show(texts, lang, kind, value) for name, value in match.groupdict().items()}
                parts.append(translate(texts, lang, key, **values))
                break
        else:
            parts.append(part)
    return "; ".join(parts)


def safe_next(path: str | None) -> str:
    """A redirect target on this site: a path "/...", never "//..." or "/\\..." (another host), no control chars."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        return "/"
    return path
