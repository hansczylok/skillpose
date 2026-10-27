"""What every route needs: which store it shows (the demo without a key, the own data with one), the language, and the
page frame.

One Store per request: the dependency opens it and closes it after the response. A background task opens its own
(start_task).
"""

import logging
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from urllib.parse import quote, urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from skillpose.core.llm import Stopped
from skillpose.core.textio import ALLOWED_CV_TYPES
from skillpose.core.vocabulary import Vocabulary
from skillpose.demo import DEMO_MODEL
from skillpose.pipeline import Run, limit_message, open_run
from skillpose.store.db import CvRow, Store
from skillpose.web.i18n import LANGS, pick_lang, translate
from skillpose.web.labels import Proposed
from skillpose.web.tasks import Start

if TYPE_CHECKING:
    from skillpose.web.app import AppState

log = logging.getLogger(__name__)

TIME_FORMAT = "%d.%m.%Y %H:%M"
REPO_URL = "https://github.com/hansczylok/skillpose"    # the project; the demo hint links to its README
START_GUIDE_URL = f"{REPO_URL}#quick-start"             # the install and start section of the README

# The ?msg= values a page may show: key -> (kind, text key). The query never supplies text, only one of these keys.
NOTICES = {
    "saved": ("ok", "notice.saved"),
    "deleted": ("ok", "notice.deleted"),
    "busy": ("error", "notice.busy"),
    "queued": ("ok", "notice.queued"),         # shown only while a task of the page waits (query_notice)
    "delete_busy": ("error", "notice.delete_busy"),
    "cv_known": ("ok", "notice.cv_known"),
    "text_short": ("error", "notice.text_short"),
    "text_long": ("error", "notice.text_long"),
    "text_locked": ("error", "notice.text_locked"),
    "text_busy": ("error", "notice.text_busy"),
    "posting_new": ("ok", "notice.posting_new"),
    "posting_known": ("ok", "notice.posting_known"),
    "uploaded": ("ok", "notice.uploaded"),     # with the counts new, present, skipped (pages_jobs)
    "no_open": ("error", "notice.no_open"),
    "conditions_saved": ("ok", "notice.conditions_saved"),  # next to the form of the answers (profile page)
}


@dataclass
class Source:
    store_path: Path
    demo: bool


def app_state(request: Request) -> "AppState":
    return request.app.state.skillpose


def source(request: Request) -> Source:
    """The demo store without a key (when demo data exist), else the own database. With a key there is no demo: a
    view cookie left from the switch of older versions changes nothing."""
    state = app_state(request)
    demo = not state.has_key and state.demo_db is not None
    return Source(store_path=state.demo_db if demo else state.config.db_path, demo=demo)


def open_store(request: Request) -> Iterator[Store]:
    with Store(source(request).store_path) as store:
        yield store


StoreDep = Annotated[Store, Depends(open_store)]


def request_lang(request: Request) -> str:
    return pick_lang(request.query_params.get("lang"), request.cookies.get("lang"),
                     request.headers.get("accept-language"))


def translator(request: Request) -> Callable[..., str]:
    texts, lang = app_state(request).texts, request_lang(request)
    return lambda key, **params: translate(texts, lang, key, **params)


def fmt_time(value: datetime | str | None, fmt: str = TIME_FORMAT) -> str:
    """Local time of a datetime or of a store timestamp (UTC without offset). Text that is no time stays as it is."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime(fmt)


def _month_usd(db_path: Path) -> float:
    """Spent this month in the own database; no file yet (an error page opens no store) means nothing spent."""
    if not db_path.exists():
        return 0.0
    with Store(db_path) as store:
        return store.month_total_usd()


def _next_path(request: Request) -> str:
    """This page as the target of the language switch, without ?lang= (it would beat the new cookie)."""
    query = [(k, v) for k, v in request.query_params.multi_items() if k != "lang"]
    return request.url.path + (f"?{urlencode(query)}" if query else "")


@dataclass(frozen=True)
class Notice:
    """A message for the place it belongs to; templates show it with notice_box (_macros.html)."""
    key: str
    kind: str   # ok | error
    text: str


def notice_for(request: Request, key: str | None, **params: object) -> Notice | None:
    """The notice of a known key, in the language of the page; None for any other value. params fill the {placeholders}
    of the text."""
    if key not in NOTICES:
        return None
    kind, text_key = NOTICES[key]
    return Notice(key=key, kind=kind, text=translator(request)(text_key, **params))


def query_notice(request: Request) -> Notice | None:
    """The notice a redirect asked for with ?msg=<key>. "queued" only while a task of this page still waits: the
    page reloads itself with the same address, and once the task runs or has ended the task line says more."""
    key = request.query_params.get("msg")
    if key == "queued" and not app_state(request).tasks.waits_for(request.url.path):
        return None
    return notice_for(request, key)


def see_other(path: str, *, fragment: str = "", **query: str) -> RedirectResponse:
    """303 to a page of this site, with a query (?msg=saved) and a #fragment, both encoded."""
    url = path + (f"?{urlencode(query)}" if query else "") + (f"#{quote(fragment, safe='')}" if fragment else "")
    return RedirectResponse(url, status_code=303)


