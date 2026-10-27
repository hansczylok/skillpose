import re
from pathlib import Path
from typing import get_args

import pytest

import skillpose.web
from skillpose.core.config import load_settings
from skillpose.core.matching import match_requirement
from skillpose.core.models import (
    CandidateProfile,
    Category,
    Conditions,
    GapType,
    Level,
    MatchStatus,
    ReqKind,
    Requirement,
    Seniority,
    Strength,
)
from skillpose.core.vocabulary import Vocabulary
from skillpose.web.i18n import LANGS, load_texts, pick_lang, reason_text, safe_next, translate

TEMPLATES = Path(skillpose.web.__file__).parent / "templates"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_languages_have_the_same_keys_and_no_empty_values():
    texts = load_texts()
    assert set(texts) == set(LANGS) and texts["de"]
    assert set(texts["de"]) == set(texts["en"])
    for lang in LANGS:
        assert not [k for k, v in texts[lang].items() if not v.strip()], lang
    for key in texts["de"]:
        assert set(PLACEHOLDER.findall(texts["de"][key])) == set(PLACEHOLDER.findall(texts["en"][key])), key


def test_every_role_family_has_a_label():
    texts = load_texts()
    for family in load_settings().role_families:
        for lang in LANGS:
            assert f"role.{family}" in texts[lang], (lang, family)


def test_every_model_value_has_a_label():
    texts = load_texts()
    groups = {"category": Category, "level": Level, "strength": Strength, "status": MatchStatus, "gap": GapType,
              "kind": ReqKind, "seniority": Seniority}
    keys = [f"{group}.{value}" for group, literal in groups.items() for value in get_args(literal)]
    keys += [f"task.{name}" for name in ["cv", "rematch", "postings", "collect", "path"]]
    for lang in LANGS:
        assert not [k for k in keys if k not in texts[lang]], lang


def test_templates_use_only_known_keys():
    texts = load_texts()
    used = {key for path in TEMPLATES.glob("*.html")
            for key in re.findall(r't\("([^"]+)"\)', path.read_text(encoding="utf-8"))}
    assert len(used) > 10
    assert not sorted(k for k in used if k not in texts["de"] or k not in texts["en"])


def test_translate_fills_placeholders_and_falls_back():
    texts = {"de": {"a": "läuft seit {time}"}, "en": {"a": "running since {time}", "b": "only English"}}
    assert translate(texts, "de", "a", time="10:31") == "läuft seit 10:31"
    assert translate(texts, "de", "b") == "only English" and translate(texts, "de", "missing.key") == "missing.key"
    assert translate(texts, "en", "a") == "running since {time}"
    assert translate(texts, "en", "a", other="x") == "running since {time}"


def test_translate_takes_the_singular_form_for_a_count_of_one():
    texts = {"en": {"n.one": "{count} posting", "n.other": "{count} postings", "plain": "{count} calls"},
             "de": {"n.one": "{count} Inserat", "n.other": "{count} Inserate"}}
    assert [translate(texts, "en", "n", count=n) for n in (0, 1, 2)] == ["0 postings", "1 posting", "2 postings"]
    assert translate(texts, "de", "n", count=1) == "1 Inserat" and translate(texts, "de", "n", count="1") == "1 Inserat"
    assert translate(texts, "de", "n", count="2,5") == "2,5 Inserate"
    assert translate(texts, "en", "plain", count=1) == "1 calls"  # a text without forms stays as it is


@pytest.mark.parametrize("lang, key, one, two", [
    ("en", "cost.cents", "about 1 cent", "about 2 cents"),
    ("de", "cost.cents", "ca. 1 Cent", "ca. 2 Cent"),
    ("en", "count.postings", "1 posting", "2 postings"),
    ("de", "count.postings", "1 Inserat", "2 Inserate"),
    ("en", "count.skills", "1 skill", "2 skills"),
    ("de", "count.skills", "1 Kompetenz", "2 Kompetenzen"),
    ("en", "taskdone.limit", "Monthly limit reached – 1 posting stays open",
     "Monthly limit reached – 2 postings stay open"),
    ("de", "taskdone.limit", "Monatsgrenze erreicht – 1 Inserat bleibt offen",
     "Monatsgrenze erreicht – 2 Inserate bleiben offen"),
    ("en", "matches.weeks", "about 1 week", "about 2 weeks"),
    ("de", "matches.weeks", "ca. 1 Woche", "ca. 2 Wochen"),
    ("en", "profile.years", "1 year", "2 years"),
    ("de", "profile.years", "1 Jahr", "2 Jahre"),
    ("en", "paths.too_little", "too little data (at least 1 posting)", "too little data (at least 2 postings)"),
    ("de", "paths.too_little", "zu wenig Daten (mindestens 1 Inserat)", "zu wenig Daten (mindestens 2 Inserate)"),
    ("en", "cv.chars", "1 character", "2 characters"),
    ("de", "cv.chars", "1 Zeichen", "2 Zeichen"),
])
def test_counted_texts_have_a_singular(lang, key, one, two):
    texts = load_texts()
    assert translate(texts, lang, key, count=1) == one and translate(texts, lang, key, count=2) == two


