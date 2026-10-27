from collections import Counter
from pathlib import Path

import pytest
import yaml
from fakes import CV, FakeClient, FakeLedger, FakeLLM, ScriptedClient, cv_draft, fail_if_called, ok, prompt_folder

from skillpose.cli import GOLDSET_DB, build_parser, main
from skillpose.core.config import load_settings
from skillpose.core.llm import LLM, estimate_usd
from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    Evidence,
    JobDraft,
    JobMatch,
    JobProfile,
    Requirement,
    RequirementDraft,
    RequirementMatch,
)
from skillpose.core.prompts import load_prompts
from skillpose.core.textio import read_cv
from skillpose.core.vocabulary import Vocabulary
from skillpose.demo import build_demo
from skillpose.evaluation import cv_metrics, job_metrics, match_metrics, read_expected, run_eval
from skillpose.pipeline import open_run
from skillpose.store.db import Store

V = Vocabulary.load()
GOLDSET = Path(__file__).parent.parent / "evals" / "goldset"
FAMILIES = ["test_automation", "rpa_process_automation", "data_analysis_bi", "data_engineering"]
ALL = {"cv", "job", "match"}


def load_expected() -> dict:
    return read_expected(GOLDSET)


def settings_min1():
    """One posting is enough for a role tier (the small gold set has two). Also the default since 2026-10-24; pinned
    here because these tests are about the tiers, not about the default."""
    settings = load_settings()
    settings.rules.min_jobs_per_role = 1
    return settings


def small_goldset(tmp_path: Path, language: str | None = "de") -> Path:
    """One CV (person_0) and two postings of test_automation, with expected statuses and a role tier."""
    g = tmp_path / "goldset"
    (g / "cvs").mkdir(parents=True)
    (g / "postings").mkdir()
    (g / "cvs" / "person_0.md").write_text(CV, encoding="utf-8")
    postings = {}
    for i in range(2):
        name = f"{i:02d}_test_automation"
        (g / "postings" / f"{name}.md").write_text(f"Test Automation Engineer {i}\nSelenium and Java required.",
                                                   encoding="utf-8")
        postings[name] = {"role_family": "test_automation", "seniority": "mid", "must": ["selenium", "java"],
                          "nice": ["kubernetes"]}
    cv = {"skills": ["selenium", "java"], "not_skills": [], "strength": {"selenium": "outcome"},
          "match_status": {"00_test_automation": {"selenium": "met", "kubernetes": "gap"}},
          "role_tiers": {"test_automation": 1}}
    if language is not None:
        cv["language"] = language
    (g / "expected.yaml").write_text(yaml.safe_dump({"cvs": {"person_0": cv}, "postings": postings}),
                                     encoding="utf-8")
    return g


def profile_with(ids, verified):
    skills = [CandidateSkill(skill_id=i, level="working", years=1, evidence=[
        Evidence(quote="q", context="c", period_from=None, period_to=None, strength="activity", verified=v)])
        for i, v in zip(ids, verified, strict=True)]
    return CandidateProfile(language="en", skills=skills, stations=[])


def job_with(must, nice, role_family, text_hash="h"):
    reqs = [Requirement(skill_id=i, importance="must", min_level=None, min_years=None, kind="tool", quote=i)
            for i in must]
    reqs += [Requirement(skill_id=i, importance="nice", min_level=None, min_years=None, kind="tool", quote=i)
             for i in nice]
    return JobProfile(text_hash=text_hash, title="t", role_family=role_family, seniority="mid", requirements=reqs)


def match_of(job: JobProfile, statuses: dict[str, str], tier: int) -> JobMatch:
    by_id = {r.skill_id: r for r in job.requirements}
    matches = [RequirementMatch(requirement=by_id[i], status=s, reason="r", evidence=None,
                                gap_type="months" if s == "gap" else None, effort_weeks=None)
               for i, s in statuses.items()]
    return JobMatch(text_hash=job.text_hash, matches=matches, must_coverage=0.5, nice_coverage=0.0, tier=tier)


