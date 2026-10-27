"""Run-to-run variance (stability): the same CV or the same posting analyzed several times.

Claude reads a text a little differently each time: two analyses of the same CV can find other skills or levels, two
analyses of the same posting other requirements. `skillpose eval --repeat N` measures how much. N and M are targets:
step 1 runs until every CV of the gold set has N new runs in the stability store and, with --repeat-jobs M, step 2
until its postings have M (from batch_threshold postings as one Message Batch per run). Each new run is a new call: a
stored profile is never reused for it. The same command again makes only what is missing, so after an interruption
it continues: nothing that was finished is paid twice, and a call interrupted mid-answer is paid once and made again.

The runs and their costs go to a store of their own (data/stability.sqlite), never to the gold-set store of `eval
--save` or to the demo. The gold-set store is only read, opened read-only: its results are the first run of each input
(the base) when the model of the step made them. Its prompt set is not stored, so it belongs in the measurement only
when it was made with the active prompts (the command has --no-base). A stored run counts when it was made with the
active prompt set, model, effort and language and answered by the model of the step (a server-side fallback answer
stays stored but does not count, as for the base). A batch is stored with its configuration and booked together with
its run, in one transaction.

Every metric compares all pairs of runs of the same input. Steps 3a and 4 are rules in code, so a different status or
tier comes from a different profile or job profile. To see what each step changes, the other side is held still: the
CV runs are matched against the first run of the postings, the posting runs against the first run of each CV. The
relations of step 3b come from both stores; a pair that neither knows is asked once (paid, cheap), so both runs of a
pair see the same relations.
"""

import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

import anthropic

from skillpose.core.batch import batch_seconds, read_jobs_batch, submit_jobs_batch
from skillpose.core.config import Rules, Settings
from skillpose.core.extract_cv import extract_cv
from skillpose.core.extract_job import extract_job
from skillpose.core.llm import LLM, Usage, estimate_cv_usd, estimate_usd
from skillpose.core.market import role_reports
from skillpose.core.matching import match_job, needed_pairs
from skillpose.core.models import (
    LEVEL_ORDER,
    STRENGTH_ORDER,
    CandidateProfile,
    CandidateSkill,
    JobMatch,
    JobProfile,
    Relation,
    text_hash,
)
from skillpose.core.prompts import active_prompts
from skillpose.core.relations import RelationService
from skillpose.core.textio import read_cv
from skillpose.core.vocabulary import Vocabulary
from skillpose.evaluation import POSTING_LANG_AUTO, check_request, read_expected
from skillpose.pipeline import batch_lost
from skillpose.store.db import BatchCost, PendingBatch, StabilityStore, Store

log = logging.getLogger(__name__)

RELATION_CALLS = 20  # step 3b calls per CV and run in the estimate, as `eval` counts them: an upper bound

# name -> (kind, label, formula). kind "share": same of all comparisons; "mean": the mean of a Jaccard value per pair.
CV_METRICS: dict[str, tuple[str, str, str]] = {
    "skill_jaccard": ("mean", "skill set (mean Jaccard)",
                      "J(A, B) = |A ∩ B| / |A ∪ B| for the skill ids A and B of the two runs (1 for two empty sets); "
                      "the mean of J over the pairs."),
    "level_same": ("share", "level the same",
                   "of the skills found in both runs, the share with the same level: the sum of the skills with the "
                   "same level over the sum of |A ∩ B|, over all pairs."),
    "level_within_one": ("share", "level at most one step apart",
                         "as above, but a level one step away counts too (basic < working < strong < expert)."),
    "evidence_same": ("share", "strongest evidence the same",
                      "of the skills found in both runs, the share whose strongest evidence (outcome > activity > "
                      "claim, or none) is the same."),
    "status_same": ("share", "requirement status the same",
                    "every requirement of every posting (in its first run) matched against both CV runs: the share "
                    "with the same status (met, partial, gap)."),
    "posting_tier_same": ("share", "posting tier the same",
                          "every posting matched against both CV runs: the share with the same tier."),
    "role_tier_same": ("share", "role tier the same",
                       "every role family with a tier in at least one of the two runs: the share with the same tier "
                       "(no tier counts as a value of its own)."),
    "main_gap_same": ("share", "heaviest gap the same",
                      "the same role families: the share whose heaviest gap (the first gap of the market view) is the "
                      "same skill (no gap counts as a value of its own)."),
}
POSTING_METRICS: dict[str, tuple[str, str, str]] = {
    "requirement_jaccard": ("mean", "requirement set (mean Jaccard)",
                            "J of the required skill ids of two runs of one posting; the mean over the pairs and the "
                            "postings that both runs have. It needs no CV."),
    "role_family_same": ("share", "role family of the posting the same",
                         "of the postings that both runs have, the share with the same role family. It needs no CV."),
    "status_same": ("share", "requirement status the same",
                    "the requirements that both runs of a posting have (the same skill id; a skill asked twice counts "
                    "in its order), matched against the first run of the CV: the share with the same status."),
    "posting_tier_same": ("share", "posting tier the same",
                          "every posting that both runs have, matched against the first run of the CV: the share with "
                          "the same tier."),
    "role_tier_same": ("share", "role tier the same",
                       "as for the CV runs, over the postings that both runs have."),
    "main_gap_same": ("share", "heaviest gap the same",
                      "as for the CV runs, over the postings that both runs have."),
}
POSTINGS_ALONE = ("requirement_jaccard", "role_family_same")  # compared without a CV


