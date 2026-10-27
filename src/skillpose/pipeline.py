"""The flow of an analysis, shared by the command line and the web app.

A Run freezes the vocabulary snapshot once, so every call of a step shares one cached prefix; the skills Claude
proposes reach the store only afterwards (merge_proposals). A caller checks each paid run against the monthly limit
before the first call (estimate, limit_message). Nothing here prints: the callers decide what to show.
"""

import logging
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import anthropic

from skillpose.core.batch import batch_seconds, read_jobs_batch, submit_jobs_batch
from skillpose.core.config import Settings
from skillpose.core.extract_cv import extract_cv
from skillpose.core.extract_job import PostingTooLong, extract_job
from skillpose.core.llm import LLM, CallResult, Stopped, estimate_cv_usd, estimate_usd, stopped
from skillpose.core.market import role_reports
from skillpose.core.matching import match_fingerprint, match_job, needed_pairs
from skillpose.core.models import (
    CandidateProfile,
    DevelopmentPath,
    JobMatch,
    JobProfile,
    Relation,
    RoleReport,
    Skill,
)
from skillpose.core.paths import build_path, path_input_hash, path_request_parts
from skillpose.core.relations import RelationService
from skillpose.core.vocabulary import Vocabulary
from skillpose.store.db import Store

log = logging.getLogger(__name__)

# path_targets: a role within reach after some learning first, then a hard one, then one that already fits
TARGET_ORDER = {2: 0, 3: 1, 1: 2}


@dataclass
class Run:                       # one run: settings, store, frozen vocabulary (one cached prefix per step)
    settings: Settings
    store: Store
    vocab: Vocabulary
    snapshot: str
    lang: str
    now_year: int
    client: object | None = None
    llm: LLM | None = None       # LLM(client, settings, store) when a client is given
    # how far a long step is: progress(phase, **counts) with the phases saving_results, matching (done, total) and
    # saving_matches; the caller turns them into text (the task line of the web app). None: nobody asks
    progress: Callable[..., None] | None = None


def _report(run: Run, phase: str, **counts: int) -> None:
    if run.progress is not None:
        run.progress(phase, **counts)


@dataclass
class PostingPlan:
    open: list[str]                          # analyze now: single, then the groups, in the order they were stored
    single: list[str]                        # as single calls (from batch_threshold: the first batch_single_first)
    groups: list[list[str]]                  # the rest, one Message Batch each (half price)
    pending: list[tuple[str, list[str]]]     # batches of earlier runs, collect first

    @property
    def batch(self) -> bool:
        return bool(self.groups)

    @property
    def batched(self) -> int:
        return sum(len(group) for group in self.groups)


@dataclass
class PostingOutcome:
    done: int
    still_open: int
    batch_ids: list[str] = field(default_factory=list)  # submitted and not yet collected (wait=False)


@dataclass(frozen=True)
class BatchProgress:
    """How far an open batch is, from the status request of a check (request_counts); not stored: the next check
    after a restart has it again."""
    batch_id: str
    total: int          # the postings stored for the batch
    done: int           # total - processing
    failed: int         # errored + expired
    checked: datetime   # UTC


def open_run(settings: Settings, store: Store, lang: str, client=None, now: datetime | None = None) -> Run:
    vocab = Vocabulary.load(extra=store.added_skills())
    run = Run(settings=settings, store=store, vocab=vocab, snapshot=vocab.snapshot(), lang=lang,
              now_year=(now or datetime.now(UTC)).year)
    return attach_client(run, client) if client is not None else run


def attach_client(run: Run, client) -> Run:
    """The same run (same snapshot) with an API client: the CLI creates the client only after the cost checks."""
    return replace(run, client=client, llm=LLM(client, run.settings, run.store))


