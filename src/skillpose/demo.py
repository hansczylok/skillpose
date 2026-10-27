"""Demo bundles: the finished results of the gold set, shipped as package data.

A stranger opens the web app without an API key and must see a complete demo at no cost, so the results travel with
the package as JSON (one bundle per person plus the costs of the build) and the app loads them into a fresh store at
start. `skillpose demo build` creates the bundles from the gold set; what an earlier run (or `skillpose eval --save`)
stored is reused, so a rebuild pays only for what is missing. The JSON is sorted, so an unchanged result gives no diff.

The demo shows each posting also in the other language. The translations are files of the gold set
(translations/<posting file stem>.<lang>.md, see the README there), written once in a Claude Code session: the build
reads them, costs nothing for them and makes no call for them.

The demo is one database, so its persons share one set of answers to the conditions of postings (driving licence,
travel, ...), as every database has one (invented, in expected.yaml of the gold set: `conditions`). The build matches
with them and writes them into every bundle; the loader saves them once.
"""

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from skillpose.core.language import Lang, detect_language, other_language
from skillpose.core.models import CandidateProfile, Conditions, DevelopmentPath, JobMatch, JobProfile, text_hash
from skillpose.core.prompts import LANGUAGE_NAMES
from skillpose.core.textio import read_cv
from skillpose.pipeline import (
    Run,
    analyze_postings,
    build_path_for,
    collect_batches,
    ensure_profile,
    estimate,
    ingest_postings,
    market,
    path_targets,
    plan_postings,
    rematch,
)
from skillpose.store.db import CostRow, Store

log = logging.getLogger(__name__)

COSTS_FILE = "costs.json"
LANGUAGES = ("de", "en")
DEMO_MODEL = "demo"  # the model column of loaded results: the bundles do not carry it
KEY = re.compile(r"[A-Za-z0-9_-]+")
TRANSLATIONS_DIR = "translations"
TRANSLATION_FILE = re.compile(r"(?P<stem>.+)\.(?P<lang>de|en)\.md")
TRANSLATION_MODEL = "claude-code"  # the model column of a translation from a file: made in a Claude Code session


class DemoTranslation(BaseModel):
    title: str  # the job title of the posting (JobProfile.title), translated
    text: str   # the whole posting, translated


class DemoPosting(BaseModel):
    hash: str
    refs: list[str]
    text: str
    job_profile: JobProfile
    translations: dict[Lang, DemoTranslation] = {}  # by language, sorted; bundles from before have none


class DemoBundle(BaseModel):
    version: Literal[1] = 1
    key: str                      # file stem of the CV, e.g. "maria_berger"
    cv_name: str
    cv_text: str
    profile: CandidateProfile
    # the answers to the conditions of the demo database, the same in every bundle; bundles from before have none
    conditions: Conditions = Field(default_factory=Conditions)
    postings: list[DemoPosting]   # sorted by hash
    matches: list[JobMatch]       # sorted by text_hash
    paths: list[DevelopmentPath]  # every target path in German and English, sorted by role_family, then language


class DemoCosts(BaseModel):
    version: Literal[1] = 1
    rows: list[CostRow]


class DemoBuildError(RuntimeError):
    """A person of the gold set got no profile; a demo without them would be incomplete."""


@dataclass
class GoldPerson:
    key: str
    lang: str
    text: str


def demo_data_dir() -> Path:
    """The packaged bundles: skillpose/demo_data."""
    return Path(__file__).parent / "demo_data"


def _write(path: Path, model: BaseModel) -> Path:
    path.write_text(model.model_dump_json(indent=1) + "\n", encoding="utf-8", newline="\n")
    return path


def export_demo(store: Store, out_dir: Path, cv_ids: dict[str, int]) -> list[Path]:
    """Write <key>.json per CV and costs.json (the whole ledger of the store); returns the files in this order. Every
    bundle carries the answers to the conditions that the store holds."""
    out_dir = Path(out_dir)
    for key in cv_ids:
        if not KEY.fullmatch(key) or key.lower() == Path(COSTS_FILE).stem:
            raise ValueError(f"demo key {key!r} must be a plain file stem (letters, digits, _ and -), not 'costs'")
    names = {row.id: row.name for row in store.cvs()}
    refs = {row.hash: row.refs for row in store.postings()}
    answers = store.conditions()
    bundles = []
    for key, cv_id in cv_ids.items():
        profile = store.profile(cv_id)
        if profile is None:
            raise ValueError(f"CV {cv_id} ({key}) has no profile")
        matches = sorted(store.matches(cv_id), key=lambda jm: jm.text_hash)
        postings = [DemoPosting(hash=jm.text_hash, refs=refs[jm.text_hash], text=store.posting_text(jm.text_hash),
                                job_profile=store.job_profile(jm.text_hash),
                                translations={lang: DemoTranslation(title=title, text=text)
                                              for lang, (title, text) in store.translations(jm.text_hash).items()})
                    for jm in matches]
        paths = store.paths_all(cv_id)
        bundles.append(DemoBundle(key=key, cv_name=names[cv_id], cv_text=store.cv_text(cv_id), profile=profile,
                                  conditions=answers, postings=postings, matches=matches, paths=paths))
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [_write(out_dir / f"{bundle.key}.json", bundle) for bundle in bundles]
    return [*files, _write(out_dir / COSTS_FILE, DemoCosts(rows=store.cost_rows()))]