@dataclass
class Tally:
    """Agreement over pairs of runs: a share (same of total comparisons) or a mean (the sum of the Jaccard values of
    total pairs). value is None when there was nothing to compare."""
    mean: bool = False
    same: float = 0.0
    total: int = 0

    def add(self, value: float) -> None:
        self.same += float(value)
        self.total += 1

    def __add__(self, other: "Tally") -> "Tally":
        return Tally(self.mean, self.same + other.same, self.total + other.total)

    @property
    def value(self) -> float | None:
        return self.same / self.total if self.total else None

    def cell(self) -> str:
        if self.value is None:
            return "–"
        return f"{self.value:.2f} (mean of {self.total})" if self.mean else \
            f"{self.value:.2f} ({round(self.same)} of {self.total})"


def tallies(metrics: Mapping[str, tuple[str, str, str]]) -> dict[str, Tally]:
    return {name: Tally(mean=kind == "mean") for name, (kind, _, _) in metrics.items()}


@dataclass(frozen=True)
class CvRun:
    created: str                  # store timestamp, UTC
    profile: CandidateProfile
    base: bool = False            # the stored result of `eval --save`


@dataclass(frozen=True)
class JobRun:
    created: str
    jobs: dict[str, JobProfile]   # posting name -> job profile; a posting without a result in this run is missing
    base: bool = False


@dataclass
class Measurement:
    cv: dict[str, dict[str, Tally]]        # per CV: its runs against the first run of the postings
    postings: dict[str, dict[str, Tally]]  # per CV: the posting runs against the first run of that CV
    posting_only: dict[str, Tally]         # the posting runs alone (POSTINGS_ALONE)


# --- comparing two runs ---

def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def _strongest(skill: CandidateSkill) -> str | None:
    best = max(skill.evidence, key=lambda e: STRENGTH_ORDER[e.strength], default=None)
    return None if best is None else best.strength


def compare_profiles(a: CandidateProfile, b: CandidateProfile, tally: dict[str, Tally]) -> None:
    """skill_jaccard, level_same, level_within_one and evidence_same of one pair of runs of a CV."""
    skills_a, skills_b = {s.skill_id: s for s in a.skills}, {s.skill_id: s for s in b.skills}
    tally["skill_jaccard"].add(_jaccard(set(skills_a), set(skills_b)))
    for skill_id in sorted(skills_a.keys() & skills_b.keys()):
        x, y = skills_a[skill_id], skills_b[skill_id]
        steps = abs(LEVEL_ORDER[x.level] - LEVEL_ORDER[y.level])
        tally["level_same"].add(steps == 0)
        tally["level_within_one"].add(steps <= 1)
        tally["evidence_same"].add(_strongest(x) == _strongest(y))


def _statuses(jm: JobMatch) -> dict[tuple[str, int], str]:
    """The status of each requirement, keyed by its skill id and the how-many-th requirement of that skill it is."""
    seen: Counter[str] = Counter()
    out = {}
    for m in jm.matches:
        skill_id = m.requirement.skill_id
        out[(skill_id, seen[skill_id])] = m.status
        seen[skill_id] += 1
    return out


def _roles(jobs: Mapping[str, JobProfile], matches: Mapping[str, JobMatch], names: list[str],
           rules: Rules) -> dict[str, tuple[int | None, str | None]]:
    """Role family -> (tier, heaviest gap) over the postings names."""
    reports = role_reports({jobs[n].text_hash: jobs[n] for n in names}, [matches[n] for n in names], rules)
    return {r.role_family: (r.tier, r.top_gaps[0].skill_id if r.top_gaps else None) for r in reports}


