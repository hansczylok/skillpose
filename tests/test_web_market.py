"""Web pages for the market view, the development paths and the costs."""

import re

import pytest
from demo_fixture import fixture_bundle, write_fixture_bundles
from fakes import CV, FailingClient, ScriptedClient, answer, current_fingerprint, prompt_folder, refusal
from webkit import make_client, task_running

from skillpose.core.config import load_settings
from skillpose.core.llm import Usage
from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    Evidence,
    JobMatch,
    JobProfile,
    PathDraft,
    PathStepDraft,
    Requirement,
    RequirementMatch,
)
from skillpose.core.paths import path_from_draft
from skillpose.core.prompts import use_prompts
from skillpose.pipeline import (
    analyze_postings,
    ensure_profile,
    estimate,
    ingest_postings,
    market,
    open_run,
    plan_postings,
    rematch,
)
from skillpose.store.db import CostRow, Store
from skillpose.web.deps import fmt_time
from skillpose.web.i18n import LANGS, load_texts

DE = {"accept-language": "de"}
ASK_ALWAYS = load_settings().model_copy(update={"confirm_above_usd": 0.0001})


def with_min(n):
    """The default settings with another minimum of postings per role family (the default is 1)."""
    settings = load_settings()
    return settings.model_copy(update={"rules": settings.rules.model_copy(update={"min_jobs_per_role": n})})


NO_ROOM = load_settings().model_copy(update={"monthly_limit_usd": 0.01})
QUOTE = "Built a Selenium test automation framework in Java"
EMPTY_ELEMENT = re.compile(r"<(p|span|strong|dd|dt|div|li|ol|ul)\b[^>]*>\s*</\1>")
QUEUED ="Queued – starts when the running task has finished."
QUEUED_DE = "Eingereiht – startet, wenn die laufende Aufgabe fertig ist."


