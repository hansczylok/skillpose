"""The look of the app: logo, app icon, the start page with its hero, and the dark mode that follows the system."""

import re
import struct
from pathlib import Path

import pytest
from demo_fixture import NAME, write_fixture_bundles
from webkit import make_client

import skillpose.web

WEB = Path(skillpose.web.__file__).parent
DE = {"accept-language": "de"}
SVGS = [WEB / "static" / name for name in ("logo.svg", "icon.svg", "favicon.svg")]
HERO = {"de": ["Wo stehe ich?", "Wohin gehe ich?", "Demo ansehen", "Eigener Lebenslauf",
               "Kompetenzen mit Zitaten aus dem Lebenslauf belegt, mit Inseraten verglichen, dazu Lücken und "
               "Entwicklungspfade.",
               "Profil – Position", "Abgleich – Lücken", "Marktsicht – Stufen", "Pfade – Orientierung",
               'alt="Logo von skillpose: zwei Hände, deren Zeigefinger sich fast berühren, dazwischen ein Funke"',
               "Demo-Personen (erfunden)", "Lebenslauf auf Deutsch"],
        "en": ["Where do I stand?", "Which way do I go?", "View demo", "Your own CV",
               "Skills proven by quotes from the CV, compared with job postings, with gaps and development paths.",
               "Profile – position", "Matches – gaps", "Market view – tiers", "Paths – orientation",
               'alt="skillpose logo: two hands whose index fingers almost touch, with a spark between them"',
               "Demo persons (invented)", "CV in German"]}


def demo_client(tmp_path, **kwargs):
    write_fixture_bundles(tmp_path / "demo")
    return make_client(tmp_path, demo_dir=tmp_path / "demo", **kwargs)


def hero(html: str) -> str:
    return re.search(r'<section class="hero".*?</section>', html, re.S)[0]


def test_header_shows_icon_and_wordmark_and_links_the_icons(tmp_path):
    page = make_client(tmp_path, key=False).get("/").text
    brand = re.search(r'<a class="brand"[^>]*>.*?</a>', page, re.S)[0]
    assert 'href="/"' in brand and 'aria-label="skillpose"' in brand
    assert re.search(r'<img src="/static/icon.svg" alt="" width="28" height="28">', brand)
    assert re.search(r'skill<svg [^>]*aria-hidden="true"', brand) and re.search(r"</svg>pose<", brand)
    assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in page
    assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">' in page
    assert "<title>skillpose</title>" in page


