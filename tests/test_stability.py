"""eval --repeat: the run-to-run variance of steps 1 and 2, and its note in the app (how much the answers vary).
No test calls the API: fake clients answer, and the paid measurement needs a deliberate call with a key."""

import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import (
    FakeBatchClient,
    ScriptedClient,
    api_error,
    connection_error,
    fail_if_called,
    ok,
    prompt_folder,
    refusal,
)
from test_evaluation import GOLDSET, settings_file, settings_min1, small_goldset
from webkit import make_client

from skillpose.cli import GOLDSET_DB, STABILITY_DB, _continue_line, build_parser, main
from skillpose.core.config import Stability, load_settings
from skillpose.core.llm import LLM, Usage, estimate_cv_usd, estimate_usd
from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    CandidateSkillDraft,
    CvDraft,
    Evidence,
    EvidenceDraft,
    JobDraft,
    JobProfile,
    RelationAnswer,
    RequirementDraft,
    text_hash,
)
from skillpose.core.prompts import use_prompts
from skillpose.core.textio import read_cv
from skillpose.core.vocabulary import Vocabulary
from skillpose.evaluation import read_expected, run_eval
from skillpose.stability import (
    CV_METRICS,
    POSTING_METRICS,
    CvRun,
    JobRun,
    compare_profiles,
    measure,
    run_stability,
    stability_plan,
    tallies,
)
from skillpose.store.db import BatchCost, StabilityStore, Store

V = Vocabulary.load()
QUOTE = "Built a Selenium test automation framework in Java"
ALL = {"cv", "job", "match"}


def skill_draft(skill_id: str, level: str, strength: str) -> CandidateSkillDraft:
    evidence = EvidenceDraft(quote=QUOTE, context="Acme", period_from=2016, period_to=None, strength=strength)
    return CandidateSkillDraft(skill_id=skill_id, name=skill_id, category="tool", level=level, years=10,
                               evidence=[evidence])


def draft(*skills: tuple[str, str, str]) -> CvDraft:
    """A CV answer with these (skill id, level, strongest evidence) and one quote each."""
    return CvDraft(language="en", stations=[], skills=[skill_draft(*s) for s in skills])


# The base (cv_draft: Selenium expert, Java strong, both with an outcome) and two new runs of the same CV.
RUN_1 = draft(("selenium", "expert", "outcome"), ("java", "working", "claim"), ("docker", "basic", "claim"))
RUN_2 = draft(("selenium", "strong", "activity"), ("java", "strong", "outcome"))


def requirement(skill_id: str, importance: str = "must", min_level: str | None = None) -> RequirementDraft:
    return RequirementDraft(skill_id=skill_id, name=skill_id, category="tool", importance=importance,
                            min_level=min_level, min_years=None, kind="tool", quote=skill_id)


# A new run of posting 00: Java now asks for expert level, and Docker is a must instead of Kubernetes as a nice.
POSTING_RUN = JobDraft(title="Test Automation Engineer 0", role_family="test_automation", seniority="mid",
                       requirements=[requirement("selenium"), requirement("java", min_level="expert"),
                                     requirement("docker")])


def profile(*skills: tuple[str, str, str]) -> CandidateProfile:
    return CandidateProfile(language="en", stations=[], skills=[
        CandidateSkill(skill_id=s, level=level, years=1, evidence=[] if strength is None else [
            Evidence(quote="q", context="c", period_from=None, period_to=None, strength=strength)])
        for s, level, strength in skills])


def gold_store(tmp_path: Path, gold: Path, settings=None) -> Path:
    """The gold-set store as `eval --save` leaves it: the base run of every CV and posting."""
    settings = settings or settings_min1()
    db = tmp_path / "goldset.sqlite"
    with Store(db) as st:
        run_eval(LLM(ScriptedClient(), settings, st), gold, V, settings, ALL, "auto", store=st)
    return db


def stability(tmp_path, gold, client, *, repeat=2, repeat_jobs=1, base=True, settings=None, **kwargs):
    """run_stability against the store of gold_store (opened read-only) and data/stability.sqlite in tmp_path."""
    settings = settings or settings_min1()
    base_db = tmp_path / "goldset.sqlite"
    with StabilityStore(tmp_path / "stability.sqlite") as store:
        base_store = Store(base_db, read_only=True) if base else None
        try:
            return run_stability(LLM(client, settings, store), gold, V, settings, store, base_store, repeat=repeat,
                                 repeat_jobs=repeat_jobs, lang="auto", sleep=lambda s: None, **kwargs)
        finally:
            if base_store is not None:
                base_store.close()


def values(tally_by_metric) -> dict:
    return {name: (t.same, t.total) for name, t in tally_by_metric.items()}


# --- the metrics of one pair ---

def test_skill_set_level_and_evidence_of_a_pair():
    a = profile(("selenium", "expert", "outcome"), ("java", "strong", "activity"), ("sql", "basic", None))
    b = profile(("selenium", "working", "outcome"), ("java", "strong", "claim"), ("docker", "basic", "claim"))
    tally = tallies(CV_METRICS)
    compare_profiles(a, b, tally)
    assert tally["skill_jaccard"].value == pytest.approx(2 / 4)       # Selenium and Java of four skills
    assert values(tally)["level_same"] == (1, 2)                        # Java the same, Selenium two steps away
    assert values(tally)["level_within_one"] == (1, 2)
    assert values(tally)["evidence_same"] == (1, 2)                     # Selenium outcome = outcome, Java not


