"""The pages of the postings, of one posting and of the matches of one CV.

Postings are pasted or uploaded (free), analyzed by a background task (paid) and then matched with every CV that has a
profile. From batch_threshold open postings the first batch_single_first of them are analyzed at once and the rest go
out as batch_groups Message Batches. Their ids are stored when they are submitted, so the next visit of the page
"Postings" collects them, also after the browser was closed or the server restarted: the page starts the task collect
when a batch is open, and the task asks the API whether each batch has ended. The answer also says how many requests
are still processed: the page shows that as one progress line per open batch. Nothing is paid twice: a task makes its
plan itself, when it runs, and postings that sit in an open batch are not in the plan.

The demo shows each posting in the language of the interface where it has a translation (made by Claude, marked as
such, the original one click away); the own data are never translated.
"""

import csv
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import anthropic
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from skillpose.core.language import other_language
from skillpose.core.llm import BudgetExceeded
from skillpose.core.models import (
    NOT_COUNTED,
    Evidence,
    JobMatch,
    JobProfile,
    Requirement,
    RequirementMatch,
    text_hash,
)
from skillpose.core.textio import POSTING_TYPES, read_postings
from skillpose.core.vocabulary import Vocabulary, proposals_of
from skillpose.pipeline import (
    BatchProgress,
    PostingPlan,
    Run,
    analyze_postings,
    collect_batches,
    estimate,
    ingest_postings,
    merge_proposals,
    open_run,
    plan_postings,
    rematch,
)
from skillpose.store.db import CvRow, PostingRow, Store
from skillpose.web.deps import (
    Notice,
    StoreDep,
    ai_model,
    app_state,
    cost_text,
    cv_name,
    cv_or_404,
    fmt_time,
    fmt_usd,
    is_locked,
    limit_text,
    notice_for,
    query_notice,
    render,
    request_lang,
    safe_filename,
    see_other,
    source,
    start_task,
    translator,
    upload_folder,
)
from skillpose.web.i18n import reason_text
from skillpose.web.labels import Proposed, percent, role_label, skill_name, tier_rules
from skillpose.web.paging import paginate
from skillpose.web.tasks import Start

log = logging.getLogger(__name__)
router = APIRouter()

UPLOAD_TYPES = (*POSTING_TYPES, ".json", ".csv")
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # per file; the guard middleware caps the whole request at 6 MB
FIXED_NAME = "posting"              # an uploaded file is written under this name, never under the one the browser sent
REF_FROM_TEXT = 60                  # a reference taken from the first line of a pasted text
REF_TYPED = 120
GAPS_SHOWN = 3
RECHECK_SECONDS = 30                # while a batch is open (the text postings.batch_running says "30 s")
TASK_SECONDS = 3                    # while a task runs: the task line stays current
# the message of ingest_postings for a posting that is too long; the page shows it in its own language
SKIPPED = re.compile(r"skipped (?P<ref>.*): (?P<chars>\d+) characters, limit (?P<limit>\d+)", re.DOTALL)
GAP_ORDER = {"gap": 0, "partial": 1, "met": 2, "unanswered": 3, "unverifiable": 4}
STATUS_MARK = {"analyzed": "ok", "open": "muted", "batch": "warn"}


@dataclass
class PostingView:
    hash: str
    title: str         # the title Claude read; before that the first reference
    ref: str | None    # the first reference, shown below a title
    role: str | None
    status: str        # analyzed | open | batch
    mark: str          # the colour class of the status mark
    error: str | None
    created: str
    translated: bool = False  # the title is a translation (the demo only)


@dataclass
class PostingRequirement:
    name: str
    importance: str
    kind: str
    min_level: str | None
    min_years: str | None  # as text: 5 and 2.5 ("2,5" in German)
    quote: str


@dataclass
class MatchRow:
    hash: str
    title: str
    role: str
    tier: int
    must: int          # per cent; without a must requirement the matcher counts all requirements, tier and order use it
    nice: int | None   # per cent; None (a dash): no must or no nice requirement (without must it only repeats must)
    gaps: list[str]
    must_coverage: float
    translated: bool = False  # the title is a translation (the demo only)


