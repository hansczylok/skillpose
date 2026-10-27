import pytest

from skillpose.cli import _top_gaps
from skillpose.core.config import load_settings
from skillpose.core.matching import _degree_fields, _degree_rank, match_job, match_requirement, needed_pairs
from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    Conditions,
    Evidence,
    JobProfile,
    Requirement,
    SkillProposal,
)
from skillpose.core.vocabulary import Vocabulary

R, Y = load_settings().rules, 2026
V = Vocabulary.load()


def sk(skill_id, level, years, last=None, strength="activity") -> CandidateSkill:
    ev = Evidence(quote=f"quote {skill_id}", context="Acme", period_from=(last - 1) if last else None, period_to=last,
                  strength=strength, verified=True)
    return CandidateSkill(skill_id=skill_id, level=level, years=years, last_used=last, evidence=[ev])


def prof(*skills) -> CandidateProfile:
    return CandidateProfile(language="en", skills=list(skills), stations=[])


def req(skill_id, importance="must", min_level=None, min_years=None, kind="tool") -> Requirement:
    return Requirement(skill_id=skill_id, importance=importance, min_level=min_level, min_years=min_years, kind=kind,
                       quote=skill_id)


def job(*reqs) -> JobProfile:
    return JobProfile(text_hash="h", title="t", role_family="other", seniority="mid", requirements=list(reqs))


def test_met_with_recent_activity():
    m = match_requirement(req("python", min_level="working"), prof(sk("python", "strong", 6, last=2025,
                                                                      strength="activity")), V, {}, R, Y)
    assert m.status == "met" and m.evidence is not None and m.gap_type is None


def test_claim_only_is_partial():
    m = match_requirement(req("sql", min_level="working"), prof(sk("sql", "working", 3, last=2025, strength="claim")),
                          V, {}, R, Y)
    assert m.status == "partial" and m.gap_type == "weeks" and m.effort_weeks == 4


def test_stale_skill_is_partial():
    m = match_requirement(req("cpp"), prof(sk("cpp", "strong", 10, last=2013, strength="outcome")), V, {}, R, Y)
    assert m.status == "partial" and "2013" in m.reason


def test_listed_language_certificate_and_degree_are_not_claims_and_do_not_go_stale():
    p = prof(sk("english", "strong", 20, last=2014, strength="claim"),
             sk("istqb_foundation", "working", None, last=2014, strength="claim"),
             sk("masters_engineering", "expert", None, last=1997, strength="claim"))
    assert match_requirement(req("english", min_level="strong", kind="spoken_language"), p, V, {}, R, Y).status == "met"
    assert match_requirement(req("istqb_foundation", kind="certificate"), p, V, {}, R, Y).status == "met"
    assert match_requirement(req("masters_engineering", kind="degree"), p, V, {}, R, Y).status == "met"
    assert match_requirement(req("english", min_level="expert", kind="spoken_language"), p, V, {}, R, Y).status \
        == "partial"  # the level rule still applies


def test_level_one_below_partial_two_below_gap():
    p = prof(sk("english", "working", 20, last=2025))
    assert match_requirement(req("english", min_level="strong", kind="spoken_language"), p, V, {}, R, Y).status \
        == "partial"
    g = match_requirement(req("english", min_level="expert", kind="spoken_language"), p, V, {}, R, Y)
    assert g.status == "gap" and g.gap_type == "months"


def test_years_shortfall_within_margin_is_partial_without_effort():
    m = match_requirement(req("java", min_years=5, kind="experience"), prof(sk("java", "strong", 4, last=2025)), V,
                          {}, R, Y)
    assert m.status == "partial" and m.gap_type is None and m.effort_weeks is None


def test_close_relation_is_partial_distant_gives_weeks_gap():
    p = prof(sk("power_automate", "strong", 3, last=2025, strength="outcome"))
    assert match_requirement(req("uipath"), p, V, {("uipath", "power_automate"): "close"}, R, Y).status == "partial"
    g = match_requirement(req("uipath"), p, V, {("uipath", "power_automate"): "distant"}, R, Y)
    assert g.status == "gap" and g.gap_type == "weeks"


def test_same_relation_counts_as_the_skill():
    p = prof(sk("power_bi", "strong", 4, last=2025, strength="outcome"))
    assert match_requirement(req("data_visualization"), p, V, {("data_visualization", "power_bi"): "same"}, R,
                             Y).status == "met"


def test_missing_years_and_degree():
    assert match_requirement(req("aws", min_years=3, kind="experience"), prof(), V, {}, R, Y).gap_type == "years"
    assert match_requirement(req("aws", min_years=2, kind="experience"), prof(), V, {}, R, Y).gap_type == "months"
    assert match_requirement(req("bachelor_computer_science", kind="degree"), prof(), V, {}, R, Y).gap_type == "degree"
    assert match_requirement(req("istqb_foundation", kind="certificate"), prof(), V, {}, R, Y).effort_weeks == 8


def test_tiers():
    p = prof(sk("python", "strong", 6, 2025, "outcome"), sk("sql", "working", 3, 2025, "activity"))
    assert match_job(job(req("python"), req("sql")), p, V, {}, R, Y).tier == 1
    assert match_job(job(req("python"), req("docker"), req("kubernetes")), p, V, {}, R, Y).tier == 2  # 2 x 12 <= 26
    assert match_job(job(req("python"), req("aws", min_years=5, kind="experience")), p, V, {}, R, Y).tier == 3


def test_no_must_requirements_counts_all_as_must():
    p = prof(sk("python", "strong", 6, 2025, "outcome"))
    jm = match_job(job(req("python", importance="nice"), req("docker", importance="nice"),
                       req("aws", importance="nice")), p, V, {}, R, Y)
    assert jm.must_coverage == pytest.approx(1 / 3) and jm.tier != 1


