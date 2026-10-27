from typing import get_args

from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    CandidateSkillDraft,
    ConditionKey,
    Conditions,
    EvidenceDraft,
    JobProfile,
    ReqKind,
    RequirementDraft,
    RequirementMatch,
    text_hash,
)


def test_text_hash_ignores_whitespace_differences():
    assert text_hash("Python  developer\n(m/w/d)") == text_hash("Python developer (m/w/d)")
    assert len(text_hash("x")) == 16


def test_candidate_skill_defaults():
    cs = CandidateSkill(skill_id="python", level="strong", years=5, evidence=[])
    assert cs.last_used is None


def test_cv_draft_has_no_code_computed_fields():
    fields = set(CandidateSkillDraft.model_fields)
    assert "last_used" not in fields and "verified" not in EvidenceDraft.model_fields


# --- soft skills and conditions ---

OLD_REQUIREMENT = ('{"skill_id": "python", "importance": "must", "min_level": null, "min_years": null, "kind": "tool", '
                   '"quote": "Python"}')


def test_an_old_job_profile_and_an_old_match_load_without_a_condition():
    jp = JobProfile.model_validate_json('{"text_hash": "h", "title": "QA", "role_family": "qa", "seniority": "mid", '
                                        f'"requirements": [{OLD_REQUIREMENT}]}}')
    assert jp.requirements[0].condition is None and jp.requirements[0].condition_value is None
    m = RequirementMatch.model_validate_json(f'{{"requirement": {OLD_REQUIREMENT}, "status": "gap", '
                                             '"reason": "not in the CV", "evidence": null, "gap_type": "months", '
                                             '"effort_weeks": 12}')
    assert m.status == "gap" and m.requirement.condition is None


def test_an_old_profile_loads_with_every_condition_unanswered():
    profile = CandidateProfile.model_validate_json('{"language": "de", "skills": [], "stations": []}')
    assert profile.conditions == Conditions()
    assert all(value is None for name, value in profile.conditions if name != "notes")
    assert profile.conditions.notes == ""


def test_the_conditions_never_go_into_the_stored_profile():
    """One record per database: the profile of a CV does not carry a copy of them."""
    profile = CandidateProfile(language="en", skills=[], stations=[], conditions=Conditions(driving_licence_b=True))
    assert "conditions" not in profile.model_dump_json() and "conditions" not in profile.model_dump()
    assert profile.visible().conditions.driving_licence_b is True  # the matching copy keeps them


def test_the_answer_schema_of_step_2_has_every_kind_and_an_optional_condition():
    """Step 2 marks soft skills and conditions (step 2): it may give every kind of a requirement, and for a
    condition its key and value. Both are optional, so a requirement that is no condition needs neither."""
    schema = RequirementDraft.model_json_schema()
    assert "condition" in get_args(ReqKind) and schema["properties"]["kind"]["enum"] == list(get_args(ReqKind))
    assert {"condition", "condition_value"} <= set(schema["properties"])
    assert not {"condition", "condition_value"} & set(schema["required"])
    assert [option.get("enum") for option in schema["properties"]["condition"]["anyOf"]] == [
        list(get_args(ConditionKey)), None]
    draft = RequirementDraft(skill_id="python", name="Python", category="programming_language", importance="must",
                             min_level=None, min_years=None, kind="tool", quote="Python")
    assert draft.condition is None and draft.condition_value is None