def compare_outcomes(jobs_a: Mapping[str, JobProfile], matches_a: Mapping[str, JobMatch],
                     jobs_b: Mapping[str, JobProfile], matches_b: Mapping[str, JobMatch], rules: Rules,
                     tally: dict[str, Tally]) -> None:
    """status_same, posting_tier_same, role_tier_same and main_gap_same of two outcomes of the rules, over the postings
    that both have. matches: posting name -> the match of one CV run with one posting run."""
    names = sorted(matches_a.keys() & matches_b.keys())
    for name in names:
        statuses_a, statuses_b = _statuses(matches_a[name]), _statuses(matches_b[name])
        for key in sorted(statuses_a.keys() & statuses_b.keys()):
            tally["status_same"].add(statuses_a[key] == statuses_b[key])
        tally["posting_tier_same"].add(matches_a[name].tier == matches_b[name].tier)
    roles_a, roles_b = _roles(jobs_a, matches_a, names, rules), _roles(jobs_b, matches_b, names, rules)
    for family in sorted(roles_a.keys() | roles_b.keys()):
        (tier_a, gap_a), (tier_b, gap_b) = roles_a.get(family, (None, None)), roles_b.get(family, (None, None))
        if tier_a is None and tier_b is None:
            continue  # too little data in both runs
        tally["role_tier_same"].add(tier_a == tier_b)
        tally["main_gap_same"].add(gap_a == gap_b)


def compare_postings(jobs_a: Mapping[str, JobProfile], jobs_b: Mapping[str, JobProfile],
                     tally: dict[str, Tally]) -> None:
    """requirement_jaccard and role_family_same of two runs of the postings."""
    for name in sorted(jobs_a.keys() & jobs_b.keys()):
        a, b = jobs_a[name], jobs_b[name]
        tally["requirement_jaccard"].add(_jaccard({r.skill_id for r in a.requirements},
                                                  {r.skill_id for r in b.requirements}))
        tally["role_family_same"].add(a.role_family == b.role_family)


def _match_all(jobs: Mapping[str, JobProfile], profile: CandidateProfile, vocab: Vocabulary,
               relations: Mapping[tuple[str, str], Relation], rules: Rules, now_year: int) -> dict[str, JobMatch]:
    return {name: match_job(jp, profile, vocab, relations, rules, now_year) for name, jp in jobs.items()}


def relation_pairs(cv_runs: Mapping[str, list[CvRun]], job_runs: list[JobRun],
                   vocab: Vocabulary) -> dict[str, list[str]]:
    """The pairs step 3b must know for measure: every CV run with the first posting run, the first run of each CV
    with every posting run."""
    wanted: dict[str, set[str]] = {}

    def add(profile: CandidateProfile, jobs: Mapping[str, JobProfile]) -> None:
        for jp in jobs.values():
            for target, candidates in needed_pairs(jp, profile, vocab).items():
                wanted.setdefault(target, set()).update(candidates)

    for runs in cv_runs.values():
        profiles = [run.profile.visible() for run in runs]
        if not profiles or not job_runs:
            continue
        for profile in profiles:
            add(profile, job_runs[0].jobs)
        for job_run in job_runs[1:]:
            add(profiles[0], job_run.jobs)
    return {target: sorted(candidates) for target, candidates in sorted(wanted.items())}


def measure(cv_runs: Mapping[str, list[CvRun]], job_runs: list[JobRun], vocab: Vocabulary,
            relations: Mapping[tuple[str, str], Relation], rules: Rules, now_year: int) -> Measurement:
    """Every metric over all pairs of runs of the same input. cv_runs: CV key -> its runs, the first one first."""
    m = Measurement(cv={}, postings={}, posting_only=tallies({k: POSTING_METRICS[k] for k in POSTINGS_ALONE}))
    for a, b in combinations(job_runs, 2):
        compare_postings(a.jobs, b.jobs, m.posting_only)
    first_jobs = job_runs[0].jobs if job_runs else None
    for key, runs in cv_runs.items():
        cv_tally = m.cv[key] = tallies(CV_METRICS)
        profiles = [run.profile.visible() for run in runs]  # a hidden skill never counts, as in the matches
        outcomes = [_match_all(first_jobs, p, vocab, relations, rules, now_year) for p in profiles] \
            if first_jobs is not None else []
        for i, j in combinations(range(len(profiles)), 2):
            compare_profiles(profiles[i], profiles[j], cv_tally)
            if outcomes:
                compare_outcomes(first_jobs, outcomes[i], first_jobs, outcomes[j], rules, cv_tally)
        posting_tally = m.postings[key] = tallies({k: v for k, v in POSTING_METRICS.items() if k not in POSTINGS_ALONE})
        if profiles and len(job_runs) > 1:
            matched = [_match_all(run.jobs, profiles[0], vocab, relations, rules, now_year) for run in job_runs]
            for i, j in combinations(range(len(job_runs)), 2):
                compare_outcomes(job_runs[i].jobs, matched[i], job_runs[j].jobs, matched[j], rules, posting_tally)
    return m


# --- the report ---