def test_posting_without_requirements_is_not_rewarded():
    jm = match_job(job(), prof(sk("python", "strong", 6, 2025)), V, {}, R, Y)
    assert jm.tier == 3 and jm.must_coverage == 0.0 and jm.nice_coverage == 0.0
    assert _top_gaps(jm, V) == "no requirements extracted"


def test_needed_pairs_only_missing_and_compatible():
    p = prof(sk("python", "strong", 6, 2025), sk("power_automate", "strong", 3, 2025),
             sk("english", "strong", 20, 2025))
    pairs = needed_pairs(job(req("python"), req("uipath"), req("german", kind="spoken_language")), p, V)
    assert pairs == {"uipath": ["power_automate", "python"], "german": ["english"]}


def test_bachelor_meets_generic_university_degree():
    m = match_requirement(req("university_degree", kind="degree"),
                          prof(sk("bachelor_business", "working", None, last=2015, strength="claim")), V, {}, R, Y)
    assert m.status == "met"
    g = match_requirement(req("university_degree", kind="degree"),
                          prof(sk("htl_diploma", "working", None, last=2005, strength="claim")), V, {}, R, Y)
    assert g.status == "gap" and g.gap_type == "degree"


def test_bachelors_degree_id_meets_generic_university_degree():
    assert V.get("bachelors_degree_in_economics") is None  # a made-up id of Claude, not in the vocabulary
    p = prof(sk("bachelors_degree_in_economics", "working", None, last=2012, strength="claim"))
    assert match_requirement(req("university_degree", kind="degree"), p, V, {}, R, Y).status == "met"
    assert needed_pairs(job(req("university_degree", kind="degree")), p, V) == {}


def test_generic_degree_needs_no_relation_question():
    p = prof(sk("masters_business", "working", None, last=2010, strength="claim"), sk("htl_diploma", "working", None))
    assert needed_pairs(job(req("university_degree", kind="degree")), p, V) == {}
    assert needed_pairs(job(req("university_degree", kind="degree")), prof(sk("htl_diploma", "working", None)),
                        V) == {"university_degree": ["htl_diploma"]}


# --- a higher degree meets a lower one ---

def degree_req(skill_id, importance="must") -> Requirement:
    return req(skill_id, importance=importance, kind="degree")


def proposed(skill_id, category) -> SkillProposal:
    return SkillProposal(id=skill_id, name=skill_id, category=category)


def degree_prof(*skill_ids) -> CandidateProfile:
    """A profile that holds these degrees; an id that the vocabulary does not know comes with a proposal of the
    category degree, as step 1 gives it."""
    return prof(*(sk(s, "working", None, last=2000, strength="claim") for s in skill_ids)).model_copy(
        update={"proposed_skills": [proposed(s, "degree") for s in skill_ids if V.get(s) is None]})


def degree_match(required, *held):
    return match_requirement(degree_req(required), degree_prof(*held), V, {}, R, Y)


def test_a_master_meets_an_htl_diploma_and_the_reason_names_the_degree():
    m = degree_match("htl_diploma", "masters_engineering")
    assert m.status == "met" and m.gap_type is None and m.effort_weeks is None
    assert m.reason == "higher degree: masters_engineering"
    assert m.evidence is not None and m.evidence.quote == "quote masters_engineering"


@pytest.mark.parametrize("held", ["htl_diploma", "bachelor_computer_science", "masters_engineering", "phd"])
@pytest.mark.parametrize("required", [
    "apprenticeship_it", "abgeschlossene_technische_ausbildung",
    "abgeschlossene_kaufmnnische_oder_technische_ausbildung", "technische_ausbildung"])
def test_a_ranked_degree_of_the_same_field_or_of_none_meets_an_apprenticeship_or_a_generic_ausbildung(required, held):
    m = degree_match(required, held)  # all technical, or a doctorate that names no field
    assert m.status == "met" and m.reason == f"higher degree: {held}"


@pytest.mark.parametrize("held", ["htl_diploma", "bachelor_computer_science", "masters_engineering", "phd", "matura"])
def test_every_degree_of_a_technical_field_or_of_none_from_rank_two_meets_an_htl_diploma(held):
    assert degree_match("htl_diploma", held).status == "met"


def test_the_lowest_degree_that_is_enough_is_named():
    assert degree_match("abgeschlossene_technische_ausbildung", "phd", "masters_engineering", "htl_diploma",
                        "apprenticeship_it").reason == "higher degree: apprenticeship_it"
    assert degree_match("htl_diploma", "phd", "masters_engineering", "bachelor_computer_science").reason \
        == "higher degree: bachelor_computer_science"
    # a degree of the field beats a lower one of another field, which meets only partly
    assert degree_match("htl_diploma", "phd", "masters_engineering", "bachelor_business").reason \
        == "higher degree: masters_engineering"


def test_an_apprenticeship_does_not_meet_an_htl_diploma():
    m = degree_match("htl_diploma", "apprenticeship_it")
    assert m.status == "gap" and m.gap_type == "degree" and m.reason == "not in the CV"


def test_bachelor_x_is_met_by_masters_x_but_masters_x_not_by_bachelor_x():
    assert degree_match("bachelor_engineering", "masters_engineering").reason == "higher degree: masters_engineering"
    assert degree_match("bachelor_computer_science", "masters_computer_science").status == "met"
    m = degree_match("masters_engineering", "bachelor_engineering")
    assert m.status == "gap" and m.gap_type == "degree"
    assert degree_match("masters_computer_science", "bachelor_computer_science").status == "gap"


