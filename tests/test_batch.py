import logging
from types import SimpleNamespace

import pytest
from fakes import FakeBatchClient, FakeLedger

from skillpose.core.batch import (
    batch_seconds,
    collect_jobs_batch,
    extract_jobs_batch,
    read_jobs_batch,
    submit_jobs_batch,
)
from skillpose.core.config import load_settings
from skillpose.core.llm import BudgetExceeded, estimate_usd
from skillpose.core.models import JobDraft, text_hash
from skillpose.core.vocabulary import Vocabulary


def test_batch_maps_by_custom_id_and_books_half_price():
    texts = {text_hash("Job A"): "Job A", text_hash("Job B"): "Job B", text_hash("Job C"): "Job C"}
    ok_json = JobDraft(title="QA", role_family="test_automation", seniority="mid", requirements=[]).model_dump_json()
    client = FakeBatchClient(results={text_hash("Job B"): ("succeeded", ok_json),
                                      text_hash("Job A"): ("errored", None),
                                      text_hash("Job C"): ("succeeded", "not json")},
                             statuses=["in_progress", "ended"])
    ledger = FakeLedger()
    sleeps = []
    out = extract_jobs_batch(client, load_settings(), ledger, texts, Vocabulary.load(), "SNAP", "de",
                             sleep=sleeps.append)
    assert out[text_hash("Job A")] is None and out[text_hash("Job C")] is None
    assert out[text_hash("Job B")].title == "QA" and out[text_hash("Job B")].text_hash == text_hash("Job B")
    assert len(ledger.records) == 2 and all(r.batch for r in ledger.records)
    assert ledger.records[0].cost_usd == pytest.approx((2000 * 0.10 + 800 * 0.50 + 5000 * 0.01) / 1e6 / 2)
    assert sleeps == [30.0]
    params = client.created[0].params
    assert "fallbacks" not in params and params["output_config"]["format"]["type"] == "json_schema"
    assert params["output_config"]["effort"] == "low" and "cache_control" in params["system"][-1]
    assert {r.custom_id for r in client.created} == set(texts)


def test_submit_and_collect_are_separate_steps():
    ok_json = JobDraft(title="QA", role_family="test_automation", seniority="mid", requirements=[]).model_dump_json()
    texts = {text_hash("Job A"): "Job A"}
    client = FakeBatchClient(results={text_hash("Job A"): ("succeeded", ok_json)}, statuses=["ended"])
    batch_id = submit_jobs_batch(client, load_settings(), FakeLedger(), texts, "SNAP", "de")
    assert batch_id == "batch_1" and len(client.created) == 1
    out = collect_jobs_batch(client, load_settings(), FakeLedger(), batch_id, texts, Vocabulary.load(),
                             sleep=lambda s: None)
    assert out[text_hash("Job A")].title == "QA"


def test_each_result_of_a_batch_is_logged_with_the_time_the_batch_took(caplog):
    # a batch result has no duration of its own: its line names how long the batch took from submission to end
    ok_json = JobDraft(title="QA", role_family="test_automation", seniority="mid", requirements=[]).model_dump_json()
    texts = {text_hash("Job A"): "Job A", text_hash("Job B"): "Job B"}
    client = FakeBatchClient(results={h: ("succeeded", ok_json) for h in texts}, statuses=["ended"], took=754.25)
    batch_id = submit_jobs_batch(client, load_settings(), FakeLedger(), texts, "SNAP", "de")
    with caplog.at_level(logging.INFO, logger="skillpose"):
        collect_jobs_batch(client, load_settings(), FakeLedger(), batch_id, texts, Vocabulary.load(),
                           sleep=lambda s: None)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("api call job: ")]
    assert lines == ["api call job: claude-haiku-5-5, input 2000, output 800, cache write 0, cache read 5000, "
                     "0.0003 USD, 754.2 s, batch batch_1"] * 2


def test_a_batch_whose_status_answer_has_no_times_is_logged_as_duration_unknown(caplog):
    ended = SimpleNamespace(id="batch_1", processing_status="ended", created_at=None, ended_at=None)
    assert batch_seconds(ended) is None
    ok_json = JobDraft(title="QA", role_family="test_automation", seniority="mid", requirements=[]).model_dump_json()
    client = FakeBatchClient(results={text_hash("Job A"): ("succeeded", ok_json)}, statuses=[])
    with caplog.at_level(logging.INFO, logger="skillpose"):
        read_jobs_batch(client, load_settings(), FakeLedger(), "batch_1", {text_hash("Job A"): "Job A"},
                        Vocabulary.load(), seconds=batch_seconds(ended))
    assert any(r.getMessage().endswith("USD, duration unknown, batch batch_1") for r in caplog.records)


def test_batch_refuses_when_its_estimate_exceeds_the_limit():
    client = FakeBatchClient(results={}, statuses=[])
    texts = {text_hash(f"Job {i}"): f"Job {i}" for i in range(100)}
    with pytest.raises(BudgetExceeded):
        extract_jobs_batch(client, load_settings(), FakeLedger(total=4.999), texts, Vocabulary.load(), "S", "de",
                           sleep=lambda s: None)
    assert client.created == []


def test_batch_respects_budget():
    with pytest.raises(BudgetExceeded):
        extract_jobs_batch(FakeBatchClient(results={}, statuses=[]), load_settings(), FakeLedger(total=5.0),
                           {"h": "x"}, Vocabulary.load(), "S", "de", sleep=lambda s: None)


def test_each_batch_must_fit_together_with_the_batches_submitted_before_it():
    # submitted batches are booked only when collected: the next one must leave room for them
    texts = {text_hash(f"Job {i}"): f"Job {i}" for i in range(10)}
    one = estimate_usd(load_settings(), "job", 10, batch=True)
    settings = load_settings().model_copy(update={"monthly_limit_usd": 1.5 * one})
    client = FakeBatchClient(results={}, statuses=[])
    assert submit_jobs_batch(client, settings, FakeLedger(), texts, "S", "de") == "batch_1"
    with pytest.raises(BudgetExceeded, match="does not fit"):
        submit_jobs_batch(client, settings, FakeLedger(), texts, "S", "de", reserved_usd=one)
    assert list(client.batches) == ["batch_1"]
