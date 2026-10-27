import pytest
from fakes import (
    CV,
    NO_RESULTS,
    FailingClient,
    FakeBatchClient,
    FakeClient,
    ScriptedClient,
    answer,
    api_error,
    connection_error,
    not_found,
    refusal,
    req,
)

from skillpose.core.config import load_settings
from skillpose.core.llm import BudgetExceeded, Stopped, Usage, estimate_cv_usd, estimate_usd
from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    Conditions,
    Evidence,
    JobDraft,
    JobProfile,
    Requirement,
    RoleReport,
    SkillProposal,
)
from skillpose.pipeline import (
    analyze_postings,
    build_path_for,
    collect_batches,
    ensure_profile,
    estimate,
    ingest_postings,
    limit_message,
    market,
    merge_proposals,
    open_run,
    path_targets,
    plan_postings,
    rematch,
    rematch_offline,
)
from skillpose.store.db import Store

FROM_BATCH = JobDraft(title="From batch", role_family="test_automation", seniority="mid",
                      requirements=[req("java")]).model_dump_json()


def fail_sleep(_seconds):
    raise AssertionError("must not wait")


def store_with_pending_batch(tmp_path) -> Store:
    store = Store(tmp_path / "s.sqlite")
    store.save_batch("batch_old", [store.save_posting("Job A " * 20, "a.md")])
    return store


def _skill(skill_id, hidden=False) -> CandidateSkill:
    ev = Evidence(quote=f"Used {skill_id}", context="Acme", period_from=2018, period_to=None, strength="outcome",
                  verified=True)
    return CandidateSkill(skill_id=skill_id, level="strong", years=6, last_used=2026, evidence=[ev], hidden=hidden)


def _requirement(skill_id, importance="must") -> Requirement:
    return Requirement(skill_id=skill_id, importance=importance, min_level=None, min_years=None, kind="tool",
                       quote=skill_id)


def store_with_profile_and_job(tmp_path, skills, requirements) -> tuple[Store, int, str]:
    store = Store(tmp_path / "s.sqlite")
    cv_id = store.save_cv("cv.md", CV)
    store.save_profile(cv_id, CandidateProfile(language="en", skills=skills, stations=[]), "fake")
    h = store.save_posting("QA Engineer\nSelenium required.", "job.md")
    store.save_job_profile(JobProfile(text_hash=h, title="QA Engineer", role_family="test_automation",
                                      seniority="mid", requirements=requirements), "fake")
    return store, cv_id, h


def open_postings_run(tmp_path, n, client=None, **values):
    """A run (settings with values) and the hashes of n stored, open postings, in the order they were stored."""
    run = open_run(load_settings().model_copy(update=values), Store(tmp_path / "s.sqlite"), "de", client=client)
    hashes, _ = ingest_postings(run, [(f"j{i}.md", f"Test Automation Engineer {i}\nJava.") for i in range(n)])
    return run, hashes


def split_run(tmp_path, n, statuses, **kwargs):
    """A run with n open postings, its plan and a ScriptedClient that holds a FakeBatchClient (kwargs) whose
    batches answer succeeded for every posting."""
    client = ScriptedClient()
    run, hashes = open_postings_run(tmp_path, n, client=client)
    batches = FakeBatchClient(results={h: ("succeeded", FROM_BATCH) for h in hashes}, statuses=statuses, **kwargs)
    client.messages.batches = batches.messages.batches
    return run, plan_postings(run, hashes), client, batches


def analyzed_run(tmp_path, n_postings, client=None, settings=None):
    """A run with a CV profile and n analyzed and matched postings of the family test_automation."""
    client = client or ScriptedClient()
    run = open_run(settings or load_settings(), Store(tmp_path / "s.sqlite"), "de", client=client)
    cv_id = run.store.save_cv("cv.md", CV)
    ensure_profile(run, cv_id)
    items = [(f"job_{i}.md", f"Test Automation Engineer {i}\nSelenium and Java required.") for i in range(n_postings)]
    hashes, _ = ingest_postings(run, items)
    analyze_postings(run, plan_postings(run, hashes, no_batch=True))
    rematch(run, cv_id, hashes)
    return run, cv_id, client


def report(family, tier, postings=5) -> RoleReport:
    return RoleReport(role_family=family, postings=postings, median_coverage=0.5, tier=tier,
                      tier_shares={1: 0.0, 2: 0.0, 3: 0.0}, top_gaps=[])


# --- postings ---