@pytest.mark.parametrize("required, held", [
    ("masters_computer_science", "masters_data_science"),         # the same rank, another field
    ("masters_business", "masters_engineering"),                  # the same rank, another field
    ("bachelor_computer_science", "phd"),                         # a doctorate names no field
    ("masters_engineering", "htl_diploma"),                       # a lower rank
    ("bachelor_engineering", "htl_diploma")])
def test_a_degree_of_rank_three_or_more_is_not_met_by_the_same_rank_or_a_lower_rank_or_a_doctorate(required, held):
    m = degree_match(required, held)
    assert m.status == "gap" and m.gap_type == "degree"


def test_a_course_is_not_ranked_and_meets_nothing_by_rank():
    assert V.get("data_science_diploma").category == "degree"  # in the vocabulary, but a course
    for required in ["htl_diploma", "apprenticeship_it", "abgeschlossene_technische_ausbildung", "bachelor_business",
                     "masters_data_science"]:
        assert degree_match(required, "data_science_diploma").status == "gap", required
    p = degree_prof("data_science_diploma")
    assert needed_pairs(job(degree_req("htl_diploma")), p, V) == {"htl_diploma": ["data_science_diploma"]}


def test_ausbildung_of_a_university_is_not_an_apprenticeship():
    assert degree_match("abgeschlossene_hochschulausbildung", "apprenticeship_it").status == "gap"


@pytest.mark.parametrize("skill_id", ["abgeschlossene_ausbildung_fh_oder_uni", "ausbildung_uni", "fh_ausbildung",
                                      "abgeschlossene_fh_ausbildung"])
def test_an_ausbildung_at_an_fh_or_a_uni_is_no_apprenticeship(skill_id):
    assert _degree_rank(skill_id) is None
    assert degree_match(skill_id, "apprenticeship_it").status == "gap"
    assert degree_match("apprenticeship_it", skill_id).status == "gap"  # nor does it count as a held apprenticeship


@pytest.mark.parametrize("skill_id", ["ausbildung_junior", "ausbildung_zur_fhrungskraft"])  # slug() drops the umlaut
def test_fh_and_uni_are_whole_words_only(skill_id):
    assert _degree_rank(skill_id) == (1, "")


def test_only_a_degree_requirement_is_ranked():
    skill = req("lehrlingsausbildung", kind="experience")  # training apprentices, no degree
    m = match_requirement(skill, degree_prof("masters_engineering"), V, {}, R, Y)
    assert m.status == "gap" and m.gap_type != "degree"


def test_a_skill_that_is_no_degree_does_not_meet_a_degree_by_rank():
    p = prof(sk("python", "strong", 6, last=2025))
    assert V.get("python").category != "degree"
    assert match_requirement(degree_req("apprenticeship_it"), p, V, {}, R, Y).status == "gap"


@pytest.mark.parametrize("category", ["method", "domain", "soft", "tool"])
def test_a_cv_skill_is_a_held_degree_only_with_the_category_degree(category):
    """Training apprentices is no apprenticeship, though its id holds "lehre" or "ausbildung"."""
    for skill_id in ("lehrlingsausbildung", "ausbildung_von_lehrlingen"):
        assert V.get(skill_id) is None
        p = prof(sk(skill_id, "strong", 5, last=2025, strength="outcome")).model_copy(
            update={"proposed_skills": [proposed(skill_id, category)]})
        assert match_requirement(degree_req("apprenticeship_it"), p, V, {}, R, Y).status == "gap"
        assert match_requirement(degree_req("abgeschlossene_ausbildung"), p, V, {}, R, Y).status == "gap"


def test_a_cv_skill_of_an_unknown_id_without_a_proposal_is_no_held_degree():
    p = prof(sk("lehrlingsausbildung", "working", None, last=2000, strength="claim"))
    assert match_requirement(degree_req("apprenticeship_it"), p, V, {}, R, Y).status == "gap"


def test_a_proposed_degree_is_a_held_degree():
    p = degree_prof("lehre_als_elektriker")  # the proposal gives the category degree
    assert V.get("lehre_als_elektriker") is None and p.proposed_skills[0].category == "degree"
    m = match_requirement(degree_req("apprenticeship_it"), p, V, {}, R, Y)
    assert m.status == "met" and m.reason == "higher degree: lehre_als_elektriker"


def test_a_university_degree_keeps_its_generic_rule():
    assert degree_match("university_degree", "masters_engineering").reason == "evidenced"
    assert degree_match("university_degree", "htl_diploma").status == "gap"


def test_a_degree_met_by_rank_asks_no_relation_question():
    p = degree_prof("masters_engineering")
    assert needed_pairs(job(degree_req("htl_diploma"), degree_req("abgeschlossene_technische_ausbildung"),
                            degree_req("bachelor_engineering")), p, V) == {}
    q = degree_prof("htl_diploma")  # not met by rank: bachelor_engineering still goes to the relation check
    assert needed_pairs(job(degree_req("bachelor_engineering")), q, V) == {"bachelor_engineering": ["htl_diploma"]}


def test_a_must_degree_met_by_rank_lifts_a_posting_out_of_tier_three():
    p = prof(sk("python", "strong", 6, 2025, "outcome"), sk("masters_engineering", "expert", None, last=1997,
                                                            strength="claim"))
    jm = match_job(job(req("python"), degree_req("htl_diploma")), p, V, {}, R, Y)
    assert jm.tier == 1 and jm.must_coverage == 1.0
    assert match_job(job(req("python"), degree_req("htl_diploma")), prof(sk("python", "strong", 6, 2025, "outcome")),
                     V, {}, R, Y).tier == 3


# --- the field of a degree: a higher degree of another field meets a requirement only partly ---