def plain(html):
    """The text of some HTML, tags gone, white space collapsed."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def main_of(page):
    return page[page.index("<main>"):page.index("</main>")]


def block(page, family):
    """The HTML of the section of one role family."""
    found = re.search(rf'<section class="family" id="family-{family}">.*?</section>', page, re.S)
    assert found is not None, family
    return found.group(0)


def no_path_lines(page):
    """The lines of the list "roles without a path" of the paths page, as text, in the order shown."""
    [section] = re.findall(r'<section class="no-path".*?</section>', page, re.S)
    return [plain(li) for li in re.findall(r"<li>.*?</li>", section, re.S)]


def gap_widths(section):
    """{gap name: width in per cent} of the gap bars of one section."""
    items = re.findall(r'<li class="gap">\s*<strong>(.*?)</strong>.*?style="width: (\d+)%"', section, re.S)
    return {name: int(width) for name, width in items}


def call_rows(page):
    """The cells of the rows of the table of the last calls, as text."""
    [table] = re.findall(r'<table class="calls">.*?</table>', page, re.S)
    rows = re.findall(r"<tr>\s*<td.*?</tr>", table, re.S)
    return [[re.sub(r"<[^>]+>", "", cell).strip() for cell in re.findall(r"<td[ >].*?</td>", row, re.S)]
            for row in rows]


def means_section(page):
    """The section with the average cost per call."""
    [section] = re.findall(r'<section aria-labelledby="means-title">.*?</section>', page, re.S)
    return section


def mean_rows(page):
    """The cells of the rows of the table of the averages, as text."""
    [table] = re.findall(r'<table class="means">.*?</table>', means_section(page), re.S)
    return [[re.sub(r"<[^>]+>", "", cell).strip() for cell in re.findall(r"<td[ >].*?</td>", row, re.S)]
            for row in re.findall(r"<tr>\s*<td.*?</tr>", table, re.S)]


def cv_with_profile(tmp_path):
    """CV 1 with a profile of one evidenced skill (the matches are seeded by the tests)."""
    evidence = Evidence(quote=QUOTE, context="Acme", period_from=2016, period_to=None, strength="outcome",
                        verified=True)
    skills = [CandidateSkill(skill_id=s, level="expert", years=10, last_used=2026, evidence=[evidence])
              for s in ("selenium", "java")]
    with Store(tmp_path / "web.sqlite") as st:
        cv_id = st.save_cv("cv.md", CV)
        st.save_profile(cv_id, CandidateProfile(language="en", skills=skills, stations=[]), "fake")
    return cv_id


def seed_family(st, cv_id, family, weights=None, *, n=5, tier=2, must_coverage=0.25):
    """At least n postings of one role family with matches. weights: skill id -> the weight the skill gets in the
    market view (a missing must requirement weighs 2, a missing nice one 1, so 8.0 is four postings with a must gap)."""
    wanted = {}
    for skill, weight in (weights or {}).items():
        musts = int(weight // 2)
        wanted[skill] = (musts, int(weight - 2 * musts))
    n = max([n, *(musts + nices for musts, nices in wanted.values())])
    for i in range(n):
        text = f"{family} posting {i}\nposting text"
        rows = [("selenium", "must", "met", None, "evidenced")]
        for skill, (musts, nices) in wanted.items():
            if i < musts:
                rows.append((skill, "must", "gap", "weeks", "not in the CV"))
            elif i < musts + nices:
                rows.append((skill, "nice", "gap", "weeks", "not in the CV"))
        h = st.save_posting(text, f"{family}_{i}.md")
        requirements = [Requirement(skill_id=s, importance=imp, min_level=None, min_years=None, kind="tool",
                                    quote=f"need {s}") for s, imp, *_ in rows]
        st.save_job_profile(JobProfile(text_hash=h, title=f"{family} {i}", role_family=family, seniority="mid",
                                       requirements=requirements), "fake")
        matches = [RequirementMatch(requirement=r, status=status, reason=reason, evidence=None, gap_type=gap,
                                    effort_weeks=4 if gap else None)
                   for r, (_, _, status, gap, reason) in zip(requirements, rows, strict=True)]
        st.save_match(cv_id, JobMatch(text_hash=h, matches=matches, must_coverage=must_coverage,
                                      nice_coverage=0.5, tier=tier), current_fingerprint())


def market_page_with_gaps(tmp_path, weights, **kwargs):
    """The market page of CV 1 with five postings of test_automation whose top gaps weigh `weights`."""
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "test_automation", weights, **kwargs)
    return c.get(f"/cv/{cv_id}/market").text


def client_with_market(tmp_path, client, settings=None):
    """The web client with CV 1 (a profile made by step 1) and five analyzed and matched postings of
    test_automation, through the pipeline with the scripted API client. The only gap is Kubernetes (nice)."""
    with Store(tmp_path / "web.sqlite") as store:
        run = open_run(load_settings(), store, "en", client=client)
        cv_id = run.store.save_cv("cv.md", CV)
        ensure_profile(run, cv_id)
        items = [(f"job_{i}.md", f"Test Automation Engineer {i}\nSelenium and Java required.") for i in range(5)]
        hashes, _ = ingest_postings(run, items)
        analyze_postings(run, plan_postings(run, hashes, no_batch=True))
        rematch(run, cv_id, hashes)
    return make_client(tmp_path, client=client, settings=settings)


def demo_client(tmp_path, **kwargs):
    write_fixture_bundles(tmp_path / "demo")
    return make_client(tmp_path, demo_dir=tmp_path / "demo", **kwargs)


def path_calls(client):
    return sum(x.path == "PathDraft" for x in client.calls)


def stored_paths(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        return st.paths(1)


# --- market view ---

def test_market_shows_tiers_and_too_little_data(tmp_path):
    c = make_client(tmp_path, settings=with_min(5))
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "test_automation", {"regression_testing": 6.0}, tier=2, must_coverage=0.25)
        seed_family(st, cv_id, "qa_management", {"api_testing": 2.0}, tier=3, must_coverage=0.5)
        seed_family(st, cv_id, "data_analysis_bi", {"performance_testing": 2.0}, n=2)  # fewer than 5 postings
    en = c.get(f"/cv/{cv_id}/market").text
    tier2 = block(en, "test_automation")
    assert "5 postings" in plain(tier2) and 'class="mark tier-2"' in tier2 and "2 · learnable" in tier2
    assert "Test automation" in tier2 and "25 %" in plain(tier2) and 'style="width: 25%"' in tier2
    assert 'class="mark tier-3"' in block(en, "qa_management") and "50 %" in plain(block(en, "qa_management"))
    few = block(en, "data_analysis_bi")
    assert "– · too few postings" in plain(few) and "2 postings" in plain(few) and 'class="mark tier-none"' in few
    assert en.index("family-test_automation") < en.index("family-qa_management") < en.index("family-data_analysis_bi")
    de = c.get(f"/cv/{cv_id}/market", headers=DE).text
    assert "– · zu wenige Inserate" in de and "3 · große Lücke" in de and "Testautomatisierung" in de
    assert "Datenanalyse und BI" in de and "5 Inserate" in plain(de) and "Median" in de


def test_gap_bars_are_relative_to_the_largest_gap(tmp_path):
    html = market_page_with_gaps(tmp_path, {"regression_testing": 8.0, "api_testing": 4.0})
    assert 'style="width: 100%"' in html and 'style="width: 50%"' in html
    assert gap_widths(block(html, "test_automation")) == {"Regression testing": 100, "API testing": 50}


def test_gap_bars_are_whole_per_cent_in_the_order_of_the_weight(tmp_path):
    html = market_page_with_gaps(tmp_path, {"regression_testing": 6.0, "api_testing": 4.0, "tdd": 1.0})
    section = block(html, "test_automation")
    assert gap_widths(section) == {"Regression testing": 100, "API testing": 67, "Test-driven development": 17}
    assert list(gap_widths(section)) == ["Regression testing", "API testing", "Test-driven development"]


def test_gap_bars_are_relative_within_each_family(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "test_automation", {"regression_testing": 10.0})
        seed_family(st, cv_id, "qa_management", {"api_testing": 2.0})
    page = c.get(f"/cv/{cv_id}/market").text
    assert gap_widths(block(page, "test_automation")) == {"Regression testing": 100}
    assert gap_widths(block(page, "qa_management")) == {"API testing": 100}


def test_gaps_show_name_and_type_in_the_ui_language(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "test_automation", {"performance_testing": 6.0})
    en = plain(block(c.get(f"/cv/{cv_id}/market").text, "test_automation"))
    de = plain(block(c.get(f"/cv/{cv_id}/market", headers=DE).text, "test_automation"))
    assert "Performance testing" in en and "Gap type: weeks" in en and "Postings: 3" in en
    assert "Lasttests" in de and "Performance testing" not in de and "Art der Lücke: Wochen" in de
    assert "Inserate: 3" in de


def test_a_family_without_gaps_says_so(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "test_automation", {}, tier=1, must_coverage=1.0)
    page = c.get(f"/cv/{cv_id}/market").text
    assert "No gaps." in block(page, "test_automation") and 'class="gap"' not in page
    assert "Keine Lücken." in c.get(f"/cv/{cv_id}/market", headers=DE).text


def test_market_page_explains_the_tier_and_links_the_cv_pages(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "test_automation", {"api_testing": 2.0})
    page = c.get(f"/cv/{cv_id}/market").text
    assert "at least 40 % of its postings" in plain(page) and "at least 1 posting." in plain(page)
    page = make_client(tmp_path, settings=with_min(5)).get(f"/cv/{cv_id}/market").text
    assert "at least 5 postings." in plain(page)
    assert 'href="/cv/1/market" aria-current="page"' in page and 'href="/cv/1/paths"' in page


def test_market_page_without_profile_or_matches(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.save_cv("raw.md", CV)
    page = c.get("/cv/1/market").text
    assert "has not been analyzed yet" in page and 'href="/cv/1/text"' in page and "family-" not in page
    cv_with_profile(tmp_path)
    page = c.get("/cv/2/market").text
    assert "No matches yet" in page and 'href="/postings"' in page and "family-" not in page
    assert "Noch kein Abgleich" in c.get("/cv/2/market", headers=DE).text


def test_market_and_path_pages_of_an_unknown_cv_are_404(tmp_path):
    c = make_client(tmp_path)
    for path in ["/cv/99/market", "/cv/99/paths"]:
        r = c.get(path, headers=DE)
        assert r.status_code == 404 and "Diese Seite gibt es nicht." in r.text, path


def test_market_page_escapes_unknown_skill_ids(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "test_automation", {"<script>alert(1)</script>": 4.0})
    page = c.get(f"/cv/{cv_id}/market").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page


def test_demo_market_and_paths_are_read_only(tmp_path):
    c = demo_client(tmp_path, key=False, client=FailingClient())
    market_page = c.get("/cv/1/market", headers=DE).text
    section = block(market_page, "test_automation")
    assert "1 · passt" in section and "5 Inserate" in plain(section) and "Kubernetes" in section
    assert "<form" not in market_page
    paths_page = c.get("/cv/1/paths", headers=DE).text
    assert "Zuerst die häufigste Lücke." in paths_page and "Einen kleinen Kubernetes-Cluster" in paths_page
    assert "<form" not in paths_page and "Pfad erstellen" not in paths_page and "neu erstellen" not in paths_page
    assert "Rollen ohne Pfad" not in paths_page  # the only family has a tier and a path
    assert "geändert" not in paths_page  # the demo cannot build a path again, so the hint would only confuse
    assert not re.search(r"erstellt \d", paths_page)  # the demo store is made at start: its paths have no real time


# --- paths ---

def test_path_page_offers_a_button_for_every_family_with_a_tier(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient(), settings=with_min(5))
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "data_analysis_bi", {"sql": 2.0}, n=2)  # no tier: no section, no button (a line, no id)
    en = c.get("/cv/1/paths").text
    assert re.search(r"Create path \(about \d+ cents\)", en) and 'action="/cv/1/paths/test_automation"' in en
    assert "Test automation" in block(en, "test_automation") and 'class="mark tier-1"' in en
    assert "data_analysis_bi" not in en
    de = c.get("/cv/1/paths", headers=DE).text
    assert re.search(r"Pfad erstellen \(ca\. \d+ Cent\)", de) and "Testautomatisierung" in de
    form = re.search(r'<form[^>]*action="/cv/1/paths/test_automation"[^>]*>', en).group(0)
    assert 'method="post"' in form and "data-busy=" in form
    assert 'name="force"' not in en


# --- paths: the roles without a path ---

FEW_EN = "{}: {} of {} postings – no tier yet, so no path. Add more postings of this role."
FEW_DE = "{}: {} von {} Inseraten – noch keine Stufe, darum kein Pfad. Fügen Sie mehr Inserate dieser Rolle hinzu."
OTHER_EN = "Other roles: {} postings – they fit no target role, so no path."
OTHER_DE = "Sonstige Rollen: {} Inserate – sie passen zu keiner Zielrolle, darum kein Pfad."


def test_the_paths_page_explains_every_other_role_family_in_one_line(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient(), settings=with_min(5))  # test_automation: 5 postings, tier 1
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "qa_management", {"api_testing": 2.0}, n=4)
        seed_family(st, 1, "sap_test_consulting", {"sql": 2.0}, n=3)
        seed_family(st, 1, "data_analysis_bi", {"sql": 2.0}, n=3)
        seed_family(st, 1, "other", {"sql": 2.0}, n=2)
    en, de = c.get("/cv/1/paths").text, c.get("/cv/1/paths", headers=DE).text
    # the family with a tier keeps its path section and button; the others have a line, in order of postings, then name
    assert 'action="/cv/1/paths/test_automation"' in en and 'action="/cv/1/paths/test_automation"' in de
    assert no_path_lines(en) == [FEW_EN.format("QA management", 4, 5), FEW_EN.format("Data analysis and BI", 3, 5),
                                 FEW_EN.format("SAP test consulting", 3, 5), OTHER_EN.format(2)]
    assert no_path_lines(de) == [FEW_DE.format("QA-Management", 4, 5), FEW_DE.format("Datenanalyse und BI", 3, 5),
                                 FEW_DE.format("SAP-Testberatung", 3, 5), OTHER_DE.format(2)]
    assert "Roles without a path" in en and "Rollen ohne Pfad" in de
    assert en.index("family-test_automation") < en.index("Roles without a path")
    for family in ("qa_management", "sap_test_consulting", "data_analysis_bi"):
        assert family not in en and family not in de  # no section, no button, no id
    assert en.count("<form") == 1 and de.count("<form") == 1
    assert "Rollen ohne Pfad" not in en and "Roles without a path" not in de


def test_the_family_other_never_gets_a_path_and_the_line_counts_in_words(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient(), settings=with_min(5))
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "other", {"sql": 2.0}, n=7)  # enough postings for a tier, still no path
    en = c.get("/cv/1/paths").text
    assert no_path_lines(en) == [OTHER_EN.format(7)]
    assert 'action="/cv/1/paths/other"' not in en and 'id="family-other"' not in en
    assert no_path_lines(c.get("/cv/1/paths", headers=DE).text) == [OTHER_DE.format(7)]


def test_one_posting_of_other_is_a_singular(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "other", {"sql": 2.0}, n=1)
    assert no_path_lines(c.get(f"/cv/{cv_id}/paths").text) == [
        "Other roles: 1 posting – they fit no target role, so no path."]
    assert no_path_lines(c.get(f"/cv/{cv_id}/paths", headers=DE).text) == [
        "Sonstige Rollen: 1 Inserat – sie passen zu keiner Zielrolle, darum kein Pfad."]


def test_the_minimum_of_the_settings_decides_who_has_a_path_and_is_named_in_the_line(tmp_path):
    client_with_market(tmp_path, ScriptedClient())
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "data_analysis_bi", {"sql": 2.0}, n=3)
        seed_family(st, 1, "qa_management", {"api_testing": 2.0}, n=2)
    three = make_client(tmp_path, settings=with_min(3)).get("/cv/1/paths").text
    four_client = make_client(tmp_path, settings=with_min(4))
    four = four_client.get("/cv/1/paths").text
    assert "family-data_analysis_bi" in three and no_path_lines(three) == [FEW_EN.format("QA management", 2, 3)]
    assert "family-data_analysis_bi" not in four
    assert no_path_lines(four) == [FEW_EN.format("Data analysis and BI", 3, 4), FEW_EN.format("QA management", 2, 4)]
    assert no_path_lines(four_client.get("/cv/1/paths", headers=DE).text) == [
        FEW_DE.format("Datenanalyse und BI", 3, 4), FEW_DE.format("QA-Management", 2, 4)]
    default = make_client(tmp_path).get("/cv/1/paths").text  # the default minimum is 1: all three have a path
    assert "Roles without a path" not in default and default.count('<section class="family"') == 3


def test_a_family_with_a_tier_but_no_gaps_is_listed_without_a_button(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient())
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "qa_management", {}, n=3, tier=1, must_coverage=1.0)
    en, de = c.get("/cv/1/paths").text, c.get("/cv/1/paths", headers=DE).text
    assert no_path_lines(en) == ["QA management: no gaps – no path needed."]
    assert no_path_lines(de) == ["QA-Management: keine Lücken – kein Pfad nötig."]
    assert "qa_management" not in en and 'action="/cv/1/paths/test_automation"' in en


def test_a_family_with_a_stored_path_keeps_its_section_although_it_has_no_gaps_now(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient())
    c.post("/cv/1/paths/test_automation")
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "qa_management", {}, n=3, tier=1, must_coverage=1.0)
        st.save_path(1, st.paths(1)["test_automation"].model_copy(update={"role_family": "qa_management"}), "fake")
    en = c.get("/cv/1/paths").text
    assert "Close the top gap first." in block(en, "qa_management") and "Roles without a path" not in en


def test_no_family_with_a_tier_the_page_says_so_with_the_hint_and_lists_each_family(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient(), settings=with_min(6))  # test_automation: 5 postings
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "qa_management", {"api_testing": 2.0}, n=3)
    en, de = c.get("/cv/1/paths").text, c.get("/cv/1/paths", headers=DE).text
    top = plain(en[en.index("<main>"):en.index("Roles without a path")])
    assert "No role has enough postings for a tier yet (at least 6). Add more postings of these roles." in top
    assert 'href="/postings"' in en and 'class="family"' not in en and 'action="/cv/1/paths/' not in en
    assert no_path_lines(en) == [FEW_EN.format("Test automation", 5, 6), FEW_EN.format("QA management", 3, 6)]
    top = plain(de[de.index("<main>"):de.index("Rollen ohne Pfad")])
    assert ("Noch keine Rolle hat genug Inserate für eine Stufe (mindestens 6). "
            "Fügen Sie mehr Inserate dieser Rollen hinzu.") in top
    assert no_path_lines(de) == [FEW_DE.format("Testautomatisierung", 5, 6), FEW_DE.format("QA-Management", 3, 6)]


@pytest.mark.parametrize("family, weights, line", [
    ("other", {"sql": 2.0}, OTHER_EN.format(3)),
    ("qa_management", {}, "QA management: no gaps – no path needed."),
])
def test_when_a_tier_exists_but_no_path_is_possible_the_page_says_so_and_gives_the_reasons(
        tmp_path, family, weights, line):
    c = make_client(tmp_path, client=ScriptedClient())
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, family, weights, n=3, tier=1, must_coverage=1.0)
    en = c.get(f"/cv/{cv_id}/paths").text
    top = plain(en[en.index("<main>"):en.index("Roles without a path")])
    assert "No path is possible yet. The reasons are below." in top and "No role has enough postings" not in top
    assert no_path_lines(en) == [line] and 'class="family"' not in en
    assert "Noch ist kein Pfad möglich. Die Gründe stehen unten." in c.get(f"/cv/{cv_id}/paths", headers=DE).text


def test_a_cv_without_matches_has_no_list_of_roles_without_a_path(tmp_path):
    c = make_client(tmp_path)
    cv_with_profile(tmp_path)
    page = c.get("/cv/1/paths").text
    assert "No role has enough postings for a tier yet" in page and "Roles without a path" not in page


def test_the_demo_names_the_roles_without_a_path_too_and_stays_read_only(tmp_path):
    c = demo_client(tmp_path, key=False, settings=with_min(6))  # the one family of the demo has 5 postings
    de = c.get("/cv/1/paths", headers=DE).text
    assert no_path_lines(de) == [FEW_DE.format("Testautomatisierung", 5, 6)]
    assert "<form" not in de and "Noch keine Rolle hat genug Inserate" in de


def test_path_is_built_once_unless_forced(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client)
    for _ in range(2):
        c.post("/cv/1/paths/test_automation", data={"confirm": "1"})
    assert path_calls(client) == 1
    c.post("/cv/1/paths/test_automation", data={"confirm": "1", "force": "1"})
    assert path_calls(client) == 2


def test_path_runs_as_the_task_path_in_the_ui_language(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client)
    r = c.post("/cv/1/paths/test_automation", headers=DE, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cv/1/paths#family-test_automation"
    task = c.app.state.skillpose.tasks.current
    assert task.name == "path" and task.target == "/cv/1/paths" and task.ok
    assert stored_paths(tmp_path)["test_automation"].language == "de"
    [call] = [x for x in client.calls if x.path == "PathDraft"]
    assert "in German." in str(call.kwargs["system"])
    page = c.get("/cv/1/paths", headers=DE).text
    assert "Pfad für Testautomatisierung fertig (Schritte: 1, Wochen gesamt: 4)" in plain(main_of(page))
    assert 'class="msg ok"' in page


def test_path_page_shows_the_path_in_the_ui_language(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient())
    c.post("/cv/1/paths/test_automation")
    with Store(tmp_path / "web.sqlite") as st:
        path = st.paths(1)["test_automation"]
        step = path.steps[0].model_copy(update={"closes": ["regression_testing"]})
        st.save_path(1, path.model_copy(update={"steps": [step]}), "fake")
        created = st.db.execute("SELECT created FROM path WHERE cv_id = 1").fetchone()[0]
    en, de = c.get("/cv/1/paths").text, c.get("/cv/1/paths", headers=DE).text
    section = plain(block(en, "test_automation"))
    for text in ["Close the top gap first.", "Build a small project", "Closes: Regression testing", "4 weeks",
                 "Proof: public repository", "Total: 4 weeks", f"created {fmt_time(created)}"]:
        assert text in section, text
    assert "Schließt: Regressionstests" in plain(block(de, "test_automation"))
    assert "Gesamt: 4 Wochen" in plain(block(de, "test_automation"))
    assert f"erstellt {fmt_time(created)}" in plain(block(de, "test_automation"))
    assert "Create path" not in en and re.search(r"Create again \(about \d+ cents\)", en)
    assert "exists only in" not in en
    form = re.search(r'<form[^>]*action="/cv/1/paths/test_automation".*?</form>', en, re.S).group(0)
    assert 'name="force" value="1"' in form


def path_form(page, family="test_automation"):
    return re.search(rf'<form[^>]*action="/cv/1/paths/{family}".*?</form>', page, re.S).group(0)


def test_a_path_only_in_another_language_shows_a_note_and_the_create_button(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client)
    c.post("/cv/1/paths/test_automation", headers=DE, data={"confirm": "1"})  # a German path only
    with Store(tmp_path / "web.sqlite") as st:
        created = st.path_created(1, "test_automation", "de")
    en = block(c.get("/cv/1/paths").text, "test_automation")
    assert "This path exists only in German." in plain(en) and "Close the top gap first." in plain(en)
    assert en.index("exists only in German") < en.index('class="ai-output"')
    assert f"created {fmt_time(created)}" in plain(en) and 'data-ai-generated="true"' in en
    assert "are unchanged" in plain(en)  # compared in the language of the shown path
    assert re.search(r"Create path \(about \d+ cents\)", en) and "Create again" not in en
    assert 'name="force"' not in path_form(en)  # the button creates the English path
    de = block(c.get("/cv/1/paths", headers=DE).text, "test_automation")
    assert "nur auf" not in de and re.search(r"Neu erstellen \(ca\. \d+ Cent\)", de)
    assert 'name="force" value="1"' in path_form(de)

    c.post("/cv/1/paths/test_automation", data={"confirm": "1"})  # the button of the English page
    assert path_calls(client) == 2
    with Store(tmp_path / "web.sqlite") as st:
        assert [p.language for p in st.paths_all(1)] == ["de", "en"]
    en = block(c.get("/cv/1/paths").text, "test_automation")
    assert "exists only in" not in en and re.search(r"Create again \(about \d+ cents\)", en)
    c.post("/cv/1/paths/test_automation", data={"confirm": "1"})
    c.post("/cv/1/paths/test_automation", headers=DE, data={"confirm": "1"})
    assert path_calls(client) == 2  # each language is reused


def test_a_path_only_in_english_says_so_on_the_german_page(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient())
    c.post("/cv/1/paths/test_automation", data={"confirm": "1"})
    de = block(c.get("/cv/1/paths", headers=DE).text, "test_automation")
    assert "Dieser Pfad liegt nur auf Englisch vor." in plain(de)
    assert re.search(r"Pfad erstellen \(ca\. \d+ Cent\)", de) and 'name="force"' not in path_form(de)


def test_demo_paths_switch_with_the_ui_language(tmp_path):
    c = demo_client(tmp_path, key=False)
    de = plain(block(c.get("/cv/1/paths", headers=DE).text, "test_automation"))
    en = plain(block(c.get("/cv/1/paths").text, "test_automation"))
    assert "Zuerst die häufigste Lücke." in de and "Einen kleinen Kubernetes-Cluster" in de
    assert "The most frequent gap first." not in de and "nur auf" not in de
    assert "The most frequent gap first." in en and "Set up a small Kubernetes cluster" in en
    assert "Zuerst die häufigste Lücke." not in en and "exists only in" not in en


def test_demo_path_in_one_language_only_is_shown_with_the_note(tmp_path):
    bundle = fixture_bundle()
    bundle.paths = [p for p in bundle.paths if p.language == "de"]
    write_fixture_bundles(tmp_path / "demo", bundle)
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    page = c.get("/cv/1/paths").text
    section = plain(block(page, "test_automation"))
    assert "This path exists only in German." in section and "Zuerst die häufigste Lücke." in section
    assert "<form" not in page and "Create path" not in page


def test_a_one_week_step_says_week(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient())
    c.post("/cv/1/paths/test_automation")
    with Store(tmp_path / "web.sqlite") as st:
        path = st.paths(1)["test_automation"]
        st.save_path(1, path.model_copy(update={"total_weeks": 1, "steps": [path.steps[0].model_copy(
            update={"effort_weeks": 1})]}), "fake")
    assert "Total: 1 week" in plain(c.get("/cv/1/paths").text)
    assert "Gesamt: 1 Woche" in plain(c.get("/cv/1/paths", headers=DE).text)


def test_path_page_says_whether_the_inputs_changed(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient())
    c.post("/cv/1/paths/test_automation", data={"confirm": "1"})
    page = c.get("/cv/1/paths").text
    assert "Profile and market view are unchanged" in plain(page) and "have changed" not in page
    with Store(tmp_path / "web.sqlite") as st:
        st.edit_skill(1, "java", hidden=True)  # the profile summary of the request changes
    changed = c.get("/cv/1/paths").text
    assert "Profile or market view have changed" in plain(changed) and "are unchanged" not in changed
    assert "Das Profil oder die Marktsicht haben sich" in plain(c.get("/cv/1/paths", headers=DE).text)


def test_changed_inputs_are_compared_in_the_language_of_the_stored_path(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client)
    c.post("/cv/1/paths/test_automation", headers=DE)  # a German path
    assert "are unchanged" in plain(c.get("/cv/1/paths").text)  # shown in English, still the same inputs


def test_path_for_family_without_tier_is_refused(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client, settings=with_min(5))
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "data_analysis_bi", {"sql": 2.0}, n=2)
    calls = len(client.calls)
    for family in ["data_analysis_bi", "qa_management"]:  # two postings; none at all
        r = c.post(f"/cv/1/paths/{family}", data={"confirm": "1", "force": "1"})
        assert r.status_code == 400 and "too little data (at least 5 postings)" in r.text
        assert r.text.index("<main>") < r.text.index("too little data")
    assert "zu wenig Daten (mindestens 5 Inserate)" in c.post("/cv/1/paths/data_analysis_bi", headers=DE).text
    assert len(client.calls) == calls and c.app.state.skillpose.tasks.current is None


def test_path_for_family_without_gaps_says_so_above_the_list_that_names_it(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "qa_management", {}, tier=1, must_coverage=1.0)
    calls = len(client.calls)
    r = c.post("/cv/1/paths/qa_management")
    # the family has no section (nothing to build): the refusal stands above the list that names it
    assert r.status_code == 400 and 'id="family-qa_management"' not in r.text
    refusal, listed = r.text.index('role="alert">no gaps – no path needed'), r.text.index("Roles without")
    assert r.text.index("<main>") < refusal < listed
    assert no_path_lines(r.text) == ["QA management: no gaps – no path needed."]
    assert 'role="alert">keine Lücken – kein Pfad nötig' in c.post("/cv/1/paths/qa_management", headers=DE).text
    assert len(client.calls) == calls and c.app.state.skillpose.tasks.current is None


def test_unknown_family_or_cv_is_a_404(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client)
    calls = len(client.calls)
    assert c.post("/cv/1/paths/not_a_family", data={"confirm": "1"}).status_code == 404
    assert c.post("/cv/99/paths/test_automation", data={"confirm": "1"}).status_code == 404
    assert len(client.calls) == calls and c.app.state.skillpose.tasks.current is None


def test_the_family_other_never_gets_a_path_even_by_a_hand_made_post(tmp_path):
    # with one posting enough for a tier, `other` has a tier and gaps; the page offers no button for it
    client = ScriptedClient()
    c = client_with_market(tmp_path, client)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "other", {"kubernetes": 2.0}, tier=2, must_coverage=0.5)
    calls = len(client.calls)
    for data in ({}, {"confirm": "1"}, {"confirm": "1", "force": "1"}):
        assert c.post("/cv/1/paths/other", data=data).status_code == 404
    assert len(client.calls) == calls and c.app.state.skillpose.tasks.current is None
    with Store(tmp_path / "web.sqlite") as st:
        assert "other" not in st.paths(1)


def test_path_over_the_monthly_limit_is_refused_next_to_the_button(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client, settings=NO_ROOM)  # the setup calls already used the 0.01 USD
    calls = len(client.calls)
    r = c.post("/cv/1/paths/test_automation", data={"confirm": "1"})
    section = block(r.text, "test_automation")
    assert r.status_code == 400 and "Monthly limit" in section and "Nothing was sent." in section
    assert section.index('action="/cv/1/paths/test_automation"') < section.index("Monthly limit")
    assert "Monatsgrenze" in c.post("/cv/1/paths/test_automation", headers=DE).text
    assert len(client.calls) == calls and c.app.state.skillpose.tasks.current is None


def test_path_above_the_threshold_asks_first_and_keeps_force(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client, settings=ASK_ALWAYS)
    r = c.post("/cv/1/paths/test_automation", data={})
    assert r.status_code == 200 and path_calls(client) == 0 and c.app.state.skillpose.tasks.current is None
    assert re.search(r"Yes, spend about \d+\.\d\d USD", r.text) and 'name="confirm" value="1"' in r.text
    assert 'action="/cv/1/paths/test_automation"' in r.text and 'href="/cv/1/paths"' in r.text
    assert 'name="force"' not in r.text
    assert 'name="force" value="1"' in c.post("/cv/1/paths/test_automation", data={"force": "1"}).text
    assert re.search(r"Ja, ca\. \d+,\d\d USD ausgeben", c.post("/cv/1/paths/test_automation", headers=DE).text)
    c.post("/cv/1/paths/test_automation", data={"confirm": "1"})
    assert path_calls(client) == 1


def test_path_while_another_task_runs_waits_and_is_built_after_it(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client)
    with task_running(c, "postings", "/postings") as runner:
        for _ in range(2):  # a double click: queued once
            r = c.post("/cv/1/paths/test_automation", data={"confirm": "1"}, follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == ("/cv/1/paths?msg=queued&family=test_automation"
                                             "#family-test_automation")
        page = c.get("/cv/1/paths?msg=queued&family=test_automation").text
        section = block(page, "test_automation")
        assert QUEUED in section and section.index("<form") < section.index(QUEUED)
        assert "Queued –" not in c.get("/cv/1/paths?msg=queued&family=other").text
        assert [w.name for w in runner.waiting] == ["path"] and path_calls(client) == 0
    assert path_calls(client) == 1 and "test_automation" in stored_paths(tmp_path)


def test_a_waiting_path_that_no_longer_fits_the_monthly_limit_sends_nothing(tmp_path):
    # the estimate fitted at the click; the task before it spent most of the rest while the path waited
    client = ScriptedClient()
    client_with_market(tmp_path, client)
    with Store(tmp_path / "web.sqlite") as st:
        before, cost = st.month_total_usd(), estimate(open_run(load_settings(), st, "en"), n_paths=1)
    limit = round(before + 1.5 * cost, 2)
    c = make_client(tmp_path, client=client, settings=load_settings().model_copy(
        update={"monthly_limit_usd": limit}))
    calls = len(client.calls)
    with task_running(c, "postings", "/postings", spend=limit - before - cost / 2):
        r = c.post("/cv/1/paths/test_automation", data={"confirm": "1"}, follow_redirects=False)
        assert r.headers["location"] == "/cv/1/paths?msg=queued&family=test_automation#family-test_automation"
    task = c.app.state.skillpose.tasks.current
    assert (task.name, task.ok) == ("path", False) and len(client.calls) == calls
    assert task.message.startswith("Monthly limit: ") and task.message.endswith(
        f"of {limit:.2f} USD spent, this path costs about {cost:.2f} USD. Nothing was sent.")
    assert "test_automation" not in stored_paths(tmp_path)


def test_path_is_refused_when_five_tasks_wait(tmp_path):
    client = ScriptedClient()
    c = client_with_market(tmp_path, client)
    with task_running(c, "postings", "/postings") as runner:
        for i in range(5):
            runner.start("cv", f"/cv/{i + 2}", lambda: "")
        r = c.post("/cv/1/paths/test_automation", data={"confirm": "1"}, follow_redirects=False)
        assert r.headers["location"] == "/cv/1/paths?msg=busy&family=test_automation#family-test_automation"
        page = c.get("/cv/1/paths?msg=busy&family=test_automation").text
        section = block(page, "test_automation")
        assert "Another task is running" in section and section.index("<form") < section.index("Another task")
        assert "Another task is running" not in c.get("/cv/1/paths?msg=busy&family=other").text
    assert path_calls(client) == 0


def test_a_failed_path_shows_the_error_on_the_page(tmp_path):
    class RefusesPaths(ScriptedClient):
        def _answer(self, **kw):
            if kw["output_config"]["format"]["schema"]["title"] == "PathDraft":
                return refusal()
            return super()._answer(**kw)

    c = client_with_market(tmp_path, RefusesPaths())
    c.post("/cv/1/paths/test_automation", data={"confirm": "1"})
    task = c.app.state.skillpose.tasks.current
    refused = ("The path for Test automation could not be created: Claude (AI) refused the request. Cost of the "
               "attempt: 0.0020 USD. Try again.")  # 500 input tokens on Opus 5.5
    assert task.name == "path" and task.ok is False and task.message == refused
    page = c.get("/cv/1/paths").text
    assert 'class="msg error"' in main_of(page) and refused in main_of(page)
    assert not stored_paths(tmp_path)
    assert "Create path" in page  # nothing was stored, so the button is still there


PATH_CUT_OFF = {
    "de": "Der Pfad für Testautomatisierung konnte nicht erstellt werden: Die Antwort war zu lang und wurde "
          "abgeschnitten (Grenze 16.000 Token). Kosten des Versuchs: 0,33 USD. Bitte erneut versuchen.",
    "en": "The path for Test automation could not be created: The answer was too long and was cut off (limit "
          "16,000 tokens). Cost of the attempt: 0.33 USD. Try again.",
}


@pytest.mark.parametrize("lang", LANGS)
def test_a_path_cut_off_at_the_token_limit_names_the_limit_and_the_cost(tmp_path, lang):
    class CutsPaths(ScriptedClient):
        def _answer(self, **kw):
            if kw["output_config"]["format"]["schema"]["title"] == "PathDraft":
                return answer('{"rationale": ', stop_reason="max_tokens", usage=Usage(3000, 16000, 0, 0))
            return super()._answer(**kw)

    c = client_with_market(tmp_path, CutsPaths())
    headers = {"accept-language": lang}
    c.post("/cv/1/paths/test_automation", data={"confirm": "1"}, headers=headers)
    task = c.app.state.skillpose.tasks.current
    assert task.ok is False and task.message == PATH_CUT_OFF[lang]
    assert PATH_CUT_OFF[lang] in main_of(c.get("/cv/1/paths", headers=headers).text)
    assert not stored_paths(tmp_path)


def test_path_page_shows_no_links(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient())
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, 1, "qa_management", {"regression_testing": 6.0})
        report = next(r for r in market(st, load_settings(), 1) if r.role_family == "qa_management")
        draft = PathDraft(rationale="Start with https://evil.example/x and www.evil.example/y today", steps=[
            PathStepDraft(activity="https://evil.example/course", closes=["regression_testing"], effort_weeks=3,
                          proof="www.evil.example/certificate"),
            PathStepDraft(activity="Build a framework, see http://evil.example/guide", closes=["regression_testing"],
                          effort_weeks=2, proof="Public repository")])
        st.save_path(1, path_from_draft(draft, report, "hash", "en"), "fake")
    page = c.get("/cv/1/paths").text
    section = block(page, "qa_management")
    assert "Start with and today" in plain(section) and "Build a framework, see" in plain(section)
    assert "Public repository" in section
    assert "http" not in page.lower() and "www." not in page.lower() and '<a href="http' not in page
    assert not EMPTY_ELEMENT.findall(main_of(page))  # the step whose activity and proof are gone shows no empty line
    steps = re.findall(r'<li class="step">.*?</li>', section, re.S)
    assert len(steps) == 2 and "Proof" not in steps[0] and "3 weeks" in steps[0] and "Proof: Public" in plain(steps[1])


def test_path_text_is_escaped(tmp_path):
    c = client_with_market(tmp_path, ScriptedClient())
    c.post("/cv/1/paths/test_automation")
    with Store(tmp_path / "web.sqlite") as st:
        path = st.paths(1)["test_automation"]
        step = path.steps[0].model_copy(update={"activity": "<script>alert(1)</script>", "proof": "<b>x</b>"})
        st.save_path(1, path.model_copy(update={"rationale": "<img src=x onerror=alert(1)>", "steps": [step]}), "fake")
    page = c.get("/cv/1/paths").text
    assert "<script>alert(1)" not in page and "<img src=x" not in page and "<b>x</b>" not in page
    assert "&lt;script&gt;alert(1)" in page and "&lt;img src=x" in page


def test_with_a_key_the_demo_market_and_paths_are_not_reachable(tmp_path):
    c = demo_client(tmp_path, client=FailingClient())   # a key and demo data: the own data only, no CV yet
    c.cookies.set("view", "demo")   # a cookie left from the switch of older versions changes nothing
    assert c.get("/cv/1/market").status_code == 404 and c.get("/cv/1/paths").status_code == 404


# --- costs ---

def record_costs(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        st.record("cv", "claude-opus-5-5", Usage(1000, 500, 2000, 3000), 0.12, False)
        st.record("job", "claude-sonnet-5-5", Usage(500, 300, 0, 4500), 0.034, True)


def test_costs_page(tmp_path):
    record_costs(tmp_path)
    c = make_client(tmp_path)
    en = plain(main_of(c.get("/costs").text))
    for text in ["Spent this month 0.15 USD", "Monthly limit 5.00 USD", "Left this month 4.85 USD",
                 "Share of cache reads 68 %"]:
        assert text in en, text
    assert "Read CV" in en and "0.1200" in en and "50 %" in en  # the table per step
    assert "Read postings" in en and "0.0340" in en and "90 %" in en
    de = plain(main_of(c.get("/costs", headers=DE).text))
    for text in ["Ausgegeben im Monat 0,15 USD", "Monatsgrenze 5,00 USD", "Noch frei 4,85 USD",
                 "Anteil Cache-Lesungen 68 %"]:
        assert text in de, text
    assert "Lebenslauf lesen" in de and "0,1200" in de


def test_costs_page_names_the_prompt_set_for_the_own_data_only(tmp_path):
    # the prompts decide results and costs, so the page of the costs says which set this installation runs with
    c = make_client(tmp_path)
    assert "Prompts: example" in plain(main_of(c.get("/costs").text))
    assert "Prompts: example" in plain(main_of(c.get("/costs", headers=DE).text))
    label = use_prompts(prompt_folder(tmp_path / "mine")).label
    assert label.startswith("private:")
    assert f"Prompts: {label}" in plain(main_of(c.get("/costs").text))
    assert f"Prompts: {label}" in plain(main_of(c.get("/costs", headers=DE).text))
    shown = demo_client(tmp_path)  # a key and demo data: the own data only
    shown.cookies.set("view", "demo")
    assert f"Prompts: {label}" in plain(main_of(shown.get("/costs").text))
    # without a key and with demo data the demo is shown; without demo data the own data show the line
    assert "Prompts" not in plain(main_of(demo_client(tmp_path, key=False).get("/costs").text))
    assert f"Prompts: {label}" in plain(main_of(make_client(tmp_path, key=False).get("/costs").text))


def test_costs_rest_is_never_negative(tmp_path):
    record_costs(tmp_path)
    c = make_client(tmp_path, settings=load_settings().model_copy(update={"monthly_limit_usd": 0.05}))
    assert "Left this month 0.00 USD" in plain(main_of(c.get("/costs").text))
    assert "Noch frei 0,00 USD" in plain(main_of(c.get("/costs", headers=DE).text))


def test_costs_page_lists_the_last_calls(tmp_path):
    record_costs(tmp_path)
    c = make_client(tmp_path)
    newest, older = call_rows(c.get("/costs").text)
    assert re.fullmatch(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", newest[0])
    assert newest[1:] == ["Read postings", "claude-sonnet-5-5", "500", "300", "0", "4,500", "0.0340", "yes"]
    assert older[1:] == ["Read CV", "claude-opus-5-5", "1,000", "500", "2,000", "3,000", "0.1200", "no"]
    de_newest, de_older = call_rows(c.get("/costs", headers=DE).text)
    assert de_newest[1:] == ["Inserate lesen", "claude-sonnet-5-5", "500", "300", "0", "4.500", "0,0340", "ja"]
    assert de_older[1:] == ["Lebenslauf lesen", "claude-opus-5-5", "1.000", "500", "2.000", "3.000", "0,1200", "nein"]


def test_costs_page_shows_only_the_last_20_calls_newest_first(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        for i in range(25):
            st.record("job", "claude-sonnet-5-5", Usage(1000 + i, 1, 0, 0), 0.001, False)
    rows = call_rows(make_client(tmp_path).get("/costs").text)
    assert [row[3] for row in rows] == [f"1,{i:03d}" for i in range(24, 4, -1)]


def test_costs_page_without_calls(tmp_path):
    page = make_client(tmp_path).get("/costs").text
    assert "No calls yet." in page and "Spent this month 0.00 USD" in plain(main_of(page))
    assert "Noch keine Aufrufe." in make_client(tmp_path).get("/costs", headers=DE).text


def record_means(tmp_path):
    """record_costs plus an older CV call (2020), a second single posting call and an older one: all count."""
    record_costs(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.record("job", "claude-sonnet-5-5", Usage(500, 300, 0, 4500), 0.010, False)
        st.record("job", "claude-sonnet-5-5", Usage(500, 300, 0, 4500), 0.020, False)
        st.import_costs([CostRow(ts="2020-01-01T00:00:00", step="cv", model="claude-opus-5-5", input=1, output=1,
                                 cache_write=0, cache_read=0, usd=0.04, batch=False)])


def test_costs_page_shows_the_average_per_call_of_all_calls(tmp_path):
    record_means(tmp_path)
    c = make_client(tmp_path)
    en = c.get("/costs").text
    assert mean_rows(en) == [["Read CV", "single", "2", "0.0800"], ["Read postings", "single", "2", "0.0150"],
                             ["Read postings", "Batch", "1", "0.0340"]]
    text = plain(means_section(en))
    assert "Average per call (all calls so far)" in text
    assert "Measured in this installation; the figures in the README are a snapshot." in text
    de = c.get("/costs", headers=DE).text
    assert mean_rows(de) == [["Lebenslauf lesen", "einzeln", "2", "0,0800"],
                             ["Inserate lesen", "einzeln", "2", "0,0150"], ["Inserate lesen", "Batch", "1", "0,0340"]]
    text = plain(means_section(de))
    assert "Durchschnitt je Aufruf (alle bisherigen Aufrufe)" in text
    assert "Gemessen in dieser Installation; die Zahlen im README sind eine Momentaufnahme." in text
    assert "None" not in en and "None" not in de


def test_costs_page_averages_without_a_key_and_with_an_unknown_step(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        st.record("mystery", "claude-opus-5-5", Usage(1, 1, 0, 0), 0.5, False)
    page = make_client(tmp_path, key=False).get("/costs").text
    assert mean_rows(page) == [["mystery", "single", "1", "0.5000"]] and "None" not in page


def test_costs_page_averages_without_calls(tmp_path):
    c = make_client(tmp_path)
    en = plain(means_section(c.get("/costs").text))
    assert "Average per call" in en and "No calls yet." in en and "snapshot" not in en
    de = c.get("/costs", headers=DE).text
    assert "Noch keine Aufrufe." in plain(means_section(de)) and "<table" not in means_section(de)


def test_demo_costs_show_the_averages_of_the_demo_build(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        st.record("cv", "claude-opus-5-5", Usage(1000, 500, 2000, 3000), 0.12, False)
    c = demo_client(tmp_path, key=False)
    en = c.get("/costs").text
    assert mean_rows(en) == [["Read CV", "single", "1", "0.1700"], ["Development paths", "single", "1", "0.0900"]]
    assert "Average per call (building the demo)" in plain(means_section(en))
    assert "Measured when building the demo; the figures in the README are a snapshot." in plain(means_section(en))
    de = c.get("/costs", headers=DE).text
    assert mean_rows(de)[1] == ["Entwicklungspfade", "einzeln", "1", "0,0900"]
    assert "Durchschnitt je Aufruf (Erzeugung der Demo)" in plain(means_section(de))
    with_key = make_client(tmp_path, demo_dir=tmp_path / "demo")   # with a key: the own data only
    assert mean_rows(with_key.get("/costs").text) == [["Read CV", "single", "1", "0.1200"]]


def test_costs_without_a_key_has_no_month_rows(tmp_path):
    record_costs(tmp_path)
    c = make_client(tmp_path, key=False)
    page = c.get("/costs").text
    assert page.count("None") == 0 and 'class="cost"' not in page
    text = plain(main_of(page))
    assert "Spent this month" not in text and "Monthly limit" not in text and "Left this month" not in text
    assert "Share of cache reads" not in text and "USD" in text  # the calls still show
    assert "Monatsgrenze" not in c.get("/costs", headers=DE).text


def test_demo_costs_show_build_total(tmp_path):
    c = demo_client(tmp_path, key=False)
    de = plain(main_of(c.get("/costs", headers=DE).text))
    assert "Kosten der Demo-Erzeugung: 0,26 USD" in de
    assert "Lebenslauf lesen" in de and "0,1700" in de and "Entwicklungspfade" in de and "0,0900" in de
    assert "Ausgegeben im Monat" not in de and "Monatsgrenze" not in de
    en = plain(main_of(c.get("/costs").text))
    assert "Cost of building the demo: 0.26 USD" in en
    page = c.get("/costs").text
    rows = call_rows(page)
    assert [row[2] for row in rows] == ["claude-opus-5-5", "claude-opus-5-5"]
    assert rows[0][0] == fmt_time("2026-10-20T09:05:00") and rows[1][0] == fmt_time("2026-10-20T09:00:00")
    assert "<form" not in page


def test_with_a_key_the_costs_page_shows_the_own_costs_only(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        st.record("cv", "claude-opus-5-5", Usage(1000, 500, 2000, 3000), 0.12, False)
    c = demo_client(tmp_path)
    c.cookies.set("view", "demo")   # a cookie left from the switch of older versions changes nothing
    text = plain(main_of(c.get("/costs").text))
    assert "Spent this month 0.12 USD" in text and "Share of cache reads 50 %" in text and "0.1200" in text
    assert "Cost of building the demo" not in text and "0.1700" not in text and "0.0900" not in text


def test_costs_page_has_no_forms_and_a_nav_link(tmp_path):
    page = make_client(tmp_path).get("/costs").text
    assert "<h1>Costs</h1>" in page and "<form" not in page and 'href="/costs" aria-current="page"' in page


# --- texts ---

def test_every_step_has_a_label_in_both_languages():
    texts = load_texts()
    for lang in LANGS:
        for step in load_settings().steps:
            assert f"step.{step}" in texts[lang], (lang, step)