def fmt_usd(value: float, lang: str, digits: int = 2) -> str:
    text = f"{value:.{digits}f}"
    return text.replace(".", ",") if lang == "de" else text


def fmt_int(value: int, lang: str) -> str:
    """A whole number with the thousands separator of the language: 4.500 in German, 4,500 in English."""
    text = f"{value:,}"
    return text.replace(",", ".") if lang == "de" else text


def cost_text(request: Request, usd: float) -> str:
    """An estimate for a button or a sentence: whole cents below one dollar ("about 9 cents"), else dollars."""
    t = translator(request)
    if usd < 1:
        return t("cost.cents", count=max(1, round(usd * 100)))
    return t("cost.usd", usd=fmt_usd(usd, request_lang(request)))


def limit_text(t, run: Run, cost: float, key: str) -> str | None:
    """Why a run of this cost must not start, as the text key (cv.limit, postings.limit, paths.limit) in the language
    of the run; None: it fits into the rest of the monthly limit (pipeline.limit_message decides). Asked when the
    button is clicked and again when the task starts: a task that waited in line may find the rest spent by the tasks
    before it, and the check of each call alone would still let one call go out."""
    if limit_message(run, cost) is None:
        return None
    return t(key, spent=fmt_usd(run.store.month_total_usd(), run.lang),
             limit=fmt_usd(run.settings.monthly_limit_usd, run.lang), cost=fmt_usd(cost, run.lang))


STOPPED_KEYS = {"max_tokens": "cut_off", "refusal": "refused"}


def stopped_text(t, lang: str, prefix: str, stop: Stopped) -> str:
    """An answer that stopped early, as the text <prefix>.cut_off or <prefix>.refused (prefix: cv, paths) in the
    language of the run: what happened, the token limit and what the attempt cost (it was paid). A cost below one
    cent shows four digits, so it does not read as nothing."""
    usd = fmt_usd(stop.cost_usd, lang, 2 if stop.cost_usd >= 0.01 else 4)
    return t(f"{prefix}.{STOPPED_KEYS[stop.reason]}", limit=fmt_int(stop.max_tokens, lang), usd=usd)


def start_task(request: Request, name: str, target: str, work: Callable[[Run], str], *, quiet: bool = False,
               key: str = "") -> Start:
    """Run work(run) in the background as the task `name` of the page `target`, now or after the tasks in line
    (TaskRunner.start: STARTED, QUEUED or REFUSED; key tells two tasks of one name and page apart). quiet: a check
    that runs only when nothing else runs or waits, and that leaves the state of the last task as it was when it
    finds nothing.

    The task opens its own Store and API client when it runs (later, in a thread): the request's store is closed by
    then. work returns the message the task line shows; while it runs, the line shows the phase the run reports
    (Run.progress), in the language of the request. A paid work function checks the monthly limit again first
    (limit_text).
    """
    state = app_state(request)
    config, lang, t = state.config, request_lang(request), translator(request)

    def report(phase: str, **counts: int) -> None:
        state.tasks.report(t(f"progress.{phase}", **counts))

    def task() -> str:
        with Store(config.db_path) as store:
            run = open_run(config.settings, store, lang, client=config.client_factory())
            run.progress = report
            return work(run)

    return state.tasks.start(name, target, task, quiet=quiet, key=key)


def person_label(name: str) -> str:
    """How the demo names a person: a gold-set file name such as "maria_berger.md" shows as "Maria Berger"."""
    for suffix in ALLOWED_CV_TYPES:
        if name.lower().endswith(suffix):
            return name[:-len(suffix)].replace("_", " ").title()
    return name