@pytest.mark.parametrize("skill_id, fields", [
    ("masters_engineering", {"technical"}), ("bachelor_computer_science", {"technical"}),
    ("masters_elektrotechnik", {"technical"}), ("bachelor_electrical_engineering", {"technical"}),
    ("diplom_ingenieur_maschinenbau", {"technical"}), ("dipl_ing", {"technical"}),
    ("masters_mechanical_engineering", {"technical"}), ("diplom_informatiker", {"technical"}),
    ("bachelors_degree_in_informatics", {"technical"}), ("htl_diploma", {"technical"}),
    ("apprenticeship_it", {"technical"}), ("technische_ausbildung", {"technical"}),
    ("abgeschlossene_technische_ausbildung", {"technical"}),
    ("masters_business", {"business"}), ("bachelor_betriebswirtschaft", {"business"}),
    ("kaufmaennische_lehre", {"business"}), ("abgeschlossene_kaufmnnische_ausbildung", {"business"}),
    ("commercial_apprenticeship", {"business"}), ("handelsschule_lehre", {"business"}),
    ("abgeschlossene_bro_ausbildung", {"business"}),  # slug() drops the umlaut of "Büro"
    ("abgeschlossene_buero_ausbildung", {"business"}), ("office_management_ausbildung", {"business"}),
    ("backoffice_lehre", {"business"}),
    ("abgeschlossene_ausbildung_als_brokauffrau", {"business"}), ("abgeschlossene_lehre_als_kauffrau", {"business"}),
    ("abgeschlossene_ausbildung_im_einzelhandel", {"business"}), ("grosshandel_lehre", {"business"}),
    ("hak_matura", {"business"}), ("buchhaltung_ausbildung", {"business"}),
    ("abgeschlossene_ausbildung_im_rechnungswesen", {"business"}), ("finance_apprenticeship", {"business"}),
    ("abgeschlossene_lehre_als_elektriker", {"technical"}), ("mechatroniker_lehre", {"technical"}),
    ("abgeschlossene_kaufmnnische_oder_technische_ausbildung", {"business", "technical"}),
    ("masters_wirtschaftsinformatik", {"business", "technical"}),
    ("masters_physics", {"physics"}), ("masters_data_science", {"data_science"}),
    ("bachelor_mathematics_statistics", {"mathematics_statistics"}), ("bachelors_degree_in_economics", {"economics"}),
    ("phd", set()), ("matura", set()), ("abgeschlossene_ausbildung", set()),
    ("rechtliche_ausbildung", set()), ("police_officer_ausbildung", set()),  # "htl" and "office" inside a word
    ("haken_ausbildung", set()),                                              # "hak" only as a whole word
    # "wirtschaft" inside a compound that is no business
    ("abgeschlossene_landwirtschaftliche_ausbildung", set()), ("forstwirtschaft_lehre", set()),
    ("hauswirtschaft_ausbildung", set()), ("landwirtschaft_und_betriebswirtschaft_lehre", {"business"})])
def test_the_field_of_a_degree_id(skill_id, fields):
    assert _degree_fields(skill_id) == fields


@pytest.mark.parametrize("held", ["masters_engineering", "masters_elektrotechnik", "diplom_ingenieur"])
@pytest.mark.parametrize("required", ["abgeschlossene_kaufmnnische_ausbildung", "kaufmaennische_lehre"])
def test_a_master_in_engineering_meets_a_commercial_requirement_only_partly(required, held):
    m = degree_match(required, held)
    assert m.status == "partial" and m.reason == f"higher degree, other field: {held}"
    assert m.gap_type is None and m.effort_weeks is None  # like the years-only partial: no weeks, no blocking gap
    assert m.evidence is not None and m.evidence.quote == f"quote {held}"


@pytest.mark.parametrize("required", [
    "abgeschlossene_technische_ausbildung", "technische_ausbildung", "htl_diploma", "apprenticeship_it",
    "abgeschlossene_kaufmnnische_oder_technische_ausbildung", "abgeschlossene_lehre_als_elektriker",
    "mechatroniker_lehre", "bachelor_engineering"])
def test_a_master_in_engineering_meets_a_technical_requirement_or_one_that_names_either_field(required):
    m = degree_match(required, "masters_engineering")
    assert m.status == "met" and m.reason == "higher degree: masters_engineering" and m.gap_type is None


@pytest.mark.parametrize("required", [
    "abgeschlossene_ausbildung_als_brokauffrau", "abgeschlossene_lehre_als_kauffrau", "hak_matura",
    "abgeschlossene_ausbildung_im_einzelhandel", "buchhaltung_ausbildung"])
def test_a_master_in_engineering_meets_the_commercial_trainings_of_the_word_lists_only_partly(required):
    assert _degree_rank(required)[0] <= 2
    m = degree_match(required, "masters_engineering")
    assert m.status == "partial" and m.reason == "higher degree, other field: masters_engineering"
    assert degree_match(required, "masters_business").reason == "higher degree: masters_business"


def test_a_requirement_of_a_trade_that_is_no_business_names_no_field():
    for required in ("abgeschlossene_landwirtschaftliche_ausbildung", "forstwirtschaft_lehre",
                     "hauswirtschaft_ausbildung"):
        for held in ("masters_engineering", "masters_business"):
            assert degree_match(required, held).reason == f"higher degree: {held}"


@pytest.mark.parametrize("required", ["bachelor_computer_science", "bachelor_electrical_engineering",
                                      "bachelor_physics"])
@pytest.mark.parametrize("held", ["masters_engineering", "dipl_ing"])
def test_from_level_three_the_suffix_decides_and_not_the_broad_group(required, held):
    """The groups technical and business are for a requirement of level 1 or 2 only."""
    m = degree_match(required, held)
    assert m.status == "partial" and m.reason == f"higher degree, other field: {held}"
    assert m.gap_type is None and m.effort_weeks is None