def _total(tally_by_key: Mapping[str, dict[str, Tally]], name: str, kind_mean: bool) -> Tally:
    total = Tally(mean=kind_mean)
    for tally in tally_by_key.values():
        total = total + tally[name]
    return total


def _dates(created: Iterable[tuple[str, bool]]) -> str:
    """The days of the runs: "2026-10-21 (base), 2026-10-25 (4)"."""
    created = list(created)
    base = [ts[:10] + " (base)" for ts, is_base in created if is_base]
    new = Counter(ts[:10] for ts, is_base in created if not is_base)
    return ", ".join(base + [f"{day} ({count})" for day, count in sorted(new.items())]) or "–"


def _pairs(n: int) -> int:
    return n * (n - 1) // 2


# The rules that decide a tier: with other values the same runs give other posting and role tiers.
TIER_RULES = ("min_jobs_per_role", "role_tier_share", "tier1_coverage", "tier2_max_weeks")


@dataclass
class StabilityReport:
    created: str                            # "2026-10-25 10:12 UTC"
    prompt_set: str
    steps: str                              # the models and efforts of steps 1 and 2, and the language
    tier_rules: Mapping[str, float]         # the names and values of TIER_RULES that the measurement used
    cv_runs: dict[str, list[CvRun]]
    job_runs: list[JobRun]
    n_postings: int
    measurement: Measurement
    failures: int                           # calls in this command without a result (paid, but no run)
    notes: list[str]

    def _cv_dates(self) -> list[str]:
        return sorted(run.created for runs in self.cv_runs.values() for run in runs)

    def _job_dates(self) -> list[str]:
        return sorted(run.created for run in self.job_runs)

    def _table(self, metrics: Mapping[str, tuple[str, str, str]], by_key: Mapping[str, dict[str, Tally]]) -> list[str]:
        keys = list(self.cv_runs)
        lines = ["| metric | " + " | ".join(keys) + " | all |", "|---|" + "---|" * (len(keys) + 1)]
        for name, (kind, label, _) in metrics.items():
            cells = [by_key[key][name].cell() for key in keys] + [_total(by_key, name, kind == "mean").cell()]
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        return lines

    def to_markdown(self) -> str:
        m = self.measurement
        rules = ", ".join(f"{name} {value}" for name, value in self.tier_rules.items())
        lines = [f"# skillpose eval --repeat - {self.created}", "", f"Prompt set: {self.prompt_set}", "", self.steps,
                 "", f"Tier rules: {rules}", "",
                 "Run-to-run variance: the same input analyzed several times, each time with a new call. Every "
                 "number compares all pairs of runs of the same input (n runs give n × (n − 1) / 2 pairs). The first "
                 "run of an input is its stored result of `eval --save` (base), if the model of the step made it.",
                 "", "## Runs", "", "| input | runs | pairs | days (UTC) |", "|---|---|---|---|"]
        for key, runs in self.cv_runs.items():
            lines.append(f"| CV {key} | {len(runs)} | {_pairs(len(runs))} | "
                         f"{_dates((r.created, r.base) for r in runs)} |")
        lines.append(f"| postings ({self.n_postings}) | {len(self.job_runs)} | {_pairs(len(self.job_runs))} | "
                     f"{_dates((r.created, r.base) for r in self.job_runs)} |")
        lines += ["", f"Calls without a result in this command: {self.failures} (paid and in the ledger, but no run).",
                  *[f"- {note}" for note in self.notes]]
        lines += ["", "## The same CV again", "", "The postings stay at their first run.", "",
                  *self._table(CV_METRICS, m.cv)]
        matched = {k: v for k, v in POSTING_METRICS.items() if k not in POSTINGS_ALONE}
        lines += ["", "## The same postings again", "", "Each CV stays at its first run.", "",
                  *self._table(matched, m.postings), "", "The postings alone, without a CV:", "",
                  "| metric | all |", "|---|---|"]
        lines += [f"| {POSTING_METRICS[name][1]} | {m.posting_only[name].cell()} |" for name in POSTINGS_ALONE]
        lines += ["", "## How the numbers are computed", "",
                  "A cell shows the value, then the comparisons it counts: \"0.88 (380 of 432)\" or \"0.95 (mean of "
                  "10)\". The column all counts the comparisons of every CV together.", "", "### The same CV again",
                  "", *[f"- **{label}**: {formula}" for _, label, formula in CV_METRICS.values()], "",
                  "### The same postings again", "",
                  *[f"- **{label}**: {formula}" for _, label, formula in POSTING_METRICS.values()],
                  "", "## Summary (JSON lines)", "", "One line per metric, over all CVs.", "", "```jsonl",
                  *self.json_lines(), "```", ""]
        return "\n".join(lines)

    def json_lines(self) -> list[str]:
        m = self.measurement
        out = []
        cv_dates, job_dates = self._cv_dates(), self._job_dates()
        common = {"prompt_set": self.prompt_set, "created": self.created, "tier_rules": dict(self.tier_rules)}
        for source, metrics, dates, runs in (
                ("cv", CV_METRICS, cv_dates, {key: len(r) for key, r in self.cv_runs.items()}),
                ("postings", POSTING_METRICS, job_dates, len(self.job_runs))):
            for name, (kind, _, _) in metrics.items():
                if source == "postings" and name in POSTINGS_ALONE:
                    tally = m.posting_only[name]
                else:
                    tally = _total(m.cv if source == "cv" else m.postings, name, kind == "mean")
                line: dict[str, object] = {"source": source, "metric": name,
                                           "value": None if tally.value is None else round(tally.value, 4)}
                line |= {"pairs": tally.total} if tally.mean else {"same": round(tally.same), "total": tally.total}
                line |= {"runs": runs, "first_run": dates[0][:10] if dates else None,
                         "last_run": dates[-1][:10] if dates else None, **common}
                out.append(json.dumps(line, ensure_ascii=False))
        return out


