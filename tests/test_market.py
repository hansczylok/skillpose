from skillpose.core.config import load_settings
from skillpose.core.market import role_reports
from skillpose.core.matching import match_job
from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    Conditions,
    Evidence,
    JobMatch,
    JobProfile,
    Requirement,
    RequirementMatch,
    RoleReport,
)
from skillpose.core.vocabulary import Vocabulary

R = load_settings().rules


def jp(h, family):
    return JobProfile(text_hash=h, title=h, role_family=family, seniority="mid", requirements=[])


def jm(h, tier, cov, *rms):
    return JobMatch(text_hash=h, matches=list(rms), must_coverage=cov, nice_coverage=0.0, tier=tier)


def rm(skill, importance, status, gap_type=None):
    r = Requirement(skill_id=skill, importance=importance, min_level=None, min_years=None, kind="tool", quote=skill)
    return RequirementMatch(requirement=r, status=status, reason="", evidence=None, gap_type=gap_type,
                            effort_weeks=None)


def test_role_tier_thresholds():
    for tiers, expected in [([1, 1, 2, 3, 3], 1), ([1, 2, 3, 3, 3], 2), ([2, 3, 3, 3, 3], 3)]:
        jobs = {f"h{i}": jp(f"h{i}", "qa") for i in range(5)}
        [rep] = role_reports(jobs, [jm(f"h{i}", t, 0.5) for i, t in enumerate(tiers)], R)
        assert rep.tier == expected and rep.postings == 5


def test_role_tier_share_exactly_on_threshold_counts():
    # 1/15 + 5/15 is 0.39999999999999997 in floats, but 6 of 15 postings are exactly 40 %: tier 2, not 3.
    tiers = [1] * 1 + [2] * 5 + [3] * 9
    jobs = {f"h{i}": jp(f"h{i}", "qa") for i in range(15)}
    [rep] = role_reports(jobs, [jm(f"h{i}", t, 0.5) for i, t in enumerate(tiers)], R)
    assert rep.tier == 2 and rep.postings == 15


def test_too_little_data_has_no_tier():
    jobs = {f"h{i}": jp(f"h{i}", "qa") for i in range(4)}
    ms = [jm(f"h{i}", 1, 0.9, rm("sql", "must", "partial", "weeks")) for i in range(4)]
    [rep] = role_reports(jobs, ms, R.model_copy(update={"min_jobs_per_role": 5}))
    assert rep.tier is None and rep.tier_shares[1] == 1.0
    assert rep.median_coverage == 0.9
    assert [(g.skill_id, g.weight, g.postings) for g in rep.top_gaps] == [("sql", 4.0, 4)]


def test_the_default_minimum_is_one_posting_and_a_few_postings_give_their_own_tier():
    """Some target roles have very few postings. With one posting the role tier is the tier of that
    posting, with two the better one (the share of one of two is 50 %, the default asks for 40 %)."""
    assert R.min_jobs_per_role == 1
    for tiers, expected in [([3], 3), ([2], 2), ([1], 1), ([3, 2], 2), ([3, 1], 1), ([3, 3], 3)]:
        jobs = {f"h{i}": jp(f"h{i}", "qa") for i in range(len(tiers))}
        [rep] = role_reports(jobs, [jm(f"h{i}", t, 0.5) for i, t in enumerate(tiers)], R)
        assert (rep.postings, rep.tier) == (len(tiers), expected), tiers


def test_gap_weights_and_order():
    ms = [jm("h0", 2, 0.5, rm("sql", "must", "gap", "months"), rm("aws", "nice", "gap", "months"),
             rm("git", "must", "met")),
          jm("h1", 2, 0.5, rm("sql", "must", "partial", "weeks"), rm("docker", "nice", "partial", "weeks"))]
    [rep] = role_reports({"h0": jp("h0", "x"), "h1": jp("h1", "x")}, ms, R)
    assert [(g.skill_id, g.weight, g.postings) for g in rep.top_gaps] == [("sql", 3.0, 2), ("aws", 1.0, 1),
                                                                         ("docker", 0.5, 1)]
    assert rep.top_gaps[0].gap_type == "months"


def test_median_order_and_unknown_hash():
    jobs = {"a": jp("a", "f1"), "b": jp("b", "f1"), "c": jp("c", "f1"), "d": jp("d", "f2")}
    ms = [jm("a", 1, 0.2), jm("b", 1, 0.5), jm("c", 1, 0.9), jm("d", 3, 0.1), jm("zz", 1, 1.0)]
    reps = role_reports(jobs, ms, R.model_copy(update={"min_jobs_per_role": 1}))
    assert [r.role_family for r in reps] == ["f1", "f2"] and reps[0].median_coverage == 0.5


def test_tier_shares_have_all_keys():
    jobs = {f"h{i}": jp(f"h{i}", "qa") for i in range(5)}
    [rep] = role_reports(jobs, [jm(f"h{i}", t, 0.5) for i, t in enumerate([1, 1, 3, 3, 3])], R)
    assert rep.tier_shares == {1: 0.4, 2: 0.0, 3: 0.6}


def test_top_gaps_limited_and_posting_counted_once_per_skill():
    ms = [jm("h0", 3, 0.0, rm("a", "must", "gap", "weeks"), rm("a", "nice", "gap", "weeks"),
            rm("b", "must", "gap", "weeks"), rm("c", "must", "gap", "weeks"))]
    [rep] = role_reports({"h0": jp("h0", "x")}, ms, R, top_n=2)
    assert [(g.skill_id, g.weight, g.postings) for g in rep.top_gaps] == [("a", 3.0, 1), ("b", 2.0, 1)]


