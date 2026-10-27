"""Step 1: CV text -> CandidateProfile with verified evidence."""

from skillpose.core.evidence import apply_verification
from skillpose.core.llm import LLM, CallResult
from skillpose.core.models import (
    LEVEL_ORDER,
    CandidateProfile,
    CandidateSkill,
    CandidateSkillDraft,
    CvDraft,
    Evidence,
    SkillProposal,
)
from skillpose.core.prompts import cv_fixed, wrap
from skillpose.core.vocabulary import Vocabulary


def _last_used(evidence: list[Evidence], now_year: int) -> int | None:
    years = [e.period_to if e.period_to is not None else now_year
             for e in evidence if e.period_from is not None or e.period_to is not None]
    return max(years) if years else None


def _years_from_periods(evidence: list[Evidence], now_year: int) -> float | None:
    """Years of use as far as the evidence periods show: overlapping periods count once, a single year counts 0.5."""
    spans = sorted((e.period_from, max(e.period_from, e.period_to if e.period_to is not None else now_year))
                   for e in evidence if e.period_from is not None)
    if not spans:
        return None
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return float(sum(end - start or 0.5 for start, end in merged))


def _merge(existing: CandidateSkill, draft: CandidateSkillDraft, evidence: list[Evidence]) -> CandidateSkill:
    level = max(existing.level, draft.level, key=LEVEL_ORDER.__getitem__)
    years = max((y for y in (existing.years, draft.years) if y is not None), default=None)
    quotes = {e.quote for e in existing.evidence}
    return existing.model_copy(update={"level": level, "years": years,
                                       "evidence": existing.evidence + [e for e in evidence if e.quote not in quotes]})


def _with_computed_fields(skill: CandidateSkill, now_year: int) -> CandidateSkill:
    """Fill in what the program computes: last year of use, and years of use if Claude gave none."""
    years = skill.years if skill.years is not None else _years_from_periods(skill.evidence, now_year)
    return skill.model_copy(update={"last_used": _last_used(skill.evidence, now_year), "years": years})


def profile_from_draft(draft: CvDraft, text: str, vocab: Vocabulary, now_year: int) -> CandidateProfile:
    proposals: dict[str, SkillProposal] = {}
    skills: dict[str, CandidateSkill] = {}
    for d in draft.skills:
        skill_id = vocab.resolve_or_propose(d.skill_id, d.name, d.category, proposals)
        evidence = [Evidence(**e.model_dump()) for e in d.evidence]
        if skill_id in skills:
            skills[skill_id] = _merge(skills[skill_id], d, evidence)
        else:
            skills[skill_id] = CandidateSkill(skill_id=skill_id, level=d.level, years=d.years, evidence=evidence)
    finished = [_with_computed_fields(s, now_year) for s in skills.values()]
    return CandidateProfile(language=draft.language, skills=apply_verification(finished, text),
                            stations=draft.stations, proposed_skills=list(proposals.values()))


def extract_cv(llm: LLM, text: str, vocab: Vocabulary, snapshot: str, lang: str,
               now_year: int) -> tuple[CandidateProfile | None, CallResult]:
    result = llm.call("cv", cv_fixed(snapshot, lang), wrap("cv", text), CvDraft)
    if result.value is None:
        return None, result
    return profile_from_draft(result.value, text, vocab, now_year), result
