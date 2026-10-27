"""The pages of one CV: upload, text check, analysis, profile and the Markdown export.

The reading pages show source(request): the demo store without a key, the own one with a key. The writing routes are
reached only with a key (the demo lock answers 403 before them). The paid analysis runs as a background task that
opens its own store and API client (deps.start_task).

The profile page also holds the answers to the conditions of postings (driving licence, travel, shift work, place of
work, working time): one record per database, so one form for every CV. A save recomputes the matches of every CV as
a task without API client (pipeline.rematch_offline). Without a key the page lists the answers and offers no form.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, get_args

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from skillpose.core.llm import Stopped
from skillpose.core.models import LEVEL_ORDER, CandidateProfile, CandidateSkill, Category, Conditions, Level
from skillpose.core.textio import ALLOWED_CV_TYPES, MAX_CV_BYTES, MIN_CV_CHARS, CvTextError, read_cv
from skillpose.core.vocabulary import Vocabulary, proposals_of
from skillpose.export import ExportError, build_export, export_filename
from skillpose.pipeline import Run, ensure_profile, estimate, merge_proposals, open_run, rematch, rematch_offline
from skillpose.store.db import CvRow, Store
from skillpose.web.deps import (
    Notice,
    StoreDep,
    ai_model,
    app_state,
    cost_text,
    cv_name,
    cv_or_404,
    fmt_usd,
    is_locked,
    limit_text,
    notice_for,
    query_notice,
    render,
    request_lang,
    safe_filename,
    see_other,
    skill_category,
    source,
    start_page,
    start_task,
    stopped_text,
    translator,
    upload_folder,
)
from skillpose.web.labels import skill_name, stability_note
from skillpose.web.tasks import Start

log = logging.getLogger(__name__)
router = APIRouter()

LEVELS = list(get_args(Level))
CATEGORY_ORDER = [*get_args(Category), "unknown"]  # "unknown": a skill the vocabulary does not know
# the answers to the conditions: a choice (yes, no, not answered) or a whole number up to its limit
CHOICE_FIELDS = ("driving_licence_b", "shift_work", "remote_only", "full_time")
NUMBER_LIMITS = {"travel_share_max": 100, "commute_km_max": 99_999}
FIELD_ORDER = ("driving_licence_b", "travel_share_max", "shift_work", "commute_km_max", "remote_only", "full_time")
NOTES_LIMIT = 1000
ASCII_NUMBER = re.compile(r"[0-9]{1,5}")  # not str.isdigit(): it takes "²"
CONDITIONS_KEY = "conditions"  # tells the rematch task of a save from the one of a skill edit (same page)
CHOICES = {"": None, "1": True, "0": False}


@dataclass
class SkillView:
    skill: CandidateSkill
    name: str
    years: str | None      # as shown: "10", "2,5"


@dataclass
class Group:
    category: str
    skills: list[SkillView]


@dataclass
class ConditionField:
    """One answer, as the form (or the read-only list) shows it."""
    name: str
    kind: str                # choice | number
    value: str               # a choice: "" (not answered), "1" or "0"; a number: digits or "", else what was typed
    error: str | None = None


def _cv_task_running(request: Request, cv_id: int) -> bool:
    """A task of this CV (its analysis or a rematch) is running: its result belongs to the CV as it is now."""
    task = app_state(request).tasks.current
    return task is not None and task.finished is None and task.target == f"/cv/{cv_id}"


def _cv_task_active(request: Request, cv_id: int) -> bool:
    """The analysis or a rematch of this CV runs or waits: a waiting analysis would read a changed text."""
    return _cv_task_running(request, cv_id) or app_state(request).tasks.waits_for(f"/cv/{cv_id}")


def _cv_in_use(request: Request, cv_id: int) -> bool:
    """Any task of this CV runs or waits, its paths too (/cv/{id}/paths): its result would have no CV to belong to.
    A path saved after the delete would fail, or attach to a new CV that got the free id. The task of a save of the
    answers recomputes every CV, from whichever page it was saved: while it runs or waits, no CV can go."""
    tasks = app_state(request).tasks
    return tasks.busy_with(f"/cv/{cv_id}") or tasks.runs_or_waits("rematch", CONDITIONS_KEY)


def _cv_cost(run: Run, cv_id: int, analyzed: bool) -> float:
    """The cost of analyzing a CV; a CV that has its profile costs nothing."""
    return estimate(run, cv_text=None if analyzed else run.store.cv_text(cv_id))


def _estimate(request: Request, store: Store, cv: CvRow) -> tuple[Run, float]:
    """The cost of analyzing a CV, from a run without API client."""
    run = open_run(app_state(request).config.settings, store, request_lang(request))
    return run, _cv_cost(run, cv.id, cv.analyzed)


# --- upload and text ---

def _read_upload(data: bytes, suffix: str) -> str:
    """The text of an uploaded CV. read_cv reads files, so the bytes go to a temporary folder, under a fixed name:
    the name the browser sent is never used as a path (on Windows, "CON.txt" is a device)."""
    with upload_folder() as folder:
        path = folder / f"cv{suffix}"
        path.write_bytes(data)
        return read_cv(path)


@router.post("/cv")
def upload_cv(request: Request, store: StoreDep, file: Annotated[UploadFile | None, File()] = None) -> Response:
    """Store an uploaded CV and show its text for correction. A text that is stored already (exactly, in any file
    type) opens that CV instead: no second CV in the list and nothing to analyze twice."""
    t = translator(request)

    def refuse(key: str, **params: str) -> Response:
        return start_page(request, store, 400, upload_error=t(key, **params))

    if file is None or not file.filename:
        return refuse("upload.missing")
    name = safe_filename(file.filename)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_CV_TYPES:
        return refuse("upload.type", suffix=suffix or "(none)")
    data = file.file.read(MAX_CV_BYTES + 1)
    if len(data) > MAX_CV_BYTES:
        return refuse("upload.size")
    try:
        text = _read_upload(data, suffix)
    except CvTextError:
        return refuse("upload.text")
    except Exception as e:  # a damaged PDF or DOCX: the parsers raise all kinds of errors
        # the type only, no traceback: a parser's error may quote the file (pypdf: about 80 bytes of a damaged PDF)
        log.warning("an uploaded CV could not be read: %s", type(e).__name__)
        return refuse("upload.broken")
    if (known := store.find_cv(text)) is not None:
        page = f"/cv/{known}" if store.profile(known) is not None else f"/cv/{known}/text"
        return see_other(page, msg="cv_known")
    return see_other(f"/cv/{store.save_cv(name, text)}/text")


def _text_page(request: Request, store: Store, cv: CvRow, status: int = 200, *, text: str | None = None,
               notice: Notice | None = None, analyze_error: str | None = None) -> Response:
    """The text of a CV. text: what the user typed, shown again after a refusal."""
    text = store.cv_text(cv.id) if text is None else text
    locked = is_locked(request)
    cost = None if cv.analyzed or locked else _estimate(request, store, cv)[1]
    return render(request, "cv_text.html", status, cv_id=cv.id, cv=cv, name=cv_name(request, cv), text=text,
                  chars=len(text), locked=locked, cost_label=None if cost is None else cost_text(request, cost),
                  notice=notice or query_notice(request), analyze_error=analyze_error, refresh=False)


@router.get("/cv/{cv_id}/text", response_class=HTMLResponse)
def text_page(request: Request, store: StoreDep, cv_id: int) -> Response:
    return _text_page(request, store, cv_or_404(store, cv_id))


@router.post("/cv/{cv_id}/text")
def save_text(request: Request, store: StoreDep, cv_id: int, text: Annotated[str, Form()]) -> Response:
    cv = cv_or_404(store, cv_id)
    text = text.replace("\r\n", "\n")  # a browser sends the line breaks of a text field as CRLF
    refusal = "text_short" if len(text.strip()) < MIN_CV_CHARS else (
        "text_long" if len(text.encode("utf-8")) > MAX_CV_BYTES else None)
    if refusal is None and _cv_task_active(request, cv_id):
        refusal = "text_busy"  # the profile would be made from another text than the one confirmed and shown
    if refusal is not None:
        return _text_page(request, store, cv, 400, text=text, notice=notice_for(request, refusal))
    try:
        store.update_cv_text(cv_id, text)
    except ValueError:  # the profile exists
        return _text_page(request, store, cv, 400, notice=notice_for(request, "text_locked"))
    return see_other(f"/cv/{cv_id}/text", msg="saved")


# --- analysis ---

def _analyze(run: Run, cv_id: int, t) -> str:
    """The task cv: step 1 unless the CV has its profile, then match it with the analyzed postings and add the skills
    Claude proposed to the vocabulary (after the run, so the cached prefix stays the same). First the monthly limit
    again: the tasks before this one may have spent the rest while it waited."""
    refusal = limit_text(t, run, _cv_cost(run, cv_id, run.store.profile(cv_id) is not None), "cv.limit")
    if refusal is not None:
        raise RuntimeError(refusal)
    profile, error = ensure_profile(run, cv_id)
    if isinstance(error, Stopped):  # paid, but cut off or refused: say so, with the limit and the cost
        raise RuntimeError(stopped_text(t, run.lang, "cv", error))
    if profile is None:
        raise RuntimeError(error or "no profile")
    rows = []
    try:
        rows = rematch(run, cv_id)
    finally:
        merge_proposals(run, [cv_id], [jp.text_hash for jp, _ in rows])
    return t("taskdone.cv", skills=t("count.skills", count=len(profile.skills)),
             postings=t("count.postings", count=len(rows)))


@router.post("/cv/{cv_id}/analyze")
def analyze(request: Request, store: StoreDep, cv_id: int, confirm: Annotated[str | None, Form()] = None,
            asked: Annotated[str | None, Form(alias="cost")] = None) -> Response:
    """Start the analysis of a CV; above confirm_above_usd only with a confirmation of this very estimate (the text may
    have changed in another tab since the question)."""
    cv = cv_or_404(store, cv_id)
    t, lang = translator(request), request_lang(request)
    run, cost = _estimate(request, store, cv)
    if (error := limit_text(t, run, cost, "cv.limit")) is not None:
        return _text_page(request, store, cv, 400, analyze_error=error)
    estimate_field = f"{cost:.6f}"
    if cost > run.settings.confirm_above_usd and not (confirm and asked == estimate_field):
        question = t("cv.confirm_question", cost=fmt_usd(cost, lang),
                     threshold=fmt_usd(run.settings.confirm_above_usd, lang))
        return render(request, "confirm.html", cv_id=cv_id, action=f"/cv/{cv_id}/analyze", question=question,
                      cost=cost, busy=t("cv.analyzing"), back=f"/cv/{cv_id}/text", fields={"cost": estimate_field},
                      note=t("cv.confirm_changed") if confirm and asked else None)
    status = start_task(request, "cv", f"/cv/{cv_id}", lambda r: _analyze(r, cv_id, t))
    if status is Start.REFUSED:
        return see_other(f"/cv/{cv_id}/text", msg="busy")
    if status is Start.QUEUED:
        return see_other(f"/cv/{cv_id}", msg="queued")  # the profile page reloads until the profile is there
    return see_other(f"/cv/{cv_id}")


# --- profile ---

def _years(years: float | None, lang: str) -> str | None:
    if years is None:
        return None
    text = f"{years:g}"
    return text.replace(".", ",") if lang == "de" else text


def _groups(profile: CandidateProfile, vocab: Vocabulary, lang: str) -> list[Group]:
    """The skills by category, in the order of the Category type. Within a category: shown skills first, then by
    level, then by name. A skill the vocabulary does not know takes name and category from Claude's proposal."""
    proposed = proposals_of(profile)
    by_category: dict[str, list[SkillView]] = {}
    for skill in profile.skills:
        category = skill_category(vocab, skill.skill_id, proposed) or "unknown"
        by_category.setdefault(category, []).append(SkillView(
            skill=skill, name=skill_name(vocab, skill.skill_id, lang, proposed), years=_years(skill.years, lang)))
    return [Group(category, sorted(by_category[category],
                                   key=lambda v: (v.skill.hidden, -LEVEL_ORDER[v.skill.level], v.name.casefold())))
            for category in CATEGORY_ORDER if category in by_category]