def test_cv_metrics_counts_traps_and_recall():
    p = profile_with(["selenium", "java", "kubernetes"], verified=[True, True, False])
    m = cv_metrics(p, {"skills": ["selenium", "java", "jenkins", "testng"], "not_skills": ["kubernetes"],
                       "strength": {}}, V)
    assert m["recall"] == 0.5 and m["trap_hits"] == 1 and m["verified_share"] == pytest.approx(2 / 3)


def test_cv_strength_accuracy():
    p = profile_with(["selenium", "sql"], verified=[True, True])
    m = cv_metrics(p, {"skills": ["selenium"], "not_skills": [], "strength": {"selenium": "activity", "sql": "claim",
                                                                               "docker": "claim"}}, V)
    assert m["strength_accuracy"] == pytest.approx(1 / 3)


def test_job_metrics():
    jp = job_with(must=["selenium", "java"], nice=["docker"], role_family="test_automation")
    m = job_metrics(jp, {"role_family": "test_automation", "seniority": "mid", "must": ["selenium", "java", "jenkins"],
                         "nice": ["docker", "aws"]}, V)
    assert m["role_family_ok"] == 1 and m["must_recall"] == pytest.approx(2 / 3) and m["nice_recall"] == 0.5
    assert m["importance_accuracy"] == 1.0 and m["must_precision"] == 1.0 and m["seniority_ok"] == 1


# --- the gold set ---

def test_goldset_ids_resolve():
    exp = load_expected()
    assert len(exp["cvs"]) == 3 and len(exp["postings"]) == 30
    ids = ({i for e in exp["cvs"].values() for i in e["skills"] + e["not_skills"] + list(e["strength"])}
           | {i for e in exp["postings"].values() for i in e["must"] + e["nice"]}
           | {i for e in exp["cvs"].values() for pairs in e["match_status"].values() for i in pairs})
    assert all(V.resolve(i) for i in ids), sorted(i for i in ids if not V.resolve(i))
    assert all(p in exp["postings"] for e in exp["cvs"].values() for p in e["match_status"])
    assert all(f in load_settings().role_families for e in exp["cvs"].values() for f in e["role_tiers"])
    assert {p.stem for p in (GOLDSET / "cvs").glob("*.md")} == set(exp["cvs"])
    assert {p.stem for p in (GOLDSET / "postings").glob("*.md")} == set(exp["postings"])
    assert all(e["role_family"] in load_settings().role_families for e in exp["postings"].values())


def test_goldset_distribution():
    fams = Counter(e["role_family"] for e in load_expected()["postings"].values())
    assert all(fams[f] >= 5 for f in FAMILIES)


def test_goldset_expectations_per_person():
    exp = load_expected()
    fams = Counter(e["role_family"] for e in exp["postings"].values())
    assert {key: e["language"] for key, e in exp["cvs"].items()} == {
        "maria_berger": "de", "daniel_novak": "en", "sabine_huber": "de"}
    for key, e in exp["cvs"].items():
        assert 8 <= sum(len(pairs) for pairs in e["match_status"].values()) <= 12 and len(e["match_status"]) >= 4, key
        statuses = {s for pairs in e["match_status"].values() for s in pairs.values()}
        assert statuses == {"met", "partial", "gap"}, key
        for posting, pairs in e["match_status"].items():  # a pair whose requirement the posting lacks is never right
            asked = {V.resolve(i) for i in exp["postings"][posting]["must"] + exp["postings"][posting]["nice"]}
            assert {V.resolve(i) for i in pairs} <= asked, (key, posting)
        assert set(e["role_tiers"]) == set(FAMILIES) and set(e["role_tiers"].values()) <= {1, 2, 3}, key
        assert all(fams[f] >= 5 for f in e["role_tiers"])


def test_the_skill_outside_the_vocabulary_stays_outside():
    text = (GOLDSET / "postings" / "27_senior_data_engineer_streaming_graz.md").read_text(encoding="utf-8")
    assert "Apache Flink" in text and V.resolve("Apache Flink") is None and V.resolve("Flink") is None


# --- match metrics ---

