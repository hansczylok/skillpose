"""Gold-set evaluation of the analysis: extraction (steps 1 and 2), match status and role tiers (steps 3 and 4), with
the cost and cache hit rate per step.

The expected match statuses and role tiers of the gold set are derived with the rules of the program, so the match
step measures extraction, relations and rules together. With a store, the eval saves its CVs, profiles, postings, job
profiles, relations and matches exactly as `skillpose demo build` reads the gold set: the build then finds them and
pays only for the development paths. Skills Claude proposes stay out of that store, so the build sees the vocabulary
the eval saw.
"""

from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

import yaml

from skillpose.core.config import Rules, Settings
from skillpose.core.extract_cv import extract_cv
from skillpose.core.extract_job import extract_job
from skillpose.core.llm import LLM, CallResult
from skillpose.core.market import role_reports
from skillpose.core.matching import match_job, needed_pairs
from skillpose.core.models import STRENGTH_ORDER, CandidateProfile, JobMatch, JobProfile
from skillpose.core.prompts import active_prompts
from skillpose.core.relations import RelationService
from skillpose.core.textio import read_cv
from skillpose.core.vocabulary import Vocabulary
from skillpose.store.db import Store

STEPS = ("cv", "job", "match")
LANGUAGES = ("de", "en")
POSTING_LANG_AUTO = "en"  # with lang "auto" postings are analyzed in English, as in phase 1 and the demo build


def _ids(names: list[str], vocab: Vocabulary) -> set[str]:
    return {vocab.resolve(n) or n for n in names}


def _share(part: int, whole: int) -> float:
    return part / whole if whole else 1.0


def cv_metrics(profile: CandidateProfile, expected: dict, vocab: Vocabulary) -> dict[str, float]:
    found = {s.skill_id: s for s in profile.skills}
    wanted, traps = _ids(expected["skills"], vocab), _ids(expected.get("not_skills", []), vocab)
    correct = 0
    for name, strength in expected.get("strength", {}).items():
        skill = found.get(vocab.resolve(name) or name)
        if skill and skill.evidence and max(skill.evidence, key=lambda e: STRENGTH_ORDER[e.strength]).strength \
                == strength:
            correct += 1
    evidence = [e for s in profile.skills for e in s.evidence]
    return {"recall": _share(len(wanted & set(found)), len(wanted)),
            "trap_hits": float(len(traps & set(found))),
            "strength_accuracy": _share(correct, len(expected.get("strength", {}))),
            "verified_share": sum(e.verified for e in evidence) / len(evidence) if evidence else 0.0}


def job_metrics(jp: JobProfile, expected: dict, vocab: Vocabulary) -> dict[str, float]:
    importance = {r.skill_id: r.importance for r in jp.requirements}
    found_must = {i for i, imp in importance.items() if imp == "must"}
    want_must, want_nice = _ids(expected["must"], vocab), _ids(expected["nice"], vocab)
    want = {**dict.fromkeys(want_nice, "nice"), **dict.fromkeys(want_must, "must")}
    both = set(want) & set(importance)
    return {"role_family_ok": float(jp.role_family == expected["role_family"]),
            "seniority_ok": float(jp.seniority == expected.get("seniority", jp.seniority)),
            "must_recall": _share(len(want_must & set(importance)), len(want_must)),
            "nice_recall": _share(len(want_nice & set(importance)), len(want_nice)),
            "importance_accuracy": _share(sum(importance[i] == want[i] for i in both), len(both)),
            "must_precision": _share(len(found_must & set(want)), len(found_must))}


def match_metrics(matches: dict[str, JobMatch], jobs: dict[str, JobProfile], expected: dict, vocab: Vocabulary,
                  rules: Rules) -> dict[str, float]:
    """For one person: share of expected statuses met (a requirement the posting lacks, or a posting without match,
    counts as wrong) and share of expected role tiers met (a family without a tier counts as wrong). Keys of matches
    and jobs are the posting names of the gold set."""
    pairs = [(posting, vocab.resolve(name) or name, status)
             for posting, statuses in (expected.get("match_status") or {}).items()
             for name, status in statuses.items()]
    correct = 0
    for posting, skill_id, status in pairs:
        jm = matches.get(posting)
        found = next((m.status for m in jm.matches if m.requirement.skill_id == skill_id), None) if jm else None
        correct += found == status
    by_hash = {jp.text_hash: jp for jp in jobs.values()}
    tiers = {r.role_family: r.tier for r in role_reports(by_hash, matches.values(), rules)}
    wanted = expected.get("role_tiers") or {}
    return {"match_status_accuracy": _share(correct, len(pairs)),
            "role_tier_accuracy": _share(sum(tiers.get(f) == tier for f, tier in wanted.items()), len(wanted))}


