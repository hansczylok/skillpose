"""SQLite store of skillpose. It is also the cost ledger (implements core.llm.CostLedger).

The file carries its schema version in PRAGMA user_version. Version 1 is the phase 1 layout (user_version 0);
version 2 added posting.error and the path table; version 3 keeps a path per language, so the paths page can switch
with the interface; version 4 adds translations of postings (only the demo shows them); version 5 gives each stored
match the fingerprint of the rules that made it, so a match from other rules is found and recomputed; version 6 keeps
the user's answers to the conditions of postings, one record per database (the app has one user per database).
Opening an older file brings it to the current version and loses nothing. The web app opens one Store per request and
per background task, so the connection may be used from another thread than the one that created it. Every connection
overwrites deleted content (secure_delete): a deleted CV leaves no readable text in the file. A store opened read-only
(`eval --repeat` reads the gold-set store so) cannot change its file at all. StabilityStore is the store of
`eval --repeat`, with the repeated runs in tables of its own.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from skillpose.core.llm import Usage
from skillpose.core.models import (
    LEVEL_ORDER,
    CandidateProfile,
    Conditions,
    DevelopmentPath,
    JobMatch,
    JobProfile,
    Level,
    Relation,
    Skill,
    text_hash,
)

SCHEMA_VERSION = 6

# the path table of version 3; the migration from version 2 creates it under another name first
PATH_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
  cv_id INTEGER NOT NULL REFERENCES cv(id), role_family TEXT NOT NULL, language TEXT NOT NULL, json TEXT NOT NULL,
  model TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY (cv_id, role_family, language));
"""

# version 4: a posting translated into another language (the demo shows each posting in the language of the interface)
TRANSLATION_TABLE = """
CREATE TABLE IF NOT EXISTS posting_translation (
  hash TEXT NOT NULL REFERENCES posting(hash), language TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
  model TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY (hash, language));
"""

# version 6: the user's answers to the conditions of postings; a single row (id 1), as the app has one user per database
CONDITIONS_TABLE = """
CREATE TABLE IF NOT EXISTS conditions (
  id INTEGER PRIMARY KEY CHECK (id = 1), json TEXT NOT NULL, updated TEXT NOT NULL);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS cv (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profile (
  cv_id INTEGER PRIMARY KEY REFERENCES cv(id), json TEXT NOT NULL, model TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS posting (
  hash TEXT PRIMARY KEY, text TEXT NOT NULL, refs TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS job_profile (
  hash TEXT PRIMARY KEY REFERENCES posting(hash), json TEXT NOT NULL, model TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS match (
  cv_id INTEGER NOT NULL REFERENCES cv(id), hash TEXT NOT NULL, json TEXT NOT NULL, fingerprint TEXT,
  PRIMARY KEY (cv_id, hash));
CREATE TABLE IF NOT EXISTS skill_added (
  id TEXT PRIMARY KEY, json TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS relation (
  target TEXT NOT NULL, candidate TEXT NOT NULL, relation TEXT NOT NULL, PRIMARY KEY (target, candidate));
CREATE TABLE IF NOT EXISTS batch (
  id TEXT PRIMARY KEY, hashes TEXT NOT NULL, created TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cost (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, step TEXT NOT NULL, model TEXT NOT NULL,
  input INTEGER NOT NULL, output INTEGER NOT NULL, cache_write INTEGER NOT NULL, cache_read INTEGER NOT NULL,
  usd REAL NOT NULL, batch INTEGER NOT NULL);
""" + PATH_TABLE.format(name="path")


@dataclass
class CostSummary:
    calls: int
    usd: float
    cache_read_share: float  # cache reads / all input tokens (uncached + cache writes + cache reads)


class CvRow(BaseModel):
    id: int
    name: str
    created: str
    analyzed: bool  # a profile exists


class PostingRow(BaseModel):
    hash: str
    refs: list[str]
    created: str
    title: str | None  # from the job profile; None while the posting is open
    role_family: str | None
    status: Literal["analyzed", "open", "batch"]  # batch: part of a batch that is not collected yet
    error: str | None  # why the last attempt failed; cleared when the job profile is saved