# --- the plan and the runs ---

@dataclass(frozen=True)
class StabilityPlan:
    """What a command makes: N and M are targets (stored runs per CV, runs of the postings), so the same command
    again makes only what is missing, as `demo build` does."""
    cv_missing: dict[str, int]   # CV key -> new runs of step 1, so that it has `repeat` runs that count
    jobs_missing: int            # new runs of the postings (a pending batch of this configuration counts as made)
    relation_calls: int          # step 3b calls at most: as `eval` counts them per new run, and the open pairs now
    n_postings: int
    batch: bool                  # the postings go as one Message Batch per run (from batch_threshold postings)
    usd: float                   # the estimate of every new call: an upper bound

    @property
    def n_cvs(self) -> int:
        return len(self.cv_missing)

    @property
    def cv_new(self) -> int:
        return sum(self.cv_missing.values())

    @property
    def calls(self) -> bool:
        """Whether the command needs the API: a new run or a relation check. A pending batch needs it too."""
        return bool(self.cv_new or self.jobs_missing or self.relation_calls)


def _config(prompt_set: str, settings: Settings, step: str, lang: str) -> str:
    cfg = settings.steps[step]
    return f"prompts {prompt_set}, {cfg.model}, effort {cfg.effort}, {lang}"


class _Relations:
    """Step 3b relations of the gold-set store (read-only) and of the stability store; a new pair goes to the
    stability store."""

    def __init__(self, own: Store, base: Store | None):
        self.own, self.base = own, base

    def relations(self, target: str) -> dict[str, Relation]:
        known = self.base.relations(target) if self.base is not None else {}
        return known | self.own.relations(target)

    def save_relations(self, target: str, rel: dict[str, Relation]) -> None:
        self.own.save_relations(target, rel)


@dataclass
class _Context:
    """The gold set and the configuration of one command."""
    settings: Settings
    store: StabilityStore
    base: Store | None
    vocab: Vocabulary
    cvs: dict[str, tuple[str, str]]   # CV key -> (text, language of the run)
    postings: dict[str, str]          # posting name -> text
    prompt_set: str
    configs: dict[str, str]           # language -> configuration of a CV run
    job_lang: str
    job_config: str

    @property
    def by_hash(self) -> dict[str, str]:
        return {text_hash(text): text for text in self.postings.values()}

    @property
    def batch(self) -> bool:
        return len(self.postings) >= self.settings.batch_threshold

    @property
    def relations(self) -> _Relations:
        return _Relations(self.store, self.base)


def _context(goldset: Path, settings: Settings, store: StabilityStore, base: Store | None, vocab: Vocabulary,
             lang: str) -> _Context:
    """ValueError or OSError for a gold set that cannot be read."""
    goldset = Path(goldset)
    expected = read_expected(goldset)
    check_request(expected, {"cv", "job"}, lang)
    cvs = {key: (read_cv(goldset / "cvs" / f"{key}.md"), entry["language"] if lang == "auto" else lang)
           for key, entry in (expected.get("cvs") or {}).items()}
    postings = {name: (goldset / "postings" / f"{name}.md").read_text(encoding="utf-8")
                for name in expected.get("postings") or {}}
    prompt_set = active_prompts().label
    job_lang = POSTING_LANG_AUTO if lang == "auto" else lang
    return _Context(settings=settings, store=store, base=base, vocab=vocab, cvs=cvs, postings=postings,
                    prompt_set=prompt_set,
                    configs={cv_lang: _config(prompt_set, settings, "cv", cv_lang) for _, cv_lang in cvs.values()},
                    job_lang=job_lang, job_config=_config(prompt_set, settings, "job", job_lang))


