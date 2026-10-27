"""Step 5: development path per target role.

Claude gets the person once as a short summary (fixed part, so the cached prefix is shared by every target role) and
one role report (variable part). The program checks the answer: only skill ids of the report may be closed, addresses
are removed (a path names activities, never courses or links), and the total effort is summed up here, not by Claude.
"""

import logging
import re

from skillpose.core.llm import LLM, CallResult
from skillpose.core.models import (
    STRENGTH_ORDER,
    CandidateProfile,
    DevelopmentPath,
    PathDraft,
    PathStep,
    RoleReport,
    Station,
    text_hash,
)
from skillpose.core.prompts import path_fixed, wrap
from skillpose.core.vocabulary import Vocabulary

log = logging.getLogger(__name__)

MAX_STEPS = 5
SKILLS_HEADER = "id | name_en | category | level | years | last_used | best_strength"
STATIONS_HEADER = "title | employer | from-to"
GAPS_HEADER = "skill_id | name_en | weight | postings | gap_type"
UNKNOWN = "-"
_ADDRESS = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _num(value: float | None) -> str:
    return UNKNOWN if value is None else f"{value:g}"


def _skill_lines(profile: CandidateProfile, vocab: Vocabulary) -> list[str]:
    proposals = {p.id: p for p in profile.proposed_skills}
    lines = []
    for s in sorted(profile.visible().skills, key=lambda s: s.skill_id):
        known, proposed = vocab.get(s.skill_id), proposals.get(s.skill_id)
        name = known.name_en if known else proposed.name if proposed else UNKNOWN
        category = known.category if known else proposed.category if proposed else UNKNOWN
        best = max((e.strength for e in s.evidence), key=STRENGTH_ORDER.__getitem__, default=UNKNOWN)
        lines.append(" | ".join([s.skill_id, name, category, s.level, _num(s.years), _num(s.last_used), best]))
    return lines


def _station_key(s: Station) -> tuple:
    """Unknown starts last, then by start, end (still running last), title and employer: a stable order."""
    return (s.period_from is None, s.period_from or 0, s.period_to is None, s.period_to or 0, s.title, s.employer)


def _period(s: Station) -> str:
    if s.period_from is None and s.period_to is None:
        return UNKNOWN
    return f"{'?' if s.period_from is None else s.period_from}-{'now' if s.period_to is None else s.period_to}"


def profile_summary(profile: CandidateProfile, vocab: Vocabulary) -> str:
    """The person in a few lines. It is the cached prefix of every path request, so it must not change between two
    calls for the same profile: skills sorted by id, stations by start year (unknown starts last), unknown values "-",
    skills the user hid left out."""
    stations = [" | ".join([_one_line(s.title), _one_line(s.employer), _period(s)])
                for s in sorted(profile.stations, key=_station_key)]
    return "\n".join([SKILLS_HEADER, *_skill_lines(profile, vocab), STATIONS_HEADER, *stations])


def report_text(report: RoleReport, vocab: Vocabulary) -> str:
    """The role report for Claude: only its gaps, which hold no soft skill and no condition (a path cannot close
    them); report.conditions_blocking stays out."""
    gaps = []
    for g in report.top_gaps:
        skill = vocab.get(g.skill_id)
        gaps.append(" | ".join([g.skill_id, skill.name_en if skill else UNKNOWN, f"{g.weight:.1f}", str(g.postings),
                                g.gap_type or UNKNOWN]))
    return "\n".join([f"role_family: {report.role_family}", f"postings: {report.postings}",
                      f"median_must_coverage: {report.median_coverage:.0%}",
                      f"tier: {report.tier if report.tier is not None else 'too little data'}",
                      GAPS_HEADER, *gaps])


def path_request_parts(profile: CandidateProfile, report: RoleReport, vocab: Vocabulary,
                       lang: str) -> tuple[list[str], str]:
    return path_fixed(profile_summary(profile, vocab), lang), wrap("role_report", report_text(report, vocab))


def path_input_hash(fixed: list[str], variable: str) -> str:
    return text_hash("\n".join([*fixed, variable]))


def _without_addresses(text: str) -> str:
    return re.sub(r" {2,}", " ", _ADDRESS.sub("", text)).strip()


def path_from_draft(draft: PathDraft, report: RoleReport, input_hash: str, lang: str) -> DevelopmentPath | None:
    """The checked path, or None if Claude gave no steps."""
    allowed = {g.skill_id for g in report.top_gaps}
    steps = []
    for d in draft.steps[:MAX_STEPS]:
        closes = list(dict.fromkeys(i for i in d.closes if i in allowed))
        if dropped := [i for i in d.closes if i not in allowed]:
            log.warning("path step closes ids that are not in the %s report: %s", report.role_family, dropped)
        steps.append(PathStep(activity=_without_addresses(d.activity), closes=closes,
                              effort_weeks=max(1, d.effort_weeks), proof=_without_addresses(d.proof)))
    if not steps:
        return None
    return DevelopmentPath(role_family=report.role_family, language=lang, rationale=_without_addresses(draft.rationale),
                           steps=steps, total_weeks=sum(s.effort_weeks for s in steps), input_hash=input_hash)


def build_path(llm: LLM, profile: CandidateProfile, report: RoleReport, vocab: Vocabulary,
               lang: str) -> tuple[DevelopmentPath | None, CallResult]:
    fixed, variable = path_request_parts(profile, report, vocab, lang)
    result = llm.call("path", fixed, variable, PathDraft)
    if result.value is None:
        return None, result
    return path_from_draft(result.value, report, path_input_hash(fixed, variable), lang), result