def test_every_singular_has_a_plural():
    for lang, texts in load_texts().items():
        forms = [k.removesuffix(".one") for k in texts if k.endswith(".one")]
        assert len(forms) >= 8 and all(f"{k}.other" in texts for k in forms), lang
        assert all("{count}" in texts[f"{k}.other"] for k in forms), lang


def test_task_messages_count_in_words():
    texts = load_texts()

    def t(lang, key, **params):
        return translate(texts, lang, key, **params)

    for lang, expected in [("en", "Profile ready: 1 skill, 1 posting matched"),
                           ("de", "Profil fertig: 1 Kompetenz, 1 Inserat abgeglichen")]:
        skills, postings = t(lang, "count.skills", count=1), t(lang, "count.postings", count=1)
        assert t(lang, "taskdone.cv", skills=skills, postings=postings) == expected
    assert t("en", "taskdone.rematch", postings=t("en", "count.postings", count=1)) == "Matches recomputed: 1 posting"
    assert t("de", "taskdone.batch", postings=t("de", "count.postings", count=20)).startswith(
        "Batch abgeschickt: 20 Inserate zum halben Preis")


def test_pick_lang():
    assert pick_lang("en", "de", "de-AT") == "en" and pick_lang(None, "de", "en") == "de"
    assert pick_lang(None, None, "de-AT,de;q=0.9") == "de" and pick_lang(None, None, "fr") == "en"


def test_pick_lang_follows_quality_and_ignores_unknown_values():
    assert pick_lang(None, None, "fr;q=1, de;q=0.5, en;q=0.8") == "en"
    assert pick_lang(None, None, "de;q=0, en-GB;q=0.3") == "en" and pick_lang(None, None, "de;q=x") == "en"
    assert pick_lang("xx", "yy", "DE-de") == "de" and pick_lang(None, None, None) == "en"


# the English reasons that use the words of the form, not the stored ones (display only: the stored text is unchanged)
REWORDED_EN = {"profile: remote only": "profile: exclusively from home",
               "profile: not only remote": "profile: work on site is possible",
               "profile: full-time": "profile: full-time possible",
               "profile: no full-time": "profile: full-time not possible"}


def test_reason_text_translates_every_matcher_reason():
    for r in ["evidenced", "not in the CV", "only listed, no evidence of use", "last used 2013",
              "level working, strong required", "3 of 5 years", "3 of 5 years required", "years of use unknown",
              "related: power_automate", "level basic, working required; last used 2012",
              "higher degree: masters_engineering", "higher degree, other field: masters_engineering",
              "soft skill: not verifiable from a CV", "not answered in the profile",
              "the profile asks nothing about this condition", "working time of the posting not recognised",
              "profile: driving licence B", "profile: no driving licence B",
              "profile: travel up to 20 %, posting asks 30 %", "profile: travel up to 20 %, posting asks 12.5 %",
              "travel share of the posting not recognised", "profile: shift work", "profile: no shift work",
              "profile: remote only", "profile: not only remote", "profile: commute up to 30 km",
              "profile: full-time", "profile: no full-time", "posting asks part-time"]:
        assert reason_text(r, "de") != r and reason_text(r, "en") == REWORDED_EN.get(r, r)