@pytest.mark.parametrize("required, held", [
    ("bachelor_engineering", "masters_engineering"), ("bachelor_engineering", "dipl_ing"),
    ("bachelor_engineering", "diplom_ingenieur_maschinenbau"),
    ("bachelor_computer_science", "masters_computer_science"),
    ("bachelor_electrical_engineering", "masters_electrical_engineering"),
    ("bachelor_physics", "masters_physics"), ("bachelor_business", "masters_business")])
def test_from_level_three_the_same_suffix_still_meets(required, held):
    m = degree_match(required, held)
    assert m.status == "met" and m.reason == f"higher degree: {held}"


def test_a_master_in_business_meets_a_commercial_requirement_and_an_htl_diploma_only_partly():
    for required in ("abgeschlossene_kaufmnnische_ausbildung", "abgeschlossene_kaufmnnische_oder_technische_ausbildung",
                     "bachelor_business"):
        assert degree_match(required, "masters_business").reason == "higher degree: masters_business"
    for required in ("htl_diploma", "abgeschlossene_technische_ausbildung", "bachelor_computer_science"):
        m = degree_match(required, "masters_business")
        assert m.status == "partial" and m.reason == "higher degree, other field: masters_business"
        assert m.gap_type is None and m.effort_weeks is None


@pytest.mark.parametrize("held", ["masters_engineering", "masters_business", "masters_physics", "phd", "htl_diploma",
                                  "apprenticeship_it"])
@pytest.mark.parametrize("required", ["abgeschlossene_ausbildung", "ausbildung_abgeschlossen"])
def test_a_requirement_without_a_field_is_met_by_any_ranked_degree(required, held):
    m = degree_match(required, held)
    assert m.status == "met" and m.reason == f"higher degree: {held}"


def test_a_higher_degree_in_a_field_outside_business_and_technical_is_compared_by_its_name():
    assert degree_match("bachelor_physics", "masters_physics").reason == "higher degree: masters_physics"
    m = degree_match("bachelor_physics", "masters_engineering")
    assert m.status == "partial" and m.reason == "higher degree, other field: masters_engineering"
    assert degree_match("bachelor_business", "masters_data_science").status == "partial"
    assert degree_match("bachelor_business", "masters_engineering").status == "partial"
    assert degree_match("bachelor_computer_science", "masters_engineering").status == "partial"  # level 3: the suffix


def test_a_degree_that_names_no_field_is_never_another_field():
    for held in ("phd", "matura"):  # as before: they meet a requirement of rank two or less, whatever its field
        assert degree_match("abgeschlossene_kaufmnnische_ausbildung", held).reason == f"higher degree: {held}"
        assert degree_match("htl_diploma", held).reason == f"higher degree: {held}"
    for required in ("bachelor_business", "bachelor_computer_science"):  # and from rank three they meet nothing
        assert degree_match(required, "phd").status == "gap"


@pytest.mark.parametrize("required, held", [
    ("masters_business", "masters_engineering"),                                # all as before: the same rank
    ("masters_computer_science", "masters_data_science"),
    ("abgeschlossene_kaufmnnische_ausbildung", "apprenticeship_it"),
    ("htl_diploma", "handelsschule_matura")])
def test_the_same_rank_in_another_field_is_no_rank_match(required, held):
    """Neither met nor partial: the pair goes to the relation check and, without a relation, is a gap of the kind
    degree. (Before 2026-10-25 the same rank met a requirement of rank two or less whatever its field.)"""
    assert _degree_rank(held)[0] == _degree_rank(required)[0]
    m = degree_match(required, held)
    assert m.status == "gap" and m.gap_type == "degree" and m.reason == "not in the CV"
    assert needed_pairs(job(degree_req(required)), degree_prof(held), V) == {required: [held]}


def test_a_degree_met_only_partly_by_rank_asks_no_relation_question():
    p = degree_prof("masters_engineering")
    assert needed_pairs(job(degree_req("abgeschlossene_kaufmnnische_ausbildung"), degree_req("bachelor_business")),
                        p, V) == {}


def test_a_stored_same_relation_beats_a_partial_degree_by_field_and_a_close_one_does_not():
    required, partial_only = "abgeschlossene_kaufmnnische_ausbildung", ("masters_engineering", "data_science_diploma")
    same = match_requirement(degree_req(required), degree_prof(*partial_only), V,
                             {(required, "data_science_diploma"): "same"}, R, Y)
    assert same.status == "met" and same.reason == "evidenced"
    close = match_requirement(degree_req(required), degree_prof(*partial_only), V,
                              {(required, "data_science_diploma"): "close"}, R, Y)
    assert close.status == "partial" and close.reason == "higher degree, other field: masters_engineering"


def test_a_partial_degree_by_field_lowers_the_coverage_and_never_blocks_tier_two():
    python = sk("python", "strong", 6, 2025, "outcome")
    master = sk("masters_engineering", "expert", None, last=1997, strength="claim")
    commercial = degree_req("abgeschlossene_kaufmnnische_ausbildung")
    jm = match_job(job(req("python"), commercial), prof(python, master), V, {}, R, Y)
    assert [m.status for m in jm.matches] == ["met", "partial"]
    assert jm.must_coverage == 0.75 and jm.tier == 2 and sum(m.effort_weeks or 0 for m in jm.matches) == 0
    # without the master the same posting has a gap of the kind degree, which blocks tier two
    assert match_job(job(req("python"), commercial), prof(python), V, {}, R, Y).tier == 3
    # enough met musts around it: tier one
    names = ("python", "sql", "java", "javascript")
    skills = [sk(name, "strong", 6, 2025, "outcome") for name in names]
    jm = match_job(job(*(req(name) for name in names), commercial), prof(*skills, master), V, {}, R, Y)
    assert jm.must_coverage == pytest.approx(0.9) and jm.tier == 1


