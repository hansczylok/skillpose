"""The demo shows each posting in the language of the interface (a translation
by Claude, marked as such, with the original one click away). Quotes stay verbatim. The own data never show one."""

import re

from demo_fixture import (
    ENGLISH_TEXT,
    ENGLISH_TITLE,
    GERMAN_TEXT,
    GERMAN_TITLE,
    MARKUP_TEXT,
    MARKUP_TITLE,
    bundle_with_translations,
    write_fixture_bundles,
)
from markupsafe import escape
from test_web_jobs import cv_with_profile, seed_match
from test_web_notices import marked
from webkit import make_client

from skillpose.core.models import text_hash
from skillpose.store.db import Store
from skillpose.web.i18n import load_texts

EN, DE = {"accept-language": "en"}, {"accept-language": "de"}
LABEL = {"en": "Translated by Claude (AI). It can contain errors.",
         "de": "Übersetzt von Claude (KI). Kann Fehler enthalten."}
NOTE = {"en": "Demo: Claude (AI) translated the postings into the language of the interface.",
        "de": "Demo: Claude (KI) hat die Inserate in die Sprache der Oberfläche übersetzt."}
GERMAN = text_hash(GERMAN_TEXT)
MARKUP = next(p.hash for p in bundle_with_translations().postings if "de" in p.translations)


def demo(tmp_path, **kwargs):
    write_fixture_bundles(tmp_path / "demo", bundle_with_translations())
    return make_client(tmp_path, key=False, demo_dir=tmp_path / "demo", **kwargs)


def translated_block(page: str) -> str:
    return re.search(r'<div class="translation[ "].*?</details>', page, re.S).group(0)


def test_texts_of_the_translation_in_both_languages():
    texts = load_texts()
    for lang in ("de", "en"):
        assert texts[lang]["ai.translated"] == LABEL[lang] and texts[lang]["posting.translated_note"].startswith(
            NOTE[lang])
    assert texts["de"]["posting.show_original"] == "Original anzeigen"
    assert texts["en"]["posting.show_original"] == "Show original"


def test_a_german_posting_in_the_english_demo_shows_the_translation_and_the_original(tmp_path):
    page = demo(tmp_path).get(f"/postings/{GERMAN}", headers=EN).text
    assert f"<h1>{ENGLISH_TITLE}</h1>" in page and f"<title>{ENGLISH_TITLE} – " in page
    block = translated_block(page)
    assert 'data-ai-generated="true"' in block and "data-ai-model" not in block and LABEL["en"] in block
    assert f'<div class="posting-text">{escape(ENGLISH_TEXT)}</div>' in block
    original = re.search(r"<details.*?</details>", block, re.S).group(0)
    assert "<summary>Show original</summary>" in original
    assert f'<div class="posting-text" lang="de">{escape(GERMAN_TEXT)}</div>' in original
    assert block.index(LABEL["en"]) < block.index(escape(ENGLISH_TEXT)) < block.index("<details")
    translation, requirements = marked(page)  # the original is no model output: it is outside the marked element
    assert translation["tag"] == "div" and LABEL["en"] in translation["text"] and "support with" in translation["text"]
    assert "Verstärkung" not in translation["text"] and "Verstärkung" not in requirements["text"]
    assert GERMAN_TITLE not in page.replace(GERMAN_TEXT, "")  # the original title only in the original text
    # the requirements keep their verbatim quotes and the note that says so
    assert "<q>Selenium required</q>" in page and "Quotes are verbatim, in the original language." in page


def test_a_german_posting_in_the_german_demo_shows_the_original_only(tmp_path):
    page = demo(tmp_path).get(f"/postings/{GERMAN}", headers=DE).text
    assert f"<h1>{GERMAN_TITLE}</h1>" in page and f'<div class="posting-text">{escape(GERMAN_TEXT)}</div>' in page
    assert ENGLISH_TITLE not in page and LABEL["de"] not in page and "<details" not in page
    assert 'class="translation"' not in page and "Original anzeigen" not in page


