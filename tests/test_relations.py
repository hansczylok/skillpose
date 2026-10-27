import threading
from concurrent.futures import Future

import pytest
from fakes import FakeLLM, RelationClient, api_error

from skillpose.core.config import load_settings
from skillpose.core.llm import LLM, BudgetExceeded
from skillpose.core.models import RelatedSkill, RelationAnswer
from skillpose.core.relations import RelationService
from skillpose.core.vocabulary import Vocabulary
from skillpose.store.db import Store

V = Vocabulary.load()


def _answer(*pairs) -> RelationAnswer:
    return RelationAnswer(related=[RelatedSkill(skill_id=s, relation=r, reason="") for s, r in pairs])


def test_relation_service_asks_only_unknown_pairs(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.save_relations("uipath", {"excel": "none"})
    llm = FakeLLM(value=_answer(("power_automate", "close")))
    rel = RelationService(llm, st, "SNAP", V).resolve({"uipath": ["excel", "power_automate", "python"]})
    assert "power_automate, python" in llm.calls[0].variable and "excel" not in llm.calls[0].variable
    assert rel[("uipath", "power_automate")] == "close" and st.relations("uipath")["python"] == "none"
    assert rel[("uipath", "excel")] == "none"


def test_known_pairs_need_no_call(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.save_relations("uipath", {"excel": "none", "python": "distant"})
    llm = FakeLLM(value=None)
    rel = RelationService(llm, st, "SNAP", V).resolve({"uipath": ["excel", "python"]})
    assert llm.calls == [] and rel[("uipath", "python")] == "distant"


def test_failed_call_stores_nothing(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    rel = RelationService(FakeLLM(value=None, error="overloaded"), st, "SNAP", V).resolve({"uipath": ["python"]})
    assert rel == {} and st.relations("uipath") == {}


def test_answer_with_names_instead_of_ids_is_mapped(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    llm = FakeLLM(value=_answer(("Power Automate", "close")))
    rel = RelationService(llm, st, "SNAP", V).resolve({"uipath": ["power_automate", "python"]})
    assert rel[("uipath", "power_automate")] == "close"
    assert st.relations("uipath") == {"power_automate": "close", "python": "none"}


def test_answer_naming_a_known_skill_that_was_not_asked_is_ignored(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    llm = FakeLLM(value=_answer(("excel", "close"), ("python", "distant")))
    RelationService(llm, st, "SNAP", V).resolve({"uipath": ["power_automate", "python"]})
    assert st.relations("uipath") == {"power_automate": "none", "python": "distant"}


def test_unmappable_answer_stores_no_none(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    llm = FakeLLM(value=_answer(("Robotic process thingy", "close"), ("python", "distant")))
    rel = RelationService(llm, st, "SNAP", V).resolve({"uipath": ["power_automate", "python"]})
    assert st.relations("uipath") == {"python": "distant"}
    assert rel == {("uipath", "python"): "distant"}


# --- several checks at once (parallel checks) ---

TARGETS = ["uipath", "excel", "docker", "kubernetes", "aws", "selenium", "jira", "git", "power_bi", "tableau"]
PAIRS = {target: ["python", "java", "sql"] for target in TARGETS}
ANSWERS = {target: [("python", "close")] if i % 2 else [("java", "distant"), ("sql", "close")]
           for i, target in enumerate(TARGETS)}
# the later targets answer faster, so in four at once they end before the earlier ones
SLOWER_FIRST = {target: 0.05 - 0.004 * i for i, target in enumerate(TARGETS)}


class ThreadStore(Store):
    """A store that notes the thread of every ledger read, ledger write and relation write."""

    def __init__(self, path):
        super().__init__(path)
        self.threads: set[int] = set()

    def month_total_usd(self, now=None):
        self.threads.add(threading.get_ident())
        return super().month_total_usd(now)

    def record(self, *args):
        self.threads.add(threading.get_ident())
        super().record(*args)

    def save_relations(self, target, rel):
        self.threads.add(threading.get_ident())
        super().save_relations(target, rel)


def _service(tmp_path, client, workers, *, name="s.sqlite", limit=None):
    settings = load_settings() if limit is None else load_settings().model_copy(update={"monthly_limit_usd": limit})
    store = ThreadStore(tmp_path / name)
    return RelationService(LLM(client, settings, store), store, "SNAP", V, workers=workers), store


def _ledger(store):
    return [(r.step, r.model, r.input, r.output, r.cache_write, r.cache_read, r.usd, r.batch)
            for r in store.cost_rows()]


def test_four_at_once_store_the_same_relations_and_costs_as_one_after_the_other(tmp_path):
    results, stored, ledgers = [], [], []
    for workers in (1, 4):
        client = RelationClient(ANSWERS, delay=SLOWER_FIRST)
        service, store = _service(tmp_path, client, workers, name=f"w{workers}.sqlite")
        results.append(service.resolve(PAIRS))
        stored.append({target: store.relations(target) for target in TARGETS})
        ledgers.append(_ledger(store))
        assert client.concurrency.most == workers
    assert results[0] == results[1] and stored[0] == stored[1] and ledgers[0] == ledgers[1]
    assert len(ledgers[1]) == len(TARGETS) and stored[1]["excel"] == {"python": "close", "java": "none", "sql": "none"}


def test_the_first_check_runs_alone_and_writes_the_cache(tmp_path):
    client = RelationClient(ANSWERS, delay=SLOWER_FIRST)
    service, _ = _service(tmp_path, client, 4)
    service.resolve(PAIRS)
    assert client.events[:2] == [("start", "uipath"), ("end", "uipath")]
    assert client.concurrency.most == 4


def test_only_the_api_requests_run_in_other_threads(tmp_path):
    # SQLite connections and the ledger are not thread-safe: reads and writes stay in the thread that resolves
    client = RelationClient(ANSWERS, delay=SLOWER_FIRST)
    service, store = _service(tmp_path, client, 4)
    service.resolve(PAIRS)
    assert store.threads == {threading.get_ident()}
    assert threading.get_ident() not in client.threads and len(client.threads) > 1


def test_known_targets_need_no_check_and_the_rest_runs_at_once(tmp_path):
    client = RelationClient(ANSWERS)
    service, store = _service(tmp_path, client, 4)
    store.save_relations("uipath", {"python": "none", "java": "none", "sql": "none"})
    store.save_relations("excel", {"python": "close"})
    rel = service.resolve(PAIRS)
    asked = [target for kind, target in client.events if kind == "start"]
    assert "uipath" not in asked and asked[0] == "excel" and sorted(asked) == sorted(TARGETS[1:])
    assert rel[("uipath", "java")] == "none" and rel[("excel", "python")] == "close"
    assert store.relations("excel") == {"python": "close", "java": "none", "sql": "none"}


def test_a_failed_check_loses_none_of_the_others(tmp_path):
    client = RelationClient(ANSWERS, fail={"docker": api_error(529)})
    service, store = _service(tmp_path, client, 4)
    rel = service.resolve(PAIRS)
    assert store.relations("docker") == {} and not any(target == "docker" for target, _ in rel)
    assert all(len(store.relations(target)) == 3 for target in TARGETS if target != "docker")
    assert len(store.cost_rows()) == len(TARGETS) - 1


def test_an_unexpected_error_stops_new_checks_but_keeps_those_under_way(tmp_path):
    client = RelationClient(ANSWERS, fail={"docker": RuntimeError("bug")})
    service, store = _service(tmp_path, client, 4)
    with pytest.raises(RuntimeError, match="bug"):
        service.resolve(PAIRS)
    # uipath alone, then excel, docker, kubernetes and aws; after excel: selenium; docker fails, nothing more starts
    asked = [target for kind, target in client.events if kind == "start"]
    assert sorted(asked) == sorted(TARGETS[:6])
    assert [t for t in TARGETS if store.relations(t)] == ["uipath", "excel", "kubernetes", "aws", "selenium"]
    assert len(store.cost_rows()) == 5


def test_ctrl_c_while_waiting_for_a_check_still_books_every_check_that_was_sent(tmp_path, monkeypatch):
    # Ctrl-C in analyze, eval or demo build arrives in the thread that waits for the answer of a check: that check
    # still ends in its worker thread and is paid for, so it must reach the ledger like the others under way
    client = RelationClient(ANSWERS)
    service, store = _service(tmp_path, client, 2)
    result, waits = Future.result, []

    def interrupted(future, timeout=None):
        waits.append(future)
        if len(waits) == 2:  # the wait for the second check (the first ran alone)
            raise KeyboardInterrupt
        return result(future, timeout)

    monkeypatch.setattr(Future, "result", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.resolve({target: PAIRS[target] for target in TARGETS[:3]})
    assert [target for kind, target in client.events if kind == "start"] == TARGETS[:3]
    assert len(store.cost_rows()) == 3  # sent three, booked three
    assert [t for t in TARGETS if store.relations(t)] == TARGETS[:1]  # only booked: no store write after Ctrl-C


@pytest.mark.parametrize("workers, calls", [(1, 2), (4, 5)])
def test_the_monthly_limit_stops_new_checks_and_books_those_under_way(tmp_path, workers, calls):
    # one check costs about 0.00011 USD: the limit is reached after the second one; with four at once, three more
    # (workers - 1) are already under way then, and they are booked and stored
    client = RelationClient(ANSWERS)
    service, store = _service(tmp_path, client, workers, limit=0.00015)
    with pytest.raises(BudgetExceeded):
        service.resolve(PAIRS)
    assert len([kind for kind, _ in client.events if kind == "start"]) == calls
    assert len(store.cost_rows()) == calls and [t for t in TARGETS if store.relations(t)] == TARGETS[:calls]


def test_progress_counts_the_targets_that_need_a_check(tmp_path):
    service, store = _service(tmp_path, RelationClient(ANSWERS), 4)
    store.save_relations("uipath", {"python": "none", "java": "none", "sql": "none"})
    seen = []
    service.resolve(PAIRS, progress=lambda done, total: seen.append((done, total)))
    assert seen == [(done, 9) for done in range(10)]
    seen.clear()
    service.resolve(PAIRS, progress=lambda done, total: seen.append((done, total)))
    assert seen == []  # everything is known now: no check, nothing to count