@dataclass
class StepStats:
    model: str
    effort: str
    items: list[tuple[str, dict[str, float]]] = field(default_factory=list)
    failures: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    cache_read_tokens: int = 0

    def add_call(self, result: CallResult | None) -> None:
        if result is None:
            return
        self.cost_usd += result.cost_usd
        if result.usage:
            u = result.usage
            self.input_tokens += u.input_tokens + u.cache_write_tokens + u.cache_read_tokens
            self.cache_read_tokens += u.cache_read_tokens

    def averages(self) -> dict[str, float]:
        keys = self.items[0][1].keys() if self.items else []
        return {k: mean(m[k] for _, m in self.items) for k in keys}

    @property
    def cache_read_share(self) -> float:
        return self.cache_read_tokens / self.input_tokens if self.input_tokens else 0.0


@dataclass
class EvalReport:
    steps: dict[str, StepStats]
    created: str
    prompt_set: str  # label of the prompts the run used: the examples give other results than the private prompts

    def to_markdown(self) -> str:
        lines = [f"# skillpose eval - {self.created}", "", f"Prompt set: {self.prompt_set}", ""]
        for step, stats in self.steps.items():
            n = len(stats.items) + stats.failures
            per_item = stats.cost_usd / n if n else 0.0
            lines += [f"## Step {step} - {stats.model} (effort {stats.effort})", "",
                      f"Items: {n}, failed: {stats.failures}, cost: {stats.cost_usd:.4f} USD "
                      f"({per_item:.5f} per item), cache read share: {stats.cache_read_share:.0%}",
                      "", "| metric | mean |", "|---|---|"]
            lines += [f"| {k} | {v:.2f} |" for k, v in stats.averages().items()]
            if stats.items:
                keys = list(stats.items[0][1])
                lines += ["", "| item | " + " | ".join(keys) + " |", "|---|" + "---|" * len(keys)]
                lines += [f"| {name} | " + " | ".join(f"{m[k]:.2f}" for k in keys) + " |" for name, m in stats.items]
            lines.append("")
        return "\n".join(lines)


class _CountingLLM:
    """Passes each call on and adds its result to the stats of a step: RelationService returns no CallResult."""

    def __init__(self, llm: LLM, stats: StepStats):
        self.llm, self.stats = llm, stats

    def call(self, step, fixed, variable, schema):
        result = self.llm.call(step, fixed, variable, schema)
        self.stats.add_call(result)
        return result

    def call_many(self, step, fixed, variables, schema, on_result, *, workers=1):
        def counted(index, result):
            self.stats.add_call(result)
            on_result(index, result)

        self.llm.call_many(step, fixed, variables, schema, counted, workers=workers)


def read_expected(goldset: Path) -> dict:
    return yaml.safe_load((Path(goldset) / "expected.yaml").read_text(encoding="utf-8")) or {}


def check_request(expected: dict, steps: set[str], lang: str) -> None:
    """ValueError before any call: an unknown step or language, match without cv and job, or (lang "auto") a CV
    without its language in expected.yaml."""
    if unknown := sorted(set(steps) - set(STEPS)):
        raise ValueError(f"unknown steps {unknown}; known: {', '.join(STEPS)}")
    if "match" in steps and not {"cv", "job"} <= set(steps):
        raise ValueError("step match needs the steps cv and job in the same run")
    if lang not in (*LANGUAGES, "auto"):
        raise ValueError(f"language must be de, en or auto, not {lang!r}")
    if lang == "auto" and "cv" in steps:
        for key, entry in (expected.get("cvs") or {}).items():
            if (language := (entry or {}).get("language")) not in LANGUAGES:
                raise ValueError(f"expected.yaml: cvs.{key}.language must be de or en, not {language!r}")


