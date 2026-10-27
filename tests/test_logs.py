"""The log file (a rolling log): what skillpose did, never what it
analyzed."""

import json
import logging
import os
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
import uvicorn
import yaml
from fakes import CV, FakeBatchClient, ScriptedClient, req
from pydantic import BaseModel, ValidationError
from pypdf.errors import PdfReadError
from webkit import make_client

from skillpose import logs
from skillpose.cli import main
from skillpose.core.models import JobDraft, text_hash
from skillpose.core.prompts import active_prompts
from skillpose.store.db import Store

CV_MARKER = "CV-MARKER-4711-Mustermann"
JOB_MARKER = "JOB-MARKER-0815-Geheimprojekt"
QUOTE = "Built a Selenium test automation framework in Java"  # the quote of fakes.cv_draft()

log = logging.getLogger("skillpose.test")


@pytest.fixture
def log_on(monkeypatch):
    monkeypatch.delenv("SKILLPOSE_LOG", raising=False)  # conftest switches the log off for every other test


def test_the_log_goes_next_to_the_database_unless_a_path_or_off_is_given(tmp_path):
    db = tmp_path / "data" / "skillpose.sqlite"
    assert logs.log_path(None, db) == tmp_path / "data" / "skillpose.log"
    assert logs.log_path("", db) == tmp_path / "data" / "skillpose.log"
    assert logs.log_path(str(tmp_path / "x.log"), db) == tmp_path / "x.log"
    assert logs.log_path("off", db) is None and logs.log_path(" OFF ", db) is None
    assert logs.log_path(None, Path("/data/skillpose.sqlite")) == Path("/data/skillpose.log")  # Docker


def test_the_file_rotates_at_one_megabyte_and_keeps_three_old_files(tmp_path):
    with logs.file_log(tmp_path / "skillpose.log") as handler:
        assert isinstance(handler, RotatingFileHandler)
        assert (handler.maxBytes, handler.backupCount, handler.encoding) == (1024 * 1024, 3, "utf-8")
        line = "x" * 1000
        for _ in range(5 * 1100):  # about 5.5 MB
            log.info(line)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["skillpose.log", "skillpose.log.1", "skillpose.log.2", "skillpose.log.3"]
    assert all(p.stat().st_size <= 1024 * 1024 for p in tmp_path.iterdir())


def test_info_of_skillpose_goes_to_the_file_and_the_console_stays_at_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(logging.root, "handlers", [])  # as in a plain command line: no handler yet
    monkeypatch.setattr(logging.root, "level", logging.WARNING)
    logs.console()
    try:
        with logs.file_log(tmp_path / "skillpose.log"):
            log.info("task cv for /cv/1 started")
            log.warning("a warning")
            logging.getLogger("httpx").info("HTTP Request: POST https://api.anthropic.com/v1/messages")
    finally:
        for handler in logging.root.handlers:
            handler.close()
    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    first = text.splitlines()[0]
    assert first.endswith(" INFO skillpose.test: task cv for /cv/1 started") and first[:4].isdigit()
    assert "WARNING skillpose.test: a warning" in text and "HTTP Request" not in text
    err = capsys.readouterr().err
    assert "a warning" in err and "started" not in err
    assert logging.getLogger("skillpose").level == logging.NOTSET  # put back after the command


class Strict(BaseModel):
    quote: int


def test_a_validation_error_is_logged_without_the_input_it_rejected(tmp_path):
    with logs.file_log(tmp_path / "skillpose.log"):
        try:
            Strict.model_validate({"quote": QUOTE})
        except ValidationError as e:
            log.error("task cv for /cv/1 failed: %s", logs.safe_error(e), exc_info=True)
    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    assert QUOTE not in text and "Strict" in text and "quote: int_parsing" in text and "Traceback" in text


