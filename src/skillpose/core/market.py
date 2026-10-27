"""Step 4: the market view. Postings of one role family are summed up to a tier of the role and its top gaps.

The rules follow the spec (sections 5 and 6): the role gets the best tier (the lowest number) that a large enough
share of its postings reaches; the gaps are weighted so that a missing must requirement outweighs a missing nice one,
and a full gap outweighs a partial one. A family with fewer postings than `min_jobs_per_role` gets no tier, only the
numbers. Soft skills and conditions are no gaps (a path could not close them); the must conditions that the user's
answers do not meet are listed apart, as blocking.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from statistics import median

from skillpose.core.config import Rules
from skillpose.core.models import NOT_COUNTED, GapStat, GapType, JobMatch, JobProfile, RequirementMatch, RoleReport

MUST_WEIGHT = 2.0
PARTIAL_WEIGHT = 0.5


def _role_tier(counts: Mapping[int, int], total: int, rules: Rules) -> int:
    """Works on counts: adding two rounded shares can land just below a threshold that the exact share meets."""
    if counts[1] / total >= rules.role_tier_share:
        return 1
    return 2 if (counts[1] + counts[2]) / total >= rules.role_tier_share else 3


def _most_frequent(types: Counter[GapType]) -> GapType | None:
    """The most frequent type; on a tie the alphabetically first."""
    return min(types, key=lambda t: (-types[t], t), default=None)


def _is_gap(m: RequirementMatch) -> bool:
    return m.status not in ("met", *NOT_COUNTED) and m.requirement.kind not in ("soft", "condition")


def is_blocking(m: RequirementMatch) -> bool:
    """A must condition that the answers in the profile do not meet: the posting is out whatever else fits."""
    return m.requirement.kind == "condition" and m.requirement.importance == "must" and m.status == "gap"


def _gap_stats(matches: list[JobMatch], top_n: int | None, counts: Callable[[RequirementMatch], bool]) -> list[GapStat]:
    weight: Counter[str] = Counter()
    postings: Counter[str] = Counter()
    types: dict[str, Counter[GapType]] = defaultdict(Counter)
    for jm in matches:
        seen: set[str] = set()
        for m in jm.matches:
            if not counts(m):
                continue
            skill_id = m.requirement.skill_id
            weight[skill_id] += ((MUST_WEIGHT if m.requirement.importance == "must" else 1.0)
                                 * (PARTIAL_WEIGHT if m.status == "partial" else 1.0))
            if m.gap_type is not None:
                types[skill_id][m.gap_type] += 1
            seen.add(skill_id)
        postings.update(seen)
    ranked = sorted(weight, key=lambda s: (-weight[s], s))[:top_n]  # top_n None: all
    return [GapStat(skill_id=s, weight=weight[s], postings=postings[s], gap_type=_most_frequent(types[s]))
            for s in ranked]


def _report(family: str, matches: list[JobMatch], rules: Rules, top_n: int) -> RoleReport:
    total = len(matches)
    counts = Counter(jm.tier for jm in matches)
    shares = {tier: counts[tier] / total for tier in (1, 2, 3)}
    return RoleReport(role_family=family, postings=total, median_coverage=median(jm.must_coverage for jm in matches),
                      tier=_role_tier(counts, total, rules) if total >= rules.min_jobs_per_role else None,
                      tier_shares=shares, top_gaps=_gap_stats(matches, top_n, _is_gap),
                      conditions_blocking=_gap_stats(matches, None, is_blocking))


def role_reports(jobs: Mapping[str, JobProfile], matches: Iterable[JobMatch], rules: Rules,
                 top_n: int = 5) -> list[RoleReport]:
    """One report per role family, easiest role first; matches whose posting is unknown are skipped."""
    by_family: dict[str, list[JobMatch]] = defaultdict(list)
    for jm in matches:
        if (job := jobs.get(jm.text_hash)) is not None:
            by_family[job.role_family].append(jm)
    reports = [_report(family, group, rules, top_n) for family, group in by_family.items()]
    return sorted(reports, key=lambda r: (r.tier is None, r.tier or 0, -r.postings, r.role_family))