def test_two_empty_profiles_agree_and_compare_no_level():
    tally = tallies(CV_METRICS)
    compare_profiles(profile(), profile(), tally)
    assert tally["skill_jaccard"].value == 1.0 and tally["level_same"].value is None


def test_measure_holds_one_side_still_and_counts_every_pair():
    """Three runs of one CV (3 pairs) against one posting run, and two posting runs against the first CV run."""
    from skillpose.core.extract_job import job_from_draft

    settings = settings_min1()
    text = "Test Automation Engineer 0\nSelenium and Java required."
    base_jobs = {"p": job_from_draft(JobDraft(title="t", role_family="test_automation", seniority="mid",
                                              requirements=[requirement("selenium"), requirement("java")]),
                                     text, V, settings.role_families)}
    other_jobs = {"p": job_from_draft(POSTING_RUN, text, V, settings.role_families)}
    runs = [CvRun("2026-10-21T10:00:00", profile(("selenium", "expert", "outcome"), ("java", "strong", "outcome")),
                  base=True),
            CvRun("2026-10-25T10:00:00", profile(("selenium", "expert", "outcome"), ("java", "strong", "claim"))),
            CvRun("2026-10-25T10:05:00", profile(("selenium", "expert", "outcome"), ("java", "strong", "outcome")))]
    jobs = [JobRun("2026-10-21T10:00:00", base_jobs, base=True), JobRun("2026-10-25T10:00:00", other_jobs)]
    relations = {("docker", "java"): "none", ("docker", "selenium"): "none"}
    m = measure({"person": runs}, jobs, V, relations, settings.rules, 2026)
    cv = values(m.cv["person"])
    assert cv["status_same"] == (4, 6)        # Java only listed in run 2: partial against met, in 2 of 3 pairs
    assert cv["posting_tier_same"] == (1, 3) and cv["role_tier_same"] == (1, 3)
    postings = values(m.postings["person"])
    assert postings["status_same"] == (1, 2)  # Selenium met twice; Java met, then partial (expert asked)
    assert postings["posting_tier_same"] == (0, 1)
    assert m.posting_only["requirement_jaccard"].value == pytest.approx(2 / 3)
    assert values(m.posting_only)["role_family_same"] == (1, 1)


# --- run_stability with the scripted client ---

def test_varying_answers_give_the_expected_metrics(tmp_path):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    client = ScriptedClient(queued={"CvDraft": [RUN_1, RUN_2], "JobDraft": [POSTING_RUN]})
    report = stability(tmp_path, gold, client)
    assert [c.path for c in client.calls].count("CvDraft") == 2      # new calls, although the base has a profile
    assert [c.path for c in client.calls].count("JobDraft") == 2     # one new run of the two postings
    assert [c.path for c in client.calls].count("RelationAnswer") == 2   # the new pairs only: Docker
    assert [len(runs) for runs in report.cv_runs.values()] == [3] and len(report.job_runs) == 2
    cv = report.measurement.cv["person_0"]
    assert cv["skill_jaccard"].value == pytest.approx(7 / 9) and cv["skill_jaccard"].total == 3
    assert values(cv) | {"skill_jaccard": None} == {
        "skill_jaccard": None,
        "level_same": (2, 6), "level_within_one": (6, 6), "evidence_same": (2, 6),
        "status_same": (14, 18),          # 3 pairs x 2 postings x 3 requirements; Java partial in run 1
        "posting_tier_same": (2, 6), "role_tier_same": (1, 3), "main_gap_same": (1, 3)}
    assert values(report.measurement.postings["person_0"]) == {
        "status_same": (4, 5), "posting_tier_same": (1, 2), "role_tier_same": (1, 1), "main_gap_same": (0, 1)}
    only = report.measurement.posting_only
    assert only["requirement_jaccard"].value == pytest.approx(0.75) and values(only)["role_family_same"] == (2, 2)


def test_the_gold_set_store_does_not_change_and_the_runs_go_to_the_stability_store(tmp_path):
    gold = small_goldset(tmp_path)
    base = gold_store(tmp_path, gold)
    before = base.read_bytes()
    stability(tmp_path, gold, ScriptedClient(queued={"CvDraft": [RUN_1, RUN_2]}))
    assert base.read_bytes() == before
    with StabilityStore(tmp_path / "stability.sqlite") as st:
        assert [run.kind for run in st.runs()] == ["cv", "cv", "job"]
        assert len(st.cost_rows()) == 2 + 2 + 1   # two CV runs, one run of two postings, one relation (Docker)
        assert st.cvs() == [] and st.postings() == []   # a run is no CV and no posting of the store
    with Store(base) as st:
        assert len(st.cost_rows()) == 4   # the costs of the eval only


def test_n_is_a_target_the_same_command_makes_nothing_again_and_a_larger_one_adds_runs(tmp_path):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    stability(tmp_path, gold, ScriptedClient(queued={"CvDraft": [RUN_1, RUN_2]}), repeat_jobs=0)
    again = ScriptedClient()
    report = stability(tmp_path, gold, again, repeat=2, repeat_jobs=0)
    assert [c.path for c in again.calls] == []   # both runs are stored: nothing to pay, the report again
    assert [len(runs) for runs in report.cv_runs.values()] == [3]
    more = ScriptedClient()
    report = stability(tmp_path, gold, more, repeat=3, repeat_jobs=0)
    assert [c.path for c in more.calls].count("CvDraft") == 1
    assert [len(runs) for runs in report.cv_runs.values()] == [4]   # base, two earlier runs, this one
    assert report.measurement.cv["person_0"]["skill_jaccard"].total == 6