def test_the_home_and_the_temporary_folder_are_not_in_the_log(tmp_path):
    upload = Path(tempfile.gettempdir()) / "skillpose-upload-x1" / "cv.docx"
    with logs.file_log(tmp_path / "skillpose.log"):
        log.warning("could not delete %s", upload.parent)
        try:
            raise FileNotFoundError(f"Package not found at '{upload}'")
        except FileNotFoundError:
            log.warning("an uploaded CV could not be read", exc_info=True)
        log.info("home %s", Path.home() / "jobs" / "a.md")
    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    assert tempfile.gettempdir() not in text and str(Path.home()) not in text
    assert "<tmp>" in text and "skillpose-upload-x1" in text and "~" in text


def folder_forms():
    """The home and the temporary folder as a log line may write them: as they are, with forward slashes, and with
    every backslash doubled (the text of an OSError writes its path with repr())."""
    for folder in (Path.home(), Path(tempfile.gettempdir())):
        yield from (str(folder), folder.as_posix(), str(folder).replace("\\", "\\\\"))


def test_a_real_os_error_names_neither_the_folders_nor_the_file(tmp_path):
    # the text of an OSError (open, stat, rmtree) writes its path with repr(), so on Windows the
    # folder guard saw C:\\Users\\<name>\\... and let the user name, the upload folder and a CV file name through
    upload = Path(tempfile.gettempdir()) / "skillpose-upload-x1" / "cv.docx"
    cv = Path.home() / "Bewerbung" / "Lebenslauf_Max_Mustermann.pdf"
    with logs.file_log(tmp_path / "skillpose.log"):
        try:
            open(upload, "rb")  # noqa: SIM115 - it fails
        except FileNotFoundError:
            log.warning("an uploaded CV could not be read", exc_info=True)
        try:
            raise PermissionError(13, "the file is in use", str(upload))  # what rmtree meets while a scanner holds it
        except PermissionError as e:
            log.error("task cv for /cv/1 failed: %s", logs.safe_error(e), exc_info=True)
        try:
            raise PermissionError(13, "the folder is in use", str(upload.parent))
        except PermissionError:  # deps.upload_folder: the line must still say which folder to delete by hand
            log.warning("could not delete the temporary folder %s with an uploaded file: delete it by hand",
                        upload.parent, exc_info=True)
        try:
            cv.stat()  # analyze --cv with a file that does not exist
        except FileNotFoundError:
            log.exception("analyze failed after 0.0 s")
        if os.name == "nt":  # Windows: the drive and the folders may come in another case
            log.info("the folder %s", str(upload.parent).lower())
    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    seen = text.casefold() if os.name == "nt" else text
    for form in folder_forms():
        assert (form.casefold() if os.name == "nt" else form) not in seen, form
    assert "Lebenslauf_Max_Mustermann" not in text and "cv.docx" not in text and "Bewerbung" not in text
    assert "No such file or directory: '<file>.docx'" in text and "the file is in use: '<file>.docx'" in text
    assert "'<file>.pdf'" in text and "Traceback" in text
    assert f"the temporary folder <tmp>{os.sep}skillpose-upload-x1 with" in text
    assert "the folder is in use: '<file>'" in text


def test_analyze_with_a_missing_cv_file_logs_the_error_without_its_name(tmp_path, log_on):
    missing = tmp_path / "Lebenslauf_Max_Mustermann.pdf"
    with pytest.raises(FileNotFoundError):
        main(["analyze", "--cv", str(missing), "--jobs", str(tmp_path / "jobs"), "--db", str(tmp_path / "s.sqlite"),
              "--yes"], client_factory=ScriptedClient)
    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    assert "analyze failed after" in text and text.rstrip().endswith(" s: FileNotFoundError")  # the type, no text
    assert "Lebenslauf_Max_Mustermann" not in text and "<file>" not in text and str(tmp_path) not in text


def marker_files(tmp_path, n):
    cv = tmp_path / "lebenslauf_max_mustermann.md"
    cv.write_text(f"{CV} {CV_MARKER}", encoding="utf-8")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    texts = []
    for i in range(n):
        texts.append(f"Test Automation Engineer {i}\nSelenium and Java required. {JOB_MARKER}")
        (jobs / f"job_{i:03d}.md").write_text(texts[-1], encoding="utf-8")
    return cv, jobs, texts