def test_ingest_skips_empty_and_too_long_texts_and_keeps_duplicates_once(tmp_path):
    run = open_run(load_settings(), Store(tmp_path / "s.sqlite"), "de")
    long_text = "x" * (run.settings.max_posting_chars + 1)
    hashes, skipped = ingest_postings(run, [("a.md", "Job A"), ("e.md", "  "), ("l.md", long_text),
                                            ("a2.md", "Job  A")])
    assert len(hashes) == 1 and len(run.store.postings()) == 1
    assert skipped == [f"skipped l.md: {len(long_text)} characters, limit {run.settings.max_posting_chars}"]
    assert run.store.postings()[0].refs == ["a.md", "a2.md"]


def test_plan_uses_batch_from_threshold_and_skips_pending(tmp_path):
    run = open_run(load_settings(), Store(tmp_path / "s.sqlite"), "de")
    hashes, _ = ingest_postings(run, [(f"j{i}.md", f"Job {i}") for i in range(21)])
    plan = plan_postings(run, hashes[1:])
    assert len(plan.open) == 20 and plan.batch and plan.pending == []
    assert not plan_postings(run, hashes[1:], no_batch=True).batch
    run.store.save_batch("batch_old", [hashes[1]])
    plan = plan_postings(run, hashes[1:])
    assert hashes[1] not in plan.open and len(plan.open) == 19 and not plan.batch
    assert plan.pending == [("batch_old", [hashes[1]])]


@pytest.mark.parametrize("n, single_first, groups, n_single, sizes", [
    (20, 1, 3, 1, [7, 6, 6]), (21, 1, 3, 1, [7, 7, 6]), (30, 1, 3, 1, [10, 10, 9]), (31, 1, 3, 1, [10, 10, 10]),
    (20, 0, 3, 0, [7, 7, 6]), (30, 0, 3, 0, [10, 10, 10]), (20, 1, 1, 1, [19]), (31, 0, 1, 0, [31]),
    (19, 1, 3, 19, []),    # below the threshold: all single calls
    (20, 25, 3, 20, []),   # nothing left for a batch
])
def test_plan_sends_the_first_postings_at_once_and_splits_the_rest_into_near_equal_batches(
        tmp_path, n, single_first, groups, n_single, sizes):
    run, hashes = open_postings_run(tmp_path, n, batch_single_first=single_first, batch_groups=groups)
    plan = plan_postings(run, hashes)
    assert plan.single == hashes[:n_single] and [len(g) for g in plan.groups] == sizes
    assert plan.single + [h for g in plan.groups for h in g] == plan.open == hashes  # stored order, each once
    assert plan.batch == bool(sizes) and plan.batched == sum(sizes)
    no_batch = plan_postings(run, hashes, no_batch=True)  # --no-batch: all single, as before
    assert no_batch.single == no_batch.open == hashes and no_batch.groups == [] and not no_batch.batch


def test_the_default_plan_is_one_at_once_and_the_rest_in_three_batches(tmp_path):
    run, hashes = open_postings_run(tmp_path, 20)
    plan = plan_postings(run, hashes)
    assert plan.single == hashes[:1] and [len(g) for g in plan.groups] == [7, 6, 6]


def test_the_batches_go_out_before_the_single_call(tmp_path):
    # the clock of a batch starts when it is submitted; the single call takes about a minute
    client = ScriptedClient()
    run, hashes = open_postings_run(tmp_path, 30, client=client)
    batches = FakeBatchClient(results={}, statuses=[], log=client.calls)
    client.messages.batches = batches.messages.batches
    plan = plan_postings(run, hashes)
    outcome = analyze_postings(run, plan, wait=False, sleep=fail_sleep)
    assert [c.path for c in client.calls] == ["batches.create"] * 3 + ["JobDraft"]
    assert [c.kwargs["requests"] for c in client.calls[:3]] == [10, 10, 9]
    assert outcome.batch_ids == ["batch_1", "batch_2", "batch_3"] and (outcome.done, outcome.still_open) == (1, 29)
    assert run.store.job_profile(hashes[0]) is not None  # the first result is stored before any batch has ended
    assert run.store.pending_batches() == list(zip(outcome.batch_ids, plan.groups, strict=True))
    assert set(batches.batches["batch_1"]) == set(plan.groups[0]) and batches.retrieved == []


