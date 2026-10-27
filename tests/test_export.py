"""The Markdown export of an analysis (to talk it over with someone else): the document, the download
of the web app and `skillpose export`."""

import os
import re
import subprocess
import sys
from datetime import date

import pytest
from demo_fixture import NAME as DEMO_NAME
from demo_fixture import write_fixture_bundles
from fakes import CV, PathDraft, PathStepDraft, fail_if_called
from test_web_jobs import NOT_COUNTED_ROWS, seed_match
from test_web_market import seed_family
from test_web_tiers import BADGES, with_rules
from webkit import make_client

from skillpose import logs
from skillpose.cli import main
from skillpose.core.config import load_settings
from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    Evidence,
    SkillProposal,
    Station,
)
from skillpose.core.paths import path_from_draft
from skillpose.export import ExportError, build_export, export_filename
from skillpose.pipeline import market
from skillpose.store.db import Store
from skillpose.web.i18n import LANGS, load_texts, translate

DE = {"accept-language": "de"}
TODAY = date(2026, 10, 24)
CV_NAME = 'Müller "CV"/2026.pdf'
MARKER = "CV-MARKER-4711-Mustermann"
TRICKY = f"Built Selenium | Java {MARKER}\nsecond line, <b>bold</b>, a \\ backslash and a `tick`"
LONG = "Led the migration of 300 regression tests from Selenium RC to WebDriver. " * 3   # 216 characters
SETTINGS = load_settings()


def evidence(quote, strength="outcome", verified=True):
    return Evidence(quote=quote, context="Acme", period_from=2016, period_to=None, strength=strength,
                    verified=verified)


def skill(skill_id, level, years, last_used, *evidence_list, hidden=False):
    return CandidateSkill(skill_id=skill_id, level=level, years=years, last_used=last_used,
                          evidence=list(evidence_list), hidden=hidden)


def profile(language="de"):
    skills = [
        skill("selenium", "expert", 10, 2026, evidence("A claim about Selenium", "claim"), evidence(TRICKY, "outcome")),
        skill("java", "strong", 2.5, 2024, evidence(LONG, "activity")),
        skill("docker", "basic", None, None),
        skill("sql", "working", 4, 2025, evidence("Wrote SQL reports", "activity"), hidden=True),
        skill("dax", "working", 1, 2025, evidence("Built DAX measures", "activity", verified=False)),
    ]
    stations = [Station(title="QA Engineer", employer="Acme", period_from=2016, period_to=None),
                Station(title="Tester", employer="Beta", period_from=2012, period_to=2016)]
    return CandidateProfile(language=language, skills=skills, stations=stations,
                            proposed_skills=[SkillProposal(id="dax", name="DAX (Daten)", category="tool")])


def seed(tmp_path, *, name=CV_NAME, paths=()):
    """CV 1 with a profile, 10 postings in 3 role families and, as asked, paths in the given languages."""
    db = tmp_path / "web.sqlite"
    with Store(db) as st:
        cv_id = st.save_cv(name, CV + " " + MARKER)
        st.save_profile(cv_id, profile(), "fake")
        seed_family(st, cv_id, "test_automation", {"regression_testing": 6.0, "api_testing": 2.0}, tier=2,
                    must_coverage=0.25)
        seed_family(st, cv_id, "qa_management", {"api_testing": 2.0}, n=2, tier=3, must_coverage=0.5)
        met = ("java", "must", "met", None, "evidenced")
        seed_match(st, cv_id, "Posting of tier 1", 1, 0.9, [met], family="software_development")
        seed_match(st, cv_id, "Posting of tier 2", 2, 0.7,
                   [met, ("kubernetes", "must", "gap", "months", "not in the CV")], family="software_development")
        seed_match(st, cv_id, "Posting of tier 3", 3, 0.4,
                   [met, ("docker", "must", "partial", "weeks", "only listed, no evidence of use"),
                    ("kubernetes", "must", "gap", "months", "not in the CV"),
                    ("sql", "nice", "gap", "weeks", "not in the CV")], family="software_development")
        report = next(r for r in market(st, SETTINGS, cv_id) if r.role_family == "test_automation")
        for lang in paths:
            draft = PathDraft(rationale=f"Close the {lang} gap first.", steps=[
                PathStepDraft(activity=f"Build a test | grid ({lang})", closes=["regression_testing"],
                              effort_weeks=3, proof="Public repository"),
                PathStepDraft(activity="", closes=["api_testing"], effort_weeks=1, proof="")])
            st.save_path(cv_id, path_from_draft(draft, report, "hash", lang), "fake")
    return db, cv_id


def export(tmp_path, lang="de", *, ids=(1,), names=None, today=TODAY, settings=SETTINGS, **seed_args):
    """The export of CV 1 of a seeded database (seeded once per folder)."""
    db = tmp_path / "web.sqlite"
    if not db.exists():
        seed(tmp_path, **seed_args)
    with Store(db) as st:
        return build_export(st, settings, list(ids), lang, today=today, names=names)