def _base_runs(ctx: _Context, notes: list[str]) -> tuple[dict[str, CvRun], JobRun | None]:
    """The stored results of `eval --save` that the models of the steps made."""
    base = ctx.base
    if base is None:
        return {}, None
    cv_model, job_model = ctx.settings.steps["cv"].model, ctx.settings.steps["job"].model
    cv_base: dict[str, CvRun] = {}
    for key, (text, _) in ctx.cvs.items():
        cv_id = base.find_cv(text)
        profile = base.profile(cv_id) if cv_id is not None else None
        if profile is None:
            notes.append(f"CV {key}: no stored profile, so no base run")
        elif (model := base.profile_model(cv_id)) != cv_model:
            notes.append(f"CV {key}: the stored profile was made by {model}, not by {cv_model}: left out")
        else:
            cv_base[key] = CvRun(base.profile_created(cv_id), profile, base=True)
    jobs, created, other = {}, [], 0
    for name, text in ctx.postings.items():
        h = text_hash(text)
        if (jp := base.job_profile(h)) is None:
            continue
        if base.job_profile_model(h) != job_model:
            other += 1
            continue
        jobs[name] = jp
        created.append(base.job_profile_created(h))
    if other:
        notes.append(f"{other} stored job profiles were made by another model than {job_model}: left out")
    if len(jobs) < len(ctx.postings):
        notes.append(f"the base run has {len(jobs)} of the {len(ctx.postings)} postings")
    return cv_base, JobRun(min(created), jobs, base=True) if jobs else None


def _stored_runs(ctx: _Context, notes: list[str]) -> tuple[dict[str, list[CvRun]], list[JobRun]]:
    """The runs of the stability store with the active configuration, oldest first. A result that another model
    answered (a server-side fallback) stays stored but does not count: the same rule as for the base."""
    cv_model, job_model = ctx.settings.steps["cv"].model, ctx.settings.steps["job"].model
    cv_runs: dict[str, list[CvRun]] = {}
    for key, (text, cv_lang) in ctx.cvs.items():
        h, other = text_hash(text), Counter()
        cv_runs[key] = []
        for run in ctx.store.runs("cv", ctx.configs[cv_lang]):
            if h not in run.results:
                continue
            model, raw = run.results[h]
            if model != cv_model:
                other[model] += 1
                continue
            cv_runs[key].append(CvRun(run.created, CandidateProfile.model_validate_json(raw)))
        if other:
            answered = ", ".join(f"{model} ({count})" for model, count in sorted(other.items()))
            notes.append(f"CV {key}: runs answered by another model than {cv_model}: {answered}; stored, left out")
    names = {text_hash(text): name for name, text in ctx.postings.items()}
    job_runs, other_jobs = [], 0
    for run in ctx.store.runs("job", ctx.job_config):
        jobs = {}
        for h, (model, raw) in run.results.items():
            if h not in names:
                continue
            if model != job_model:
                other_jobs += 1
                continue
            jobs[names[h]] = JobProfile.model_validate_json(raw)
        if jobs:
            job_runs.append(JobRun(run.created, jobs))
            if len(jobs) < len(ctx.postings):
                notes.append(f"the run of the postings of {run.created[:10]} has {len(jobs)} of the "
                             f"{len(ctx.postings)} postings")
    if other_jobs:
        notes.append(f"{other_jobs} job profiles of the stored runs were answered by another model than {job_model}: "
                     "left out")
    return cv_runs, job_runs


def _all_runs(ctx: _Context, notes: list[str]) -> tuple[dict[str, list[CvRun]], list[JobRun], dict[str, int], int]:
    """The base and the stored runs of every input, and how many stored runs count (per CV, of the postings)."""
    cv_base, job_base = _base_runs(ctx, notes)
    cv_stored, job_stored = _stored_runs(ctx, notes)
    cv_runs = {key: ([cv_base[key]] if key in cv_base else []) + cv_stored[key] for key in ctx.cvs}
    job_runs = ([job_base] if job_base is not None else []) + job_stored
    return cv_runs, job_runs, {key: len(runs) for key, runs in cv_stored.items()}, len(job_stored)


def _open_targets(pairs: Mapping[str, list[str]], relations: _Relations) -> int:
    """The targets of step 3b with a candidate that neither store knows: one call each."""
    count = 0
    for target, candidates in pairs.items():
        known = relations.relations(target)
        count += any(c not in known for c in candidates)
    return count


