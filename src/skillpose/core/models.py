"""Data model of skillpose.

The *Draft* classes are the schemas Claude fills in. They leave out every field the program computes itself
(verification of quotes, last year of use, text hash), so the model cannot invent them.

A soft skill cannot be proven by a CV, and only the user can answer a condition of a posting (a driving licence, travel,
shift work, the place of work, the working time): their matches are "unverifiable" or, without an answer,
"unanswered", and these two count in no sum.
"""

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["tool", "programming_language", "method", "domain", "certificate", "degree", "spoken_language",
                   "soft"]
Level = Literal["basic", "working", "strong", "expert"]
Strength = Literal["outcome", "activity", "claim"]
Importance = Literal["must", "nice"]
ReqKind = Literal["tool", "experience", "certificate", "degree", "spoken_language", "soft", "condition"]
ConditionKey = Literal["driving_licence_b", "travel", "shift_work", "location", "working_time"]
Seniority = Literal["junior", "mid", "senior", "lead"]
MatchStatus = Literal["met", "partial", "gap", "unverifiable", "unanswered"]
NOT_COUNTED = ("unverifiable", "unanswered")  # statuses that count in no coverage, effort, tier or list of gaps
GapType = Literal["weeks", "months", "years", "certificate", "degree"]
Relation = Literal["same", "close", "distant", "none"]

LEVEL_ORDER = {"basic": 0, "working": 1, "strong": 2, "expert": 3}
STRENGTH_ORDER = {"claim": 0, "activity": 1, "outcome": 2}


class Skill(BaseModel):
    id: str
    name_en: str
    name_de: str
    category: Category
    aliases: list[str] = []
    esco_uri: str | None = None  # filled in phase 2 (ESCO mapping)


class SkillProposal(BaseModel):
    id: str
    name: str
    category: Category


class Evidence(BaseModel):
    quote: str  # verbatim from the CV
    context: str  # position or section the quote belongs to
    period_from: int | None
    period_to: int | None  # None: until today
    strength: Strength
    verified: bool = False  # set by the program, never by Claude


class CandidateSkill(BaseModel):
    skill_id: str
    level: Level
    years: float | None
    last_used: int | None = None  # computed from the evidence periods
    evidence: list[Evidence]
    hidden: bool = False  # the user hid the skill: it stays in the profile but no later step uses it


class Station(BaseModel):
    title: str
    employer: str
    period_from: int | None
    period_to: int | None  # None: until today


class Conditions(BaseModel):
    """The user's answers to the conditions of postings; None: not answered."""
    driving_licence_b: bool | None = None
    travel_share_max: int | None = Field(default=None, ge=0, le=100)  # per cent of the working time; 0: no travel
    shift_work: bool | None = None
    commute_km_max: int | None = Field(default=None, ge=0)
    remote_only: bool | None = None
    full_time: bool | None = None
    notes: str = ""  # free text, only shown


class CandidateProfile(BaseModel):
    language: Literal["de", "en"]
    skills: list[CandidateSkill]
    stations: list[Station]
    proposed_skills: list[SkillProposal] = []
    # The store keeps the answers once per database (Store.conditions) and puts them here before matching; they are
    # never written with the profile of a CV.
    conditions: Conditions = Field(default_factory=Conditions, exclude=True)

    def visible(self) -> "CandidateProfile":
        """A copy without the skills the user hid."""
        return self.model_copy(update={"skills": [s for s in self.skills if not s.hidden]})


class Requirement(BaseModel):
    skill_id: str
    importance: Importance
    min_level: Level | None
    min_years: float | None
    kind: ReqKind
    quote: str  # verbatim from the posting
    condition: ConditionKey | None = None  # kind "condition": which one; None for one the profile has no answer to
    condition_value: str | None = None  # kind "condition": as the posting says it ("ca. 30 %", "Linz", "Vollzeit")


class JobProfile(BaseModel):
    text_hash: str
    title: str
    role_family: str
    seniority: Seniority
    requirements: list[Requirement]
    proposed_skills: list[SkillProposal] = []


class RequirementMatch(BaseModel):
    requirement: Requirement
    status: MatchStatus
    reason: str
    evidence: Evidence | None
    gap_type: GapType | None
    effort_weeks: int | None


class JobMatch(BaseModel):
    text_hash: str
    matches: list[RequirementMatch]
    must_coverage: float
    nice_coverage: float
    tier: Literal[1, 2, 3]


class GapStat(BaseModel):
    """How much one skill is missing across the postings of a role family."""
    skill_id: str
    weight: float  # summed over all unmet requirements: must counts more than nice, a gap more than a partial
    postings: int  # postings of the family that ask for it and are not met
    gap_type: GapType | None  # the most frequent type of gap


class RoleReport(BaseModel):
    role_family: str
    postings: int
    median_coverage: float  # median must coverage over the postings
    tier: Literal[1, 2, 3] | None  # None: too few postings to say
    tier_shares: dict[int, float]  # share of postings per tier, keys 1, 2 and 3
    top_gaps: list[GapStat]  # neither soft skills nor conditions: a path could not close them
    conditions_blocking: list[GapStat] = []  # must conditions the user's answers do not meet (knockouts)


class PathStep(BaseModel):
    activity: str
    closes: list[str]  # skill ids from the gap list of the role report
    effort_weeks: int
    proof: str  # the visible result an employer can check


class DevelopmentPath(BaseModel):
    role_family: str
    language: Literal["de", "en"]
    rationale: str
    steps: list[PathStep]
    total_weeks: int
    input_hash: str  # hash of the whole request, to see whether the path still matches profile and report


# --- schemas Claude fills in ---

class EvidenceDraft(BaseModel):
    quote: str
    context: str
    period_from: int | None
    period_to: int | None
    strength: Strength


class CandidateSkillDraft(BaseModel):
    skill_id: str
    name: str
    category: Category
    level: Level
    years: float | None
    evidence: list[EvidenceDraft]


class CvDraft(BaseModel):
    language: Literal["de", "en"]
    skills: list[CandidateSkillDraft]
    stations: list[Station]


class RequirementDraft(BaseModel):
    skill_id: str
    name: str
    category: Category
    importance: Importance
    min_level: Level | None
    min_years: float | None
    kind: ReqKind
    quote: str
    condition: ConditionKey | None = None  # kind condition only; optional, so no other requirement spends tokens on it
    condition_value: str | None = None


class JobDraft(BaseModel):
    title: str
    role_family: str
    seniority: Seniority
    requirements: list[RequirementDraft]


class PathStepDraft(BaseModel):
    activity: str
    closes: list[str]
    effort_weeks: int
    proof: str


class PathDraft(BaseModel):
    rationale: str
    steps: list[PathStepDraft]


class RelatedSkill(BaseModel):
    skill_id: str
    relation: Relation
    reason: str


class RelationAnswer(BaseModel):
    related: list[RelatedSkill]


def text_hash(text: str) -> str:
    """Key of a posting: whitespace differences do not create a second entry."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]
