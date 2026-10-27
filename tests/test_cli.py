import re

import anthropic
import pytest
from fakes import (
    CV,
    FailingClient,
    FakeBatchClient,
    FakeClient,
    ScriptedClient,
    answer,
    fail_if_called,
    not_found,
    prompt_folder,
    refusal,
    req,
    scripted_client,
)
from test_evaluation import small_goldset

from skillpose.cli import _print_table, apply_env, load_env_file, main
from skillpose.core.config import load_settings
from skillpose.core.llm import Usage
from skillpose.core.models import (
    JobDraft,
    JobMatch,
    JobProfile,
    Requirement,
    RequirementMatch,
    SkillProposal,
    text_hash,
)
from skillpose.core.prompts import load_prompts
from skillpose.core.vocabulary import Vocabulary
from skillpose.store.db import Store


def cv_file(tmp_path):
    p = tmp_path / "cv.md"
    p.write_text(CV, encoding="utf-8")
    return p


def jobs_dir(tmp_path, n):
    d = tmp_path / "jobs"
    d.mkdir()
    for i in range(n):
        text = f"Test Automation Engineer {i}\nSelenium and Java required."
        (d / f"job_{i:03d}.md").write_text(text, encoding="utf-8")
    return d


def settings_yaml(tmp_path, **values):
    p = tmp_path / "settings.yaml"
    p.write_text("".join(f"{k}: {v}\n" for k, v in values.items()), encoding="utf-8")
    return p


def test_analyze_end_to_end_prints_tiers(tmp_path, capsys):
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=2)),
               "--db", str(tmp_path / "s.sqlite"), "--yes"], client_factory=scripted_client)
    out = capsys.readouterr().out
    assert rc == 0 and "tier" in out and "cache read share" in out
    assert "Test Automation Engineer 0" in out and "Kubernetes" in out
    assert len(Store(tmp_path / "s.sqlite").matches(1)) == 2


def test_estimate_above_threshold_needs_yes(tmp_path, capsys):
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=3)),
               "--db", str(tmp_path / "s.sqlite"), "--settings", str(settings_yaml(tmp_path, confirm_above_usd=0.01))],
              client_factory=fail_if_called)
    assert rc == 3 and "estimated" in capsys.readouterr().out


def test_estimate_above_monthly_limit_stops_before_any_call(tmp_path, capsys):
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=3)),
               "--db", str(tmp_path / "s.sqlite"), "--yes",
               "--settings", str(settings_yaml(tmp_path, monthly_limit_usd=0.05))], client_factory=fail_if_called)
    assert rc == 4 and "monthly limit" in capsys.readouterr().out


def long_cv_file(tmp_path):
    """A real two-page CV: 9,289 characters (4,657 input tokens), which cost about 0.40 USD."""
    p = tmp_path / "long_cv.md"
    p.write_text((CV + " ") * (9289 // (len(CV) + 1)), encoding="utf-8")
    return p


def test_the_estimate_of_a_long_cv_decides_on_the_confirmation_and_the_monthly_limit(tmp_path, capsys):
    cv, jobs = long_cv_file(tmp_path), jobs_dir(tmp_path, n=1)
    assert 9000 < len(cv.read_text(encoding="utf-8")) <= 9289
    # about 0.48 USD with one posting (with the former 4,000 output tokens for a CV: 0.15): above 0.40 both times
    rc = main(["analyze", "--cv", str(cv), "--jobs", str(jobs), "--db", str(tmp_path / "a.sqlite"),
               "--settings", str(settings_yaml(tmp_path, confirm_above_usd=0.40))], client_factory=fail_if_called)
    out = capsys.readouterr().out
    assert rc == 3 and re.search(r"estimated cost 0\.4\d USD is above 0\.40 USD", out), out
    rc = main(["analyze", "--cv", str(cv), "--jobs", str(jobs), "--db", str(tmp_path / "b.sqlite"), "--yes",
               "--settings", str(settings_yaml(tmp_path, monthly_limit_usd=0.40))], client_factory=fail_if_called)
    assert rc == 4 and "monthly limit" in capsys.readouterr().out


def test_budget_reached_mid_run_keeps_results(tmp_path, capsys):
    db = tmp_path / "s.sqlite"
    # 4.50 USD spent, limit 4.90: the estimate (~0.26) fits, but the CV call costs more (0.44) than estimated
    store = Store(db)
    store.record("cv", "claude-opus-5-5", Usage(0, 0, 0, 0), 4.50, False)
    store.db.close()
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=3)), "--db", str(db),
               "--yes", "--settings", str(settings_yaml(tmp_path, monthly_limit_usd=4.90))],
              client_factory=lambda: ScriptedClient(cv_usage=Usage(60000, 10000, 0, 0)))
    assert rc == 4 and "left open" in capsys.readouterr().out
    assert Store(db).profile(1) is not None and len(Store(db).open_postings()) >= 1


def test_second_run_reuses_profile_and_job_profiles(tmp_path):
    db, cv, jobs = tmp_path / "s.sqlite", cv_file(tmp_path), jobs_dir(tmp_path, n=2)
    main(["analyze", "--cv", str(cv), "--jobs", str(jobs), "--db", str(db), "--yes"], client_factory=scripted_client)
    second = ScriptedClient()
    main(["analyze", "--cv", str(cv), "--jobs", str(jobs), "--db", str(db), "--yes"], client_factory=lambda: second)
    assert second.calls == []


def test_pending_batch_of_an_interrupted_run_is_collected_not_resubmitted(tmp_path, capsys):
    db, jobs = tmp_path / "s.sqlite", jobs_dir(tmp_path, n=2)
    store = Store(db)
    hashes = [store.save_posting(p.read_text(encoding="utf-8").strip(), p.name) for p in sorted(jobs.iterdir())]
    store.save_batch("batch_old", hashes)
    store.db.close()
    draft = JobDraft(title="From batch", role_family="test_automation", seniority="mid", requirements=[req("java")])
    batches = FakeBatchClient(results={h: ("succeeded", draft.model_dump_json()) for h in hashes}, statuses=["ended"])
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs), "--db", str(db), "--yes"],
              client_factory=lambda: client)
    assert rc == 0 and "batch_old" in capsys.readouterr().out
    assert batches.created == [] and not any(c.path == "JobDraft" for c in client.calls)
    after = Store(db)
    assert after.pending_batches() == [] and all(after.job_profile(h).title == "From batch" for h in hashes)


def test_a_lost_batch_of_an_earlier_run_is_given_up_without_a_traceback(tmp_path, capsys):
    db, jobs = tmp_path / "s.sqlite", jobs_dir(tmp_path, n=2)
    store = Store(db)
    hashes = [store.save_posting(p.read_text(encoding="utf-8").strip(), p.name) for p in sorted(jobs.iterdir())]
    store.save_batch("batch_old", hashes)
    store.db.close()
    client = ScriptedClient()
    client.messages.batches = FakeBatchClient(results={}, statuses=[], retrieve_error=not_found()).messages.batches
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs), "--db", str(db), "--yes"],
              client_factory=lambda: client)
    out = capsys.readouterr().out
    assert rc == 0 and "0 of 2 postings analyzed" in out and "stays open: batch batch_old lost" in out
    after = Store(db)
    assert after.pending_batches() == [] and after.open_postings() == hashes
    second = ScriptedClient()  # the next run analyzes the two postings again
    assert main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs), "--db", str(db), "--yes"],
                client_factory=lambda: second) == 0
    assert [c.path for c in second.calls if c.path == "JobDraft"] == ["JobDraft"] * 2


def test_new_batches_are_recorded_and_the_first_result_comes_first(tmp_path, capsys):
    db, jobs = tmp_path / "s.sqlite", jobs_dir(tmp_path, n=20)
    texts = [p.read_text(encoding="utf-8").strip() for p in sorted(jobs.iterdir())]
    draft = JobDraft(title="From batch", role_family="test_automation", seniority="mid", requirements=[req("java")])
    client = ScriptedClient()
    batches = FakeBatchClient(results={text_hash(t): ("succeeded", draft.model_dump_json()) for t in texts},
                              statuses=["ended"] * 3, log=client.calls)
    client.messages.batches = batches.messages.batches
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs), "--db", str(db), "--yes"],
              client_factory=lambda: client)
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert rc == 0
    order = ["batch batch_1 with 7 postings submitted (half price)", "batch batch_2 with 6 postings submitted",
             "batch batch_3 with 6 postings submitted", "first result: 1 of 1 postings analyzed at once",
             "Test Automation Engineer 0", "waiting for the batches", "batch batch_1 collected: 7 of 7",
             "batch batch_2 collected: 6 of 6", "batch batch_3 collected: 6 of 6",
             "19 of 19 postings analyzed, 0 stay open", "From batch"]
    at = [next(i for i, line in enumerate(lines) if part in line) for part in order]
    assert at == sorted(at), at  # the single result is printed before the batches are collected
    assert sum(line.startswith("title ") for line in lines) == 2  # the table of the first result, then all of them
    sent = [c.path for c in client.calls if c.path in ("batches.create", "JobDraft")]
    assert sent == ["batches.create"] * 3 + ["JobDraft"] and len(batches.created) == 19
    assert Store(db).pending_batches() == [] and Store(db).open_postings() == []


def test_no_batch_sends_every_posting_as_a_single_call(tmp_path, capsys):
    client = ScriptedClient()
    client.messages.batches = FakeBatchClient(results={}, statuses=[], log=client.calls).messages.batches
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=20)),
               "--db", str(tmp_path / "s.sqlite"), "--yes", "--no-batch"], client_factory=lambda: client)
    out = capsys.readouterr().out
    assert rc == 0 and "batch" not in out
    assert [c.path for c in client.calls if c.path in ("batches.create", "JobDraft")] == ["JobDraft"] * 20


def test_the_table_names_a_skill_claude_proposed_in_this_run(capsys):
    # the proposals reach the vocabulary only after the run, so the table must take the name from the job profile
    flink = Requirement(skill_id="apache_flink", importance="must", min_level=None, min_years=None, kind="tool",
                        quote="Apache Flink")
    jp = JobProfile(text_hash="h", title="Data Engineer", role_family="data_engineering", seniority="mid",
                    requirements=[flink],
                    proposed_skills=[SkillProposal(id="apache_flink", name="Apache Flink", category="tool")])
    gap = RequirementMatch(requirement=flink, status="gap", reason="not in the CV", evidence=None, gap_type="weeks",
                           effort_weeks=4)
    _print_table([(jp, JobMatch(text_hash="h", matches=[gap], must_coverage=0.0, nice_coverage=0.0, tier=2))],
                 Vocabulary.load())
    assert "Apache Flink (weeks)" in capsys.readouterr().out


def test_unreadable_cv_returns_2(tmp_path, capsys):
    p = tmp_path / "cv.txt"
    p.write_text("too short")
    rc = main(["analyze", "--cv", str(p), "--jobs", str(jobs_dir(tmp_path, n=1)), "--db", str(tmp_path / "s.sqlite")],
              client_factory=fail_if_called)
    assert rc == 2 and "no readable text" in capsys.readouterr().out


def test_new_skills_are_listed_after_run(tmp_path, capsys):
    db = tmp_path / "s.sqlite"
    main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=1)), "--db", str(db), "--yes"],
         client_factory=lambda: ScriptedClient(extra_requirement="n8n"))
    capsys.readouterr()
    main(["vocab", "--new", "--db", str(db)])
    assert "n8n" in capsys.readouterr().out
    main(["vocab", "--accept", "n8n", "--db", str(db)])
    assert "accepted: n8n" in capsys.readouterr().out
    main(["vocab", "--new", "--db", str(db)])
    assert "n8n" not in capsys.readouterr().out


def test_vocab_reject(tmp_path, capsys):
    db, cv, jobs = tmp_path / "s.sqlite", cv_file(tmp_path), jobs_dir(tmp_path, n=1)
    main(["analyze", "--cv", str(cv), "--jobs", str(jobs), "--db", str(db), "--yes"],
         client_factory=lambda: ScriptedClient(extra_requirement="n8n"))
    capsys.readouterr()
    assert main(["vocab", "--reject", "n8n", "--db", str(db)]) == 0
    assert "rejected: n8n" in capsys.readouterr().out
    main(["vocab", "--new", "--db", str(db)])
    assert "n8n" not in capsys.readouterr().out
    # a later run that proposes n8n again does not bring it back
    main(["analyze", "--cv", str(cv), "--jobs", str(jobs), "--db", str(db), "--yes"],
         client_factory=lambda: ScriptedClient(extra_requirement="n8n"))
    capsys.readouterr()
    main(["vocab", "--new", "--db", str(db)])
    assert "n8n" not in capsys.readouterr().out
    assert Store(db).added_skills() == []


def test_vocab_reject_several_ids(tmp_path, capsys):
    db = tmp_path / "s.sqlite"
    main(["vocab", "--reject", "n8n", "zapier", "--db", str(db)])
    assert "rejected: n8n, zapier" in capsys.readouterr().out


def test_load_env_file_sets_variables_without_printing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SKILLPOSE_TEST_KEY", raising=False)
    p = tmp_path / "x.env"
    p.write_text("# comment\nSKILLPOSE_TEST_KEY = 'abc'\n\n", encoding="utf-8")
    load_env_file(p)
    import os
    assert os.environ["SKILLPOSE_TEST_KEY"] == "abc" and capsys.readouterr().out == ""


def test_analyze_prints_role_families(tmp_path, capsys):
    five = ["--settings", str(settings_yaml(tmp_path, rules="{min_jobs_per_role: 5}"))]  # 2 postings are too few
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=2)),
               "--db", str(tmp_path / "s.sqlite"), "--yes", *five], client_factory=scripted_client)
    out = capsys.readouterr().out
    assert rc == 0 and "Role families (tier, postings, median must coverage):" in out
    assert f"  {'test_automation':24}  too little data (2 postings)" in out.splitlines()
    assert out.index("Role families") > out.index("top gaps") and out.index("Role families") < out.index("Cost this")


def test_the_default_minimum_gives_a_role_with_two_postings_a_tier(tmp_path, capsys):
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=2)),
               "--db", str(tmp_path / "s.sqlite"), "--yes"], client_factory=scripted_client)
    out = capsys.readouterr().out
    assert rc == 0 and "too little data" not in out
    assert "  test_automation           tier 1      2 postings  100 %" in out


def test_role_family_line_with_a_tier(tmp_path, capsys):
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=5)),
               "--db", str(tmp_path / "s.sqlite"), "--yes"], client_factory=scripted_client)
    assert rc == 0 and "  test_automation           tier 1      5 postings  100 %" in capsys.readouterr().out


def test_monthly_limit_from_environment(monkeypatch):
    monkeypatch.setenv("MONTHLY_LIMIT_USD", "2.5")
    settings = load_settings()
    assert apply_env(settings).monthly_limit_usd == 2.5 and settings.monthly_limit_usd == 5.0
    monkeypatch.delenv("MONTHLY_LIMIT_USD")
    assert apply_env(settings).monthly_limit_usd == 5.0


def test_analyze_stops_at_the_monthly_limit_from_the_env_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MONTHLY_LIMIT_USD", "5")  # so the value the env file sets is removed after the test
    env = tmp_path / "x.env"
    env.write_text("MONTHLY_LIMIT_USD=0.01\n", encoding="utf-8")
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=1)),
               "--db", str(tmp_path / "s.sqlite"), "--yes", "--env-file", str(env)], client_factory=fail_if_called)
    assert rc == 4 and "> 0.01 USD" in capsys.readouterr().out


def test_failed_posting_is_reported_and_keeps_its_reason(tmp_path, capsys):
    db = tmp_path / "s.sqlite"
    client = ScriptedClient()
    original = client._answer
    client.beta.messages.create = lambda **kw: (refusal() if kw["output_config"]["format"]["schema"]["title"]
                                                == "JobDraft" else original(**kw))
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=1)), "--db", str(db),
               "--yes"], client_factory=lambda: client)
    [row] = Store(db).postings()
    assert rc == 0 and f"posting {row.hash} stays open: refusal" in capsys.readouterr().out
    assert row.error == "refusal"


@pytest.mark.parametrize("stop, line", [
    ("max_tokens", "CV analysis failed: the answer was too long and was cut off (limit 32,000 tokens). Cost of the "
                   "attempt: 0.66 USD. Try again or shorten the CV."),
    ("refusal", "CV analysis failed: Claude refused the analysis. Cost of the attempt: 0.0020 USD. Check the text and "
                "try again."),
])
def test_a_cv_analysis_that_stops_early_says_why_with_the_limit_and_the_cost(tmp_path, capsys, stop, line):
    db = tmp_path / "s.sqlite"
    stopped = (answer('{"skills": [', stop_reason="max_tokens", usage=Usage(4657, 32000, 0, 0))
               if stop == "max_tokens" else refusal())
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=1)), "--db", str(db),
               "--yes"], client_factory=lambda: FakeClient([stopped]))
    out = capsys.readouterr().out
    assert rc == 5 and line in out
    assert Store(db).cost_summary().calls == 1  # the attempt was paid and is booked


def test_jobs_from_a_csv_file(tmp_path, capsys):
    jobs = tmp_path / "jobs.csv"
    jobs.write_text("text\n\"Test Automation Engineer CSV\nSelenium and Java required.\"\n", encoding="utf-8-sig")
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs), "--db", str(tmp_path / "s.sqlite"),
               "--yes"], client_factory=scripted_client)
    assert rc == 0 and "Test Automation Engineer CSV" in capsys.readouterr().out
    assert Store(tmp_path / "s.sqlite").postings()[0].refs == ["jobs.csv#1"]


# --- prompt sets ---

STEP_OF_SCHEMA = {"CvDraft": "CV", "JobDraft": "JOB", "RelationAnswer": "RELATION", "PathDraft": "PATH"}


@pytest.mark.parametrize("command", [["analyze", "--cv", "cv.md", "--jobs", "jobs"], ["eval"], ["demo", "build"]])
@pytest.mark.parametrize("through", ["flag", "environment", "env file"])
def test_a_prompt_folder_that_cannot_be_loaded_stops_the_command(tmp_path, monkeypatch, capsys, command, through):
    # never a silent fallback to the example prompts: results and costs would change without notice
    missing = tmp_path / "my_prompts"
    monkeypatch.setenv("SKILLPOSE_PROMPTS", str(missing) if through == "environment" else "")  # restored afterwards
    extra = ["--prompts", str(missing)] if through == "flag" else []
    if through == "env file":
        env = tmp_path / "x.env"
        env.write_text(f"SKILLPOSE_PROMPTS={missing}\n", encoding="utf-8")
        extra = ["--env-file", str(env)]
    rc = main([*command, *extra, "--db", str(tmp_path / "x.sqlite")], client_factory=fail_if_called)
    assert rc == 2 and capsys.readouterr().out == "prompt folder my_prompts: missing cv.md\n"
    assert not (tmp_path / "x.sqlite").exists()  # stopped before anything else


def test_analyze_prints_the_prompt_set_first_before_the_estimate_and_the_decision(tmp_path, capsys):
    # the label comes before anything that could be paid for: the user sees which prompts a run would use
    folder = prompt_folder(tmp_path / "mine")
    ask = ["--settings", str(settings_yaml(tmp_path, confirm_above_usd=0.01))]
    base = ["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=3)),
            "--db", str(tmp_path / "s.sqlite")]
    for extra, label in (([], "example"), (["--prompts", str(folder)], load_prompts(folder).label)):
        assert main([*base, *ask, *extra], client_factory=fail_if_called) == 3
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == f"prompt set: {label}" and lines[1].startswith("estimated cost")
    assert main([*base, "--yes", "--prompts", str(folder)], client_factory=scripted_client) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"prompt set: {load_prompts(folder).label}"


def test_the_prompts_flag_wins_over_the_environment_and_reaches_every_call(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLPOSE_PROMPTS", str(tmp_path / "missing"))
    client = ScriptedClient()
    rc = main(["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=1)), "--yes",
               "--prompts", str(prompt_folder(tmp_path / "mine")), "--db", str(tmp_path / "s.sqlite")],
              client_factory=lambda: client)
    systems = {call.path: call.kwargs["system"][0]["text"] for call in client.calls}
    assert rc == 0 and {"CvDraft", "JobDraft"} <= set(systems)
    assert {schema: text.splitlines()[0] for schema, text in systems.items()} == {
        schema: f"{STEP_OF_SCHEMA[schema]} PROMPT" for schema in systems}


def test_the_help_of_demo_dir_says_the_bundles_are_used_only_without_a_key(capsys):
    # with a key the app shows no demo, so the folder of bundles has no effect then
    with pytest.raises(SystemExit) as stop:
        main(["web", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert stop.value.code == 0
    assert "folder with demo bundles (default: the packaged demo; used only without a key)" in help_text


# --- no API key: one clear line and exit code 2, before any call and before any write ---

NO_KEY_LINE = "no API key: set ANTHROPIC_API_KEY or pass --env-file"


@pytest.fixture
def no_key(monkeypatch):
    """No key in the environment, and an SDK client that records every attempt to build or use it (a FailingClient)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")   # so that the undo of the next line, and of an env file, deletes it
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    attempts: list[str] = []

    def build(*args, **kwargs):
        attempts.append("anthropic.Anthropic()")
        return FailingClient("client", attempts)

    monkeypatch.setattr(anthropic, "Anthropic", build)
    return attempts


def keyless_commands(tmp_path) -> dict[str, list[str]]:
    gold = small_goldset(tmp_path)
    db = str(tmp_path / "x.sqlite")
    return {
        "analyze": ["analyze", "--cv", str(cv_file(tmp_path)), "--jobs", str(jobs_dir(tmp_path, n=2)), "--db", db,
                    "--yes"],
        "eval": ["eval", "--goldset", str(gold), "--db", db, "--save", "--yes"],
        "demo build": ["demo", "build", "--goldset", str(gold), "--db", db, "--out", str(tmp_path / "out"), "--yes"]}


@pytest.mark.parametrize("name", ["analyze", "eval", "demo build"])
def test_a_paid_command_without_a_key_stops_with_one_line_before_any_call_or_write(tmp_path, capsys, no_key, name):
    rc = main(keyless_commands(tmp_path)[name])   # no client factory: the command would build the SDK client
    out = capsys.readouterr().out.splitlines()
    assert rc == 2 and out == ["prompt set: example", NO_KEY_LINE]
    assert no_key == []                                              # the client was never built, so never called
    assert not (tmp_path / "x.sqlite").exists() and not (tmp_path / "out").exists()   # nothing a run writes


def test_the_check_comes_after_the_env_file_and_a_key_in_it_lets_the_command_go_on(tmp_path, capsys, monkeypatch,
                                                                                      no_key):
    env = tmp_path / "k.env"
    env.write_text("ANTHROPIC_API_KEY=sk-test-not-real\n", encoding="utf-8")
    monkeypatch.setattr(anthropic, "Anthropic", lambda: (_ for _ in ()).throw(RuntimeError("client built")))
    with pytest.raises(RuntimeError, match="client built"):   # past the check: the command reached the SDK client
        main([*keyless_commands(tmp_path)["analyze"], "--env-file", str(env)])
    assert NO_KEY_LINE not in capsys.readouterr().out


def test_a_blank_key_is_no_key_and_a_client_factory_brings_its_own(tmp_path, capsys, monkeypatch, no_key):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    argv = keyless_commands(tmp_path)["analyze"]
    assert main(argv) == 2 and NO_KEY_LINE in capsys.readouterr().out
    assert main(argv, client_factory=scripted_client) == 0   # a test's own client needs no key
    assert NO_KEY_LINE not in capsys.readouterr().out and no_key == []


def test_eval_repeat_without_a_key_stops_before_the_first_call_when_it_has_something_to_make(tmp_path, capsys, no_key):
    gold = small_goldset(tmp_path)
    settings = settings_yaml(tmp_path, rules="{min_jobs_per_role: 1}")
    argv = ["eval", "--repeat", "1", "--goldset", str(gold), "--db", str(tmp_path / "g.sqlite"),
            "--stability-db", str(tmp_path / "s.sqlite"), "--settings", str(settings), "--yes"]
    rc = main(argv)
    assert rc == 2 and capsys.readouterr().out.splitlines()[-1] == NO_KEY_LINE and no_key == []
    with Store(tmp_path / "s.sqlite") as st:       # the store of the runs is empty: no result, no cost row
        assert st.cost_rows() == []


def test_eval_repeat_with_nothing_to_make_needs_no_key(tmp_path, capsys, no_key):
    # the report of the stored runs makes no call, so it asks for no key (stability.py, StabilityPlan.calls)
    gold = small_goldset(tmp_path)
    rc = main(["eval", "--repeat", "0", "--goldset", str(gold), "--db", str(tmp_path / "missing.sqlite"),
               "--stability-db", str(tmp_path / "s.sqlite")])
    assert rc == 0 and NO_KEY_LINE not in capsys.readouterr().out and no_key == []
