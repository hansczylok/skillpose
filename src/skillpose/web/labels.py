"""How the pages name things: skills, role families, and the numbers that go into the texts about the tier rules.

No web library is imported here, so the command line (`skillpose export`) writes the same words and numbers as the
pages, also without the web extra.
"""

from collections.abc import Mapping

from skillpose.core.config import Settings
from skillpose.core.models import SkillProposal
from skillpose.core.vocabulary import Vocabulary, proposals_of
from skillpose.store.db import Store

Proposed = Mapping[str, SkillProposal]  # skill id -> what Claude proposed for it (vocabulary.proposals_of)


def percent(value: float) -> int:
    """A share (0 to 1) as whole per cent, .5 rounded up."""
    return int(value * 100 + 0.5)


def tier_rules(settings: Settings) -> dict[str, int]:
    """The numbers of the tier rules for the help box, from the settings: a change in the settings changes the text.
    count (the minimum of postings per role) is the one that picks the singular."""
    rules = settings.rules
    return {"tier1_pct": percent(rules.tier1_coverage), "weeks": rules.tier2_max_weeks,
            "share": percent(rules.role_tier_share), "count": rules.min_jobs_per_role}


def stability_note(t, settings: Settings, lang: str) -> str | None:
    """The note about the run-to-run variance of the analysis, from the `stability` block of the settings (measured
    with `skillpose eval --repeat`); None without the block. It names the day the rules changed after the measurement,
    if the block gives one."""
    measured = settings.stability
    if measured is None:
        return None
    note = t("stability.note", level_same=percent(measured.level_same),
             role_tier_same=percent(measured.role_tier_same), runs=measured.runs, min_jobs=measured.min_jobs)
    if measured.rules_before is None:
        return note
    day = measured.rules_before.strftime("%d.%m.%Y" if lang == "de" else "%Y-%m-%d")
    return f"{note} {t('stability.rules_before', date=day)}"


def role_label(t, family: str) -> str:
    """The name of a role family in the language of the page; a family without a label shows as its id."""
    label = t(f"role.{family}")
    return family if label == f"role.{family}" else label


def skill_name(vocab: Vocabulary, skill_id: str, lang: str, proposed: Proposed | None = None) -> str:
    """The name of a skill in the language of the page. An id the vocabulary does not know shows the name Claude
    proposed for it, else the id itself."""
    known = vocab.get(skill_id)
    if known is not None:
        return known.name_de if lang == "de" else known.name_en
    proposal = (proposed or {}).get(skill_id)
    return skill_id if proposal is None else proposal.name


def cv_proposals(store: Store, cv_id: int) -> dict[str, SkillProposal]:
    """The skills Claude proposed in the profile of a CV and in the job profiles of its matches: the names of the
    market view and of the paths."""
    jobs = [store.job_profile(jm.text_hash) for jm in store.matches(cv_id)]
    return proposals_of(store.profile(cv_id), *jobs)