@dataclass
class RequirementView:
    anchor: str
    name: str
    importance: str
    status: str
    gap_type: str | None
    weeks: int | None
    evidence: Evidence | None
    posting_quote: str
    reason: str
    block: str = "requirements"  # requirements | unverifiable (a soft skill) | conditions
    asks: str | None = None      # a condition: what the posting asks, e.g. "Travel: ca. 30 %"


def _clean(text: str, limit: int) -> str:
    """One line to show: control characters and runs of white space become one space."""
    spaced = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    return " ".join(spaced.split())[:limit]


def _count(value: str | None) -> int:
    return int(value) if value and value.isascii() and value.isdigit() and len(value) <= 7 else 0


def _skip_text(t, message: str) -> str:
    """The message of ingest_postings for a posting that is too long, in the language of the page."""
    found = SKIPPED.fullmatch(message)
    return message if found is None else t("postings.too_long", **found.groupdict())


def _stored_hashes(store: Store) -> set[str]:
    return {row.hash for row in store.postings()}


# --- the page of the postings ---

def _plan(request: Request, store: Store) -> tuple[Run, PostingPlan, float]:
    """What the button would start and what it would cost, from a run without API client. Only for the estimate and
    the decision to ask: the task makes its own plan."""
    run = open_run(app_state(request).config.settings, store, request_lang(request))
    plan = plan_postings(run, store.open_postings())
    return run, plan, estimate(run, plan=plan)


def _query_notice(request: Request) -> Notice | None:
    if request.query_params.get("msg") == "uploaded":
        counts = {name: _count(request.query_params.get(name)) for name in ("new", "present", "skipped")}
        return notice_for(request, "uploaded", **counts)
    return query_notice(request)


def _translation(request: Request, store: Store, posting_hash: str) -> tuple[str, str] | None:
    """(title, text) of the posting in the language of the page. Only the demo shows translations: the own data show
    what the user gave, as it is."""
    if not source(request).demo:
        return None
    return store.translation(posting_hash, request_lang(request))


def _translated_title(request: Request, store: Store, posting_hash: str) -> str | None:
    translation = _translation(request, store, posting_hash)
    return translation[0] if translation else None


def _view(t, row: PostingRow, title: str | None = None) -> PostingView:
    """title: the translated title, if the page shows one."""
    first = row.refs[0] if row.refs else None
    return PostingView(
        hash=row.hash, title=title or row.title or first or row.hash, ref=first if row.title else None,
        role=role_label(t, row.role_family) if row.role_family else None, status=row.status,
        mark=STATUS_MARK[row.status], error=row.error, created=row.created, translated=title is not None)


def _views(request: Request, store: Store) -> list[PostingView]:
    """The postings, newest first."""
    t = translator(request)
    return [_view(t, row, _translated_title(request, store, row.hash)) for row in reversed(store.postings())]


def _progress_line(t, progress: BatchProgress | None, number: int, count: int) -> str:
    """How far one of count open batches is (number: its place in the order they went out)."""
    if progress is None:
        return t("postings.batch_waiting_of", number=number, batches=count)
    values = {"done": progress.done, "total": progress.total, "time": fmt_time(progress.checked, "%H:%M")}
    line = (t("postings.batch_progress", **values) if count == 1
            else t("postings.batch_progress_of", number=number, batches=count, **values))
    return f"{line} – {t('postings.batch_failed', failed=progress.failed)}" if progress.failed else line


def _batch_lines(request: Request, store: Store) -> list[str]:
    """One progress line per open batch, from what the last check found; before the first check (or after a
    restart) the one line that says that the page checks. Only for the own data: the demo is never checked."""
    pending = store.pending_batches()
    if not pending:
        return []
    t, known = translator(request), app_state(request).batch_progress
    progress = [known.get(batch_id) for batch_id, _ in pending]
    if not any(progress):
        return [t("postings.batch_running", count=len(pending))]
    return [_progress_line(t, p, number, len(pending)) for number, p in enumerate(progress, 1)]