def test_each_batch_is_collected_as_soon_as_it_has_ended(tmp_path):
    run, plan, _, batches = split_run(tmp_path, 30, {"batch_1": ["in_progress", "ended"], "batch_2": ["ended"],
                                                     "batch_3": ["in_progress", "in_progress", "ended"]})
    analyze_postings(run, plan, wait=False)
    assert collect_batches(run, wait=False, sleep=fail_sleep) == 10  # batch 2 only
    assert [batch_id for batch_id, _ in run.store.pending_batches()] == ["batch_1", "batch_3"]
    assert all(run.store.job_profile(h) for h in plan.groups[1])
    assert not any(run.store.job_profile(h) for h in plan.groups[0] + plan.groups[2])
    assert collect_batches(run, wait=False, sleep=fail_sleep) == 10  # batch 1; batch 3 still runs
    assert [batch_id for batch_id, _ in run.store.pending_batches()] == ["batch_3"]
    assert collect_batches(run, wait=False, sleep=fail_sleep) == 9 and run.store.pending_batches() == []
    assert run.store.open_postings() == [] and batches.retrieved.count("batch_2") == 1  # one status request each


def test_waiting_collects_the_batches_in_the_order_they_end(tmp_path):
    run, plan, _, _ = split_run(tmp_path, 30, {"batch_1": ["in_progress", "in_progress", "ended"],
                                               "batch_2": ["ended"], "batch_3": ["in_progress", "ended"]})
    analyze_postings(run, plan, wait=False)
    collected, sleeps = [], []
    assert collect_batches(run, poll_seconds=5, sleep=sleeps.append,
                           collected=lambda *args: collected.append(args)) == 29
    assert collected == [("batch_2", 10, 10), ("batch_3", 9, 9), ("batch_1", 10, 10)] and sleeps == [5, 5]


def test_analyze_with_wait_makes_the_single_call_and_collects_every_batch(tmp_path):
    run, plan, client, _ = split_run(tmp_path, 20, {"batch_1": ["ended"], "batch_2": ["in_progress", "ended"],
                                                    "batch_3": ["ended"]})
    sleeps = []
    outcome = analyze_postings(run, plan, poll_seconds=7, sleep=sleeps.append)
    assert (outcome.done, outcome.still_open) == (20, 0) and sleeps == [7]
    assert run.store.open_postings() == [] and run.store.pending_batches() == []
    assert [c.path for c in client.calls] == ["JobDraft"]


def test_a_check_reports_the_progress_of_each_running_batch(tmp_path):
    counts = {"batch_1": {"processing": 4, "succeeded": 4, "errored": 1, "expired": 1}, "batch_3": {"processing": 9}}
    run, plan, _, batches = split_run(tmp_path, 30, ["in_progress"] * 3, counts=counts)
    analyze_postings(run, plan, wait=False)
    seen = []
    assert collect_batches(run, wait=False, sleep=fail_sleep, progress=seen.append) == 0
    # done = total - processing, total = the postings stored for the batch; batch 2 answered without counts
    assert [(p.batch_id, p.total, p.done, p.failed) for p in seen] == [("batch_1", 10, 6, 2), ("batch_3", 9, 0, 0)]
    assert all(p.checked.tzinfo is not None for p in seen)
    assert batches.retrieved == ["batch_1", "batch_2", "batch_3"]  # the check itself, no extra request


def test_a_batch_that_does_not_fit_after_the_others_stops_the_run_before_the_single_call(tmp_path):
    s = load_settings()
    limit = 2 * estimate_usd(s, "job", 10, batch=True) + estimate_usd(s, "job", 9, batch=True) / 2
    client = ScriptedClient()
    run, hashes = open_postings_run(tmp_path, 30, client=client, monthly_limit_usd=limit)
    client.messages.batches = FakeBatchClient(results={}, statuses=[], log=client.calls).messages.batches
    with pytest.raises(BudgetExceeded):
        analyze_postings(run, plan_postings(run, hashes), wait=False)
    assert [c.path for c in client.calls] == ["batches.create"] * 2  # not the third batch, not the single call
    assert [batch_id for batch_id, _ in run.store.pending_batches()] == ["batch_1", "batch_2"]  # collected later


def test_failed_single_posting_keeps_its_reason(tmp_path):
    run = open_run(load_settings(), Store(tmp_path / "s.sqlite"), "de", client=FakeClient([refusal()]))
    hashes, _ = ingest_postings(run, [("a.md", "Job A " * 20)])
    outcome = analyze_postings(run, plan_postings(run, hashes))
    assert run.store.postings()[0].error == "refusal"
    assert (outcome.done, outcome.still_open, outcome.batch_ids) == (0, 1, [])


