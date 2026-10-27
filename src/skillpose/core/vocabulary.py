"""Skill vocabulary: one id per skill, many spellings.

Claude sees a frozen, sorted snapshot of the vocabulary in the cached part of every prompt. New skills that Claude
proposes are merged only after a run, so the cached prefix never changes during a run.

The category of a skill also checks the kind that step 2 gives a requirement: a soft skill that comes back as a tool,
certificate, degree or language is a soft skill (requirement_kind).
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path

import yaml

from skillpose.core.models import CandidateProfile, JobProfile, ReqKind, Skill, SkillProposal

_SEPARATORS = re.compile(r"[-_./]")
SNAPSHOT_HEADER = "id | name_en | name_de | category | aliases"
# the kinds that a skill of the category soft cannot have. Experience stays: "Erfahrung in der Führung von Teams" is
# evidence a CV can show (spec B), and the vocabulary files leadership under soft. A condition stays: Claude files
# "Reisebereitschaft" under soft.
NOT_SOFT_KINDS = ("tool", "certificate", "degree", "spoken_language")


def normalize_name(s: str) -> str:
    s = _SEPARATORS.sub(" ", unicodedata.normalize("NFKC", s).casefold())
    return " ".join(s.split())


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "", normalize_name(s).replace(" ", "_"))
    return re.sub(r"_+", "_", s).strip("_")


def _keys(name: str) -> set[str]:
    """Lookup keys of a name: normalized, and normalized without spaces ("power bi" = "powerbi")."""
    n = normalize_name(name)
    return {n, n.replace(" ", "")} - {""}


def _names(skill: Skill) -> list[str]:
    return [skill.id, skill.name_en, skill.name_de, *skill.aliases]


def proposals_of(*owners: CandidateProfile | JobProfile | None) -> dict[str, SkillProposal]:
    """The skills Claude proposed in these profiles and job profiles, by id (None is skipped). They name the ids a
    vocabulary may not know: the demo store has no vocabulary additions, and a run merges its proposals only at its
    end."""
    found: dict[str, SkillProposal] = {}
    for owner in owners:
        for proposal in owner.proposed_skills if owner is not None else []:
            found.setdefault(proposal.id, proposal)
    return found


def _read_seed(path: Path | None) -> list[Skill]:
    if path is None:
        text = resources.files("skillpose.core").joinpath("data/skills_seed.yaml").read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    skills = []
    for category, rows in yaml.safe_load(text).items():
        for row in rows:
            skill_id, name_en, name_de, *rest = row
            skills.append(Skill(id=skill_id, name_en=str(name_en), name_de=str(name_de), category=category,
                                aliases=[str(a) for a in (rest[0] if rest else [])]))
    return skills


class Vocabulary:
    def __init__(self, skills: Iterable[Skill] = ()):
        self._skills: dict[str, Skill] = {}
        self._index: dict[str, str] = {}
        for skill in skills:
            self._add(skill, strict=True)

    @classmethod
    def load(cls, seed: Path | None = None, extra: Iterable[Skill] = ()) -> "Vocabulary":
        vocab = cls(_read_seed(seed))
        for skill in extra:
            vocab._add(skill, strict=False)
        return vocab

    def _add(self, skill: Skill, strict: bool) -> bool:
        keys = set().union(*(_keys(n) for n in _names(skill)))
        clash = {self._index[k] for k in keys if k in self._index} - {skill.id}
        if clash or skill.id in self._skills:
            if strict:
                raise ValueError(f"skill {skill.id!r} collides with {sorted(clash) or [skill.id]}")
            return False
        self._skills[skill.id] = skill
        for k in keys:
            self._index[k] = skill.id
        return True

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def ids(self) -> set[str]:
        return set(self._skills)

    def resolve(self, name_or_id: str) -> str | None:
        for k in _keys(name_or_id):
            if k in self._index:
                return self._index[k]
        return None

    def snapshot(self) -> str:
        lines = sorted(f"{s.id} | {s.name_en} | {s.name_de} | {s.category} | {', '.join(sorted(s.aliases))}"
                       for s in self._skills.values())
        return "\n".join([SNAPSHOT_HEADER, *lines])

    def resolve_or_propose(self, skill_id: str, name: str, category: str,
                           proposals: dict[str, SkillProposal]) -> str:
        """Id of a skill Claude named; an unknown skill becomes a proposal (merged only after the run)."""
        found = self.resolve(skill_id) or self.resolve(name)
        if found:
            return found
        new_id = slug(name) or slug(skill_id)
        proposals.setdefault(new_id, SkillProposal(id=new_id, name=name, category=category))
        return new_id

    def requirement_kind(self, skill_id: str, kind: ReqKind, proposals: Mapping[str, SkillProposal]) -> ReqKind:
        """The kind of a requirement after the post-check of step 2: soft for a skill of the category soft that came
        back as a tool, certificate, degree or language. The category is that of the vocabulary, or of Claude's
        proposal for a skill the vocabulary does not know."""
        known = self.get(skill_id)
        category = known.category if known else (proposals[skill_id].category if skill_id in proposals else None)
        return "soft" if category == "soft" and kind in NOT_SOFT_KINDS else kind

    def merge(self, proposals: Iterable[SkillProposal]) -> list[Skill]:
        added = []
        for p in proposals:
            if self.resolve(p.name) or self.resolve(p.id):
                continue
            skill = Skill(id=slug(p.id) or slug(p.name), name_en=p.name, name_de=p.name, category=p.category)
            if skill.id and self._add(skill, strict=False):
                added.append(skill)
        return added