def assert_no_personal_data(text):
    for secret in (CV_MARKER, JOB_MARKER, QUOTE, "Maria Berger", "lebenslauf_max_mustermann", "Selenium and Java",
                   active_prompts().cv[:40], active_prompts().job[:40], active_prompts().relation[:40]):
        assert secret not in text, secret


def test_a_full_run_logs_calls_and_batches_but_no_cv_or_posting_text(tmp_path, log_on):
    cv, jobs, texts = marker_files(tmp_path, 20)
    draft = JobDraft(title="From batch", role_family="test_automation", seniority="mid",
                     requirements=[req("java"), req("playwright")])
    client = ScriptedClient(extra_requirement="playwright")
    batches = FakeBatchClient(results={text_hash(t.strip()): ("succeeded", draft.model_dump_json()) for t in texts},
                              statuses=["ended"] * 3)
    client.messages.batches = batches.messages.batches
    db = tmp_path / "data" / "s.sqlite"
    rc = main(["analyze", "--cv", str(cv), "--jobs", str(jobs), "--db", str(db), "--yes"],
              client_factory=lambda: client)
    assert rc == 0 and Store(db).open_postings() == []
    text = (tmp_path / "data" / "skillpose.log").read_text(encoding="utf-8")
    assert "INFO skillpose.cli: analyze started" in text and "analyze ended with code 0" in text
    assert "api call cv: claude-opus-5-5, input 1000, output 500, cache write 0, cache read 0" in text
    assert "api call job: " in text and ", single" in text and "api call relation: " in text
    assert text.count("api call job: claude-haiku-5-5") == 19
    assert text.count("USD, 754.0 s, batch batch_") == 19  # each batch result: the time its batch took
    assert "batch batch_1 submitted with 7 postings" in text and "batch batch_3 collected: 6 of 6" in text
    assert_no_personal_data(text)


def damaged_pdf() -> bytes:
    """A PDF whose page object holds no PDF object, only the marker: pypdf writes about 80 bytes of it in its error."""
    return (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [3 0 R]"
            b" /Count 1 >>\nendobj\n3 0 obj\n" + CV_MARKER.encode() + b"\nendobj\ntrailer\n<< /Root 1 0 R >>\n"
            b"startxref\n0\n%%EOF\n")


def test_an_upload_that_cannot_be_read_is_logged_without_its_content_or_name(tmp_path):
    # pypdf quotes about 80 bytes of a damaged PDF in its error, and the traceback
    # of the warning carried them into the log; the line now names the type of the error only
    pdf = damaged_pdf()
    posting = json.dumps({f"Test Automation Engineer {JOB_MARKER}": "a dict, not a list"}).encode()
    c = make_client(tmp_path)
    with logs.file_log(tmp_path / "skillpose.log"):
        cv = c.post("/cv", files={"file": ("Lebenslauf_Max_Mustermann.pdf", pdf)})
        jobs = c.post("/postings/upload", files=[("files", ("Inserate_Geheimprojekt.json", posting))])
    assert cv.status_code == 400 and "could not be read" in cv.text
    assert jobs.status_code == 400 and "unexpected structure" in jobs.text
    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    assert "WARNING skillpose.web.pages_cv: an uploaded CV could not be read: PdfReadError" in text
    assert "WARNING skillpose.web.pages_jobs: an uploaded posting file could not be read: AttributeError" in text
    assert "Traceback" not in text
    for secret in (CV_MARKER, JOB_MARKER, "Lebenslauf_Max_Mustermann", "Inserate_Geheimprojekt", "cv.pdf",
                   "posting.json", "Test Automation Engineer"):
        assert secret not in text, secret