def test_runs_with_other_prompts_do_not_count(tmp_path):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    stability(tmp_path, gold, ScriptedClient(), repeat_jobs=0)
    use_prompts(prompt_folder(tmp_path / "mine"))
    report = stability(tmp_path, gold, ScriptedClient(), repeat=1, repeat_jobs=0)
    assert [len(runs) for runs in report.cv_runs.values()] == [2]   # base and the run with these prompts
    assert report.prompt_set.startswith("private:")


def test_without_the_base_only_the_new_runs_count(tmp_path):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    report = stability(tmp_path, gold, ScriptedClient(queued={"CvDraft": [RUN_1, RUN_2]}), repeat_jobs=2,
                       base=False)
    assert [len(runs) for runs in report.cv_runs.values()] == [2] and len(report.job_runs) == 2
    assert not any(run.base for runs in report.cv_runs.values() for run in runs)
    assert values(report.measurement.cv["person_0"])["level_same"] == (0, 2)


def test_a_base_made_by_another_model_is_left_out_with_a_note(tmp_path):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    settings = settings_min1()
    settings.steps["cv"].model = "claude-sonnet-5-5"
    report = stability(tmp_path, gold, ScriptedClient(), repeat=2, repeat_jobs=0, settings=settings)
    assert [len(runs) for runs in report.cv_runs.values()] == [2]
    assert any("person_0" in note and "claude-opus-5-5" in note for note in report.notes)


def test_a_call_without_a_result_is_counted_and_gives_no_run(tmp_path):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    report = stability(tmp_path, gold, ScriptedClient(queued={"CvDraft": [refusal(), RUN_1]}), repeat_jobs=0)
    assert report.failures == 1 and [len(runs) for runs in report.cv_runs.values()] == [2]
    assert "Calls without a result in this command: 1" in report.to_markdown()


def test_the_postings_go_again_as_one_batch_and_the_cvs_meanwhile(tmp_path):
    settings = load_settings()
    gold_store(tmp_path, GOLDSET, settings)
    texts = {name: (GOLDSET / "postings" / f"{name}.md").read_text(encoding="utf-8")
             for name in read_expected(GOLDSET)["postings"]}
    again = JobDraft(title="t", role_family="test_automation", seniority="mid",
                     requirements=[requirement("selenium"), requirement("java"), requirement("docker", "nice")])
    client = ScriptedClient()
    batches = FakeBatchClient({text_hash(t): ("succeeded", again.model_dump_json()) for t in texts.values()},
                              ["in_progress", "ended"], log=client.calls)
    client.messages.batches = batches.messages.batches
    report = stability(tmp_path, GOLDSET, client, repeat=1, repeat_jobs=1, settings=settings)
    order = [c.path for c in client.calls]
    assert order[0] == "batches.create" and order.count("CvDraft") == 3 and "JobDraft" not in order
    assert batches.batches == {"batch_1": [text_hash(t) for t in texts.values()]}
    assert batches.retrieved == ["batch_1", "batch_1"]   # still running, then ended
    with StabilityStore(tmp_path / "stability.sqlite") as st:
        assert st.pending_batches() == []   # collected and finished
        [job_run] = [run for run in st.runs() if run.kind == "job"]
        assert len(job_run.results) == 30
    assert len(report.job_runs) == 2 and all(len(run.jobs) == 30 for run in report.job_runs)
    only = report.measurement.posting_only
    assert only["requirement_jaccard"].value == pytest.approx(0.5) and only["requirement_jaccard"].total == 30


def test_a_batch_without_its_configuration_is_collected_but_does_not_count(tmp_path):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    texts = [(gold / "postings" / f"{i:02d}_test_automation.md").read_text(encoding="utf-8") for i in range(2)]
    with StabilityStore(tmp_path / "stability.sqlite") as st:
        st.save_batch("batch_old", [text_hash(t) for t in texts])   # not sent by eval --repeat: no configuration
    client = ScriptedClient()
    batches = FakeBatchClient({text_hash(t): ("succeeded", POSTING_RUN.model_dump_json()) for t in texts}, ["ended"])
    client.messages.batches = batches.messages.batches
    report = stability(tmp_path, gold, client, repeat=0, repeat_jobs=1)
    assert batches.batches == {}   # collected (paid already), never sent again
    assert [c.path for c in client.calls].count("JobDraft") == 2   # the run of this configuration is still made
    with StabilityStore(tmp_path / "stability.sqlite") as st:
        assert st.pending() == [] and sorted(run.config for run in st.runs("job"))[-1] == "unknown"
    assert len(report.job_runs) == 2   # the base and the new run; the unknown one does not count


def test_a_pending_batch_counts_only_for_its_configuration(tmp_path):
    with StabilityStore(tmp_path / "s.sqlite") as st:
        st.save_pending_batch("batch_a", ["h1", "h2"], "config a")
        st.save_batch("batch_b", ["h3"])
        assert [(b.id, b.hashes, b.config) for b in st.pending()] == [("batch_a", ["h1", "h2"], "config a"),
                                                                       ("batch_b", ["h3"], None)]