def _bundle_files(src_dir: Path) -> list[Path]:
    if not src_dir.is_dir():
        return []
    return sorted(p for p in src_dir.glob("*.json") if p.name != COSTS_FILE)


def load_demo(src_dir: Path, db_path: Path) -> int:
    """A fresh store at db_path (an old file there is deleted) with every bundle of src_dir, its costs and the answers
    to the conditions. Returns the number of persons; without bundles 0, and no file is created. ValueError, before
    any file changes, for bundles with different answers: the demo store has one set, as every database."""
    src_dir, db_path = Path(src_dir), Path(db_path)
    bundles = [DemoBundle.model_validate_json(p.read_bytes()) for p in _bundle_files(src_dir)]
    if not bundles:
        return 0
    answers = bundles[0].conditions
    if differ := [bundle.key for bundle in bundles if bundle.conditions != answers]:
        raise ValueError(f"demo bundles {', '.join(differ)}: the answers to the conditions differ from those of "
                         f"{bundles[0].key}; the demo has one set of answers, so rebuild the bundles together")
    costs = src_dir / COSTS_FILE
    rows = DemoCosts.model_validate_json(costs.read_bytes()).rows if costs.is_file() else []
    db_path.unlink(missing_ok=True)
    with Store(db_path) as store:
        for bundle in bundles:
            cv_id = store.save_cv(bundle.cv_name, bundle.cv_text)
            store.save_profile(cv_id, bundle.profile, DEMO_MODEL)
            for posting in bundle.postings:
                hashes = {store.save_posting(posting.text, ref) for ref in posting.refs or [None]}
                if hashes != {posting.hash} or posting.job_profile.text_hash != posting.hash:
                    raise ValueError(f"demo bundle {bundle.key}: posting {posting.hash} does not match its text")
                store.save_job_profile(posting.job_profile, DEMO_MODEL)
                for lang, translation in posting.translations.items():
                    store.save_translation(posting.hash, lang, translation.title, translation.text, DEMO_MODEL)
            for jm in bundle.matches:
                store.save_match(cv_id, jm)
            for path in bundle.paths:
                store.save_path(cv_id, path, DEMO_MODEL)
        store.save_conditions(answers)
        store.import_costs(rows)
    return len(bundles)


def read_goldset(goldset: Path) -> tuple[list[GoldPerson], list[tuple[str, str]]]:
    """The persons of expected.yaml (each with the language of their CV) and the postings as (file name, text)."""
    goldset = Path(goldset)
    expected = yaml.safe_load((goldset / "expected.yaml").read_text(encoding="utf-8")) or {}
    people = []
    for key, entry in (expected.get("cvs") or {}).items():
        lang = (entry or {}).get("language")
        if lang not in LANGUAGES:
            raise ValueError(f"expected.yaml: cvs.{key}.language must be de or en, not {lang!r}")
        people.append(GoldPerson(key=key, lang=lang, text=read_cv(goldset / "cvs" / f"{key}.md")))
    postings = [(p.name, p.read_text(encoding="utf-8")) for p in sorted((goldset / "postings").glob("*.md"))]
    return people, postings


def read_conditions(goldset: Path) -> Conditions:
    """The invented answers of the demo persons to the conditions of postings: `conditions` in expected.yaml, none
    answered without it. ValueError for an answer that Conditions does not know or take (a misspelt one would else
    count as not answered)."""
    expected = yaml.safe_load((Path(goldset) / "expected.yaml").read_text(encoding="utf-8")) or {}
    answers = expected.get("conditions") or {}
    if isinstance(answers, dict) and (unknown := sorted(set(map(str, answers)) - set(Conditions.model_fields))):
        raise ValueError(f"expected.yaml: conditions: unknown answer {', '.join(unknown)}")
    try:
        return Conditions.model_validate(answers)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" if error["loc"] else error["msg"]
                             for error in e.errors())
        raise ValueError(f"expected.yaml: conditions: {problems}") from None


def parse_translation(path: Path) -> DemoTranslation:
    """A translation file: "# <translated title>" on the first line, the translated posting below it. ValueError,
    naming the file, for anything else."""
    path = Path(path)
    try:
        lines = path.read_bytes().decode("utf-8-sig").splitlines()  # a BOM of a Windows editor is no part of the title
    except UnicodeDecodeError:
        raise ValueError(f"{path.name}: the file is not UTF-8") from None
    head = lines[0] if lines else ""
    if not head.startswith("# ") or not head[2:].strip():
        raise ValueError(f"{path.name}: the first line must be '# <translated title>'")
    text = "\n".join(lines[1:]).lstrip("\n").rstrip()
    if not text:
        raise ValueError(f"{path.name}: no text below the title")
    return DemoTranslation(title=head[2:].strip(), text=text)