def _plan(ctx: _Context, repeat: int, repeat_jobs: int) -> StabilityPlan:
    cv_runs, job_runs, counted, jobs_counted = _all_runs(ctx, [])
    pending = sum(1 for batch in ctx.store.pending() if batch.config == ctx.job_config)
    cv_missing = {key: max(0, repeat - counted[key]) for key in ctx.cvs}
    jobs_missing = max(0, repeat_jobs - jobs_counted - pending)
    open_now = _open_targets(relation_pairs(cv_runs, job_runs, ctx.vocab), ctx.relations)
    # a pending batch costs nothing more (it was sent and is paid), but its run brings new pairs to check
    new_job_runs = jobs_missing + pending
    relation_calls = RELATION_CALLS * (sum(cv_missing.values()) + new_job_runs * len(ctx.cvs)) + open_now
    settings = ctx.settings
    usd = sum(n * estimate_cv_usd(settings, len(ctx.cvs[key][0])) for key, n in cv_missing.items())
    usd += jobs_missing * estimate_usd(settings, "job", len(ctx.postings), batch=ctx.batch)
    usd += estimate_usd(settings, "relation", relation_calls, avg_in=300, avg_out=300, fixed_tokens=6000)
    return StabilityPlan(cv_missing=cv_missing, jobs_missing=jobs_missing, relation_calls=relation_calls,
                         n_postings=len(ctx.postings), batch=ctx.batch, usd=usd)


def stability_plan(settings: Settings, goldset: Path, store: StabilityStore, base: Store | None, vocab: Vocabulary,
                   repeat: int, repeat_jobs: int, lang: str) -> StabilityPlan:
    """What a command with these targets makes and may cost, before any call: the missing runs of every CV at the
    estimate of step 1 for its length, the missing runs of the postings (batch price from batch_threshold postings),
    and the relation checks. Runs that count already (the same configuration, the model of the step) and pending
    batches of this configuration are not paid again. ValueError or OSError for a gold set that cannot be read."""
    return _plan(_context(goldset, settings, store, base, vocab, lang), repeat, repeat_jobs)


class _BatchCosts:
    """The ledger while a batch is read: the costs wait here and are booked together with the run and the end of the
    batch, in one transaction (StabilityStore.save_batch_run)."""

    def __init__(self, store: StabilityStore):
        self.store = store
        self.rows: list[BatchCost] = []

    def month_total_usd(self) -> float:
        return self.store.month_total_usd() + sum(row.usd for row in self.rows)

    def record(self, step: str, model: str, usage: Usage, cost_usd: float, batch: bool) -> None:
        self.rows.append(BatchCost(step=step, model=model, usage=usage, usd=cost_usd))


def _passes(error: anthropic.AnthropicError) -> bool:
    """An error that may pass: no connection, a timeout, 429 or 5xx. The next status request asks again."""
    if isinstance(error, anthropic.APIConnectionError):
        return True
    return isinstance(error, anthropic.APIStatusError) and (error.status_code == 429 or error.status_code >= 500)


def _collect(llm: LLM, ctx: _Context, batch: PendingBatch, notes: list[str], *, poll_seconds: float,
             sleep: Callable[[float], None], say: Callable[[str], None]) -> int:
    """Wait for a batch and store its results as one run of the configuration it was sent with. An error that may
    pass is asked again after poll_seconds; a lost batch is given up; any other error goes to the caller and the batch
    stays open. Returns the postings without a result."""
    texts = {h: ctx.by_hash[h] for h in batch.hashes if h in ctx.by_hash}
    say(f"waiting for batch {batch.id} - if this command is interrupted, the same command collects it")
    while True:
        costs = _BatchCosts(ctx.store)  # a read that broke off books nothing: the next read books every result
        try:
            status = llm.client.messages.batches.retrieve(batch.id)
            if status.processing_status != "ended":
                sleep(poll_seconds)
                continue
            results = read_jobs_batch(llm.client, ctx.settings, costs, batch.id, texts, ctx.vocab,
                                      seconds=batch_seconds(status))
            break
        except anthropic.AnthropicError as e:
            if (reason := batch_lost(e)) is not None:
                ctx.store.finish_batch(batch.id)
                notes.append(f"batch {batch.id} lost: {reason}")
                return 0
            if not _passes(e):
                raise
            say(f"batch {batch.id}: {type(e).__name__}, asking again in {poll_seconds:g} s")
            sleep(poll_seconds)
    found = {h: (ctx.settings.steps["job"].model, jp) for h, jp in results.items() if jp is not None}
    # a batch sent before its configuration was stored with it counts for no configuration
    ctx.store.save_batch_run(batch.id, batch.config or "unknown", found, costs.rows)
    say(f"batch {batch.id} collected: {len(found)} of {len(texts)} postings analyzed")
    return len(results) - len(found)