def test_a_cv_run_answered_by_a_fallback_model_is_stored_but_does_not_count(tmp_path):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    fallback = ok(RUN_1, model="claude-opus-5")
    report = stability(tmp_path, gold, ScriptedClient(queued={"CvDraft": [fallback, RUN_2]}), repeat_jobs=0)
    assert [len(runs) for runs in report.cv_runs.values()] == [2]   # the base and RUN_2
    assert any("person_0" in n and "claude-opus-5 (1)" in n and "left out" in n for n in report.notes)
    assert any("1 of 2 new runs count" in n for n in report.notes)
    with StabilityStore(tmp_path / "stability.sqlite") as st:
        assert sorted(model for run in st.runs("cv") for model, _ in run.results.values()) == [
            "claude-opus-5", "claude-opus-5-5"]
    again = ScriptedClient()
    stability(tmp_path, gold, again, repeat=2, repeat_jobs=0)
    assert [c.path for c in again.calls].count("CvDraft") == 1   # the fallback run is replaced, once


def test_a_status_request_that_fails_for_a_while_is_asked_again(tmp_path):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    settings = settings_min1().model_copy(update={"batch_threshold": 2})
    texts = [(gold / "postings" / f"{i:02d}_test_automation.md").read_text(encoding="utf-8") for i in range(2)]
    client = ScriptedClient()
    batches = FakeBatchClient({text_hash(t): ("succeeded", POSTING_RUN.model_dump_json()) for t in texts},
                              [connection_error(), api_error(529), "in_progress", "ended"])
    client.messages.batches = batches.messages.batches
    said, slept = [], []
    report = stability(tmp_path, gold, client, repeat=0, repeat_jobs=1, settings=settings, say=said.append,
                       poll_seconds=7.0)
    assert len(batches.retrieved) == 4 and len(report.job_runs) == 2
    assert sum("asking again in 7 s" in line for line in said) == 2
    for error in (api_error(401), api_error(400)):   # not an error that passes: it stops, the batch stays open
        (tmp_path / "other").mkdir(exist_ok=True)
        client = ScriptedClient()
        client.messages.batches = FakeBatchClient({}, [error]).messages.batches
        with StabilityStore(tmp_path / "other" / "s.sqlite") as st:
            st.save_pending_batch("batch_x", [text_hash(texts[0])], "config")
            with pytest.raises(type(error)):
                run_stability(LLM(client, settings, st), gold, V, settings, st, None, repeat=0, repeat_jobs=0,
                              lang="auto", sleep=slept.append)
            assert [b.id for b in st.pending()] == ["batch_x"]
            st.finish_batch("batch_x")


def test_a_batch_is_booked_stored_and_finished_in_one_transaction(tmp_path, monkeypatch):
    profile_value = JobProfile(text_hash="h1", title="t", role_family="other", seniority="mid", requirements=[])
    costs = [BatchCost(step="job", model="claude-sonnet-5-5", usage=Usage(10, 5, 0, 0), usd=0.25)]
    def crash(*args):
        raise RuntimeError("crash between the booking and the run")

    with StabilityStore(tmp_path / "s.sqlite") as st:
        st.save_pending_batch("batch_1", ["h1"], "config")
        monkeypatch.setattr(st, "_insert_run", crash)
        with pytest.raises(RuntimeError):
            st.save_batch_run("batch_1", "config", {"h1": ("claude-sonnet-5-5", profile_value)}, costs)
        assert st.cost_rows() == [] and st.runs() == [] and [b.id for b in st.pending()] == ["batch_1"]
        monkeypatch.undo()
        st.save_batch_run("batch_1", "config", {"h1": ("claude-sonnet-5-5", profile_value)}, costs)
        assert [r.usd for r in st.cost_rows()] == [0.25] and st.cost_rows()[0].batch
        assert len(st.runs("job", "config")) == 1 and st.pending() == []


# --- a command that stops in the middle, and the same command again ---

def interrupted(tmp_path, capsys, argv, client, error):
    """main(argv) with the client, stopped by error (KeyboardInterrupt or an exception); returns what it printed."""
    with pytest.raises(error):
        main(argv, client_factory=lambda: client)
    return capsys.readouterr().out


def repeat_argv(tmp_path, gold, settings, *extra):
    return ["eval", "--repeat", "2", *extra, "--goldset", str(gold), "--db", str(tmp_path / "goldset.sqlite"),
            "--stability-db", str(tmp_path / "stability.sqlite"), "--settings", str(settings), "--yes"]


def test_stopped_in_the_cv_runs_the_same_command_pays_only_the_missing_run(tmp_path, capsys):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    argv = repeat_argv(tmp_path, gold, settings_file(tmp_path, "rules:\n  min_jobs_per_role: 1\n"))
    out = interrupted(tmp_path, capsys, argv, ScriptedClient(queued={"CvDraft": [RUN_1, KeyboardInterrupt()]}),
                      KeyboardInterrupt)
    assert "USD for 2 new CV runs (target 2 per CV) and at most 40 relation checks (an upper bound)" in out
    assert "to continue, run the same command again" in out and "skillpose eval --repeat 2 --goldset" in out
    client = ScriptedClient(queued={"CvDraft": [RUN_2]})
    assert main(argv, client_factory=lambda: client) == 0
    out = capsys.readouterr().out
    # the missing run, and the one relation check that the stored run 1 needs (Docker for Kubernetes)
    assert "for 1 new CV run (target 2 per CV) and at most 21 relation checks" in out
    assert [c.path for c in client.calls].count("CvDraft") == 1
    assert "| CV person_0 | 3 | 3 |" in out
    with StabilityStore(tmp_path / "stability.sqlite") as st:
        assert len(st.runs("cv")) == 2 and [r.step for r in st.cost_rows()].count("cv") == 2