def _form_values(answers: Conditions) -> dict[str, str]:
    """The stored answers as the strings of the form."""
    def choice(value: bool | None) -> str:
        return "" if value is None else "1" if value else "0"

    def number(value: int | None) -> str:
        return "" if value is None else str(value)

    return {"driving_licence_b": choice(answers.driving_licence_b),
            "travel_share_max": number(answers.travel_share_max), "shift_work": choice(answers.shift_work),
            "commute_km_max": number(answers.commute_km_max), "remote_only": choice(answers.remote_only),
            "full_time": choice(answers.full_time), "notes": answers.notes}


def _read_answers(form: dict[str, str], t) -> tuple[Conditions | None, dict[str, str]]:
    """The answers of the form, or None and a message for each wrong field. A number is a whole number from 0 to its
    limit (no sign, no fraction, no exponent); an empty field is not answered."""
    errors: dict[str, str] = {}
    numbers: dict[str, int | None] = {}
    for name, limit in NUMBER_LIMITS.items():
        text = form[name].strip()
        if not text:
            numbers[name] = None
        elif ASCII_NUMBER.fullmatch(text) and int(text) <= limit:
            numbers[name] = int(text)
        else:
            errors[name] = t(f"conditions.error_{name}")
    notes = form["notes"].strip()
    if len(notes) > NOTES_LIMIT:
        errors["notes"] = t("conditions.error_notes", limit=NOTES_LIMIT)
    if errors:
        return None, errors
    return Conditions(**{name: CHOICES[form[name]] for name in CHOICE_FIELDS}, **numbers, notes=notes), {}


