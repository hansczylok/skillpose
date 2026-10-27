"""The static demo site: every page of the demo as a file, so a visitor of the repository sees it without installing.

The pages come from the app itself (a TestClient, no server, no key, no API call), so the site cannot drift from the
app. A crawl from the start page follows every link of every page; the pages are then rewritten: links become relative
paths between the written files (the site works from a folder, from a file:// address and under any path of GitHub
Pages), the language switch leads to the same page of the other tree, and everything that would send data is
disabled. The result is the same on every run: the date of the export and the time zone of the times are fixed.

Layout: index.html (choose the language), de/ and en/ (one tree each, a page is <path>/index.html), static/ (CSS,
icons, logo). A list with paging gets one file per ?per=...&page=... link (index-per10-page2.html); the default view
(30 per page, page 1) is the index.html itself, so every size of the app is there.
"""

import html
import logging
import posixpath
import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from skillpose import export
from skillpose.core.config import load_settings
from skillpose.web import app as web_app
from skillpose.web import deps, pages_cv
from skillpose.web.i18n import LANGS, load_texts
from skillpose.web.paging import DEFAULT_SIZE

log = logging.getLogger(__name__)

# The repository the site links to. --repo-url or SKILLPOSE_REPO_URL override it (the Pages workflow passes the
# repository it runs in, so a fork links to itself).
DEFAULT_REPO_URL = deps.REPO_URL
SITE_DAY = date(2026, 10, 25)      # the date in the name and the head of the Markdown export: the day of the demo build
OWN_DIRS = ("de", "en", "static")  # what a build replaces; other files in the folder stay
UNUSED_ASSETS = ("app.js",)        # the script only serves forms and reloads, and the site has neither

ATTRIBUTE = re.compile(r'\b(?P<name>href|src)="(?P<value>[^"]*)"')
FORM = re.compile(r"<form\b.*?</form>", re.S)
FORM_ATTRIBUTES = re.compile(r'\s(?:method|action|enctype|data-busy)="[^"]*"')
CONTROL = re.compile(r"<(?P<tag>button|input|textarea|select)\b(?P<attrs>[^>]*)>")
SCRIPT = re.compile(r"<script\b[^>]*></script>\n?")
REFRESH = re.compile(r'<meta http-equiv="refresh"[^>]*>\n?')
FILENAME = re.compile(r'filename="([^"]+)"')
SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class SiteError(Exception):
    """The site cannot be written as it is: a link leads to a missing page or to something the site has no file for."""


Key = tuple[str, tuple[tuple[str, str], ...]]   # (path, query without lang and without the default view of a list)