def test_match_metrics():
    jobs = {"a": job_with(["selenium", "java"], [], "test_automation", text_hash="ha"),
            "b": job_with(["sql"], [], "data_engineering", text_hash="hb"),
            "c": job_with(["uipath"], [], "rpa_process_automation", text_hash="hc")}
    matches = {"a": match_of(jobs["a"], {"selenium": "met", "java": "partial"}, tier=1),
               "b": match_of(jobs["b"], {"sql": "gap"}, tier=3),
               "c": match_of(jobs["c"], {"uipath": "partial"}, tier=2)}
    expected = {"match_status": {"a": {"Selenium": "met", "java": "partial"},
                                 "b": {"docker": "gap"}},  # b does not ask for Docker: counts as wrong
                "role_tiers": {"test_automation": 1, "data_engineering": 2, "rpa_process_automation": 3}}
    m = match_metrics(matches, jobs, expected, V, settings_min1().rules)
    assert m == {"match_status_accuracy": pytest.approx(2 / 3), "role_tier_accuracy": pytest.approx(1 / 3)}
    # with a minimum of 5 postings (the earlier default) no family gets a tier, so no expected tier is met
    five = load_settings().rules.model_copy(update={"min_jobs_per_role": 5})
    assert match_metrics(matches, jobs, expected, V, five)["role_tier_accuracy"] == 0.0
    # the default of today counts every family
    assert match_metrics(matches, jobs, expected, V, load_settings().rules) == m


def test_match_metrics_counts_a_posting_without_job_profile_as_wrong():
    jobs = {"a": job_with(["selenium"], [], "test_automation", text_hash="ha")}
    matches = {"a": match_of(jobs["a"], {"selenium": "met"}, tier=1)}
    expected = {"match_status": {"a": {"selenium": "met"}, "missing": {"java": "met"}}, "role_tiers": {}}
    assert match_metrics(matches, jobs, expected, V, settings_min1().rules) == {
        "match_status_accuracy": 0.5, "role_tier_accuracy": 1.0}


# --- run_eval ---

def _draft() -> JobDraft:
    req = RequirementDraft(skill_id="selenium", name="Selenium", category="tool", importance="must", min_level=None,
                           min_years=None, kind="tool", quote="Selenium")
    return JobDraft(title="QA", role_family="test_automation", seniority="mid", requirements=[req])


def test_run_eval_with_fake_llm():
    report = run_eval(FakeLLM(value=_draft()), GOLDSET, V, load_settings(), {"job"}, "en")
    md = report.to_markdown()
    assert len(report.steps["job"].items) == 30 and report.steps["job"].failures == 0
    assert "must_recall" in md and "01_test_automation_linz" in md and "cv" not in report.steps
    assert md.splitlines()[2] == "Prompt set: example"


def test_run_eval_all_saves_into_the_store(tmp_path):
    gold = small_goldset(tmp_path)
    st = Store(tmp_path / "g.sqlite")
    report = run_eval(LLM(ScriptedClient(), settings_min1(), st), gold, V, settings_min1(), ALL, "auto", store=st)
    assert set(report.steps) == {"cv", "job", "match"} and len(st.matches(st.cvs()[0].id)) == 2
    [cv] = st.cvs()
    assert cv.name == "person_0.md" and cv.analyzed and st.cv_text(cv.id) == read_cv(gold / "cvs" / "person_0.md")
    assert sorted(ref for row in st.postings() for ref in row.refs) == ["00_test_automation.md",
                                                                        "01_test_automation.md"]
    assert all(row.status == "analyzed" for row in st.postings())
    assert st.relations("kubernetes") == {"java": "none", "selenium": "none"}
    assert report.steps["match"].items == [("person_0", {"match_status_accuracy": 1.0, "role_tier_accuracy": 1.0})]
    st.close()