def test_batch_without_wait_returns_its_id_and_the_next_collect_finishes_it(tmp_path):
    # all in one batch, nothing at once: the client below has no messages.create
    settings = load_settings().model_copy(update={"batch_threshold": 2, "batch_single_first": 0, "batch_groups": 1})
    store = Store(tmp_path / "s.sqlite")
    texts = ["Test Automation Engineer 1\nJava.", "Test Automation Engineer 2\nJava."]
    hashes = [store.save_posting(t) for t in texts]
    draft = JobDraft(title="From batch", role_family="test_automation", seniority="mid", requirements=[req("java")])
    batches = FakeBatchClient(results={hashes[0]: ("succeeded", draft.model_dump_json())}, statuses=["ended"])
    run = open_run(settings, store, "de", client=batches)
    plan = plan_postings(run, hashes)
    outcome = analyze_postings(run, plan, wait=False)
    assert plan.batch and outcome.batch_ids == ["batch_1"] and outcome.still_open == 2
    assert store.pending_batches() == [("batch_1", hashes)]
    assert collect_batches(run, sleep=fail_sleep) == 1 and store.pending_batches() == []
    rows = {r.hash: r for r in store.postings()}
    assert rows[hashes[0]].title == "From batch" and rows[hashes[1]].error == "no result in batch"


def test_collect_without_wait_leaves_a_running_batch(tmp_path):
    batches = FakeBatchClient(results={}, statuses=["in_progress"])
    run = open_run(load_settings(), store_with_pending_batch(tmp_path), "de", client=batches)
    assert collect_batches(run, wait=False, sleep=fail_sleep) == 0 and len(run.store.pending_batches()) == 1


def test_collect_without_wait_takes_an_ended_batch(tmp_path):
    store = store_with_pending_batch(tmp_path)
    [(_, [h])] = store.pending_batches()
    draft = JobDraft(title="From batch", role_family="test_automation", seniority="mid", requirements=[req("java")])
    batches = FakeBatchClient(results={h: ("succeeded", draft.model_dump_json())}, statuses=["ended", "ended"])
    run = open_run(load_settings(), store, "de", client=batches)
    assert collect_batches(run, wait=False, sleep=fail_sleep) == 1 and store.pending_batches() == []
    assert store.job_profile(h).title == "From batch"


@pytest.mark.parametrize("wait", [True, False])
@pytest.mark.parametrize("errors", [
    {"retrieve_error": not_found()},                            # the id of another key or workspace
    {"results_error": not_found()},                             # the results answer 404
    {"results_error": NO_RESULTS},                              # ended, but the results are gone (29 days)
], ids=["retrieve_404", "results_404", "results_gone"])
def test_a_lost_batch_gives_its_postings_back(tmp_path, wait, errors):
    store = Store(tmp_path / "s.sqlite")
    lost, analyzed = store.save_posting("Job A " * 20, "a.md"), store.save_posting("Job B " * 20, "b.md")
    store.save_job_profile(JobProfile(text_hash=analyzed, title="B", role_family="test_automation", seniority="mid",
                                      requirements=[]), "fake")
    store.save_batch("batch_old", [lost, analyzed])
    batches = FakeBatchClient(results={}, statuses=["ended", "ended"], **errors)
    run = open_run(load_settings(), store, "de", client=batches)
    assert collect_batches(run, wait=wait, sleep=fail_sleep) == 0
    assert store.pending_batches() == [] and store.open_postings() == [lost]
    rows = {row.hash: row for row in store.postings()}
    assert rows[lost].status == "open" and rows[lost].error.startswith("batch batch_old lost: ")
    assert rows[analyzed].error is None and store.job_profile(analyzed).title == "B"
    assert plan_postings(run, [lost, analyzed]).open == [lost]  # the next run analyzes it again


@pytest.mark.parametrize("error", [connection_error(), api_error(529), api_error(429)],
                         ids=["connection", "529", "429"])
def test_a_passing_error_keeps_the_batch_open(tmp_path, error):
    store = store_with_pending_batch(tmp_path)
    run = open_run(load_settings(), store, "de", client=FakeBatchClient(results={}, statuses=[], retrieve_error=error))
    with pytest.raises(type(error)):
        collect_batches(run, wait=False, sleep=fail_sleep)
    [(_, [h])] = store.pending_batches()
    assert store.postings()[0].error is None and store.postings()[0].status == "batch"
    assert plan_postings(run, [h]).open == []  # still paid for: never sent again


