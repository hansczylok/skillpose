"""The tiers on the pages: number, word and colour; the help box with the numbers of the settings; the colours."""

import re
from pathlib import Path

import pytest
from fakes import ScriptedClient
from test_web_jobs import cells, row, seed_match
from test_web_market import block, cv_with_profile, seed_family
from webkit import make_client

import skillpose.web
from skillpose.core.config import load_settings
from skillpose.store.db import Store
from skillpose.web.i18n import LANGS, load_texts

WEB = Path(skillpose.web.__file__).parent
DE = {"accept-language": "de"}
BADGES = {"en": {1: "1 · fits", 2: "2 · learnable", 3: "3 · big gap", None: "– · too few postings"},
          "de": {1: "1 · passt", 2: "2 · lernbar", 3: "3 · große Lücke", None: "– · zu wenige Inserate"}}
HELP = {"en": ("What do the tiers mean?", "1 is best."), "de": ("Was bedeuten die Stufen?", "1 ist am besten.")}


def badge(html: str, kind: str) -> str:
    """The text of the one badge of a kind (tier-1, tier-2, tier-3, tier-none) in some HTML."""
    [found] = re.findall(rf'<span class="mark tier-{kind}">([^<]*)</span>', html)
    return found


def seeded_matches(tmp_path, **kwargs):
    """CV 1 with one posting per tier (the matches page sorts them 1, 2, 3)."""
    c = make_client(tmp_path, **kwargs)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        hashes = {tier: seed_match(st, cv_id, f"Posting of tier {tier}", tier, 0.9 - tier / 10,
                                   [("java", "must", "met", None, "evidenced")]) for tier in (1, 2, 3)}
    return c, hashes


def with_rules(**changes):
    settings = load_settings()
    return settings.model_copy(update={"rules": settings.rules.model_copy(update=changes)})


def help_box(html: str) -> str:
    [found] = re.findall(r'<details class="tier-help">.*?</details>', html, re.S)
    return found


# --- the badge: number, word and colour class ---

@pytest.mark.parametrize("lang", LANGS)
def test_the_texts_of_the_badges_are_in_both_languages(lang):
    texts = load_texts()[lang]
    assert [texts[k] for k in ("tier.fits", "tier.learnable", "tier.big_gap", "tier.none")] == [
        BADGES[lang][1], BADGES[lang][2], BADGES[lang][3], BADGES[lang][None]]


@pytest.mark.parametrize("lang", LANGS)
def test_matches_table_shows_number_word_and_class_of_each_posting(tmp_path, lang):
    c, _ = seeded_matches(tmp_path)
    page = c.get("/cv/1/matches", headers=DE if lang == "de" else {}).text
    for tier in (1, 2, 3):
        assert cells(row(page, f"Posting of tier {tier}"))[2] == BADGES[lang][tier]
        assert badge(row(page, f"Posting of tier {tier}"), str(tier)) == BADGES[lang][tier]


def test_matches_table_keeps_the_order_by_tier(tmp_path):
    c, _ = seeded_matches(tmp_path)
    page = c.get("/cv/1/matches").text
    assert re.findall(r">(Posting of tier \d)</a>", page) == [f"Posting of tier {tier}" for tier in (1, 2, 3)]
    assert page.index('class="mark tier-1"') < page.index('class="mark tier-2"') < page.index('class="mark tier-3"')


@pytest.mark.parametrize("lang", LANGS)
def test_match_detail_shows_the_tier_with_its_word(tmp_path, lang):
    c, hashes = seeded_matches(tmp_path)
    for tier in (1, 2, 3):
        page = c.get(f"/cv/1/matches/{hashes[tier]}", headers=DE if lang == "de" else {}).text
        assert badge(page, str(tier)) == BADGES[lang][tier]


@pytest.mark.parametrize("lang", LANGS)
def test_market_shows_the_tier_of_each_role_with_its_word(tmp_path, lang):
    c = make_client(tmp_path, settings=with_rules(min_jobs_per_role=5))
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "test_automation", {"regression_testing": 6.0}, tier=1)
        seed_family(st, cv_id, "qa_management", {"api_testing": 2.0}, tier=2)
        seed_family(st, cv_id, "rpa_process_automation", {"sql": 2.0}, tier=3)
        seed_family(st, cv_id, "data_analysis_bi", {"performance_testing": 2.0}, n=2)  # too few postings for a tier
    page = c.get(f"/cv/{cv_id}/market", headers=DE if lang == "de" else {}).text
    for family, kind in (("test_automation", "1"), ("qa_management", "2"), ("rpa_process_automation", "3"),
                         ("data_analysis_bi", "none")):
        tier = None if kind == "none" else int(kind)
        assert badge(block(page, family), kind) == BADGES[lang][tier], family
    assert len(re.findall(r'class="mark tier-', page)) == 4   # one badge per role, none besides the help box
    assert page.index("family-test_automation") < page.index("family-qa_management") < page.index(
        "family-rpa_process_automation") < page.index("family-data_analysis_bi")