def _profile_page(request: Request, store: Store, cv_id: int, status: int = 200, *,
                  answers: dict[str, str] | None = None, errors: dict[str, str] | None = None) -> Response:
    """The profile page. answers, errors: what a refused form shows again, the strings typed and a message per field."""
    cv = cv_or_404(store, cv_id)
    profile = store.profile(cv_id)
    groups = [] if profile is None else _groups(profile, Vocabulary.load(extra=store.added_skills()),
                                                request_lang(request))
    created = None if source(request).demo else store.profile_created(cv_id)  # the demo store is made at start
    values = _form_values(store.conditions()) if answers is None else answers
    errors = errors or {}
    fields = [ConditionField(name, "choice" if name in CHOICE_FIELDS else "number", values[name], errors.get(name))
              for name in FIELD_ORDER]
    locked = is_locked(request)
    return render(request, "profile.html", status, cv_id=cv_id, cv=cv, name=cv_name(request, cv), profile=profile,
                  groups=groups, created=created, locked=locked, levels=LEVELS,
                  stability=stability_note(translator(request), app_state(request).config.settings,
                                            request_lang(request)),
                  ai_model=ai_model(store.profile_model(cv_id)), notice=query_notice(request),
                  notice_skill=request.query_params.get("skill"),
                  notice_form=request.query_params.get("form") == CONDITIONS_KEY,
                  condition_fields=fields, notes=values["notes"], notes_error=errors.get("notes"),
                  notes_limit=NOTES_LIMIT,
                  travel_default=app_state(request).config.settings.rules.conditions_default_travel_share,
                  # skill forms and the answers: no reload under a choice being made, and never after a POST
                  refresh=request.method == "GET" and (profile is None or locked))