# --- only the known shapes of a degree id have a level ---

@pytest.mark.parametrize("skill_id", [
    "master_craftsman", "master_class", "masters_thesis", "phd_candidate", "bachelor_thesis", "doctorate_student",
    "magister_ludi", "bachelors", "diplom", "diplomlehrgang_data_science", "masters_student",
    "bachelor_candidate", "diplom_course", "masters_physics_student"])
def test_an_id_that_only_starts_like_a_degree_has_no_level(skill_id):
    assert _degree_rank(skill_id) is None
    assert degree_match("htl_diploma", skill_id).status == "gap"          # it does not meet a lower degree
    assert degree_match(skill_id, "masters_engineering").status == "gap"  # and a higher one does not meet it


@pytest.mark.parametrize("skill_id, level", [
    ("bachelor_business", (3, "business")), ("bachelors_degree_in_economics", (3, "economics")),
    ("master_computer_science", (4, "computer_science")), ("masters_degree_in_economics", (4, "economics")),
    ("magister_business", (4, "business")), ("phd", (5, "")), ("doctorate", (5, "")), ("doktorat", (5, "")),
    ("phd_engineering", (5, "engineering")), ("htl_diploma", (2, "")), ("matura", (2, "")),
    ("apprenticeship_it", (1, "")), ("abgeschlossene_technische_ausbildung", (1, ""))])
def test_the_known_shapes_have_a_level_and_a_field(skill_id, level):
    assert _degree_rank(skill_id) == level


def test_every_degree_of_the_vocabulary_has_a_level_except_the_course_and_the_generic_one():
    unranked = {s.id for s in V._skills.values() if s.category == "degree" and _degree_rank(s.id) is None}
    assert unranked == {"university_degree", "data_science_diploma"}


@pytest.mark.parametrize("skill_id", [
    "diplom_ingenieur", "diplomingenieur", "dipl_ing", "diplom_ingenieur_maschinenbau"])
def test_a_dipl_ing_is_a_master_of_engineering(skill_id):
    assert _degree_rank(skill_id) == (4, "engineering")
    assert degree_match("htl_diploma", skill_id).reason == f"higher degree: {skill_id}"
    assert degree_match("bachelor_engineering", skill_id).status == "met"
    assert degree_match("bachelor_business", skill_id).status == "partial"  # higher, but another field
    assert degree_match(skill_id, "bachelor_engineering").status == "gap"   # a lower rank
    if skill_id.startswith("diplom"):  # as before: GENERIC_DEGREES takes every id that starts with "diplom"
        assert degree_match("university_degree", skill_id).status == "met"


def test_a_master_of_one_spelling_meets_a_bachelor_of_another_spelling():
    assert degree_match("bachelors_degree_in_economics", "masters_degree_in_economics").status == "met"
    assert degree_match("bachelor_economics", "master_economics").status == "met"


@pytest.mark.parametrize("skill_id, level, htl, bachelor_cs", [
    # (the id, its level and field, what it does for an HTL diploma, and for a bachelor in computer science)
    ("masters_physics", (4, "physics"), "partial", "partial"),
    ("bachelor_electrical_engineering", (3, "electrical_engineering"), "met", "gap"),    # technical, the same rank
    ("masters_engineering_management", (4, "engineering_management"), "met", "partial"),  # level 3: the suffix
    ("diplom_informatiker", (4, "informatiker"), "met", "partial"),
    ("diplom_physik", (4, "physik"), "partial", "partial"),
    ("diplom_business", (4, "business"), "partial", "partial")])
def test_the_spellings_of_the_vocabulary_and_a_diplom_rank_with_any_field(skill_id, level, htl, bachelor_cs):
    assert _degree_rank(skill_id) == level
    assert degree_match("htl_diploma", skill_id).status == htl
    assert degree_match("bachelor_computer_science", skill_id).status == bachelor_cs


def test_a_master_with_a_field_outside_the_list_meets_the_bachelor_of_that_field():
    assert degree_match("bachelor_physics", "masters_physics").reason == "higher degree: masters_physics"
    assert degree_match("masters_physics", "bachelor_physics").status == "gap"  # a lower rank


@pytest.mark.parametrize("skill_id", ["master_physics", "bachelors_physics", "master_of_physics", "magister_physik",
                                      "phd_physics"])
def test_the_other_spellings_keep_the_closed_list_of_fields(skill_id):
    assert _degree_rank(skill_id) is None


# --- soft skills and conditions: a CV cannot prove a soft skill, and only the user can answer a
# condition of a posting ---

PY = sk("python", "strong", 6, last=2025, strength="outcome")
ALL_ANSWERED = {"driving_licence_b": True, "travel_share_max": 100, "shift_work": True, "commute_km_max": 100,
                "remote_only": False, "full_time": True}


def soft(skill_id, importance="must") -> Requirement:
    return req(skill_id, importance=importance, kind="soft")


def cond(skill_id, key, value=None, importance="must") -> Requirement:
    return Requirement(skill_id=skill_id, importance=importance, min_level=None, min_years=None, kind="condition",
                       quote=skill_id, condition=key, condition_value=value)


def answered(*skills, **answers) -> CandidateProfile:
    """A profile with Python evidenced (and skills), and these answers to the conditions."""
    return prof(PY, *skills).model_copy(update={"conditions": Conditions(**answers)})


