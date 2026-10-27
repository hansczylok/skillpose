"""Step 3a: match requirements against a profile with rules in code, and give each posting a tier.

The rules follow the spec (section 6): a skill that is only claimed or unused for many years counts as partial;
the type of a gap decides how fast it can be closed; tiers follow from the coverage of must requirements. A soft skill
is not verifiable from a CV, and a condition of the posting (driving licence, travel, shift work, place of work,
working time) is checked against the user's answers: neither is ever an effort, and an unmet must condition is a
knockout.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Literal, NamedTuple

from skillpose.core.config import Rules
from skillpose.core.models import (
    LEVEL_ORDER,
    NOT_COUNTED,
    STRENGTH_ORDER,
    CandidateProfile,
    CandidateSkill,
    Conditions,
    Evidence,
    GapType,
    JobMatch,
    JobProfile,
    Relation,
    Requirement,
    RequirementMatch,
)
from skillpose.core.vocabulary import Vocabulary

COMPATIBLE = {"tool", "programming_language", "method"}
# For these, being listed in the CV is the usual proof and time does not wear them out: a certificate from 2014 is
# still held, a stated language level is the standard way to show it. Claim and staleness rules do not apply.
LISTED_IS_PROOF = {"spoken_language", "degree", "certificate"}
_LEARNABLE = ("weeks", "months", "certificate")
_NOT_FROM_THE_CV = ("soft", "condition")  # kinds decided without the skills of the CV: no relation question for them
# "Any university degree" has no field, so no vocabulary relation can say a specific degree meets it: any listed degree
# whose id starts with one of these prefixes does.
GENERIC_DEGREES = {"university_degree": ("bachelor_", "bachelors_", "master_", "masters_", "magister", "diplom", "phd",
                                         "doctorate")}
# Bump when a change in this module changes a match or a tier (a rule in code): stored matches carry a fingerprint of
# the rules in the settings and of this number, and are recomputed when it differs (match_fingerprint).
MATCHING_CODE_VERSION = 5  # 2: soft skills and conditions count in no sum; 3: only a posting
# that marks no must counts all as must, and only a must condition gap is a knockout; 4: the field of a degree counts
# (a higher degree of another field meets a degree requirement only partly); 5: a travel share is read only from a
# number with a per-cent mark, "fh" and "uni" name a university, a CV skill is a held degree only by its category
# The levels of degrees, so that a higher one meets a lower one: 1 apprenticeship (also a proposed id that names a
# Lehre, Lehrabschluss, Ausbildung or vocational training), 2 HTL or Matura, 3 bachelor, 4 master (also Dipl.-Ing., a
# master of engineering), 5 doctorate. Only the known shapes have a level: the vocabulary's own spellings bachelor_
# and masters_, and diplom_ (a university diploma, as GENERIC_DEGREES takes it), followed by any field; the other
# spellings (bachelors, master, magister, phd) followed by a field of the vocabulary's degrees (or a few like it); and
# phd on its own. So master_craftsman, masters_thesis, phd_candidate and diplomlehrgang_data_science have none, nor
# have a course (data_science_diploma) or the generic university_degree.
_FIELDS = ("computer_science|informatics|engineering|business|economics|mathematics_statistics|mathematics|statistics|"
           "data_science")
_DEGREE_IN = r"(?:(?:degree_)?(?:in|of)_)?"
_FIELD_DEGREE = re.compile(rf"(?P<level>bachelors?|masters?|magister|diplom|phd)_{_DEGREE_IN}(?P<field>{_FIELDS})")
_ANY_FIELD_DEGREE = re.compile(rf"(?P<level>bachelor|masters|diplom)_{_DEGREE_IN}(?P<field>[a-z0-9]+(?:_[a-z0-9]+)*)")
_NO_FIELD = {"thesis", "student", "candidate", "course"}  # a word after the degree that names no field
_DIPL_ING = re.compile(r"dipl(?:om)?_?ing(?:enieur)?(?:_\w+)?")
_DOCTORATES = {"phd", "doctorate", "doktorat"}
_LEVEL_RANK = {"bachelor": 3, "bachelors": 3, "master": 4, "masters": 4, "magister": 4, "diplom": 4, "phd": 5}
_RANK_TWO = {"htl", "matura", "abitur"}                       # a word of the id
_RANK_ONE = ("ausbildung", "lehrabschluss", "apprentice", "vocational")  # a part of the id; "lehre" must be a word
_UNIVERSITY = ("hochschul", "universit", "studium", "akadem")  # an "Ausbildung" at a university is no apprenticeship
_UNIVERSITY_WORDS = {"fh", "uni"}  # whole words only: "uni" is also a part of "junior", "fh" of "fhrung"
# The field of a degree (a master in electrical engineering does not meet "completed commercial
# training"; across fields an employer decides, so the match is partial). For a requirement of level 1 or 2
# (apprenticeship, HTL, Matura, "completed training") the field is one of two broad groups. A vocabulary id carries it
# as the suffix (masters_engineering, bachelor_business) or in a word (htl_diploma and apprenticeship_it are
# technical); a proposed id names it in words, in German or English, so each group has word parts. slug() drops
# umlauts, so "Büro" is the word "bro". Any other field is compared by its own name (masters_physics). The word parts
# are searched in the id; where a part would sit inside another word it is a whole word ("htl" in "rechtliche", "it",
# "hak") or excluded ("wirtschaft" in landwirtschaft, forstwirtschaft and hauswirtschaft, which are no business).
# From level 3 the field is the suffix itself (15w): masters_engineering is another field than
# bachelor_computer_science.
_BUSINESS_PARTS = re.compile(r"kaufm|kauff|commercial|business|(?<!land)(?<!forst)(?<!haus)wirtschaft|handel|buero|"
                             r"buchhalt|rechnungswesen|finance")
_BUSINESS_WORDS = {"bro", "hak"}
_BUSINESS_END = "office"  # also the end of a compound such as backoffice
_TECHNICAL_PARTS = re.compile(r"techn|engineering|ingenieur|elektr|electrical|maschinenbau|mechanical|mechatron|"
                              r"informatik|informatics|computer_science")
_TECHNICAL_WORDS = {"htl", "it"}


def _degree_rank(skill_id: str) -> tuple[int, str] | None:
    """(level, field) of a degree id, or None when it has no level; the field is empty when the id names none."""
    if skill_id in _DOCTORATES:
        return 5, ""
    if (found := _FIELD_DEGREE.fullmatch(skill_id)) is not None:
        return _LEVEL_RANK[found["level"]], found["field"]
    if _DIPL_ING.fullmatch(skill_id):
        return 4, "engineering"
    if (found := _ANY_FIELD_DEGREE.fullmatch(skill_id)) is not None and not _NO_FIELD & set(found["field"].split("_")):
        return _LEVEL_RANK[found["level"]], found["field"]
    words = set(skill_id.split("_"))
    if words & _UNIVERSITY_WORDS or any(part in skill_id for part in _UNIVERSITY):
        return None
    if words & _RANK_TWO:
        return 2, ""
    if "lehre" in words or any(part in skill_id for part in _RANK_ONE):
        return 1, ""
    return None


def _degree_fields(skill_id: str) -> frozenset[str]:
    """The broad field of a degree id: "business" and/or "technical" when the id names it in words, else the field of
    its level shape (masters_physics: "physics"), else none (phd, Matura, "any completed training": no field named)."""
    ranked = _degree_rank(skill_id)
    shape = ranked[1] if ranked else ""
    text = f"{skill_id} {shape}"  # the shape alone names the field of dipl_ing
    words = set(re.split(r"[_ ]", text))
    fields = set()
    if (_BUSINESS_PARTS.search(text) or words & _BUSINESS_WORDS
            or any(word.endswith(_BUSINESS_END) for word in words)):
        fields.add("business")
    if _TECHNICAL_PARTS.search(text) or words & _TECHNICAL_WORDS:
        fields.add("technical")
    return frozenset(fields) if fields else frozenset({shape} if shape else ())


def match_fingerprint(rules: Rules, now_year: int) -> str:
    """What decides a match and a tier: the rules of the settings, the year (it makes a skill stale) and
    MATCHING_CODE_VERSION. A stored match with another fingerprint is out of date."""
    state = json.dumps({"code": MATCHING_CODE_VERSION, "rules": rules.model_dump(), "year": now_year}, sort_keys=True)
    return hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]


def _category(vocab: Vocabulary, skill_id: str) -> str | None:
    skill = vocab.get(skill_id)
    return skill.category if skill else None


def _compatible(a: str | None, b: str | None) -> bool:
    """Unknown categories (skills proposed in this run) are compatible with everything."""
    return a is None or b is None or a == b or (a in COMPATIBLE and b in COMPATIBLE)


def _held_category(vocab: Vocabulary, profile: CandidateProfile, skill_id: str) -> str | None:
    """The category of a skill of the CV: the vocabulary's, else the one Claude proposed for the unknown id."""
    if (skill := vocab.get(skill_id)) is not None:
        return skill.category
    return next((p.category for p in profile.proposed_skills if p.id == skill_id), None)