def _page(request: Request, store: Store, status: int = 200, *, notice: Notice | None = None,
          paste_error: str | None = None, paste_text: str = "", paste_ref: str = "",
          upload_problems: list[str] | None = None, analyze_error: str | None = None,
          checking: bool = False) -> Response:
    """The page of the postings. The keyword arguments are what a refused form shows again: the message next to the
    form, and what the user typed. checking: this request started the task collect, so a running task is only the
    check of the batch and no reason to reload soon (it would start the next check)."""
    state = app_state(request)
    locked = is_locked(request)
    batch_open = not locked and bool(store.pending_batches())
    open_count, cost_label = 0, ""
    if not locked:
        _, plan, cost = _plan(request, store)
        open_count, cost_label = len(plan.open), cost_text(request, cost)
    reload_after = None
    if request.method == "GET" and not locked:
        # not the meta reload of base.html: this page has a text field and a file field (app.js keeps their input)
        busy = state.tasks.running and not checking
        reload_after = TASK_SECONDS if busy else RECHECK_SECONDS if batch_open else None
    page = paginate(request, _views(request, store), "/postings", lambda view: view.hash)
    return render(request, "postings.html", status, rows=page.items, page=page, locked=locked,
                  batch_lines=_batch_lines(request, store) if batch_open else [],
                  open_count=open_count, cost_label=cost_label, threshold=state.config.settings.batch_threshold,
                  notice=notice or _query_notice(request), paste_error=paste_error, paste_text=paste_text,
                  paste_ref=paste_ref, upload_problems=upload_problems or [], analyze_error=analyze_error,
                  refresh=locked, refresh_after=reload_after)


def _collect_due(request: Request, store: Store) -> bool:
    """Collect a batch now? Only with a key, only when a batch is open and no task runs (none waits then either),
    and not for a request that another site caused (an image or a link on a foreign page)."""
    return (not is_locked(request) and not app_state(request).tasks.running
            and request.headers.get("sec-fetch-site", "").lower() != "cross-site" and bool(store.pending_batches()))


def _rematch_all(run: Run, cv_ids: list[int], t) -> str:
    """Match every CV that has a profile with all analyzed postings. The note if the monthly limit stops it."""
    try:
        for cv_id in cv_ids:
            rematch(run, cv_id)
    except BudgetExceeded:
        return t("taskdone.match_limit")
    return ""


def _collect(run: Run, t, progress: dict[str, BatchProgress]) -> str:
    """The task collect: take the batches that have ended, then match their postings with every CV that has a
    profile. "": no batch has ended yet, the next visit asks again. progress gets what the check found about each
    batch that still runs; a batch that is no longer open leaves it. An API error during the check (e.g. the status
    request of batch 2 after batch 1 was taken) still lets the batches taken before it be matched and their proposals
    reach the vocabulary: they are finished, so the next check no longer sees them. Without such a batch the error
    ends the task, as before."""
    store = run.store
    before = store.pending_batches()
    error: anthropic.AnthropicError | None = None
    try:
        collect_batches(run, wait=False, progress=lambda p: progress.__setitem__(p.batch_id, p))
    except anthropic.AnthropicError as e:  # a lost batch never comes here (collect_batches gives it up)
        error = e
    finally:
        still_open = {batch_id for batch_id, _ in store.pending_batches()}
        for batch_id in [b for b in progress if b not in still_open]:
            progress.pop(batch_id, None)
    hashes = [h for batch_id, batch_hashes in before if batch_id not in still_open for h in batch_hashes]
    if not hashes:
        if error is not None:
            raise error
        return ""
    cv_ids = [cv.id for cv in store.cvs() if cv.analyzed]
    done = sum(store.job_profile(h) is not None for h in hashes)
    try:
        note = _rematch_all(run, cv_ids, t)
    finally:
        merge_proposals(run, cv_ids, hashes)
    values = {"done": done, "failed": len(hashes) - done}
    collected = (t("taskdone.collect", **values) if error is None
                 else t("taskdone.collect_partly", error=str(error) or type(error).__name__, **values))
    return " ".join(filter(None, [collected, note]))


