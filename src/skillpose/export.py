"""Markdown export of the analysis of one or more CVs, to talk it over (with a coach, a friend, in a chat).

The document comes from the stored results only: no key, no call. It holds content of the CV (quotes, positions), so
it starts with a privacy line. The web app (a link on the profile page) and `skillpose export` call the same function.
The words are those of the pages (web/i18n/*.yaml): tiers, levels, statuses, role families and skill names.

Free text (a quote, a name) goes into a table cell or a line as one line, with `\\`, `|`, `<` and the backtick escaped,
so that it can neither break a table nor open a tag. Emphasis and links in such a text stay as they were written. Text
that begins a line (the rationale of a path, as a quote, and the activity of a step) also has a leading mark escaped,
so that it cannot open a heading, a list, a rule, a fence or a link definition.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from skillpose.core.config import Settings
from skillpose.core.market import is_blocking
from skillpose.core.models import (
    LEVEL_ORDER,
    NOT_COUNTED,
    STRENGTH_ORDER,
    CandidateProfile,
    CandidateSkill,
    DevelopmentPath,
    JobMatch,
    RoleReport,
)
from skillpose.core.textio import ALLOWED_CV_TYPES
from skillpose.core.vocabulary import Vocabulary
from skillpose.pipeline import market
from skillpose.store.db import CvRow, PostingRow, Store
from skillpose.web.i18n import load_texts, reason_text, translate
from skillpose.web.labels import Proposed, cv_proposals, percent, role_label, skill_name, tier_rules

QUOTE_CHARS = 110      # a quote in the skills table is cut after this many characters
NO_VALUE = "–"
TIER_KEYS = {1: "fits", 2: "learnable", 3: "big_gap", None: "none"}
LEVELS_BEST_FIRST = sorted(LEVEL_ORDER, key=lambda level: -LEVEL_ORDER[level])
FILENAME_CHARS = 60
_GERMAN_LETTERS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})
_MARKDOWN = re.compile(r"([\\|<`])")
# what opens a block at the start of a line: a heading, a quote, a list item, a rule, a fence, a link definition
_BLOCK_MARK = re.compile(r"[#>+*=_~\[-]|\d{1,9}[.)]")


class ExportError(Exception):
    """A CV that cannot be exported: reason "unknown" (no such CV) or "no_profile" (it is not analyzed yet)."""

    def __init__(self, cv_id: int, reason: Literal["unknown", "no_profile"]):
        super().__init__(f"CV {cv_id}: {reason}")
        self.cv_id = cv_id
        self.reason = reason


def _md(text: object) -> str:
    """Text as one line, safe in a table cell or a line of the document."""
    return _MARKDOWN.sub(r"\\\1", " ".join(str(text).split()))


def _md_start(text: object) -> str:
    """Text as one line that begins a block (a quote, a list item): as _md, and a mark at its start that would open a
    block of its own is escaped, so it stays text."""
    line = _md(text)
    if (mark := _BLOCK_MARK.match(line)) is not None:
        at = mark.end() - 1
        line = f"{line[:at]}\\{line[at:]}"
    return line


def _cut(text: str, limit: int = QUOTE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(_md(cell) for cell in cells) + " |"

    return [line(headers), "|" + "---|" * len(headers), *(line(row) for row in rows)]


def _heading(level: int, text: str) -> str:
    return f"{'#' * level} {text}"


def export_filename(cv_name: str, day: date) -> str:
    """skillpose-<name>-<YYYY-MM-DD>.md for the download: ASCII letters, digits, "_" and "-" only (umlauts spelled
    out), no path parts, no quote or line break that could end the header value."""
    name = cv_name
    for suffix in ALLOWED_CV_TYPES:
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
            break
    ascii_name = unicodedata.normalize("NFKD", name.translate(_GERMAN_LETTERS)).encode("ascii", "ignore").decode()
    safe = re.sub(r"[^A-Za-z0-9_]+", "-", ascii_name).strip("-")[:FILENAME_CHARS].strip("-") or "cv"
    return f"skillpose-{safe}-{day.isoformat()}.md"


@dataclass
class _Ctx:
    """What the parts of the document share."""
    store: Store
    settings: Settings
    vocab: Vocabulary
    lang: str
    texts: dict[str, dict[str, str]]
    postings: Mapping[str, PostingRow]
    upload_time: bool   # the demo store is made at start: its CVs have no real upload time

    def t(self, key: str, **params: object) -> str:
        return translate(self.texts, self.lang, key, **params)

    def tier(self, tier: int | None) -> str:
        return self.t(f"tier.{TIER_KEYS[tier]}")

    def number(self, value: float | None) -> str:
        """Years as the pages show them: 10, 2.5 (2,5 in German); a dash when unknown."""
        if value is None:
            return NO_VALUE
        return f"{value:g}".replace(".", ",") if self.lang == "de" else f"{value:g}"

    def weeks(self, count: int) -> str:
        return self.t("paths.week") if count == 1 else self.t("paths.weeks", weeks=count)


def _legend(ctx: _Ctx, level: int) -> list[str]:
    t = ctx.t
    words = ", ".join(f"**{ctx.tier(tier)}**" for tier in TIER_KEYS)
    rules = tier_rules(ctx.settings)
    return [_heading(level, t("export.tiers_title")), "",
            f"{t('tier.help_best')} {t('export.tier_words', words=words)}", "",
            *(f"- {t(f'tier.{key}', **rules)}" for key in ("help_1", "help_2", "help_3", "help_role")), ""]


def _overview(ctx: _Ctx, cv: CvRow, profile: CandidateProfile, level: int) -> list[str]:
    t = ctx.t
    hidden = sum(skill.hidden for skill in profile.skills)
    skills = t("count.skills", count=len(profile.skills)) + (f" ({t('export.skills_hidden', count=hidden)})"
                                                            if hidden else "")
    lines = [_heading(level, t("export.overview_title")), ""]
    if ctx.upload_time:
        lines.append("- " + t("export.uploaded", time=cv.created[:16].replace("T", " ")))
    lines += ["- " + t("export.text_length", length=t("cv.chars", count=len(ctx.store.cv_text(cv.id)))),
              "- " + t("export.profile_counts", skills=skills,
                       stations=t("count.stations", count=len(profile.stations)))]
    counts = Counter(skill.level for skill in profile.skills)
    if counts:
        levels = ", ".join(f"{t(f'level.{name}')} {counts[name]}" for name in LEVELS_BEST_FIRST if counts[name])
        lines.append("- " + t("export.levels", levels=levels))
    return [*lines, ""]


def _strongest_quote(ctx: _Ctx, skill: CandidateSkill) -> str:
    """The quote that shows the skill best: an outcome before an activity before a claim, a quote found in the CV
    before one that was not (that one is marked). The first of equal ones wins."""
    best = max(skill.evidence, key=lambda e: (STRENGTH_ORDER[e.strength], e.verified), default=None)
    if best is None:
        return NO_VALUE
    return _cut(best.quote) + ("" if best.verified else f" ({ctx.t('profile.unverified')})")


def _skills(ctx: _Ctx, profile: CandidateProfile, proposed: Proposed, level: int) -> list[str]:
    t = ctx.t
    head = [_heading(level, t("export.skills_title")), ""]
    if not profile.skills:
        return [*head, t("profile.no_skills"), ""]
    names = {s.skill_id: skill_name(ctx.vocab, s.skill_id, ctx.lang, proposed) for s in profile.skills}
    ordered = sorted(profile.skills, key=lambda s: (s.hidden, -LEVEL_ORDER[s.level], -(s.years or 0),
                                                    names[s.skill_id].casefold(), s.skill_id))
    rows = [[names[s.skill_id] + (f" ({t('profile.hidden')})" if s.hidden else ""), t(f"level.{s.level}"),
             ctx.number(s.years), str(s.last_used or NO_VALUE), str(len(s.evidence)), _strongest_quote(ctx, s)]
            for s in ordered]
    headers = [t("export.col_skill"), t("profile.level"), t("export.col_years"), t("export.col_last_used"),
               t("export.col_evidence"), t("export.col_quote")]
    return [*head, *_table(headers, rows), ""]


def _market(ctx: _Ctx, reports: list[RoleReport], proposed: Proposed, level: int) -> list[str]:
    t = ctx.t
    head = [_heading(level, t("nav.market")), ""]
    if not reports:
        return [*head, t("matches.none"), ""]
    rows = [[role_label(t, r.role_family), ctx.tier(r.tier), str(r.postings), f"{percent(r.median_coverage)} %",
             ", ".join(f"{skill_name(ctx.vocab, g.skill_id, ctx.lang, proposed)} ({g.postings})"
                       for g in r.top_gaps) or NO_VALUE] for r in reports]
    headers = [t("matches.col_role"), t("matches.col_tier"), t("postings.title"), t("market.coverage"),
               t("export.col_gaps")]
    return [*head, *_table(headers, rows), ""]


def _open_musts(ctx: _Ctx, jm: JobMatch, proposed: Proposed) -> str:
    """The must requirements that are not met, a gap before a partly met one: "Kubernetes (gap, months)". A soft
    skill is no gap and a condition is no requirement to learn: neither is open (a condition that blocks has its own
    line, see _blocked)."""
    unmet = [m for m in jm.matches if m.requirement.importance == "must" and m.status not in ("met", *NOT_COUNTED)
             and m.requirement.kind not in ("soft", "condition")]
    unmet.sort(key=lambda m: m.status != "gap")
    parts = []
    for m in unmet:
        why = [ctx.t(f"status.{m.status}")] + ([ctx.t(f"gap.{m.gap_type}")] if m.gap_type else [])
        parts.append(f"{skill_name(ctx.vocab, m.requirement.skill_id, ctx.lang, proposed)} ({', '.join(why)})")
    return ", ".join(parts) or NO_VALUE


def _not_verifiable(jm: JobMatch) -> int:
    """How many soft skills the posting names: a CV cannot prove them (a match from an older run may still hold one as
    a gap, so the kind counts as well as the status)."""
    return sum(m.status == "unverifiable" or m.requirement.kind == "soft" for m in jm.matches)


def _blocked(ctx: _Ctx, ref: str, jm: JobMatch, proposed: Proposed) -> list[str]:
    """One line for each must condition of the posting that the answers in the profile do not meet."""
    return [f"- {_md(ref)}: {_md(skill_name(ctx.vocab, m.requirement.skill_id, ctx.lang, proposed))} "
            f"({_md(reason_text(m.reason, ctx.lang))})" for m in jm.matches if is_blocking(m)]


def _matches(ctx: _Ctx, cv_id: int, proposed: Proposed, level: int) -> list[str]:
    t = ctx.t
    head = [_heading(level, t("nav.matches")), ""]
    rows = []
    for jm in ctx.store.matches(cv_id):
        jp = ctx.store.job_profile(jm.text_hash)
        if jp is None:
            continue
        posting = ctx.postings.get(jm.text_hash)
        ref = posting.refs[0] if posting is not None and posting.refs else jp.title
        rows.append(((jm.tier, -jm.must_coverage, ref.casefold(), jm.text_hash),
                     [ctx.tier(jm.tier), f"{percent(jm.must_coverage)} %", ref, role_label(t, jp.role_family),
                      _open_musts(ctx, jm, proposed), str(_not_verifiable(jm))],
                     _blocked(ctx, ref, jm, proposed)))
    if not rows:
        return [*head, t("matches.none"), ""]
    headers = [t("matches.col_tier"), t("matches.col_must"), t("matches.col_title"), t("matches.col_role"),
               t("export.col_open"), t("export.col_not_verifiable")]
    rows.sort(key=lambda r: r[0])
    blocked = [line for _, _, lines in rows for line in lines]
    return [*head, *_table(headers, [cells for _, cells, _ in rows]), "",
            *([t("export.blocked_title"), "", *blocked, ""] if blocked else [])]


def _path(ctx: _Ctx, path: DevelopmentPath, proposed: Proposed, level: int) -> list[str]:
    t = ctx.t
    lines = [_heading(level, f"{_md(role_label(t, path.role_family))} – {t('paths.total')}: "
                             f"{ctx.weeks(path.total_weeks)}")]
    if rationale := _md_start(path.rationale):
        lines += ["", f"> {rationale}"]   # Claude's words, as a quote
    if path.language != ctx.lang:
        lines += ["", t(f"paths.only_{path.language}")]
    lines.append("")
    for number, step in enumerate(path.steps, 1):
        activity, proof = _md_start(step.activity), _md(step.proof)
        line = f"{number}. " + (f"{activity} – " if activity else "") + ctx.weeks(step.effort_weeks)
        if step.closes:
            line += f"; {t('paths.closes')}: " + ", ".join(
                _md(skill_name(ctx.vocab, skill_id, ctx.lang, proposed)) for skill_id in step.closes)
        if proof:
            line += f"; {t('paths.proof')}: {proof}"
        lines.append(line)
    return [*lines, ""]


def _paths(ctx: _Ctx, cv_id: int, families: list[str], proposed: Proposed, level: int) -> list[str]:
    """The stored paths: in the language of the export if there is one, else the stored one with a note. In the order
    of the market view."""
    stored = ctx.store.paths(cv_id, ctx.lang)
    if not stored:
        return []
    order = sorted(stored, key=lambda f: (families.index(f) if f in families else len(families), f))
    lines = [_heading(level, ctx.t("step.path")), ""]
    for family in order:
        lines += _path(ctx, stored[family], proposed, level + 1)
    return lines


def _cv_part(ctx: _Ctx, cv: CvRow, profile: CandidateProfile, level: int) -> list[str]:
    proposed = cv_proposals(ctx.store, cv.id)
    reports = market(ctx.store, ctx.settings, cv.id)
    return [*_overview(ctx, cv, profile, level), *_skills(ctx, profile, proposed, level),
            *_market(ctx, reports, proposed, level), *_matches(ctx, cv.id, proposed, level),
            *_paths(ctx, cv.id, [r.role_family for r in reports], proposed, level)]


def _comparison(ctx: _Ctx, cvs: list[tuple[CvRow, str, CandidateProfile]], level: int) -> list[str]:
    """Which skills only one CV has and which skills differ in level. A hidden skill does not count: it is not in the
    matches either."""
    t = ctx.t
    held = {cv.id: {s.skill_id: s for s in profile.skills if not s.hidden} for cv, _, profile in cvs}
    proposals = {cv.id: cv_proposals(ctx.store, cv.id) for cv, _, _ in cvs}

    def name(skill_id: str) -> str:
        owner = next(cv_id for cv_id, skills in held.items() if skill_id in skills)
        return skill_name(ctx.vocab, skill_id, ctx.lang, proposals[owner])

    holders: dict[str, list[int]] = {}
    for cv_id, skills in held.items():
        for skill_id in skills:
            holders.setdefault(skill_id, []).append(cv_id)
    lines = [_heading(level, t("export.compare_title")), ""]
    for cv, shown, _ in cvs:
        only = sorted((name(i) for i, owners in holders.items() if owners == [cv.id]), key=str.casefold)
        label = t("export.cv_label", id=cv.id, name=_md(shown))
        lines.append("- " + t("export.only_in", cv=label, skills=", ".join(_md(n) for n in only) or NO_VALUE))
    different = []
    for skill_id in sorted(holders, key=lambda i: name(i).casefold()):
        levels = {cv_id: held[cv_id][skill_id].level for cv_id in holders[skill_id]}
        if len(set(levels.values())) > 1:
            where = "; ".join(t("export.level_in", id=cv_id, level=t(f"level.{level_name}"))
                              for cv_id, level_name in levels.items())
            different.append(f"{_md(name(skill_id))} ({where})")
    lines.append("- " + t("export.other_level", skills=", ".join(different) or NO_VALUE))
    return [*lines, ""]


def build_export(store: Store, settings: Settings, cv_ids: Sequence[int], lang: str, *,
                 texts: dict[str, dict[str, str]] | None = None, names: Mapping[int, str] | None = None,
                 today: date | None = None, upload_time: bool = True) -> str:
    """The Markdown document of these CVs (all analyzed) in lang. With several CVs it holds each one and a comparison.
    names: the name to show for a CV (the demo shows a person, not a file name); texts: the loaded i18n texts;
    upload_time: False leaves out the upload time (the demo has none).
    ExportError: a CV that does not exist or has no profile; nothing is built then."""
    if not cv_ids:
        raise ValueError("no CV to export")
    rows = {cv.id: cv for cv in store.cvs()}
    cvs = []
    for cv_id in dict.fromkeys(cv_ids):
        if cv_id not in rows:
            raise ExportError(cv_id, "unknown")
        profile = store.profile(cv_id)
        if profile is None:
            raise ExportError(cv_id, "no_profile")
        cvs.append((rows[cv_id], (names or {}).get(cv_id, rows[cv_id].name), profile))
    ctx = _Ctx(store=store, settings=settings, vocab=Vocabulary.load(extra=store.added_skills()), lang=lang,
               texts=texts if texts is not None else load_texts(), postings={p.hash: p for p in store.postings()},
               upload_time=upload_time)
    t, several = ctx.t, len(cvs) > 1
    day = (today or date.today()).isoformat()
    title = t("export.title_all", date=day) if several else t("export.title", name=_md(cvs[0][1]), date=day)
    level = 3 if several else 2
    lines = [_heading(1, title), "", t("export.privacy"), "", t("ai.generated"), "", *_legend(ctx, 2)]
    for cv, shown, profile in cvs:
        if several:
            lines += [_heading(2, t("export.cv_heading", id=cv.id, name=_md(shown))), ""]
        lines += _cv_part(ctx, cv, profile, level)
    if several:
        lines += _comparison(ctx, cvs, 2)
    return "\n".join(lines).rstrip("\n") + "\n"