def ingest_postings(run: Run, items: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """Store postings; return their hashes (each once) and a message per posting that was too long."""
    limit = run.settings.max_posting_chars
    hashes, skipped = [], []
    for ref, text in items:
        if not text.strip():
            continue
        if len(text) > limit:
            skipped.append(f"skipped {ref}: {len(text)} characters, limit {limit}")
            continue
        hashes.append(run.store.save_posting(text, ref))
    return list(dict.fromkeys(hashes)), skipped


def split_groups(hashes: list[str], n: int) -> list[list[str]]:
    """hashes in n groups of near-equal size, in order: sizes differ by at most one, larger groups first (29 in 3:
    10, 10, 9). Never an empty group: fewer hashes than n give fewer groups."""
    n = min(n, len(hashes))
    if n <= 0:
        return []
    size, larger = divmod(len(hashes), n)
    groups, start = [], 0
    for i in range(n):
        end = start + size + (i < larger)
        groups.append(hashes[start:end])
        start = end
    return groups


def plan_postings(run: Run, hashes: list[str], *, no_batch: bool = False) -> PostingPlan:
    """The open postings among hashes and how to send them. From batch_threshold of them (and without no_batch) the
    first batch_single_first go as single calls, so a first result comes at once, and the rest in batch_groups
    Message Batches at half price; below it, all as single calls."""
    pending = run.store.pending_batches()  # submitted by an earlier, interrupted run: collect, never pay twice
    in_batch = {h for _, batch_hashes in pending for h in batch_hashes}
    wanted = set(hashes)
    open_hashes = [h for h in run.store.open_postings() if h in wanted and h not in in_batch]
    settings = run.settings
    if no_batch or len(open_hashes) < settings.batch_threshold:
        return PostingPlan(open=open_hashes, single=open_hashes, groups=[], pending=pending)
    first = settings.batch_single_first
    return PostingPlan(open=open_hashes, single=open_hashes[:first],
                       groups=split_groups(open_hashes[first:], settings.batch_groups), pending=pending)


def estimate(run: Run, *, cv_text: str | None = None, n_postings: int = 0, plan: PostingPlan | None = None,
             n_paths: int = 0) -> float:
    """What a run may cost. n_postings: single calls of step 2. plan: its single calls at the single price and each
    of its batches at the batch price, one cache write each (as the check before each batch counts it)."""
    settings = run.settings
    cost = estimate_usd(settings, "job", n_postings + (len(plan.single) if plan else 0))
    for group in plan.groups if plan else []:
        cost += estimate_usd(settings, "job", len(group), batch=True)
    if cv_text is not None:
        cost += estimate_cv_usd(settings, len(cv_text))
    if n_paths > 0:
        cost += n_paths * estimate_usd(settings, "path", 1, avg_in=1500, avg_out=3000, fixed_tokens=2500)
    return cost


def limit_message(run: Run, estimate_usd: float) -> str | None:
    """Why a run must not start: its estimate does not fit into the rest of the monthly limit. None: it fits."""
    spent, limit = run.store.month_total_usd(), run.settings.monthly_limit_usd
    if spent + estimate_usd <= limit:
        return None
    return (f"monthly limit: {spent:.2f} USD spent + {estimate_usd:.2f} USD estimated for this run > "
            f"{limit:.2f} USD - nothing was sent")


def _llm(run: Run) -> LLM:
    if run.llm is None:
        raise RuntimeError("this run has no API client")
    return run.llm


def _failure(run: Run, step: str, result: CallResult, default: str) -> str | Stopped:
    """Why a call gave no value. An early stop (cut off at the token limit, or refused) comes as Stopped with the limit
    of the step and the cost of the attempt, so the caller can say so in its own language; str() of it is the stop
    reason."""
    return stopped(result, run.settings.steps[step].max_tokens) or result.error or default


def ensure_profile(run: Run, cv_id: int) -> tuple[CandidateProfile | None, str | Stopped | None]:
    """The stored profile of a CV, or step 1 now. (None, reason) when the call fails or the run has no client."""
    profile = run.store.profile(cv_id)
    if profile is not None:
        return profile, None
    if run.llm is None:
        return None, "no client"
    profile, result = extract_cv(run.llm, run.store.cv_text(cv_id), run.vocab, run.snapshot, run.lang, run.now_year)
    if profile is None:
        return None, _failure(run, "cv", result, "no result")
    run.store.save_profile(cv_id, profile, result.model)
    return profile, None


def _collect(run: Run, batch_id: str, hashes: list[str], seconds: float | None = None) -> int:
    """Take the results of a batch that has ended (seconds: how long it took, for the log). Returns the number of
    analyzed postings."""
    _report(run, "saving_results")
    texts = {h: run.store.posting_text(h) for h in hashes}
    results = read_jobs_batch(run.client, run.settings, run.store, batch_id, texts, run.vocab, seconds=seconds)
    model = run.settings.steps["job"].model
    for h, jp in results.items():
        if jp is None:
            run.store.set_posting_error(h, "no result in batch")
        else:
            run.store.save_job_profile(jp, model)
    run.store.finish_batch(batch_id)
    analyzed = sum(jp is not None for jp in results.values())
    log.info("batch %s collected: %d of %d postings analyzed", batch_id, analyzed, len(hashes))
    return analyzed


def analyze_postings(run: Run, plan: PostingPlan, *, wait: bool = True, poll_seconds: float = 30.0,
                     sleep: Callable[[float], None] = time.sleep) -> PostingOutcome:
    """Step 2 for the open postings of a plan. The batches go out first, so their clock starts, then the single
    calls: the first result is there about a minute after the start. With wait, each batch of the plan is collected
    as soon as it has ended. A failed posting keeps its reason; BudgetExceeded goes to the caller (a batch that went
    out stays stored and is collected later)."""
    if not plan.open:
        return PostingOutcome(done=0, still_open=0)
    llm = _llm(run)  # no client, no step 2 - neither single nor as a batch
    batch_ids: list[str] = []
    reserved = 0.0
    for group in plan.groups:
        texts = {h: run.store.posting_text(h) for h in group}
        batch_id = submit_jobs_batch(run.client, run.settings, run.store, texts, run.snapshot, run.lang,
                                     reserved_usd=reserved)
        run.store.save_batch(batch_id, group)  # an interrupted run collects it later instead of paying twice
        reserved += estimate_usd(run.settings, "job", len(group), batch=True)
        batch_ids.append(batch_id)
    done = _analyze_single(run, llm, plan.single)
    if wait and batch_ids:
        done += _collect_open(run, set(batch_ids), wait=True, poll_seconds=poll_seconds, sleep=sleep)
        batch_ids = []
    return PostingOutcome(done=done, still_open=len(plan.open) - done, batch_ids=batch_ids)


def _analyze_single(run: Run, llm: LLM, hashes: list[str]) -> int:
    """Step 2 as one call per posting. Returns the number of analyzed postings."""
    done = 0
    for h in hashes:
        try:
            jp, result = extract_job(llm, run.store.posting_text(h), run.vocab, run.snapshot,
                                     run.settings.role_families, run.lang, run.settings.max_posting_chars)
        except PostingTooLong as e:
            run.store.set_posting_error(h, str(e))
            continue
        if jp is None:
            run.store.set_posting_error(h, result.error or "no result")
        else:
            run.store.save_job_profile(jp, result.model)
            done += 1
    return done


def batch_lost(error: Exception) -> str | None:
    """Why a batch can never be collected, or None for an error that may pass (no connection, 429, 5xx: the next
    attempt asks again). Lost: the API does not know the id (a batch of another key or workspace), or the batch has
    ended without results (they are kept for 29 days)."""
    if isinstance(error, anthropic.NotFoundError):
        return "not found (another API key or workspace?)"
    if isinstance(error, anthropic.AnthropicError) and not isinstance(error, anthropic.APIError):
        return "no results (they are kept for 29 days)"  # the SDK finds no results_url
    return None


def _give_up(run: Run, batch_id: str, hashes: list[str], reason: str) -> None:
    """A lost batch: its postings without a job profile are open again, with the reason, and the next run analyzes
    them. What the batch may have cost never reaches the ledger: the usage comes only with the results."""
    log.warning("batch %s lost: %s", batch_id, reason)
    for h in hashes:
        if run.store.job_profile(h) is None:
            run.store.set_posting_error(h, f"batch {batch_id} lost: {reason}")
    run.store.finish_batch(batch_id)


def _progress(batch, batch_id: str, hashes: list[str]) -> BatchProgress | None:
    """The progress in the answer of a status request; None when it holds no request_counts."""
    counts = getattr(batch, "request_counts", None)
    if counts is None:
        return None
    total = len(hashes)
    return BatchProgress(batch_id=batch_id, total=total, done=max(0, min(total, total - counts.processing)),
                         failed=counts.errored + counts.expired, checked=datetime.now(UTC))


def _collect_open(run: Run, only: set[str] | None, *, wait: bool, poll_seconds: float,
                  sleep: Callable[[float], None], progress: Callable[[BatchProgress], None] | None = None,
                  collected: Callable[[str, int, int], None] | None = None) -> int:
    """One status request per open batch (only: these ids), and the results of each batch that has ended; with wait
    again after poll_seconds until none of them runs. Returns the number of analyzed postings."""
    done = 0
    while True:
        running = False
        for batch_id, hashes in run.store.pending_batches():
            if only is not None and batch_id not in only:
                continue
            try:
                batch = run.client.messages.batches.retrieve(batch_id)
                if batch.processing_status != "ended":
                    running = True
                    if progress is not None and (state := _progress(batch, batch_id, hashes)) is not None:
                        progress(state)
                    continue
                analyzed = _collect(run, batch_id, hashes, batch_seconds(batch))
            except anthropic.AnthropicError as e:
                if (reason := batch_lost(e)) is None:
                    raise
                _give_up(run, batch_id, hashes, reason)
                continue
            done += analyzed
            if collected is not None:
                collected(batch_id, analyzed, len(hashes))
        if not (wait and running):
            return done
        sleep(poll_seconds)


def collect_batches(run: Run, *, wait: bool = True, poll_seconds: float = 30.0,
                    sleep: Callable[[float], None] = time.sleep,
                    progress: Callable[[BatchProgress], None] | None = None,
                    collected: Callable[[str, int, int], None] | None = None) -> int:
    """Collect the open batches, each as soon as it has ended; without wait only those that have ended now. Returns
    the number of analyzed postings. progress gets the BatchProgress of each batch that still runs (from the status
    request the check makes anyway); collected gets (batch id, analyzed, postings) of each collected batch. A lost
    batch (batch_lost) is given up; any other API error goes to the caller and the batch stays open."""
    if run.client is None:
        return 0
    return _collect_open(run, None, wait=wait, poll_seconds=poll_seconds, sleep=sleep, progress=progress,
                         collected=collected)


def _known_relations(store: Store, pairs: dict[str, list[str]]) -> dict[tuple[str, str], Relation]:
    out: dict[tuple[str, str], Relation] = {}
    for target, candidates in pairs.items():
        known = store.relations(target)
        out.update({(target, c): known[c] for c in candidates if c in known})
    return out


def rematch(run: Run, cv_id: int, hashes: list[str] | None = None) -> list[tuple[JobProfile, JobMatch]]:
    """Match the visible profile, with the answers to the conditions stored in the database, against the given (or
    all analyzed) postings and store the matches. Without a client only relations already in the store count."""
    stored = run.store.profile(cv_id)
    if stored is None:
        return []
    profile = stored.visible().model_copy(update={"conditions": run.store.conditions()})  # a hidden skill never counts
    if hashes is None:
        hashes = [row.hash for row in run.store.postings()]
    jobs = [jp for h in dict.fromkeys(hashes) if (jp := run.store.job_profile(h)) is not None]
    wanted: dict[str, set[str]] = {}
    for jp in jobs:
        for target, candidates in needed_pairs(jp, profile, run.vocab).items():
            wanted.setdefault(target, set()).update(candidates)
    pairs = {t: sorted(c) for t, c in wanted.items()}
    if run.llm is None:
        relations = _known_relations(run.store, pairs)
    else:
        relations = RelationService(run.llm, run.store, run.snapshot, run.vocab,
                                    workers=run.settings.relation_workers).resolve(
            pairs, progress=lambda done, total: _report(run, "matching", done=done, total=total))
    _report(run, "saving_matches")
    fingerprint = match_fingerprint(run.settings.rules, run.now_year)
    rows = []
    for jp in jobs:
        jm = match_job(jp, profile, run.vocab, relations, run.settings.rules, run.now_year)
        run.store.save_match(cv_id, jm, fingerprint)
        rows.append((jp, jm))
    return rows


def refresh_matches(run: Run, cv_ids: Collection[int] | None = None) -> int:
    """Recompute the stored matches that other rules made (a change of the rules in code or in the settings, or the
    year), without any API call, even when the run has a client: only relations already in the store count. cv_ids
    narrows it to these CVs (default: all). Returns the number of matches recomputed. Call it before matches are shown
    or exported."""
    offline = replace(run, client=None, llm=None)
    stale = run.store.stale_matches(match_fingerprint(run.settings.rules, run.now_year))
    return sum(len(rematch(offline, cv_id, hashes)) for cv_id, hashes in stale.items()
               if cv_ids is None or cv_id in cv_ids)


def rematch_offline(run: Run) -> int:
    """Recompute the matches of every CV that has a profile with the answers to the conditions that the database holds
    now, without any API call, even when the run has a client: only relations already in the store count. The answers
    are no part of the match fingerprint, so refresh_matches does not see a change of them; whoever saves them calls
    this. Returns the number of matches recomputed."""
    offline = replace(run, client=None, llm=None)
    return sum(len(rematch(offline, cv.id)) for cv in run.store.cvs())


def merge_proposals(run: Run, cv_ids: list[int], hashes: list[str]) -> list[Skill]:
    """Add the skills Claude proposed for these CVs and postings to the vocabulary. Only after the run, so the
    cached prefix stayed the same during it."""
    proposals = []
    for cv_id in cv_ids:
        if (profile := run.store.profile(cv_id)) is not None:
            proposals += profile.proposed_skills
    for h in hashes:
        if (jp := run.store.job_profile(h)) is not None:
            proposals += jp.proposed_skills
    added = run.vocab.merge(proposals)
    run.store.add_skills(added)
    return added


def market(store: Store, settings: Settings, cv_id: int) -> list[RoleReport]:
    matches = store.matches(cv_id)
    jobs = {jm.text_hash: jp for jm in matches if (jp := store.job_profile(jm.text_hash)) is not None}
    return role_reports(jobs, matches, settings.rules)


def path_targets(reports: list[RoleReport], n: int) -> list[str]:
    """Role families worth a path: tier 2, then 3, then 1; more postings first, then by name. Never `other`, never a
    family with too little data."""
    candidates = [r for r in reports if r.role_family != "other" and r.tier is not None]
    candidates.sort(key=lambda r: (TARGET_ORDER[r.tier], -r.postings, r.role_family))
    return [r.role_family for r in candidates[:n]]


def build_path_for(run: Run, cv_id: int, role_family: str, *,
                   force: bool = False) -> tuple[DevelopmentPath | None, str | Stopped | None]:
    """Step 5 for one role family in the language of the run. A stored path in that language for the same input comes
    back without a call unless forced; a path in another language never does."""
    report = next((r for r in market(run.store, run.settings, cv_id) if r.role_family == role_family), None)
    if report is None or report.tier is None:
        return None, "too little data"
    if not report.top_gaps:
        return None, "no gaps"  # a path would close nothing
    stored = run.store.profile(cv_id)
    if stored is None:
        return None, "no profile"
    profile = stored.visible()
    input_hash = path_input_hash(*path_request_parts(profile, report, run.vocab, run.lang))
    old = run.store.paths(cv_id, run.lang).get(role_family)
    if old is not None and old.language == run.lang and old.input_hash == input_hash and not force:
        return old, None
    if run.llm is None:
        return None, "no client"
    path, result = build_path(run.llm, profile, report, run.vocab, run.lang)
    if path is None:
        return None, _failure(run, "path", result, "no steps")
    run.store.save_path(cv_id, path, result.model)
    return path, None