def _sent_text(t, plan: PostingPlan, done: int) -> str:
    """What the task postings did with a plan that has batches."""
    batches = t("count.batches", count=len(plan.groups))
    if plan.single:
        return t("taskdone.split", analyzed=t("count.postings", count=done), sent=plan.batched, batches=batches)
    if len(plan.groups) == 1:
        return t("taskdone.batch", postings=t("count.postings", count=plan.batched))
    return t("taskdone.batches", postings=t("count.postings", count=plan.batched), batches=batches)


def _analyze_open(run: Run, t, asked: int) -> str:
    """The task postings: step 2 for the postings that are open now; from batch_threshold of them the first at once
    and the rest as batches (plan_postings). The batches go out first, then the single calls, whose results are
    matched with every CV that has a profile right away. The plan is made here and not in the request: another task
    may have analyzed some postings in between, and they must not be paid for twice. asked: the number of open
    postings the request counted (and the user confirmed); more than that are not sent, fewer are cheaper. The plan
    must fit into the rest of the monthly limit now: the tasks before this one may have spent it while it waited."""
    store = run.store
    plan = plan_postings(run, store.open_postings())
    if not plan.open:
        return t("taskdone.nothing_open")
    if len(plan.open) > asked:
        return t("taskdone.recount", now=len(plan.open), asked=asked)
    if (refusal := limit_text(t, run, estimate(run, plan=plan), "postings.limit")) is not None:
        raise RuntimeError(refusal)
    cv_ids = [cv.id for cv in store.cvs() if cv.analyzed]
    try:
        outcome = analyze_postings(run, plan, wait=False)  # batches are only submitted; the page collects them later
        note = _rematch_all(run, cv_ids, t) if plan.single else ""
        done = (_sent_text(t, plan, outcome.done) if plan.batch
                else t("taskdone.postings", done=outcome.done, left=outcome.still_open))
        return " ".join(filter(None, [done, note]))
    except BudgetExceeded:
        open_now = set(store.open_postings()) - {h for _, hashes in store.pending_batches() for h in hashes}
        return t("taskdone.limit", count=len(open_now & set(plan.open)))  # a batch that went out is not open
    finally:
        merge_proposals(run, cv_ids, plan.open)


@router.get("/postings", response_class=HTMLResponse)
def postings_page(request: Request, store: StoreDep) -> Response:
    checking = False
    if _collect_due(request, store):
        t, progress = translator(request), app_state(request).batch_progress
        checking = start_task(request, "collect", "/postings", lambda run: _collect(run, t, progress),
                              quiet=True) is Start.STARTED
    return _page(request, store, checking=checking)


# --- paste and upload ---

@router.post("/postings/text")
def paste(request: Request, store: StoreDep, text: Annotated[str, Form()] = "",
          ref: Annotated[str, Form()] = "") -> Response:
    t = translator(request)
    text = text.replace("\r\n", "\n")  # a browser sends the line breaks of a text field as CRLF
    posting = text.strip()

    def refuse(message: str) -> Response:
        return _page(request, store, 400, paste_error=message, paste_text=text, paste_ref=ref)

    if not posting:
        return refuse(t("postings.paste_empty"))
    first_line = next(line for line in posting.splitlines() if line.strip())
    name = _clean(ref, REF_TYPED) or _clean(first_line, REF_FROM_TEXT)
    known = text_hash(posting) in _stored_hashes(store)
    run = open_run(app_state(request).config.settings, store, request_lang(request))
    _, skipped = ingest_postings(run, [(name, posting)])
    if skipped:
        return refuse(_skip_text(t, skipped[0]))
    return see_other("/postings", msg="posting_known" if known else "posting_new")