def test_an_english_posting_in_the_german_demo_shows_the_german_translation_escaped(tmp_path):
    c = demo(tmp_path)
    page = c.get(f"/postings/{MARKUP}", headers=DE).text
    assert f"<h1>{escape(MARKUP_TITLE)}</h1>" in page and "<b>Eins</b>" not in page
    block = translated_block(page)
    assert LABEL["de"] in block and f'<div class="posting-text">{escape(MARKUP_TEXT)}</div>' in block
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in block and "<script>alert" not in page
    assert "<summary>Original anzeigen</summary>" in block and '<div class="posting-text" lang="en">' in block
    english = c.get(f"/postings/{MARKUP}", headers=EN).text  # English already: the original, no label
    assert LABEL["en"] not in english and "<details" not in english and MARKUP_TITLE not in english


def test_the_demo_lists_show_the_translated_titles_with_a_note(tmp_path):
    c = demo(tmp_path)
    en = c.get("/postings", headers=EN).text
    assert f'<a href="/postings/{GERMAN}">{ENGLISH_TITLE}</a>' in en and GERMAN_TITLE not in en
    assert NOTE["en"] in en
    de = c.get("/postings", headers=DE).text
    assert f'<a href="/postings/{GERMAN}">{GERMAN_TITLE}</a>' in de
    assert f'<a href="/postings/{MARKUP}">{escape(MARKUP_TITLE)}</a>' in de and NOTE["de"] in de
    matches = c.get("/cv/1/matches", headers=EN).text
    assert f'<a href="/cv/1/matches/{GERMAN}">{ENGLISH_TITLE}</a>' in matches and NOTE["en"] in matches
    detail = c.get(f"/cv/1/matches/{GERMAN}", headers=EN).text
    assert f"<h1>{ENGLISH_TITLE}</h1>" in detail and NOTE["en"] in detail and "<q>Selenium required</q>" in detail
    german_detail = c.get(f"/cv/1/matches/{GERMAN}", headers=DE).text
    assert f"<h1>{GERMAN_TITLE}</h1>" in german_detail and NOTE["de"] not in german_detail


def test_a_demo_without_translations_shows_no_note(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    for headers in (EN, DE):
        for url in ["/postings", "/cv/1/matches"]:
            page = c.get(url, headers=headers).text
            assert NOTE["en"] not in page and NOTE["de"] not in page, url


def own_posting_with_a_translation(tmp_path) -> tuple[int, str]:
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_match(st, cv_id, GERMAN_TITLE, 2, 0.5, [("selenium", "must", "met", None, "evidenced")])
        st.save_translation(h, "en", "Own translated title", "Own translated text", "claude-code")
    return cv_id, h


def assert_no_translation(c, cv_id: int, h: str) -> None:
    for url in ["/postings", f"/postings/{h}", f"/cv/{cv_id}/matches", f"/cv/{cv_id}/matches/{h}"]:
        page = c.get(url, headers=EN).text
        assert GERMAN_TITLE in page and "Own translated" not in page, url
        assert LABEL["en"] not in page and NOTE["en"] not in page and '<details class="original"' not in page, url


def test_the_own_data_with_a_key_never_show_a_translation(tmp_path):
    cv_id, h = own_posting_with_a_translation(tmp_path)
    write_fixture_bundles(tmp_path / "demo", bundle_with_translations())
    c = make_client(tmp_path, demo_dir=tmp_path / "demo")  # with a key: the own data only
    assert_no_translation(c, cv_id, h)
    c.cookies.set("view", "demo")  # a cookie left from the switch of older versions changes nothing
    assert_no_translation(c, cv_id, h)
    assert h != GERMAN and c.get(f"/postings/{GERMAN}", headers=EN).status_code == 404  # the demo posting


def test_the_own_data_without_a_key_never_show_a_translation(tmp_path):
    cv_id, h = own_posting_with_a_translation(tmp_path)
    assert_no_translation(make_client(tmp_path, key=False), cv_id, h)  # no demo data: the own data, read-only