def test_stopped_while_waiting_for_the_batch_the_same_command_only_collects_it(tmp_path, capsys):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    settings = settings_file(tmp_path, "batch_threshold: 2\nrules:\n  min_jobs_per_role: 1\n")
    argv = repeat_argv(tmp_path, gold, settings, "--repeat-jobs", "1")
    texts = [(gold / "postings" / f"{i:02d}_test_automation.md").read_text(encoding="utf-8") for i in range(2)]
    results = {text_hash(t): ("succeeded", POSTING_RUN.model_dump_json()) for t in texts}
    first = ScriptedClient(queued={"CvDraft": [RUN_1, RUN_2]})
    sent = FakeBatchClient(results, [KeyboardInterrupt()])
    first.messages.batches = sent.messages.batches
    out = interrupted(tmp_path, capsys, argv, first, KeyboardInterrupt)
    assert list(sent.batches) == ["batch_1"] and "to continue, run the same command again" in out
    second = ScriptedClient()
    collected = FakeBatchClient(results, ["ended"])
    second.messages.batches = collected.messages.batches
    assert main(argv, client_factory=lambda: second) == 0
    out = capsys.readouterr().out
    # the pending batch costs nothing more, but its run brings pairs to check (20), and run 1 has Docker open (1)
    assert ("for 0 new CV runs (target 2 per CV), 0 new runs of the 2 postings as a batch (target 1) and at most 21 "
            "relation checks") in out
    assert collected.batches == {} and collected.retrieved == ["batch_1"]   # collected, not sent again
    assert not {"CvDraft", "JobDraft"} & {c.path for c in second.calls}
    assert "| postings (2) | 2 | 1 |" in out
    # once more: nothing to make, so no client (and no key) is needed, and the report comes again
    assert main(argv, client_factory=fail_if_called) == 0
    out = capsys.readouterr().out
    assert "estimated cost 0.00 USD for 0 new CV runs" in out and "| postings (2) | 2 | 1 |" in out


def test_stopped_in_the_relation_checks_the_same_command_asks_only_the_open_ones(tmp_path):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    settings = settings_min1().model_copy(update={"relation_workers": 1})
    stop = RuntimeError("stopped in step 3b")
    first = ScriptedClient(queued={"CvDraft": [RUN_1], "JobDraft": [POSTING_RUN],
                                   "RelationAnswer": [RelationAnswer(related=[]), stop]})
    with pytest.raises(RuntimeError):
        stability(tmp_path, gold, first, repeat=1, repeat_jobs=1, settings=settings)
    with StabilityStore(tmp_path / "stability.sqlite") as st:
        plan = stability_plan(settings, gold, st, None, V, 1, 1, "auto")
        assert (plan.cv_new, plan.jobs_missing) == (0, 0)
    with StabilityStore(tmp_path / "stability.sqlite") as st, Store(tmp_path / "goldset.sqlite", read_only=True) as b:
        plan = stability_plan(settings, gold, st, b, V, 1, 1, "auto")
    assert plan.relation_calls == 1   # Docker was answered and stored; Kubernetes is open
    assert plan.usd == pytest.approx(estimate_usd(settings, "relation", 1, avg_in=300, avg_out=300,
                                                  fixed_tokens=6000))
    second = ScriptedClient()
    stability(tmp_path, gold, second, repeat=1, repeat_jobs=1, settings=settings)
    assert [c.path for c in second.calls] == ["RelationAnswer"]


def test_the_monthly_limit_mid_run_stops_with_the_command_that_continues(tmp_path, capsys):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    argv = repeat_argv(tmp_path, gold, settings_file(tmp_path, "monthly_limit_usd: 0.5\n"))
    client = ScriptedClient(cv_usage=Usage(1000, 50000, 0, 0))   # one answer costs more than the estimate: 1 USD
    assert main(argv, client_factory=lambda: client) == 4
    out = capsys.readouterr().out
    assert "monthly limit of 0.50 USD reached: stopped" in out and "to continue, run the same command again" in out
    assert [c.path for c in client.calls].count("CvDraft") == 1   # the second run was not sent


def test_an_api_error_stops_with_the_command_that_continues(tmp_path, capsys):
    gold = small_goldset(tmp_path)
    gold_store(tmp_path, gold)
    settings = settings_file(tmp_path, "batch_threshold: 2\n")
    argv = repeat_argv(tmp_path, gold, settings, "--repeat-jobs", "1")
    client = ScriptedClient()
    client.messages.batches = FakeBatchClient({}, [api_error(401)]).messages.batches
    assert main(argv, client_factory=lambda: client) == 5
    out = capsys.readouterr().out
    assert "stopped by an API error (APIStatusError" in out and "to continue, run the same command again" in out