def _generic_degree(skill_id: str, profile: CandidateProfile, vocab: Vocabulary) -> CandidateSkill | None:
    """The first degree (sorted by id) in the profile that meets a generic degree requirement, if any."""
    prefixes = GENERIC_DEGREES.get(skill_id)
    if prefixes is None:
        return None
    return next((s for s in sorted(profile.skills, key=lambda s: s.skill_id)
                 if s.skill_id.startswith(prefixes) and _category(vocab, s.skill_id) in ("degree", None)), None)


class _ByRank(NamedTuple):
    degree: CandidateSkill
    other_field: bool  # a higher degree of another field: it meets the requirement only partly


def _meets_by_rank(rank: int, want: frozenset[str], have: int, own: frozenset[str]) -> Literal["met", "partial"] | None:
    """How a degree of level `have` and field `own` meets a required one of level `rank` and field `want` (the broad
    groups for a requirement of level 1 or 2, the suffix of the id from level 3). A requirement of level 1 or 2 needs
    at least its level, from level 3 a higher one (the same level in another spelling goes to the relation check). In
    the field of the requirement (or in either of two it names, or in any when it names none) the degree meets it; a
    higher degree of another field meets it partly. A degree that names no field (a doctorate, Matura) is no other
    field: it meets a requirement of level 1 or 2 whatever its field, but not one from level 3 that names a field."""
    if have < rank or (have == rank and rank > 2):
        return None
    if not want or want & own:
        return "met"
    if not own:
        return "met" if rank <= 2 else None
    return "partial" if have > rank else None