def read_translations(goldset: Path, postings: list[tuple[str, str]]) -> dict[str, dict[str, DemoTranslation]]:
    """The translation files of the gold set by posting file stem and language; postings as read_goldset returns
    them. A file for a posting that the gold set does not have is skipped with a warning. ValueError, naming the file,
    for a name that is not <posting file stem>.<de|en>.md, for a file that parse_translation refuses and for a
    translation into the language the posting is written in."""
    folder = Path(goldset) / TRANSLATIONS_DIR
    if not folder.is_dir():
        return {}
    texts = {Path(name).stem: text for name, text in postings}
    found: dict[str, dict[str, DemoTranslation]] = {}
    for path in sorted(folder.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        name = TRANSLATION_FILE.fullmatch(path.name)
        if name is None:
            raise ValueError(f"{path.name}: the name must be <posting file stem>.<de|en>.md")
        stem, lang = name["stem"], name["lang"]
        if stem not in texts:
            log.warning("%s: the gold set has no posting %s.md; skipped", path.name, stem)
            continue
        if detect_language(texts[stem]) == lang:
            raise ValueError(f"{path.name}: posting {stem} is written in {LANGUAGE_NAMES[lang]} already")
        found.setdefault(stem, {})[lang] = parse_translation(path)
    return found


def _store_translations(run: Run, items: list[tuple[str, str]], hashes: list[str],
                        translations: dict[str, dict[str, DemoTranslation]]) -> None:
    """The translations of each stored posting are exactly those of its files, under the hash of its text now. A
    posting without a translation into the other language gets a warning: the demo shows it in its language only."""
    stored = set(hashes)
    for name, text in items:
        if (h := text_hash(text)) not in stored:
            continue  # empty or too long: not in the store
        own = translations.get(Path(name).stem, {})
        run.store.set_translations(h, {lang: (t.title, t.text) for lang, t in sorted(own.items())}, TRANSLATION_MODEL)
        if (target := other_language(detect_language(text))) not in own:
            log.warning("posting %s has no translation into %s (%s/%s.%s.md)", name, target, TRANSLATIONS_DIR,
                        Path(name).stem, target)


def build_estimate(run: Run, goldset: Path, n_paths: int) -> float:
    """What build_demo may cost, before any call: the missing profiles, the open postings and n_paths paths per
    person in each language (a stored path that still fits costs nothing, so this is an upper bound). Translations
    are files and cost nothing; they are read here too, so a broken file stops the command before it asks for money.
    Stores the postings (free), so the plan knows which of them are open."""
    people, items = read_goldset(goldset)
    read_translations(goldset, items)
    read_conditions(goldset)
    hashes, _ = ingest_postings(run, items)
    plan = plan_postings(run, hashes)
    missing = [p for p in people if (cv_id := run.store.find_cv(p.text)) is None or run.store.profile(cv_id) is None]
    n_all = len(people) * n_paths * len(LANGUAGES)
    return (sum(estimate(run, cv_text=p.text) for p in missing)
            + estimate(run, plan=plan, n_paths=n_all))


def build_demo(run: Run, goldset: Path, out_dir: Path, *, n_paths: int = 2) -> list[Path]:
    """Profiles, job profiles, matches and up to n_paths paths per person of the gold set, then export_demo.

    Each person is analyzed in the language of their CV (expected.yaml: cvs.<key>.language), so the profile quotes
    the CV as written. Each path is built in German and in English, so the paths page switches with the interface:
    first all paths in the language of the CV, then all in the other one (one cached prefix per language). Postings
    are analyzed once, in the language of the run. The translation files and the answers to the conditions are read
    before any call (a broken one stops the build with ValueError); the answers replace those of the store, so every
    person is matched with them. BudgetExceeded goes to the caller; what was paid for stays in the store and the next
    run continues from there."""
    people, items = read_goldset(goldset)
    translations = read_translations(goldset, items)
    answers = read_conditions(goldset)
    hashes, skipped = ingest_postings(run, items)
    for message in skipped:
        log.warning(message)
    _store_translations(run, items, hashes, translations)
    plan = plan_postings(run, hashes)
    if plan.pending:
        collect_batches(run)  # an interrupted build submitted them: collect, never pay twice
    analyze_postings(run, plan, wait=True)
    run.store.save_conditions(answers)
    cv_ids: dict[str, int] = {}
    for person in people:
        cv_id = run.store.find_cv(person.text)
        if cv_id is None:
            cv_id = run.store.save_cv(f"{person.key}.md", person.text)
        own = replace(run, lang=person.lang)
        profile, error = ensure_profile(own, cv_id)
        if profile is None:
            raise DemoBuildError(f"CV {person.key}: {error}")
        rematch(own, cv_id, hashes)
        targets = path_targets(market(run.store, run.settings, cv_id), n_paths)
        for lang in [person.lang, *[other for other in LANGUAGES if other != person.lang]]:  # the CV's language first
            for family in targets:
                path, reason = build_path_for(replace(own, lang=lang), cv_id, family)
                if path is None:
                    log.warning("no %s path for %s and %s: %s", lang, person.key, family, reason)
        cv_ids[person.key] = cv_id
    return export_demo(run.store, out_dir, cv_ids)