@pytest.mark.parametrize("stability_db", ["goldset.sqlite", "data/skillpose.sqlite"])
def test_the_stability_store_must_be_a_file_of_its_own(tmp_path, capsys, stability_db):
    path = tmp_path / stability_db if stability_db == "goldset.sqlite" else Path(stability_db)
    rc = main(["eval", "--repeat", "1", "--goldset", str(GOLDSET), "--db", str(tmp_path / "goldset.sqlite"),
               "--stability-db", str(path)], client_factory=fail_if_called)
    assert rc == 2 and "must be a file of its own" in capsys.readouterr().out
    assert not path.exists() and not list(tmp_path.iterdir())


# --- the estimate ---

def test_the_estimate_covers_every_missing_call(tmp_path):
    settings = load_settings()
    cvs = [read_cv(GOLDSET / "cvs" / f"{key}.md") for key in read_expected(GOLDSET)["cvs"]]
    with StabilityStore(tmp_path / "s.sqlite") as st:
        plan = stability_plan(settings, GOLDSET, st, None, V, 4, 1, "auto")
    cv = 4 * sum(estimate_cv_usd(settings, len(text)) for text in cvs)
    jobs = estimate_usd(settings, "job", 30, batch=True)
    relations = estimate_usd(settings, "relation", (4 + 1) * 3 * 20, avg_in=300, avg_out=300, fixed_tokens=6000)
    assert (plan.n_cvs, plan.cv_new, plan.jobs_missing, plan.relation_calls) == (3, 12, 1, 300)
    assert (plan.n_postings, plan.batch) == (30, True) and plan.usd == pytest.approx(cv + jobs + relations)


def test_below_the_batch_threshold_the_postings_are_estimated_as_single_calls(tmp_path):
    settings = load_settings()
    with StabilityStore(tmp_path / "s.sqlite") as st:
        plan = stability_plan(settings, small_goldset(tmp_path), st, None, V, 0, 2, "auto")
    relations = estimate_usd(settings, "relation", 2 * 20, avg_in=300, avg_out=300, fixed_tokens=6000)
    assert not plan.batch and plan.usd == pytest.approx(2 * estimate_usd(settings, "job", 2) + relations)


# --- the command ---

def test_eval_repeat_defaults():
    args = build_parser().parse_args(["eval"])
    assert (args.repeat, args.repeat_jobs, args.stability_db, args.no_base, args.db) == (
        None, 0, STABILITY_DB, False, GOLDSET_DB)
    assert Path("data/stability.sqlite") == STABILITY_DB


def test_eval_repeat_prints_the_prompt_set_then_the_estimate_and_needs_yes(tmp_path, capsys):
    rc = main(["eval", "--repeat", "4", "--repeat-jobs", "1", "--goldset", str(GOLDSET),
               "--db", str(tmp_path / "goldset.sqlite"), "--stability-db", str(tmp_path / "stability.sqlite")],
              client_factory=fail_if_called)
    with StabilityStore(tmp_path / "plan.sqlite") as st:
        usd = stability_plan(load_settings(), GOLDSET, st, None, V, 4, 1, "auto").usd
    lines = capsys.readouterr().out.splitlines()
    assert rc == 3 and lines[0] == "prompt set: example"
    assert lines[1] == (f"estimated cost {usd:.2f} USD for 12 new CV runs (target 4 per CV), 1 new run of the 30 "
                        "postings as a batch (target 1) and at most 300 relation checks: above 0.50 USD - run again "
                        "with --yes")
    assert lines[2].startswith("no stored result in")
    assert not (tmp_path / "goldset.sqlite").exists()   # the gold-set store is only read, never created


def test_eval_repeat_stops_at_the_monthly_limit_before_the_client(tmp_path, capsys):
    settings = settings_file(tmp_path, "monthly_limit_usd: 0.01\n")
    rc = main(["eval", "--repeat", "1", "--goldset", str(GOLDSET), "--settings", str(settings),
               "--db", str(tmp_path / "g.sqlite"), "--stability-db", str(tmp_path / "s.sqlite"), "--yes"],
              client_factory=fail_if_called)
    assert rc == 4 and "monthly limit" in capsys.readouterr().out


def test_a_gold_set_store_of_an_older_version_names_the_free_command_that_opens_it(tmp_path, capsys):
    db = tmp_path / "goldset.sqlite"
    Store(db).close()
    sqlite3.connect(db).execute("PRAGMA user_version = 4").connection.close()  # a store of the last version
    rc = main(["eval", "--repeat", "1", "--goldset", str(GOLDSET), "--db", str(db),
               "--stability-db", str(tmp_path / "s.sqlite")], client_factory=fail_if_called)
    out = capsys.readouterr().out
    assert rc == 2 and f'skillpose vocab --db "{db}"' in out and "no API call" in out
    assert not (tmp_path / "s.sqlite").exists()
    assert main(["vocab", "--db", str(db)], client_factory=fail_if_called) == 0  # the command it names opens the file
    with Store(db, read_only=True):
        pass


def test_eval_repeat_does_not_go_with_save(tmp_path, capsys):
    rc = main(["eval", "--repeat", "1", "--save", "--goldset", str(GOLDSET), "--db", str(tmp_path / "g.sqlite"),
               "--stability-db", str(tmp_path / "s.sqlite")], client_factory=fail_if_called)
    assert rc == 2 and "--save" in capsys.readouterr().out and not list(tmp_path.iterdir())


def json_lines(markdown: str) -> list[dict]:
    block = markdown[markdown.index("```jsonl\n") + len("```jsonl\n"):]
    return [json.loads(line) for line in block[:block.index("```")].splitlines()]