def test_one_lost_batch_does_not_stop_the_next(tmp_path):
    store = store_with_pending_batch(tmp_path)
    h = store.save_posting("Job C " * 20, "c.md")
    store.save_batch("batch_new", [h])
    draft = JobDraft(title="From batch", role_family="test_automation", seniority="mid", requirements=[req("java")])
    batches = FakeBatchClient(results={h: ("succeeded", draft.model_dump_json())}, statuses=["ended", "ended"])
    real = batches.messages.batches.retrieve

    def retrieve(batch_id):
        if batch_id == "batch_old":
            raise not_found()
        return real(batch_id)

    batches.messages.batches.retrieve = retrieve
    run = open_run(load_settings(), store, "de", client=batches)
    assert collect_batches(run, wait=False, sleep=fail_sleep) == 1 and store.pending_batches() == []
    assert store.job_profile(h).title == "From batch"


# --- costs ---

def test_estimate_adds_cv_postings_and_paths(tmp_path):
    run = open_run(load_settings(), Store(tmp_path / "s.sqlite"), "de")
    s = run.settings
    path_cost = estimate_usd(s, "path", 1, avg_in=1500, avg_out=3000, fixed_tokens=2500)
    assert estimate(run) == 0.0
    assert estimate(run, n_paths=3) == 3 * path_cost
    expected = estimate_usd(s, "job", 4) + estimate_cv_usd(s, len(CV)) + 2 * path_cost
    assert abs(estimate(run, cv_text=CV, n_postings=4, n_paths=2) - expected) < 1e-12


# What step 1 cost (costs.json of the demo, cold cache, and the first real runs of 2026-10): the output, which
# includes the thinking, grows with the CV. (characters of the CV, paid USD)
GOLD_SET_CVS = [(1778, 0.178726), (968, 0.119466)]  # 889 and 484 input tokens, 5,738 and 2,856 output tokens
OWNER_CV_CHARS, OWNER_CV_PAID = 9289, 0.40          # 4,657 input tokens, 15,694 and 16,000 output tokens


def test_the_estimate_of_a_long_cv_is_not_a_third_of_what_it_costs(tmp_path):
    run = open_run(load_settings(), Store(tmp_path / "s.sqlite"), "de")
    owner = estimate(run, cv_text="x" * OWNER_CV_CHARS)
    assert 0.39 <= owner <= 1.25 * OWNER_CV_PAID


def test_the_estimate_of_a_short_gold_set_cv_stays_near_what_it_cost(tmp_path):
    run = open_run(load_settings(), Store(tmp_path / "s.sqlite"), "de")
    for chars, paid in GOLD_SET_CVS:
        assert paid <= estimate(run, cv_text="x" * chars) <= 1.6 * paid, chars
    floor = run.settings.estimates.cv_out_floor
    assert estimate(run, cv_text="x" * 100) == pytest.approx(  # a tiny CV: the floor of the output
        estimate_usd(run.settings, "cv", 1, avg_in=50, avg_out=floor, fixed_tokens=12000))


def test_the_estimate_of_the_cv_grows_with_its_length_and_stops_at_the_max_tokens_cost(tmp_path):
    run = open_run(load_settings(), Store(tmp_path / "s.sqlite"), "de")
    s = run.settings
    costs = [estimate(run, cv_text="x" * n) for n in (500, 3000, 6000, OWNER_CV_CHARS, 14000, 60000, 600000)]
    assert costs[0] < costs[2] < costs[3] < costs[4] < costs[5]
    e = s.estimates
    for chars in (60000, 600000):  # the output never counts more than the step may write
        tokens = round(chars / e.cv_chars_per_token)
        cap = estimate_usd(s, "cv", 1, avg_in=tokens, avg_out=s.steps["cv"].max_tokens, fixed_tokens=e.cv_fixed_tokens)
        assert estimate(run, cv_text="x" * chars) == pytest.approx(cap)
    s.steps["cv"].max_tokens = 3000  # a limit below the floor: the estimate follows the limit
    assert estimate(run, cv_text="x" * 3000) == pytest.approx(
        estimate_usd(s, "cv", 1, avg_in=1500, avg_out=3000, fixed_tokens=e.cv_fixed_tokens))


def test_the_cv_estimate_follows_the_settings_of_the_estimates(tmp_path):
    settings = load_settings()
    settings.estimates.cv_out_per_in, settings.estimates.cv_out_floor = 1.0, 0
    run = open_run(settings, Store(tmp_path / "s.sqlite"), "de")
    assert estimate(run, cv_text="x" * 4000) == pytest.approx(
        estimate_usd(settings, "cv", 1, avg_in=2000, avg_out=2000, fixed_tokens=settings.estimates.cv_fixed_tokens))


