"""Step 2: posting text -> JobProfile (one call; the batch variant lives in batch.py)."""

from skillpose.core.llm import LLM, CallResult
from skillpose.core.models import JobDraft, JobProfile, Requirement, RequirementDraft, SkillProposal, text_hash
from skillpose.core.prompts import job_fixed, wrap
from skillpose.core.vocabulary import Vocabulary


class PostingTooLong(ValueError):
    pass


def job_request_parts(text: str, snapshot: str, role_families: list[str], lang: str) -> tuple[list[str], str]:
    return job_fixed(snapshot, role_families, lang), wrap("posting", text)


def _requirement(r: RequirementDraft, vocab: Vocabulary, proposals: dict[str, SkillProposal]) -> Requirement:
    """A requirement of the job profile. The vocabulary checks its kind (a soft skill is soft); key and value of a
    condition are kept for the kind condition only."""
    skill_id = vocab.resolve_or_propose(r.skill_id, r.name, r.category, proposals)
    kind = vocab.requirement_kind(skill_id, r.kind, proposals)
    is_condition = kind == "condition"
    return Requirement(skill_id=skill_id, importance=r.importance, min_level=r.min_level, min_years=r.min_years,
                       kind=kind, quote=r.quote, condition=r.condition if is_condition else None,
                       condition_value=r.condition_value if is_condition else None)


def job_from_draft(draft: JobDraft, text: str, vocab: Vocabulary, role_families: list[str]) -> JobProfile:
    proposals: dict[str, SkillProposal] = {}
    requirements = [_requirement(r, vocab, proposals) for r in draft.requirements]
    family = draft.role_family if draft.role_family in role_families else "other"
    return JobProfile(text_hash=text_hash(text), title=draft.title, role_family=family, seniority=draft.seniority,
                      requirements=requirements, proposed_skills=list(proposals.values()))


def extract_job(llm: LLM, text: str, vocab: Vocabulary, snapshot: str, role_families: list[str], lang: str,
                max_chars: int) -> tuple[JobProfile | None, CallResult | None]:
    if len(text) > max_chars:
        raise PostingTooLong(f"posting has {len(text)} characters, limit is {max_chars}")
    fixed, variable = job_request_parts(text, snapshot, role_families, lang)
    result = llm.call("job", fixed, variable, JobDraft)
    if result.value is None:
        return None, result
    return job_from_draft(result.value, text, vocab, role_families), result