def _read_upload(data: bytes, name: str, suffix: str) -> list[tuple[str, str]]:
    """The postings of an uploaded file. read_postings reads files, so the bytes go to a temporary folder, under a fixed
    name (on Windows, "CON.md" is a device). The references that name that file show the name the user sent."""
    with upload_folder() as folder:
        path = folder / f"{FIXED_NAME}{suffix}"
        path.write_bytes(data)
        items = read_postings([path])
    fixed = path.name

    def shown(ref: str) -> str:
        if ref == fixed or ref.startswith(f"{fixed}#"):
            ref = name + ref[len(fixed):]
        return _clean(ref, REF_TYPED)

    return [(shown(ref), text) for ref, text in items]


@router.post("/postings/upload")
def upload(request: Request, store: StoreDep,
           files: Annotated[list[UploadFile | str] | None, File()] = None) -> Response:
    t = translator(request)
    # a browser sends one empty part when no file was chosen; the multipart parser reads it as a plain text field
    chosen = [file for file in files or [] if not isinstance(file, str) and file.filename]
    if not chosen:
        return _page(request, store, 400, upload_problems=[t("postings.upload_missing")])
    items: list[tuple[str, str]] = []
    problems: list[str] = []
    for file in chosen:
        name = safe_filename(file.filename)
        suffix = Path(name).suffix.lower()
        if suffix not in UPLOAD_TYPES:
            problems.append(t("postings.upload_type", name=name, suffix=suffix or "(none)"))
            continue
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            problems.append(t("postings.upload_size", name=name))
            continue
        try:
            items += _read_upload(data, name, suffix)
        except (ValueError, csv.Error) as e:  # no text column, invalid JSON, a CSV field over the csv module's limit
            detail = str(e).replace(f"{FIXED_NAME}{suffix}", name)
            problems.append(t("postings.upload_format", name=name, detail=detail))
        except Exception as e:  # JSON of another shape, too deep (RecursionError): one file must not cost the rest
            # the type only, no traceback: the text of an error may quote the file
            log.warning("an uploaded posting file could not be read: %s", type(e).__name__)
            problems.append(t("postings.upload_format", name=name, detail=t("postings.upload_structure")))
    if not items and problems:
        return _page(request, store, 400, upload_problems=problems)
    run = open_run(app_state(request).config.settings, store, request_lang(request))
    before = _stored_hashes(store)
    hashes, skipped = ingest_postings(run, items)
    new = sum(h not in before for h in hashes)
    empty = sum(not text.strip() for _, text in items)
    counts = {"new": new, "present": len(items) - empty - len(skipped) - new, "skipped": empty + len(skipped)}
    problems += [_skip_text(t, message) for message in skipped]
    if not problems:
        return see_other("/postings", msg="uploaded", **{name: str(value) for name, value in counts.items()})
    stored_any = new + counts["present"] > 0
    return _page(request, store, 200 if stored_any else 400, notice=notice_for(request, "uploaded", **counts),
                 upload_problems=problems)


# --- analysis ---

@router.post("/postings/analyze")
def analyze(request: Request, store: StoreDep, confirm: Annotated[str | None, Form()] = None,
            count: Annotated[str | None, Form()] = None) -> Response:
    """Start step 2 for the open postings; above confirm_above_usd only with a confirmation of this very number of
    postings (count: the number the confirm page asked about; another tab may have added postings since)."""
    t, lang = translator(request), request_lang(request)
    run, plan, cost = _plan(request, store)
    if not plan.open:
        return see_other("/postings", msg="no_open")
    if (error := limit_text(t, run, cost, "postings.limit")) is not None:
        return _page(request, store, 400, analyze_error=error)
    asked = len(plan.open)
    if cost > run.settings.confirm_above_usd and not (confirm and count == str(asked)):
        question = t("postings.confirm_question", count=asked, cost=fmt_usd(cost, lang),
                     threshold=fmt_usd(run.settings.confirm_above_usd, lang))
        return render(request, "confirm.html", action="/postings/analyze", question=question, cost=cost,
                      busy=t("postings.analyzing"), back="/postings", fields={"count": str(asked)},
                      note=t("postings.confirm_changed") if confirm and count else None)
    status = start_task(request, "postings", "/postings", lambda r: _analyze_open(r, t, asked))
    if status is Start.REFUSED:
        return see_other("/postings", msg="busy")
    if status is Start.QUEUED:
        return see_other("/postings", msg="queued")
    return see_other("/postings")


