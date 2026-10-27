import logging
import re
import threading
import time

import pytest

from skillpose.web.tasks import Start, TaskRunner, Waiting


def wait_until_idle(runner: TaskRunner, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not runner.running


def test_inline_task_records_message():
    runner = TaskRunner(inline=True)
    assert runner.current is None and not runner.running
    assert runner.start("cv", "/cv/1", lambda: "profile ready: 3 skills")
    state = runner.current
    assert (state.name, state.target, state.ok, state.message) == ("cv", "/cv/1", True, "profile ready: 3 skills")
    assert state.finished is not None and state.finished >= state.started and not runner.running


class Held:
    """A task that runs until release() (at most 5 s); entered is set when it has started."""

    def __init__(self, message: str = "done"):
        self.entered, self._release, self.message = threading.Event(), threading.Event(), message

    def __call__(self) -> str:
        self.entered.set()
        self._release.wait(5)
        return self.message

    def release(self) -> None:
        self._release.set()


def test_a_task_started_while_another_runs_waits_and_starts_after_it():
    runner, first, ran = TaskRunner(), Held("first done"), []
    assert runner.start("postings", "/postings", first) is Start.STARTED
    assert first.entered.wait(5) and runner.running and runner.current.finished is None
    assert runner.start("cv", "/cv/1", lambda: ran.append("cv") or "second") is Start.QUEUED
    assert runner.waiting == [Waiting("cv", "/cv/1")] and runner.current.name == "postings" and ran == []
    first.release()
    wait_until_idle(runner)
    assert ran == ["cv"] and runner.waiting == []
    assert runner.current.name == "cv" and runner.current.ok and runner.current.message == "second"


def test_waiting_tasks_run_in_order_and_at_most_five_wait():
    runner, first, ran = TaskRunner(), Held(), []
    runner.start("postings", "/postings", first)
    assert first.entered.wait(5)
    for i in range(5):
        assert runner.start("cv", f"/cv/{i}", lambda i=i: ran.append(i) or f"cv {i}") is Start.QUEUED
    assert runner.start("cv", "/cv/9", lambda: ran.append(9) or "too many") is Start.REFUSED
    assert [w.target for w in runner.waiting] == [f"/cv/{i}" for i in range(5)]
    first.release()
    wait_until_idle(runner)
    assert ran == [0, 1, 2, 3, 4] and runner.current.message == "cv 4"


def test_a_quiet_check_is_never_queued():
    runner, first, ran = TaskRunner(), Held(), []
    runner.start("postings", "/postings", first)
    assert first.entered.wait(5)
    assert runner.start("collect", "/postings", lambda: ran.append(1) or "", quiet=True) is Start.REFUSED
    assert runner.waiting == []
    first.release()
    wait_until_idle(runner)
    assert ran == []


def test_the_same_task_is_not_queued_twice():
    # a double click: the second request gets the status of the task that runs or waits already
    runner, first, ran = TaskRunner(), Held(), []
    assert runner.start("cv", "/cv/1", first) is Start.STARTED
    assert first.entered.wait(5)
    assert runner.start("cv", "/cv/1", lambda: ran.append("again") or "") is Start.STARTED
    assert runner.start("postings", "/postings", lambda: ran.append("postings") or "") is Start.QUEUED
    assert runner.start("postings", "/postings", lambda: ran.append("twice") or "") is Start.QUEUED
    # the same name and page, another key (the role family of a path): two tasks
    assert runner.start("path", "/cv/1/paths", lambda: ran.append("a") or "", key="a") is Start.QUEUED
    assert runner.start("path", "/cv/1/paths", lambda: ran.append("b") or "", key="b") is Start.QUEUED
    assert runner.start("path", "/cv/1/paths", lambda: ran.append("a2") or "", key="a") is Start.QUEUED
    assert [(w.name, w.key) for w in runner.waiting] == [("postings", ""), ("path", "a"), ("path", "b")]
    first.release()
    wait_until_idle(runner)
    assert ran == ["postings", "a", "b"]


def test_a_page_is_busy_while_a_task_of_it_or_of_a_page_below_it_runs_or_waits():
    # review 15o: the path task of CV 1 belongs to /cv/1/paths, and it needs CV 1 as much as an analysis does
    runner, first = TaskRunner(), Held()
    assert not runner.busy_with("/cv/1")
    runner.start("path", "/cv/1/paths", first, key="it")
    assert first.entered.wait(5)
    assert runner.busy_with("/cv/1") and runner.busy_with("/cv/1/paths")
    assert not runner.busy_with("/cv/10") and not runner.busy_with("/cv/2")
    assert runner.start("path", "/cv/12/paths", lambda: "", key="it") is Start.QUEUED
    assert runner.start("cv", "/cv/3", lambda: "") is Start.QUEUED
    assert runner.busy_with("/cv/12") and runner.busy_with("/cv/3")
    assert not runner.busy_with("/cv/1/paths/it") and not runner.busy_with("/cv/4")
    first.release()
    wait_until_idle(runner)
    assert not any(runner.busy_with(page) for page in ("/cv/1", "/cv/12", "/cv/3"))  # a finished task counts no more


def test_a_task_of_a_name_and_key_is_found_whatever_its_page():
    # 15y review: the rematch of a save of the answers belongs to the page of one CV but recomputes every CV
    runner, first = TaskRunner(), Held()
    assert not runner.runs_or_waits("rematch", "conditions")
    runner.start("rematch", "/cv/1", first, key="conditions")
    assert first.entered.wait(5)
    assert runner.runs_or_waits("rematch", "conditions")
    assert not runner.runs_or_waits("rematch", "") and not runner.runs_or_waits("cv", "conditions")
    first.release()
    wait_until_idle(runner)
    assert not runner.runs_or_waits("rematch", "conditions")  # a finished task counts no more
    second = Held()
    runner.start("postings", "/postings", second)
    assert second.entered.wait(5)
    assert runner.start("rematch", "/cv/2", lambda: "", key="conditions") is Start.QUEUED
    assert runner.runs_or_waits("rematch", "conditions")  # waiting
    second.release()
    wait_until_idle(runner)
    assert not runner.runs_or_waits("rematch", "conditions")


def test_a_failing_task_does_not_block_the_queue(caplog):
    runner, first, ran = TaskRunner(), Held(), []

    def boom() -> str:
        raise RuntimeError("no connection")

    runner.start("postings", "/postings", first)
    assert first.entered.wait(5)
    runner.start("collect", "/postings", boom)
    runner.start("cv", "/cv/1", lambda: ran.append("cv") or "after the error")
    with caplog.at_level(logging.ERROR):
        first.release()
        wait_until_idle(runner)
    assert ran == ["cv"] and runner.current.message == "after the error" and "no connection" in caplog.text


class Abort(BaseException):
    """Not an Exception: like SystemExit or KeyboardInterrupt."""


def test_a_base_exception_in_a_task_ends_the_task_and_is_raised_again():
    # the old deferred minor: before, the runner stayed "running" for ever
    runner = TaskRunner(inline=True)

    def abort() -> str:
        raise Abort("stop")

    with pytest.raises(Abort):
        runner.start("cv", "/cv/1", abort)
    assert not runner.running and runner.current.ok is False and runner.current.message == "stop"
    assert runner.start("cv", "/cv/1", lambda: "again") is Start.STARTED and runner.current.message == "again"


def test_a_base_exception_in_a_thread_does_not_block_the_queue():
    runner, first, ran = TaskRunner(), Held(), []

    def leave() -> str:
        raise SystemExit(3)

    runner.start("postings", "/postings", first)
    assert first.entered.wait(5)
    runner.start("collect", "/postings", leave)
    runner.start("cv", "/cv/1", lambda: ran.append("cv") or "still runs")
    first.release()
    wait_until_idle(runner)
    assert ran == ["cv"] and runner.current.message == "still runs"


def test_closing_drops_the_waiting_tasks_and_lets_the_running_one_end():
    runner, first, ran = TaskRunner(), Held("first done"), []
    runner.start("postings", "/postings", first)
    assert first.entered.wait(5)
    runner.start("cv", "/cv/1", lambda: ran.append("cv") or "")
    runner.close()  # the app shuts down: nothing that waits may start (it would be paid after Ctrl-C)
    assert runner.waiting == [] and runner.start("cv", "/cv/2", lambda: "") is Start.REFUSED
    first.release()
    assert runner.join(5)
    assert ran == [] and runner.current.message == "first done"


def test_exception_becomes_error_message(caplog):
    runner = TaskRunner(inline=True)

    def boom() -> str:
        raise RuntimeError("monthly limit reached")

    with caplog.at_level(logging.ERROR):
        assert runner.start("path", "/cv/1/paths", boom)
    state = runner.current
    assert state.ok is False and state.message == "monthly limit reached" and state.finished is not None
    assert not runner.running and "path" in caplog.text and "RuntimeError" in caplog.text
    assert runner.start("path", "/cv/1/paths", lambda: "again")  # a failed task does not block the next one


def test_quiet_task_without_message_leaves_no_trace():
    runner = TaskRunner(inline=True)
    assert runner.start("collect", "/postings", lambda: "", quiet=True)
    assert runner.current is None and not runner.running  # nothing before it, nothing after it
    runner.start("postings", "/postings", lambda: "20 postings submitted")
    last = runner.current
    seen = []
    assert runner.start("collect", "/postings", lambda: seen.append(runner.running) or "", quiet=True)
    assert seen == [True] and runner.current is last  # it ran, and the last result is back
    runner.start("collect", "/postings", lambda: "batch collected", quiet=True)
    assert runner.current.name == "collect" and runner.current.message == "batch collected"


def test_quiet_task_that_fails_is_reported():
    runner = TaskRunner(inline=True)
    runner.start("postings", "/postings", lambda: "done")

    def boom() -> str:
        raise RuntimeError("no connection")

    runner.start("collect", "/postings", boom, quiet=True)
    assert runner.current.name == "collect" and runner.current.ok is False and runner.current.message == "no connection"


def test_a_task_thread_is_not_a_daemon():
    # Ctrl-C during a paid call: the interpreter waits for the thread, so the answer is still booked in the ledger
    runner, seen = TaskRunner(), []
    assert runner.start("cv", "/cv/1", lambda: seen.append(threading.current_thread().daemon) or "done")
    wait_until_idle(runner)
    assert seen == [False] and runner.current.message == "done"


def test_quiet_threaded_task_shows_as_running_and_then_steps_back():
    runner, entered, release = TaskRunner(), threading.Event(), threading.Event()
    runner.start("postings", "/postings", lambda: "first")
    wait_until_idle(runner)
    first = runner.current

    def check() -> str:
        entered.set()
        release.wait(5)
        return ""

    assert runner.start("collect", "/postings", check, quiet=True)
    assert entered.wait(5) and runner.running and runner.current.name == "collect"
    release.set()
    wait_until_idle(runner)
    assert runner.current is first


def test_a_running_task_reports_its_progress_and_the_finished_state_drops_it():
    runner, seen = TaskRunner(inline=True), []

    def work() -> str:
        runner.report("Saving results")
        seen.append(runner.current.progress)
        runner.report("Matching with the CV: 1 of 3 checked")
        seen.append(runner.current.progress)
        return "done"

    runner.start("collect", "/postings", work)
    assert seen == ["Saving results", "Matching with the CV: 1 of 3 checked"]
    assert runner.current.progress == "" and runner.current.message == "done"


def test_progress_from_another_thread_or_without_a_task_changes_nothing():
    runner, entered, release = TaskRunner(), threading.Event(), threading.Event()
    runner.report("nothing runs")
    assert runner.current is None

    def slow() -> str:
        entered.set()
        release.wait(5)
        return "done"

    runner.start("cv", "/cv/1", slow)
    assert entered.wait(5)
    runner.report("from the thread of a request")  # only the thread that runs the task reports its progress
    assert runner.current.progress == ""
    release.set()
    wait_until_idle(runner)


def test_the_log_has_start_end_duration_and_result_of_each_task(caplog):
    runner, first = TaskRunner(), Held("Profile ready: 3 skills")

    def boom() -> str:
        raise RuntimeError("no connection")

    with caplog.at_level(logging.INFO, logger="skillpose"):
        runner.start("cv", "/cv/1", first)
        assert first.entered.wait(5)
        runner.start("path", "/cv/1/paths", boom, key="test_automation")
        first.release()
        wait_until_idle(runner)
        runner.start("collect", "/postings", lambda: "", quiet=True)  # a check that finds nothing: no line
        wait_until_idle(runner)
    lines = [r.getMessage() for r in caplog.records if r.name == "skillpose.web.tasks"]
    assert len(lines) == 5, lines
    assert lines[0] == "task cv for /cv/1 started"
    assert lines[1] == "task path for /cv/1/paths queued (1 waiting)"
    assert re.fullmatch(r"task cv for /cv/1 ended ok after \d+\.\d s: Profile ready: 3 skills", lines[2])
    assert lines[3] == "task path for /cv/1/paths started"
    assert re.fullmatch(r"task path for /cv/1/paths failed after \d+\.\d s: no connection", lines[4])
