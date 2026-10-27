"""Stored matches follow the rules: each match carries the fingerprint of the rules that made it, and
a match from other rules is recomputed, offline, when the web app starts with a key, before `skillpose export` reads
matches and before `skillpose analyze` prints the market view."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from demo_fixture import fixture_bundle, write_fixture_bundles
from fakes import CV, FailingClient, ScriptedClient, fail_if_called
from webkit import make_client

from skillpose import cli
from skillpose.cli import main
from skillpose.core import matching
from skillpose.core.config import Settings, load_settings
from skillpose.core.matching import match_fingerprint, needed_pairs
from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    Evidence,
    JobMatch,
    JobProfile,
    Requirement,
    RequirementMatch,
)
from skillpose.pipeline import attach_client, open_run, refresh_matches, rematch
from skillpose.store.db import Store

YEAR = datetime.now(UTC).year
SETTINGS = load_settings()
FINGERPRINT = match_fingerprint(SETTINGS.rules, YEAR)


def settings_with_rules(**changes) -> Settings:
    return SETTINGS.model_copy(update={"rules": SETTINGS.rules.model_copy(update=changes)})


def _skill(skill_id, level="strong", strength="outcome") -> CandidateSkill:
    ev = Evidence(quote=f"Used {skill_id}", context="Acme", period_from=2018, period_to=None, strength=strength,
                  verified=True)
    return CandidateSkill(skill_id=skill_id, level=level, years=6, last_used=YEAR, evidence=[ev])


def _requirement(skill_id, kind="tool") -> Requirement:
    return Requirement(skill_id=skill_id, importance="must", min_level=None, min_years=None, kind=kind, quote=skill_id)


def _out_of_date(jp: JobProfile) -> JobMatch:
    """What the rules before the degree ranking made of the posting: the degree is a gap, tier 3."""
    matches = [RequirementMatch(requirement=r, status="gap" if r.kind == "degree" else "met",
                                reason="not in the CV" if r.kind == "degree" else "evidenced", evidence=None,
                                gap_type="degree" if r.kind == "degree" else None, effort_weeks=None)
               for r in jp.requirements]
    return JobMatch(text_hash=jp.text_hash, matches=matches, must_coverage=0.5, nice_coverage=0.0, tier=3)


def seed(db_path, *, fingerprint=None, postings=1, extra=()) -> int:
    """A CV with a master's degree and Python (CV 1; a second call adds CV 2 with a match of the same posting). Each
    posting asks for Python and an HTL diploma, and for the skills of extra. The matches stored are out of date
    (tier 3). Returns the CV id."""
    with Store(db_path) as store:
        cv_id = store.save_cv("cv.md", CV)
        store.save_profile(cv_id, CandidateProfile(language="en", stations=[],
                                                   skills=[_skill("python"), _skill("masters_engineering", "expert",
                                                                                    "claim")]), "fake")
        for i in range(postings):
            h = store.save_posting(f"Software Engineer {i}\nPython and HTL.", f"job_{i}.md")
            jp = JobProfile(text_hash=h, title=f"Software Engineer {i}", role_family="software_development",
                            seniority="mid", requirements=[_requirement("python"),
                                                           _requirement("htl_diploma", "degree"),
                                                           *map(_requirement, extra)])
            store.save_job_profile(jp, "fake")
            store.save_match(cv_id, _out_of_date(jp), fingerprint)
    return cv_id


def tiers(db_path, cv_id=1) -> list[int]:
    with Store(db_path) as store:
        return [jm.tier for jm in store.matches(cv_id)]


# --- the fingerprint ---

def test_the_fingerprint_is_the_same_for_the_same_rules_year_and_code():
    assert match_fingerprint(SETTINGS.rules, 2026) == match_fingerprint(load_settings().rules, 2026)
    assert len(match_fingerprint(SETTINGS.rules, 2026)) == 16


def test_the_fingerprint_changes_with_the_rules_of_a_settings_file(tmp_path):
    override = tmp_path / "settings.yaml"
    override.write_text("rules: {tier2_max_weeks: 4}\n", encoding="utf-8")
    changed = load_settings(override)  # what --settings does
    assert changed.rules.tier2_max_weeks == 4
    assert match_fingerprint(changed.rules, 2026) != match_fingerprint(SETTINGS.rules, 2026)
    override.write_text("rules: {effort_weeks: {weeks: 5, months: 12, certificate: 8}}\n", encoding="utf-8")
    assert match_fingerprint(load_settings(override).rules, 2026) != match_fingerprint(SETTINGS.rules, 2026)


def test_the_fingerprint_ignores_settings_that_decide_no_match(tmp_path):
    override = tmp_path / "settings.yaml"
    override.write_text("monthly_limit_usd: 9\nbatch_threshold: 7\n", encoding="utf-8")
    assert match_fingerprint(load_settings(override).rules, 2026) == match_fingerprint(SETTINGS.rules, 2026)


def test_the_fingerprint_changes_with_the_year_and_with_the_code_version(monkeypatch):
    base = match_fingerprint(SETTINGS.rules, 2026)
    assert match_fingerprint(SETTINGS.rules, 2027) != base  # a skill last used 2018 is stale from 2027 on
    monkeypatch.setattr(matching, "MATCHING_CODE_VERSION", matching.MATCHING_CODE_VERSION + 1)
    assert match_fingerprint(SETTINGS.rules, 2026) != base  # a rule in code changed: the next task bumps this number


# --- recompute without a client ---

def test_rematch_stores_the_fingerprint_of_the_rules(tmp_path):
    db = tmp_path / "s.sqlite"
    seed(db)
    with Store(db) as store:
        assert store.stale_matches(FINGERPRINT) == {1: [store.postings()[0].hash]}
        rematch(open_run(SETTINGS, store, "en"), 1)
        assert store.stale_matches(FINGERPRINT) == {}
        assert store.stale_matches(match_fingerprint(settings_with_rules(tier2_max_weeks=4).rules, YEAR)) != {}


def test_stale_matches_are_recomputed_without_an_api_call(tmp_path):
    db = tmp_path / "s.sqlite"
    seed(db, postings=3, extra=("kubernetes",))
    client = FailingClient()
    with Store(db) as store:
        run = attach_client(open_run(SETTINGS, store, "en"), client)  # a run with a client: refreshing must not use it
        job = store.job_profile(store.postings()[0].hash)
        assert needed_pairs(job, store.profile(1), run.vocab) == {"kubernetes": ["python"]}  # an open relation pair
        assert refresh_matches(run) == 3
        assert [jm.tier for jm in store.matches(1)] == [2, 2, 2]  # Kubernetes is a gap: only stored relations count
        by_skill = {m.requirement.skill_id: m for m in store.matches(1)[0].matches}
        assert (by_skill["htl_diploma"].status, by_skill["htl_diploma"].reason) == (
            "met", "higher degree: masters_engineering")
        assert by_skill["kubernetes"].status == "gap" and by_skill["kubernetes"].reason == "not in the CV"
        assert store.stale_matches(FINGERPRINT) == {} and store.relations("kubernetes") == {}
    assert client.calls == []


def test_a_rematch_with_a_client_does_ask_for_the_open_pair(tmp_path):
    """The contrast of the test above: without the strip in refresh_matches the same run would have called."""
    db = tmp_path / "s.sqlite"
    seed(db, extra=("kubernetes",))
    client = FailingClient()
    with Store(db) as store, pytest.raises(AssertionError, match="no API call"):
        rematch(attach_client(open_run(SETTINGS, store, "en"), client), 1)
    assert client.calls != []


def test_matches_made_by_the_current_rules_are_left_alone(tmp_path):
    db = tmp_path / "s.sqlite"
    seed(db, postings=2)
    with Store(db) as store:
        run = open_run(SETTINGS, store, "en")
        assert refresh_matches(run) == 2
        before = store.db.execute("SELECT cv_id, hash, json, fingerprint FROM match ORDER BY rowid").fetchall()
        assert refresh_matches(run) == 0
        assert store.db.execute("SELECT cv_id, hash, json, fingerprint FROM match ORDER BY rowid").fetchall() == before


def test_only_the_stale_matches_are_recomputed(tmp_path):
    db = tmp_path / "s.sqlite"
    seed(db, postings=2)
    with Store(db) as store:
        [first, second] = [row.hash for row in store.postings()]
        rematch(open_run(SETTINGS, store, "en"), 1, [first])  # the first is current, the second is out of date
        current = store.db.execute("SELECT json FROM match WHERE hash = ?", (first,)).fetchone()
        assert store.stale_matches(FINGERPRINT) == {1: [second]}
        assert refresh_matches(open_run(SETTINGS, store, "en")) == 1
        assert store.db.execute("SELECT json FROM match WHERE hash = ?", (first,)).fetchone() == current
        assert [jm.tier for jm in store.matches(1)] == [1, 1]


def test_only_the_asked_cvs_are_refreshed(tmp_path):
    db = tmp_path / "s.sqlite"
    seed(db)
    seed(db)  # CV 2, the same posting
    with Store(db) as store:
        [h] = [row.hash for row in store.postings()]
        assert refresh_matches(open_run(SETTINGS, store, "en"), [2]) == 1
        assert store.stale_matches(FINGERPRINT) == {1: [h]}
        assert refresh_matches(open_run(SETTINGS, store, "en"), []) == 0
        assert refresh_matches(open_run(SETTINGS, store, "en"), {1, 2}) == 1
        assert store.stale_matches(FINGERPRINT) == {}


def test_matches_follow_a_change_of_the_rules(tmp_path):
    db = tmp_path / "s.sqlite"
    seed(db)
    with Store(db) as store:
        job = store.job_profile(store.postings()[0].hash)
        store.save_job_profile(job.model_copy(update={"requirements": [*job.requirements,
                                                                       _requirement("kubernetes")]}), "fake")
        rematch(open_run(SETTINGS, store, "en"), 1)
        assert [jm.tier for jm in store.matches(1)] == [2]  # the missing Kubernetes takes 12 weeks
        assert refresh_matches(open_run(SETTINGS, store, "en")) == 0
        assert refresh_matches(open_run(settings_with_rules(tier2_max_weeks=4), store, "en")) == 1
        assert [jm.tier for jm in store.matches(1)] == [3]
        assert refresh_matches(open_run(SETTINGS, store, "en")) == 1
        assert [jm.tier for jm in store.matches(1)] == [2]


def test_a_match_stored_before_soft_skills_and_conditions_is_recomputed(tmp_path):
    """The fingerprint of the rules before soft skills and conditions (code version 1, no default travel share): a
    match stored with it is out of date, and the soft skill it counted as a gap is now not verifiable."""
    rules = SETTINGS.rules.model_dump(exclude={"conditions_default_travel_share"})
    state = json.dumps({"code": 1, "rules": rules, "year": YEAR}, sort_keys=True)
    db = tmp_path / "s.sqlite"
    seed(db, fingerprint=hashlib.sha256(state.encode("utf-8")).hexdigest()[:16])
    with Store(db) as store:
        job = store.job_profile(store.postings()[0].hash)
        soft = Requirement(skill_id="communication", importance="must", min_level=None, min_years=None, kind="soft",
                           quote="communication")
        store.save_job_profile(job.model_copy(update={"requirements": [*job.requirements, soft]}), "fake")
        assert store.stale_matches(FINGERPRINT) == {1: [job.text_hash]}
        assert refresh_matches(open_run(SETTINGS, store, "en")) == 1
        [jm] = store.matches(1)
        assert jm.matches[-1].status == "unverifiable" and jm.tier == 1


def test_a_cv_without_a_profile_is_skipped(tmp_path):
    db = tmp_path / "s.sqlite"
    seed(db)
    with Store(db) as store:
        store.db.execute("DELETE FROM profile")
        store.db.commit()
        assert refresh_matches(open_run(SETTINGS, store, "en")) == 0  # nothing to match it with: it stays as it is
        assert [jm.tier for jm in store.matches(1)] == [3]


# --- when the web app starts ---

def test_the_app_with_a_key_recomputes_stale_matches_at_start_without_a_call(tmp_path, capsys):
    db = tmp_path / "web.sqlite"
    seed(db, postings=2, extra=("kubernetes",))
    client = FailingClient()
    with make_client(tmp_path, client=client):
        assert tiers(db) == [2, 2]
    assert client.calls == []
    with Store(db) as store:
        assert store.stale_matches(FINGERPRINT) == {} and store.relations("kubernetes") == {}
    assert capsys.readouterr().out == "recomputed 2 stored matches with the current rules (no API call)\n"


def test_the_app_says_nothing_when_no_match_is_out_of_date(tmp_path, capsys):
    db = tmp_path / "web.sqlite"
    seed(db)
    with Store(db) as store:
        rematch(open_run(SETTINGS, store, "en"), 1)
    with make_client(tmp_path):
        pass
    assert capsys.readouterr().out == ""


def test_the_app_starts_and_serves_the_stored_matches_when_the_recompute_fails(tmp_path, monkeypatch, capsys, caplog):
    db = tmp_path / "web.sqlite"
    seed(db)

    def broken(run, cv_ids=None):
        raise RuntimeError("the recompute broke")

    monkeypatch.setattr("skillpose.web.app.refresh_matches", broken)
    with caplog.at_level("ERROR", logger="skillpose.web.app"), make_client(tmp_path) as c:
        assert c.get("/healthz").status_code == 200 and c.get("/").status_code == 200
        assert tiers(db) == [3]  # as stored
    [record] = [r for r in caplog.records if r.name == "skillpose.web.app"]
    assert record.levelname == "ERROR" and record.exc_info is not None and record.exc_info[0] is RuntimeError
    assert capsys.readouterr().out == ""  # nothing was recomputed, so no start line


def test_the_demo_at_start_keeps_the_matches_of_its_bundles(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    bundle = fixture_bundle()
    with make_client(tmp_path, key=False, demo_dir=tmp_path / "demo") as c:
        demo_db = c.app.state.skillpose.demo_db
        assert demo_db is not None
        with Store(demo_db) as store:
            stored = store.db.execute("SELECT hash, json, fingerprint FROM match ORDER BY rowid").fetchall()
            assert [JobMatch.model_validate_json(raw) for _, raw, _ in stored] == bundle.matches
            assert {fingerprint for *_, fingerprint in stored} == {None}  # nothing was recomputed or stamped
            assert store.stale_matches(FINGERPRINT) != {}
        assert c.get("/cv/1/matches", headers={"accept-language": "en"}).status_code == 200


def test_the_app_without_a_key_changes_nothing_at_start(tmp_path):
    db = tmp_path / "web.sqlite"
    seed(db)
    before = db.read_bytes()
    with make_client(tmp_path, key=False):
        pass
    assert db.read_bytes() == before and tiers(db) == [3]


def test_the_app_with_a_key_creates_no_database_at_start(tmp_path):
    with make_client(tmp_path):
        pass
    assert not (tmp_path / "web.sqlite").exists()


def test_the_app_follows_the_settings_it_is_started_with(tmp_path):
    db = tmp_path / "web.sqlite"
    seed(db)
    with Store(db) as store:
        job = store.job_profile(store.postings()[0].hash)
        store.save_job_profile(job.model_copy(update={"requirements": [*job.requirements,
                                                                       _requirement("kubernetes")]}), "fake")
    with make_client(tmp_path):
        assert tiers(db) == [2]
    with make_client(tmp_path, settings=settings_with_rules(tier2_max_weeks=4)):
        assert tiers(db) == [3]
    with make_client(tmp_path):
        assert tiers(db) == [2]


# --- the command line ---

@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def export_text(db, capsys, *args) -> str:
    rc = main(["export", "--db", str(db), "--cv", "1", "--lang", "en", *args], client_factory=fail_if_called)
    out = capsys.readouterr()
    assert rc == 0 and out.err == ""
    return out.out


def tier_cell(text: str) -> str:
    """The tier cell of the one posting in the table of matches."""
    row = next(line for line in text.splitlines() if line.startswith("|") and "job_0.md" in line)
    return row.split("|")[1].strip()


def test_export_brings_stale_matches_up_to_date_before_it_reads_them_and_writes_nothing(tmp_path, capsys, no_key):
    db = tmp_path / "s.sqlite"
    seed(db)
    before = db.read_bytes()
    assert tier_cell(export_text(db, capsys)).startswith("1")
    assert db.read_bytes() == before and tiers(db) == [3]  # the recompute lives in memory only
    with Store(db) as store:
        assert store.stale_matches(FINGERPRINT) != {}


def test_export_follows_the_settings_file_and_the_next_export_follows_the_defaults(tmp_path, capsys, no_key):
    db = tmp_path / "s.sqlite"
    seed(db)
    with Store(db) as store:
        job = store.job_profile(store.postings()[0].hash)
        store.save_job_profile(job.model_copy(update={"requirements": [*job.requirements,
                                                                       _requirement("kubernetes")]}), "fake")
    strict = tmp_path / "strict.yaml"
    strict.write_text("rules: {tier2_max_weeks: 4}\n", encoding="utf-8")
    before = db.read_bytes()
    assert tier_cell(export_text(db, capsys)).startswith("2")
    assert tier_cell(export_text(db, capsys, "--settings", str(strict))).startswith("3")
    assert tier_cell(export_text(db, capsys)).startswith("2")
    assert db.read_bytes() == before  # also with --settings, the file is never written


@pytest.fixture
def refresh_spy(monkeypatch):
    """What `skillpose export` hands to refresh_matches: (CV ids, whether its store lives in memory only)."""
    calls = []
    real = cli.refresh_matches

    def spy(run, cv_ids=None):
        calls.append((sorted(cv_ids) if cv_ids is not None else None,
                      [row[2] for row in run.store.db.execute("PRAGMA database_list")] == [""]))
        return real(run, cv_ids)

    monkeypatch.setattr(cli, "refresh_matches", spy)
    return calls


def test_export_recomputes_only_the_asked_cv_and_only_in_memory(tmp_path, capsys, no_key, refresh_spy):
    db = tmp_path / "s.sqlite"
    seed(db)
    seed(db)
    export_text(db, capsys)  # --cv 1
    assert refresh_spy == [([1], True)]
    rc = main(["export", "--db", str(db), "--lang", "en"], client_factory=fail_if_called)
    assert rc == 0 and refresh_spy[1:] == [([1, 2], True)]  # without --cv: every analyzed CV
    capsys.readouterr()


def test_export_checks_the_cv_before_it_recomputes_anything(tmp_path, capsys, no_key, refresh_spy):
    db = tmp_path / "s.sqlite"
    seed(db)
    rc = main(["export", "--db", str(db), "--cv", "99"], client_factory=fail_if_called)
    assert rc == 2 and "CV 99 does not exist" in capsys.readouterr().err and refresh_spy == []


def test_export_creates_no_database_and_makes_no_call(tmp_path, capsys, no_key):
    rc = main(["export", "--db", str(tmp_path / "none.sqlite")], client_factory=fail_if_called)
    assert rc == 2 and "no database" in capsys.readouterr().err and not (tmp_path / "none.sqlite").exists()


def analyze_args(tmp_path):
    """(the arguments of analyze for one CV, a function that makes a folder with postings of the given numbers)."""
    cv = tmp_path / "cv.md"
    cv.write_text(CV, encoding="utf-8")

    def jobs(name, *numbers):
        folder = tmp_path / name
        folder.mkdir()
        for i in numbers:
            (folder / f"job_{i}.md").write_text(f"Test Automation Engineer {i}\nSelenium and Java required.",
                                                encoding="utf-8")
        return folder

    return ["analyze", "--cv", str(cv), "--db", str(tmp_path / "s.sqlite"), "--yes"], jobs


def test_analyze_prints_the_market_view_from_matches_that_follow_the_rules(tmp_path, capsys):
    args, jobs = analyze_args(tmp_path)
    db = tmp_path / "s.sqlite"
    assert main([*args, "--jobs", str(jobs("first", 0, 1))], client_factory=ScriptedClient) == 0
    capsys.readouterr()
    with Store(db) as store:  # two postings of an earlier run, matched by other rules: no longer fits
        for jm in store.matches(1):
            store.save_match(1, jm.model_copy(update={"tier": 3, "must_coverage": 0.0}))
    assert main([*args, "--jobs", str(jobs("second", 2))], client_factory=ScriptedClient) == 0
    out = capsys.readouterr().out
    assert "  test_automation           tier 1      3 postings  100 %" in out
    assert tiers(db) == [1, 1, 1]


def test_analyze_recomputes_the_earlier_matches_without_a_call(tmp_path, capsys):
    args, jobs = analyze_args(tmp_path)
    db = tmp_path / "s.sqlite"
    first = ScriptedClient(extra_requirement="terraform")  # the first two postings also ask for Terraform
    assert main([*args, "--jobs", str(jobs("first", 0, 1))], client_factory=lambda: first) == 0
    capsys.readouterr()
    with Store(db) as store:
        store.db.execute("DELETE FROM relation WHERE target = 'terraform'")  # its relation pair is open again
        store.db.commit()
        for jm in store.matches(1):
            store.save_match(1, jm.model_copy(update={"tier": 3, "must_coverage": 0.0}))
    second = ScriptedClient()
    assert main([*args, "--jobs", str(jobs("second", 2))], client_factory=lambda: second) == 0
    out = capsys.readouterr().out
    assert [call.path for call in second.calls] == ["JobDraft"]  # the new posting only: no relation call at all
    with Store(db) as store:
        assert store.relations("terraform") == {} and store.stale_matches(FINGERPRINT) == {}
    assert "  test_automation           tier 2      3 postings   67 %" in out  # Terraform stays a gap in the old two
    assert sorted(tiers(db)) == [1, 2, 2]