# --- one posting ---

def _years(value: float, lang: str) -> str:
    text = f"{value:g}"
    return text.replace(".", ",") if lang == "de" else text


def _requirement_view(r: Requirement, vocab: Vocabulary, lang: str, proposed: Proposed) -> PostingRequirement:
    return PostingRequirement(name=skill_name(vocab, r.skill_id, lang, proposed), importance=r.importance,
                              kind=r.kind, min_level=r.min_level,
                              min_years=_years(r.min_years, lang) if r.min_years else None, quote=r.quote)


def _match_context(request: Request, store: Store, posting_hash: str) -> CvRow | None:
    """The CV the visitor came from (?cv=<id>), if it exists and has a match for this posting: the page then links back
    to that match. Any other value is ignored."""
    cv_id = _count(request.query_params.get("cv"))
    cv = next((row for row in store.cvs() if row.id == cv_id), None) if cv_id else None
    if cv is None or not any(m.text_hash == posting_hash for m in store.matches(cv.id)):
        return None
    return cv


@router.get("/postings/{posting_hash}", response_class=HTMLResponse)
def posting_page(request: Request, store: StoreDep, posting_hash: str) -> Response:
    """One posting: its full text and, once analyzed, the requirements Claude read from it. Nothing here writes."""
    row = next((r for r in store.postings() if r.hash == posting_hash), None)
    if row is None:
        raise HTTPException(404)
    t, lang = translator(request), request_lang(request)
    jp = store.job_profile(posting_hash)
    requirements, seniority = [], None
    if jp is not None:
        vocab = Vocabulary.load(extra=store.added_skills())
        proposed = proposals_of(jp)
        requirements = [_requirement_view(r, vocab, lang, proposed) for r in jp.requirements]
        requirements.sort(key=lambda r: r.importance != "must")  # stable: the order of the posting stays
        seniority = jp.seniority
    cv = _match_context(request, store, posting_hash)
    text, translation = store.posting_text(posting_hash), _translation(request, store, posting_hash)
    return render(request, "posting.html", cv_id=cv.id if cv else None,
                  view=_view(t, row, translation[0] if translation else None), refs=row.refs,
                  text=translation[1] if translation else text, original=text if translation else None,
                  original_lang=other_language(lang), analyzed=jp is not None, seniority=seniority,
                  requirements=requirements, ai_model=ai_model(store.job_profile_model(posting_hash)),
                  match_link=f"/cv/{cv.id}/matches/{posting_hash}" if cv else None)


# --- the matches of one CV ---

def _gaps(jm: JobMatch, vocab: Vocabulary, lang: str, proposed: Proposed) -> list[str]:
    """The skills that are not met, the most important first: must before nice, a gap before a partial match. One entry
    per skill, at most GAPS_SHOWN. A soft skill is no gap, and a condition is no skill to learn (one that is not met
    has its own line on the page of the match, as in the export and the market view)."""
    unmet = sorted((m for m in jm.matches if m.status not in ("met", *NOT_COUNTED)
                    and m.requirement.kind not in ("soft", "condition")),
                   key=lambda m: (m.requirement.importance != "must", GAP_ORDER[m.status]))
    return list(dict.fromkeys(skill_name(vocab, m.requirement.skill_id, lang, proposed) for m in unmet))[:GAPS_SHOWN]


def _row(t, jm: JobMatch, jp: JobProfile, vocab: Vocabulary, lang: str, proposed: Proposed,
         title: str | None = None) -> MatchRow:
    """title: the translated title, if the page shows one."""
    importances = {r.importance for r in jp.requirements}
    return MatchRow(hash=jm.text_hash, title=title or jp.title, role=role_label(t, jp.role_family), tier=jm.tier,
                    must=percent(jm.must_coverage),
                    nice=percent(jm.nice_coverage) if {"must", "nice"} <= importances else None,
                    gaps=_gaps(jm, vocab, lang, proposed), must_coverage=jm.must_coverage,
                    translated=title is not None)