def headings(text, level):
    return re.findall(rf"^{'#' * level} (.*)$", text, re.M)


def table(text, first_header):
    """The rows of the table whose header row starts with first_header, as lists of cells (the escaped text)."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(f"| {first_header} |"))
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append(re.split(r"(?<!\\)\|", line)[1:-1])
    return [[cell.strip() for cell in row] for row in rows]


# --- the document ---

@pytest.mark.parametrize("lang", LANGS)
def test_the_document_has_title_privacy_line_legend_and_the_sections(tmp_path, lang):
    text = export(tmp_path, lang)
    texts = load_texts()[lang]
    word = "Export" if lang == "de" else "export"
    assert text.startswith(f'# skillpose – Müller "CV"/2026.pdf – {word} 2026-10-24\n')
    assert texts["export.privacy"] in text and texts["ai.generated"] in text
    assert headings(text, 1) == [text.splitlines()[0][2:]]
    assert headings(text, 2) == [texts["export.tiers_title"], texts["export.overview_title"],
                                 texts["export.skills_title"], texts["nav.market"], texts["nav.matches"]]  # no path
    assert text.endswith("\n") and not text.endswith("\n\n") and "\r" not in text


@pytest.mark.parametrize("lang", LANGS)
def test_the_legend_uses_the_tier_words_and_the_help_texts_of_the_pages(tmp_path, lang):
    text = export(tmp_path, lang)
    texts = load_texts()[lang]
    legend = text[text.index(f"## {texts['export.tiers_title']}"):text.index(f"## {texts['export.overview_title']}")]
    assert texts["tier.help_best"] in legend
    for tier in (1, 2, 3, None):
        assert f"**{BADGES[lang][tier]}**" in legend
    for key in ("help_1", "help_2", "help_3"):
        assert texts[f"tier.{key}"].format(tier1_pct=80, weeks=26, share=40) in legend, key
    assert translate(load_texts(), lang, "tier.help_role", share=40, count=1) in legend  # the default: 1 posting


def test_the_legend_follows_the_settings_like_the_help_box(tmp_path):
    settings = with_rules(tier1_coverage=0.7, tier2_max_weeks=12, role_tier_share=0.6, min_jobs_per_role=3)
    text = export(tmp_path, "en", settings=settings)
    for expected in ("at least 70 %", "at most 12 weeks", "more than 12 weeks", "at least 60 %", "at least 3 postings"):
        assert expected in text
    assert "80 %" not in text and "26 weeks" not in text


def test_overview_numbers(tmp_path):
    text = export(tmp_path, "de")
    block = text[text.index("## Überblick"):text.index("## Kompetenzen")]
    lines = [line for line in block.splitlines() if line.startswith("- ")]
    assert re.fullmatch(r"- Hochgeladen: \d{4}-\d\d-\d\d \d\d:\d\d UTC", lines[0])
    assert lines[1] == f"- Text: {len(CV + ' ' + MARKER)} Zeichen"
    assert lines[2] == "- Profil: 5 Kompetenzen (1 ausgeblendet), 2 Stationen"
    assert lines[3] == ("- Niveaus der Kompetenzen: Expertenwissen 1, Vertieftes Wissen 1, Praxiswissen 2, "
                        "Grundkenntnisse 1")
    en = export(tmp_path, "en")
    assert "- Profile: 5 skills (1 hidden), 2 positions" in en
    assert "- Levels of the skills: Expert 1, Strong 1, Working 2, Basic 1" in en


def test_a_profile_without_hidden_skills_names_no_hidden_ones(tmp_path):
    db, cv_id = seed(tmp_path)
    with Store(db) as st:
        st.edit_skill(cv_id, "sql", hidden=False)
        text = build_export(st, SETTINGS, [cv_id], "de", today=TODAY)
    assert "- Profil: 5 Kompetenzen, 2 Stationen" in text and "ausgeblendet" not in text


# --- the skills table ---

def test_skills_table_columns_order_and_values(tmp_path):
    rows = table(export(tmp_path, "de"), "Kompetenz")
    assert rows[0] == ["Kompetenz", "Niveau", "Jahre", "Zuletzt genutzt", "Belege", "Stärkster Beleg"]
    assert set(rows[1]) == {"---"} and len(rows[1]) == 6
    body = rows[2:]   # by level, then years; a hidden skill last
    assert [r[0] for r in body] == ["Selenium", "Java", "DAX (Daten)", "Docker", "SQL (ausgeblendet)"]
    selenium, java, dax, docker, sql = body
    assert selenium[1:5] == ["Expertenwissen", "10", "2026", "2"]
    assert java[1:5] == ["Vertieftes Wissen", "2,5", "2024", "1"]          # a comma in German
    assert dax[1:5] == ["Praxiswissen", "1", "2025", "1"]                    # a name that only Claude proposed
    assert docker[1:5] == ["Grundkenntnisse", "–", "–", "0"] and docker[5] == "–"
    assert sql[1:5] == ["Praxiswissen", "4", "2025", "1"]
    english = table(export(tmp_path, "en"), "Skill")
    assert english[0] == ["Skill", "Level", "Years", "Last used", "Evidence", "Strongest quote"]
    assert [r[2] for r in english[2:]] == ["10", "2.5", "1", "–", "4"] and english[2][1] == "Expert"


def test_the_strongest_quote_is_the_outcome_and_a_long_one_is_cut_at_110_characters(tmp_path):
    body = table(export(tmp_path, "de"), "Kompetenz")[2:]
    assert body[0][5].startswith("Built Selenium \\| Java ") and "A claim" not in body[0][5]
    cut = body[1][5]
    assert cut == LONG[:110].rstrip() + "…" and len(cut) <= 111
    assert body[2][5] == "Built DAX measures (nicht im Text gefunden)"   # a quote the program did not find in the CV
    assert "nicht im Text gefunden" not in body[0][5] + body[1][5]


def test_a_cell_never_breaks_the_table(tmp_path):
    text = export(tmp_path, "de")
    selenium = next(line for line in text.splitlines() if line.startswith("| Selenium |"))
    assert "Built Selenium \\| Java" in selenium and "\\<b>bold\\</b>" in selenium and "a \\\\ backslash" in selenium
    assert "\\`tick\\`" in selenium and "Java " + MARKER + " second line" in selenium   # the line break is a space
    for first_header in ("Kompetenz", "Rollenfamilie", "Stufe"):
        rows = table(text, first_header)
        assert len({len(row) for row in rows}) == 1 and len(rows) > 2, first_header


def test_html_in_a_name_a_posting_or_a_path_is_escaped(tmp_path):
    db, cv_id = seed(tmp_path, name="<script>x</script> | y.pdf", paths=("de",))
    with Store(db) as st:
        seed_match(st, cv_id, "<img src=x> | Posting", 1, 0.9, [("java", "must", "met", None, "evidenced")])
        path = st.paths(cv_id)["test_automation"]
        st.save_path(cv_id, path.model_copy(update={"rationale": "<b>bold</b> | rationale"}), "fake")
        text = build_export(st, SETTINGS, [cv_id], "de", today=TODAY)
    assert text.startswith("# skillpose – \\<script>x\\</script> \\| y.pdf – Export")
    assert "\\<img src=x> \\| Posting.md" in text and "\\<b>bold\\</b> \\| rationale" in text
    assert not re.search(r"(?<!\\)<", text)   # no tag opens anywhere


# --- the market view and the matches ---

@pytest.mark.parametrize("lang", LANGS)
def test_market_table_has_tier_word_postings_median_and_top_gaps(tmp_path, lang):
    learnable, none = BADGES[lang][2], BADGES[lang][None]
    text = export(tmp_path, lang, settings=with_rules(min_jobs_per_role=5))  # the roles with 3 and 2 postings: no tier
    rows = table(text, "Rollenfamilie" if lang == "de" else "Role family")
    assert [r[0] for r in rows[2:]] == (["Testautomatisierung", "Softwareentwicklung", "QA-Management"]
                                        if lang == "de" else ["Test automation", "Software development",
                                                              "QA management"])
    automation, software, qa = rows[2:]
    gaps = "Regressionstests (3), API-Tests (1)" if lang == "de" else "Regression testing (3), API testing (1)"
    assert automation[1:] == [learnable, "5", "25 %", gaps]
    assert software[1:3] == [none, "3"] and software[3] == "70 %"
    assert qa[1:] == [none, "2", "50 %", "API-Tests (1)" if lang == "de" else "API testing (1)"]


def test_matches_are_sorted_by_tier_then_coverage_and_name_the_open_must_requirements(tmp_path):
    text = export(tmp_path, "de")
    rows = table(text, "Stufe")
    assert rows[0] == ["Stufe", "Muss", "Inserat", "Rollenfamilie", "Offene Muss-Anforderungen",
                       "Nicht aus dem Lebenslauf prüfbar"]
    assert table(text, "Rollenfamilie")[0] == ["Rollenfamilie", "Stufe", "Inserate", "Median der Muss-Abdeckung",
                                               "Häufigste Lücken (Inserate)"]
    body = rows[2:]
    assert [r[2] for r in body] == [
        "Posting of tier 1.md", "Posting of tier 2.md", "test_automation_0.md", "test_automation_1.md",
        "test_automation_2.md", "test_automation_3.md", "test_automation_4.md", "qa_management_0.md",
        "qa_management_1.md", "Posting of tier 3.md"]
    by_ref = {r[2]: r for r in body}
    assert by_ref["Posting of tier 1.md"][:2] == ["1 · passt", "90 %"] and by_ref["Posting of tier 1.md"][4] == "–"
    assert by_ref["Posting of tier 2.md"][:4] == ["2 · lernbar", "70 %", "Posting of tier 2.md", "Softwareentwicklung"]
    assert by_ref["Posting of tier 2.md"][4] == "Kubernetes (Lücke, Monate)"
    # a gap before a partial one, the nice requirement (SQL) is not listed
    assert by_ref["Posting of tier 3.md"][4] == "Kubernetes (Lücke, Monate), Docker (teilweise, Wochen)"
    assert by_ref["test_automation_0.md"][4] == "Regressionstests (Lücke, Wochen), API-Tests (Lücke, Wochen)"
    assert by_ref["test_automation_4.md"][4] == "–"


def test_the_open_must_requirements_leave_out_soft_skills_and_unanswered_conditions(tmp_path):
    db, cv_id = seed(tmp_path)
    with Store(db) as st:
        seed_match(st, cv_id, "Soft and conditions", 2, 0.5, NOT_COUNTED_ROWS, family="software_development")
        text = build_export(st, SETTINGS, [cv_id], "de", today=TODAY)
    [soft] = [r for r in table(text, "Stufe")[2:] if r[2] == "Soft and conditions.md"]
    assert soft[4] == "Kubernetes (Lücke, Monate), Docker (teilweise, Wochen)"


SOFT_ROWS = [
    ("java", "must", "met", None, "evidenced"),
    ("kubernetes", "must", "gap", "months", "not in the CV"),
    ("teamwork", "must", "unverifiable", None, "soft skill: not verifiable from a CV"),
    ("communication", "must", "unverifiable", None, "soft skill: not verifiable from a CV"),
    ("structured_working", "nice", "unverifiable", None, "soft skill: not verifiable from a CV"),
    ("driving_licence_b", "must", "gap", None, "profile: no driving licence B"),
    ("shift_work", "must", "unanswered", None, "not answered in the profile"),
    ("travel_willingness", "nice", "gap", None, "profile: travel up to 0 %, posting asks 30 %")]
SOFT_FIELDS = {
    **{skill_id: {"kind": "soft"} for skill_id in ("teamwork", "communication", "structured_working")},
    "driving_licence_b": {"kind": "condition", "condition": "driving_licence_b"},
    "shift_work": {"kind": "condition", "condition": "shift_work"},
    "travel_willingness": {"kind": "condition", "condition": "travel", "importance": "nice"}}


def soft_export(tmp_path, lang, title="Soft and conditions"):
    db, cv_id = seed(tmp_path)
    with Store(db) as st:
        seed_match(st, cv_id, title, 3, 0.5, SOFT_ROWS, family="software_development",
                   requirement_fields=SOFT_FIELDS)
        return build_export(st, SETTINGS, [cv_id], lang, today=TODAY)


def test_the_export_counts_the_soft_skills_apart_and_lists_none_of_them(tmp_path):
    """Spec test 5: a match with three soft skills has "not verifiable" 3, and no soft skill stands in the open must
    requirements or in the most frequent gaps."""
    de = soft_export(tmp_path, "de")
    rows = table(de, "Stufe")
    [soft] = [r for r in rows[2:] if r[2] == "Soft and conditions.md"]
    assert soft[5] == "3" and soft[4] == "Kubernetes (Lücke, Monate)"  # soft skills and conditions are not open
    assert {r[5] for r in rows[2:] if r is not soft} == {"0"}
    for name in ("Teamfähigkeit", "Kommunikationsfähigkeit", "structured_working"):
        assert name not in de, name
    gaps = [r[4] for r in table(de, "Rollenfamilie")[2:]]
    assert any("Kubernetes" in cell for cell in gaps) and not any("Führerschein" in cell for cell in gaps)
    en = soft_export(tmp_path, "en")
    assert table(en, "Tier")[0][5] == "Not verifiable from the CV"
    assert [r[5] for r in table(en, "Tier")[2:] if r[2] == "Soft and conditions.md"] == ["3"]


def test_the_export_has_one_line_for_each_must_condition_that_blocks(tmp_path):
    de, en = soft_export(tmp_path, "de"), soft_export(tmp_path, "en")
    title_de = "Blockiert durch Bedingungen (Muss-Bedingungen, die die Antworten im Profil nicht erfüllen):"
    assert title_de in de.split("\n")
    assert [line for line in de.split("\n") if line.startswith("- Soft and conditions.md:")] == [
        "- Soft and conditions.md: Führerschein B (Profil: kein Führerschein B)"]  # not the unanswered or the nice one
    title_en = "Blocked by conditions (must conditions that the answers in the profile do not meet):"
    assert title_en in en.split("\n")
    assert [line for line in en.split("\n") if line.startswith("- Soft and conditions.md:")] == [
        "- Soft and conditions.md: Driving licence class B (profile: no driving licence B)"]
    lines = de.split("\n")
    at = lines.index(title_de)
    assert lines[at - 1] == "" and lines[at + 1] == "" and lines[at - 2].startswith("| ")  # after the table


def test_a_condition_line_is_one_line_of_text(tmp_path):
    text = soft_export(tmp_path, "de", title="Bad | <b>x</b>\nsecond line")
    [line] = [line for line in text.split("\n") if line.startswith("- Bad")]
    assert line == r"- Bad \| \<b>x\</b> second line.md: Führerschein B (Profil: kein Führerschein B)"


def test_an_export_without_a_blocking_condition_has_no_such_list(tmp_path):
    text = export(tmp_path, "de")
    assert "Blockiert durch Bedingungen" not in text
    assert {r[5] for r in table(text, "Stufe")[2:]} == {"0"}


def test_matches_sort_key_is_tier_then_coverage_then_name(tmp_path):
    body = table(export(tmp_path, "en"), "Tier")[2:]
    keys = [(int(r[0][0]) if r[0][0].isdigit() else 9, -int(r[1].rstrip(" %")), r[2].casefold()) for r in body]
    assert keys == sorted(keys) and len(body) == 10


def test_the_export_says_so_when_there_is_nothing_to_compare(tmp_path):
    db = tmp_path / "web.sqlite"
    with Store(db) as st:
        cv_id = st.save_cv("a.md", CV)
        st.save_profile(cv_id, CandidateProfile(language="en", skills=[], stations=[]), "fake")
        de = build_export(st, SETTINGS, [cv_id], "de", today=TODAY)
        en = build_export(st, SETTINGS, [cv_id], "en", today=TODAY)
    assert "Das Profil enthält keine Kompetenzen." in de and "Noch kein Abgleich." in de
    assert "The profile has no skills." in en and "No matches yet." in en
    assert "| Rollenfamilie" not in de and "Entwicklungspfade" not in de


# --- the paths ---

def test_a_path_in_the_language_of_the_export_comes_without_a_note(tmp_path):
    text = export(tmp_path, "en", paths=("de", "en"))
    section = text[text.index("## Development paths"):]
    assert "Close the en gap first." in section and "Close the de gap first." not in section
    assert "only in" not in section.lower()
    assert "Test automation – Total: 4 weeks" in section
    assert ("1. Build a test \\| grid (en) – 3 weeks; Closes: Regression testing; Proof: Public repository"
            in section)
    assert "\n2. 1 week; Closes: API testing\n" in section   # no activity and no proof: nothing empty


@pytest.mark.parametrize("start", ["~~~ python", "``` python", "# Heading", "> quote", "- item", "1. item", "---",
                                   "[ref]: https://example.org", "* item", "+ item", "___"])
def test_free_text_that_starts_a_line_cannot_change_the_structure(tmp_path, start):
    # the rationale is a quote, and a leading mark of a heading, a list, a rule, a fence or a link
    # definition stays text; the activity of a step starts a list item and is guarded the same way
    db, cv_id = seed(tmp_path, paths=("en",))
    with Store(db) as st:
        path = st.paths(cv_id)["test_automation"]
        steps = [path.steps[0].model_copy(update={"activity": f"{start} step"}), *path.steps[1:]]
        st.save_path(cv_id, path.model_copy(update={"rationale": f"{start} why\nprint('x')", "steps": steps}), "fake")
        text = build_export(st, SETTINGS, [cv_id], "en", today=TODAY)
    section = text[text.index("## Development paths"):]
    escaped = re.sub(r"^(\d*)(\S)", r"\1\\\2", start) if not start.startswith("`") else start.replace("`", "\\`")
    assert f"\n> {escaped} why print('x')\n" in section
    assert f"\n1. {escaped} step – 3 weeks;" in section
    assert headings(text, 1) == [text.splitlines()[0][2:]]
    # no line opens a fence, a rule or a link definition, and no quote or list item opens a block of its own
    assert not re.search(r"^(~~~|```|---|___|\* |\+ |\[ref\])", text, re.M)
    assert not re.search(r"^(> |\d+\. )([#>+*=~\[_-]|\d+[.)]|```)", text, re.M)


def test_a_path_only_in_another_language_is_shown_with_a_note(tmp_path):
    text = export(tmp_path, "de", paths=("en",))
    section = text[text.index("## Entwicklungspfade"):]
    assert "Dieser Pfad liegt nur auf Englisch vor." in section and "Close the en gap first." in section
    assert "Testautomatisierung – Gesamt: 4 Wochen" in section
    (tmp_path / "x").mkdir()
    assert "This path exists only in German." in export(tmp_path / "x", "en", paths=("de",))


# --- several CVs ---

def two_cvs(tmp_path):
    db, _ = seed(tmp_path)
    with Store(db) as st:
        second = st.save_cv("zweiter.md", CV + " second")
        other = profile()
        other.skills = [skill("selenium", "working", 3, 2020, evidence("Used Selenium")),
                        skill("java", "strong", 2, 2024, evidence("Java work")),
                        skill("python", "basic", 1, 2023, evidence("Some Python")),
                        skill("sql", "basic", 1, 2023, evidence("SQL"), hidden=True)]
        other.proposed_skills = []
        st.save_profile(second, other, "fake")
        return build_export(st, SETTINGS, [1, second], "de", today=TODAY), second


def test_several_cvs_share_one_document_with_a_comparison(tmp_path):
    text, second = two_cvs(tmp_path)
    assert second == 2
    assert headings(text, 1) == ["skillpose – Export 2026-10-24"]
    assert headings(text, 2) == ["Stufen", 'Lebenslauf 1: Müller "CV"/2026.pdf', "Lebenslauf 2: zweiter.md",
                                 "Vergleich der Lebensläufe"]
    assert text.count("Privat: Diese Datei enthält") == 1 and text.count("Die Stufen heißen") == 1
    assert headings(text, 3)[:5] == ["Überblick", "Kompetenzen", "Marktsicht", "Abgleich", "Überblick"]
    compare = text[text.index("## Vergleich der Lebensläufe"):]
    # SQL is hidden in both CVs (Praxiswissen and Grundkenntnisse): a hidden skill does not count, it is not in the
    # matches either
    assert compare.splitlines()[2:5] == [
        '- Nur in Lebenslauf 1 (Müller "CV"/2026.pdf): DAX (Daten), Docker',
        "- Nur in Lebenslauf 2 (zweiter.md): Python",
        "- Verschiedene Niveaus: Selenium (Lebenslauf 1: Expertenwissen; Lebenslauf 2: Praxiswissen)"]


def test_one_cv_has_no_comparison(tmp_path):
    assert "Vergleich" not in export(tmp_path, "de")


def test_a_cv_without_a_profile_or_an_unknown_one_is_an_error(tmp_path):
    db, _ = seed(tmp_path)
    with Store(db) as st:
        bare = st.save_cv("bare.md", CV + " bare")
        for cv_id, reason in ((bare, "no_profile"), (99, "unknown")):
            with pytest.raises(ExportError) as caught:
                build_export(st, SETTINGS, [1, cv_id], "de", today=TODAY)
            assert caught.value.cv_id == cv_id and caught.value.reason == reason


def test_display_names_replace_the_stored_ones(tmp_path):
    text = export(tmp_path, "de", names={1: "Maria Berger"})
    assert text.startswith("# skillpose – Maria Berger – Export 2026-10-24")


# --- the file name ---

@pytest.mark.parametrize("name, expected", [
    ('Müller "CV"/2026.pdf', "skillpose-Mueller-CV-2026-2026-10-24.md"),
    ("Hans_Lebenslauf.docx", "skillpose-Hans_Lebenslauf-2026-10-24.md"),
    ("Maria Berger", "skillpose-Maria-Berger-2026-10-24.md"),
    ("../../etc/passwd", "skillpose-etc-passwd-2026-10-24.md"),
    ("dir\\sub\\cv.txt", "skillpose-dir-sub-cv-2026-10-24.md"),
    ('a"; filename="evil.exe', "skillpose-a-filename-evil-exe-2026-10-24.md"),
    ("Straße ÄÖÜ ß.md", "skillpose-Strasse-AeOeUe-ss-2026-10-24.md"),
    ("日本語.md", "skillpose-cv-2026-10-24.md"),
    ("", "skillpose-cv-2026-10-24.md"),
    ("x" * 300 + ".txt", "skillpose-" + "x" * 60 + "-2026-10-24.md"),
    ("con\r\nx-injected: 1", "skillpose-con-x-injected-1-2026-10-24.md"),
])
def test_the_file_name_is_ascii_and_has_no_path_parts(name, expected):
    assert export_filename(name, TODAY) == expected
    assert re.fullmatch(r"skillpose-[A-Za-z0-9_-]+-2026-10-24\.md", export_filename(name, TODAY))


# --- the web app ---

def get_export(c, cv_id=1, query="", **kwargs):
    return c.get(f"/cv/{cv_id}/export.md{query}", **kwargs)


def test_the_download_has_the_headers_of_a_markdown_attachment(tmp_path):
    seed(tmp_path)
    c = make_client(tmp_path)
    before = date.today()
    r = get_export(c, query="?lang=de")
    after = date.today()
    assert r.status_code == 200 and r.headers["content-type"] == "text/markdown; charset=utf-8"
    disposition = r.headers["content-disposition"]
    assert re.fullmatch(r'attachment; filename="skillpose-Mueller-CV-2026-(\d{4}-\d\d-\d\d)\.md"', disposition)
    assert re.search(r"(\d{4}-\d\d-\d\d)\.md", disposition)[1] in {before.isoformat(), after.isoformat()}
    assert disposition.isascii() and r.headers["x-content-type-options"] == "nosniff"
    assert r.content.decode("utf-8").startswith('# skillpose – Müller "CV"/2026.pdf – Export ')


@pytest.mark.parametrize("query, headers, title", [
    ("?lang=en", DE, "export"), ("?lang=de", {}, "Export"),
    ("", DE, "Export"), ("", {"accept-language": "en"}, "export")])
def test_the_language_follows_the_query_then_the_browser(tmp_path, query, headers, title):
    seed(tmp_path)
    page = get_export(make_client(tmp_path), query=query, headers=headers).text
    assert f" – {title} 20" in page.splitlines()[0]


def test_the_document_of_the_web_is_the_document_of_the_function(tmp_path):
    db, _ = seed(tmp_path)
    c = make_client(tmp_path)
    body = get_export(c, query="?lang=en").content.decode("utf-8")
    with Store(db) as st:
        assert body == build_export(st, SETTINGS, [1], "en", today=date.today())


def test_an_unknown_cv_and_a_cv_without_a_profile_answer_404_with_a_page(tmp_path):
    db, _ = seed(tmp_path)
    with Store(db) as st:
        bare = st.save_cv("bare.md", CV + " bare")
    c = make_client(tmp_path)
    unknown = get_export(c, 99, "?lang=en")
    assert unknown.status_code == 404 and unknown.headers["content-type"].startswith("text/html")
    assert "This page does not exist." in unknown.text
    for lang, message in (("en", "This CV has not been analyzed yet. There is nothing to export."),
                          ("de", "Dieser Lebenslauf ist noch nicht analysiert. Es gibt nichts zu exportieren.")):
        r = get_export(c, bare, f"?lang={lang}")
        assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
        assert message in r.text and "content-disposition" not in r.headers
    assert get_export(c, "abc").status_code == 400   # not a number: the invalid-request page


def test_the_demo_exports_without_a_key_and_names_the_person(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    r = get_export(c, query="?lang=en")
    assert r.status_code == 200 and f'filename="skillpose-{DEMO_NAME.replace(" ", "-")}-' in r.headers[
        "content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith(f"# skillpose – {DEMO_NAME} – export ") and "Selenium" in text
    assert "Kubernetes" in text and "Tier" in text
    assert "Uploaded" not in text and "Text: " in text   # the demo store is made at start: no real upload time
    assert c.app.state.skillpose.tasks.current is None
    (tmp_path / "own").mkdir()
    seed(tmp_path / "own")
    assert "Uploaded: " in get_export(make_client(tmp_path / "own"), query="?lang=en").text   # the own data have it


def test_with_a_key_the_export_serves_only_the_own_database(tmp_path):
    # a demo folder next to the key changes nothing; CV 1 of the demo is not reachable
    write_fixture_bundles(tmp_path / "demo")
    c = make_client(tmp_path, key=True, demo_dir=tmp_path / "demo")
    r = get_export(c, query="?lang=en")
    assert r.status_code == 404 and "content-disposition" not in r.headers and DEMO_NAME not in r.text
    seed(tmp_path)
    own = get_export(make_client(tmp_path, key=True, demo_dir=tmp_path / "demo"), query="?lang=en")
    assert own.status_code == 200 and own.content.decode("utf-8").startswith('# skillpose – Müller "CV"/2026.pdf')
    assert DEMO_NAME not in own.content.decode("utf-8")


def test_the_export_changes_nothing_and_starts_no_task(tmp_path):
    db, _ = seed(tmp_path)
    c = make_client(tmp_path)
    before = (db.read_bytes(), c.app.state.skillpose.tasks.current)
    for query in ("", "?lang=de", "?lang=en"):
        assert get_export(c, query=query).status_code == 200
    assert (db.read_bytes(), c.app.state.skillpose.tasks.current) == before


def test_the_password_and_the_host_check_apply(tmp_path):
    seed(tmp_path)
    c = make_client(tmp_path, password="secret")
    assert get_export(c).status_code == 401
    assert get_export(c, auth=("any", "secret")).status_code == 200
    assert get_export(c, auth=("any", "wrong")).status_code == 401
    other = make_client(tmp_path)
    assert get_export(other, headers={"host": "evil.example"}).status_code == 400


@pytest.mark.parametrize("lang, label, hint", [
    ("de", "Als Markdown exportieren", "Die Datei enthält Inhalte deines Lebenslaufs."),
    ("en", "Export as Markdown", "The file contains content of your CV.")])
def test_the_profile_page_offers_the_export(tmp_path, lang, label, hint):
    seed(tmp_path)
    page = make_client(tmp_path).get("/cv/1", headers={"accept-language": lang}).text
    assert re.search(rf'<a class="button secondary" href="/cv/1/export\.md\?lang={lang}"[^>]*>{label}</a>', page)
    assert hint in page


def test_the_demo_profile_page_offers_the_export_and_a_cv_without_a_profile_does_not(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    demo = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    assert 'href="/cv/1/export.md?lang=en"' in demo.get("/cv/1", headers={"accept-language": "en"}).text
    (tmp_path / "own").mkdir()
    with Store(tmp_path / "own" / "web.sqlite") as st:
        st.save_cv("bare.md", CV)
    own = make_client(tmp_path / "own")
    assert "export.md" not in own.get("/cv/1").text


def test_the_log_names_the_cv_and_the_size_but_none_of_its_text(tmp_path):
    seed(tmp_path)
    c = make_client(tmp_path)
    log_file = tmp_path / "skillpose.log"
    with logs.file_log(log_file):
        body = get_export(c, query="?lang=de").content
        get_export(c, 99)
    text = log_file.read_text(encoding="utf-8")
    assert re.search(rf"exported CV 1 as Markdown: {len(body)} bytes", text)
    for secret in (MARKER, "Selenium", "Müller", "Posting of tier", "Built Selenium"):
        assert secret not in text, secret


# --- the command ---

@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def run(args, capsys, **kwargs):
    rc = main(["export", *args], client_factory=fail_if_called, **kwargs)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_export_writes_a_utf8_file_with_the_privacy_line(tmp_path, capsys):
    db, _ = seed(tmp_path)
    out = tmp_path / "sub" / "analysis.md"
    rc, stdout, err = run(["--cv", "1", "--db", str(db), "--lang", "en", "--out", str(out)], capsys)
    assert rc == 0 and err == ""
    data = out.read_bytes()
    assert b"\r" not in data
    text = data.decode("utf-8")
    assert text.startswith('# skillpose – Müller "CV"/2026.pdf – export ') and "Private: This file contains" in text
    assert str(out) in stdout and "private" in stdout.lower() and text not in stdout
    with Store(db) as st:
        assert text == build_export(st, SETTINGS, [1], "en", today=date.today())


def test_export_prints_to_stdout_as_utf8_without_a_file(tmp_path, capsys):
    db, _ = seed(tmp_path)
    rc, stdout, err = run(["--cv", "1", "--db", str(db)], capsys)   # German by default, like analyze
    assert rc == 0 and err == "" and stdout.startswith('# skillpose – Müller "CV"/2026.pdf – Export ')
    assert "1 · passt" in stdout and "große Lücke" in stdout and stdout.endswith("\n") and "\r" not in stdout
    assert sorted(p.name for p in tmp_path.iterdir()) == ["web.sqlite"]   # no file, not even a log


def test_export_without_cv_writes_every_analyzed_cv_and_the_comparison(tmp_path, capsys):
    two_cvs(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.save_cv("bare.md", CV + " bare")   # no profile: left out
    rc, stdout, _ = run(["--db", str(tmp_path / "web.sqlite"), "--lang", "en"], capsys)
    assert rc == 0
    assert headings(stdout, 2)[:3] == ["Tiers", 'CV 1: Müller "CV"/2026.pdf', "CV 2: zweiter.md"]
    assert headings(stdout, 2)[-1] == "Comparison of the CVs" and "bare.md" not in stdout
    assert "- Only in CV 2 (zweiter.md): Python" in stdout


def test_export_of_the_only_analyzed_cv_has_no_comparison(tmp_path, capsys):
    db, _ = seed(tmp_path)
    with Store(db) as st:
        st.save_cv("bare.md", CV + " bare")
    rc, stdout, _ = run(["--db", str(db)], capsys)
    assert rc == 0 and "Vergleich" not in stdout and stdout.startswith("# skillpose – Müller")


def test_export_exits_2_for_an_unknown_cv_a_cv_without_a_profile_and_no_database(tmp_path, capsys):
    db, _ = seed(tmp_path)
    with Store(db) as st:
        bare = st.save_cv("bare.md", CV + " bare")
    rc, stdout, err = run(["--cv", "99", "--db", str(db)], capsys)
    assert rc == 2 and stdout == "" and "99" in err
    rc, stdout, err = run(["--cv", str(bare), "--db", str(db), "--out", str(tmp_path / "never.md")], capsys)
    assert rc == 2 and stdout == "" and "not analyzed" in err and not (tmp_path / "never.md").exists()
    rc, stdout, err = run(["--db", str(tmp_path / "missing" / "none.sqlite")], capsys)
    assert rc == 2 and stdout == "" and "no database" in err
    assert not (tmp_path / "missing").exists()   # a read-only command creates nothing
    empty = tmp_path / "empty.sqlite"
    Store(empty).close()
    rc, _, err = run(["--db", str(empty)], capsys)
    assert rc == 2 and "no analyzed CV" in err


def test_export_needs_no_key_and_makes_no_call(tmp_path, capsys):
    db, _ = seed(tmp_path)
    assert "ANTHROPIC_API_KEY" not in os.environ   # the autouse fixture removed it
    rc, _, _ = run(["--cv", "1", "--db", str(db)], capsys)   # client_factory=fail_if_called would raise
    assert rc == 0


def test_the_export_is_importable_without_the_web_libraries():
    code = ("import sys; sys.modules['fastapi'] = None; sys.modules['jinja2'] = None; import skillpose.export, "
            "skillpose.cli; print('ok')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr
