import json
import re
from pathlib import Path

import pytest
import yaml
from demo_fixture import (
    COSTS,
    DEMO_ANSWERS,
    ENGLISH_TEXT,
    ENGLISH_TITLE,
    GERMAN_TEXT,
    KEY,
    NAME,
    bundle_with_answers,
    bundle_with_translations,
    fixture_bundle,
    write_fixture_bundles,
)
from fakes import CV, FakeClient, ScriptedClient, fail_if_called, prompt_folder, refusal, req

import skillpose
from skillpose.cli import main
from skillpose.core.config import load_settings
from skillpose.core.models import Conditions, JobDraft, RequirementDraft, text_hash
from skillpose.core.prompts import load_prompts
from skillpose.demo import (
    COSTS_FILE,
    DemoBundle,
    DemoCosts,
    DemoTranslation,
    build_demo,
    build_estimate,
    demo_data_dir,
    export_demo,
    load_demo,
    read_conditions,
    read_goldset,
    read_translations,
)
from skillpose.pipeline import estimate, open_run
from skillpose.store.db import Store


def goldset(tmp_path: Path, languages: tuple[str, ...] = ("de",), translations: dict[str, str] | None = None,
            conditions: dict | list | None = None) -> Path:
    """A small gold set: one CV per language (person_0, person_1, ...) and two English postings of test_automation.
    translations: file name -> content of the files in translations/; conditions: the answers in expected.yaml."""
    g = tmp_path / "goldset"
    (g / "cvs").mkdir(parents=True)
    (g / "postings").mkdir()
    cvs = {}
    for i, lang in enumerate(languages):
        (g / "cvs" / f"person_{i}.md").write_text(f"{CV} Person {i}.", encoding="utf-8")
        cvs[f"person_{i}"] = {"language": lang, "skills": ["selenium"]}
    expected = {"cvs": cvs, "postings": {}} | ({} if conditions is None else {"conditions": conditions})
    (g / "expected.yaml").write_text(yaml.safe_dump(expected), encoding="utf-8")
    for i in range(2):
        (g / "postings" / f"{i:02d}_test_automation.md").write_text(
            f"Test Automation Engineer {i}\nSelenium and Java required.", encoding="utf-8")
    if translations is not None:
        (g / "translations").mkdir()
        for name, content in translations.items():
            (g / "translations" / name).write_text(content, encoding="utf-8")
    return g


# the German translation of posting 00 of goldset(), as a file of translations/
TRANSLATION_00 = "# Testautomatisierer 0\n\nTestautomatisierer 0\nSelenium und Java erforderlich.\n"


def settings_file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "settings.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def demo_build(tmp_path: Path, g: Path, *extra: str) -> list[str]:
    return ["demo", "build", "--goldset", str(g), "--db", str(tmp_path / "g.sqlite"), "--out", str(tmp_path / "out"),
            *extra]


# --- bundles ---

def test_load_demo_builds_a_store(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    assert load_demo(tmp_path / "demo", tmp_path / "d.sqlite") == 1
    with Store(tmp_path / "d.sqlite") as st:
        [cv] = st.cvs()
        assert len(st.matches(cv.id)) == 5 and "test_automation" in st.paths(cv.id) and len(st.cost_rows()) == 2
        assert st.paths_all(cv.id) == fixture_bundle().paths  # the path in German and in English
        assert cv.name == NAME and cv.analyzed and st.cv_text(cv.id) == fixture_bundle().cv_text
        assert sorted(jm.tier for jm in st.matches(cv.id)) == [1, 1, 1, 2, 3]
        assert all(row.status == "analyzed" and row.refs for row in st.postings()) and len(st.postings()) == 5
        assert st.cost_rows() == COSTS  # with the timestamps of the build


def test_load_demo_replaces_an_existing_file(tmp_path):
    db = tmp_path / "d.sqlite"
    with Store(db) as st:
        st.save_cv("old.md", "an old CV")
    write_fixture_bundles(tmp_path / "demo")
    assert load_demo(tmp_path / "demo", db) == 1
    with Store(db) as st:
        assert [cv.name for cv in st.cvs()] == [NAME] and len(st.cost_rows()) == 2


@pytest.mark.parametrize("make", [fixture_bundle, bundle_with_answers])
def test_export_then_load_is_lossless(tmp_path, make):
    write_fixture_bundles(tmp_path / "demo", make())
    load_demo(tmp_path / "demo", tmp_path / "a.sqlite")
    with Store(tmp_path / "a.sqlite") as st:
        [cv] = st.cvs()
        files = export_demo(st, tmp_path / "out", {KEY: cv.id})
    assert files == [tmp_path / "out" / f"{KEY}.json", tmp_path / "out" / COSTS_FILE]
    assert DemoBundle.model_validate_json(files[0].read_bytes()) == make()
    assert DemoCosts.model_validate_json(files[1].read_bytes()).rows == COSTS
    for f in files:  # same dumps, byte for byte: a rebuild with the same results gives no diff
        assert f.read_bytes() == (tmp_path / "demo" / f.name).read_bytes()
    assert load_demo(tmp_path / "out", tmp_path / "b.sqlite") == 1
    with Store(tmp_path / "b.sqlite") as st:
        [cv] = st.cvs()
        assert export_demo(st, tmp_path / "again", {KEY: cv.id})[0].read_bytes() == files[0].read_bytes()


def test_load_demo_saves_the_translations(tmp_path):
    bundle = bundle_with_translations()
    write_fixture_bundles(tmp_path / "demo", bundle)
    load_demo(tmp_path / "demo", tmp_path / "d.sqlite")
    with Store(tmp_path / "d.sqlite") as st:
        german = text_hash(GERMAN_TEXT)
        assert st.translations(german) == {"en": (ENGLISH_TITLE, ENGLISH_TEXT)}
        translated = [p.hash for p in bundle.postings if p.translations]
        assert len(translated) == 2 and all(st.translations(p.hash) == {} for p in bundle.postings
                                            if not p.translations)
        assert st.db.execute("SELECT DISTINCT model FROM posting_translation").fetchall() == [("demo",)]


def test_export_then_load_keeps_the_translations(tmp_path):
    write_fixture_bundles(tmp_path / "demo", bundle_with_translations())
    load_demo(tmp_path / "demo", tmp_path / "a.sqlite")
    with Store(tmp_path / "a.sqlite") as st:
        [cv] = st.cvs()
        [bundle_file, _] = export_demo(st, tmp_path / "out", {KEY: cv.id})
    assert DemoBundle.model_validate_json(bundle_file.read_bytes()) == bundle_with_translations()
    assert bundle_file.read_bytes() == (tmp_path / "demo" / bundle_file.name).read_bytes()


def test_a_bundle_without_translations_still_loads(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    path = tmp_path / "demo" / f"{KEY}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for posting in raw["postings"]:
        del posting["translations"]  # a bundle from before the translations
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_demo(tmp_path / "demo", tmp_path / "d.sqlite") == 1
    with Store(tmp_path / "d.sqlite") as st:
        assert all(st.translations(row.hash) == {} for row in st.postings())


def test_load_demo_saves_the_answers_of_the_bundles_once(tmp_path):
    write_fixture_bundles(tmp_path / "demo", bundle_with_answers())
    other = bundle_with_answers().model_copy(update={"key": "other_person", "cv_name": "Other Person"})
    (tmp_path / "demo" / "other_person.json").write_text(other.model_dump_json(indent=1), encoding="utf-8")
    assert load_demo(tmp_path / "demo", tmp_path / "d.sqlite") == 2
    with Store(tmp_path / "d.sqlite") as st:
        assert st.conditions() == DEMO_ANSWERS
        assert st.db.execute("SELECT COUNT(*) FROM conditions").fetchone()[0] == 1
        assert all(st.profile(cv.id).conditions == Conditions() for cv in st.cvs())  # not a copy per profile


def test_a_bundle_from_before_the_answers_still_loads(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    path = tmp_path / "demo" / f"{KEY}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["conditions"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert DemoBundle.model_validate(raw).conditions == Conditions()
    assert load_demo(tmp_path / "demo", tmp_path / "d.sqlite") == 1
    with Store(tmp_path / "d.sqlite") as st:
        assert st.conditions() == Conditions() and len(st.matches(st.cvs()[0].id)) == 5


def test_load_demo_refuses_bundles_with_different_answers_and_keeps_the_old_file(tmp_path):
    db = tmp_path / "d.sqlite"
    with Store(db) as st:
        st.save_cv("old.md", "an old CV")
    write_fixture_bundles(tmp_path / "demo", bundle_with_answers())
    other = fixture_bundle().model_copy(update={"key": "other_person"})  # from before the answers: none
    (tmp_path / "demo" / "other_person.json").write_text(other.model_dump_json(indent=1), encoding="utf-8")
    with pytest.raises(ValueError, match="^demo bundles other_person: the answers to the conditions differ from those "
                                         "of maria_demo; the demo has one set of answers, so rebuild the bundles "
                                         "together$"):
        load_demo(tmp_path / "demo", db)
    with Store(db) as st:
        assert [cv.name for cv in st.cvs()] == ["old.md"]


def test_a_bundle_with_a_translation_into_an_unknown_language_is_refused(tmp_path):
    bundle = fixture_bundle().model_dump(mode="json")
    bundle["postings"][0]["translations"] = {"fr": {"title": "Testeur", "text": "Testeur"}}
    with pytest.raises(ValueError, match="fr"):
        DemoBundle.model_validate(bundle)


def test_load_demo_without_bundles(tmp_path):
    assert load_demo(tmp_path, tmp_path / "d.sqlite") == 0 and not (tmp_path / "d.sqlite").exists()
    assert load_demo(tmp_path / "missing", tmp_path / "d.sqlite") == 0
    (tmp_path / COSTS_FILE).write_text(DemoCosts(rows=COSTS).model_dump_json(), encoding="utf-8")
    assert load_demo(tmp_path, tmp_path / "d.sqlite") == 0 and not (tmp_path / "d.sqlite").exists()


def test_export_refuses_a_key_that_is_no_plain_file_stem(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    load_demo(tmp_path / "demo", tmp_path / "a.sqlite")
    with Store(tmp_path / "a.sqlite") as st:
        for key in ["costs", "../outside", "Maria Demo"]:
            with pytest.raises(ValueError, match="key"):
                export_demo(st, tmp_path / "out", {key: 1})


def test_demo_data_is_package_data():
    assert demo_data_dir() == Path(skillpose.__file__).parent / "demo_data"
    assert (demo_data_dir() / "README.md").is_file()


# --- demo build ---

def test_demo_build_needs_yes_above_threshold(tmp_path, capsys):
    settings = settings_file(tmp_path, "confirm_above_usd: 0.0001\n")
    rc = main(demo_build(tmp_path, goldset(tmp_path), "--settings", str(settings)), client_factory=fail_if_called)
    assert rc == 3 and "--yes" in capsys.readouterr().out and not (tmp_path / "out").exists()


def test_demo_build_prints_the_prompt_set_first_before_the_estimate_and_the_decision(tmp_path, capsys):
    folder, g = prompt_folder(tmp_path / "mine"), goldset(tmp_path)
    ask = ("--settings", str(settings_file(tmp_path, "confirm_above_usd: 0.0001\n")))
    for extra, label in (((), "example"), (("--prompts", str(folder)), load_prompts(folder).label)):
        assert main(demo_build(tmp_path, g, *ask, *extra), client_factory=fail_if_called) == 3
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == f"prompt set: {label}" and lines[1].startswith("estimated cost")
        assert "paid again" not in "\n".join(lines)  # a fresh store holds no paths


PAID_AGAIN = "paths made with other prompts are paid again"


def test_demo_build_with_the_examples_warns_that_stored_paths_are_paid_again(tmp_path, capsys):
    # the paths of the author's store were made with the private prompts; the examples give other input, so every
    # path is made again (INSERT OR REPLACE) and paid for
    folder, g = prompt_folder(tmp_path / "mine"), goldset(tmp_path)
    rules = "rules:\n  min_jobs_per_role: 1\n"
    settings = str(settings_file(tmp_path, rules))
    assert main(demo_build(tmp_path, g, "--settings", settings, "--yes", "--prompts", str(folder)),
                client_factory=ScriptedClient) == 0
    capsys.readouterr()
    ask = ("--settings", str(settings_file(tmp_path, "confirm_above_usd: 0.0001\n" + rules)))
    # above the threshold: the note is part of the estimate line that asks for --yes
    assert main(demo_build(tmp_path, g, *ask), client_factory=fail_if_called) == 3
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "prompt set: example" and "run again with --yes" in lines[1] and PAID_AGAIN in lines[1]
    # with --yes the run goes on, but the user has seen the note before the first call
    client = ScriptedClient()
    assert main(demo_build(tmp_path, g, *ask, "--yes"), client_factory=lambda: client) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "prompt set: example" and lines[1].startswith("estimated cost") and PAID_AGAIN in lines[1]
    assert any(call.path == "PathDraft" for call in client.calls)  # and the paths were made again
    # the private set again: no note
    assert main(demo_build(tmp_path, g, *ask, "--prompts", str(folder)), client_factory=fail_if_called) == 3
    assert PAID_AGAIN not in capsys.readouterr().out
    (tmp_path / "g.sqlite").unlink()  # the command closed its store (an open file cannot be deleted on Windows)


def test_demo_build_stops_at_the_monthly_limit(tmp_path, capsys):
    settings = settings_file(tmp_path, "monthly_limit_usd: 0.01\n")
    rc = main(demo_build(tmp_path, goldset(tmp_path), "--settings", str(settings), "--yes"),
              client_factory=fail_if_called)
    assert rc == 4 and "monthly limit" in capsys.readouterr().out and not (tmp_path / "out").exists()


def test_demo_build_end_to_end(tmp_path, capsys):
    g = goldset(tmp_path, languages=("de", "en"))
    args = demo_build(tmp_path, g, "--settings", str(settings_file(tmp_path, "rules:\n  min_jobs_per_role: 1\n")),
                      "--yes")
    client = ScriptedClient()
    assert main(args, client_factory=lambda: client) == 0
    out = capsys.readouterr().out
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [COSTS_FILE, "person_0.json", "person_1.json"]
    assert "person_0.json" in out and "costs.json" in out and "Cost this run" in out

    first, second = (DemoBundle.model_validate_json((tmp_path / "out" / f"person_{i}.json").read_bytes())
                     for i in range(2))
    assert (first.key, first.cv_name, first.cv_text) == ("person_0", "person_0.md", f"{CV} Person 0.")
    assert first.conditions == second.conditions == Conditions()  # the gold set has no answers: none answered
    assert {p.refs[0] for p in first.postings} == {"00_test_automation.md", "01_test_automation.md"}
    assert len(first.matches) == 2
    for bundle in (first, second):  # every target path in both languages
        assert [(p.role_family, p.language) for p in bundle.paths] == [("test_automation", "de"),
                                                                        ("test_automation", "en")]
    cv_calls = [c for c in client.calls if c.path == "CvDraft"]  # each person read in the language of their CV
    assert "in German" in cv_calls[0].kwargs["system"][0]["text"]
    assert "in English" in cv_calls[1].kwargs["system"][0]["text"]
    path_calls = [c for c in client.calls if c.path == "PathDraft"]
    languages = ["German" if "in German." in str(c.kwargs["system"]) else "English" for c in path_calls]
    assert languages == ["German", "English", "English", "German"]  # the language of the CV first, then the other
    costs = DemoCosts.model_validate_json((tmp_path / "out" / COSTS_FILE).read_bytes())
    assert len(costs.rows) == len(client.calls)

    before = {p.name: p.read_bytes() for p in (tmp_path / "out").iterdir()}
    again = ScriptedClient()
    assert main(args, client_factory=lambda: again) == 0 and again.calls == []  # everything is stored: no cost
    assert {p.name: p.read_bytes() for p in (tmp_path / "out").iterdir()} == before
    assert load_demo(tmp_path / "out", tmp_path / "d.sqlite") == 2
    (tmp_path / "g.sqlite").unlink()  # the command closed its store (an open file cannot be deleted on Windows)


def test_demo_build_adds_only_the_missing_language(tmp_path):
    g = goldset(tmp_path)
    settings = load_settings(settings_file(tmp_path, "rules:\n  min_jobs_per_role: 1\n"))
    with Store(tmp_path / "g.sqlite") as st:
        build_demo(open_run(settings, st, "en", client=ScriptedClient()), g, tmp_path / "out")
        st.db.execute("DELETE FROM path WHERE language = 'en'")  # as after a build that made German paths only
        st.db.commit()
        client = ScriptedClient()
        build_demo(open_run(settings, st, "en", client=client), g, tmp_path / "out")
        [call] = client.calls
        assert call.path == "PathDraft" and "in English." in str(call.kwargs["system"])
        again = ScriptedClient()
        build_demo(open_run(settings, st, "en", client=again), g, tmp_path / "out")
        assert again.calls == []
    bundle = DemoBundle.model_validate_json((tmp_path / "out" / "person_0.json").read_bytes())
    assert [p.language for p in bundle.paths] == ["de", "en"]


def test_demo_build_estimate_counts_each_path_in_both_languages(tmp_path):
    g = goldset(tmp_path, languages=("de", "en"))
    with Store(tmp_path / "g.sqlite") as st:
        run = open_run(load_settings(), st, "en")
        cost = build_estimate(run, g, 2)
        cvs = sum(estimate(run, cv_text=(g / "cvs" / f"person_{i}.md").read_text(encoding="utf-8")) for i in range(2))
        assert cost == pytest.approx(cvs + estimate(run, n_postings=2, n_paths=2 * 2 * 2))  # persons × paths × 2


def test_demo_build_reuses_the_stored_cv(tmp_path):
    g = goldset(tmp_path)
    settings = load_settings(settings_file(tmp_path, "rules:\n  min_jobs_per_role: 1\n"))
    with Store(tmp_path / "g.sqlite") as st:
        cv_id = st.save_cv("from_eval.md", (g / "cvs" / "person_0.md").read_text(encoding="utf-8"))
        build_demo(open_run(settings, st, "en", client=ScriptedClient()), g, tmp_path / "out")
        assert [cv.id for cv in st.cvs()] == [cv_id]  # found by its text, so its profile is reused
    bundle = DemoBundle.model_validate_json((tmp_path / "out" / "person_0.json").read_bytes())
    assert bundle.cv_name == "from_eval.md"


def test_demo_build_stops_when_a_cv_gets_no_profile(tmp_path, capsys):
    client = FakeClient([refusal()] * 3)  # two postings, then the CV
    assert main(demo_build(tmp_path, goldset(tmp_path), "--yes"), client_factory=lambda: client) == 5
    out = capsys.readouterr().out
    assert "demo build failed: CV person_0: refusal" in out and "Cost this run" in out
    assert not (tmp_path / "out").exists()  # no incomplete demo


# --- the answers to the conditions of postings (one invented set for the demo) ---

ANSWERS = {"driving_licence_b": False, "travel_share_max": 20, "shift_work": False, "commute_km_max": 40,
           "remote_only": False, "full_time": True, "notes": "Invented"}
LICENCE = RequirementDraft(skill_id="driving_licence_b", name="Führerschein B", category="certificate",
                           importance="must", min_level=None, min_years=None, kind="condition", quote="Führerschein B",
                           condition="driving_licence_b")
WITH_LICENCE = JobDraft(title="Test Automation Engineer", role_family="test_automation", seniority="mid",
                        requirements=[req("selenium"), LICENCE])


def test_demo_build_matches_with_the_answers_of_the_gold_set_and_writes_them_into_every_bundle(tmp_path):
    g = goldset(tmp_path, languages=("de", "en"), conditions=ANSWERS)
    settings = load_settings(settings_file(tmp_path, "rules:\n  min_jobs_per_role: 1\n"))
    client = ScriptedClient(queued={"JobDraft": [WITH_LICENCE, WITH_LICENCE]})
    with Store(tmp_path / "g.sqlite") as st:
        st.save_conditions(Conditions(driving_licence_b=True))  # answers from before: the gold set replaces them
        build_demo(open_run(settings, st, "en", client=client), g, tmp_path / "out")
        assert st.conditions() == Conditions(**ANSWERS)
    bundles = [DemoBundle.model_validate_json((tmp_path / "out" / f"person_{i}.json").read_bytes()) for i in range(2)]
    for bundle in bundles:
        assert bundle.conditions == Conditions(**ANSWERS)
        # no driving licence B: a knockout in every posting of every person
        assert [(jm.matches[1].status, jm.matches[1].reason, jm.tier) for jm in bundle.matches] == [
            ("gap", "profile: no driving licence B", 3)] * 2
    assert load_demo(tmp_path / "out", tmp_path / "d.sqlite") == 2
    with Store(tmp_path / "d.sqlite") as st:
        assert st.conditions() == Conditions(**ANSWERS)


@pytest.mark.parametrize("answers, problem", [
    ({"driving_licence_b": "maybe"}, "conditions: driving_licence_b: Input should be a valid boolean"),
    ({"travel_share_max": 150}, "conditions: travel_share_max: Input should be less than or equal to 100"),
    ({"driving_licence": True}, "conditions: unknown answer driving_licence"),
    (["yes"], "conditions: Input should be a valid dictionary")])
def test_the_answers_of_the_gold_set_are_checked_before_any_call(tmp_path, capsys, answers, problem):
    g = goldset(tmp_path, conditions=answers)
    with pytest.raises(ValueError, match=f"^expected.yaml: {problem}"):
        read_conditions(g)
    assert main(demo_build(tmp_path, g, "--yes"), client_factory=fail_if_called) == 2
    assert f"expected.yaml: {problem}" in capsys.readouterr().out and not (tmp_path / "out").exists()


def test_a_gold_set_without_answers_answers_nothing(tmp_path):
    assert read_conditions(goldset(tmp_path)) == Conditions()


def test_the_gold_set_answers_every_condition_for_the_demo():
    answers = read_conditions(Path(__file__).parents[1] / "evals" / "goldset")
    assert [name for name, value in answers if value is None] == []


# --- translations of the postings (files of the gold set, no API call) ---

def test_demo_build_stores_the_translation_files_in_the_bundles(tmp_path, caplog):
    g = goldset(tmp_path, translations={"00_test_automation.de.md": TRANSLATION_00})
    settings = load_settings(settings_file(tmp_path, "rules:\n  min_jobs_per_role: 1\n"))
    client = ScriptedClient()
    with Store(tmp_path / "g.sqlite") as st:
        build_demo(open_run(settings, st, "en", client=client), g, tmp_path / "out")
        first = text_hash("Test Automation Engineer 0\nSelenium and Java required.")
        assert st.db.execute("SELECT model FROM posting_translation WHERE hash = ?", (first,)).fetchall() == [
            ("claude-code",)]
    assert {c.path for c in client.calls} <= {"CvDraft", "JobDraft", "RelationAnswer", "PathDraft"}  # no other call
    bundle = DemoBundle.model_validate_json((tmp_path / "out" / "person_0.json").read_bytes())
    by_ref = {p.refs[0]: p for p in bundle.postings}
    german = DemoTranslation(title="Testautomatisierer 0", text="Testautomatisierer 0\nSelenium und Java erforderlich.")
    assert by_ref["00_test_automation.md"].translations == {"de": german}
    assert by_ref["01_test_automation.md"].translations == {}  # no file, no translation
    assert "01_test_automation.md has no translation into de" in caplog.text
    assert "00_test_automation.md has no translation" not in caplog.text


def test_demo_build_follows_the_translation_files(tmp_path):
    g = goldset(tmp_path, translations={"00_test_automation.de.md": TRANSLATION_00})
    settings = load_settings(settings_file(tmp_path, "rules:\n  min_jobs_per_role: 1\n"))
    with Store(tmp_path / "g.sqlite") as st:
        build_demo(open_run(settings, st, "en", client=ScriptedClient()), g, tmp_path / "out")
        (g / "translations" / "00_test_automation.de.md").write_text(TRANSLATION_00.replace("0", "Null"),
                                                                       encoding="utf-8")
        (g / "translations" / "01_test_automation.de.md").write_text("# Eins\n\nEins", encoding="utf-8")
        again = ScriptedClient()
        build_demo(open_run(settings, st, "en", client=again), g, tmp_path / "out")
        assert again.calls == []  # a changed file costs nothing
        bundle = DemoBundle.model_validate_json((tmp_path / "out" / "person_0.json").read_bytes())
        assert [p.translations["de"].title for p in sorted(bundle.postings, key=lambda p: p.refs[0])] == [
            "Testautomatisierer Null", "Eins"]
        (g / "translations" / "01_test_automation.de.md").unlink()  # the file is gone: so is its translation
        build_demo(open_run(settings, st, "en", client=ScriptedClient()), g, tmp_path / "out")
    bundle = DemoBundle.model_validate_json((tmp_path / "out" / "person_0.json").read_bytes())
    assert sorted((p.refs[0], list(p.translations)) for p in bundle.postings) == [
        ("00_test_automation.md", ["de"]), ("01_test_automation.md", [])]


def test_read_translations(tmp_path, caplog):
    g = goldset(tmp_path, translations={"99_unknown.de.md": "# Unbekannt\n\nText", "README.md": "How to write them."})
    (g / "translations" / "00_test_automation.de.md").write_bytes(  # with a BOM and CRLF, as a Windows editor saves
        "﻿# Testautomatisierer 0  \r\n\r\nZeile 1\r\n  - Punkt\r\n\r\n".encode())
    _, postings = read_goldset(g)
    found = read_translations(g, postings)
    assert found == {"00_test_automation": {"de": DemoTranslation(title="Testautomatisierer 0",
                                                                  text="Zeile 1\n  - Punkt")}}
    assert "99_unknown.de.md" in caplog.text  # no such posting: skipped with a warning
    assert read_translations(goldset(tmp_path / "none"), postings) == {}  # no folder translations/


@pytest.mark.parametrize("name, content, problem", [
    ("00_test_automation.de.md", "Testautomatisierer 0\n\nText", "first line"),
    ("00_test_automation.de.md", "#Testautomatisierer 0\n\nText", "first line"),
    ("00_test_automation.de.md", "#   \n\nText", "first line"),
    ("00_test_automation.de.md", "", "first line"),
    ("00_test_automation.de.md", "# Testautomatisierer 0\n\n  \n", "no text"),
    ("00_test_automation.fr.md", "# Testeur 0\n\nTexte", "<posting file stem>.<de|en>.md"),
    ("00_test_automation.md", "# Testautomatisierer 0\n\nText", "<posting file stem>.<de|en>.md"),
    ("00_test_automation.en.md", "# Test Automation Engineer 0\n\nText", "written in English"),
])
def test_read_translations_names_a_broken_file(tmp_path, name, content, problem):
    g = goldset(tmp_path, translations={name: content})
    _, postings = read_goldset(g)
    with pytest.raises(ValueError, match=re.escape(problem)) as error:
        read_translations(g, postings)
    assert name in str(error.value)


def test_read_translations_names_a_file_that_is_not_utf8(tmp_path):
    g = goldset(tmp_path, translations={})
    (g / "translations" / "00_test_automation.de.md").write_bytes("# Prüfer\n\nText".encode("cp1252"))
    with pytest.raises(ValueError, match="00_test_automation.de.md.*UTF-8"):
        read_translations(g, read_goldset(g)[1])


def test_demo_build_stops_at_a_broken_translation_file_before_any_call(tmp_path, capsys):
    g = goldset(tmp_path, translations={"01_test_automation.de.md": "no heading"})
    assert main(demo_build(tmp_path, g, "--yes"), client_factory=fail_if_called) == 2
    assert "01_test_automation.de.md" in capsys.readouterr().out and not (tmp_path / "out").exists()
    settings = load_settings(settings_file(tmp_path, "rules:\n  min_jobs_per_role: 1\n"))
    with Store(tmp_path / "g2.sqlite") as st, pytest.raises(ValueError, match="01_test_automation.de.md"):
        build_demo(open_run(settings, st, "en", client=fail_if_called), g, tmp_path / "out")


def test_demo_build_needs_the_language_of_each_cv(tmp_path, capsys):
    g = goldset(tmp_path)
    expected = yaml.safe_load((g / "expected.yaml").read_text(encoding="utf-8"))
    del expected["cvs"]["person_0"]["language"]
    (g / "expected.yaml").write_text(yaml.safe_dump(expected), encoding="utf-8")
    with Store(tmp_path / "g.sqlite") as st, pytest.raises(ValueError, match="person_0.language"):
        build_demo(open_run(load_settings(), st, "en", client=ScriptedClient()), g, tmp_path / "out")
    assert main(demo_build(tmp_path, g, "--yes"), client_factory=fail_if_called) == 2
    assert "language" in capsys.readouterr().out
