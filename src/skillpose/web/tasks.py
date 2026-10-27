"""Background tasks of the web app: one runs at a time, the others wait in line.

Paid work (an analysis, a batch, a path) runs in a thread, so a page answers at once and shows the task line. One task
at a time keeps two runs from paying for the same work. A task started while another runs waits in a queue (first in,
first out) and starts when the one before it has ended, with or without an error; every task makes its plan only when
it runs, so it sees what the task before it did and pays for nothing twice. The last task stays visible with its finish
time and message until the next one starts.
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from skillpose.logs import safe_error

log = logging.getLogger(__name__)

MAX_WAITING = 5


class Start(StrEnum):
    STARTED = "started"    # runs now, or the same task runs already
    QUEUED = "queued"      # waits for the tasks before it, or the same task waits already
    REFUSED = "refused"    # not run: the queue is full, a quiet check found the runner busy, or the app shuts down


@dataclass(frozen=True)
class TaskState:
    name: str                          # cv | rematch | postings | collect | path (label: task.<name>)
    target: str                        # the page the task belongs to, e.g. /cv/3
    started: datetime
    finished: datetime | None = None
    ok: bool | None = None
    message: str = ""                  # what fn returned, or the error
    progress: str = ""                 # while it runs: how far it is (report), e.g. "Saving matches"


@dataclass(frozen=True)
class Waiting:
    name: str
    target: str
    key: str = ""                      # tells two tasks of one name and page apart, e.g. the role family of a path


@dataclass(frozen=True)
class _Job:
    task: Waiting
    fn: Callable[[], str]
    quiet: bool


class TaskRunner:
    def __init__(self, inline: bool = False, max_waiting: int = MAX_WAITING):
        self.inline = inline           # tests: run fn in start() itself
        self.max_waiting = max_waiting
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._current: TaskState | None = None
        self._job: _Job | None = None    # the running task
        self._waiting: deque[_Job] = deque()
        self._worker: int | None = None  # the thread that runs the current task: only it reports progress
        self._closed = False

    @property
    def current(self) -> TaskState | None:
        """The running task, else the last finished one."""
        return self._current

    @property
    def running(self) -> bool:
        return self._job is not None

    @property
    def waiting(self) -> list[Waiting]:
        """The tasks in line, the next first."""
        with self._lock:
            return [job.task for job in self._waiting]

    def waits_for(self, target: str) -> bool:
        """A task of this page is in line."""
        return any(task.target == target for task in self.waiting)

    def busy_with(self, page: str) -> bool:
        """A task of this page or of a page below it runs or waits: /cv/3 covers the paths of CV 3 (/cv/3/paths),
        not /cv/30."""
        def below(target: str) -> bool:
            return target == page or target.startswith(f"{page}/")

        with self._lock:
            return (self._job is not None and below(self._job.task.target)) or any(
                below(job.task.target) for job in self._waiting)

    def runs_or_waits(self, name: str, key: str) -> bool:
        """A task of this name and key runs or waits, whatever its page (the save of the answers to the conditions
        belongs to the page of one CV but recomputes every CV)."""
        with self._lock:
            jobs = [*([self._job] if self._job is not None else []), *self._waiting]
            return any(job.task.name == name and job.task.key == key for job in jobs)

    def report(self, text: str) -> None:
        """How far the running task is, for the task line. Only the thread that runs it may say so: a call from any
        other thread (or when nothing runs) changes nothing."""
        with self._lock:
            current = self._current
            if current is not None and current.finished is None and self._worker == threading.get_ident():
                self._current = replace(current, progress=text)

    def accepts(self, name: str, target: str, key: str = "") -> bool:
        """Whether start would run or queue this task now (a caller that must change data first asks this)."""
        task = Waiting(name, target, key)
        with self._lock:
            return not self._closed and (self._job is None or self._job.task == task
                                         or any(job.task == task for job in self._waiting)
                                         or len(self._waiting) < self.max_waiting)

    def start(self, name: str, target: str, fn: Callable[[], str], *, quiet: bool = False, key: str = "") -> Start:
        """Run fn in its own thread now, or queue it until the tasks before it have ended.

        A task of the same name, target and key that runs or waits already is not added again (a double click): the
        answer is the status of that one. At most max_waiting tasks wait; beyond that the task is refused.

        quiet: a check that may find nothing. It runs only when no task runs or waits, else it is refused. If it ends
        without error and with an empty message, it leaves no trace: the state from before comes back, so a check
        cannot wipe the result of the last task before anyone has read it.
        """
        job = _Job(task=Waiting(name, target, key), fn=fn, quiet=quiet)
        with self._lock:
            if self._closed:
                return Start.REFUSED
            if self._job is not None:
                if quiet:
                    return Start.REFUSED
                if self._job.task == job.task:
                    return Start.STARTED
                if any(waiting.task == job.task for waiting in self._waiting):
                    return Start.QUEUED
                if len(self._waiting) >= self.max_waiting:
                    return Start.REFUSED
                self._waiting.append(job)
                log.info("task %s for %s queued (%d waiting)", name, target, len(self._waiting))
                return Start.QUEUED
            previous = self._begin(job)
        self._launch(job, previous)
        return Start.STARTED

    def close(self) -> None:
        """The app shuts down: the waiting tasks are dropped (they would start after the user stopped the app) and no
        new task starts. A running task ends on its own."""
        with self._lock:
            self._closed = True
            self._waiting.clear()

    def join(self, timeout: float | None = None) -> bool:
        """Wait until no task runs; False: the timeout came first."""
        with self._idle:
            return self._idle.wait_for(lambda: self._job is None, timeout)

    def _begin(self, job: _Job) -> TaskState | None:
        """Make job the running task (the caller holds the lock). Returns the state a quiet check brings back."""
        previous = self._current if job.quiet else None
        self._job = job
        self._current = TaskState(name=job.task.name, target=job.task.target, started=datetime.now(UTC))
        return previous

    def _launch(self, job: _Job, previous: TaskState | None) -> None:
        if self.inline:
            self._run(job, previous)
        else:
            # not a daemon: after Ctrl-C the interpreter waits for the whole running task to end (it may start
            # several confirmed calls), so their cost is booked; each call has an SDK timeout, so the wait is bounded
            threading.Thread(target=self._run, args=(job, previous), name=f"skillpose-task-{job.task.name}",
                             daemon=False).start()

    def _run(self, job: _Job, previous: TaskState | None) -> None:
        """Run job and log it: start, end, duration and result; a quiet check that finds nothing leaves no line."""
        with self._lock:
            self._worker = threading.get_ident()
        name, target, started = job.task.name, job.task.target, time.monotonic()
        if not job.quiet:
            log.info("task %s for %s started", name, target)
        raised: BaseException | None = None
        try:
            message, ok = job.fn() or "", True
        except BaseException as e:  # also SystemExit and the like: the runner must never stay "running"
            log.error("task %s for %s failed after %.1f s: %s", name, target, time.monotonic() - started,
                      safe_error(e), exc_info=True)
            message, ok = str(e) or type(e).__name__, False
            raised = None if isinstance(e, Exception) else e
        else:
            if message or not job.quiet:
                log.info("task %s for %s ended ok after %.1f s%s", name, target, time.monotonic() - started,
                         f": {message}" if message else "")
        following = self._end(job, ok, message, previous)
        if following is not None:
            self._launch(*following)
        if raised is not None and self.inline:  # in the thread of the caller: it goes on there; a task thread just ends
            raise raised

    def _end(self, job: _Job, ok: bool, message: str,
             previous: TaskState | None) -> tuple[_Job, TaskState | None] | None:
        """Record the end of job and begin the next one in line; returns it (to launch) or None."""
        with self._lock:
            self._worker = None
            if job.quiet and ok and not message:
                self._current = previous
            else:
                self._current = replace(self._current, finished=datetime.now(UTC), ok=ok, message=message,
                                        progress="")
            self._job = None
            if self._waiting:
                following = self._waiting.popleft()
                return following, self._begin(following)
            self._idle.notify_all()
            return None