def ai_model(model: str | None) -> str | None:
    """The stored model name for the data-ai-model attribute; none for the demo, whose "model" is a placeholder."""
    return None if not model or model == DEMO_MODEL else model


def render(request: Request, template: str, status: int = 200, **ctx) -> HTMLResponse:
    """A page in the frame of base.html. Pass cv_id to show the navigation of one CV. Without a key there is no month
    cost: nothing can be spent.

    While a task runs, base.html reloads the page every 3 s so the task line stays current. refresh says whether it
    may: a GET page yes, the answer to a POST never (a reload would be a GET of the address of the form). A page with
    a field to type in or a choice to make passes refresh=False, or the reload would throw the input away. Such a page
    may pass refresh_after=<seconds> instead: app.js then reloads it after that time, but not while a field holds input
    or has the focus."""
    state = app_state(request)
    lang = request_lang(request)
    context = {
        "t": translator(request),
        "lang": lang,
        "langs": LANGS,
        "demo": source(request).demo,
        "demo_available": state.demo_db is not None,
        "has_key": state.has_key,
        "start_guide_url": START_GUIDE_URL,
        "task": state.tasks.current,
        "waiting": state.tasks.waiting,
        "month_usd": _month_usd(state.config.db_path) if state.has_key else None,
        "limit_usd": state.config.settings.monthly_limit_usd,
        "fmt_time": fmt_time,
        "usd": lambda x: fmt_usd(x, lang),
        "next_path": _next_path(request),
        "person_label": person_label,
        "cv_id": None,
        "refresh": request.method == "GET",
        "refresh_after": None,
        **ctx,
    }
    return state.templates.TemplateResponse(request, template, context, status_code=status)


def _query_cv(request: Request) -> int | None:
    """The CV a notice of the start page belongs to (?cv=<id>), or None."""
    try:
        return int(request.query_params.get("cv", ""))
    except ValueError:
        return None


def start_page(request: Request, store: Store, status: int = 200, **ctx) -> HTMLResponse:
    """The start page; a failed upload shows it again with upload_error next to the form. A refused delete from the
    list shows at the row of its CV (notice_cv). The cards of the demo persons name the language of each CV."""
    ctx.setdefault("notice", query_notice(request))
    ctx.setdefault("notice_cv", _query_cv(request))
    ctx.setdefault("refresh", False)  # the upload form: a reload would drop the chosen file
    cvs = store.cvs()
    languages = {}
    if source(request).demo:
        languages = {cv.id: profile.language for cv in cvs if (profile := store.profile(cv.id)) is not None}
    return render(request, "start.html", status, cvs=cvs, cv_languages=languages, **ctx)


@contextmanager
def upload_folder() -> Iterator[Path]:
    """A temporary folder for the bytes of an upload: the readers read files. If it cannot be deleted (on Windows a
    scanner may still hold the file), the request goes on and the log says which folder to delete by hand."""
    folder = Path(tempfile.mkdtemp(prefix="skillpose-upload-"))
    try:
        yield folder
    finally:
        try:
            shutil.rmtree(folder)
        except OSError:
            log.warning("could not delete the temporary folder %s with an uploaded file: delete it by hand", folder,
                        exc_info=True)


def safe_filename(name: str | None) -> str:
    """The name of an uploaded file for display: the last part of a path, no control characters, at most 120
    characters; nothing left (or "." / "..") gives "upload"."""
    base = re.split(r"[/\\]", name or "")[-1]
    base = "".join(c for c in base if not unicodedata.category(c).startswith("C")).strip()[:120]
    return "upload" if base in {"", ".", ".."} else base



def cv_or_404(store: Store, cv_id: int) -> CvRow:
    cv = next((row for row in store.cvs() if row.id == cv_id), None)
    if cv is None:
        raise HTTPException(404)
    return cv


def is_locked(request: Request) -> bool:
    """No API key: the pages (the demo, or the own data when there are no demo data) show everything but offer no
    change."""
    return not app_state(request).has_key


def cv_name(request: Request, cv: CvRow) -> str:
    return person_label(cv.name) if source(request).demo else cv.name


def skill_category(vocab: Vocabulary, skill_id: str, proposed: Proposed | None = None) -> str | None:
    """The category of a skill, from the vocabulary or else from Claude's proposal; None: neither knows the id."""
    known = vocab.get(skill_id)
    if known is not None:
        return known.category
    proposal = (proposed or {}).get(skill_id)
    return None if proposal is None else proposal.category