def _cv_id(store: Store, key: str, text: str) -> int:
    """The CV row of this text (the demo build finds it the same way), created under <key>.md if missing."""
    cv_id = store.find_cv(text)
    return cv_id if cv_id is not None else store.save_cv(f"{key}.md", text)


def _relation_pairs(profile: CandidateProfile, jobs: Mapping[str, JobProfile],
                    vocab: Vocabulary) -> dict[str, list[str]]:
    """The pairs step 3b asks for one person, collected over all postings as pipeline.rematch does."""
    wanted: dict[str, set[str]] = {}
    for jp in jobs.values():
        for target, candidates in needed_pairs(jp, profile, vocab).items():
            wanted.setdefault(target, set()).update(candidates)
    return {target: sorted(candidates) for target, candidates in wanted.items()}


def run_eval(llm: LLM, goldset: Path, vocab: Vocabulary, settings: Settings, steps: set[str], lang: str,
             store: Store | None = None) -> EvalReport:
    """steps: a subset of {cv, job, match}; match needs cv and job in the same run. lang "auto": each CV in the
    language of expected.yaml (cvs.<key>.language), postings in English. With a store, the results are saved there
    (see the module docstring); without one, the relations of the match step live in a store in memory."""
    goldset = Path(goldset)
    expected = read_expected(goldset)
    check_request(expected, steps, lang)
    snapshot = vocab.snapshot()
    now_year = datetime.now(UTC).year
    report = EvalReport(steps={}, created=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
                        prompt_set=active_prompts().label)
    profiles: dict[str, CandidateProfile] = {}
    cv_ids: dict[str, int] = {}
    jobs: dict[str, JobProfile] = {}

    if "cv" in steps:
        stats = report.steps["cv"] = StepStats(settings.steps["cv"].model, settings.steps["cv"].effort)
        for name, exp in expected["cvs"].items():
            text = read_cv(goldset / "cvs" / f"{name}.md")
            if store is not None:
                cv_ids[name] = _cv_id(store, name, text)
            profile, result = extract_cv(llm, text, vocab, snapshot, exp["language"] if lang == "auto" else lang,
                                         now_year)
            stats.add_call(result)
            if profile is None:
                stats.failures += 1
                continue
            stats.items.append((name, cv_metrics(profile, exp, vocab)))
            profiles[name] = profile
            if store is not None:
                store.save_profile(cv_ids[name], profile, result.model)

    if "job" in steps:
        stats = report.steps["job"] = StepStats(settings.steps["job"].model, settings.steps["job"].effort)
        for name, exp in expected["postings"].items():
            path = goldset / "postings" / f"{name}.md"
            text = path.read_text(encoding="utf-8")
            posting_hash = store.save_posting(text, path.name) if store is not None else None
            jp, result = extract_job(llm, text, vocab, snapshot, settings.role_families,
                                     POSTING_LANG_AUTO if lang == "auto" else lang, settings.max_posting_chars)
            stats.add_call(result)
            if jp is None:
                stats.failures += 1
                if store is not None:
                    store.set_posting_error(posting_hash, result.error or "no result")
                continue
            stats.items.append((name, job_metrics(jp, exp, vocab)))
            jobs[name] = jp
            if store is not None:
                store.save_job_profile(jp, result.model)

    if "match" in steps:
        stats = report.steps["match"] = StepStats(settings.steps["relation"].model, settings.steps["relation"].effort)
        with nullcontext(store) if store is not None else Store(Path(":memory:")) as relation_store:
            service = RelationService(_CountingLLM(llm, stats), relation_store, snapshot, vocab,
                                      workers=settings.relation_workers)
            for name, exp in expected["cvs"].items():
                profile = profiles.get(name)
                if profile is None:  # step 1 failed for this CV
                    stats.failures += 1
                    continue
                relations = service.resolve(_relation_pairs(profile, jobs, vocab))
                matches = {posting: match_job(jp, profile, vocab, relations, settings.rules, now_year)
                           for posting, jp in jobs.items()}
                if store is not None:
                    for jm in matches.values():
                        store.save_match(cv_ids[name], jm)
                stats.items.append((name, match_metrics(matches, jobs, exp, vocab, settings.rules)))
    return report
