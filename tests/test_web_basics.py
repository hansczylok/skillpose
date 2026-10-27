import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime

import anthropic
import pytest
import uvicorn
from fakes import ScriptedClient, prompt_folder
from webkit import make_client

from skillpose.cli import main
from skillpose.core.llm import Usage
from skillpose.core.prompts import load_prompts
from skillpose.store.db import Store
from skillpose.web.deps import fmt_time, safe_filename
from skillpose.web.tasks import TaskRunner

DE = {"accept-language": "de"}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in ["ANTHROPIC_API_KEY", "APP_PASSWORD", "SKILLPOSE_ALLOWED_HOSTS", "MONTHLY_LIMIT_USD"]:
        monkeypatch.delenv(name, raising=False)


def test_start_without_key_offers_no_upload(tmp_path):
    r = make_client(tmp_path, key=False).get("/")
    assert r.status_code == 200 and "disabled" in r.text


def test_start_with_key_offers_the_upload_form(tmp_path):
    r = make_client(tmp_path).get("/")
    assert r.status_code == 200 and "disabled" not in r.text
    assert 'action="/cv"' in r.text and 'enctype="multipart/form-data"' in r.text and "data-busy=" in r.text
    assert 'accept=".pdf,.docx,.txt,.md"' in r.text


def test_start_lists_cvs_newest_first(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        first, second = st.save_cv("first.md", "x" * 300), st.save_cv("second.pdf", "y" * 300)
    text = make_client(tmp_path).get("/").text
    assert f'href="/cv/{first}"' in text and f'href="/cv/{second}"' in text
    assert text.index("second.pdf") < text.index("first.md")


def test_lang_switch(tmp_path):
    c = make_client(tmp_path)
    r = c.get("/lang/en?next=//evil.example", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and c.cookies.get("lang") == "en"
    cookie = r.headers["set-cookie"].lower()
    assert "path=/" in cookie and "samesite=lax" in cookie and "max-age=31536000" in cookie and "httponly" in cookie
    assert list(c.cookies.keys()) == ["lang"]   # the one cookie of the app
    r = c.get("/lang/de?next=/costs", follow_redirects=False)
    assert r.headers["location"] == "/costs" and c.cookies.get("lang") == "de"
    assert '<html lang="de">' in c.get("/").text
    assert '<html lang="en">' in c.get("/?lang=en").text
    assert c.get("/lang/fr", follow_redirects=False).status_code == 404


def test_language_from_the_browser(tmp_path):
    c = make_client(tmp_path)
    assert '<html lang="de">' in c.get("/", headers={"accept-language": "de-AT,de;q=0.9"}).text
    assert '<html lang="en">' in c.get("/", headers={"accept-language": "fr"}).text


def test_healthz(tmp_path):
    r = make_client(tmp_path).get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_unknown_page_shows_the_error_page(tmp_path):
    r = make_client(tmp_path).get("/no/such/page", headers=DE)
    assert r.status_code == 404 and "Diese Seite gibt es nicht." in r.text and '<html lang="de">' in r.text


def test_user_text_is_escaped(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        st.save_cv("<script>alert(1)</script>.md", "x" * 300)
    r = make_client(tmp_path).get("/")
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text


def test_header_shows_mode_and_month_cost(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        st.record("cv", "claude-opus-5-5", Usage(1000, 500, 0, 0), 0.12, False)
    c = make_client(tmp_path)
    de, en = c.get("/", headers=DE).text, c.get("/").text
    assert "0,12 / 5,00 USD" in de and "Eigene Daten" in de
    assert "0.12 / 5.00 USD" in en and "Own data" in en


def test_header_without_costs_shows_zero(tmp_path):
    assert "0.00 / 5.00 USD" in make_client(tmp_path).get("/").text  # without a key: no cost line (test_web_demo)


def test_task_line_shows_running_then_finished(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        st.save_cv("cv.md", "x" * 300)
    c = make_client(tmp_path)  # the page is the profile of a CV without a profile: no form, so it reloads
    runner = TaskRunner()
    c.app.state.skillpose.tasks = runner
    entered, release = threading.Event(), threading.Event()

    def slow() -> str:
        entered.set()
        release.wait(5)
        return "profile ready"

    assert runner.start("cv", "/cv/1", slow) and entered.wait(5)
    text = c.get("/cv/1", headers=DE).text
    assert '<meta http-equiv="refresh" content="3">' in text and "läuft seit" in text
    assert "Lebenslauf analysieren" in text and 'href="/cv/1"' in text
    release.set()
    deadline = time.monotonic() + 5
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.01)
    text = c.get("/cv/1", headers=DE).text
    assert 'http-equiv="refresh"' not in text and "fertig" in text and "profile ready" in text
    assert 'class="task ok"' in text


def test_failed_task_is_marked_as_error(tmp_path):
    c = make_client(tmp_path)

    def boom() -> str:
        raise RuntimeError("<b>monthly limit</b>")

    c.app.state.skillpose.tasks.start("postings", "/postings", boom)
    text = c.get("/").text
    assert 'class="task error"' in text and "&lt;b&gt;monthly limit&lt;/b&gt;" in text


def test_safe_filename():
    assert safe_filename("..\\..\\evil.md") == "evil.md" and safe_filename("a/b/c.pdf") == "c.pdf"
    assert safe_filename("x\x00y\n‮.md") == "xy.md" and safe_filename(None) == "upload"
    assert safe_filename("dir/") == "upload" and safe_filename("..") == "upload"
    assert len(safe_filename("a" * 300 + ".md")) == 120


def test_fmt_time_shows_local_time():
    assert fmt_time(None) == "" and fmt_time("not a time") == "not a time"
    utc = datetime(2026, 10, 21, 8, 31, tzinfo=UTC)
    local = utc.astimezone().strftime("%d.%m.%Y %H:%M")
    assert fmt_time("2026-10-21T08:31:00") == local and fmt_time(utc) == local  # the store writes UTC
    assert fmt_time(utc, "%H:%M") == local[-5:]


def test_web_command_refuses_open_bind_with_key(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert main(["web", "--host", "0.0.0.0"]) == 2 and "APP_PASSWORD" in capsys.readouterr().out


def test_web_command_without_the_extra(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # import uvicorn raises ImportError
    assert main(["web"]) == 2 and 'pip install "skillpose[web]"' in capsys.readouterr().out


def test_command_line_loads_no_web_library():
    code = "import sys, skillpose.cli; print(any(m in sys.modules for m in ('fastapi', 'starlette', 'uvicorn')))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False"


def test_web_command_starts_uvicorn(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setenv("SKILLPOSE_ALLOWED_HOSTS", " skillpose.example, ,other.example ")
    assert main(["web", "--port", "8123", "--db", str(tmp_path / "w.sqlite"), "--demo-dir", str(tmp_path)]) == 0
    [(app, kw)] = calls
    assert kw == {"host": "127.0.0.1", "port": 8123}
    config = app.state.skillpose.config
    assert config.client_factory is None and config.password is None
    assert config.db_path == tmp_path / "w.sqlite" and config.demo_dir == tmp_path and not config.inline_tasks
    assert config.allowed_hosts == ["127.0.0.1", "localhost", "skillpose.example", "other.example"]


def test_web_command_with_key_and_password_on_an_open_address(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=secret-key-value\nAPP_PASSWORD=secret-password\nMONTHLY_LIMIT_USD=2.5\n",
                   encoding="utf-8")
    for name in ["ANTHROPIC_API_KEY", "APP_PASSWORD", "MONTHLY_LIMIT_USD"]:
        monkeypatch.setenv(name, "")  # restored after the test; load_env_file overwrites them
    assert main(["web", "--host", "0.0.0.0", "--env-file", str(env), "--db", str(tmp_path / "w.sqlite")]) == 0
    [(app, kw)] = calls
    config = app.state.skillpose.config
    assert kw["host"] == "0.0.0.0" and config.client_factory is anthropic.Anthropic
    assert config.password == "secret-password" and config.settings.monthly_limit_usd == 2.5
    out = capsys.readouterr().out
    assert "secret-key-value" not in out and "secret-password" not in out


@pytest.mark.parametrize("hosts", ["*", "skillpose.example, *", "*.example.org"])
@pytest.mark.parametrize("key", [True, False])
def test_web_command_refuses_wildcard_hosts_without_a_password(monkeypatch, tmp_path, capsys, hosts, key):
    # Starlette's host check accepts any host for "*" (and a pattern for "*.x"): DNS rebinding would be open again
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setenv("SKILLPOSE_ALLOWED_HOSTS", hosts)
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert main(["web", "--db", str(tmp_path / "w.sqlite")]) == 2 and calls == []
    out = capsys.readouterr().out
    assert "SKILLPOSE_ALLOWED_HOSTS" in out and "APP_PASSWORD" in out and "*" in out


def test_web_command_accepts_a_wildcard_host_with_a_password(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setenv("SKILLPOSE_ALLOWED_HOSTS", "*")
    monkeypatch.setenv("APP_PASSWORD", "secret-password")
    assert main(["web", "--db", str(tmp_path / "w.sqlite")]) == 0
    assert "*" in calls[0][0].state.skillpose.config.allowed_hosts


def test_web_command_allows_the_bind_address_as_host(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    assert main(["web", "--host", "::1", "--db", str(tmp_path / "w.sqlite")]) == 0
    assert "[::1]" in calls[0][0].state.skillpose.config.allowed_hosts


@pytest.mark.parametrize("through", ["flag", "environment"])
def test_web_command_refuses_to_start_with_a_prompt_folder_that_cannot_be_loaded(monkeypatch, tmp_path, capsys,
                                                                                 through):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setenv("SKILLPOSE_PROMPTS", str(tmp_path / "gone") if through == "environment" else "")
    extra = ["--prompts", str(tmp_path / "gone")] if through == "flag" else []
    assert main(["web", *extra, "--db", str(tmp_path / "w.sqlite")]) == 2 and calls == []
    assert capsys.readouterr().out == "prompt folder gone: missing cv.md\n"


def test_web_command_prints_the_prompt_set_before_it_starts(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(capsys.readouterr().out))
    monkeypatch.delenv("SKILLPOSE_PROMPTS", raising=False)
    assert main(["web", "--db", str(tmp_path / "w.sqlite")]) == 0
    folder = prompt_folder(tmp_path / "mine")
    assert main(["web", "--prompts", str(folder), "--db", str(tmp_path / "w.sqlite")]) == 0
    assert calls == ["prompt set: example\n", f"prompt set: {load_prompts(folder).label}\n"]


def test_scripted_client_is_the_default_fake(tmp_path):
    factory = make_client(tmp_path).app.state.skillpose.config.client_factory
    assert isinstance(factory(), ScriptedClient)


def test_the_task_line_shows_the_progress_while_the_task_runs(tmp_path):
    c = make_client(tmp_path)
    runner = c.app.state.skillpose.tasks = TaskRunner()
    entered, release = threading.Event(), threading.Event()

    def slow() -> str:
        runner.report("Abgleich mit Lebenslauf: 3 von 86 geprüft")
        entered.set()
        release.wait(5)
        return "Batch abgeholt"

    assert runner.start("collect", "/postings", slow) and entered.wait(5)
    started = fmt_time(runner.current.started, "%H:%M")
    line = " ".join(re.sub(r"<[^>]+>", " ", re.search(r'<p class="task.*?</p>', c.get("/costs", headers=DE).text,
                                                         re.S).group(0)).split())
    assert line == f"läuft seit {started}: Batch abholen und abgleichen : Abgleich mit Lebenslauf: 3 von 86 geprüft"
    release.set()
    deadline = time.monotonic() + 5
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.01)
    page = c.get("/costs").text
    assert "Collect and match batch" in page and "3 of 86" not in page and "geprüft" not in page


def test_waiting_tasks_are_dropped_when_the_app_shuts_down(tmp_path):
    # after Ctrl-C nothing that waits may start: it would be paid for after the user stopped the app
    runner, entered, release, ran = TaskRunner(), threading.Event(), threading.Event(), []

    def slow() -> str:
        entered.set()
        release.wait(5)
        return "done"

    with make_client(tmp_path) as c:  # the context runs the lifespan of the app
        c.app.state.skillpose.tasks = runner
        runner.start("postings", "/postings", slow)
        assert entered.wait(5)
        runner.start("cv", "/cv/1", lambda: ran.append(1) or "")
    release.set()
    assert runner.join(5) and ran == [] and runner.waiting == []
