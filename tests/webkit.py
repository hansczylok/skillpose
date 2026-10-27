"""Test client of the web app: own database in tmp_path, tasks run inline, the API client is a fake."""

import threading
import time
from contextlib import contextmanager
from pathlib import Path

from fakes import ScriptedClient
from fastapi.testclient import TestClient

from skillpose.core.config import Settings, load_settings
from skillpose.core.llm import Usage
from skillpose.store.db import Store
from skillpose.web.app import WebConfig, create_app
from skillpose.web.tasks import TaskRunner


def make_client(tmp_path: Path, *, key: bool = True, client=None, settings: Settings | None = None,
                password: str | None = None, demo_dir: Path | None = None) -> TestClient:
    """key=False: no API key, the app offers the demo only. The database is tmp_path / "web.sqlite"."""
    if demo_dir is None:
        demo_dir = tmp_path / "no_demo"
        demo_dir.mkdir(exist_ok=True)
    config = WebConfig(db_path=tmp_path / "web.sqlite", settings=settings or load_settings(),
                       client_factory=(lambda: client or ScriptedClient()) if key else None, demo_dir=demo_dir,
                       password=password, allowed_hosts=["testserver"], inline_tasks=True)
    return TestClient(create_app(config))


@contextmanager
def task_running(c, name="postings", target="/postings", *, spend: float = 0.0, key: str = ""):
    """A task that runs until the block ends; the app shows its task line meanwhile. spend: US dollars the task books
    in the own database just before it ends, as a paid task would (a task waiting behind it finds less of the
    monthly limit left). key: the key of the task (TaskRunner.start)."""
    runner = TaskRunner()
    c.app.state.skillpose.tasks = runner
    entered, release = threading.Event(), threading.Event()
    db_path = c.app.state.skillpose.config.db_path

    def slow() -> str:
        entered.set()
        release.wait(5)
        if spend:
            with Store(db_path) as store:
                store.record("job", "claude-haiku-5-5", Usage(0, 0, 0, 0), spend, False)
        return "done"

    assert runner.start(name, target, slow, key=key) and entered.wait(5)
    try:
        yield runner
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while runner.running and time.monotonic() < deadline:
            time.sleep(0.01)


def record_progress(c) -> list[str]:
    """Every progress text the tasks of the app report, in order (the task line shows the last one)."""
    runner, seen = c.app.state.skillpose.tasks, []
    report = runner.report
    runner.report = lambda text: seen.append(text) or report(text)
    return seen