def _higher_degree(req: Requirement, profile: CandidateProfile, vocab: Vocabulary) -> _ByRank | None:
    """The degree in the profile that meets a degree requirement by level and field, if any. A degree that meets it
    in full comes first: the lowest one (ties by id), e.g. masters_engineering for bachelor_engineering or for
    "completed technical training". Else the lowest higher degree of another field, which meets it only partly (a
    master in business for an HTL diploma, a master in engineering for bachelor_computer_science). A degree of the same
    level in another field meets it not at all. The field is a broad group for a requirement of level 1 or 2 and the
    suffix of the id from level 3."""
    if req.kind != "degree" and _category(vocab, req.skill_id) != "degree":
        return None
    needed = _degree_rank(req.skill_id)
    if needed is None:
        return None
    broad = needed[0] <= 2

    def field(skill_id: str, ranked: tuple[int, str]) -> frozenset[str]:
        return _degree_fields(skill_id) if broad else frozenset({ranked[1]} if ranked[1] else ())

    want = field(req.skill_id, needed)
    meeting = []
    for s in profile.skills:
        held = _degree_rank(s.skill_id) if _held_category(vocab, profile, s.skill_id) == "degree" else None
        if held is None:
            continue
        how = _meets_by_rank(needed[0], want, held[0], field(s.skill_id, held))
        if how is not None:
            meeting.append((how == "partial", held[0], s.skill_id, s))
    if not meeting:
        return None
    other_field, _, _, degree = min(meeting, key=lambda m: m[:3])
    return _ByRank(degree, other_field)


def needed_pairs(job: JobProfile, profile: CandidateProfile, vocab: Vocabulary) -> dict[str, list[str]]:
    have = {s.skill_id for s in profile.skills}
    pairs: dict[str, list[str]] = {}
    for r in job.requirements:
        if (r.kind in _NOT_FROM_THE_CV or r.skill_id in have or r.skill_id in pairs
                or _generic_degree(r.skill_id, profile, vocab) or _higher_degree(r, profile, vocab)):
            continue
        category = _category(vocab, r.skill_id)
        candidates = sorted(s.skill_id for s in profile.skills
                            if _compatible(category, _category(vocab, s.skill_id)))
        if candidates:
            pairs[r.skill_id] = candidates
    return pairs


def _best_evidence(skill: CandidateSkill) -> Evidence | None:
    return max(skill.evidence, key=lambda e: STRENGTH_ORDER[e.strength], default=None)


def _effort(gap_type: GapType | None, rules: Rules) -> int | None:
    return rules.effort_weeks.get(gap_type) if gap_type else None


def _result(req: Requirement, status: str, reason: str, evidence: Evidence | None, gap_type: GapType | None,
            rules: Rules) -> RequirementMatch:
    return RequirementMatch(requirement=req, status=status, reason=reason, evidence=evidence, gap_type=gap_type,
                            effort_weeks=_effort(gap_type, rules))