@pytest.mark.parametrize("name", ["logo.svg", "icon.svg", "favicon.svg"])
def test_svg_files_are_served_as_svg(tmp_path, name):
    r = make_client(tmp_path, key=False).get(f"/static/{name}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_apple_touch_icon_is_a_small_png_of_180_pixels(tmp_path):
    r = make_client(tmp_path, key=False).get("/static/apple-touch-icon.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n" and r.content[12:16] == b"IHDR"
    assert struct.unpack(">II", r.content[16:24]) == (180, 180) and len(r.content) <= 30 * 1024


def test_start_page_hero_follows_the_language(tmp_path):
    c = demo_client(tmp_path, key=False)
    german = c.get("/", headers=DE).text
    assert [x for x in HERO["de"] if x not in german] == []
    english = c.get("/lang/en").text   # the switch redirects to the start page
    assert [x for x in HERO["en"] if x not in english] == []
    assert "Wo stehe ich?" not in english and "Where do I stand?" not in german
    assert hero(english).index("Where do I stand?") < hero(english).index('src="/static/logo.svg"')


def test_hero_comes_first_and_shows_the_logo(tmp_path):
    page = demo_client(tmp_path, key=False).get("/").text
    main = page[page.index("<main>"):]
    assert main.index('<section class="hero"') < main.index("<h2")
    assert re.search(r'<h1[^>]*>\s*<span>Where do I stand\?</span>\s*<span>Which way do I go\?</span>\s*</h1>', main)
    assert re.search(r'<img [^>]*src="/static/logo.svg"', hero(page))


def test_with_a_key_the_hero_leads_to_the_upload_only(tmp_path):
    own = demo_client(tmp_path).get("/", headers=DE).text   # with a key: the own data only, no demo button
    assert "Demo ansehen" not in own and "<form" not in hero(own) and "/view/" not in own
    assert re.findall(r'<a class="[^"]*" href="[^"]*">[^<]*</a>', hero(own)) == [
        '<a class="button button-brand" href="#upload">Eigener Lebenslauf</a>']


def test_the_hint_of_the_disabled_button_is_german_in_german(tmp_path):
    page = hero(demo_client(tmp_path, key=False).get("/", headers=DE).text)
    assert "Eigener Lebenslauf</button>" in page and "mit eigenem Anthropic-API-Schlüssel starten." in page
    assert "Anleitung im README</a>" in page


def test_with_a_key_the_own_cv_button_is_active_and_no_hint_shows(tmp_path):
    own = hero(demo_client(tmp_path).get("/", headers=DE).text)
    assert "<button" not in own and "disabled" not in own and "own-hint" not in own and "quick-start" not in own


def test_in_the_demo_the_buttons_lead_to_the_first_person_and_to_the_upload(tmp_path):
    page = demo_client(tmp_path, key=False).get("/").text
    assert "/view/" not in page   # without a key there is only the demo
    assert re.search(r'<a class="button button-brand" href="/cv/1">View demo</a>', hero(page))
    # without a key the button is greyed out (not a link) and a hint says what to do, with a link to the README
    assert re.search(r'<button type="button" class="button secondary" disabled[^>]*>Your own CV</button>', hero(page))
    assert 'href="#upload"' not in hero(page)
    assert "To analyze your own CV, run the app locally with your own Anthropic API key." in hero(page)
    assert '<a href="https://github.com/hansczylok/skillpose#quick-start">Instructions in the README</a>' in hero(page)
    assert 'id="upload"' in page
    assert "No API key set: only the demo is available." in page   # the hint at the upload stays


def test_demo_persons_are_cards_with_name_and_cv_language(tmp_path):
    page = demo_client(tmp_path, key=False).get("/", headers=DE).text
    card = re.search(r'<li class="person">.*?</li>', page, re.S)[0]
    assert f'<a href="/cv/1">{NAME}</a>' in card and "Lebenslauf auf Deutsch" in card


def test_steps_link_to_the_pages_of_the_first_demo_person(tmp_path):
    page = demo_client(tmp_path, key=False).get("/").text
    strip = re.search(r'<ol class="flow".*?</ol>', page, re.S)[0]
    links = re.findall(r'<a href="([^"]+)">([^<]+)</a>', strip)
    assert links == [("/cv/1", "Profile – position"), ("/cv/1/matches", "Matches – gaps"),
                     ("/cv/1/market", "Market view – tiers"), ("/cv/1/paths", "Paths – orientation")]


def test_steps_without_the_demo_are_plain_text(tmp_path):
    page = make_client(tmp_path).get("/").text   # own data, no demo
    strip = re.search(r'<ol class="flow".*?</ol>', page, re.S)[0]
    assert "Profile – position" in strip and "<a " not in strip


def test_current_page_is_marked_in_brand_colour():
    css = (WEB / "static" / "style.css").read_text(encoding="utf-8")
    rule = re.search(r'nav a\[aria-current="page"\][^{]*\{([^}]*)\}', css)[1]
    assert "underline" in rule and "var(--brand-line)" in rule
    assert re.search(r"\.button-brand[^{]*\{[^}]*background: var\(--brand\)", css)


def _without_root_blocks(css: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.sub(r":root(\[[^\]]*\])?\s*\{[^{}]*\}", "", css)


def test_style_follows_the_system_and_keeps_every_colour_in_variables():
    css = (WEB / "static" / "style.css").read_text(encoding="utf-8")
    assert "color-scheme: light dark;" in css
    dark = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{([^{}]*)\}\s*\}", css)
    assert dark and "--bg: #0d1117;" in dark[1] and "--brand-line: #5DCAA5;" in dark[1]
    rest = _without_root_blocks(css)
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(", rest) == []
    light = re.search(r":root\s*\{([^{}]*)\}", css)[1]
    for value in ("--brand: #0F6E56;", "--brand-soft: #E1F5EE;", "--spark: #EF9F27;", "--brand-line: #0F6E56;"):
        assert value in light, value


def test_logo_has_its_own_dark_rule_and_a_title():
    logo = (WEB / "static" / "logo.svg").read_text(encoding="utf-8")
    assert 'role="img"' in logo and "<title" in logo and 'viewBox="0 0 240 110"' in logo
    assert "#1f2328" in logo and "#EF9F27" in logo
    assert re.search(r"@media \(prefers-color-scheme: dark\)\s*\{[^}]*#e6edf3", logo)


def test_icon_and_favicon_are_the_same_rounded_green_square():
    icon = (WEB / "static" / "icon.svg").read_text(encoding="utf-8")
    assert icon == (WEB / "static" / "favicon.svg").read_text(encoding="utf-8")
    assert 'rx="22"' in icon and "#0F6E56" in icon and "#E1F5EE" in icon and "#EF9F27" in icon


@pytest.mark.parametrize("path", sorted(WEB.glob("templates/*.html")) + SVGS,
                         ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_inline_script_handler_or_data_url(path):
    # the CSP allows scripts only from /static and no data: URLs; templates and images keep to it
    text = path.read_text(encoding="utf-8")
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", text) == []
    assert re.findall(r"<[^>]*\son[a-z]+\s*=", text) == []
    assert re.findall(r"\bdata:", text) == []
    if path.suffix == ".svg":
        assert "<script" not in text and "href=" not in text   # an image that loads nothing