@pytest.mark.parametrize("lang", LANGS)
def test_paths_page_shows_the_tier_of_the_role_with_its_word(tmp_path, lang):
    c = make_client(tmp_path, client=ScriptedClient())
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "qa_management", {"api_testing": 2.0}, tier=2)
    page = c.get(f"/cv/{cv_id}/paths", headers=DE if lang == "de" else {}).text
    assert badge(block(page, "qa_management"), "2") == BADGES[lang][2]


# --- the help box ---

@pytest.mark.parametrize("lang", LANGS)
def test_matches_page_has_a_closed_help_box_above_the_table(tmp_path, lang):
    c, _ = seeded_matches(tmp_path)
    page = c.get("/cv/1/matches", headers=DE if lang == "de" else {}).text
    box = help_box(page)
    assert re.match(r'<details class="tier-help">\s*<summary>' + re.escape(HELP[lang][0]) + "</summary>", box)
    assert " open" not in box.split(">", 1)[0] and box.index(HELP[lang][1]) < box.index("<li")
    assert page.index(box) < page.index("<table")
    assert box.count("<li") == 4


@pytest.mark.parametrize("lang", LANGS)
def test_market_page_has_a_closed_help_box_above_the_roles(tmp_path, lang):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "test_automation", {"api_testing": 2.0})
    page = c.get(f"/cv/{cv_id}/market", headers=DE if lang == "de" else {}).text
    box = help_box(page)
    assert re.match(r'<details class="tier-help">\s*<summary>' + re.escape(HELP[lang][0]) + "</summary>", box)
    assert " open" not in box.split(">", 1)[0] and box.index(HELP[lang][1]) < box.index("<li")
    assert page.index(box) < page.index('<section class="family"')


def test_help_box_names_the_numbers_of_the_settings(tmp_path):
    c, _ = seeded_matches(tmp_path)
    en = " ".join(re.sub(r"<[^>]+>", " ", help_box(c.get("/cv/1/matches").text)).split())
    assert "Tier 1 – fits well: at least 80 % of the must requirements are met (partly met counts half)" in en
    assert "no gap in years of experience or degree, and no must condition that your answers do not meet." in en
    assert ("Tier 2 – gaps can be learned: no gap in years of experience or degree and no must condition that your "
            "answers do not meet;") in en and "in at most 26 weeks" in en
    assert "Tier 3 – big gap: otherwise, for example missing years of experience, a missing degree" in en
    assert "or more than 26 weeks" in en
    assert "Tier of a role: the best tier that at least 40 % of its postings reach" in en
    assert "a role needs at least 1 posting." in en  # the default minimum: one posting
    de = " ".join(re.sub(r"<[^>]+>", " ", help_box(c.get("/cv/1/matches", headers=DE).text)).split())
    assert ("Stufe 1 – passt weitgehend: mindestens 80 % der Muss-Anforderungen erfüllt (teilweise erfüllt zählt "
            "halb), keine Lücke bei Berufsjahren oder Abschluss und keine Muss-Bedingung, die du nicht erfüllst.") in de
    assert ("Stufe 2 – Lücken lernbar: keine Lücke bei Berufsjahren oder Abschluss und keine Muss-Bedingung, die du "
            "nicht erfüllst; alle fehlenden Muss-Anforderungen zusammen in höchstens 26 Wochen lernbar.") in de
    assert ("Stufe 3 – große Lücke: sonst, z. B. fehlende Berufsjahre, ein fehlender Abschluss oder mehr als "
            "26 Wochen.") in de
    assert ("Stufe einer Rolle: die beste Stufe, die mindestens 40 % ihrer Inserate erreichen; "
            "mindestens 1 Inserat.") in de


@pytest.mark.parametrize("minimum, en, de", [
    (1, "a role needs at least 1 posting.", "mindestens 1 Inserat."),
    (2, "a role needs at least 2 postings.", "mindestens 2 Inserate."),
    (5, "a role needs at least 5 postings.", "mindestens 5 Inserate."),
])
def test_help_box_counts_the_minimum_of_postings_in_words(tmp_path, minimum, en, de):
    c, _ = seeded_matches(tmp_path, settings=with_rules(min_jobs_per_role=minimum))
    for headers, expected in (({}, en), (DE, de)):
        box = " ".join(re.sub(r"<[^>]+>", " ", help_box(c.get("/cv/1/market", headers=headers).text)).split())
        assert expected in box