def test_eval_repeat_writes_the_report_with_tables_formulas_and_json_lines(tmp_path, capsys):
    gold = small_goldset(tmp_path)
    settings = settings_file(tmp_path, "rules:\n  min_jobs_per_role: 1\n  role_tier_share: 0.5\n")
    base = tmp_path / "goldset.sqlite"
    assert main(["eval", "--goldset", str(gold), "--db", str(base), "--settings", str(settings), "--save"],
                client_factory=ScriptedClient) == 0
    before = base.read_bytes()
    capsys.readouterr()
    out = tmp_path / "results" / "stability.md"
    client = ScriptedClient(queued={"CvDraft": [RUN_1, RUN_2], "JobDraft": [POSTING_RUN]})
    rc = main(["eval", "--repeat", "2", "--repeat-jobs", "1", "--goldset", str(gold), "--db", str(base),
               "--stability-db", str(tmp_path / "stability.sqlite"), "--settings", str(settings), "--out", str(out),
               "--yes"], client_factory=lambda: client)
    printed = capsys.readouterr().out
    assert rc == 0 and base.read_bytes() == before
    assert printed.splitlines()[0] == "prompt set: example"
    assert printed.splitlines()[1].startswith("estimated cost 0.4")
    text = out.read_text(encoding="utf-8")
    assert text in printed and "\nPrompt set: example\n" in text
    # the rules that decide the tiers belong to the numbers (the header and the JSON lines say it)
    rules = "min_jobs_per_role 1, role_tier_share 0.5, tier1_coverage 0.8, tier2_max_weeks 26"
    assert f"\nTier rules: {rules}\n" in text
    for heading in ("## Runs", "## The same CV again", "## The same postings again", "## How the numbers are computed",
                    "## Summary (JSON lines)"):
        assert f"\n{heading}\n" in text, heading
    assert "| CV person_0 | 3 | 3 |" in text and "| postings (2) | 2 | 1 |" in text
    assert "| level the same | 0.33 (2 of 6) | 0.33 (2 of 6) |" in text
    assert "| skill set (mean Jaccard) | 0.78 (mean of 3) | 0.78 (mean of 3) |" in text
    for name, (_, label, formula) in {**CV_METRICS, **POSTING_METRICS}.items():
        assert f"- **{label}**: {formula}" in text, name
    lines = {(line["source"], line["metric"]): line for line in json_lines(text)}
    assert set(lines) == {("cv", m) for m in CV_METRICS} | {("postings", m) for m in POSTING_METRICS}
    level = lines[("cv", "level_same")]
    assert (level["value"], level["same"], level["total"], level["runs"]) == (0.3333, 2, 6, {"person_0": 3})
    assert level["prompt_set"] == "example" and re.fullmatch(r"\d{4}-\d\d-\d\d", level["first_run"])
    assert level["tier_rules"] == {"min_jobs_per_role": 1, "role_tier_share": 0.5, "tier1_coverage": 0.8,
                                   "tier2_max_weeks": 26}
    assert lines[("cv", "skill_jaccard")]["pairs"] == 3 and lines[("postings", "role_tier_same")]["runs"] == 2


def test_the_continue_line_does_not_promise_that_nothing_is_paid_twice():
    # a call interrupted mid-answer is paid but gives no run, so the next command makes it again
    line = _continue_line(SimpleNamespace(argv=["eval", "--repeat", "2"]))
    assert line.startswith("to continue, run the same command again (it makes only what is missing: ")
    assert ("nothing finished is paid twice, a call interrupted mid-answer is paid once and made again): "
            "skillpose eval --repeat 2") in line
    assert "nothing paid is paid again" not in line


def test_eval_repeat_without_any_pair_says_so(tmp_path, capsys):
    gold = small_goldset(tmp_path)
    rc = main(["eval", "--repeat", "1", "--goldset", str(gold), "--db", str(tmp_path / "missing.sqlite"),
               "--stability-db", str(tmp_path / "s.sqlite")], client_factory=ScriptedClient)
    text = capsys.readouterr().out
    assert rc == 0 and "| skill set (mean Jaccard) | – | – |" in text
    assert "no stored result in" in text and json_lines(text)[0]["value"] is None


# --- the stores ---

def test_a_store_opened_read_only_reads_and_cannot_write(tmp_path):
    db = tmp_path / "g.sqlite"
    with Store(db) as st:
        st.save_cv("a.md", "text")
    before = db.read_bytes()
    with Store(db, read_only=True) as st:
        assert st.find_cv("text") == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            st.save_cv("b.md", "other")
    assert db.read_bytes() == before
    with pytest.raises(FileNotFoundError):
        Store(tmp_path / "missing.sqlite", read_only=True)
    assert not (tmp_path / "missing.sqlite").exists()
    sqlite3.connect(db).execute("PRAGMA user_version = 3").connection.close()
    with pytest.raises(ValueError, match="version 3"):
        Store(db, read_only=True)