def test_estimate_prices_the_single_calls_and_each_batch_of_a_plan(tmp_path):
    run, hashes = open_postings_run(tmp_path, 30)
    s = run.settings
    expected = estimate_usd(s, "job", 1) + sum(estimate_usd(s, "job", n, batch=True) for n in (10, 10, 9))
    assert estimate(run, plan=plan_postings(run, hashes)) == pytest.approx(expected)
    assert estimate(run, plan=plan_postings(run, hashes[:19])) == pytest.approx(estimate_usd(s, "job", 19))
    assert estimate(run, plan=plan_postings(run, hashes, no_batch=True)) == pytest.approx(estimate_usd(s, "job", 30))


def test_limit_message_only_when_the_estimate_does_not_fit(tmp_path):
    run = open_run(load_settings().model_copy(update={"monthly_limit_usd": 1.0}), Store(tmp_path / "s.sqlite"), "de")
    assert limit_message(run, 1.0) is None
    message = limit_message(run, 1.5)
    assert message == ("monthly limit: 0.00 USD spent + 1.50 USD estimated for this run > 1.00 USD - nothing was sent")


# --- profile and matching ---

def test_ensure_profile_calls_once_and_reports_a_failure(tmp_path):
    client = ScriptedClient()
    run = open_run(load_settings(), Store(tmp_path / "s.sqlite"), "de", client=client)
    cv_id = run.store.save_cv("cv.md", CV)
    first, error = ensure_profile(run, cv_id)
    again, _ = ensure_profile(run, cv_id)
    assert error is None and first == again == run.store.profile(cv_id) and len(client.calls) == 1
    failing = open_run(load_settings(), run.store, "de", client=FakeClient([refusal()]))
    # an early stop comes with the limit of the step and what the attempt cost (500 input tokens on Opus 5.5)
    assert ensure_profile(failing, run.store.save_cv("other.md", CV + " Other.")) == (
        None, Stopped("refusal", 32000, pytest.approx(0.002)))
    offline = open_run(load_settings(), run.store, "de")
    assert ensure_profile(offline, cv_id) == (first, None)
    assert ensure_profile(offline, run.store.save_cv("third.md", CV + " Third.")) == (None, "no client")


def test_analyze_postings_needs_a_client(tmp_path):
    run = open_run(load_settings(), Store(tmp_path / "s.sqlite"), "de")
    hashes, _ = ingest_postings(run, [("a.md", "Job A")])
    assert analyze_postings(run, plan_postings(run, [])).done == 0
    with pytest.raises(RuntimeError, match="no API client"):
        analyze_postings(run, plan_postings(run, hashes))


def test_rematch_without_client_uses_known_relations_only(tmp_path):
    store, cv_id, h = store_with_profile_and_job(tmp_path, [_skill("selenium")],
                                                 [_requirement("playwright"), _requirement("cypress")])
    store.save_relations("playwright", {"selenium": "close"})
    run = open_run(load_settings(), store, "de")  # no client: a relation call would fail
    [(jp, jm)] = rematch(run, cv_id)
    by_skill = {m.requirement.skill_id: m for m in jm.matches}
    assert jp.text_hash == h and by_skill["playwright"].status == "partial"
    assert by_skill["cypress"].status == "gap" and store.relations("cypress") == {}
    assert store.matches(cv_id) == [jm]


def test_rematch_reports_the_checks_and_then_the_saving(tmp_path):
    store, cv_id, _ = store_with_profile_and_job(tmp_path, [_skill("selenium")],
                                                 [_requirement("playwright"), _requirement("cypress")])
    run = open_run(load_settings(), store, "de", client=ScriptedClient())
    seen = []
    run.progress = lambda phase, **counts: seen.append((phase, counts))
    rematch(run, cv_id)
    assert seen == [("matching", {"done": done, "total": 2}) for done in range(3)] + [("saving_matches", {})]


def test_collecting_an_ended_batch_reports_the_saving_of_its_results(tmp_path):
    store = store_with_pending_batch(tmp_path)
    [(_, [h])] = store.pending_batches()
    batches = FakeBatchClient(results={h: ("succeeded", FROM_BATCH)}, statuses=["in_progress", "ended"])
    run = open_run(load_settings(), store, "de", client=batches)
    seen = []
    run.progress = lambda phase, **counts: seen.append(phase)
    collect_batches(run, wait=False)
    assert seen == []  # still running: nothing to save
    collect_batches(run, wait=False)
    assert seen == ["saving_results"] and store.job_profile(h).title == "From batch"


