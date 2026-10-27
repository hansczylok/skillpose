"""The web app: server-rendered pages for one user on their own machine.

Every request passes the host check first (a foreign host name may be DNS rebinding), then the guard of security.py
(password, cross-site forms, body size, demo lock). Paid work runs as a background task, one at a time; the others
wait in line, and the line is dropped when the app shuts down.

Without an API key the pages show the demo: the packaged bundles, loaded at start into a store in a temporary folder.
The demo lock keeps it read-only and free: it answers every writing request with 403 before any route (and so any form
check) is reached. With a key the pages show the own data only: no demo store, no switch between the two.
"""

import logging
import mimetypes
import shutil
import tempfile
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import jinja2
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from skillpose.core.config import Settings
from skillpose.demo import demo_data_dir, load_demo
from skillpose.pipeline import BatchProgress, open_run, refresh_matches
from skillpose.store.db import Store
from skillpose.web.deps import StoreDep, app_state, render, start_page, translator
from skillpose.web.i18n import LANGS, load_texts, safe_next
from skillpose.web.pages_cv import router as cv_router
from skillpose.web.pages_jobs import router as jobs_router
from skillpose.web.pages_market import router as market_router
from skillpose.web.security import GuardMiddleware, Scope, writing_request
from skillpose.web.tasks import TaskRunner

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
COOKIE_MAX_AGE = 365 * 24 * 3600

# StaticFiles takes the type from mimetypes, which on Windows reads the registry; an installed program can change the
# type it gives for .svg (a known problem for .js), and with nosniff a wrong type breaks the logo and the icons
mimetypes.add_type("image/svg+xml", ".svg")


@dataclass
class WebConfig:
    db_path: Path
    settings: Settings
    client_factory: Callable[[], object] | None = None    # None: no key, the demo only
    demo_dir: Path | None = None                          # None: package data skillpose/demo_data
    password: str | None = None                           # APP_PASSWORD
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])
    inline_tasks: bool = False                            # tests: tasks run synchronously


@dataclass
class AppState:
    """Everything a route reaches through request.app.state.skillpose."""
    config: WebConfig
    texts: dict[str, dict[str, str]]
    templates: Jinja2Templates
    tasks: TaskRunner
    demo_db: Path | None = None                           # the store of the demo: without a key, when demo data exist
    # batch id -> what the last check of an open batch of the own data found; in memory only: the next check after a
    # restart has it again
    batch_progress: dict[str, BatchProgress] = field(default_factory=dict)

    @property
    def has_key(self) -> bool:
        return self.config.client_factory is not None


router = APIRouter()


def _remember(response: Response, key: str, value: str) -> None:
    response.set_cookie(key, value, max_age=COOKIE_MAX_AGE, path="/", samesite="lax", httponly=True)


@router.get("/healthz")
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@router.get("/", response_class=HTMLResponse)
def start(request: Request, store: StoreDep) -> HTMLResponse:
    return start_page(request, store)


@router.get("/lang/{lang}")
def choose_lang(lang: str, next_page: Annotated[str | None, Query(alias="next")] = None) -> RedirectResponse:
    if lang not in LANGS:
        raise HTTPException(404)
    response = RedirectResponse(safe_next(next_page), status_code=303)
    _remember(response, "lang", lang)
    return response


def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    message = translator(request)("error.not_found") if exc.status_code == 404 else str(exc.detail)
    response = render(request, "error.html", exc.status_code, code=exc.status_code, message=message)
    response.headers.update(exc.headers or {})
    return response


def _invalid_request(request: Request, exc: RequestValidationError) -> Response:
    return render(request, "error.html", 400, code=400, message=translator(request)("error.invalid"))


def demo_lock(scope: Scope) -> Response | None:
    """The lock of the guard: without a key every writing request gets 403 with blocked.html, in the demo and in the
    own data that are shown read-only when there are no demo data. With a key nothing is locked: there is no demo."""
    if not writing_request(scope):
        return None
    request = Request(scope)
    if app_state(request).has_key:
        return None
    return render(request, "blocked.html", 403, message=translator(request)("blocked.no_key"))


def _open_demo(state: AppState) -> Callable[[], None] | None:
    """Load the demo bundles into a store in a fresh temporary folder and set state.demo_db. Returns what removes the
    folder (it also runs at exit, should the app never shut down); None without bundles: no demo mode."""
    folder = Path(tempfile.mkdtemp(prefix="skillpose-demo-"))
    remove = weakref.finalize(state, shutil.rmtree, folder, ignore_errors=True)
    try:
        if load_demo(state.config.demo_dir or demo_data_dir(), folder / "demo.sqlite") == 0:
            remove()
            return None
    except BaseException:
        remove()
        raise
    state.demo_db = folder / "demo.sqlite"
    return remove


def _refresh_matches(config: WebConfig) -> None:
    """Recompute the stored matches of the own data that other rules made, before the first page shows them. Offline: it
    needs no key and makes no call. A database that does not exist yet stays as it is. A failure is logged and the app
    starts anyway, with the matches as they are stored."""
    if not config.db_path.is_file():
        return
    try:
        with Store(config.db_path) as store:
            count = refresh_matches(open_run(config.settings, store, "en"))
    except Exception:
        log.exception("the stored matches could not be recomputed at start; the pages show them as stored")
        return
    if count:
        message = f"recomputed {count} stored matches with the current rules (no API call)"
        log.info(message)
        print(message)


def create_app(config: WebConfig) -> FastAPI:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB_DIR / "templates"), autoescape=True,
                             trim_blocks=True, lstrip_blocks=True)
    state = AppState(config=config, texts=load_texts(), templates=Jinja2Templates(env=env),
                     tasks=TaskRunner(inline=config.inline_tasks))
    remove_demo = None if state.has_key else _open_demo(state)  # with a key the demo is never shown

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if state.has_key:  # the own data are shown, and with a key it is a database that the rules may have outdated
            _refresh_matches(config)
        yield
        state.tasks.close()  # after Ctrl-C nothing that waits may start (and be paid for)
        if remove_demo is not None:
            remove_demo()

    app = FastAPI(title="skillpose", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.skillpose = state
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(router)
    app.include_router(cv_router)
    app.include_router(jobs_router)
    app.include_router(market_router)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    # added last = runs first: the host check comes before everything else
    app.add_middleware(GuardMiddleware, password=config.password, lock=demo_lock)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts)
    return app
