import re

from fakes import FakeLLM

from skillpose.core.extract_cv import _years_from_periods, extract_cv, profile_from_draft
from skillpose.core.models import CandidateSkillDraft, CvDraft, Evidence, EvidenceDraft, Station
from skillpose.core.prompts import cv_fixed, wrap
from skillpose.core.vocabulary import Vocabulary


def test_fixed_part_has_no_volatile_content():
    a = cv_fixed(Vocabulary.load().snapshot(), "de")
    b = cv_fixed(Vocabulary.load().snapshot(), "de")
    assert a == b and not re.search(r"\d{4}-\d{2}-\d{2}", "".join(a))
    assert "German" in "".join(a) and "power_bi |" in a[-1]


def test_wrap_marks_untrusted_text():
    assert wrap("cv", "text") == "<cv>\ntext\n</cv>"


def test_profile_from_draft_maps_merges_and_verifies():
    draft = CvDraft(language="de", stations=[Station(title="Analyst", employer="Firma A", period_from=2021,
                                                     period_to=None)], skills=[
        CandidateSkillDraft(skill_id="PowerBI", name="Power BI", category="tool", level="working", years=2,
                            evidence=[EvidenceDraft(quote="Dashboards in Power BI erstellt", context="Firma A",
                                                    period_from=2021, period_to=None, strength="activity")]),
        CandidateSkillDraft(skill_id="power_bi", name="Power BI", category="tool", level="strong", years=3,
                            evidence=[EvidenceDraft(quote="frei erfunden", context="", period_from=None,
                                                    period_to=None, strength="outcome")]),
        CandidateSkillDraft(skill_id="n8n_flows", name="n8n", category="tool", level="basic", years=None,
                            evidence=[])])
    llm = FakeLLM(value=draft)
    profile, _ = extract_cv(llm, "Ich habe Dashboards in Power BI erstellt. " * 10, Vocabulary.load(), "SNAP", "de",
                            2026)
    pbi = next(s for s in profile.skills if s.skill_id == "power_bi")
    assert pbi.level == "strong" and pbi.years == 3 and pbi.last_used == 2026 and len(pbi.evidence) == 2
    assert [e.verified for e in pbi.evidence] == [True, False]
    assert [p.id for p in profile.proposed_skills] == ["n8n"]
    assert next(s for s in profile.skills if s.skill_id == "n8n").last_used is None
    assert profile.stations[0].employer == "Firma A"
    assert llm.calls[0].step == "cv" and llm.calls[0].variable.startswith("<cv>")


def test_last_used_from_closed_period():
    draft = CvDraft(language="en", stations=[], skills=[
        CandidateSkillDraft(skill_id="cpp", name="C++", category="programming_language", level="strong", years=8,
                            evidence=[EvidenceDraft(quote="x", context="", period_from=2005, period_to=2013,
                                                    strength="activity")])])
    profile, _ = extract_cv(FakeLLM(value=draft), "y" * 300, Vocabulary.load(), "S", "en", 2026)
    assert profile.skills[0].last_used == 2013


def test_failed_call_returns_none():
    profile, result = extract_cv(FakeLLM(value=None, error="refusal"), "x" * 300, Vocabulary.load(), "S", "de", 2026)
    assert profile is None and result.error == "refusal"


def test_years_estimated_from_evidence_periods_when_unknown():
    def ev(f, t):
        return EvidenceDraft(quote="Dashboards in Power BI erstellt", context="A", period_from=f, period_to=t,
                             strength="activity")
    draft = CvDraft(language="de", stations=[], skills=[
        CandidateSkillDraft(skill_id="power_bi", name="Power BI", category="tool", level="working", years=None,
                            evidence=[ev(2015, 2018), ev(2017, 2020)]),
        CandidateSkillDraft(skill_id="sql", name="SQL", category="programming_language", level="working", years=None,
                            evidence=[ev(2022, None)]),
        CandidateSkillDraft(skill_id="excel", name="Excel", category="tool", level="working", years=None,
                            evidence=[])])
    p = profile_from_draft(draft, "Ich habe Dashboards in Power BI erstellt. " * 10, Vocabulary.load(), 2026)
    years = {s.skill_id: s.years for s in p.skills}
    assert years == {"power_bi": 5, "sql": 4, "excel": None}


def test_years_from_periods_merges_touching_periods_and_counts_a_single_year_half():
    def ev(f, t):
        return Evidence(quote="q", context="A", period_from=f, period_to=t, strength="activity")
    assert _years_from_periods([ev(2015, 2018), ev(2018, 2020), ev(2022, 2022)], 2026) == 5.5
    assert _years_from_periods([ev(2020, 2020)], 2026) == 0.5
    assert _years_from_periods([ev(None, 2020), ev(None, None)], 2026) is None


def test_stated_years_are_kept_when_periods_disagree():
    draft = CvDraft(language="de", stations=[], skills=[
        CandidateSkillDraft(skill_id="sql", name="SQL", category="programming_language", level="working", years=7,
                            evidence=[EvidenceDraft(quote="SQL", context="A", period_from=2024, period_to=2025,
                                                    strength="activity")])])
    assert profile_from_draft(draft, "SQL", Vocabulary.load(), 2026).skills[0].years == 7