def test_gap_type_ties_alphabetical_and_none_ignored():
    ms = [jm("h0", 3, 0.0, rm("a", "must", "gap", "weeks"), rm("b", "must", "partial")),
          jm("h1", 3, 0.0, rm("a", "must", "gap", "months"), rm("b", "must", "partial"))]
    [rep] = role_reports({"h0": jp("h0", "x"), "h1": jp("h1", "x")}, ms, R)
    by_id = {g.skill_id: g for g in rep.top_gaps}
    assert by_id["a"].gap_type == "months" and by_id["b"].gap_type is None


def test_reports_sorted_by_tier_none_last_then_postings():
    jobs = {"a": jp("a", "thin"), "b": jp("b", "big"), "c": jp("c", "big"), "d": jp("d", "easy"),
            "e": jp("e", "easy"), "f": jp("f", "easy")}
    ms = [jm("a", 1, 0.9), jm("b", 3, 0.1), jm("c", 3, 0.1), jm("d", 1, 0.9), jm("e", 1, 0.9), jm("f", 1, 0.9)]
    reps = role_reports(jobs, ms, R.model_copy(update={"min_jobs_per_role": 2}))
    assert [(r.role_family, r.tier) for r in reps] == [("easy", 1), ("big", 3), ("thin", None)]


def test_no_matches_no_reports():
    assert role_reports({"a": jp("a", "x")}, [], R) == []


# --- soft skills and conditions ---

V = Vocabulary.load()
CV_PROFILE = CandidateProfile(language="en", stations=[], skills=[
    CandidateSkill(skill_id="python", level="strong", years=6, last_used=2026, evidence=[
        Evidence(quote="Built tools in Python", context="Acme", period_from=2020, period_to=None, strength="outcome",
                 verified=True)])])


def need(skill_id, kind="tool", importance="must", condition=None):
    return Requirement(skill_id=skill_id, importance=importance, min_level=None, min_years=None, kind=kind,
                       quote=skill_id, condition=condition)


def matched(h, *requirements, **answers):
    """The match of a posting of the family qa with these requirements, against Python evidenced and answers."""
    job = JobProfile(text_hash=h, title=h, role_family="qa", seniority="mid", requirements=list(requirements))
    profile = CV_PROFILE.model_copy(update={"conditions": Conditions(**answers)})
    return job, match_job(job, profile, V, {}, R, 2026)


def test_soft_skills_are_no_top_gaps():
    """Spec test 4: two postings of one family, both with soft skills the CV does not name. Before, communication
    led the gaps (must in both postings)."""
    pairs = [matched("h0", need("python"), need("communication", "soft"), need("teamwork", "soft"),
                     need("sql", importance="nice")),
             matched("h1", need("python"), need("communication", "soft"), need("docker", importance="nice"))]
    [rep] = role_reports({jp.text_hash: jp for jp, _ in pairs}, [m for _, m in pairs], R)
    assert [g.skill_id for g in rep.top_gaps] == ["docker", "sql"]
    assert rep.conditions_blocking == []


def test_a_condition_that_blocks_is_listed_apart_from_the_gaps():
    pairs = [matched("h0", need("python"), need("driving_licence_b", "condition", condition="driving_licence_b"),
                     need("shift_work", "condition", condition="shift_work"), need("sql"), driving_licence_b=False),
             matched("h1", need("python"), need("driving_licence_b", "condition", condition="driving_licence_b"),
                     driving_licence_b=False),
             matched("h2", need("python"), need("driving_licence_b", "condition", "nice", "driving_licence_b"),
                     driving_licence_b=False)]
    [rep] = role_reports({jp.text_hash: jp for jp, _ in pairs}, [m for _, m in pairs], R)
    assert [(g.skill_id, g.weight, g.postings, g.gap_type) for g in rep.conditions_blocking] == [
        ("driving_licence_b", 4.0, 2, None)]  # a nice condition blocks nothing; shift work is unanswered
    assert [g.skill_id for g in rep.top_gaps] == ["sql"]


def test_an_old_role_report_loads_without_blocking_conditions():
    rep = RoleReport.model_validate_json('{"role_family": "qa", "postings": 1, "median_coverage": 1.0, "tier": 1, '
                                         '"tier_shares": {"1": 1.0, "2": 0.0, "3": 0.0}, "top_gaps": []}')
    assert rep.conditions_blocking == []


def test_a_soft_skill_gap_stored_before_the_new_statuses_is_no_top_gap():
    """A match computed before soft skills were not verifiable: the soft skill sits there as a gap of the kind months.
    The market view leaves it out by its kind, as long as the match is not recomputed."""
    soft = Requirement(skill_id="communication", importance="must", min_level=None, min_years=None, kind="soft",
                       quote="communication")
    old_gap = RequirementMatch(requirement=soft, status="gap", reason="not in the CV", evidence=None,
                               gap_type="months", effort_weeks=12)
    [rep] = role_reports({"h0": jp("h0", "qa")}, [jm("h0", 3, 0.5, old_gap, rm("sql", "must", "gap", "months"))], R)
    assert [g.skill_id for g in rep.top_gaps] == ["sql"] and rep.conditions_blocking == []