def test_rematch_without_profile_is_empty(tmp_path):
    run = open_run(load_settings(), Store(tmp_path / "s.sqlite"), "de")
    assert rematch(run, run.store.save_cv("cv.md", CV)) == []


def test_hidden_skill_is_not_matched(tmp_path):
    store, cv_id, _ = store_with_profile_and_job(tmp_path, [_skill("selenium", hidden=True), _skill("java")],
                                                 [_requirement("selenium")])
    [(_, jm)] = rematch(open_run(load_settings(), store, "de"), cv_id)
    assert jm.matches[0].status == "gap"


def test_rematch_uses_the_conditions_stored_in_the_database(tmp_path):
    licence = Requirement(skill_id="driving_licence_b", importance="must", min_level=None, min_years=None,
                          kind="condition", quote="Führerschein B", condition="driving_licence_b")
    store, cv_id, _ = store_with_profile_and_job(tmp_path, [_skill("selenium")], [_requirement("selenium"), licence])
    run = open_run(load_settings(), store, "de")
    [(_, jm)] = rematch(run, cv_id)
    assert (jm.matches[1].status, jm.tier) == ("unanswered", 1)
    store.save_conditions(Conditions(driving_licence_b=False))
    [(_, jm)] = rematch(run, cv_id)
    assert (jm.matches[1].status, jm.tier) == ("gap", 3) and store.matches(cv_id) == [jm]
    store.save_conditions(Conditions(driving_licence_b=True))
    assert [(m.matches[1].status, m.tier) for _, m in rematch(run, cv_id)] == [("met", 1)]
    assert store.profile(cv_id).conditions == Conditions()  # the answers stay in their own record


def test_rematch_offline_follows_new_answers_for_every_cv_and_calls_nothing(tmp_path):
    """The answers are not part of the match fingerprint, so a save of them recomputes every CV by hand. The run has a
    client that fails on any call: a requirement that needs a relation question gets none."""
    licence = Requirement(skill_id="driving_licence_b", importance="must", min_level=None, min_years=None,
                          kind="condition", quote="Führerschein B", condition="driving_licence_b")
    store, first, _ = store_with_profile_and_job(tmp_path, [_skill("selenium")],
                                                 [_requirement("selenium"), _requirement("docker"), licence])
    second = store.save_cv("other.md", CV + " Other.")
    store.save_profile(second, CandidateProfile(language="en", skills=[_skill("selenium")], stations=[]), "fake")
    store.save_cv("no_profile.md", CV + " Third.")
    api = FailingClient()
    run = open_run(load_settings(), store, "de", client=api)
    assert rematch_offline(run) == 2
    assert [store.matches(i)[0].matches[2].status for i in (first, second)] == ["unanswered", "unanswered"]
    store.save_conditions(Conditions(driving_licence_b=False))
    assert rematch_offline(run) == 2
    assert [(jm.matches[2].status, jm.tier) for i in (first, second) for jm in store.matches(i)] == [
        ("gap", 3), ("gap", 3)]
    store.save_conditions(Conditions(driving_licence_b=True))
    assert rematch_offline(run) == 2
    assert [store.matches(i)[0].matches[2].status for i in (first, second)] == ["met", "met"]
    assert api.calls == [] and run.client is api  # the run itself keeps its client


def test_merge_proposals_adds_skills_of_profiles_and_postings_after_the_run(tmp_path):
    store, cv_id, h = store_with_profile_and_job(tmp_path, [_skill("selenium")], [_requirement("n8n")])
    profile = store.profile(cv_id).model_copy(update={"proposed_skills": [
        SkillProposal(id="zapier", name="Zapier", category="tool")]})
    store.save_profile(cv_id, profile, "fake")
    job = store.job_profile(h).model_copy(update={"proposed_skills": [
        SkillProposal(id="n8n", name="n8n", category="tool")]})
    store.save_job_profile(job, "fake")
    run = open_run(load_settings(), store, "de")
    snapshot = run.snapshot
    added = merge_proposals(run, [cv_id], [h])
    assert sorted(s.id for s in added) == ["n8n", "zapier"] and run.snapshot == snapshot
    assert sorted(s.id for s in store.added_skills()) == ["n8n", "zapier"] and run.vocab.get("n8n") is not None


# --- market and paths ---

def test_market_reports_role_families(tmp_path):
    run, cv_id, _ = analyzed_run(tmp_path, n_postings=5)
    [rep] = market(run.store, run.settings, cv_id)
    assert (rep.role_family, rep.postings, rep.tier) == ("test_automation", 5, 1)
    assert [g.skill_id for g in rep.top_gaps] == ["kubernetes"]