def test_demo_build_after_eval_with_store_pays_only_for_the_paths(tmp_path):
    gold = small_goldset(tmp_path)
    settings = settings_min1()
    with Store(tmp_path / "g.sqlite") as st:
        cv_id = st.save_cv("from_an_earlier_run.md", read_cv(gold / "cvs" / "person_0.md"))
        run_eval(LLM(ScriptedClient(extra_requirement="apache_flink"), settings, st), gold, V, settings, ALL, "auto",
                 store=st)
        assert [cv.id for cv in st.cvs()] == [cv_id]  # the CV row is found by its text, never a second one
        assert st.added_skills() == []  # proposed skills stay out: the demo build sees the vocabulary of the eval
        client = ScriptedClient()
        build_demo(open_run(settings, st, "en", client=client), gold, tmp_path / "out")
    assert [c.path for c in client.calls] == ["PathDraft", "PathDraft"]  # the one path in German and in English


def test_run_eval_without_store_keeps_relations_in_memory(tmp_path):
    gold = small_goldset(tmp_path)
    ledger = FakeLedger()
    report = run_eval(LLM(ScriptedClient(), settings_min1(), ledger), gold, V, settings_min1(), ALL, "auto")
    relation_costs = [r.cost_usd for r in ledger.records if r.step == "relation"]
    match = report.steps["match"]
    assert (match.model, match.effort) == ("claude-haiku-5-5", "low") and len(relation_costs) == 1
    assert match.cost_usd == pytest.approx(sum(relation_costs)) and match.cost_usd > 0
    md = report.to_markdown()
    assert "## Step match - claude-haiku-5-5 (effort low)" in md and "| person_0 | 1.00 | 1.00 |" in md
    assert "role_tier_accuracy" in md and not list(tmp_path.rglob("*.sqlite"))


def test_auto_language_follows_the_cv():
    llm = FakeLLM(value=cv_draft())
    run_eval(llm, GOLDSET, V, load_settings(), {"cv"}, "auto")
    languages = {key: "German" if "in German" in call.fixed[0] else "English"
                 for key, call in zip(load_expected()["cvs"], llm.calls, strict=True)}
    assert languages == {"maria_berger": "German", "daniel_novak": "English", "sabine_huber": "German"}
    assert "Maria Berger" in llm.calls[0].variable
    jobs = FakeLLM(value=_draft())
    run_eval(jobs, GOLDSET, V, load_settings(), {"job"}, "auto")
    assert len(jobs.calls) == 30 and all("in English" in call.fixed[0] for call in jobs.calls)  # as in phase 1
    forced = FakeLLM(value=cv_draft())
    run_eval(forced, GOLDSET, V, load_settings(), {"cv"}, "de")
    assert all("in German" in call.fixed[0] for call in forced.calls)


@pytest.mark.parametrize("steps", [{"match"}, {"cv", "match"}, {"job", "match"}])
def test_match_needs_cv_and_job_in_the_same_run(steps):
    llm = FakeLLM()
    with pytest.raises(ValueError, match="match"):
        run_eval(llm, GOLDSET, V, load_settings(), steps, "auto")
    assert llm.calls == []


def test_auto_needs_the_language_of_each_cv(tmp_path):
    llm = FakeLLM(value=cv_draft())
    with pytest.raises(ValueError, match="person_0.language"):
        run_eval(llm, small_goldset(tmp_path, language=None), V, load_settings(), {"cv"}, "auto")
    assert llm.calls == []


# --- command line ---

def settings_file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "settings.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_eval_defaults():
    args = build_parser().parse_args(["eval"])
    assert (args.step, args.lang, args.db, args.save) == ("all", "auto", GOLDSET_DB, False)


def test_eval_command_writes_report(tmp_path):
    out = tmp_path / "r.md"
    client = FakeClient(answers=[ok(_draft()) for _ in range(30)])
    rc = main(["eval", "--goldset", str(GOLDSET), "--step", "job", "--job-model", "claude-sonnet-5-5",
               "--db", str(tmp_path / "e.sqlite"), "--out", str(out)], client_factory=lambda: client)
    assert rc == 0 and "claude-sonnet-5-5" in out.read_text(encoding="utf-8")
    assert all(c.kwargs["model"] == "claude-sonnet-5-5" for c in client.calls) and len(client.calls) == 30