def _match_present(req: Requirement, cs: CandidateSkill, category: str | None, rules: Rules,
                   now_year: int) -> RequirementMatch:
    best = _best_evidence(cs)
    listed_is_proof = category in LISTED_IS_PROOF
    level_gap = LEVEL_ORDER[req.min_level] - LEVEL_ORDER[cs.level] if req.min_level else 0
    if level_gap >= 2:
        return _result(req, "gap", f"level {cs.level}, {req.min_level} required", best, "months", rules)
    if req.min_years and cs.years is not None and req.min_years - cs.years > rules.years_gap_margin:
        return _result(req, "gap", f"{cs.years:g} of {req.min_years:g} years required", best, "years", rules)

    reasons, years_only = [], False
    if level_gap == 1:
        reasons.append(f"level {cs.level}, {req.min_level} required")
    if not listed_is_proof and (best is None or best.strength == "claim"):
        reasons.append("only listed, no evidence of use")
    if not listed_is_proof and cs.last_used is not None and cs.last_used < now_year - rules.stale_years:
        reasons.append(f"last used {cs.last_used}")
    if req.min_years and (cs.years is None or cs.years < req.min_years):
        years_only = not reasons
        reasons.append("years of use unknown" if cs.years is None else f"{cs.years:g} of {req.min_years:g} years")
    if not reasons:
        return _result(req, "met", "evidenced", best, None, rules)
    return _result(req, "partial", "; ".join(reasons), best, None if years_only else "weeks", rules)


_PER_CENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:%|prozent|percent)", re.IGNORECASE)
_PART_TIME = re.compile(r"teilzeit|part[\s-]?time", re.IGNORECASE)
_FULL_TIME = re.compile(r"vollzeit|full[\s-]?time", re.IGNORECASE)


def _travel_share(value: str | None, rules: Rules) -> float | None:
    """The share of travel a posting asks for, in per cent: the first number with a per-cent sign or the word
    Prozent/percent ("ca. 30 %", "up to 30 percent"); the default of the rules when the text has no number at all; None
    when it has a number but no per-cent mark ("2 Tage pro Woche" is no share)."""
    text = value or ""
    if (found := _PER_CENT.search(text)) is not None:
        return float(found[1].replace(",", "."))
    return None if any(c.isdigit() for c in text) else rules.conditions_default_travel_share


def _condition_answer(req: Requirement, answers: Conditions, rules: Rules) -> tuple[bool, str] | str:
    """(met, reason) from the user's answers, or the reason why they do not decide the condition. The place of work
    is checked without a distance: only "remote only" rules it out."""
    key, value = req.condition, req.condition_value or ""
    if key == "driving_licence_b" and answers.driving_licence_b is not None:
        return answers.driving_licence_b, ("profile: driving licence B" if answers.driving_licence_b
                                           else "profile: no driving licence B")
    if key == "shift_work" and answers.shift_work is not None:
        return answers.shift_work, "profile: shift work" if answers.shift_work else "profile: no shift work"
    if key == "travel" and answers.travel_share_max is not None:
        if (asked := _travel_share(value, rules)) is None:
            return "travel share of the posting not recognised"
        return (answers.travel_share_max >= asked,
                f"profile: travel up to {answers.travel_share_max} %, posting asks {asked:g} %")
    if key == "location" and (answers.remote_only is not None or answers.commute_km_max is not None):
        if answers.remote_only:
            return False, "profile: remote only"
        km = answers.commute_km_max
        return True, "profile: not only remote" if km is None else f"profile: commute up to {km} km"
    if key == "working_time":
        if _PART_TIME.search(value):
            return True, "posting asks part-time"
        if not _FULL_TIME.search(value):
            return "working time of the posting not recognised"
        if answers.full_time is not None:
            return answers.full_time, "profile: full-time" if answers.full_time else "profile: no full-time"
    return "the profile asks nothing about this condition" if key is None else "not answered in the profile"


def _match_condition(req: Requirement, answers: Conditions, rules: Rules) -> RequirementMatch:
    """Met, a gap (never with an effort: the user said it does not fit) or unanswered."""
    answer = _condition_answer(req, answers, rules)
    if isinstance(answer, str):
        return _result(req, "unanswered", answer, None, None, rules)
    met, reason = answer
    return _result(req, "met" if met else "gap", reason, None, None, rules)