def test_build_path_reuses_unchanged_input(tmp_path):
    run, cv_id, client = analyzed_run(tmp_path, n_postings=5)
    first, reason = build_path_for(run, cv_id, "test_automation")
    again, _ = build_path_for(run, cv_id, "test_automation")
    assert reason is None and first == again == run.store.paths(cv_id)["test_automation"]
    assert first.steps[0].closes == ["kubernetes"] and first.language == "de"
    assert sum(c.path == "PathDraft" for c in client.calls) == 1
    forced, _ = build_path_for(run, cv_id, "test_automation", force=True)
    assert forced is not None and sum(c.path == "PathDraft" for c in client.calls) == 2


def test_a_path_cut_off_at_the_token_limit_names_the_limit_and_the_cost(tmp_path):
    run, cv_id, client = analyzed_run(tmp_path, n_postings=5)
    original = client._answer
    cut = answer('{"rationale": ', stop_reason="max_tokens", usage=Usage(3000, 16000, 0, 0))
    client.beta.messages.create = lambda **kw: (cut if kw["output_config"]["format"]["schema"]["title"] == "PathDraft"
                                                else original(**kw))
    path, reason = build_path_for(run, cv_id, "test_automation")
    assert path is None and reason == Stopped("max_tokens", 16000, pytest.approx(3000 * 4e-6 + 16000 * 20e-6))
    assert str(reason) == "max_tokens" and run.store.paths(cv_id) == {}


def test_build_path_without_client_returns_only_a_stored_path(tmp_path):
    run, cv_id, _ = analyzed_run(tmp_path, n_postings=5)
    offline = open_run(run.settings, run.store, "de")
    assert build_path_for(offline, cv_id, "test_automation") == (None, "no client")
    path, _ = build_path_for(run, cv_id, "test_automation")
    assert build_path_for(offline, cv_id, "test_automation") == (path, None)


def test_build_path_builds_and_reuses_per_language(tmp_path):
    run, cv_id, client = analyzed_run(tmp_path, n_postings=5)
    german, _ = build_path_for(run, cv_id, "test_automation")
    english_run = open_run(run.settings, run.store, "en", client=client)
    offline = open_run(run.settings, run.store, "en")
    assert build_path_for(offline, cv_id, "test_automation") == (None, "no client")  # the German one is not reused
    english, reason = build_path_for(english_run, cv_id, "test_automation")
    assert reason is None and (german.language, english.language) == ("de", "en")
    paths = [c for c in client.calls if c.path == "PathDraft"]
    assert len(paths) == 2 and "in English." in str(paths[1].kwargs["system"])
    assert english.input_hash != german.input_hash
    assert run.store.paths_all(cv_id) == [german, english]  # both kept
    assert build_path_for(english_run, cv_id, "test_automation") == (english, None)
    assert build_path_for(run, cv_id, "test_automation") == (german, None)
    assert build_path_for(offline, cv_id, "test_automation") == (english, None)
    assert sum(c.path == "PathDraft" for c in client.calls) == 2  # each language once


def test_path_needs_enough_postings(tmp_path):
    settings = load_settings()
    five = settings.model_copy(update={"rules": settings.rules.model_copy(update={"min_jobs_per_role": 5})})
    run, cv_id, client = analyzed_run(tmp_path, n_postings=2, settings=five)
    assert build_path_for(run, cv_id, "test_automation") == (None, "too little data")
    assert build_path_for(run, cv_id, "qa_management") == (None, "too little data")
    assert not any(c.path == "PathDraft" for c in client.calls)


def test_path_needs_gaps(tmp_path):
    run, cv_id, client = analyzed_run(tmp_path, n_postings=5)
    profile = run.store.profile(cv_id)
    profile.skills.append(_skill("kubernetes"))
    run.store.save_profile(cv_id, profile, "fake")
    rematch(run, cv_id)
    assert build_path_for(run, cv_id, "test_automation") == (None, "no gaps")
    assert not any(c.path == "PathDraft" for c in client.calls)


def test_path_targets_prefer_tier_2():
    reports = [report("t1", 1), report("t2", 2), report("t3", 3), report("other", 2), report("few", None)]
    assert path_targets(reports, 2) == ["t2", "t3"]
    assert path_targets(reports, 9) == ["t2", "t3", "t1"]


def test_path_targets_then_more_postings_then_name():
    reports = [report("b", 2, postings=5), report("a", 2, postings=5), report("c", 2, postings=9)]
    assert path_targets(reports, 3) == ["c", "a", "b"]
