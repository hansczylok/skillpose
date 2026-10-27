import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from skillpose.core.llm import Usage
from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    Conditions,
    DevelopmentPath,
    Evidence,
    JobMatch,
    JobProfile,
    PathStep,
    Skill,
)
from skillpose.core.vocabulary import Vocabulary
from skillpose.store.db import CostRow, StepMean, Store

# The SCHEMA string of store/db.py at the end of phase 1, copied verbatim: a database that phase 1 wrote.
PHASE1_SCHEMA = """
CREATE TABLE IF NOT EXISTS cv (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profile (
  cv_id INTEGER PRIMARY KEY REFERENCES cv(id), json TEXT NOT NULL, model TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS posting (
  hash TEXT PRIMARY KEY, text TEXT NOT NULL, refs TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_profile (
  hash TEXT PRIMARY KEY REFERENCES posting(hash), json TEXT NOT NULL, model TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS match (
  cv_id INTEGER NOT NULL REFERENCES cv(id), hash TEXT NOT NULL, json TEXT NOT NULL, PRIMARY KEY (cv_id, hash));
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
"""

# What version 2 (an early state of phase 2) added to it: posting.error and one path per CV and role family.
VERSION2_ADDITIONS = """
ALTER TABLE posting ADD COLUMN error TEXT;
CREATE TABLE IF NOT EXISTS path (
  cv_id INTEGER NOT NULL REFERENCES cv(id), role_family TEXT NOT NULL, json TEXT NOT NULL, model TEXT NOT NULL,
  created TEXT NOT NULL, PRIMARY KEY (cv_id, role_family));
PRAGMA user_version = 2;
"""


