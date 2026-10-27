"""The market view and the development paths of one CV, and the page of the costs.

All three pages show source(request): the demo store without a key, the own one with a key. A path is built by a
background task (step 5, paid) in the language of the page; the button posts to /cv/{id}/paths/{role_family} and the
task opens its own store and API client (deps.start_task). The costs page lists what the ledger of the shown store
holds; with a key it also shows the month and its limit, which are real money.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from skillpose.core.llm import Stopped
from skillpose.core.models import CandidateProfile, DevelopmentPath, RoleReport
from skillpose.core.paths import path_input_hash, path_request_parts
from skillpose.core.prompts import active_prompts
from skillpose.core.vocabulary import Vocabulary
from skillpose.pipeline import Run, build_path_for, estimate, market, open_run
from skillpose.store.db import CvRow, Store
from skillpose.web.deps import (
    StoreDep,
    ai_model,
    app_state,
    cost_text,
    cv_name,
    cv_or_404,
    fmt_int,
    fmt_usd,
    is_locked,
    limit_text,
    query_notice,
    render,
    request_lang,
    see_other,
    source,
    start_task,
    stopped_text,
    translator,
)
from skillpose.web.labels import (
    Proposed,
    cv_proposals,
    percent,
    role_label,
    skill_name,
    stability_note,
    tier_rules,
)
from skillpose.web.tasks import Start

router = APIRouter()

RECENT_CALLS = 20
OTHER = "other"  # the family of the postings that fit no target role: it never gets a path (pipeline.path_targets)
# the reasons build_path_for gives before any call; the page tells them in its own language
REFUSALS = {"too little data": "paths.too_little", "no gaps": "paths.no_gaps"}


# --- market view ---

@dataclass
class GapView:
    name: str
    gap_type: str | None
    postings: int
    width: int          # per cent of the largest weight of the family


@dataclass
class BlockedView:
    """A must condition that the answers in the profile do not meet, in some postings of the family."""
    name: str
    postings: int


@dataclass
class FamilyView:
    family: str
    label: str
    postings: int
    tier: int | None    # None: too little data
    coverage: int       # per cent: the median coverage of the must requirements
    gaps: list[GapView]
    blocked: list[BlockedView]


def _family_view(t, report: RoleReport, vocab: Vocabulary, lang: str, proposed: Proposed) -> FamilyView:
    top = max((g.weight for g in report.top_gaps), default=0.0)
    gaps = [GapView(name=skill_name(vocab, g.skill_id, lang, proposed), gap_type=g.gap_type, postings=g.postings,
                    width=percent(g.weight / top) if top > 0 else 0) for g in report.top_gaps]
    blocked = [BlockedView(name=skill_name(vocab, g.skill_id, lang, proposed), postings=g.postings)
               for g in report.conditions_blocking]
    return FamilyView(family=report.role_family, label=role_label(t, report.role_family), postings=report.postings,
                      tier=report.tier, coverage=percent(report.median_coverage), gaps=gaps, blocked=blocked)


@router.get("/cv/{cv_id}/market", response_class=HTMLResponse)
def market_page(request: Request, store: StoreDep, cv_id: int) -> Response:
    cv = cv_or_404(store, cv_id)
    t, lang = translator(request), request_lang(request)
    settings = app_state(request).config.settings
    vocab, proposed = Vocabulary.load(extra=store.added_skills()), cv_proposals(store, cv_id)
    families = [_family_view(t, report, vocab, lang, proposed) for report in market(store, settings, cv_id)]
    return render(request, "market.html", cv_id=cv_id, cv=cv, name=cv_name(request, cv), families=families,
                  tier_rules=tier_rules(settings), stability=stability_note(t, settings, lang))


# --- development paths ---

@dataclass
class StepView:
    activity: str       # may be empty: the program removes addresses from the text Claude wrote
    closes: list[str]   # names of the skills in the language of the page
    weeks: int
    proof: str


@dataclass
class PathView:
    rationale: str
    steps: list[StepView]
    total_weeks: int
    created: str | None
    changed: bool | None    # the inputs differ from the ones the path was made from; None: not shown
    model: str | None       # the model that made the path, for the AI label; None in the demo
    only_in: str | None     # the language of the path when there is none in the language of the page


@dataclass
class PathFamilyView:
    family: str
    label: str
    tier: int
    path: PathView | None


@dataclass
class NoPathView:
    """A role family of the market view that gets no path section, with the reason: "few" (no tier: fewer postings
    than the minimum), "other" or "no_gaps" (a path would close nothing)."""
    label: str
    postings: int
    reason: str


def _no_path_reason(report: RoleReport, path: DevelopmentPath | None) -> str | None:
    """Why a family gets no section on the paths page; None when it gets one. A family with a tier keeps its section
    when a path is stored for it, whatever the gaps are now."""
    if report.tier is None:
        return OTHER if report.role_family == OTHER else "few"
    if path is not None:
        return None
    if report.role_family == OTHER:
        return OTHER
    return None if report.top_gaps else "no_gaps"


def _inputs_changed(path: DevelopmentPath, profile: CandidateProfile, report: RoleReport, vocab: Vocabulary) -> bool:
    """Would the request for this path be another one now? Compared in the language the path was made in."""
    request = path_request_parts(profile.visible(), report, vocab, path.language)
    return path_input_hash(*request) != path.input_hash


def _path_view(path: DevelopmentPath, created: str | None, vocab: Vocabulary, proposed: Proposed, lang: str,
               changed: bool | None, model: str | None) -> PathView:
    steps = [StepView(activity=s.activity.strip(), closes=[skill_name(vocab, i, lang, proposed) for i in s.closes],
                      weeks=s.effort_weeks, proof=s.proof.strip()) for s in path.steps]
    return PathView(rationale=path.rationale.strip(), steps=steps, total_weeks=path.total_weeks, created=created,
                    changed=changed, model=ai_model(model), only_in=None if path.language == lang else path.language)


def _paths_page(request: Request, store: Store, cv: CvRow, status: int = 200, *,
                errors: dict[str, str] | None = None) -> Response:
    """The paths of a CV, each in the language of the page; a path that exists only in another language is shown
    with a note, and the button creates it in the language of the page. A family without a path section (no tier,
    the family "other", no gaps) is named in the list "roles without a path", with the reason. errors: a message per
    role family, shown next to its button (or above the list when the family has no section)."""
    settings = app_state(request).config.settings
    t, lang = translator(request), request_lang(request)
    locked, demo = is_locked(request), source(request).demo
    families: list[PathFamilyView] = []
    without_path: list[NoPathView] = []
    any_tier = False
    cost_label = ""
    profile = store.profile(cv.id)
    if profile is not None:
        vocab, proposed = Vocabulary.load(extra=store.added_skills()), cv_proposals(store, cv.id)
        stored = store.paths(cv.id, lang)
        for report in market(store, settings, cv.id):
            family = report.role_family
            path = stored.get(family)
            any_tier = any_tier or report.tier is not None
            if (reason := _no_path_reason(report, path)) is not None:
                without_path.append(NoPathView(label=role_label(t, family), postings=report.postings, reason=reason))
                continue
            view = None
            if path is not None:
                changed = None if locked else _inputs_changed(path, profile, report, vocab)
                # the demo store is made at start: its paths have no real time
                created = None if demo else store.path_created(cv.id, family, path.language)
                view = _path_view(path, created, vocab, proposed, lang, changed,
                                  store.path_model(cv.id, family, path.language))
            families.append(PathFamilyView(family=family, label=role_label(t, family), tier=report.tier, path=view))
        without_path.sort(key=lambda v: (-v.postings, v.label))
        if not locked:
            cost_label = cost_text(request, estimate(open_run(settings, store, lang), n_paths=1))
    errors = errors or {}
    shown = {f.family for f in families}
    return render(request, "paths.html", status, cv_id=cv.id, cv=cv, name=cv_name(request, cv), families=families,
                  without_path=without_path, any_tier=any_tier, locked=locked, cost_label=cost_label, errors=errors,
                  orphans=[text for family, text in errors.items() if family not in shown],
                  notice=query_notice(request), notice_family=request.query_params.get("family"),
                  min_jobs=settings.rules.min_jobs_per_role)


@router.get("/cv/{cv_id}/paths", response_class=HTMLResponse)
def paths_page(request: Request, store: StoreDep, cv_id: int) -> Response:
    return _paths_page(request, store, cv_or_404(store, cv_id))


def _build_path(run: Run, cv_id: int, role_family: str, force: bool, t) -> str:
    """The task path: step 5 for one family, unless a stored path still fits (build_path_for decides). The refusals
    that need no call were made in the request; here they can only mean that the data changed in between. The
    monthly limit is checked again first: the tasks before this one may have spent the rest while it waited."""
    if (refusal := limit_text(t, run, estimate(run, n_paths=1), "paths.limit")) is not None:
        raise RuntimeError(refusal)
    path, reason = build_path_for(run, cv_id, role_family, force=force)
    role = role_label(t, role_family)
    if path is not None:
        return t("taskdone.path", role=role, steps=len(path.steps), weeks=path.total_weeks)
    if reason in REFUSALS:
        return t(REFUSALS[reason], count=run.settings.rules.min_jobs_per_role)
    if reason == "no client":
        raise RuntimeError(t("start.no_key"))
    if isinstance(reason, Stopped):  # paid, but cut off or refused: say so, with the limit and the cost
        reason = stopped_text(t, run.lang, "paths", reason)
    raise RuntimeError(t("paths.failed", role=role, reason=reason))


@router.post("/cv/{cv_id}/paths/{role_family}")
def create_path(request: Request, store: StoreDep, cv_id: int, role_family: str,
                confirm: Annotated[str | None, Form()] = None, force: Annotated[str | None, Form()] = None) -> Response:
    """Build the path of one family: "force" builds a new one although the stored path still fits."""
    cv = cv_or_404(store, cv_id)
    settings = app_state(request).config.settings
    if role_family not in settings.role_families or role_family == OTHER:  # `other` never gets a path
        raise HTTPException(404)
    t, lang = translator(request), request_lang(request)
    again = force == "1"
    run = open_run(settings, store, lang)
    _, reason = build_path_for(run, cv_id, role_family, force=again)  # no client: only the refusals come back
    if reason in REFUSALS:
        text = t(REFUSALS[reason], count=settings.rules.min_jobs_per_role)
        return _paths_page(request, store, cv, 400, errors={role_family: text})
    cost = estimate(run, n_paths=1)  # an upper bound: a stored path that still fits costs nothing
    if (error := limit_text(t, run, cost, "paths.limit")) is not None:
        return _paths_page(request, store, cv, 400, errors={role_family: error})
    page = f"/cv/{cv_id}/paths"
    if cost > settings.confirm_above_usd and not confirm:
        question = t("paths.confirm_question", role=role_label(t, role_family), cost=fmt_usd(cost, lang),
                     threshold=fmt_usd(settings.confirm_above_usd, lang))
        return render(request, "confirm.html", cv_id=cv_id, action=f"{page}/{role_family}", question=question,
                      cost=cost, busy=t("paths.creating"), back=page, fields={"force": "1"} if again else {})
    status = start_task(request, "path", page, lambda r: _build_path(r, cv_id, role_family, again, t),
                        key=f"{role_family}{' again' if again else ''}")
    if status is Start.REFUSED:
        return see_other(page, msg="busy", family=role_family, fragment=f"family-{role_family}")
    if status is Start.QUEUED:
        return see_other(page, msg="queued", family=role_family, fragment=f"family-{role_family}")
    return see_other(page, fragment=f"family-{role_family}")


# --- costs ---

@dataclass
class MonthView:
    spent: float
    limit: float
    rest: float             # never negative
    cache_share: int        # per cent of the input tokens that were cache reads


@dataclass
class StepRow:
    label: str
    calls: int
    usd: str
    cache_share: int


@dataclass
class MeanRow:
    label: str
    batch: bool
    calls: int
    mean: str


@dataclass
class CallRow:
    time: str
    step: str
    model: str
    input: str
    output: str
    cache_write: str
    cache_read: str
    usd: str
    batch: bool


def _month_start() -> datetime:
    now = datetime.now(UTC)
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def _step_label(t, step: str) -> str:
    label = t(f"step.{step}")
    return step if label == f"step.{step}" else label


def _month(request: Request, store: Store) -> MonthView:
    """This month of the own database: asked only with a key, so `store` is the own one."""
    spent, summary = store.month_total_usd(), store.cost_summary(since=_month_start())
    limit = app_state(request).config.settings.monthly_limit_usd
    return MonthView(spent=spent, limit=limit, rest=max(0.0, limit - spent),
                     cache_share=percent(summary.cache_read_share))


@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, store: StoreDep) -> Response:
    t, lang = translator(request), request_lang(request)
    demo = source(request).demo
    # the demo ledger holds the costs of building the demo, once: all of it; the own ledger by month
    steps = [StepRow(label=_step_label(t, s.step), calls=s.calls, usd=fmt_usd(s.usd, lang, 4),
                     cache_share=percent(s.cache_read_share))
             for s in store.cost_by_step(since=None if demo else _month_start())]
    # every call ever booked in the shown store (not one month): the demo's build, or the own ledger
    means = [MeanRow(label=_step_label(t, m.step), batch=m.batch, calls=m.calls, mean=fmt_usd(m.mean_usd, lang, 4))
             for m in store.cost_means()]
    calls = [CallRow(time=row.ts, step=_step_label(t, row.step), model=row.model, input=fmt_int(row.input, lang),
                     output=fmt_int(row.output, lang), cache_write=fmt_int(row.cache_write, lang),
                     cache_read=fmt_int(row.cache_read, lang), usd=fmt_usd(row.usd, lang, 4), batch=row.batch)
             for row in store.recent_costs(RECENT_CALLS)]
    build_total = sum(row.usd for row in store.cost_rows()) if demo else None
    # the prompts of this installation decide what the own ledger holds, so the own data name them; the demo was
    # built with the author's own prompts, not with the ones loaded here, so it names none
    return render(request, "costs.html", month=_month(request, store) if app_state(request).has_key else None,
                  steps=steps, means=means, calls=calls, build_total=build_total, recent=RECENT_CALLS,
                  prompt_set=None if demo else active_prompts().label)
