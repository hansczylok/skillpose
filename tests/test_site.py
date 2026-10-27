"""The static demo site (`skillpose demo site`): complete, self-contained, read-only, the same every time."""

import hashlib
import html
import posixpath
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fakes import FailingClient

from skillpose.cli import main
from skillpose.site import DEFAULT_REPO_URL
from skillpose.web.i18n import load_texts

REF = re.compile(r'\b(?:href|src)="([^"]*)"')
REPO = "https://github.com/example-user/skillpose"


@pytest.fixture(scope="module")
def site(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("site") / "out"
    calls: list[str] = []
    rc = main(["demo", "site", "--out", str(out), "--repo-url", REPO],
              client_factory=lambda: FailingClient(calls=calls))
    assert rc == 0 and calls == []
    return out


def pages(site: Path) -> list[Path]:
    return sorted(site.rglob("*.html"))


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_expected_files_exist_in_both_languages(site):
    assert "English" in text(site / "index.html") and "Deutsch" in text(site / "index.html")
    for lang in ("de", "en"):
        tree = site / lang
        for name in ("index.html", "postings/index.html", "costs/index.html"):
            assert (tree / name).is_file(), (lang, name)
        for cv in (1, 2, 3):
            for name in ("index.html", "matches/index.html", "market/index.html", "paths/index.html",
                         "text/index.html"):
                assert (tree / "cv" / str(cv) / name).is_file(), (lang, cv, name)
            assert len(list((tree / "cv" / str(cv)).glob("skillpose-*.md"))) == 1  # the export as a file
        assert len(list((tree / "cv/1/matches").glob("*/index.html"))) == 30  # every match detail
        assert len(list((tree / "postings").glob("*/index.html"))) == 30
        assert (tree / "cv/1/matches/index-per10-page2.html").is_file()  # the paging links are files too
    assert (site / "static/style.css").is_file() and not (site / "static/app.js").exists()


def test_every_link_and_source_resolves_to_a_written_file(site):
    for page in pages(site):
        for ref in REF.findall(text(page)):
            ref = html.unescape(ref)
            if ref.startswith("#") or ref == REPO or ref.startswith(REPO + "#"):
                continue
            assert not re.match(r"[a-z]+:|//|/", ref), (page, ref)
            target = posixpath.normpath(posixpath.join(page.parent.as_posix(), urlsplit(ref).path))
            assert Path(target).is_file(), (page.relative_to(site), ref)
            assert Path(target).resolve().is_relative_to(site.resolve()), (page, ref)


def test_the_language_switch_leads_to_the_same_page_in_the_other_tree(site):
    page = site / "en/cv/2/matches/index-per10-page2.html"
    refs = [html.unescape(r) for r in REF.findall(text(page))]
    assert "../../../../de/cv/2/matches/index-per10-page2.html" in refs
    assert "index-per10-page2.html" in refs  # EN: the page itself
    assert (site / "de/cv/2/matches/index-per10-page2.html").is_file()
    assert (site / "de/costs/index.html").read_text(encoding="utf-8").count("../en/costs/index.html") == 1


def test_no_page_posts_a_form_loads_a_script_or_calls_another_host(site):
    for page in pages(site):
        content = text(page)
        assert 'method="post"' not in content.lower(), page
        assert "<script" not in content and "http-equiv" not in content, page
        for form in re.findall(r"<form\b.*?</form>", content, re.S):  # a read-only field outside a form is fine
            for control in re.findall(r"<(?:button|input|textarea|select)\b[^>]*>", form):
                assert "disabled" in control or 'type="hidden"' in control, (page, control)
        assert "action=" not in content, page
        for ref in REF.findall(content):
            assert not ref.startswith(("http:", "https:", "//")) or ref.startswith(REPO), (page, ref)
        assert "url(" not in content and "@import" not in content


def test_every_page_has_the_note_in_its_language_and_the_repository_link(site):
    for lang, note in (("de", "Statische Demo mit erfundenen Daten – für eine eigene Analyse die App lokal starten"),
                       ("en", "Static demo with invented data – to analyze your own CV, run the app locally")):
        for page in pages(site / lang):
            content = text(page)
            assert note in content, page
            assert f'<a href="{REPO}#readme">' in content, page
            assert f'<html lang="{lang}">' in content


def test_light_and_dark_come_from_the_stylesheet(site):
    css = text(site / "static/style.css")
    assert "prefers-color-scheme: dark" in css and ".site-note" in css
    for page in pages(site):
        assert '<meta name="color-scheme" content="light dark">' in text(page)


DISCLOSURE = ("Offenlegung gemäß § 25 Abs. 5 MedienG: Medieninhaber: Hans Czylok, Ottensheim, Österreich. "
              "Privates, nicht-kommerzielles Lernprojekt.")


def test_every_page_carries_the_disclosure_in_german(site):
    for page in [*pages(site / "de"), *pages(site / "en"), site / "index.html"]:
        assert html.escape(DISCLOSURE) in text(page), page


def test_the_default_address_is_the_project_repository():
    assert DEFAULT_REPO_URL == "https://github.com/hansczylok/skillpose"


def test_the_own_cv_button_is_disabled_and_a_hint_leads_to_the_readme(site):
    texts = load_texts()
    for lang in ("de", "en"):
        start = text(site / lang / "index.html")
        assert re.search(r'<button type="button" class="button secondary" disabled[^>]*>'
                         + re.escape(texts[lang]["hero.own"]) + "</button>", start)
        assert html.escape(texts[lang]["hero.own_hint"]) in start
        assert f'<a href="{REPO}#quick-start">{html.escape(texts[lang]["hero.own_guide"])}</a>' in start
        assert 'href="#upload"' not in start.split('<section id="upload"')[0]


def test_the_pages_do_not_say_that_no_key_is_set(site):
    texts = load_texts()
    for lang in ("de", "en"):
        for key in ("start.no_key", "blocked.no_key"):
            for page in pages(site / lang):
                assert html.escape(texts[lang][key]) not in text(page), (page, key)


def test_no_key_or_owner_data_in_the_output(site):
    for path in site.rglob("*"):
        if path.is_file() and path.suffix in {".html", ".md", ".css", ".js", ".svg"}:
            content = path.read_text(encoding="utf-8")
            assert not re.search(r"sk-" r"ant-|ANTHROPIC_API_KEY|C:[\\/]Users|127\.0\.0\.1|localhost", content), path
            assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", content), path


def digest(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_the_output_is_the_same_twice_and_needs_no_key(site, tmp_path, monkeypatch):
    """The second build runs with a key in the environment: the site is the demo all the same, byte for byte."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" "ant-not-a-real-key")
    again = tmp_path / "again"
    assert main(["demo", "site", "--out", str(again), "--repo-url", REPO]) == 0
    assert digest(again) == digest(site)
    assert "mode-demo" in text(again / "en/index.html") and "mode-own" not in text(again / "en/index.html")


def test_a_second_run_replaces_the_old_site_and_the_default_link_is_the_project_repository(site, tmp_path):
    out = tmp_path / "replaced"
    shutil.copytree(site, out)
    stale = out / "en" / "stale.html"
    stale.write_text("old", encoding="utf-8")
    keep = out / "CNAME"  # a file that is not part of the site stays
    keep.write_text("example.org", encoding="utf-8")
    assert main(["demo", "site", "--out", str(out)]) == 0
    assert not stale.exists() and keep.exists()
    assert f'<a href="{DEFAULT_REPO_URL}#readme">' in text(out / "de/index.html")


def test_the_pages_workflow_builds_with_read_only_rights():
    workflow = (Path(__file__).parent.parent / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    assert "uses: actions/configure-pages" not in workflow  # it needs pages: write, the build job has contents: read
    assert "skillpose demo site" in workflow and "actions/deploy-pages" in workflow