def test_duplicate_posting_stored_once(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    h1 = st.save_posting("Python developer\n(m/w/d)", ref="a.md")
    h2 = st.save_posting("Python  developer (m/w/d)", ref="b.md")
    assert h1 == h2 and st.open_postings() == [h1]


def test_open_postings_oldest_first(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    hashes = [st.save_posting(f"Job number {i}") for i in range(3)]
    assert st.open_postings() == hashes
    assert st.posting_text(hashes[1]) == "Job number 1"


def test_job_profile_roundtrip_closes_posting(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    h = st.save_posting("Job text " * 20)
    st.save_job_profile(JobProfile(text_hash=h, title="QA", role_family="test_automation", seniority="mid",
                                   requirements=[]), "claude-haiku-5-5")
    assert st.job_profile(h).title == "QA" and st.open_postings() == []


def test_cv_profile_and_match_roundtrip(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id = st.save_cv("maria.md", "CV text")
    assert st.cv_text(cv_id) == "CV text" and st.profile(cv_id) is None
    st.save_profile(cv_id, CandidateProfile(language="de", skills=[], stations=[]), "claude-opus-5-5")
    assert st.profile(cv_id).language == "de"
    jm = JobMatch(text_hash="abc", matches=[], must_coverage=1.0, nice_coverage=1.0, tier=1)
    st.save_match(cv_id, jm)
    st.save_match(cv_id, jm.model_copy(update={"tier": 2}))
    assert [m.tier for m in st.matches(cv_id)] == [2]


def test_month_total_and_cache_share(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.record("job", "claude-haiku-5-5", Usage(1000, 500, 0, 3000), 0.01, False)
    st.record("cv", "claude-opus-5-5", Usage(1000, 500, 4000, 0), 0.05, False)
    assert st.month_total_usd() == pytest.approx(0.06)
    assert st.month_total_usd(now=datetime(2020, 1, 15, tzinfo=UTC)) == 0.0
    summary = st.cost_summary()
    assert summary.calls == 2 and summary.usd == pytest.approx(0.06)
    assert summary.cache_read_share == pytest.approx(3000 / 9000)


def test_batches_are_remembered_until_finished(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.save_batch("batch_1", ["h1", "h2"])
    assert st.pending_batches() == [("batch_1", ["h1", "h2"])]
    st.finish_batch("batch_1")
    assert st.pending_batches() == []


def test_relations_and_added_skills(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.save_relations("uipath", {"power_automate": "close", "excel": "none"})
    assert st.relations("uipath") == {"power_automate": "close", "excel": "none"}
    st.add_skills([Skill(id="n8n", name_en="n8n", name_de="n8n", category="tool")])
    assert [s.id for s in st.added_skills(reviewed=False)] == ["n8n"]
    st.accept_skills(["n8n"])
    assert st.added_skills(reviewed=False) == [] and [s.id for s in st.added_skills()] == ["n8n"]


# --- schema version 2 ---

def _evidence() -> Evidence:
    return Evidence(quote="Wrote Python scripts", context="Acme", period_from=2020, period_to=None, strength="activity")


def _skill(skill_id: str, level="working") -> CandidateSkill:
    return CandidateSkill(skill_id=skill_id, level=level, years=2.0, evidence=[_evidence()])


def _profile(*skill_ids: str) -> CandidateProfile:
    return CandidateProfile(language="en", skills=[_skill(s) for s in skill_ids], stations=[])


def _job(h: str, title="QA", role_family="test_automation") -> JobProfile:
    return JobProfile(text_hash=h, title=title, role_family=role_family, seniority="mid", requirements=[])


def _path(role_family="test_automation", weeks=6) -> DevelopmentPath:
    step = PathStep(activity="Build a small Playwright suite", closes=["playwright"], effort_weeks=weeks,
                    proof="A public repository with CI")
    return DevelopmentPath(role_family=role_family, language="en", rationale="Playwright is the biggest gap.",
                           steps=[step], total_weeks=weeks, input_hash="abc123")


def _phase1_database(path):
    """A phase 1 database with a little data in every table that a phase 2 store must keep."""
    con = sqlite3.connect(path)
    con.executescript(PHASE1_SCHEMA)
    skill = Skill(id="n8n", name_en="n8n", name_de="n8n", category="tool")
    con.execute("INSERT INTO cv (name, text, created) VALUES ('maria.md', 'CV text', '2026-10-20T09:00:00')")
    con.execute("INSERT INTO profile VALUES (1, ?, 'claude-opus-5-5', '2026-10-20T09:01:00')",
                (_profile("python").model_dump_json(),))
    con.execute("INSERT INTO posting (hash, text, refs, created) VALUES ('h1', 'Job', ?, '2026-10-20T10:00:00')",
                ('["a.md"]',))
    con.execute("INSERT INTO posting (hash, text, refs, created) VALUES ('h2', 'Job 2', '[]', '2026-10-20T10:01:00')")
    con.execute("INSERT INTO job_profile VALUES ('h2', ?, 'claude-haiku-5-5', '2026-10-20T10:02:00')",
                (_job("h2").model_dump_json(),))
    jm = JobMatch(text_hash="h2", matches=[], must_coverage=0.5, nice_coverage=1.0, tier=2)
    con.execute("INSERT INTO match VALUES (1, 'h2', ?)", (jm.model_dump_json(),))
    con.execute("INSERT INTO skill_added (id, json) VALUES ('n8n', ?)", (skill.model_dump_json(),))
    con.execute("INSERT INTO relation VALUES ('uipath', 'power_automate', 'close')")
    con.execute("INSERT INTO batch (id, hashes, created) VALUES ('batch_1', ?, '2026-10-20T10:03:00')", ('["h1"]',))
    con.execute("INSERT INTO cost (ts, step, model, input, output, cache_write, cache_read, usd, batch) "
                "VALUES ('2026-10-20T10:04:00', 'job', 'claude-haiku-5-5', 10, 20, 30, 40, 0.5, 1)")
    con.commit()
    con.close()


def test_phase1_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript(PHASE1_SCHEMA)
    con.execute("INSERT INTO posting (hash, text, refs, created) VALUES ('h1', 'Job', '[]', '2026-10-20T10:00:00')")
    con.commit()
    con.close()
    st = Store(path)
    assert st.db.execute("PRAGMA user_version").fetchone()[0] == 6
    st.set_posting_error("h1", "refusal")
    assert st.postings()[0].error == "refusal" and st.paths(1) == {}
    assert [row[1] for row in st.db.execute("PRAGMA table_info(path)")] == PATH_COLUMNS


def test_phase1_database_loses_nothing(tmp_path):
    path = tmp_path / "old.sqlite"
    _phase1_database(path)
    st = Store(path)
    assert [(c.id, c.name, c.analyzed) for c in st.cvs()] == [(1, "maria.md", True)]
    assert st.cv_text(1) == "CV text" and [s.skill_id for s in st.profile(1).skills] == ["python"]
    assert st.posting_text("h1") == "Job" and st.open_postings() == ["h1"]
    assert st.job_profile("h2").title == "QA" and [m.tier for m in st.matches(1)] == [2]
    assert [s.id for s in st.added_skills(reviewed=False)] == ["n8n"]
    assert st.relations("uipath") == {"power_automate": "close"}
    assert st.pending_batches() == [("batch_1", ["h1"])]
    assert [(r.step, r.cache_read, r.batch) for r in st.cost_rows()] == [("job", 40, True)]
    assert [(p.hash, p.refs, p.status, p.error) for p in st.postings()] == [
        ("h1", ["a.md"], "batch", None), ("h2", [], "analyzed", None)]


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite"
    _phase1_database(path)
    Store(path).close()
    st = Store(path)
    st.set_posting_error("h1", "refusal")
    st.close()
    st = Store(path)
    columns = [row[1] for row in st.db.execute("PRAGMA table_info(posting)")]
    assert columns.count("error") == 1 and st.db.execute("PRAGMA user_version").fetchone()[0] == 6
    assert st.postings()[0].error == "refusal" and len(st.cost_rows()) == 1


PATH_COLUMNS = ["cv_id", "role_family", "language", "json", "model", "created"]


def _path_key(st: Store) -> list[str]:
    """The primary key columns of the path table, in key order."""
    return [name for pk, name in sorted((row[5], row[1]) for row in st.db.execute("PRAGMA table_info(path)")) if pk]


def test_fresh_database_has_version_6(tmp_path):
    st = Store(tmp_path / "new.sqlite")
    assert st.db.execute("PRAGMA user_version").fetchone()[0] == 6
    assert [row[1] for row in st.db.execute("PRAGMA table_info(path)")] == PATH_COLUMNS
    assert _path_key(st) == ["cv_id", "role_family", "language"]


# --- schema version 3: a path per language ---

def _version2_database(path):
    """A version 2 database: the phase 1 data, a posting error and three paths (two CVs, German and English)."""
    _phase1_database(path)
    con = sqlite3.connect(path)
    con.executescript(VERSION2_ADDITIONS)
    con.execute("INSERT INTO cv (name, text, created) VALUES ('daniel.md', 'CV text 2', '2026-10-20T09:02:00')")
    con.execute("UPDATE posting SET error = 'refusal' WHERE hash = 'h1'")
    rows = [(1, "test_automation", _path("test_automation").model_copy(update={"language": "de"}), "claude-opus-5-5",
             "2026-10-21T08:00:00"),
            (1, "rpa", _path("rpa", weeks=9), "claude-sonnet-5-5", "2026-10-21T08:01:00"),
            (2, "rpa", _path("rpa", weeks=3), "claude-opus-5-5", "2026-10-21T08:02:00")]
    con.executemany("INSERT INTO path VALUES (?, ?, ?, ?, ?)",
                    [(cv_id, family, p.model_dump_json(), model, created) for cv_id, family, p, model, created in rows])
    con.commit()
    con.close()
    return {(cv_id, family, p.language): (p, model, created) for cv_id, family, p, model, created in rows}


def _stored_paths(st: Store) -> dict:
    rows = st.db.execute("SELECT cv_id, role_family, language, json, model, created FROM path")
    return {(cv_id, family, lang): (DevelopmentPath.model_validate_json(raw), model, created)
            for cv_id, family, lang, raw, model, created in rows}


def test_version2_database_keeps_every_path_with_its_language(tmp_path):
    path = tmp_path / "v2.sqlite"
    before = _version2_database(path)
    st = Store(path)
    assert st.db.execute("PRAGMA user_version").fetchone()[0] == 6
    assert [row[1] for row in st.db.execute("PRAGMA table_info(path)")] == PATH_COLUMNS
    assert _path_key(st) == ["cv_id", "role_family", "language"]
    assert _stored_paths(st) == before  # json, model and time of each path, the language read from its json
    assert st.paths(1, "de")["test_automation"].language == "de" and st.paths(1, "en")["rpa"].total_weeks == 9
    assert st.path_created(1, "test_automation", "de") == "2026-10-21T08:00:00"
    assert st.path_model(1, "rpa", "en") == "claude-sonnet-5-5" and st.paths(2)["rpa"].total_weeks == 3
    assert st.postings()[0].error == "refusal" and [s.skill_id for s in st.profile(1).skills] == ["python"]
    assert [(c.id, c.name) for c in st.cvs()] == [(2, "daniel.md"), (1, "maria.md")] and len(st.cost_rows()) == 1
    assert st.db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    tables = {row[0] for row in st.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "path" in tables and not [t for t in tables if t.startswith("path") and t != "path"]


def test_version2_migration_is_idempotent(tmp_path):
    path = tmp_path / "v2.sqlite"
    before = _version2_database(path)
    Store(path).close()
    with Store(path) as st:
        assert st.db.execute("PRAGMA user_version").fetchone()[0] == 6 and _stored_paths(st) == before
        st.save_path(1, _path("test_automation"), "claude-opus-5-5")  # the English one beside the German one
    with Store(path) as st:
        assert len(_stored_paths(st)) == 4 and _stored_paths(st)[(1, "test_automation", "de")] == before[
            (1, "test_automation", "de")]


def test_version2_migration_is_one_transaction(tmp_path):
    path = tmp_path / "v2.sqlite"
    _version2_database(path)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO path VALUES (2, 'test_automation', 'not json', 'claude-opus-5-5', '2026-10-21T08:03:00')")
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        Store(path)
    con = sqlite3.connect(path)  # nothing changed: the old table with all four rows, still version 2
    assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    assert [row[1] for row in con.execute("PRAGMA table_info(path)")] == ["cv_id", "role_family", "json", "model",
                                                                           "created"]
    assert con.execute("SELECT COUNT(*) FROM path").fetchone()[0] == 4
    assert [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE name LIKE 'path%'")] == ["path"]
    assert con.execute("SELECT name FROM sqlite_master WHERE name = 'posting_translation'").fetchone() is None
    con.close()


# --- schema version 4: translations of postings (the demo shows each posting in both languages) ---

TRANSLATION_COLUMNS = ["hash", "language", "title", "text", "model", "created"]

# What version 3 (a later state of phase 2) added to the phase 1 layout: posting.error and a path per language.
VERSION3_ADDITIONS = """
ALTER TABLE posting ADD COLUMN error TEXT;
CREATE TABLE IF NOT EXISTS path (
  cv_id INTEGER NOT NULL REFERENCES cv(id), role_family TEXT NOT NULL, language TEXT NOT NULL, json TEXT NOT NULL,
  model TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY (cv_id, role_family, language));
PRAGMA user_version = 3;
"""


def _version3_database(path):
    """A version 3 database: the phase 1 data, a posting error and a German path."""
    _phase1_database(path)
    con = sqlite3.connect(path)
    con.executescript(VERSION3_ADDITIONS)
    con.execute("UPDATE posting SET error = 'refusal' WHERE hash = 'h1'")
    german = _path("test_automation").model_copy(update={"language": "de"})
    con.execute("INSERT INTO path VALUES (1, 'test_automation', 'de', ?, 'claude-opus-5-5', '2026-10-21T08:00:00')",
                (german.model_dump_json(),))
    con.commit()
    con.close()


def _translation_key(st: Store) -> list[str]:
    rows = st.db.execute("PRAGMA table_info(posting_translation)")
    return [name for pk, name in sorted((row[5], row[1]) for row in rows) if pk]


def test_fresh_database_has_the_translation_table(tmp_path):
    with Store(tmp_path / "new.sqlite") as st:
        assert [row[1] for row in st.db.execute("PRAGMA table_info(posting_translation)")] == TRANSLATION_COLUMNS
        assert _translation_key(st) == ["hash", "language"]


def test_version3_database_gets_the_translation_table_and_loses_nothing(tmp_path):
    path = tmp_path / "v3.sqlite"
    _version3_database(path)
    with Store(path) as st:
        assert st.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert [row[1] for row in st.db.execute("PRAGMA table_info(posting_translation)")] == TRANSLATION_COLUMNS
        assert _translation_key(st) == ["hash", "language"] and st.translations("h2") == {}
        assert st.paths(1)["test_automation"].language == "de" and st.postings()[0].error == "refusal"
        assert st.job_profile("h2").title == "QA" and [m.tier for m in st.matches(1)] == [2]
        assert st.pending_batches() == [("batch_1", ["h1"])] and len(st.cost_rows()) == 1
        st.save_translation("h2", "de", "QS", "QS-Text", "claude-code")
    with Store(path) as st:  # opening it again changes nothing
        assert st.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert st.translations("h2") == {"de": ("QS", "QS-Text")} and st.paths(1)["test_automation"].language == "de"


def test_translations_roundtrip(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        h = st.save_posting("Testautomatisierer (m/w/d)\nSelenium und Java", "a.md")
        assert st.translation(h, "en") is None and st.translations(h) == {}
        st.save_translation(h, "en", "Test automation engineer", "Test automation engineer\nSelenium and Java", "m1")
        assert st.translation(h, "en") == ("Test automation engineer", "Test automation engineer\nSelenium and Java")
        assert st.translation(h, "de") is None and st.translation("nosuchhash", "en") is None
        st.save_translation(h, "en", "Tester", "Tester\nSelenium and Java", "m2")  # a new one replaces the old one
        st.save_translation(h, "de", "Tester", "Tester (Deutsch)", "m2")
        assert st.translations(h) == {"de": ("Tester", "Tester (Deutsch)"),
                                      "en": ("Tester", "Tester\nSelenium and Java")}
        assert list(st.translations(h)) == ["de", "en"]  # sorted by language
        st.set_translations(h, {"en": ("Only", "Only English")}, "claude-code")  # exactly these, the others go
        assert st.translations(h) == {"en": ("Only", "Only English")}
        assert st.db.execute("SELECT model FROM posting_translation").fetchall() == [("claude-code",)]
        st.set_translations(h, {}, "claude-code")
        assert st.translations(h) == {}


def test_a_translation_needs_its_posting(tmp_path):
    with Store(tmp_path / "s.sqlite") as st, pytest.raises(sqlite3.IntegrityError):
        st.save_translation("nosuchhash", "en", "Title", "Text", "m")


def test_store_is_usable_from_another_thread(tmp_path):
    st, errors = Store(tmp_path / "s.sqlite"), []
    t = threading.Thread(target=lambda: errors.append(None) if st.cvs() == [] else None)
    t.start()
    t.join()
    assert errors == [None]


def test_close_and_context_manager(tmp_path):
    path = tmp_path / "s.sqlite"
    with Store(path) as st:
        cv_id = st.save_cv("maria.md", "CV text")
    with pytest.raises(sqlite3.ProgrammingError):
        st.db.execute("SELECT 1")
    with pytest.raises(RuntimeError), Store(path) as again:
        raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        again.db.execute("SELECT 1")
    reopened = Store(path)
    assert [c.id for c in reopened.cvs()] == [cv_id]
    reopened.close()
    with pytest.raises(sqlite3.ProgrammingError):
        reopened.db.execute("SELECT 1")


def test_cvs_newest_first_with_analyzed_flag(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    first, second = st.save_cv("a.md", "CV a"), st.save_cv("b.md", "CV b")
    st.save_profile(first, _profile("python"), "claude-opus-5-5")
    rows = st.cvs()
    assert [(r.id, r.name, r.analyzed) for r in rows] == [(second, "b.md", False), (first, "a.md", True)]
    assert rows[0].created == st.db.execute("SELECT created FROM cv WHERE id = ?", (second,)).fetchone()[0]


def test_edit_skill_and_visible_profile(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id = st.save_cv("maria.md", "CV text")
    st.save_profile(cv_id, _profile("python", "sql"), "claude-opus-5-5")
    st.edit_skill(cv_id, "python", hidden=True)
    st.edit_skill(cv_id, "sql", level="expert")
    p = st.profile(cv_id)
    assert [s.skill_id for s in p.visible().skills] == ["sql"] and p.visible().skills[0].level == "expert"
    assert [s.skill_id for s in p.skills] == ["python", "sql"] and p.skills[0].hidden
    with pytest.raises(KeyError):
        st.edit_skill(cv_id, "cobol", hidden=True)
    st.edit_skill(cv_id, "python", hidden=False)
    assert [s.skill_id for s in st.profile(cv_id).visible().skills] == ["python", "sql"]


def test_edit_skill_changes_only_what_it_is_given(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id = st.save_cv("maria.md", "CV text")
    st.save_profile(cv_id, _profile("python"), "claude-opus-5-5")
    before = st.db.execute("SELECT model, created FROM profile WHERE cv_id = ?", (cv_id,)).fetchone()
    st.edit_skill(cv_id, "python", level="strong")
    skill = st.profile(cv_id).skills[0]
    assert skill.level == "strong" and not skill.hidden and skill.years == 2.0 and skill.evidence == [_evidence()]
    assert st.db.execute("SELECT model, created FROM profile WHERE cv_id = ?", (cv_id,)).fetchone() == before


def test_edit_skill_rejects_bad_input_and_keeps_the_profile(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id = st.save_cv("maria.md", "CV text")
    with pytest.raises(ValueError, match="no profile"):
        st.edit_skill(cv_id, "python", hidden=True)
    st.save_profile(cv_id, _profile("python"), "claude-opus-5-5")
    with pytest.raises(ValueError, match="level"):
        st.edit_skill(cv_id, "python", level="guru")
    assert st.profile(cv_id).skills[0].level == "working"


def test_delete_cv_keeps_postings(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    h = st.save_posting("Job text " * 20, "a.md")
    st.save_job_profile(_job(h), "claude-haiku-5-5")
    gone, kept = st.save_cv("a.md", "CV a"), st.save_cv("b.md", "CV b")
    for cv_id in (gone, kept):
        st.save_profile(cv_id, _profile("python"), "claude-opus-5-5")
        st.save_match(cv_id, JobMatch(text_hash=h, matches=[], must_coverage=1.0, nice_coverage=1.0, tier=1))
        st.save_path(cv_id, _path(), "claude-opus-5-5")
        st.save_path(cv_id, _de(_path()), "claude-opus-5-5")
    postings = st.postings()
    st.delete_cv(gone)
    assert [c.id for c in st.cvs()] == [kept]
    assert st.profile(gone) is None and st.matches(gone) == [] and st.paths_all(gone) == []  # every language
    assert st.profile(kept) is not None and len(st.matches(kept)) == 1 and len(st.paths_all(kept)) == 2
    assert st.postings() == postings and st.job_profile(h) is not None and st.posting_text(h)


def test_delete_cv_without_profile_or_unknown_cv(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id = st.save_cv("a.md", "CV a")
    st.delete_cv(cv_id)
    st.delete_cv(cv_id)
    assert st.cvs() == []


def test_cv_text_can_change_only_before_the_profile(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id = st.save_cv("a.md", "first text")
    st.update_cv_text(cv_id, "second text")
    assert st.cv_text(cv_id) == "second text" and st.find_cv("second text") == cv_id
    st.save_profile(cv_id, _profile("python"), "claude-opus-5-5")
    with pytest.raises(ValueError, match="profile"):
        st.update_cv_text(cv_id, "third text")
    assert st.cv_text(cv_id) == "second text"
    with pytest.raises(KeyError):
        st.update_cv_text(cv_id + 1, "text")


def test_postings_listing(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    h_open, h_done, h_batch = (st.save_posting(f"Job number {i}", f"{i}.md") for i in range(3))
    st.set_posting_error(h_open, "refusal")
    st.save_job_profile(_job(h_done), "claude-haiku-5-5")
    st.save_batch("batch_1", [h_batch])
    assert {r.hash: (r.status, r.error) for r in st.postings()} == {
        h_open: ("open", "refusal"), h_done: ("analyzed", None), h_batch: ("batch", None)}


def test_postings_listing_details_and_order(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    h_first = st.save_posting("Job number 0", "0.md")
    h_second = st.save_posting("Job number 1")
    st.save_job_profile(_job(h_second, title="Tester", role_family="rpa"), "claude-haiku-5-5")
    st.save_posting("Job number 0", "copy.md")
    first, second = st.postings()
    assert (first.hash, first.refs, first.title, first.role_family) == (h_first, ["0.md", "copy.md"], None, None)
    assert (second.hash, second.refs, second.title, second.role_family) == (h_second, [], "Tester", "rpa")
    assert first.created and second.created


def test_finished_batch_no_longer_marks_the_posting(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    h = st.save_posting("Job number 0")
    st.save_batch("batch_1", [h])
    assert st.postings()[0].status == "batch"
    st.finish_batch("batch_1")
    assert st.postings()[0].status == "open"


def test_job_profile_clears_the_posting_error(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    h = st.save_posting("Job number 0")
    st.set_posting_error(h, "refusal")
    assert st.postings()[0].error == "refusal"
    st.save_job_profile(_job(h), "claude-haiku-5-5")
    assert st.postings()[0].error is None and st.open_postings() == []


def test_rejected_skills_leave_the_snapshot(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.add_skills([Skill(id="n8n", name_en="n8n", name_de="n8n", category="tool")])
    st.reject_skills(["n8n"])
    assert st.added_skills() == [] and Vocabulary.load(extra=st.added_skills()).resolve("n8n") is None


def test_added_skills_by_review_state(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.add_skills([Skill(id=i, name_en=i, name_de=i, category="tool") for i in ("n8n", "zapier", "make")])
    st.accept_skills(["zapier"])
    st.reject_skills(["make"])
    assert [s.id for s in st.added_skills()] == ["n8n", "zapier"]
    assert [s.id for s in st.added_skills(reviewed=False)] == ["n8n"]
    assert [s.id for s in st.added_skills(reviewed=True)] == ["zapier"]
    st.add_skills([Skill(id="make", name_en="make", name_de="make", category="tool")])  # proposed again
    assert [s.id for s in st.added_skills()] == ["n8n", "zapier"]


def test_paths_roundtrip(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id, other = st.save_cv("a.md", "CV a"), st.save_cv("b.md", "CV b")
    assert st.paths(cv_id) == {}
    st.save_path(cv_id, _path("test_automation"), "claude-opus-5-5")
    st.save_path(cv_id, _path("rpa", weeks=9), "claude-opus-5-5")
    st.save_path(other, _path("rpa", weeks=3), "claude-opus-5-5")
    paths = st.paths(cv_id)
    assert list(paths) == ["rpa", "test_automation"]
    assert paths["rpa"] == _path("rpa", weeks=9) and paths["test_automation"] == _path("test_automation")
    st.save_path(cv_id, _path("rpa", weeks=12), "claude-opus-5-5")  # a new path for the same family replaces it
    assert st.paths(cv_id)["rpa"].total_weeks == 12 and len(st.paths(cv_id)) == 2
    assert st.paths(other)["rpa"].total_weeks == 3


def _de(path: DevelopmentPath, **update) -> DevelopmentPath:
    return path.model_copy(update={"language": "de", "rationale": "Playwright ist die größte Lücke.", **update})


def test_a_path_is_stored_per_language(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id = st.save_cv("a.md", "CV a")
    st.save_path(cv_id, _path("rpa", weeks=9), "claude-opus-5-5")
    st.save_path(cv_id, _de(_path("rpa", weeks=7)), "claude-sonnet-5-5")  # the German one does not replace it
    st.save_path(cv_id, _de(_path("test_automation")), "claude-opus-5-5")  # only in German
    assert st.paths(cv_id, "en")["rpa"].total_weeks == 9 and st.paths(cv_id, "de")["rpa"].total_weeks == 7
    assert st.paths(cv_id, "en")["test_automation"].language == "de"  # no English one: the German one
    assert {f: p.language for f, p in st.paths(cv_id, "de").items()} == {"rpa": "de", "test_automation": "de"}
    assert st.paths(cv_id) == st.paths(cv_id)  # without a language: one path per family, always the same one
    assert [(p.role_family, p.language) for p in st.paths_all(cv_id)] == [
        ("rpa", "de"), ("rpa", "en"), ("test_automation", "de")]
    assert st.path_model(cv_id, "rpa", "de") == "claude-sonnet-5-5" and st.path_model(cv_id, "rpa", "en") == (
        "claude-opus-5-5")
    assert st.path_model(cv_id, "test_automation", "en") is None and st.path_created(cv_id, "rpa", "fr") is None
    st.save_path(cv_id, _de(_path("rpa", weeks=2)), "claude-opus-5-5")  # replaces only the German one
    assert st.paths(cv_id, "de")["rpa"].total_weeks == 2 and st.paths(cv_id, "en")["rpa"].total_weeks == 9
    assert len(st.paths_all(cv_id)) == 3


def test_path_created_is_the_time_of_each_stored_path(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id, other = st.save_cv("a.md", "CV a"), st.save_cv("b.md", "CV b")
    assert st.path_created(cv_id, "test_automation", "en") is None
    st.save_path(cv_id, _path("test_automation"), "claude-opus-5-5")
    st.save_path(other, _path("rpa"), "claude-opus-5-5")
    created = st.path_created(cv_id, "test_automation", "en")
    assert created == st.db.execute(
        "SELECT created FROM path WHERE cv_id = ? AND role_family = ?", (cv_id, "test_automation")).fetchone()[0]
    assert st.path_created(cv_id, "rpa", "en") is None and st.path_created(cv_id, "test_automation", "de") is None
    st.db.execute("UPDATE path SET created = '2020-01-01T00:00:00' WHERE cv_id = ?", (cv_id,))
    st.save_path(cv_id, _de(_path("test_automation")), "claude-opus-5-5")  # another language: its own time
    assert st.path_created(cv_id, "test_automation", "en") == "2020-01-01T00:00:00"
    assert st.path_created(cv_id, "test_automation", "de") != "2020-01-01T00:00:00"
    st.save_path(cv_id, _path("test_automation", weeks=9), "claude-opus-5-5")  # a new path has its own time
    assert st.path_created(cv_id, "test_automation", "en") != "2020-01-01T00:00:00"


def test_cost_by_step_recent_and_import(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.record("job", "claude-haiku-5-5", Usage(1000, 500, 0, 3000), 0.01, False)
    st.record("cv", "claude-opus-5-5", Usage(1000, 500, 4000, 0), 0.05, False)
    st.record("job", "claude-haiku-5-5", Usage(2000, 800, 0, 0), 0.02, True)
    by_step = st.cost_by_step()
    assert [s.step for s in by_step] == ["cv", "job"]
    cv, job = by_step
    assert cv.calls == 1 and cv.usd == pytest.approx(0.05) and cv.cache_read_share == 0.0
    assert job.calls == 2 and job.usd == pytest.approx(0.03) and job.cache_read_share == pytest.approx(3000 / 6000)
    assert st.cost_by_step(since=datetime.now(UTC) - timedelta(minutes=1)) == by_step
    assert st.cost_by_step(since=datetime.now(UTC) + timedelta(days=1)) == []
    assert [r.usd for r in st.recent_costs()] == [0.02, 0.05, 0.01]
    assert [r.usd for r in st.recent_costs(limit=2)] == [0.02, 0.05]
    rows = st.cost_rows()
    assert [(r.step, r.batch) for r in rows] == [("job", False), ("cv", False), ("job", True)]
    assert rows[0] == CostRow(ts=rows[0].ts, step="job", model="claude-haiku-5-5", input=1000, output=500,
                              cache_write=0, cache_read=3000, usd=0.01, batch=False)
    copy = Store(tmp_path / "copy.sqlite")
    copy.import_costs(rows)
    assert copy.cost_rows() == rows


def test_cost_by_step_uses_the_cost_summary_definition(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.record("job", "claude-haiku-5-5", Usage(1000, 500, 2000, 3000), 0.01, False)
    assert st.cost_by_step()[0].cache_read_share == st.cost_summary().cache_read_share == pytest.approx(3000 / 6000)
    assert Store(tmp_path / "empty.sqlite").cost_by_step() == []


def test_import_costs_keeps_the_timestamps(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    old = CostRow(ts="2020-01-15T10:00:00", step="cv", model="claude-opus-5-5", input=1, output=2, cache_write=3,
                  cache_read=4, usd=0.25, batch=True)
    st.import_costs([old])
    assert st.cost_rows() == [old] and st.month_total_usd() == 0.0
    assert st.month_total_usd(now=datetime(2020, 1, 20, tzinfo=UTC)) == pytest.approx(0.25)


def test_profile_created_is_the_time_of_the_profile(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id = st.save_cv("maria.md", "CV text")
    assert st.profile_created(cv_id) is None and st.profile_created(99) is None
    st.save_profile(cv_id, _profile("python"), "claude-opus-5-5")
    created = st.profile_created(cv_id)
    assert created == st.db.execute("SELECT created FROM profile WHERE cv_id = ?", (cv_id,)).fetchone()[0]
    st.edit_skill(cv_id, "python", hidden=True)
    assert st.profile_created(cv_id) == created  # an edit does not make a new profile


# --- the models that wrote the stored results (the AI label names them) ---

def test_stored_model_names(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    cv_id, other = st.save_cv("a.md", "CV a"), st.save_cv("b.md", "CV b")
    h = st.save_posting("Job text " * 20, "a.md")
    assert st.profile_model(cv_id) is None and st.job_profile_model(h) is None
    assert st.path_model(cv_id, "rpa", "en") is None
    st.save_profile(cv_id, _profile("python"), "claude-opus-5-5")
    st.save_job_profile(_job(h), "claude-sonnet-5-5")
    st.save_path(cv_id, _path("test_automation"), "claude-opus-5-5")
    st.save_path(cv_id, _path("rpa"), "claude-haiku-5-5")
    st.save_path(other, _path("rpa"), "claude-sonnet-5-5")
    assert st.profile_model(cv_id) == "claude-opus-5-5" and st.profile_model(other) is None
    assert st.job_profile_model(h) == "claude-sonnet-5-5" and st.job_profile_model("nosuchhash") is None
    assert st.path_model(cv_id, "rpa", "en") == "claude-haiku-5-5"
    assert st.path_model(cv_id, "test_automation", "en") == "claude-opus-5-5"
    assert st.path_model(other, "rpa", "en") == "claude-sonnet-5-5"
    st.edit_skill(cv_id, "python", hidden=True)
    assert st.profile_model(cv_id) == "claude-opus-5-5"  # an edit keeps the model


# --- a deleted CV leaves no text in the file (PRAGMA secure_delete) ---

def test_every_store_connection_overwrites_deleted_content(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    assert st.db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    st.close()
    assert Store(tmp_path / "s.sqlite").db.execute("PRAGMA secure_delete").fetchone()[0] == 1  # also a reopened file


def test_the_text_of_a_deleted_cv_is_not_left_in_the_file(tmp_path):
    path = tmp_path / "s.sqlite"
    marker = "Zyxwvu Qpon Marker 4711"
    cv_text = f"{marker} - QA Engineer. " + "Wrote tests for a long time. " * 40
    quote = f"{marker} quoted as evidence"
    evidence = Evidence(quote=quote, context="Acme", period_from=2020, period_to=None, strength="activity")
    skill = CandidateSkill(skill_id="python", level="working", years=2.0, evidence=[evidence])
    with Store(path) as st:
        cv_id = st.save_cv("a.md", cv_text)
        h = st.save_posting("Job text " * 20, "a.md")
        st.save_profile(cv_id, CandidateProfile(language="en", skills=[skill], stations=[]), "claude-opus-5-5")
        st.save_match(cv_id, JobMatch(text_hash=h, matches=[], must_coverage=1.0, nice_coverage=1.0, tier=1))
    assert marker.encode() in path.read_bytes()  # the marker is in the file before the delete
    with Store(path) as st:
        st.delete_cv(cv_id)
    assert marker.encode() not in path.read_bytes()


def test_cost_means_group_all_calls_by_step_and_mode(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    assert st.cost_means() == []
    st.record("job", "claude-sonnet-5-5", Usage(1000, 500, 0, 0), 0.010, False)
    st.record("job", "claude-sonnet-5-5", Usage(1000, 500, 0, 0), 0.020, False)
    st.record("job", "claude-sonnet-5-5", Usage(1000, 500, 0, 0), 0.006, True)
    st.record("cv", "claude-opus-5-5", Usage(1000, 500, 0, 0), 0.125, False)
    st.record("cv", "claude-opus-5-5", Usage(1000, 500, 0, 0), 0.075, True)
    st.import_costs([CostRow(ts="2020-01-01T00:00:00", step="job", model="claude-sonnet-5-5", input=1, output=1,
                             cache_write=0, cache_read=0, usd=0.003, batch=False)])  # an old month counts too
    means = st.cost_means()
    assert [(m.step, m.batch, m.calls) for m in means] == [("cv", False, 1), ("cv", True, 1), ("job", False, 3),
                                                           ("job", True, 1)]
    assert [m.mean_usd for m in means] == pytest.approx([0.125, 0.075, 0.011, 0.006])
    assert means[0] == StepMean(step="cv", batch=False, calls=1, mean_usd=0.125)


# --- schema version 5: each stored match carries the fingerprint of the rules that made it ---

MATCH_COLUMNS = ["cv_id", "hash", "json", "fingerprint"]

VERSION4_ADDITIONS = VERSION3_ADDITIONS.replace("user_version = 3", "user_version = 4") + """
CREATE TABLE IF NOT EXISTS posting_translation (
  hash TEXT NOT NULL REFERENCES posting(hash), language TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
  model TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY (hash, language));
"""


def _version4_database(path):
    """A schema-4 database: the phase 1 data with a match, a posting error, a German path and a translation."""
    _phase1_database(path)
    con = sqlite3.connect(path)
    con.executescript(VERSION4_ADDITIONS)
    con.execute("UPDATE posting SET error = 'refusal' WHERE hash = 'h1'")
    german = _path("test_automation").model_copy(update={"language": "de"})
    con.execute("INSERT INTO path VALUES (1, 'test_automation', 'de', ?, 'claude-opus-5-5', '2026-10-21T08:00:00')",
                (german.model_dump_json(),))
    con.execute("INSERT INTO posting_translation VALUES ('h2', 'de', 'QS', 'QS-Text', 'claude-code', "
                "'2026-10-22T08:00:00')")
    con.commit()
    con.close()


def _assert_old_data_kept(st: Store) -> None:
    assert [(c.id, c.name, c.analyzed) for c in st.cvs()] == [(1, "maria.md", True)]
    assert st.cv_text(1) == "CV text" and [s.skill_id for s in st.profile(1).skills] == ["python"]
    assert st.job_profile("h2").title == "QA" and [m.tier for m in st.matches(1)] == [2]
    assert st.relations("uipath") == {"power_automate": "close"} and len(st.cost_rows()) == 1
    assert st.pending_batches() == [("batch_1", ["h1"])] and [s.id for s in st.added_skills(reviewed=False)] == ["n8n"]


def test_a_fresh_database_has_the_fingerprint_column(tmp_path):
    with Store(tmp_path / "new.sqlite") as st:
        assert [row[1] for row in st.db.execute("PRAGMA table_info(match)")] == MATCH_COLUMNS


def test_a_phase1_database_gets_the_fingerprint_column_and_keeps_its_matches(tmp_path):
    path = tmp_path / "old.sqlite"
    _phase1_database(path)
    with Store(path) as st:
        assert st.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert [row[1] for row in st.db.execute("PRAGMA table_info(match)")] == MATCH_COLUMNS
        _assert_old_data_kept(st)
        assert st.postings()[0].error is None
        assert st.stale_matches("any") == {1: ["h2"]}  # made before there were fingerprints: out of date


def test_a_schema4_database_opens_and_keeps_its_data(tmp_path):
    path = tmp_path / "v4.sqlite"
    _version4_database(path)
    with Store(path) as st:
        assert st.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert [row[1] for row in st.db.execute("PRAGMA table_info(match)")] == MATCH_COLUMNS
        _assert_old_data_kept(st)
        assert st.postings()[0].error == "refusal" and st.paths(1)["test_automation"].language == "de"
        assert st.translation("h2", "de") == ("QS", "QS-Text")
        assert st.stale_matches("any") == {1: ["h2"]}
    with Store(path) as st:  # opening it again changes nothing
        assert st.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert [row[1] for row in st.db.execute("PRAGMA table_info(match)")] == MATCH_COLUMNS
        _assert_old_data_kept(st)


def test_save_match_stores_the_fingerprint_and_a_new_one_replaces_it(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        cv_id = st.save_cv("maria.md", "CV text")
        jm = JobMatch(text_hash="abc", matches=[], must_coverage=1.0, nice_coverage=1.0, tier=1)
        st.save_match(cv_id, jm, "fp-1")
        assert st.stale_matches("fp-1") == {} and st.stale_matches("fp-2") == {cv_id: ["abc"]}
        st.save_match(cv_id, jm.model_copy(update={"tier": 2}), "fp-2")
        assert st.stale_matches("fp-2") == {} and [m.tier for m in st.matches(cv_id)] == [2]
        st.save_match(cv_id, jm)  # without a fingerprint: of unknown origin, so out of date for every fingerprint
        assert st.stale_matches("fp-2") == {cv_id: ["abc"]}


def test_stale_matches_are_listed_per_cv_in_the_order_they_were_stored(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        first, second = st.save_cv("a.md", "A"), st.save_cv("b.md", "B")
        for cv_id, h, fp in [(first, "h3", "old"), (first, "h1", "new"), (second, "h2", "old"), (first, "h4", None)]:
            st.save_match(cv_id, JobMatch(text_hash=h, matches=[], must_coverage=1.0, nice_coverage=1.0, tier=1), fp)
        assert st.stale_matches("new") == {first: ["h3", "h4"], second: ["h2"]}
        assert st.stale_matches("old") == {first: ["h1", "h4"]}
        assert st.stale_matches("other") == {first: ["h3", "h1", "h4"], second: ["h2"]}


def test_a_read_only_store_of_version_4_names_the_free_command_that_opens_it_once(tmp_path):
    path = tmp_path / "v4.sqlite"
    _version4_database(path)
    with pytest.raises(ValueError, match="version 4") as refused:
        Store(path, read_only=True)
    message = str(refused.value)
    assert f'skillpose vocab --db "{path}"' in message and "no API call" in message
    assert "eval" not in message and "demo build" not in message  # a paid command is not the way to open it


def test_an_in_memory_copy_holds_the_data_and_never_changes_the_file(tmp_path):
    path = tmp_path / "s.sqlite"
    jm = JobMatch(text_hash="abc", matches=[], must_coverage=1.0, nice_coverage=1.0, tier=1)
    with Store(path) as st:
        st.save_match(st.save_cv("a.md", "CV text"), jm, "fp")
    before = path.read_bytes()
    with Store(path) as st, st.in_memory_copy() as copy:
        assert copy.cv_text(1) == "CV text" and copy.stale_matches("fp") == {}
        assert copy.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert [row[2] for row in copy.db.execute("PRAGMA database_list")] == [""]  # no file
        copy.save_match(1, jm.model_copy(update={"tier": 3}), "other")
        copy.save_cv("b.md", "more text")
        assert copy.stale_matches("fp") == {1: ["abc"]} and [c.id for c in copy.cvs()] == [2, 1]
        assert st.stale_matches("fp") == {} and [c.id for c in st.cvs()] == [1]  # the original has not changed
    assert path.read_bytes() == before


# --- schema version 6: the answers to the conditions of postings, one record per database ---

def _version5_database(path):
    """A schema-5 database: the schema-4 one, and its match carries the fingerprint of the rules that made it."""
    _version4_database(path)
    con = sqlite3.connect(path)
    con.executescript("ALTER TABLE match ADD COLUMN fingerprint TEXT; UPDATE match SET fingerprint = 'fp-5'; "
                      "PRAGMA user_version = 5;")
    con.commit()
    con.close()


ANSWERS = Conditions(driving_licence_b=True, travel_share_max=20, shift_work=False, commute_km_max=40,
                     remote_only=False, full_time=True, notes="ab März")


def test_a_fresh_database_has_no_answers_yet(tmp_path):
    with Store(tmp_path / "new.sqlite") as st:
        assert st.conditions() == Conditions()


def test_the_conditions_are_saved_once_per_database_and_a_new_save_replaces_them(tmp_path):
    path = tmp_path / "s.sqlite"
    with Store(path) as st:
        st.save_cv("a.md", "A")
        st.save_cv("b.md", "B")
        st.save_conditions(ANSWERS)
        assert st.conditions() == ANSWERS
    with Store(path) as st:
        assert st.conditions() == ANSWERS
        st.save_conditions(Conditions(shift_work=True))
        assert st.conditions() == Conditions(shift_work=True)
        assert st.db.execute("SELECT COUNT(*) FROM conditions").fetchone()[0] == 1
    with Store(path, read_only=True) as st:
        assert st.conditions() == Conditions(shift_work=True)


@pytest.mark.parametrize("make, version", [(_phase1_database, 0), (_version4_database, 4), (_version5_database, 5)])
def test_an_older_database_gets_the_conditions_and_keeps_its_data(tmp_path, make, version):
    path = tmp_path / "old.sqlite"
    make(path)
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == version
    con.close()
    with Store(path) as st:
        assert st.db.execute("PRAGMA user_version").fetchone()[0] == 6
        _assert_old_data_kept(st)
        assert st.conditions() == Conditions()
        st.save_conditions(ANSWERS)
    with Store(path) as st:  # opening it again changes nothing
        _assert_old_data_kept(st)
        assert st.conditions() == ANSWERS


def test_a_schema5_database_keeps_the_fingerprints_of_its_matches(tmp_path):
    path = tmp_path / "v5.sqlite"
    _version5_database(path)
    with Store(path) as st:
        assert st.stale_matches("fp-5") == {} and st.stale_matches("other") == {1: ["h2"]}
        assert st.translation("h2", "de") == ("QS", "QS-Text") and st.postings()[0].error == "refusal"
        assert st.paths(1)["test_automation"].language == "de"


def test_a_read_only_store_of_version_5_is_refused(tmp_path):
    path = tmp_path / "v5.sqlite"
    _version5_database(path)
    with pytest.raises(ValueError, match="version 5"):
        Store(path, read_only=True)


def test_an_in_memory_copy_has_the_conditions(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        st.save_conditions(ANSWERS)
        with st.in_memory_copy() as copy:
            assert copy.conditions() == ANSWERS
