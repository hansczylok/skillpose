"""The notices before publication: footer note, AI label on pages with model output, hint on the CV text page."""

import html
import re
from html.parser import HTMLParser

from demo_fixture import fixture_bundle, write_fixture_bundles
from fakes import CV
from test_web_jobs import seed_match
from test_web_market import cv_with_profile, seed_family
from webkit import make_client

from skillpose.core.models import DevelopmentPath, PathStep
from skillpose.store.db import Store
from skillpose.web.i18n import LANGS, load_texts

HEADERS = {"de": {"accept-language": "de"}, "en": {"accept-language": "en"}}
NOTE = {
    "de": "Privates, nicht-kommerzielles Lernprojekt. Analysen mit Claude (KI) von Anthropic: "
          "Ergebnisse können falsch sein, bitte prüfen.",
    "en": "A private, non-commercial learning project. Analyses use Claude (AI) by Anthropic: "
          "results can be wrong, please check them.",
}
AI_LABEL = {
    "de": "Von Claude (KI) erstellt. Kann Fehler enthalten. Bitte mit dem Lebenslauf und dem Inserat vergleichen.",
    "en": "Created by Claude (AI). It can contain errors. Compare it with the CV and the posting.",
}
PRIVACY_HINT = {
    "de": "Beim Analysieren geht dieser Text an die Anthropic-API (USA). Entferne vorher, was die Analyse nicht "
          "braucht: Adresse, Telefon, Geburtsdatum, Familienstand, Religion, Angaben zu Gesundheit oder Behinderung. "
          "Fotos liest skillpose nicht.",
    "en": "When you analyse, this text goes to the Anthropic API (USA). First remove what the analysis does not "
          "need: address, phone, date of birth, family status, religion, health or disability information. "
          "skillpose does not read photos.",
}
FAMILIES = ["test_automation", "qa_management"]


