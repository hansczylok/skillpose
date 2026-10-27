import pytest
from fakes import FakeLLM

from skillpose.core.config import load_settings
from skillpose.core.extract_job import PostingTooLong, extract_job, job_from_draft, job_request_parts
from skillpose.core.models import JobDraft, RequirementDraft, text_hash
from skillpose.core.vocabulary import Vocabulary


def _req(skill_id: str, name: str, **kw) -> RequirementDraft:
    data = {"category": "tool", "importance": "must", "min_level": None, "min_years": None, "kind": "tool",
            "quote": name} | kw
    return RequirementDraft(skill_id=skill_id, name=name, **data)


def test_unknown_role_family_becomes_other_and_ids_resolve():
    draft = JobDraft(title="QA", role_family="quantum_qa", seniority="mid",
                     requirements=[_req("Selenium WebDriver", "Selenium")])
    jp = job_from_draft(draft, "posting text", Vocabulary.load(), load_settings().role_families)
    assert jp.role_family == "other" and jp.requirements[0].skill_id == "selenium"
    assert jp.text_hash == text_hash("posting text")


def test_new_skill_in_posting_becomes_proposal():
    draft = JobDraft(title="Automation", role_family="rpa_process_automation", seniority="mid",
                     requirements=[_req("n8n", "n8n"), _req("uipath", "UiPath", importance="nice")])
    jp = job_from_draft(draft, "text", Vocabulary.load(), load_settings().role_families)
    assert jp.role_family == "rpa_process_automation"
    assert [r.skill_id for r in jp.requirements] == ["n8n", "uipath"]
    assert [p.id for p in jp.proposed_skills] == ["n8n"]


def test_too_long_posting_is_not_sent():
    llm = FakeLLM(value=None)
    with pytest.raises(PostingTooLong):
        extract_job(llm, "x" * 30001, Vocabulary.load(), "S", ["other"], "de", 30000)
    assert llm.calls == []


def test_fixed_part_independent_of_posting():
    f1, v1 = job_request_parts("Job A", "SNAP", ["other"], "de")
    f2, v2 = job_request_parts("Job B", "SNAP", ["other"], "de")
    assert f1 == f2 and v1 != v2 and v1.startswith("<posting>")
    assert "other" in "".join(f1)


def test_extract_job_returns_profile():
    draft = JobDraft(title="QA", role_family="test_automation", seniority="senior", requirements=[_req("java", "Java")])
    llm = FakeLLM(value=draft)
    jp, result = extract_job(llm, "Senior QA posting", Vocabulary.load(), "S", load_settings().role_families, "en",
                             30000)
    assert jp.seniority == "senior" and llm.calls[0].step == "job" and result.error is None


# --- soft skills and conditions ---

def _job(*requirements: RequirementDraft):
    draft = JobDraft(title="QA", role_family="test_automation", seniority="mid", requirements=list(requirements))
    return job_from_draft(draft, "posting text", Vocabulary.load(), load_settings().role_families)


def test_a_condition_reaches_the_job_profile_with_its_key_and_value():
    jp = _job(_req("driving_licence_b", "Führerschein B", category="certificate", kind="condition",
                   condition="driving_licence_b"),
              # Claude files travel under soft: a condition stays one
              _req("travel_willingness", "Travel willingness", category="soft", kind="condition", condition="travel",
                   condition_value="ca. 30 %", quote="Reisebereitschaft ca. 30 %"),
              _req("flexibility", "Flexibilität", category="soft", kind="condition", condition="working_time",
                   condition_value="Vollzeit"),
              _req("availability", "Availability", category="domain", kind="condition", condition_value="ab sofort"),
              _req("python", "Python", condition="travel", condition_value="Linz"))  # no condition: neither is kept
    assert [(r.skill_id, r.kind, r.condition, r.condition_value) for r in jp.requirements] == [
        ("driving_licence_b", "condition", "driving_licence_b", None),
        ("travel_willingness", "condition", "travel", "ca. 30 %"),
        ("flexibility", "condition", "working_time", "Vollzeit"),
        ("availability", "condition", None, "ab sofort"),  # a condition without a key
        ("python", "tool", None, None)]
    assert jp.requirements[1].quote == "Reisebereitschaft ca. 30 %"
    assert [p.id for p in jp.proposed_skills] == ["travel_willingness", "availability"]


def test_a_soft_skill_given_the_kind_of_a_tool_certificate_degree_or_language_becomes_soft():
    """The post-check: a skill of the category soft (in the vocabulary, or as Claude proposes it) is a soft skill."""
    jp = _job(_req("communication", "Kommunikationsfähigkeit", kind="tool"),
              _req("teamwork", "Teamfähigkeit", kind="certificate"),
              _req("Strukturierte Arbeitsweise", "Strukturierte Arbeitsweise", kind="degree"),
              _req("initiative", "Eigeninitiative", kind="spoken_language"),
              _req("hands_on_mentality", "Hands-on mentality", category="soft", kind="tool"),  # a proposal
              _req("python", "Python", category="soft", kind="tool"))  # the vocabulary decides for a known skill
    assert [(r.skill_id, r.kind) for r in jp.requirements] == [
        ("communication", "soft"), ("teamwork", "soft"), ("structured_working", "soft"), ("initiative", "soft"),
        ("hands_on_mentality", "soft"), ("python", "tool")]


def test_the_post_check_keeps_experience():
    """Spec B: "Erfahrung in der Führung von Teams" is experience, not soft - a CV can show it. The vocabulary files
    leadership under soft, so the post-check leaves the kind experience as it is; a condition
    stays one too (see above)."""
    jp = _job(_req("Team leadership", "Erfahrung in der Führung von Teams", category="soft", kind="experience",
                   min_years=3, quote="mehrjährige Erfahrung in der Führung von Teams"),
              _req("communication", "Kommunikationsfähigkeit", kind="experience"))
    assert [(r.skill_id, r.kind, r.min_years) for r in jp.requirements] == [
        ("leadership", "experience", 3), ("communication", "experience", None)]