def test_reason_text_keeps_values_and_unknown_parts():
    assert "2,5" in reason_text("2.5 of 5 years", "de") and "2013" in reason_text("last used 2013", "de")
    assert "power_automate" in reason_text("related: power_automate", "de")
    assert reason_text("higher degree: masters_engineering", "de") == "höherer Abschluss: masters_engineering"
    assert reason_text("higher degree, other field: masters_engineering", "de") \
        == "höherer Abschluss, anderes Fach: masters_engineering"
    assert "Praxiswissen" in reason_text("level basic, working required", "de")
    assert reason_text("something new; evidenced", "de").startswith("something new; ")
    assert reason_text("profile: travel up to 20 %, posting asks 30 %", "de") \
        == "Profil: Reisen bis 20 %, Inserat verlangt 30 %"
    assert reason_text("profile: commute up to 45 km", "de") == "Profil: Pendeln bis 45 km"
    assert reason_text("profile: travel up to 20 %, posting asks 12.5 %", "de") \
        == "Profil: Reisen bis 20 %, Inserat verlangt 12,5 %"
    assert reason_text("travel share of the posting not recognised", "de") == "Reiseanteil des Inserats nicht erkannt"


def test_the_reasons_of_the_place_of_work_and_the_working_time_use_the_words_of_the_form_in_both_languages():
    """The questions say "ausschließlich von zu Hause", "vor Ort" and "Vollzeit arbeiten"; the matcher keeps its stored
    reasons, only the display changes (English too)."""
    assert reason_text("profile: remote only", "de") == "Profil: ausschließlich von zu Hause"
    assert reason_text("profile: not only remote", "de") == "Profil: Arbeit vor Ort möglich"
    assert reason_text("profile: full-time", "de") == "Profil: Vollzeit möglich"
    assert reason_text("profile: no full-time", "de") == "Profil: Vollzeit nicht möglich"
    assert reason_text("profile: remote only", "en") == "profile: exclusively from home"
    assert reason_text("profile: not only remote", "en") == "profile: work on site is possible"
    assert reason_text("profile: full-time", "en") == "profile: full-time possible"
    assert reason_text("profile: no full-time", "en") == "profile: full-time not possible"
    assert reason_text("profile: commute up to 30 km; evidenced", "en") == "profile: commute up to 30 km; evidenced"


def test_english_reasons_keep_the_stored_form_of_levels_and_numbers():
    stored = "level basic, working required; last used 2012"
    assert reason_text(stored, "en") == stored
    assert reason_text("2.5 of 5 years; something new", "en") == "2.5 of 5 years; something new"


def test_the_place_of_work_has_one_name_in_the_conditions_texts():
    """Question, hints and reasons say "Arbeitsort" / "place of work", not "Ortsvorgabe" / "place-of-work"."""
    texts = load_texts()
    for lang, term in (("de", "Arbeitsort"), ("en", "place of work")):
        mine = {k: v for k, v in texts[lang].items() if k.startswith(("conditions.", "reason.", "matches.conditions"))}
        assert not [k for k, v in mine.items() if "Ortsvorgabe" in v or "place-of-work" in v], lang
        assert term in mine["conditions.remote_only_hint"] and term in mine["conditions.commute_km_max_hint"], lang


@pytest.mark.parametrize("value, german", [
    ("ca. 30 %", "Profil: Reisen bis 20 %, Inserat verlangt 30 %"),
    ("12,5 %", "Profil: Reisen bis 20 %, Inserat verlangt 12,5 %"),
    ("2 Tage pro Woche", "Reiseanteil des Inserats nicht erkannt")])
def test_the_travel_reasons_that_the_matcher_makes_are_translated(value, german):
    """The strings come from core.matching, not from this file: a change of the matcher text shows here."""
    req = Requirement(skill_id="travel_willingness", importance="must", min_level=None, min_years=None,
                      kind="condition", quote="Reisebereitschaft", condition="travel", condition_value=value)
    profile = CandidateProfile(language="de", skills=[], stations=[], conditions=Conditions(travel_share_max=20))
    reason = match_requirement(req, profile, Vocabulary.load(), {}, load_settings().rules, 2026).reason
    assert reason_text(reason, "en") == reason and reason_text(reason, "de") == german


def test_safe_next():
    assert safe_next("/cv/1") == "/cv/1" and safe_next("//evil.example") == "/" and safe_next("https://x") == "/"
    assert safe_next(None) == "/"
    assert safe_next("/\\evil.example") == "/" and safe_next("/a\r\nSet-Cookie: x=1") == "/" and safe_next("") == "/"