def test_soft_skills_count_in_no_sum():
    """Spec test 1: Python evidenced and three soft skills that the CV does not name."""
    jm = match_job(job(req("python"), soft("communication"), soft("teamwork"), soft("structured_working")), prof(PY),
                   V, {}, R, Y)
    assert jm.must_coverage == 1.0 and jm.tier == 1
    unverifiable = [m for m in jm.matches if m.status == "unverifiable"]
    assert len(unverifiable) == 3
    assert all(m.effort_weeks is None and m.gap_type is None and m.evidence is None for m in unverifiable)
    assert unverifiable[0].reason == "soft skill: not verifiable from a CV"


def test_a_soft_skill_is_not_verifiable_even_when_the_cv_names_it():
    m = match_requirement(soft("teamwork"), prof(sk("teamwork", "strong", 5, last=2025, strength="outcome")), V, {},
                          R, Y)
    assert m.status == "unverifiable" and m.evidence is None


@pytest.mark.parametrize("answer, status, tier", [(False, "gap", 3), (True, "met", 1), (None, "unanswered", 1)])
def test_a_must_condition_is_met_a_knockout_or_unanswered(answer, status, tier):
    """Spec test 2: Python evidenced and a driving licence B."""
    jm = match_job(job(req("python"), cond("driving_licence_b", "driving_licence_b")),
                   answered(driving_licence_b=answer), V, {}, R, Y)
    licence = jm.matches[1]
    assert licence.status == status and licence.gap_type is None and licence.effort_weeks is None
    assert jm.tier == tier


def test_a_must_condition_gap_is_a_knockout_even_when_the_coverage_reaches_tier_one():
    skills = ("python", "sql", "git", "docker")
    jm = match_job(job(*map(req, skills), cond("shift_work", "shift_work")),
                   answered(*(sk(s, "strong", 6, last=2025, strength="outcome") for s in skills[1:]), shift_work=False),
                   V, {}, R, Y)
    assert jm.must_coverage == 0.8 and jm.must_coverage >= R.tier1_coverage
    assert jm.tier == 3  # without the knockout: tier 1


@pytest.mark.parametrize("value, answer, status", [
    ("ca. 30 %", 20, "gap"), ("ca. 30 %", 40, "met"), ("30 %", 30, "met"), ("bis zu 50 % Reisetätigkeit", 40, "gap"),
    (None, 10, "met"), (None, 5, "gap"), ("Reisebereitschaft", 10, "met"), ("ca. 30 %", None, "unanswered"),
    (None, 0, "gap")])
def test_travel_reads_the_share_of_the_posting(value, answer, status):
    """Spec test 3; a posting without a number asks for rules.conditions_default_travel_share (10 %)."""
    m = match_requirement(cond("travel_willingness", "travel", value), answered(travel_share_max=answer), V, {}, R, Y)
    assert m.status == status and m.gap_type is None and m.effort_weeks is None


@pytest.mark.parametrize("value, answer, status", [
    ("ca. 30%", 30, "met"), ("ca. 30%", 29, "gap"), ("30 Prozent", 30, "met"), ("30 Prozent", 20, "gap"),
    ("up to 30 percent", 30, "met"), ("up to 30 percent", 29, "gap"), ("12,5 %", 12, "gap"), ("12,5 %", 13, "met"),
    ("30-50 %", 40, "gap"), ("2 Tage pro Woche, ca. 40 %", 30, "gap"), ("2 Tage pro Woche, ca. 40 %", 40, "met")])
def test_travel_takes_the_share_from_a_number_with_a_per_cent_mark(value, answer, status):
    m = match_requirement(cond("travel_willingness", "travel", value), answered(travel_share_max=answer), V, {}, R, Y)
    assert m.status == status


@pytest.mark.parametrize("value", ["2 Tage pro Woche", "1-2 Mal im Monat", "2 days a week", "1x pro Monat"])
@pytest.mark.parametrize("answer", [0, 10, 100])
def test_travel_with_a_number_but_no_per_cent_mark_is_unanswered(value, answer):
    """The first integer of "2 Tage pro Woche" is no share (40 % of the week): the posting is not read, so it can be
    neither met nor a knockout."""
    m = match_requirement(cond("travel_willingness", "travel", value), answered(travel_share_max=answer), V, {}, R, Y)
    assert m.status == "unanswered" and m.gap_type is None and m.effort_weeks is None
    assert m.reason == "travel share of the posting not recognised"


def test_travel_names_both_shares_and_the_default_share_comes_from_the_rules():
    m = match_requirement(cond("travel_willingness", "travel", "ca. 30 %"), answered(travel_share_max=20), V, {}, R,
                          Y)
    assert m.reason == "profile: travel up to 20 %, posting asks 30 %"
    rules = R.model_copy(update={"conditions_default_travel_share": 25})
    assert R.conditions_default_travel_share == 10
    assert match_requirement(cond("travel_willingness", "travel"), answered(travel_share_max=20), V, {}, rules,
                             Y).status == "gap"


@pytest.mark.parametrize("answers, status", [
    ({}, "unanswered"), ({"remote_only": True}, "gap"), ({"remote_only": True, "commute_km_max": 50}, "gap"),
    ({"remote_only": False}, "met"), ({"commute_km_max": 30}, "met"),
    ({"commute_km_max": 0, "remote_only": False}, "met")])
def test_location_is_a_gap_only_for_remote_only(answers, status):
    """No distance calculation; unanswered while both answers are missing."""
    m = match_requirement(cond("work_location", "location", "Linz"), answered(**answers), V, {}, R, Y)
    assert m.status == status