@router.get("/cv/{cv_id}", response_class=HTMLResponse)
def profile_page(request: Request, store: StoreDep, cv_id: int) -> Response:
    return _profile_page(request, store, cv_id)


@router.get("/cv/{cv_id}/export.md")
def export_markdown(request: Request, store: StoreDep, cv_id: int) -> Response:
    """The analysis of a CV as a Markdown file to download, in the language of the page. It reads the stored results
    only, so it needs no key and the demo lock does not touch it (a GET). The log gets the id and the size, not the
    text."""
    cv = cv_or_404(store, cv_id)
    state, name = app_state(request), cv_name(request, cv)
    try:
        text = build_export(store, state.config.settings, [cv_id], request_lang(request), texts=state.texts,
                            names={cv_id: name}, upload_time=not source(request).demo)
    except ExportError:   # the CV has no profile yet
        return render(request, "error.html", 404, code=404, message=translator(request)("export.no_profile"))
    body = text.encode("utf-8")
    log.info("exported CV %d as Markdown: %d bytes", cv_id, len(body))
    return Response(body, media_type="text/markdown; charset=utf-8", headers={
        "content-disposition": f'attachment; filename="{export_filename(name, date.today())}"'})


def _rematch(run: Run, cv_id: int, t) -> str:
    return t("taskdone.rematch", postings=t("count.postings", count=len(rematch(run, cv_id))))