def test_eval_report_names_the_prompt_set_of_the_run(tmp_path):
    folder = prompt_folder(tmp_path / "mine")
    out = tmp_path / "r.md"
    rc = main(["eval", "--goldset", str(small_goldset(tmp_path)), "--db", str(tmp_path / "g.sqlite"),
               "--prompts", str(folder), "--out", str(out)], client_factory=ScriptedClient)
    assert rc == 0 and f"\nPrompt set: {load_prompts(folder).label}\n" in out.read_text(encoding="utf-8")


def test_eval_prints_the_prompt_set_first_before_the_estimate_and_the_decision(tmp_path, capsys):
    folder, gold = prompt_folder(tmp_path / "mine"), small_goldset(tmp_path)
    base = ["eval", "--goldset", str(gold), "--db", str(tmp_path / "g.sqlite"),
            "--settings", str(settings_file(tmp_path, "confirm_above_usd: 0.0001\n"))]
    for extra, label in (([], "example"), (["--prompts", str(folder)], load_prompts(folder).label)):
        assert main([*base, *extra], client_factory=fail_if_called) == 3
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == f"prompt set: {label}" and lines[1].startswith("estimated cost")
    assert main(["eval", "--goldset", str(gold), "--db", str(tmp_path / "g.sqlite"), "--prompts", str(folder)],
                client_factory=ScriptedClient) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"prompt set: {load_prompts(folder).label}"


def test_eval_save_stores_the_results(tmp_path, capsys):
    gold, db = small_goldset(tmp_path), tmp_path / "g.sqlite"
    settings = settings_file(tmp_path, "rules:\n  min_jobs_per_role: 1\n")
    rc = main(["eval", "--goldset", str(gold), "--db", str(db), "--settings", str(settings), "--save"],
              client_factory=ScriptedClient)
    assert rc == 0 and "## Step match" in capsys.readouterr().out
    with Store(db) as st:
        [cv] = st.cvs()
        assert len(st.matches(cv.id)) == 2 and len(st.cost_rows()) == 4  # CV, two postings, one relation
    db.unlink()  # the command closed its store (an open file cannot be deleted on Windows)


def test_eval_without_save_keeps_only_the_costs(tmp_path):
    gold, db = small_goldset(tmp_path), tmp_path / "g.sqlite"
    assert main(["eval", "--goldset", str(gold), "--db", str(db)], client_factory=ScriptedClient) == 0
    with Store(db) as st:
        assert st.cvs() == [] and st.postings() == [] and len(st.cost_rows()) == 4


def test_eval_estimate_includes_the_relations(tmp_path, capsys):
    s = load_settings()
    base = estimate_usd(s, "cv", 3, avg_out=4000) + estimate_usd(s, "job", 30)
    relations = estimate_usd(s, "relation", 3 * 20, avg_in=300, avg_out=300, fixed_tokens=6000)
    settings = settings_file(tmp_path, f"confirm_above_usd: {base + relations / 2}\n")
    rc = main(["eval", "--goldset", str(GOLDSET), "--db", str(tmp_path / "g.sqlite"), "--settings", str(settings)],
              client_factory=fail_if_called)
    assert rc == 3 and f"estimated cost {base + relations:.2f} USD" in capsys.readouterr().out


def test_eval_stops_at_the_monthly_limit_before_the_client(tmp_path, capsys):
    settings = settings_file(tmp_path, "monthly_limit_usd: 0.01\n")
    rc = main(["eval", "--goldset", str(GOLDSET), "--db", str(tmp_path / "g.sqlite"), "--settings", str(settings),
               "--yes"], client_factory=fail_if_called)
    assert rc == 4 and "monthly limit" in capsys.readouterr().out


def test_eval_with_a_cv_without_language_exits_2(tmp_path, capsys):
    rc = main(["eval", "--goldset", str(small_goldset(tmp_path, language=None)), "--db", str(tmp_path / "g.sqlite")],
              client_factory=fail_if_called)
    assert rc == 2 and "person_0.language" in capsys.readouterr().out