def run_stability(llm: LLM | None, goldset: Path, vocab: Vocabulary, settings: Settings, store: StabilityStore,
                  base: Store | None, *, repeat: int, repeat_jobs: int, lang: str, poll_seconds: float = 30.0,
                  sleep: Callable[[float], None] = time.sleep,
                  say: Callable[[str], None] = log.info) -> StabilityReport:
    """Make the runs that are missing for the targets (stability_plan: repeat runs per CV, repeat_jobs runs of the
    postings), store them in store (also its cost ledger), then the report over every run that counts, with the base
    (base: the gold-set store, opened read-only, or None). A pending batch is collected, never sent again.
    BudgetExceeded, an API error and Ctrl-C go to the caller: what was paid for stays stored, and the same command
    continues without paying it again. llm None: only when nothing is to be made (the report of the stored runs, no
    call). say gets a line for each step that takes long."""
    ctx = _context(goldset, settings, store, base, vocab, lang)
    plan = _plan(ctx, repeat, repeat_jobs)
    if llm is None and (plan.calls or store.pending()):
        raise ValueError("this command makes calls: it needs an API client")
    now = datetime.now(UTC)
    snapshot = vocab.snapshot()
    by_hash = ctx.by_hash
    cv_model = settings.steps["cv"].model
    failures = 0
    notes: list[str] = []

    if plan.batch:  # the batches go out first: they run while step 1 runs
        reserved = 0.0
        for _ in range(plan.jobs_missing):
            batch_id = submit_jobs_batch(llm.client, settings, store, by_hash, snapshot, ctx.job_lang,
                                         reserved_usd=reserved)
            store.save_pending_batch(batch_id, list(by_hash), ctx.job_config)  # collected later, never paid twice
            reserved += estimate_usd(settings, "job", len(by_hash), batch=True)
            say(f"batch {batch_id} with {len(by_hash)} postings submitted (half price)")

    for key, (text, cv_lang) in ctx.cvs.items():  # the runs of one CV one after the other: one cached prefix
        for number in range(1, plan.cv_missing[key] + 1):
            profile, result = extract_cv(llm, text, vocab, snapshot, cv_lang, now.year)
            if profile is None:
                failures += 1
                say(f"CV {key}: new run {number} of {plan.cv_missing[key]} gave no profile "
                    f"({result.stop_reason or result.error})")
                continue
            store.save_run("cv", ctx.configs[cv_lang], {text_hash(text): (result.model, profile)})
            if result.model != cv_model:
                say(f"CV {key}: new run {number} was answered by {result.model}: stored, but it does not count")

    for _ in range(0 if plan.batch else plan.jobs_missing):
        found = {}
        try:
            for name, text in ctx.postings.items():
                jp, result = extract_job(llm, text, vocab, snapshot, settings.role_families, ctx.job_lang,
                                         settings.max_posting_chars)
                if jp is None:
                    failures += 1
                    say(f"posting {name} gave no result ({result.error})")
                else:
                    found[text_hash(text)] = (result.model, jp)
        finally:  # a run that stops at the monthly limit keeps what it has
            if found:
                store.save_run("job", ctx.job_config, found)

    for batch in store.pending():
        failures += _collect(llm, ctx, batch, notes, poll_seconds=poll_seconds, sleep=sleep, say=say)

    cv_runs, job_runs, counted, jobs_counted = _all_runs(ctx, notes)
    for key, n in counted.items():
        if n < repeat:
            notes.append(f"CV {key}: {n} of {repeat} new runs count; the same command again makes the missing ones")
    if jobs_counted < repeat_jobs:
        notes.append(f"the postings: {jobs_counted} of {repeat_jobs} new runs count; the same command again makes "
                     "the missing ones")
    if not job_runs:
        notes.append("no run of the postings: the CV runs are compared without matches (run `eval --save` first, "
                     "or add --repeat-jobs)")
    pairs = relation_pairs(cv_runs, job_runs, vocab)
    service = RelationService(llm, ctx.relations, snapshot, vocab, workers=settings.relation_workers)
    relations = service.resolve(pairs)
    if open_after := _open_targets(pairs, ctx.relations):
        notes.append(f"{open_after} relation checks gave no answer: their pairs count as unrelated here; the same "
                     "command asks them again")
    cv, job = settings.steps["cv"], settings.steps["job"]
    return StabilityReport(
        created=now.strftime("%Y-%m-%d %H:%M UTC"), prompt_set=ctx.prompt_set,
        steps=f"Step 1: {cv.model} (effort {cv.effort}); step 2: {job.model} (effort {job.effort}); language: {lang}",
        tier_rules={name: getattr(settings.rules, name) for name in TIER_RULES},
        cv_runs=cv_runs, job_runs=job_runs, n_postings=len(ctx.postings),
        measurement=measure(cv_runs, job_runs, vocab, relations, settings.rules, now.year), failures=failures,
        notes=notes)