def page_key(url: str) -> Key:
    """The page an address of the app stands for. lang is the tree, not part of the page; the first page of the
    default size is the list itself."""
    parts = urlsplit(url)
    query = sorted(((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "lang"),
                   key=lambda item: (item[0] != "per", item))  # per first: index-per10-page2.html
    if query == [("per", str(DEFAULT_SIZE)), ("page", "1")]:
        query = []
    return parts.path, tuple(query)


def file_name(key: Key, attachment: str | None = None) -> str:
    """The file of a page, relative to the tree: /cv/1 is cv/1/index.html, /cv/1/matches?per=10&page=2 is
    cv/1/matches/index-per10-page2.html, a download is a file under the name the app gives it."""
    path, query = key
    parts = [p for p in path.split("/") if p]
    suffix = "".join(f"-{k}{v}" for k, v in query)
    if any(not SAFE_NAME.fullmatch(p) for p in parts) or (suffix and not SAFE_NAME.fullmatch(suffix[1:])):
        raise SiteError(f"no file name for the address {path}?{urlencode(query)}")
    if attachment is not None:
        if not SAFE_NAME.fullmatch(attachment):
            raise SiteError(f"no file name for the download {attachment}")
        return "/".join([*parts[:-1], attachment])
    return "/".join([*parts, f"index{suffix}.html"])


def fmt_utc(value: datetime | str | None, fmt: str = deps.TIME_FORMAT) -> str:
    """deps.fmt_time in UTC: the times of the costs page do not depend on the time zone of the build."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).strftime(fmt)


class FixedDay(date):
    @classmethod
    def today(cls):
        return SITE_DAY


@contextmanager
def fixed_clock() -> Iterator[None]:
    """The date of the export and the time zone of the times are fixed while the pages are rendered."""
    saved = (deps.fmt_time, pages_cv.date, export.date)
    deps.fmt_time, pages_cv.date, export.date = fmt_utc, FixedDay, FixedDay
    try:
        yield
    finally:
        deps.fmt_time, pages_cv.date, export.date = saved


class Crawl:
    """The pages of one language: the answer (HTML, or the bytes of a download) by page key, found by following every
    link from the start page."""

    def __init__(self, client, lang: str):
        self.client, self.lang = client, lang
        self.pages: dict[Key, bytes] = {}
        self.files: dict[Key, str] = {}

    def run(self) -> None:
        queue = ["/"]
        while queue:
            url = queue.pop()
            key = page_key(url)
            if key in self.pages:
                continue
            response = self.client.get(url, follow_redirects=False)
            if response.status_code != 200:
                raise SiteError(f"{self.lang}: {url} answers {response.status_code}")
            disposition = FILENAME.search(response.headers.get("content-disposition", ""))
            self.pages[key] = response.content
            self.files[key] = file_name(key, disposition.group(1) if disposition else None)
            if response.headers["content-type"].startswith("text/html"):
                queue.extend(self.links(response.text))

    @staticmethod
    def links(content: str) -> Iterator[str]:
        for match in ATTRIBUTE.finditer(content):
            value = html.unescape(match["value"])
            if value.startswith("/") and not value.startswith(("/static/", "/lang/", "//")):
                yield value


def relative(target: str, source: str) -> str:
    """The path from the file `source` to the file `target`, both relative to the root of the site."""
    return posixpath.relpath(target, posixpath.dirname(source) or ".")


class Writer:
    def __init__(self, crawls: dict[str, Crawl], repo_url: str):
        self.crawls, self.repo_url, self.texts = crawls, repo_url, load_texts()

    def target(self, lang: str, url: str, source: str) -> str:
        """The relative link from the page `source` (a path in the site) to the page of `url` in the tree `lang`."""
        key = page_key(url)
        files = self.crawls[lang].files
        if key not in files:
            raise SiteError(f"{source}: the link {url} leads to a page that is not in the site")
        fragment = urlsplit(url).fragment
        return relative(f"{lang}/{files[key]}", source) + (f"#{fragment}" if fragment else "")

    def rewrite_links(self, content: str, lang: str, source: str) -> str:
        def replace(match: re.Match) -> str:
            name, value = match["name"], html.unescape(match["value"])
            if value.startswith("/static/"):
                link = relative(value.lstrip("/"), source)
            elif value.startswith("/lang/"):
                other = urlsplit(value).path.split("/")[2]
                if other not in LANGS:
                    raise SiteError(f"{source}: language switch {value}")
                link = self.target(other, dict(parse_qsl(urlsplit(value).query)).get("next", "/"), source)
            elif value.startswith("/") and not value.startswith("//"):
                link = self.target(lang, value, source)
            elif value.startswith("#"):
                link = value
            elif value == deps.START_GUIDE_URL:    # the hint of the disabled button: the README of this repository
                link = f"{self.repo_url}#quick-start"
            else:
                raise SiteError(f"{source}: {name}={value!r} leaves the site")
            return f'{name}="{html.escape(link)}"'

        return ATTRIBUTE.sub(replace, content)

    @staticmethod
    def disable_forms(content: str) -> str:
        """Nothing on the site can send anything: the forms lose address and method, their controls are disabled."""
        def control(match: re.Match) -> str:
            attrs = match["attrs"]
            if re.search(r'\stype="hidden"|\sdisabled\b', attrs):
                return match[0]
            return f"<{match['tag']}{attrs} disabled>"

        return FORM.sub(lambda form: CONTROL.sub(control, FORM_ATTRIBUTES.sub("", form[0])), content)

    def drop_key_hints(self, lang: str, content: str) -> str:
        """The app's lines "no API key set" talk about running the app; every page of the site says what it is."""
        for key in ("start.no_key", "blocked.no_key"):
            hint = f'<p class="hint">{html.escape(self.texts[lang][key])}</p>'
            content = content.replace(hint + "\n", "").replace(hint, "")
        return content

    def note(self, lang: str, attrs: str = "") -> str:
        text = html.escape(self.texts[lang]["site.note"])
        readme = html.escape(self.texts[lang]["site.readme"])
        link = html.escape(f"{self.repo_url}#readme")
        disclosure = html.escape(self.texts["de"]["site.disclosure"])    # the German wording on every page
        return (f'<p class="site-note" role="note"{attrs}>{text} (<a href="{link}">{readme}</a>)'
                f' <span class="disclosure" lang="de">{disclosure}</span></p>\n')

    def page(self, lang: str, key: Key, source: str) -> bytes:
        raw = self.crawls[lang].pages[key]
        if not source.endswith(".html"):
            return raw     # a download stays as the app wrote it
        content = raw.decode("utf-8")
        content = self.drop_key_hints(lang, SCRIPT.sub("", REFRESH.sub("", content)))
        content = self.disable_forms(content)
        content = self.rewrite_links(content, lang, source)
        end = content.index(">", content.index("<body")) + 1
        return (content[:end] + "\n" + self.note(lang) + content[end:].lstrip("\n")).encode("utf-8")

    def index(self) -> str:
        buttons = "\n".join(
            f'    <a class="button {"button-brand" if lang == "en" else "secondary"}" href="{lang}/index.html"'
            f' hreflang="{lang}" lang="{lang}">{name}</a>' for lang, name in (("en", "English"), ("de", "Deutsch")))
        lead = "\n".join(f'  <p class="lead" lang="{lang}">{html.escape(self.texts[lang]["hero.lead"])}</p>'
                         for lang in ("en", "de"))
        notes = "".join(self.note(lang, f' lang="{lang}"') for lang in ("en", "de"))
        pick = f'{self.texts["en"]["site.pick"]} / {self.texts["de"]["site.pick"]}'
        alt = html.escape(self.texts["en"]["logo.alt"])
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>skillpose</title>
<link rel="icon" href="static/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="static/apple-touch-icon.png">
<link rel="stylesheet" href="static/style.css">
</head>
<body>
{notes}<main class="site-pick">
  <img src="static/logo.svg" alt="{alt}" width="240" height="110">
  <h1>skillpose</h1>
{lead}
  <p>{html.escape(pick)}</p>
  <div class="hero-actions">
{buttons}
  </div>
</main>
</body>
</html>
"""


def build_site(out: Path, repo_url: str = DEFAULT_REPO_URL) -> int:
    """Write the site into out and return the number of HTML pages written (the downloads not counted). The folders
    de, en and static of an earlier build are replaced; anything else in out stays."""
    from fastapi.testclient import TestClient

    # No client factory: the app is the app without a key, whatever the environment holds; it makes no API call.
    scratch = Path(tempfile.mkdtemp(prefix="skillpose-site-"))   # the own database that the app never opens
    config = web_app.WebConfig(db_path=scratch / "unused.sqlite", settings=load_settings(), client_factory=None,
                               allowed_hosts=["testserver"])
    crawls: dict[str, Crawl] = {}
    started = time.monotonic()
    try:
        with fixed_clock(), TestClient(web_app.create_app(config)) as client:
            if client.app.state.skillpose.demo_db is None:
                raise SiteError("no demo data in the package: `skillpose demo build` creates them")
            for lang in LANGS:
                client.cookies.set("lang", lang)
                crawls[lang] = Crawl(client, lang)
                crawls[lang].run()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if set(crawls["de"].files) != set(crawls["en"].files):
        raise SiteError("the two languages have different pages")
    writer = Writer(crawls, repo_url)
    for name in OWN_DIRS:
        shutil.rmtree(out / name, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    count = 1
    for lang, crawl in crawls.items():
        for key, name in sorted(crawl.files.items(), key=lambda item: item[1]):
            path = out / lang / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(writer.page(lang, key, f"{lang}/{name}"))
            count += name.endswith(".html")
    shutil.copytree(web_app.WEB_DIR / "static", out / "static", ignore=shutil.ignore_patterns(*UNUSED_ASSETS))
    (out / "index.html").write_text(writer.index(), encoding="utf-8")
    log.info("static demo site: %d pages in %.1f s", count, time.monotonic() - started)
    return count