@pytest.mark.parametrize("path", ["matches", "market"])
def test_help_box_follows_changed_settings(tmp_path, path):
    settings = with_rules(tier1_coverage=0.7, tier2_max_weeks=12, role_tier_share=0.6, min_jobs_per_role=3)
    c, _ = seeded_matches(tmp_path, settings=settings)
    for headers, expected in (({}, ["at least 70 %", "at most 12 weeks", "more than 12 weeks", "at least 60 %",
                                    "at least 3 postings"]),
                              (DE, ["mindestens 70 %", "höchstens 12 Wochen", "mehr als 12 Wochen", "mindestens 60 %",
                                    "mindestens 3 Inserate"])):
        box = " ".join(re.sub(r"<[^>]+>", " ", help_box(c.get(f"/cv/1/{path}", headers=headers).text)).split())
        assert [x for x in expected if x not in box] == []
        assert not re.search(r"\b(80|26|40) %|\b26 (weeks|Wochen)|\b5 (postings|Inserate)", box)


def test_the_old_one_line_legends_are_gone(tmp_path):
    c, _ = seeded_matches(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "test_automation", {"api_testing": 2.0})
    for headers in ({}, DE):
        for path in ("matches", "market"):
            page = c.get(f"/cv/1/{path}", headers=headers).text
            assert 'class="hint">Tier' not in page and 'class="hint">Stufe' not in page
            assert "the lowest tier" not in page and "die niedrigste Stufe" not in page
    for texts in load_texts().values():
        assert not [k for k in ("matches.tier", "matches.legend", "market.legend_tier", "market.too_little")
                    if k in texts]


# --- the colours ---

def _css() -> str:
    return (WEB / "static" / "style.css").read_text(encoding="utf-8")


def _variables(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """The variables of the light :root and of the dark one (the dark one on top of the light one)."""
    light = re.search(r":root\s*\{([^{}]*)\}", css)[1]
    dark = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{([^{}]*)\}\s*\}", css)[1]

    def read(block_text: str) -> dict[str, str]:
        block_text = re.sub(r"/\*.*?\*/", "", block_text, flags=re.S)
        return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block_text))
    return read(light), {**read(light), **read(dark)}


def _rule(css: str, selector: str) -> dict[str, str]:
    """The declarations of the rule whose selector list names `selector` (without a comment, as a dict)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in [s.strip() for s in selectors.split(",")]:
            return {k.strip(): v.strip() for k, v in re.findall(r"([\w-]+)\s*:\s*([^;]+);?", body)}
    raise AssertionError(selector)


def _luminance(colour: str) -> float:
    channels = [int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _pair(rule: dict[str, str], variables: dict[str, str]) -> tuple[str, str, str]:
    """(text colour, background it is meant for, the page colour behind a transparent background)."""
    text = variables[re.fullmatch(r"var\((--[\w-]+)\)", rule["color"])[1]]
    background = rule.get("background", "transparent")
    if background != "transparent":
        background = variables[re.fullmatch(r"var\((--[\w-]+)\)", background)[1]]
    return text, background, variables["--bg"]


@pytest.mark.parametrize("kind", ["1", "2", "3", "none"])
def test_every_tier_colour_reaches_4_5_to_1_in_light_and_dark(kind):
    css = _css()
    for variables in _variables(css):
        text, background, page = _pair(_rule(css, f".tier-{kind}"), variables)
        for behind in {page, variables["--soft"]} if background == "transparent" else {background}:
            assert contrast(text, behind) >= 4.5, (kind, text, behind)


def test_tier_3_is_neutral_not_red_and_tier_none_is_an_outline():
    css = _css()
    assert _rule(css, ".tier-3")["color"] == "var(--tag-neutral)"
    assert _rule(css, ".tier-1")["color"] == "var(--tag-ok)" and _rule(css, ".tier-2")["color"] == "var(--tag-warn)"
    assert _rule(css, ".status-gap")["color"] == "var(--tag-bad)"   # a gap of one requirement stays red
    assert "--tag-bad" not in " ".join(_rule(css, ".tier-3").values())
    none = _rule(css, ".tier-none")
    assert none["background"] == "transparent" and none["border-style"] == "dashed"
    light, dark = _variables(css)
    for variables in (light, dark):
        assert {"--tag-neutral", "--tag-neutral-bg"} <= set(variables)


def test_no_tier_rule_uses_a_colour_literal():
    css = _css()
    for kind in ("1", "2", "3", "none"):
        declarations = " ".join(_rule(css, f".tier-{kind}").values())
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(", declarations)