def plain(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


class _Marked(HTMLParser):
    """The elements that carry data-ai-generated, each with the text inside it."""

    VOID = {"meta", "link", "input", "br", "img", "hr"}

    def __init__(self, page: str):
        super().__init__()
        self.stack: list[list] = []   # [tag, marked element or None]
        self.marked: list[dict] = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        element = None
        if "data-ai-generated" in dict(attrs):
            element = {"tag": tag, "attrs": dict(attrs), "text": []}
            self.marked.append(element)
        self.stack.append([tag, element])

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop()[0] != tag:
            pass

    def handle_data(self, data):
        for _, element in self.stack:
            if element is not None:
                element["text"].append(data)


def marked(page: str) -> list[dict]:
    """The elements that carry data-ai-generated: tag, attributes and the text inside."""
    found = _Marked(page).marked
    for element in found:
        element["text"] = plain(" ".join(element["text"]))
    return found


def check_label(page: str, lang: str, models: list[str | None]) -> None:
    """The page holds exactly these marked elements (data-ai-model of each; None: no attribute), each with the
    visible label as its note."""
    elements = marked(page)
    assert [e["attrs"].get("data-ai-model") for e in elements] == models
    assert all(e["attrs"]["data-ai-generated"] == "true" for e in elements)
    assert all(AI_LABEL[lang] in e["text"] for e in elements)
    assert page.count('<p class="ai-note">') == len(elements)


def own_data(tmp_path):
    """CV 1 with a profile (Opus), two families with a tier and a stored path each, one posting made by Sonnet."""
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.save_profile(cv_id, st.profile(cv_id), "claude-opus-5-5")
        for family in FAMILIES:
            seed_family(st, cv_id, family, {"kubernetes": 4.0})
            step = PathStep(activity="Build a small Kubernetes project", closes=["kubernetes"], effort_weeks=4,
                            proof="A public repository")
            st.save_path(cv_id, DevelopmentPath(role_family=family, language="en", rationale="The biggest gap first.",
                                                steps=[step], total_weeks=4, input_hash="abc123"), "claude-opus-5-5")
        posting = st.postings()[0].hash
        st.save_job_profile(st.job_profile(posting), "claude-sonnet-5-5")
    return c, cv_id, posting


def demo_data(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    bundle = fixture_bundle()
    return make_client(tmp_path, key=False, demo_dir=tmp_path / "demo"), bundle.matches[0].text_hash


# --- footer (item 8) ---

def test_footer_names_the_project_character_and_claude_in_both_languages(tmp_path):
    texts = load_texts()
    c = make_client(tmp_path)
    for lang in LANGS:
        assert texts[lang]["app.note"] == NOTE[lang]
        for url in ["/", "/costs", "/cv/99"]:  # also an error page
            footer = re.search(r"<footer>(.*?)</footer>", c.get(url, headers=HEADERS[lang]).text, re.S).group(1)
            assert plain(footer) == NOTE[lang], (lang, url)


# --- AI label (item 9) ---

def test_ai_label_texts_in_both_languages():
    texts = load_texts()
    for lang in LANGS:
        assert texts[lang]["ai.generated"] == AI_LABEL[lang]


def test_profile_page_marks_the_model_output(tmp_path):
    c, cv_id, _ = own_data(tmp_path)
    for lang in LANGS:
        page = c.get(f"/cv/{cv_id}", headers=HEADERS[lang]).text
        check_label(page, lang, ["claude-opus-5-5"])
        assert page.index('class="ai-note"') < page.index('class="category"')  # the label stands above the skills
        assert marked(page)[0]["tag"] == "div" and "Selenium" in marked(page)[0]["text"]  # it encloses the skills


def test_match_detail_marks_the_requirements(tmp_path):
    c, cv_id, posting = own_data(tmp_path)
    for lang in LANGS:
        page = c.get(f"/cv/{cv_id}/matches/{posting}", headers=HEADERS[lang]).text
        check_label(page, lang, ["claude-sonnet-5-5"])
        assert page.index('class="ai-note"') < page.index('class="skills"')
        assert marked(page)[0]["tag"] == "section" and "need selenium" in marked(page)[0]["text"]


def test_match_detail_without_requirements_has_nothing_to_label(tmp_path):
    c, cv_id, _ = own_data(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        empty = seed_match(st, cv_id, "Empty one", 3, 0.0, [])
    check_label(c.get(f"/cv/{cv_id}/matches/{empty}").text, "en", [])


def test_paths_page_marks_each_path_and_only_paths(tmp_path):
    c, cv_id, _ = own_data(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_family(st, cv_id, "business_analysis", {"sql": 2.0})  # a family with a tier but no path yet
    for lang in LANGS:
        page = c.get(f"/cv/{cv_id}/paths", headers=HEADERS[lang]).text
        assert 'id="family-business_analysis"' in page
        check_label(page, lang, ["claude-opus-5-5"] * 2)
        assert [e["tag"] for e in marked(page)] == ["div", "div"]
        assert all("Build a small Kubernetes project" in e["text"] for e in marked(page))
        other = re.search(r'<section class="family"[^>]*id="family-business_analysis".*?</section>', page, re.S)
        assert "ai-note" not in other.group(0)


def test_pages_without_model_output_carry_no_label(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        cv_id = st.save_cv("cv.md", CV)
    for url in ["/", "/costs", "/postings", f"/cv/{cv_id}", f"/cv/{cv_id}/text", f"/cv/{cv_id}/paths"]:
        page = c.get(url).text
        assert "data-ai-generated" not in page and "ai-note" not in page, url


def test_demo_pages_carry_the_label_without_a_model_name(tmp_path):
    c, posting = demo_data(tmp_path)
    for lang in LANGS:
        for url in ["/cv/1", f"/cv/1/matches/{posting}", "/cv/1/paths"]:
            page = c.get(url, headers=HEADERS[lang]).text
            check_label(page, lang, [None])  # the demo models are a placeholder, not a model name
            assert "data-ai-model" not in page, url


def test_model_name_is_escaped_in_the_attribute(tmp_path):
    c, cv_id, _ = own_data(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.save_profile(cv_id, st.profile(cv_id), '"><script>alert(1)</script>')
    page = c.get(f"/cv/{cv_id}").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


# --- hint on the CV text page (item 10) ---

def test_cv_text_page_warns_before_the_text_goes_to_the_api(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        cv_id = st.save_cv("cv.md", CV)
    texts = load_texts()
    for lang in LANGS:
        assert texts[lang]["cv.privacy_hint"] == PRIVACY_HINT[lang]
        page = c.get(f"/cv/{cv_id}/text", headers=HEADERS[lang]).text
        assert page.count(PRIVACY_HINT[lang].split(":")[0]) == 1 and PRIVACY_HINT[lang] in plain(page)
        hint = page.index('class="privacy-hint"')
        assert hint < page.index(f'action="/cv/{cv_id}/text"') < page.index(f'action="/cv/{cv_id}/analyze"')


def test_cv_text_page_shows_no_hint_when_nothing_can_be_sent(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)  # analyzed: the text is fixed
    assert "privacy-hint" not in c.get(f"/cv/{cv_id}/text").text
    demo, _ = demo_data(tmp_path / "d")
    assert "privacy-hint" not in demo.get("/cv/1/text").text


# --- quotes stay in their original language (acceptance test 2026-10-22) ---

QUOTES_NOTE = {"de": "Zitate stehen wörtlich in der Originalsprache.",
               "en": "Quotes are verbatim, in the original language."}


def test_quotes_note_texts_in_both_languages():
    texts = load_texts()
    for lang in LANGS:
        assert texts[lang]["quotes.verbatim"] == QUOTES_NOTE[lang]


def test_profile_and_match_detail_say_that_quotes_stay_in_the_original_language(tmp_path):
    c, cv_id, posting = own_data(tmp_path)
    demo, demo_posting = demo_data(tmp_path / "d")
    pages = [(c, f"/cv/{cv_id}"), (c, f"/cv/{cv_id}/matches/{posting}"), (demo, "/cv/1"),
             (demo, f"/cv/1/matches/{demo_posting}")]
    for client, url in pages:
        for lang, other in (("de", "en"), ("en", "de")):
            page = client.get(url, headers=HEADERS[lang]).text
            assert page.count(QUOTES_NOTE[lang]) == 1 and QUOTES_NOTE[other] not in page, (url, lang)
            assert page.index('class="ai-note"') < page.index(QUOTES_NOTE[lang]) < page.index("<q>"), (url, lang)
    for lang in LANGS:  # a page without quotes has no note
        assert QUOTES_NOTE[lang] not in c.get(f"/cv/{cv_id}/paths", headers=HEADERS[lang]).text