def match_requirement(req: Requirement, profile: CandidateProfile, vocab: Vocabulary,
                      relations: Mapping[tuple[str, str], Relation], rules: Rules, now_year: int) -> RequirementMatch:
    if req.kind == "soft":  # whatever the CV says about it
        return _result(req, "unverifiable", "soft skill: not verifiable from a CV", None, None, rules)
    if req.kind == "condition":
        return _match_condition(req, profile.conditions, rules)
    by_id = {s.skill_id: s for s in profile.skills}
    if req.skill_id not in by_id and (degree := _generic_degree(req.skill_id, profile, vocab)) is not None:
        return _match_present(req, degree, "degree", rules, now_year)  # judged as the required degree itself
    higher = None if req.skill_id in by_id else _higher_degree(req, profile, vocab)
    if higher is not None and not higher.other_field:
        met = _match_present(req, higher.degree, "degree", rules, now_year)  # judged like the degree itself
        reason = f"higher degree: {higher.degree.skill_id}" + ("" if met.status == "met" else f"; {met.reason}")
        return met.model_copy(update={"reason": reason})
    related = {c: rel for (t, c), rel in relations.items() if t == req.skill_id and c in by_id}
    cs = by_id.get(req.skill_id)
    if cs is None:
        same = sorted(c for c, rel in related.items() if rel == "same")
        cs = by_id[same[0]] if same else None
    if cs is not None:
        return _match_present(req, cs, _category(vocab, cs.skill_id), rules, now_year)
    if higher is not None:  # a higher degree of another field: partly, with no gap type and no weeks (like years only)
        return _result(req, "partial", f"higher degree, other field: {higher.degree.skill_id}",
                       _best_evidence(higher.degree), None, rules)

    close = sorted(c for c, rel in related.items() if rel == "close")
    if close:
        return _result(req, "partial", f"related: {close[0]}", _best_evidence(by_id[close[0]]), "weeks", rules)
    if req.kind == "degree":
        gap_type: GapType = "degree"
    elif req.kind == "certificate":
        gap_type = "certificate"
    elif req.min_years and req.min_years > rules.years_gap_margin:
        gap_type = "years"
    elif any(rel == "distant" for rel in related.values()) and _category(vocab, req.skill_id) in COMPATIBLE | {None}:
        gap_type = "weeks"
    else:
        gap_type = "months"
    return _result(req, "gap", "not in the CV", None, gap_type, rules)


def _coverage(matches: list[RequirementMatch]) -> float:
    if not matches:
        return 1.0
    return sum(1.0 if m.status == "met" else 0.5 if m.status == "partial" else 0.0 for m in matches) / len(matches)


def match_job(job: JobProfile, profile: CandidateProfile, vocab: Vocabulary,
              relations: Mapping[tuple[str, str], Relation], rules: Rules, now_year: int) -> JobMatch:
    matches = [match_requirement(r, profile, vocab, relations, rules, now_year) for r in job.requirements]
    if not matches:  # nothing extracted: an extraction failure must not look like a perfect fit
        return JobMatch(text_hash=job.text_hash, matches=[], must_coverage=0.0, nice_coverage=0.0, tier=3)
    # Soft skills and unanswered conditions count in no sum: a posting of only those fits (its coverage is 1.0).
    counted = [m for m in matches if m.status not in NOT_COUNTED]
    nice = [m for m in counted if m.requirement.importance == "nice"]
    # A posting that marks nothing as must would otherwise look fully covered (review focus 2): all its requirements
    # count as must. One whose musts are all soft skills or unanswered conditions has none to count: coverage 1.0.
    marks_must = any(m.requirement.importance == "must" for m in matches)
    must = [m for m in counted if m.requirement.importance == "must"] if marks_must else counted
    coverage = _coverage(must)
    # an unmet must condition is a knockout whatever the coverage (a nice one never is); it has no effort, so it
    # adds no weeks and is no reason why the gaps cannot be learned
    hard = any(m.status == "gap" and (m.gap_type in ("years", "degree") or (m.requirement.kind == "condition"
                                                                              and m.requirement.importance == "must"))
               for m in must)
    learnable = all(m.gap_type in _LEARNABLE for m in must if m.status == "gap" and m.requirement.kind != "condition")
    weeks = sum(m.effort_weeks or 0 for m in must if m.status != "met")
    if coverage >= rules.tier1_coverage and not hard:
        tier = 1
    elif not hard and learnable and weeks <= rules.tier2_max_weeks:
        tier = 2
    else:
        tier = 3
    return JobMatch(text_hash=job.text_hash, matches=matches, must_coverage=coverage, nice_coverage=_coverage(nice),
                    tier=tier)