@router.post("/cv/{cv_id}/skill/{skill_id}")
def edit_skill(request: Request, store: StoreDep, cv_id: int, skill_id: str,
               hidden: Annotated[Literal["0", "1"] | None, Form()] = None,  # not bool: a missing one would read False
               level: Annotated[Level | None, Form()] = None) -> Response:
    """Hide ("1") or show ("0") a skill, or change its level; then the matches are computed again without the hidden
    skills."""
    cv_or_404(store, cv_id)
    profile = store.profile(cv_id)
    if profile is None or all(skill.skill_id != skill_id for skill in profile.skills):
        raise HTTPException(404)
    page, anchor = f"/cv/{cv_id}", f"skill-{skill_id}"
    if hidden is None and level is None:
        return see_other(page, fragment=anchor)
    tasks, t = app_state(request).tasks, translator(request)
    busy = see_other(page, msg="busy", skill=skill_id, fragment=anchor)
    # the edit and its rematch belong together: change nothing while the rematch can neither start nor wait. A task
    # of this CV that runs may have read the profile before the edit, and the same task is never queued twice.
    if _cv_task_running(request, cv_id) or not tasks.accepts("rematch", page):
        return busy
    store.edit_skill(cv_id, skill_id, hidden=None if hidden is None else hidden == "1", level=level)
    status = start_task(request, "rematch", page, lambda r: _rematch(r, cv_id, t))
    if status is Start.REFUSED:
        return busy  # the line filled up between the check and now: saving again recomputes
    if status is Start.QUEUED:
        return see_other(page, msg="queued", skill=skill_id, fragment=anchor)
    return see_other(page, fragment=anchor)


def _apply_conditions(run: Run, t) -> str:
    """The task rematch of a save of the answers: every CV, no API call."""
    return t("taskdone.conditions", matches=t("count.matches", count=rematch_offline(run)))


Choice = Annotated[Literal["", "1", "0"], Form()]  # a choice of the form: not answered, yes, no


@router.post("/cv/{cv_id}/conditions")
def save_conditions(request: Request, store: StoreDep, cv_id: int, driving_licence_b: Choice = "",
                    travel_share_max: Annotated[str, Form()] = "", shift_work: Choice = "",
                    commute_km_max: Annotated[str, Form()] = "", remote_only: Choice = "", full_time: Choice = "",
                    notes: Annotated[str, Form()] = "") -> Response:
    """Save the answers to the conditions (one record for the database) and recompute the matches of every CV, as the
    task rematch and without an API client: the answers are no part of the match fingerprint, so nothing else would."""
    cv_or_404(store, cv_id)
    t = translator(request)
    form = {"driving_licence_b": driving_licence_b, "travel_share_max": travel_share_max, "shift_work": shift_work,
            "commute_km_max": commute_km_max, "remote_only": remote_only, "full_time": full_time,
            "notes": notes.replace("\r\n", "\n")}  # a browser sends the line breaks of a text field as CRLF
    answers, errors = _read_answers(form, t)
    if answers is None:
        return _profile_page(request, store, cv_id, 400, answers=form, errors=errors)
    tasks, page = app_state(request).tasks, f"/cv/{cv_id}"
    busy = see_other(page, msg="busy", form=CONDITIONS_KEY, fragment=CONDITIONS_KEY)
    # as the skill edit: a task of this CV that runs may have read the answers before the save, and the same task is
    # never queued twice; save only when the rematch can start or wait
    if _cv_task_running(request, cv_id) or not tasks.accepts("rematch", page, CONDITIONS_KEY):
        return busy
    store.save_conditions(answers)
    status = start_task(request, "rematch", page, lambda r: _apply_conditions(r, t), key=CONDITIONS_KEY)
    if status is Start.REFUSED:
        return busy  # the line filled up between the check and now: saving again recomputes
    msg = "queued" if status is Start.QUEUED else "conditions_saved"
    return see_other(page, msg=msg, form=CONDITIONS_KEY, fragment=CONDITIONS_KEY)


@router.post("/cv/{cv_id}/delete")
def delete_cv(request: Request, store: StoreDep, cv_id: int,
              back: Annotated[Literal["start"] | None, Form()] = None) -> Response:
    """Delete a CV, from its pages or from the list of the start page (back="start": a refusal shows at its row)."""
    cv_or_404(store, cv_id)
    if _cv_in_use(request, cv_id):
        if back == "start":
            return see_other("/", msg="delete_busy", cv=str(cv_id), fragment=f"cv-{cv_id}")
        return see_other(f"/cv/{cv_id}", msg="delete_busy")
    store.delete_cv(cv_id)
    return see_other("/", msg="deleted")