class CostRow(BaseModel):
    """One row of the cost table."""
    ts: str
    step: str
    model: str
    input: int
    output: int
    cache_write: int
    cache_read: int
    usd: float
    batch: bool


class StepCost(BaseModel):
    step: str
    calls: int
    usd: float
    cache_read_share: float  # same definition as CostSummary.cache_read_share


class StepMean(BaseModel):
    """The mean cost of one call of a step, in one mode (Message Batch or single call), over the whole ledger."""
    step: str
    batch: bool
    calls: int
    mean_usd: float


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _ts(now: datetime | None = None) -> str:
    return _now(now).strftime("%Y-%m-%dT%H:%M:%S")


def _cache_read_share(uncached: int, written: int, read: int) -> float:
    total = uncached + written + read
    return read / total if total else 0.0


class Store:
    def __init__(self, path: Path, *, read_only: bool = False):
        """read_only: an existing file of the current schema version, opened so that nothing in it can change (no
        schema, no migration, no write): FileNotFoundError for a missing file, ValueError for an older version."""
        path = Path(path)
        if read_only:
            self._open_read_only(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA secure_delete=ON")  # a deleted CV leaves no readable text in the file
            self.db.executescript(SCHEMA)
            self._migrate()
        except BaseException:
            self.db.close()
            raise

    def _open_read_only(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"no database at {path}")
        self.db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
        try:
            if (version := self._one("PRAGMA user_version")[0]) < SCHEMA_VERSION:
                raise ValueError(f"{path} has schema version {version}, this version of skillpose reads version "
                                 f"{SCHEMA_VERSION}: open it once with `skillpose vocab --db \"{path}\"` (no API call, "
                                 "no cost), then try again")
        except BaseException:
            self.db.close()
            raise

    def _migrate(self) -> None:
        """Bring an older database up to SCHEMA_VERSION. Safe to repeat and to run in two stores at once."""
        if self._one("PRAGMA user_version")[0] >= SCHEMA_VERSION:
            return
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")  # a second store waits here, then finds nothing left to do
            if self._one("PRAGMA user_version")[0] >= SCHEMA_VERSION:
                return
            # version 2: posting.error (a phase 1 file gets the path table of version 3 from the CREATE statements)
            if "error" not in self._columns("posting"):
                self.db.execute("ALTER TABLE posting ADD COLUMN error TEXT")
            # version 3: a path per language
            if "language" not in self._columns("path"):
                self._path_per_language()
            # version 4: translations of postings; created here, not with SCHEMA, so a failed migration adds nothing
            self.db.execute(TRANSLATION_TABLE)
            # version 5: the fingerprint of a match; the matches of older files have none, so they count as out of date
            if "fingerprint" not in self._columns("match"):
                self.db.execute("ALTER TABLE match ADD COLUMN fingerprint TEXT")
            # version 6: the answers to the conditions (none yet: every condition is unanswered)
            self.db.execute(CONDITIONS_TABLE)
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}

    def _path_per_language(self) -> None:
        """Version 2 kept one path per CV and role family. SQLite cannot change a primary key, so the rows move to a
        new table with the language of each path (from its JSON) in the key, and the new table takes the old name.
        Runs inside the migration transaction: a row that cannot be read stops the migration, and nothing changes."""
        rows = self.db.execute("SELECT cv_id, role_family, json, model, created FROM path ORDER BY rowid").fetchall()
        self.db.execute("DROP TABLE IF EXISTS path_v3")
        self.db.execute(PATH_TABLE.format(name="path_v3"))
        self.db.executemany(
            "INSERT INTO path_v3 (cv_id, role_family, language, json, model, created) VALUES (?, ?, ?, ?, ?, ?)",
            [(cv_id, family, DevelopmentPath.model_validate_json(raw).language, raw, model, created)
             for cv_id, family, raw, model, created in rows])
        self.db.execute("DROP TABLE path")
        self.db.execute("ALTER TABLE path_v3 RENAME TO path")

    def in_memory_copy(self) -> "Store":
        """A Store on a copy of this database that exists in memory only: whatever is written to the copy never reaches
        the file (the export recomputes matches in it). Close it like any Store."""
        copy = Store(Path(":memory:"))
        try:
            self.db.backup(copy.db)
        except BaseException:
            copy.close()
            raise
        return copy

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _one(self, sql: str, args: tuple = ()):
        return self.db.execute(sql, args).fetchone()

    # --- CVs and profiles ---

    def save_cv(self, name: str, text: str) -> int:
        with self.db:
            cursor = self.db.execute("INSERT INTO cv (name, text, created) VALUES (?, ?, ?)", (name, text, _ts()))
            return cursor.lastrowid

    def find_cv(self, text: str) -> int | None:
        """Id of a CV with exactly this text, so a repeated run reuses its profile instead of paying again."""
        row = self._one("SELECT id FROM cv WHERE text = ? ORDER BY id DESC", (text,))
        return row[0] if row else None

    def cv_text(self, cv_id: int) -> str:
        return self._one("SELECT text FROM cv WHERE id = ?", (cv_id,))[0]

    def cvs(self) -> list[CvRow]:
        """All CVs, newest first."""
        rows = self.db.execute(
            "SELECT id, name, created, EXISTS (SELECT 1 FROM profile WHERE cv_id = cv.id) FROM cv ORDER BY id DESC")
        return [CvRow(id=i, name=name, created=created, analyzed=bool(analyzed)) for i, name, created, analyzed in rows]

    def update_cv_text(self, cv_id: int, text: str) -> None:
        """Replace the text of a CV. KeyError: no such CV. ValueError: the profile exists, so the text is fixed."""
        with self.db:
            changed = self.db.execute(
                "UPDATE cv SET text = ? WHERE id = ? AND NOT EXISTS (SELECT 1 FROM profile WHERE cv_id = ?)",
                (text, cv_id, cv_id)).rowcount
        if changed:
            return
        if self._one("SELECT 1 FROM cv WHERE id = ?", (cv_id,)) is None:
            raise KeyError(cv_id)
        raise ValueError(f"CV {cv_id} has a profile already; its text can no longer change")

    def delete_cv(self, cv_id: int) -> None:
        """Delete a CV with its profile, matches and paths (all languages). Postings and job profiles belong to no CV
        and stay."""
        with self.db:
            self.db.execute("DELETE FROM match WHERE cv_id = ?", (cv_id,))
            self.db.execute("DELETE FROM path WHERE cv_id = ?", (cv_id,))
            self.db.execute("DELETE FROM profile WHERE cv_id = ?", (cv_id,))
            self.db.execute("DELETE FROM cv WHERE id = ?", (cv_id,))

    def save_profile(self, cv_id: int, profile: CandidateProfile, model: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO profile VALUES (?, ?, ?, ?)",
                            (cv_id, profile.model_dump_json(), model, _ts()))

    def profile(self, cv_id: int) -> CandidateProfile | None:
        row = self._one("SELECT json FROM profile WHERE cv_id = ?", (cv_id,))
        return CandidateProfile.model_validate_json(row[0]) if row else None

    def profile_created(self, cv_id: int) -> str | None:
        """When the profile of a CV was made (store timestamp, UTC); None without a profile."""
        row = self._one("SELECT created FROM profile WHERE cv_id = ?", (cv_id,))
        return row[0] if row else None

    def profile_model(self, cv_id: int) -> str | None:
        """The model that made the profile of a CV; None without a profile."""
        row = self._one("SELECT model FROM profile WHERE cv_id = ?", (cv_id,))
        return row[0] if row else None

    def edit_skill(self, cv_id: int, skill_id: str, *, hidden: bool | None = None, level: Level | None = None) -> None:
        """Hide, show or re-level one skill of the stored profile; model and creation time of the profile stay.

        KeyError: the profile has no such skill. ValueError: the CV has no profile (also an unknown CV), or the level
        is not one of the four levels.
        """
        if level is not None and level not in LEVEL_ORDER:
            raise ValueError(f"unknown level {level!r}")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")  # read and write the profile as one step
            profile = self.profile(cv_id)
            if profile is None:
                raise ValueError(f"CV {cv_id} has no profile")
            skill = next((s for s in profile.skills if s.skill_id == skill_id), None)
            if skill is None:
                raise KeyError(skill_id)
            if hidden is not None:
                skill.hidden = hidden
            if level is not None:
                skill.level = level
            self.db.execute("UPDATE profile SET json = ? WHERE cv_id = ?", (profile.model_dump_json(), cv_id))

    # --- the user's answers to the conditions of postings (one record for every CV of the database) ---

    def conditions(self) -> Conditions:
        """The stored answers; all unanswered until the first save."""
        row = self._one("SELECT json FROM conditions WHERE id = 1")
        return Conditions.model_validate_json(row[0]) if row else Conditions()

    def save_conditions(self, conditions: Conditions) -> None:
        """Replace the answers. The stored matches stay as they are: the caller recomputes them
        (pipeline.rematch_offline)."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO conditions (id, json, updated) VALUES (1, ?, ?)",
                            (conditions.model_dump_json(), _ts()))

    # --- postings and job profiles ---

    def save_posting(self, text: str, ref: str | None = None) -> str:
        h = text_hash(text)
        with self.db:
            row = self._one("SELECT refs FROM posting WHERE hash = ?", (h,))
            if row is None:
                self.db.execute("INSERT INTO posting (hash, text, refs, created) VALUES (?, ?, ?, ?)",
                                (h, text, json.dumps([ref] if ref else []), _ts()))
            elif ref and ref not in (refs := json.loads(row[0])):
                self.db.execute("UPDATE posting SET refs = ? WHERE hash = ?", (json.dumps([*refs, ref]), h))
        return h

    def posting_text(self, h: str) -> str:
        return self._one("SELECT text FROM posting WHERE hash = ?", (h,))[0]

    def open_postings(self) -> list[str]:
        rows = self.db.execute("SELECT p.hash FROM posting p LEFT JOIN job_profile j ON j.hash = p.hash "
                               "WHERE j.hash IS NULL ORDER BY p.rowid")
        return [r[0] for r in rows]

    def postings(self) -> list[PostingRow]:
        """All postings in the order they came in, with the state the postings page shows."""
        in_batch = {h for _, hashes in self.pending_batches() for h in hashes}
        rows = self.db.execute("SELECT p.hash, p.refs, p.created, p.error, j.json FROM posting p "
                               "LEFT JOIN job_profile j ON j.hash = p.hash ORDER BY p.rowid")
        result = []
        for h, refs, created, error, jp_json in rows:
            jp = json.loads(jp_json) if jp_json else None
            status = "batch" if h in in_batch else "analyzed" if jp else "open"
            result.append(PostingRow(hash=h, refs=json.loads(refs), created=created, error=error, status=status,
                                     title=jp["title"] if jp else None, role_family=jp["role_family"] if jp else None))
        return result

    def set_posting_error(self, h: str, reason: str) -> None:
        with self.db:
            self.db.execute("UPDATE posting SET error = ? WHERE hash = ?", (reason, h))

    def save_job_profile(self, jp: JobProfile, model: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO job_profile VALUES (?, ?, ?, ?)",
                            (jp.text_hash, jp.model_dump_json(), model, _ts()))
            self.db.execute("UPDATE posting SET error = NULL WHERE hash = ?", (jp.text_hash,))

    def job_profile(self, h: str) -> JobProfile | None:
        row = self._one("SELECT json FROM job_profile WHERE hash = ?", (h,))
        return JobProfile.model_validate_json(row[0]) if row else None

    def job_profile_model(self, h: str) -> str | None:
        """The model that read the requirements of a posting; None while the posting has no job profile."""
        row = self._one("SELECT model FROM job_profile WHERE hash = ?", (h,))
        return row[0] if row else None

    def job_profile_created(self, h: str) -> str | None:
        """When the job profile of a posting was made (store timestamp, UTC); None without a job profile."""
        row = self._one("SELECT created FROM job_profile WHERE hash = ?", (h,))
        return row[0] if row else None

    # --- translations of postings (the demo shows them; the own data have none) ---

    def save_translation(self, h: str, lang: str, title: str, text: str, model: str) -> None:
        """Store the translation of a posting into lang; a new one replaces the old one."""
        with self.db:
            self._insert_translation(h, lang, title, text, model)

    def set_translations(self, h: str, translations: dict[str, tuple[str, str]], model: str) -> None:
        """The translations of a posting are exactly these afterwards: lang -> (title, text). Others are deleted."""
        with self.db:
            self.db.execute("DELETE FROM posting_translation WHERE hash = ?", (h,))
            for lang, (title, text) in translations.items():
                self._insert_translation(h, lang, title, text, model)

    def _insert_translation(self, h: str, lang: str, title: str, text: str, model: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO posting_translation (hash, language, title, text, model, created) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (h, lang, title, text, model, _ts()))

    def translation(self, h: str, lang: str) -> tuple[str, str] | None:
        """(title, text) of the posting in lang; None without such a translation."""
        row = self._one("SELECT title, text FROM posting_translation WHERE hash = ? AND language = ?", (h, lang))
        return (row[0], row[1]) if row else None

    def translations(self, h: str) -> dict[str, tuple[str, str]]:
        """Every translation of a posting, lang -> (title, text), sorted by language."""
        rows = self.db.execute("SELECT language, title, text FROM posting_translation WHERE hash = ? ORDER BY language",
                               (h,))
        return {lang: (title, text) for lang, title, text in rows}

    # --- matches ---

    def save_match(self, cv_id: int, jm: JobMatch, fingerprint: str | None = None) -> None:
        """Store a match with the fingerprint of the rules that made it (core.matching.match_fingerprint). Without
        one the origin is unknown, and stale_matches lists the match for every fingerprint."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO match (cv_id, hash, json, fingerprint) VALUES (?, ?, ?, ?)",
                            (cv_id, jm.text_hash, jm.model_dump_json(), fingerprint))

    def stale_matches(self, fingerprint: str) -> dict[int, list[str]]:
        """The matches that another fingerprint made (or none): CV id -> posting hashes, in the order stored."""
        rows = self.db.execute("SELECT cv_id, hash FROM match WHERE fingerprint IS NOT ? ORDER BY cv_id, rowid",
                               (fingerprint,))
        stale: dict[int, list[str]] = {}
        for cv_id, h in rows:
            stale.setdefault(cv_id, []).append(h)
        return stale

    def matches(self, cv_id: int) -> list[JobMatch]:
        rows = self.db.execute("SELECT json FROM match WHERE cv_id = ? ORDER BY rowid", (cv_id,))
        return [JobMatch.model_validate_json(r[0]) for r in rows]

    # --- vocabulary additions ---

    def add_skills(self, skills: list[Skill]) -> None:
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO skill_added (id, json) VALUES (?, ?)",
                                [(s.id, s.model_dump_json()) for s in skills])

    def added_skills(self, reviewed: bool | None = None) -> list[Skill]:
        """None: accepted and unreviewed skills (the vocabulary snapshot), never rejected ones. False: only the
        unreviewed. True: only the accepted."""
        if reviewed is None:
            rows = self.db.execute("SELECT json FROM skill_added WHERE reviewed >= 0 ORDER BY id")
        else:
            rows = self.db.execute("SELECT json FROM skill_added WHERE reviewed = ? ORDER BY id", (int(reviewed),))
        return [Skill.model_validate_json(r[0]) for r in rows]

    def accept_skills(self, ids: list[str]) -> None:
        with self.db:
            self.db.executemany("UPDATE skill_added SET reviewed = 1 WHERE id = ?", [(i,) for i in ids])

    def reject_skills(self, ids: list[str]) -> None:
        """Mark proposals as rejected (reviewed = -1). They leave added_skills() and stay rejected if proposed again."""
        with self.db:
            self.db.executemany("UPDATE skill_added SET reviewed = -1 WHERE id = ?", [(i,) for i in ids])

    # --- skill relations (step 3b cache) ---

    def relations(self, target: str) -> dict[str, Relation]:
        rows = self.db.execute("SELECT candidate, relation FROM relation WHERE target = ?", (target,))
        return {c: r for c, r in rows}

    def save_relations(self, target: str, rel: dict[str, Relation]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO relation VALUES (?, ?, ?)",
                                [(target, c, r) for c, r in rel.items()])

    # --- development paths ---

    def save_path(self, cv_id: int, path: DevelopmentPath, model: str) -> None:
        """Store the path of one role family in its language; a new path for the same CV, family and language
        replaces the old one, a path in another language stays."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO path (cv_id, role_family, language, json, model, created) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (cv_id, path.role_family, path.language, path.model_dump_json(), model, _ts()))

    def paths(self, cv_id: int, lang: str | None = None) -> dict[str, DevelopmentPath]:
        """One stored path per role family of a CV: the one in lang if there is one, else the one whose language
        comes first in the alphabet (also without lang). path.language says which one it is."""
        rows = self.db.execute("SELECT role_family, language, json FROM path WHERE cv_id = ? "
                               "ORDER BY role_family, language", (cv_id,))
        chosen: dict[str, str] = {}
        for role_family, language, raw in rows:
            if role_family not in chosen or language == lang:
                chosen[role_family] = raw
        return {role_family: DevelopmentPath.model_validate_json(raw) for role_family, raw in chosen.items()}

    def paths_all(self, cv_id: int) -> list[DevelopmentPath]:
        """Every stored path of a CV in every language, sorted by role family and language (the demo export)."""
        rows = self.db.execute("SELECT json FROM path WHERE cv_id = ? ORDER BY role_family, language", (cv_id,))
        return [DevelopmentPath.model_validate_json(raw) for (raw,) in rows]

    def has_paths(self) -> bool:
        """Whether any CV has a stored path."""
        return self._one("SELECT 1 FROM path LIMIT 1") is not None

    def path_model(self, cv_id: int, role_family: str, lang: str) -> str | None:
        """The model that made the stored path of a family in lang; None without such a path."""
        row = self._one("SELECT model FROM path WHERE cv_id = ? AND role_family = ? AND language = ?",
                        (cv_id, role_family, lang))
        return row[0] if row else None

    def path_created(self, cv_id: int, role_family: str, lang: str) -> str | None:
        """When the stored path of a family in lang was made (store timestamp, UTC); None without such a path."""
        row = self._one("SELECT created FROM path WHERE cv_id = ? AND role_family = ? AND language = ?",
                        (cv_id, role_family, lang))
        return row[0] if row else None

    # --- message batches (an interrupted run collects its batch instead of paying twice) ---

    def save_batch(self, batch_id: str, hashes: list[str]) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO batch (id, hashes, created) VALUES (?, ?, ?)",
                            (batch_id, json.dumps(hashes), _ts()))

    def pending_batches(self) -> list[tuple[str, list[str]]]:
        rows = self.db.execute("SELECT id, hashes FROM batch WHERE done = 0 ORDER BY rowid")
        return [(batch_id, json.loads(hashes)) for batch_id, hashes in rows]

    def finish_batch(self, batch_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE batch SET done = 1 WHERE id = ?", (batch_id,))

    # --- cost ledger ---

    def record(self, step: str, model: str, usage: Usage, cost_usd: float, batch: bool) -> None:
        with self.db:
            self._insert_cost(step, model, usage, cost_usd, batch)

    def _insert_cost(self, step: str, model: str, usage: Usage, cost_usd: float, batch: bool) -> None:
        """One ledger row, inside the transaction of the caller."""
        self.db.execute(
            "INSERT INTO cost (ts, step, model, input, output, cache_write, cache_read, usd, batch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_ts(), step, model, usage.input_tokens, usage.output_tokens, usage.cache_write_tokens,
             usage.cache_read_tokens, cost_usd, int(batch)))

    def month_total_usd(self, now: datetime | None = None) -> float:
        month = _now(now).strftime("%Y-%m")
        return self._one("SELECT COALESCE(SUM(usd), 0) FROM cost WHERE ts LIKE ?", (f"{month}%",))[0]

    def cost_summary(self, since: datetime | None = None) -> CostSummary:
        calls, usd, uncached, written, read = self._one(
            "SELECT COUNT(*), COALESCE(SUM(usd), 0), COALESCE(SUM(input), 0), COALESCE(SUM(cache_write), 0), "
            "COALESCE(SUM(cache_read), 0) FROM cost WHERE ts >= ?", (_ts(since) if since else "",))
        return CostSummary(calls=calls, usd=usd, cache_read_share=_cache_read_share(uncached, written, read))

    def cost_by_step(self, since: datetime | None = None) -> list[StepCost]:
        rows = self.db.execute(
            "SELECT step, COUNT(*), COALESCE(SUM(usd), 0), COALESCE(SUM(input), 0), COALESCE(SUM(cache_write), 0), "
            "COALESCE(SUM(cache_read), 0) FROM cost WHERE ts >= ? GROUP BY step ORDER BY step",
            (_ts(since) if since else "",))
        return [StepCost(step=step, calls=calls, usd=usd, cache_read_share=_cache_read_share(uncached, written, read))
                for step, calls, usd, uncached, written, read in rows]

    def cost_means(self) -> list[StepMean]:
        """Mean cost per call over all recorded calls (not one month), by step and then single call before batch."""
        rows = self.db.execute("SELECT step, batch, COUNT(*), AVG(usd) FROM cost GROUP BY step, batch "
                               "ORDER BY step, batch")
        return [StepMean(step=step, batch=bool(batch), calls=calls, mean_usd=mean) for step, batch, calls, mean in rows]

    def _cost_rows(self, order: str, limit: int = -1) -> list[CostRow]:
        rows = self.db.execute("SELECT ts, step, model, input, output, cache_write, cache_read, usd, batch "
                               f"FROM cost ORDER BY id {order} LIMIT ?", (limit,))
        return [CostRow(ts=ts, step=step, model=model, input=i, output=o, cache_write=w, cache_read=r, usd=usd,
                        batch=bool(batch)) for ts, step, model, i, o, w, r, usd, batch in rows]

    def recent_costs(self, limit: int = 20) -> list[CostRow]:
        """The latest calls, newest first."""
        return self._cost_rows("DESC", limit)

    def cost_rows(self) -> list[CostRow]:
        """The whole ledger, oldest first."""
        return self._cost_rows("ASC")

    def import_costs(self, rows: list[CostRow]) -> None:
        """Add ledger rows as they are, with their own timestamps (a demo package brings the costs of its build)."""
        with self.db:
            self.db.executemany(
                "INSERT INTO cost (ts, step, model, input, output, cache_write, cache_read, usd, batch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(r.ts, r.step, r.model, r.input, r.output, r.cache_write, r.cache_read, r.usd, int(r.batch))
                 for r in rows])


# The runs of `skillpose eval --repeat`: only data/stability.sqlite has these tables (StabilityStore). A run is one
# analysis of an input: of one CV (one result), or of the postings of the gold set (one result per posting).
STABILITY_TABLES = """
CREATE TABLE IF NOT EXISTS stability_run (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, config TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stability_result (
  run_id INTEGER NOT NULL REFERENCES stability_run(id), hash TEXT NOT NULL, model TEXT NOT NULL, json TEXT NOT NULL,
  PRIMARY KEY (run_id, hash));
CREATE TABLE IF NOT EXISTS stability_batch (
  id TEXT PRIMARY KEY REFERENCES batch(id), config TEXT NOT NULL);
"""


@dataclass(frozen=True)
class PendingBatch:
    id: str
    hashes: list[str]
    config: str | None   # the configuration of the run the batch is; None: unknown (not sent by eval --repeat)


@dataclass(frozen=True)
class BatchCost:
    """A result of a batch, booked only together with its run (StabilityStore.save_batch_run)."""
    step: str
    model: str
    usage: Usage
    usd: float


class StoredRun(BaseModel):
    id: int
    kind: Literal["cv", "job"]
    config: str          # the prompt set, model, effort and language the run asked for
    created: str         # store timestamp, UTC
    results: dict[str, tuple[str, str]]  # text hash -> (the model that answered, JSON of the profile or job profile)


class StabilityStore(Store):
    """The store of `skillpose eval --repeat`: a Store (cost ledger, relations, batches) and every repeated run. The
    gold-set store and the own database never get the run tables."""

    def __init__(self, path: Path):
        super().__init__(path)
        try:
            self.db.executescript(STABILITY_TABLES)
        except BaseException:
            self.db.close()
            raise

    def save_run(self, kind: Literal["cv", "job"], config: str, results: dict[str, tuple[str, BaseModel]]) -> int:
        """Store one run: results maps the text hash of each input to (the model that answered, its result)."""
        with self.db:
            return self._insert_run(kind, config, results)

    def _insert_run(self, kind: str, config: str, results: dict[str, tuple[str, BaseModel]]) -> int:
        run_id = self.db.execute("INSERT INTO stability_run (kind, config, created) VALUES (?, ?, ?)",
                                 (kind, config, _ts())).lastrowid
        self.db.executemany("INSERT INTO stability_result (run_id, hash, model, json) VALUES (?, ?, ?, ?)",
                            [(run_id, h, model, value.model_dump_json()) for h, (model, value) in results.items()])
        return run_id

    def save_pending_batch(self, batch_id: str, hashes: list[str], config: str) -> None:
        """A submitted batch with the configuration of the run it is: an interrupted command collects it later."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO batch (id, hashes, created) VALUES (?, ?, ?)",
                            (batch_id, json.dumps(hashes), _ts()))
            self.db.execute("INSERT OR REPLACE INTO stability_batch (id, config) VALUES (?, ?)", (batch_id, config))

    def pending(self) -> list[PendingBatch]:
        """The batches not collected yet, oldest first, each with its configuration."""
        rows = self.db.execute("SELECT b.id, b.hashes, s.config FROM batch b "
                               "LEFT JOIN stability_batch s ON s.id = b.id WHERE b.done = 0 ORDER BY b.rowid")
        return [PendingBatch(batch_id, json.loads(hashes), config) for batch_id, hashes, config in rows]

    def save_batch_run(self, batch_id: str, config: str, results: dict[str, tuple[str, BaseModel]],
                       costs: list[BatchCost]) -> int | None:
        """The results of a collected batch in one transaction: their costs, the run (none without a result) and the
        batch marked as done. A crash before it leaves the batch open and nothing booked; after it, nothing is open:
        the batch is never booked or stored twice. Returns the id of the run."""
        with self.db:
            for cost in costs:
                self._insert_cost(cost.step, cost.model, cost.usage, cost.usd, True)
            run_id = self._insert_run("job", config, results) if results else None
            self.db.execute("UPDATE batch SET done = 1 WHERE id = ?", (batch_id,))
        return run_id

    def runs(self, kind: Literal["cv", "job"] | None = None, config: str | None = None) -> list[StoredRun]:
        """The stored runs, oldest first; kind and config narrow them down."""
        rows = self.db.execute(
            "SELECT r.id, r.kind, r.config, r.created, s.hash, s.model, s.json FROM stability_run r "
            "LEFT JOIN stability_result s ON s.run_id = r.id WHERE (? IS NULL OR r.kind = ?) "
            "AND (? IS NULL OR r.config = ?) ORDER BY r.id, s.rowid", (kind, kind, config, config))
        runs: dict[int, StoredRun] = {}
        for run_id, run_kind, run_config, created, h, model, raw in rows:
            run = runs.setdefault(run_id, StoredRun(id=run_id, kind=run_kind, config=run_config, created=created,
                                                    results={}))
            if h is not None:
                run.results[h] = (model, raw)
        return list(runs.values())