def test_analyze_with_a_damaged_cv_logs_only_the_kind_of_error(tmp_path, log_on):
    # the line of a failed command carried the traceback, and the error of pypdf quotes the file
    cv = tmp_path / "Lebenslauf_Max_Mustermann.pdf"
    cv.write_bytes(damaged_pdf())
    with pytest.raises(PdfReadError):  # the command still stops with the error on the console, as before
        main(["analyze", "--cv", str(cv), "--jobs", str(tmp_path / "jobs"), "--db", str(tmp_path / "s.sqlite"),
              "--yes"], client_factory=ScriptedClient)
    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    assert "ERROR skillpose.cli: analyze failed after " in text and text.rstrip().endswith(" s: PdfReadError")
    assert "Traceback" not in text
    for secret in (CV_MARKER, "Lebenslauf_Max_Mustermann", "cv.pdf", str(tmp_path)):
        assert secret not in text, secret


@pytest.mark.parametrize("command", [["eval"], ["demo", "build"]])
def test_eval_and_demo_build_with_a_broken_gold_set_log_only_the_kind_of_error(tmp_path, log_on, command):
    # a YAML error quotes the line it stops at; expected.yaml lies next to the CVs and postings of the gold set
    goldset = tmp_path / "goldset"
    goldset.mkdir()
    (goldset / "expected.yaml").write_text(f"cvs:\n  person: [{CV_MARKER}\n    language: de\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError) as raised:
        main([*command, "--goldset", str(goldset), "--db", str(tmp_path / "g.sqlite"), "--yes"],
             client_factory=ScriptedClient)
    assert CV_MARKER in str(raised.value)  # the error does quote the file, so the log line must not carry it
    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    assert f"ERROR skillpose.cli: {' '.join(command)} failed after " in text
    assert text.rstrip().endswith(f" s: {type(raised.value).__name__}")
    assert "Traceback" not in text
    for secret in (CV_MARKER, "expected.yaml", str(tmp_path)):
        assert secret not in text, secret


def test_the_log_can_be_switched_off_or_moved(tmp_path, log_on, monkeypatch):
    cv, jobs, _ = marker_files(tmp_path, 1)
    base = ["analyze", "--cv", str(cv), "--jobs", str(jobs), "--yes"]
    assert main([*base, "--db", str(tmp_path / "a" / "s.sqlite"), "--log-file", "off"],
                client_factory=ScriptedClient) == 0
    monkeypatch.setenv("SKILLPOSE_LOG", "off")
    assert main([*base, "--db", str(tmp_path / "b" / "s.sqlite")], client_factory=ScriptedClient) == 0
    assert not list(tmp_path.rglob("*.log"))
    monkeypatch.setenv("SKILLPOSE_LOG", str(tmp_path / "logs" / "env.log"))
    assert main([*base, "--db", str(tmp_path / "c" / "s.sqlite")], client_factory=ScriptedClient) == 0
    assert main([*base, "--db", str(tmp_path / "d" / "s.sqlite"), "--log-file", str(tmp_path / "flag.log")],
                client_factory=ScriptedClient) == 0  # the flag wins over the environment
    assert sorted(p.name for p in tmp_path.rglob("*.log")) == ["env.log", "flag.log"]


def test_the_web_command_logs_next_to_its_database_without_the_password(tmp_path, log_on, monkeypatch):
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: logging.getLogger("skillpose.web").info("served"))
    monkeypatch.setenv("APP_PASSWORD", "pw-MARKER-31337")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["web", "--db", str(tmp_path / "w.sqlite"), "--demo-dir", str(tmp_path)]) == 0
    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    assert "web started" in text and "INFO skillpose.web: served" in text and "web ended with code 0" in text
    assert "pw-MARKER-31337" not in text


@pytest.mark.parametrize("command, name", [(["eval", "--step", "job"], "eval"), (["demo", "build"], "demo build")])
def test_eval_and_demo_build_log_next_to_their_database(tmp_path, log_on, command, name):
    rc = main([*command, "--goldset", str(tmp_path / "missing"), "--db", str(tmp_path / "g.sqlite")])
    assert rc == 2  # the gold set is missing: nothing is paid, but the command is in the log
    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    assert f"{name} started" in text and f"{name} ended with code 2" in text