def _block(m: RequirementMatch) -> str:
    """Where a requirement is shown on the page of a match: a soft skill is not verifiable from the CV, whatever status
    an older run stored for it; a condition is checked against the answers in the profile."""
    if m.requirement.kind == "soft" or m.status == "unverifiable":
        return "unverifiable"
    return "conditions" if m.requirement.kind == "condition" else "requirements"


def _asks(t, r: Requirement, shown_name: str) -> str | None:
    """What a posting asks for with a condition, as one line under its name: the kind of condition and the value as the
    posting says it ("Travel: ca. 30 %"). The kind is left out when the requirement already carries that name; None
    when nothing is left."""
    kind = t(f"conditions.key.{r.condition}") if r.condition else None
    return ": ".join(part for part in (kind if kind != shown_name else None, r.condition_value) if part) or None


@router.get("/cv/{cv_id}/matches", response_class=HTMLResponse)
def matches_page(request: Request, store: StoreDep, cv_id: int) -> Response:
    cv = cv_or_404(store, cv_id)
    t, lang = translator(request), request_lang(request)
    vocab = Vocabulary.load(extra=store.added_skills())
    profile = store.profile(cv_id)
    rows = [_row(t, jm, jp, vocab, lang, proposals_of(jp, profile), _translated_title(request, store, jm.text_hash))
            for jm in store.matches(cv_id) if (jp := store.job_profile(jm.text_hash)) is not None]
    rows.sort(key=lambda r: (r.tier, -r.must_coverage, r.title.casefold()))
    page = paginate(request, rows, f"/cv/{cv_id}/matches", lambda row: row.hash)
    return render(request, "matches.html", cv_id=cv_id, cv=cv, name=cv_name(request, cv), rows=page.items, page=page,
                  tier_rules=tier_rules(app_state(request).config.settings))


@router.get("/cv/{cv_id}/matches/{posting_hash}", response_class=HTMLResponse)
def match_page(request: Request, store: StoreDep, cv_id: int, posting_hash: str) -> Response:
    cv = cv_or_404(store, cv_id)
    jm = next((m for m in store.matches(cv_id) if m.text_hash == posting_hash), None)
    jp = store.job_profile(posting_hash)
    if jm is None or jp is None:
        raise HTTPException(404)
    t, lang = translator(request), request_lang(request)
    vocab = Vocabulary.load(extra=store.added_skills())
    proposed = proposals_of(jp, store.profile(cv_id))
    seen: dict[str, int] = {}
    views = []
    for m in jm.matches:
        skill_id = m.requirement.skill_id
        seen[skill_id] = seen.get(skill_id, 0) + 1
        shown = skill_name(vocab, skill_id, lang, proposed)
        views.append(RequirementView(
            anchor=f"req-{skill_id}" if seen[skill_id] == 1 else f"req-{skill_id}-{seen[skill_id]}",
            name=shown, importance=m.requirement.importance, status=m.status,
            gap_type=m.gap_type, weeks=m.effort_weeks if m.gap_type else None, evidence=m.evidence,
            posting_quote=m.requirement.quote, reason=reason_text(m.reason, lang), block=_block(m),
            asks=_asks(t, m.requirement, shown) if m.requirement.kind == "condition" else None))
    views.sort(key=lambda v: (v.importance != "must", GAP_ORDER[v.status]))  # stable: the order of the posting stays
    row = _row(t, jm, jp, vocab, lang, proposed, _translated_title(request, store, posting_hash))
    blocks = {name: [v for v in views if v.block == name] for name in ("requirements", "unverifiable", "conditions")}
    return render(request, "match_detail.html", cv_id=cv_id, name=cv_name(request, cv), row=row, **blocks,
                  ai_model=ai_model(store.job_profile_model(posting_hash)))