def test_only_the_stability_store_has_the_run_tables(tmp_path):
    with StabilityStore(tmp_path / "s.sqlite") as st:
        run_id = st.save_run("cv", "config a", {"h1": ("claude-opus-5-5", CandidateProfile(
            language="en", skills=[], stations=[]))})
        st.save_run("cv", "config b", {"h1": ("claude-opus-5-5", CandidateProfile(language="de", skills=[],
                                                                                    stations=[]))})
        [run] = st.runs("cv", "config a")
        assert run.id == run_id and run.config == "config a" and set(run.results) == {"h1"}
        assert CandidateProfile.model_validate_json(run.results["h1"][1]).language == "en"
        assert [r.config for r in st.runs()] == ["config a", "config b"]
    with Store(tmp_path / "own.sqlite") as st:
        tables = {row[0] for row in st.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {name for name in tables if name.startswith("stability")}


def test_job_profile_created_is_the_time_of_the_job_profile(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        h = st.save_posting("Posting text")
        assert st.job_profile_created(h) is None
        st.save_job_profile(JobProfile(text_hash=h, title="t", role_family="other", seniority="mid",
                                       requirements=[]), "fake")
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", st.job_profile_created(h))


# --- the note in the app ---

MEASURED = Stability(level_same=0.88, role_tier_same=1.0, runs=5, min_jobs=3)  # 3, not the default: it is passed on
NOTES = {"de": "Zwei Analysen desselben Lebenslaufs: 88 % der Niveaus gleich, Rollen-Stufen in 100 % gleich "
               "(gemessen mit 5 Läufen am Goldsatz, Rollen mit mindestens 3 Inseraten).",
         "en": "Two analyses of the same CV: 88 % of the skill levels the same, role tiers the same in 100 % "
               "(measured with 5 runs on the gold set, roles with at least 3 postings)."}


def test_the_defaults_carry_the_measured_stability_and_a_settings_file_can_replace_it(tmp_path):
    # the numbers of the report evals/results/2026-10-stability.md (JSON lines of source "cv"); the role
    # tiers were measured when a role needed 5 postings (min_jobs), the rules now ask for 1; and with the rules of
    # matches and tiers before 2026-10-25 (degree ranking, soft skills and conditions came after: rules_before)
    assert load_settings().stability == Stability(level_same=0.932, role_tier_same=1.0, runs=5, min_jobs=5,
                                                  rules_before=date(2026, 10, 25))
    assert load_settings().rules.min_jobs_per_role == 1
    path = settings_file(tmp_path, "stability: {level_same: 0.88, role_tier_same: 1.0, runs: 5, min_jobs: 3}\n")
    assert load_settings(path).stability == MEASURED  # a whole measurement: nothing of the defaults (rules_before)
    for bad in ("{level_same: 1.5, role_tier_same: 1.0, runs: 5, min_jobs: 3}",   # each fails for its one value
                "{level_same: 0.8, role_tier_same: 1.0, runs: 1, min_jobs: 3}",
                "{level_same: 0.8, role_tier_same: 1.0, runs: 5, min_jobs: 0}"):
        with pytest.raises(ValueError):
            load_settings(settings_file(tmp_path, f"stability: {bad}\n"))
    with pytest.raises(ValueError):  # a partial block is refused: it does not borrow min_jobs from the defaults
        load_settings(settings_file(tmp_path, "stability: {level_same: 0.88, role_tier_same: 1.0, runs: 5}\n"))


def seeded(tmp_path, stability_block):
    from test_web_market import seed_family

    with Store(tmp_path / "web.sqlite") as st:
        cv_id = st.save_cv("cv.md", "Some CV text that is long enough.")
        st.save_profile(cv_id, profile(("selenium", "expert", "outcome")), "fake")
        seed_family(st, cv_id, "test_automation", {"kubernetes": 2.0}, tier=2, must_coverage=0.5)
    settings = load_settings().model_copy(update={"stability": stability_block})
    return make_client(tmp_path, settings=settings)


@pytest.mark.parametrize("lang", ["de", "en"])
@pytest.mark.parametrize("page", ["/cv/1", "/cv/1/market"])
def test_the_profile_page_and_the_market_view_show_the_measured_numbers(tmp_path, lang, page):
    html = seeded(tmp_path, MEASURED).get(page, headers={"accept-language": lang}).text
    assert f'<p class="meta stability-note">{NOTES[lang]}</p>' in html


DEFAULT_NOTES = {"de": "Zwei Analysen desselben Lebenslaufs: 93 % der Niveaus gleich, Rollen-Stufen in 100 % gleich "
                       "(gemessen mit 5 Läufen am Goldsatz, Rollen mit mindestens 5 Inseraten). Gemessen mit den "
                       "Regeln für Abgleich und Stufen und dem Prompt von Schritt 2 vor dem 25.10.2026.",
                 "en": "Two analyses of the same CV: 93 % of the skill levels the same, role tiers the same in 100 % "
                       "(measured with 5 runs on the gold set, roles with at least 5 postings). Measured with the "
                       "rules for matches and tiers and the step-2 prompt before 2026-10-25."}


@pytest.mark.parametrize("lang", ["de", "en"])
@pytest.mark.parametrize("page", ["/cv/1", "/cv/1/market"])
def test_the_pages_show_the_measured_numbers_of_the_defaults(tmp_path, lang, page):
    html = seeded(tmp_path, load_settings().stability).get(page, headers={"accept-language": lang}).text
    assert f'<p class="meta stability-note">{DEFAULT_NOTES[lang]}</p>' in html


@pytest.mark.parametrize("page", ["/cv/1", "/cv/1/market"])
def test_without_the_block_there_is_no_note(tmp_path, page):
    html = seeded(tmp_path, None).get(page, headers={"accept-language": "en"}).text
    assert "stability-note" not in html and "Two analyses" not in html