@pytest.mark.parametrize("value, answer, status", [
    ("Vollzeit", True, "met"), ("Full-time", True, "met"), ("full time", False, "gap"),
    ("Vollzeit (38,5 Stunden)", False, "gap"), ("Vollzeit", None, "unanswered"), ("Teilzeit", False, "met"),
    ("part-time", None, "met"), ("Voll- oder Teilzeit", False, "met"), ("38,5 Stunden", True, "unanswered"),
    (None, True, "unanswered")])
def test_working_time_knows_full_time_and_part_time_in_german_and_english(value, answer, status):
    m = match_requirement(cond("working_time", "working_time", value), answered(full_time=answer), V, {}, R, Y)
    assert m.status == status


@pytest.mark.parametrize("answer, status", [(True, "met"), (False, "gap"), (None, "unanswered")])
def test_shift_work(answer, status):
    assert match_requirement(cond("shift_work", "shift_work"), answered(shift_work=answer), V, {}, R, Y).status \
        == status


def test_a_condition_without_a_key_is_always_unanswered():
    """E.g. a start date ("ab sofort") has no answer in the profile."""
    m = match_requirement(cond("immediate_start", None, "ab sofort"), answered(**ALL_ANSWERED), V, {}, R, Y)
    assert m.status == "unanswered" and m.gap_type is None and m.effort_weeks is None


OWN_FIELDS = {"driving_licence_b": ["driving_licence_b"], "travel": ["travel_share_max"], "shift_work": ["shift_work"],
              "location": ["commute_km_max", "remote_only"], "working_time": ["full_time"]}


@pytest.mark.parametrize("key, fields", OWN_FIELDS.items())
def test_a_condition_is_answered_by_its_own_fields_only(key, fields):
    others = {name: value for name, value in ALL_ANSWERED.items() if name not in fields}
    assert match_requirement(cond(key, key, "Vollzeit"), answered(**others), V, {}, R, Y).status == "unanswered"
    assert match_requirement(cond(key, key, "Vollzeit"), answered(**ALL_ANSWERED), V, {}, R, Y).status == "met"


def test_a_nice_condition_and_a_nice_soft_skill_change_neither_the_tier_nor_the_must_coverage():
    base = match_job(job(req("python"), req("sql")), answered(), V, {}, R, Y)
    jm = match_job(job(req("python"), req("sql"), soft("teamwork", "nice"),
                       cond("driving_licence_b", "driving_licence_b", importance="nice")),
                   answered(driving_licence_b=False), V, {}, R, Y)
    assert (base.tier, base.must_coverage) == (2, 0.5)
    assert (jm.tier, jm.must_coverage) == (base.tier, base.must_coverage)
    # no nice requirement: the coverage of nothing is 1.0. The soft skill counts in no sum; the unmet nice condition is
    # the only nice requirement that counts, and it counts as unmet.
    assert (base.nice_coverage, jm.nice_coverage) == (1.0, 0.0)


def test_a_posting_of_soft_skills_and_unanswered_conditions_only_fits():
    jm = match_job(job(soft("communication"), soft("teamwork", "nice"), cond("shift_work", "shift_work")), prof(), V,
                   {}, R, Y)
    assert (jm.tier, jm.must_coverage) == (1, 1.0)
    blocked = match_job(job(soft("communication"), cond("shift_work", "shift_work")), answered(shift_work=False), V,
                        {}, R, Y)
    assert blocked.tier == 3


def test_a_posting_whose_musts_are_all_soft_or_unanswered_does_not_count_its_nice_requirements_as_must():
    """The musts exist but none counts: nothing to cover (1.0). The fallback "all count as must" is only for a posting
    that marks no must at all."""
    jm = match_job(job(soft("communication"), cond("shift_work", "shift_work"), req("python", importance="nice"),
                       req("docker", importance="nice")), prof(PY), V, {}, R, Y)
    assert (jm.must_coverage, jm.tier) == (1.0, 1)
    assert jm.nice_coverage == 0.5  # the nice requirements keep their own number


def test_only_a_must_condition_gap_is_a_knockout_even_when_the_posting_marks_no_must():
    nice_licence = cond("driving_licence_b", "driving_licence_b", importance="nice")
    jm = match_job(job(req("python", importance="nice"), nice_licence), answered(driving_licence_b=False), V, {}, R, Y)
    assert jm.matches[1].status == "gap"  # unmet, but it asks no effort and shuts nothing out
    assert (jm.must_coverage, jm.tier) == (0.5, 2)
    must_licence = cond("driving_licence_b", "driving_licence_b")
    jm = match_job(job(req("python", importance="nice"), must_licence), answered(driving_licence_b=False), V, {}, R, Y)
    assert jm.tier == 3  # a must condition gap is one whatever else the posting marks


def test_soft_skills_and_conditions_ask_no_relation_question():
    p = prof(PY, sk("leadership", "strong", 5, last=2025, strength="outcome"))
    assert needed_pairs(job(soft("communication"), cond("driving_licence_b", "driving_licence_b")), p, V) == {}


def test_the_command_line_lists_no_soft_skill_and_no_unanswered_condition_as_a_gap():
    jm = match_job(job(req("python"), soft("communication"), cond("shift_work", "shift_work"), req("docker")),
                   prof(PY), V, {}, R, Y)
    assert _top_gaps(jm, V) == "Docker (months)"


def test_the_command_line_shows_a_condition_that_is_not_met_as_a_condition_and_no_gap():
    jm = match_job(job(req("python"), soft("communication"), cond("driving_licence_b", "driving_licence_b"),
                       cond("shift_work", "shift_work"), req("docker")),
                   answered(driving_licence_b=False), V, {}, R, Y)  # shift work: not answered
    assert _top_gaps(jm, V) == "Driving licence class B (condition: not met), Docker (months)"
