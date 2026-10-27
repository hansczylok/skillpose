"""Web pages for the CV: upload, text check, analysis and profile."""

import logging
import re
import shutil
import time
from pathlib import Path

import pytest
from conftest import make_docx, make_pdf
from demo_fixture import NAME, write_fixture_bundles
from fakes import CV, FakeClient, ScriptedClient, answer, refusal
from webkit import make_client, record_progress, task_running

from skillpose.core.config import load_settings
from skillpose.core.llm import Usage
from skillpose.core.models import CandidateProfile, CandidateSkill, Evidence
from skillpose.pipeline import analyze_postings, estimate, ingest_postings, open_run, plan_postings, rematch
from skillpose.store.db import Store
from skillpose.web.tasks import Start, TaskRunner

DE = {"accept-language": "de"}
ASK_ALWAYS = load_settings().model_copy(update={"confirm_above_usd": 0.0001})
REFRESH = '<meta http-equiv="refresh"'  # base.html reloads a page every 3 s while a task runs
QUEUED = "Queued – starts when the running task has finished."
QUEUED_DE = "Eingereiht – startet, wenn die laufende Aufgabe fertig ist."


def upload(c, name, data):
    return c.post("/cv", files={"file": (name, data)}, follow_redirects=False)


def test_an_upload_folder_that_cannot_be_deleted_is_logged_and_the_upload_works(tmp_path, monkeypatch, caplog):
    # the bytes of an upload go to a temporary folder; if it stays (a scanner holds the file), the log must say where
    real, left = shutil.rmtree, []

    def stuck(path, *args, **kwargs):
        left.append(Path(path))
        raise PermissionError("the file is in use")

    c = make_client(tmp_path)
    monkeypatch.setattr(shutil, "rmtree", stuck)
    with caplog.at_level(logging.WARNING):
        cv = upload(c, "cv.md", CV.encode())
        posting = c.post("/postings/upload", files=[("files", ("job.md", b"Test Automation Engineer\nSelenium."))],
                         follow_redirects=False)
    monkeypatch.undo()
    for folder in left:
        real(folder, ignore_errors=True)
    assert cv.status_code == 303 and posting.status_code == 303 and len(left) == 2
    assert caplog.text.count("could not delete the temporary folder") == 2
    assert all(str(folder) in caplog.text for folder in left)


def analyzed_client(tmp_path, headers=None):
    """A client with CV 1 uploaded and analyzed by a scripted API client; returns (client, api client)."""
    api = ScriptedClient()
    c = make_client(tmp_path, client=api)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    r = c.post(loc.replace("/text", "/analyze"), data={"confirm": "1"}, headers=headers, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cv/1"
    return c, api


def analyze_postings_for_cv_1(tmp_path, api, n=2):
    with Store(tmp_path / "web.sqlite") as store:
        run = open_run(load_settings(), store, "en", client=api)
        items = [(f"job_{i}.md", f"Test Automation Engineer {i}\nSelenium and Java required.") for i in range(n)]
        hashes, _ = ingest_postings(run, items)
        analyze_postings(run, plan_postings(run, hashes, no_batch=True))
        rematch(run, 1, hashes)


def statuses(tmp_path, skill_id):
    with Store(tmp_path / "web.sqlite") as store:
        return [m.status for jm in store.matches(1) for m in jm.matches if m.requirement.skill_id == skill_id]


def skill_block(page, skill_id):
    """The HTML of one skill of the profile page, from its id to the next skill or the end of its category."""
    return re.split(r'<li class="skill|</section>', page[page.index(f'id="skill-{skill_id}"'):])[0]


def own_profile(tmp_path, quote_flags=(True, True, False)):
    """CV 1 with a profile of known evidence: outcome, activity and claim quotes; the flags tell which are verified."""
    strengths = ["outcome", "activity", "claim"]
    evidence = [Evidence(quote=f"Quote {s} <b>x</b>", context="Acme", period_from=2016, period_to=None, strength=s,
                         verified=v) for s, v in zip(strengths, quote_flags, strict=True)]
    skills = [CandidateSkill(skill_id="test_automation", level="strong", years=10, last_used=2026,
                             evidence=evidence),
              CandidateSkill(skill_id="java", level="working", years=2.5, last_used=2019,
                             evidence=[evidence[0].model_copy(update={"period_to": 2019, "quote": "Java quote"})]),
              CandidateSkill(skill_id="zz_unknown_skill", level="basic", years=None, evidence=[])]
    with Store(tmp_path / "web.sqlite") as store:
        cv_id = store.save_cv("own.md", CV)
        store.save_profile(cv_id, CandidateProfile(language="en", skills=skills, stations=[]), "fake")
    return cv_id


# --- upload ---

def test_upload_shows_the_text_for_correction(tmp_path):
    c = make_client(tmp_path)
    r = upload(c, "cv.md", CV.encode())
    assert r.status_code == 303 and "Selenium test automation framework" in c.get(r.headers["location"]).text


def test_upload_rejects_type_size_and_scanned_text(tmp_path):
    c = make_client(tmp_path)
    assert "unsupported" in upload(c, "cv.odt", b"x" * 300).text
    assert "5 MB" in upload(c, "cv.txt", b"a" * (5 * 1024 * 1024 + 1)).text
    assert "no readable text" in upload(c, "cv.txt", b"short").text


def test_upload_errors_show_next_to_the_form_with_status_400(tmp_path):
    c = make_client(tmp_path)
    r = upload(c, "cv.odt", b"x" * 300)
    assert r.status_code == 400 and r.text.index('action="/cv"') < r.text.index('class="msg error"')
    assert "unsupported file type .odt" in r.text
    assert upload(c, "noextension", b"x" * 300).status_code == 400 and "(none)" in upload(c, "x", b"y").text
    assert "no readable text" in upload(c, "scan.pdf", make_pdf(tmp_path, "").read_bytes()).text
    assert "could not be read" in upload(c, "broken.pdf", b"%PDF-1.4 this is not a pdf").text
    with Store(tmp_path / "web.sqlite") as st:
        assert st.cvs() == []


def test_upload_errors_in_german(tmp_path):
    c = make_client(tmp_path)
    assert "Dateityp .odt" in c.post("/cv", files={"file": ("cv.odt", b"x" * 300)}, headers=DE).text
    assert "größer als 5 MB" in c.post("/cv", files={"file": ("cv.txt", b"a" * (5 * 1024 * 1024 + 1))},
                                       headers=DE).text
    assert "Kein lesbarer Text" in c.post("/cv", files={"file": ("cv.txt", b"short")}, headers=DE).text


def test_upload_without_a_file_says_so(tmp_path):
    r = make_client(tmp_path).post("/cv", files={"file": ("", b"")})
    assert r.status_code == 400 and "Choose a file first" in r.text


def test_upload_of_docx_and_pdf(tmp_path):
    c = make_client(tmp_path)
    word = make_docx(tmp_path, para="Senior QA Engineer at Acme " * 10, cell="Selenium").read_bytes()
    docx = upload(c, "cv.DOCX", word)
    pdf = upload(c, "cv.pdf", make_pdf(tmp_path, "Testautomatisierung mit Selenium " * 10).read_bytes())
    assert docx.status_code == 303 and pdf.status_code == 303
    assert "Senior QA Engineer at Acme" in c.get(docx.headers["location"]).text
    assert "Testautomatisierung mit Selenium" in c.get(pdf.headers["location"]).text


def test_upload_file_name_is_sanitized(tmp_path):
    upload(make_client(tmp_path), "..\\..\\evil.md", CV.encode())
    with Store(tmp_path / "web.sqlite") as st:
        assert st.cvs()[0].name == "evil.md"


def test_upload_never_writes_under_the_given_name(tmp_path):
    # a device name such as CON.md would not be a file on Windows; the bytes go to a fixed name in a temporary folder
    c = make_client(tmp_path)
    assert upload(c, "CON.md", CV.encode()).status_code == 303
    assert upload(c, "nul.txt", (CV + " Other.").encode()).status_code == 303
    with Store(tmp_path / "web.sqlite") as st:
        assert [cv.name for cv in st.cvs()] == ["nul.txt", "CON.md"]


KNOWN = "This CV is already here."


def test_the_same_file_uploaded_twice_opens_the_stored_cv(tmp_path):
    c = make_client(tmp_path)
    assert upload(c, "cv.md", CV.encode()).headers["location"] == "/cv/1/text"
    again = upload(c, "cv.md", CV.encode())
    assert again.status_code == 303 and again.headers["location"] == "/cv/1/text?msg=cv_known"
    page = c.get(again.headers["location"]).text
    assert KNOWN in page and page.index(KNOWN) < page.index('action="/cv/1/text"')
    assert "Dieser Lebenslauf ist schon vorhanden." in c.get(again.headers["location"], headers=DE).text
    assert KNOWN not in c.get("/cv/1/text").text and KNOWN not in c.get("/?msg=cv_known").text
    with Store(tmp_path / "web.sqlite") as st:
        assert [cv.id for cv in st.cvs()] == [1]


def test_the_same_text_in_another_file_type_opens_the_stored_cv(tmp_path):
    c = make_client(tmp_path)
    upload(c, "cv.txt", CV.encode())
    assert upload(c, "lebenslauf.md", CV.encode()).headers["location"] == "/cv/1/text?msg=cv_known"
    with Store(tmp_path / "web.sqlite") as st:
        assert [cv.name for cv in st.cvs()] == ["cv.txt"]


def test_a_text_that_differs_by_one_character_is_a_new_cv(tmp_path):
    c = make_client(tmp_path)
    upload(c, "cv.md", CV.encode())
    assert upload(c, "cv.md", (CV + ".").encode()).headers["location"] == "/cv/2/text"
    with Store(tmp_path / "web.sqlite") as st:
        assert len(st.cvs()) == 2


def test_an_analyzed_cv_uploaded_again_opens_its_profile_without_a_call(tmp_path):
    c, api = analyzed_client(tmp_path)
    calls = len(api.calls)
    with Store(tmp_path / "web.sqlite") as st:
        spent = st.month_total_usd()
    again = upload(c, "cv.md", CV.encode())
    assert again.status_code == 303 and again.headers["location"] == "/cv/1?msg=cv_known"
    page = c.get(again.headers["location"]).text
    assert KNOWN in page and page.index(KNOWN) < page.index('class="ai-output"')
    with Store(tmp_path / "web.sqlite") as st:
        assert len(st.cvs()) == 1 and st.month_total_usd() == spent
    assert len(api.calls) == calls and c.app.state.skillpose.tasks.current.name == "cv"


# --- text page ---

def test_text_page_has_the_text_the_size_and_the_cost_on_the_button(tmp_path):
    c = make_client(tmp_path)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    page = c.get(loc).text
    assert f"{len(CV)} characters" in page and "Analyze (about " in page and " cents)" in page
    assert re.search(r"Analyze \(about \d+ cents\)", page) and 'action="/cv/1/analyze"' in page
    assert "<textarea" in page and "readonly" not in page and "uploaded " in page
    de = c.get(loc, headers=DE).text
    assert f"{len(CV)} Zeichen" in de and re.search(r"Analysieren \(ca\. \d+ Cent\)", de)


def test_text_page_has_the_cv_navigation_and_busy_forms(tmp_path):
    c = make_client(tmp_path)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    page = c.get(loc).text
    for link in ["/cv/1", "/cv/1/matches", "/cv/1/market", "/cv/1/paths"]:
        assert f'href="{link}"' in page
    assert page.count("data-busy=") >= 2


def test_saved_text_is_kept_and_confirmed_next_to_the_form(tmp_path):
    c = make_client(tmp_path)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    r = c.post(loc, data={"text": CV + "\r\nNew line."}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cv/1/text?msg=saved"
    page = c.get(r.headers["location"]).text
    assert "Saved." in page and page.index("<textarea") < page.index("Saved.") < page.index("/cv/1/analyze")
    with Store(tmp_path / "web.sqlite") as st:
        assert st.cv_text(1) == CV + "\nNew line."
    assert "Saved." not in c.get(loc).text


def test_message_in_the_query_is_never_echoed(tmp_path):
    c = make_client(tmp_path)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    page = c.get(loc + "?msg=<script>alert(1)</script>").text
    assert "<script>alert(1)" not in page and "alert(1)" not in page


def test_text_that_is_too_short_is_refused_and_not_lost(tmp_path):
    c = make_client(tmp_path)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    r = c.post(loc, data={"text": "tiny <b>edit</b>"})
    assert r.status_code == 400 and "too short" in r.text and "tiny &lt;b&gt;edit&lt;/b&gt;" in r.text
    with Store(tmp_path / "web.sqlite") as st:
        assert st.cv_text(1) == CV


def test_text_is_fixed_once_the_profile_exists(tmp_path):
    c, _ = analyzed_client(tmp_path)
    page = c.get("/cv/1/text").text
    assert "readonly" in page and 'action="/cv/1/analyze"' not in page and 'href="/cv/1"' in page
    assert 'action="/cv/1/text"' not in page
    r = c.post("/cv/1/text", data={"text": CV + " Changed text."})
    assert r.status_code == 400 and "can no longer change" in r.text
    with Store(tmp_path / "web.sqlite") as st:
        assert st.cv_text(1) == CV


def test_user_text_is_escaped_on_the_cv_pages(tmp_path):
    evil = CV + " </textarea><script>alert(1)</script>"
    c = make_client(tmp_path)
    r = upload(c, "<img src=x onerror=alert(1)>.md", evil.encode())
    text_page = c.get(r.headers["location"]).text
    assert "<script>alert(1)" not in text_page and "&lt;/textarea&gt;&lt;script&gt;" in text_page
    assert "<img src=x" not in text_page and "&lt;img src=x" in text_page
    own_profile(tmp_path)
    profile_page = c.get("/cv/2").text
    assert "<b>x</b>" not in profile_page and "Quote outcome &lt;b&gt;x&lt;/b&gt;" in profile_page


def test_unknown_cv_is_a_404_with_the_error_page(tmp_path):
    c = make_client(tmp_path)
    for path in ["/cv/99", "/cv/99/text"]:
        r = c.get(path, headers=DE)
        assert r.status_code == 404 and "Diese Seite gibt es nicht." in r.text, path
    for path in ["/cv/99/text", "/cv/99/analyze", "/cv/99/delete", "/cv/99/skill/java"]:
        assert c.post(path, data={"text": CV, "level": "basic"}).status_code == 404, path


def test_text_is_not_saved_while_the_cvs_own_task_runs(tmp_path):
    # the profile would be made from the older text, and the page would then say it was made from the new one
    c = make_client(tmp_path)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    with task_running(c, "cv", "/cv/1"):
        r = c.post(loc, data={"text": CV + " Changed while analyzing."}, follow_redirects=False)
        assert r.status_code == 400 and "is being analyzed" in r.text
        assert "Changed while analyzing." in r.text  # what the user typed stays in the field
        assert r.text.index("<textarea") < r.text.index("is being analyzed") < r.text.index("/cv/1/analyze")
        with Store(tmp_path / "web.sqlite") as st:
            assert st.cv_text(1) == CV
    de = c.post(loc, data={"text": CV + " Changed."}, headers=DE, follow_redirects=False)
    assert de.status_code == 303  # the task is over: saving works again (the profile does not exist yet)
    with task_running(c, "cv", "/cv/1"):
        assert "wird gerade analysiert" in c.post(loc, data={"text": CV + " Again."}, headers=DE).text


def test_text_is_saved_while_a_task_of_another_cv_runs(tmp_path):
    c = make_client(tmp_path)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    with task_running(c, "cv", "/cv/2"):
        assert c.post(loc, data={"text": CV + " Other CV."}, follow_redirects=False).status_code == 303
    with Store(tmp_path / "web.sqlite") as st:
        assert st.cv_text(1) == CV + " Other CV."


# --- no reload while a task runs ---

def test_pages_with_input_do_not_reload_while_a_task_runs(tmp_path):
    # the reload would throw away what the user typed or chose
    c = make_client(tmp_path)
    own_profile(tmp_path)  # CV 1: a profile, so skill forms
    upload(c, "cv.md", (CV + " Other.").encode())  # CV 2: no profile yet, so the text form
    with task_running(c, "cv", "/cv/2"):
        for path in ["/cv/2/text", "/", "/cv/1"]:  # textarea, upload form, skill forms
            page = c.get(path).text
            assert "running since" in page and REFRESH not in page, path


def test_read_only_pages_still_reload_while_a_task_runs(tmp_path):
    c = make_client(tmp_path)
    own_profile(tmp_path)
    upload(c, "cv.md", CV.encode())
    with task_running(c, "cv", "/cv/2"):
        assert f'{REFRESH} content="3">' in c.get("/cv/2").text  # the profile of the CV being analyzed: no forms yet
        assert REFRESH in c.get("/cv/99").text  # an error page
    assert REFRESH not in c.get("/cv/2").text  # nothing runs any more


def test_the_demo_profile_has_no_forms_and_reloads(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    with task_running(c, "cv", "/cv/1"):
        assert REFRESH in c.get("/cv/1").text


def test_answers_to_a_post_do_not_reload_while_a_task_runs(tmp_path):
    # the reload would be a GET of the address of the form: 405 instead of the page, or the typed text lost
    c = make_client(tmp_path, settings=ASK_ALWAYS)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    with task_running(c, "postings", "/postings"):
        confirm = c.post("/cv/1/analyze", data={})
        assert confirm.status_code == 200 and "Yes, spend about" in confirm.text
        refused = c.post(loc, data={"text": "tiny"})
        assert refused.status_code == 400 and "too short" in refused.text
        bad_upload = upload(c, "cv.odt", b"x" * 300)
        assert bad_upload.status_code == 400 and "unsupported" in bad_upload.text
        for page in [confirm, refused, bad_upload]:
            assert "running since" in page.text and REFRESH not in page.text


# --- analysis ---

def test_corrected_text_is_analyzed(tmp_path):
    client = ScriptedClient()
    c = make_client(tmp_path, client=client)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    c.post(loc, data={"text": CV + " Corrected line."})
    c.post(loc.replace("/text", "/analyze"), data={"confirm": "1"})
    cv_call = next(x for x in client.calls if x.path == "CvDraft")
    assert "Corrected line." in cv_call.kwargs["messages"][0]["content"]


def test_analysis_starts_the_cv_task_in_the_ui_language(tmp_path):
    client = ScriptedClient()
    c = make_client(tmp_path, client=client)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    r = c.post(loc.replace("/text", "/analyze"), headers=DE, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cv/1"
    task = c.app.state.skillpose.tasks.current
    assert task.name == "cv" and task.target == "/cv/1" and task.ok
    [cv_call] = [x for x in client.calls if x.path == "CvDraft"]
    assert "in German." in str(cv_call.kwargs["system"])


def test_analysis_above_threshold_asks_first(tmp_path):
    client = ScriptedClient()
    c = make_client(tmp_path, client=client, settings=ASK_ALWAYS)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    r = c.post(loc.replace("/text", "/analyze"), data={})
    assert r.status_code == 200 and client.calls == [] and c.app.state.skillpose.tasks.current is None
    assert re.search(r"Yes, spend about \d+\.\d\d USD", r.text) and 'name="confirm" value="1"' in r.text
    assert 'action="/cv/1/analyze"' in r.text and 'href="/cv/1/text"' in r.text
    de = c.post("/cv/1/analyze", data={}, headers=DE).text
    assert re.search(r"Ja, ca\. \d+,\d\d USD ausgeben", de)
    cost = re.search(r'name="cost" value="([^"]+)"', r.text)[1]  # the confirmation names the cost it confirms
    done = c.post("/cv/1/analyze", data={"confirm": "1", "cost": cost}, follow_redirects=False)
    assert done.status_code == 303 and [x.path for x in client.calls].count("CvDraft") == 1


def test_the_confirmation_holds_only_for_the_cost_asked(tmp_path):
    client = ScriptedClient()
    c = make_client(tmp_path, client=client, settings=ASK_ALWAYS)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    cost = re.search(r'name="cost" value="([^"]+)"', c.post("/cv/1/analyze", data={}).text)[1]
    c.post(loc, data={"text": CV + " More experience." * 1000})  # another tab: a longer text costs more
    again = c.post("/cv/1/analyze", data={"confirm": "1", "cost": cost})
    assert again.status_code == 200 and client.calls == [] and c.app.state.skillpose.tasks.current is None
    assert "The cost has changed" in again.text
    new_cost = re.search(r'name="cost" value="([^"]+)"', again.text)[1]
    assert float(new_cost) > float(cost)
    assert c.post("/cv/1/analyze", data={"confirm": "1"}).status_code == 200 and client.calls == []
    done = c.post("/cv/1/analyze", data={"confirm": "1", "cost": new_cost}, follow_redirects=False)
    assert done.status_code == 303 and [x.path for x in client.calls].count("CvDraft") == 1


def test_analysis_over_the_monthly_limit_is_refused_next_to_the_button(tmp_path):
    client = ScriptedClient()
    settings = load_settings().model_copy(update={"monthly_limit_usd": 0.01})
    c = make_client(tmp_path, client=client, settings=settings)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    r = c.post(loc.replace("/text", "/analyze"), data={"confirm": "1"})
    assert r.status_code == 400 and client.calls == [] and c.app.state.skillpose.tasks.current is None
    assert "Monthly limit" in r.text and "nothing was sent" in r.text.lower()
    assert r.text.index('action="/cv/1/analyze"') < r.text.index("Monthly limit")
    assert "Monatsgrenze" in c.post("/cv/1/analyze", data={}, headers=DE).text


def test_analysis_while_another_task_runs_waits_and_runs_after_it(tmp_path):
    client = ScriptedClient()
    c = make_client(tmp_path, client=client)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    with task_running(c, "postings", "/postings"):
        r = c.post(loc.replace("/text", "/analyze"), data={"confirm": "1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/cv/1?msg=queued"
        page = c.get(r.headers["location"]).text
        assert QUEUED in page and "Waiting: Analyze CV" in page
        assert client.calls == []
        assert f'{REFRESH} content="3">' in page  # the page reloads until the profile is there
        de = c.get(r.headers["location"], headers=DE).text
        assert QUEUED_DE in de and "Wartet: Lebenslauf analysieren" in de
    task = c.app.state.skillpose.tasks.current
    assert task.name == "cv" and task.ok and [x.path for x in client.calls] == ["CvDraft"]
    page = c.get("/cv/1?msg=queued").text
    assert "Queued –" not in page and "Waiting:" not in page and "Selenium" in page


def test_a_waiting_analysis_that_no_longer_fits_the_monthly_limit_sends_nothing(tmp_path):
    # the click found room for the estimate; while the analysis waited, the task before it spent most of the rest.
    # The check runs again when the analysis starts, so the month cannot end above the limit by one call
    client = ScriptedClient()
    settings = load_settings().model_copy(update={"monthly_limit_usd": 1.0})
    c = make_client(tmp_path, client=client, settings=settings)
    upload(c, "cv.md", CV.encode())
    with Store(tmp_path / "web.sqlite") as st:
        cost = estimate(open_run(settings, st, "en"), cv_text=st.cv_text(1))
    spent = 1.0 - cost / 2  # below the limit: the check of each call alone would let the call go out
    with task_running(c, "postings", "/postings", spend=spent):
        r = c.post("/cv/1/analyze", data={"confirm": "1"}, follow_redirects=False)
        assert r.headers["location"] == "/cv/1?msg=queued"
    task = c.app.state.skillpose.tasks.current
    assert (task.name, task.target, task.ok) == ("cv", "/cv/1", False) and client.calls == []
    assert task.message == (f"Monthly limit: {spent:.2f} of 1.00 USD spent, this analysis costs about {cost:.2f} "
                            "USD. Nothing was sent.")
    assert "Error: Monthly limit:" in c.get("/cv/1").text
    with Store(tmp_path / "web.sqlite") as st:
        assert st.month_total_usd() == pytest.approx(spent) and st.profile(1) is None


def test_analysis_is_refused_when_five_tasks_wait(tmp_path):
    client = ScriptedClient()
    c = make_client(tmp_path, client=client)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    with task_running(c, "postings", "/postings") as runner:
        for i in range(5):
            assert runner.start("cv", f"/cv/{i + 2}", lambda: "") is Start.QUEUED
        r = c.post(loc.replace("/text", "/analyze"), data={"confirm": "1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/cv/1/text?msg=busy"
        page = c.get(r.headers["location"]).text
        assert "Another task is running" in page and page.index('action="/cv/1/analyze"') < page.index("Another task")
    assert client.calls == []


def test_a_cv_whose_analysis_waits_can_neither_change_its_text_nor_be_deleted(tmp_path):
    c = make_client(tmp_path)
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    with task_running(c, "postings", "/postings"):
        c.post(loc.replace("/text", "/analyze"), data={"confirm": "1"})
        refused = c.post(loc, data={"text": CV + " Changed while waiting."})
        assert refused.status_code == 400 and "is being analyzed" in refused.text
        r = c.post("/cv/1/delete", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/cv/1?msg=delete_busy"
        assert "A task for this CV is running" in c.get(r.headers["location"]).text
    with Store(tmp_path / "web.sqlite") as st:
        assert st.cv_text(1) == CV and st.profile(1) is not None


def test_failed_analysis_shows_the_error_on_the_profile_page(tmp_path):
    c = make_client(tmp_path, client=FakeClient([refusal()] * 5))
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    c.post(loc.replace("/text", "/analyze"), data={"confirm": "1"})
    task = c.app.state.skillpose.tasks.current
    assert task.ok is False
    page = c.get("/cv/1").text
    assert "not been analyzed" in page and 'class="msg error"' in page and 'href="/cv/1/text"' in page


CV_STOPPED = {
    ("max_tokens", "de"): "Die Antwort war zu lang und wurde abgeschnitten (Grenze 32.000 Token). Kosten des Versuchs: "
                          "0,66 USD. Bitte erneut versuchen oder den Lebenslauf kürzen.",
    ("max_tokens", "en"): "The answer was too long and was cut off (limit 32,000 tokens). Cost of the attempt: "
                          "0.66 USD. Try again or shorten the CV.",
    ("refusal", "de"): "Claude (KI) hat die Analyse abgelehnt. Kosten des Versuchs: 0,0020 USD. Bitte den Text prüfen "
                       "und erneut versuchen.",
    ("refusal", "en"): "Claude (AI) refused the analysis. Cost of the attempt: 0.0020 USD. Check the text and try "
                       "again.",
}


@pytest.mark.parametrize("stop, lang", list(CV_STOPPED))
def test_an_analysis_that_stops_early_says_why_with_the_limit_and_the_cost(tmp_path, stop, lang):
    # a bare "Fehler: max_tokens" told nothing, and the attempt was paid (here 4,657 input tokens and the
    # 32,000 output tokens of the limit on Opus 5.5; a refusal: 500 input tokens)
    stopped = (answer('{"skills": [', stop_reason="max_tokens", usage=Usage(4657, 32000, 0, 0))
               if stop == "max_tokens" else refusal())
    c = make_client(tmp_path, client=FakeClient([stopped]))
    headers = {"accept-language": lang}
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    c.post(loc.replace("/text", "/analyze"), headers=headers)
    task = c.app.state.skillpose.tasks.current
    assert (task.name, task.ok, task.message) == ("cv", False, CV_STOPPED[stop, lang])
    assert CV_STOPPED[stop, lang] in c.get("/cv/1", headers=headers).text
    with Store(tmp_path / "web.sqlite") as st:
        assert st.profile(1) is None and st.cost_summary().calls == 1  # booked, as it was paid


def test_task_message_is_shown_on_the_profile(tmp_path):
    c, _ = analyzed_client(tmp_path, headers=DE)  # the task writes its message in the language of the request
    page = c.get("/cv/1?lang=de").text
    assert "Profil fertig: 2 Kompetenzen, 0 Inserate abgeglichen" in page
    assert 'class="msg ok"' in page and page.index('class="msg ok"') < page.index("Selenium")
    assert 'class="task ok"' in page and page.count("Profil fertig") == 2  # next to the content and in the header
    other = c.get("/cv/1/text?lang=de").text
    assert "Profil fertig" in other and 'class="msg ok"' not in other  # only the task line of the header
    with Store(tmp_path / "web.sqlite") as st:
        st.save_cv("second.md", CV + " x")
    assert 'class="msg ok"' not in c.get("/cv/2").text  # the message belongs to CV 1 only


# --- profile ---

def test_profile_groups_by_category_with_names_in_the_ui_language(tmp_path):
    own_profile(tmp_path)
    c = make_client(tmp_path)
    en, de = c.get("/cv/1").text, c.get("/cv/1", headers=DE).text
    headings = re.compile(r'<h2 class="category">\s*([^<]+?)\s*</h2>')
    assert headings.findall(en) == ["Programming language", "Method", "Other"]
    assert headings.findall(de) == ["Programmiersprache", "Methode", "Sonstige"]
    assert "Test automation" in en and "Testautomatisierung" not in en and "zz_unknown_skill" in en
    assert "Testautomatisierung" in de and "Programmiersprache" in de and "Sonstige" in de
    assert "Strong" in en and "Vertieftes Wissen" in de
    assert "10 years" in en and "2.5 years" in en and "2,5 Jahre" in de and "10 Jahre" in de
    assert "last used 2019" in en and "zuletzt genutzt 2019" in de
    assert "2016–today" in en and "2016–heute" in de


def test_one_year_and_one_character_are_singular(tmp_path):
    evidence = Evidence(quote="Java quote", context="Acme", period_from=2025, period_to=None, strength="outcome",
                        verified=True)
    with Store(tmp_path / "web.sqlite") as store:
        cv_id = store.save_cv("own.md", CV)
        store.save_profile(cv_id, CandidateProfile(language="en", stations=[], skills=[
            CandidateSkill(skill_id="java", level="working", years=1, last_used=2026, evidence=[evidence])]), "fake")
    c = make_client(tmp_path)
    en, de = c.get("/cv/1").text, c.get("/cv/1", headers=DE).text
    assert "1 year" in en and "1 years" not in en and "1 Jahr" in de and "1 Jahre" not in de
    upload(c, "cv.md", (CV + " Other.").encode())  # another text: the same one would open CV 1
    refused = c.post("/cv/2/text", data={"text": "x"})
    assert refused.status_code == 400 and "1 character" in refused.text and "1 characters" not in refused.text


def test_profile_marks_unverified_quotes_and_strength(tmp_path):
    own_profile(tmp_path)
    page = make_client(tmp_path).get("/cv/1").text
    for strength in ["outcome", "activity", "claim"]:
        assert f"strength-{strength}" in page
    assert page.count("not found in the text") == 1
    quote = page.index("Quote claim")
    assert page.index("not found in the text") > quote - 400  # next to the unverified quote
    assert "Nur genannt" in make_client(tmp_path).get("/cv/1", headers=DE).text
    assert "nicht im Text gefunden" in make_client(tmp_path).get("/cv/1", headers=DE).text


def test_profile_shows_when_it_was_created_and_links_to_the_text(tmp_path):
    own_profile(tmp_path)
    page = make_client(tmp_path).get("/cv/1").text
    assert re.search(r"profile created \d\d\.\d\d\.\d{4} \d\d:\d\d", page) and 'href="/cv/1/text"' in page
    for link in ["/cv/1", "/cv/1/matches", "/cv/1/market", "/cv/1/paths"]:
        assert f'href="{link}"' in page


def test_profile_of_an_unanalyzed_cv_points_to_the_analysis(tmp_path):
    c = make_client(tmp_path)
    upload(c, "cv.md", CV.encode())
    page = c.get("/cv/1").text
    assert "not been analyzed" in page and 'href="/cv/1/text"' in page


def test_profile_has_a_level_and_hide_form_per_skill(tmp_path):
    own_profile(tmp_path)
    page = make_client(tmp_path).get("/cv/1").text
    assert page.count('action="/cv/1/skill/java"') == 2 and 'id="skill-java"' in page
    block = skill_block(page, "java")
    assert block.count("<option") == 4 and re.search(r'<option value="working" selected>', block)
    assert 'name="hidden" value="1"' in block and ">Hide<" in block


def test_hide_skill_and_change_level(tmp_path):
    c, api = analyzed_client(tmp_path)
    analyze_postings_for_cv_1(tmp_path, api)
    assert statuses(tmp_path, "selenium") == ["met", "met"]
    r = c.post("/cv/1/skill/selenium", data={"hidden": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cv/1#skill-selenium"
    assert statuses(tmp_path, "selenium") == ["gap", "gap"]
    assert c.app.state.skillpose.tasks.current.name == "rematch"
    page = c.get("/cv/1").text
    assert re.search(r'class="skill is-hidden" id="skill-selenium"', page) and ">Show<" in page
    assert "Matches recomputed: 2 postings" in page
    c.post("/cv/1/skill/selenium", data={"hidden": "0"})
    assert statuses(tmp_path, "selenium") == ["met", "met"]
    c.post("/cv/1/skill/java", data={"level": "basic"})
    with Store(tmp_path / "web.sqlite") as st:
        profile = st.profile(1)
        assert {s.skill_id: (s.level, s.hidden) for s in profile.skills} == {"selenium": ("expert", False),
                                                                          "java": ("basic", False)}
    assert 'value="basic" selected' in c.get("/cv/1").text


def test_skill_edit_rejects_wrong_values_and_unknown_skills(tmp_path):
    c, _ = analyzed_client(tmp_path)
    assert c.post("/cv/1/skill/no_such_skill", data={"hidden": "1"}).status_code == 404
    assert c.post("/cv/1/skill/java", data={"level": "god"}).status_code == 400
    assert c.post("/cv/1/skill/java", data={"hidden": "maybe"}).status_code == 400
    r = c.post("/cv/1/skill/java", data={}, follow_redirects=False)  # nothing to change
    assert r.status_code == 303 and c.app.state.skillpose.tasks.current.name == "cv"
    with Store(tmp_path / "web.sqlite") as st:
        assert {s.skill_id: (s.level, s.hidden) for s in st.profile(1).skills}["java"] == ("strong", False)


def test_a_skill_edit_while_another_task_runs_is_saved_and_its_rematch_waits(tmp_path):
    c, _ = analyzed_client(tmp_path)
    with task_running(c, "postings", "/postings"):
        r = c.post("/cv/1/skill/java", data={"level": "basic"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/cv/1?msg=queued&skill=java#skill-java"
        page = c.get("/cv/1?msg=queued&skill=java").text
        assert QUEUED in skill_block(page, "java")
        assert "Queued" not in skill_block(page, "selenium")
        assert "Waiting: Recompute matches" in page
        with Store(tmp_path / "web.sqlite") as st:
            assert {s.skill_id: s.level for s in st.profile(1).skills}["java"] == "basic"
    task = c.app.state.skillpose.tasks.current
    assert task.name == "rematch" and task.target == "/cv/1" and task.ok


def test_a_skill_edit_while_a_task_of_this_cv_runs_changes_nothing(tmp_path):
    # the running rematch may have read the profile before the edit, and the same task is never queued twice
    c, _ = analyzed_client(tmp_path)
    with task_running(c, "rematch", "/cv/1"):
        r = c.post("/cv/1/skill/java", data={"level": "basic"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/cv/1?msg=busy&skill=java#skill-java"
        page = c.get(r.headers["location"]).text
        assert "Another task is running" in skill_block(page, "java")
        assert "Another task is running" not in skill_block(page, "selenium")
    with Store(tmp_path / "web.sqlite") as st:
        assert {s.skill_id: s.level for s in st.profile(1).skills}["java"] == "strong"


def test_a_skill_edit_when_five_tasks_wait_changes_nothing(tmp_path):
    c, _ = analyzed_client(tmp_path)
    with task_running(c, "postings", "/postings") as runner:
        for i in range(5):
            runner.start("cv", f"/cv/{i + 2}", lambda: "")
        r = c.post("/cv/1/skill/java", data={"level": "basic"}, follow_redirects=False)
        assert r.headers["location"] == "/cv/1?msg=busy&skill=java#skill-java"
    with Store(tmp_path / "web.sqlite") as st:
        assert {s.skill_id: s.level for s in st.profile(1).skills}["java"] == "strong"


def test_every_task_creates_its_own_client(tmp_path):
    api, made = ScriptedClient(), []
    c, _ = analyzed_client(tmp_path)
    c.app.state.skillpose.config.client_factory = lambda: made.append(1) or api
    c.post("/cv/1/skill/java", data={"level": "basic"})
    task = c.app.state.skillpose.tasks.current
    assert task.name == "rematch" and task.target == "/cv/1" and task.ok and made == [1]
    assert task.message == "Matches recomputed: 0 postings"


def test_the_cv_task_reports_the_matching_with_the_postings(tmp_path):
    api = ScriptedClient(extra_requirement="playwright")
    c = make_client(tmp_path, client=api)
    analyze_postings_for_cv_1(tmp_path, api, n=2)  # two postings that need selenium, java, kubernetes and playwright
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    seen = record_progress(c)
    c.post(loc.replace("/text", "/analyze"), data={"confirm": "1"})
    assert seen == ["Matching with the CV: 0 of 2 checked", "Matching with the CV: 1 of 2 checked",
                    "Matching with the CV: 2 of 2 checked", "Saving matches"]
    assert c.app.state.skillpose.tasks.current.message == "Profile ready: 2 skills, 2 postings matched"


def test_analysis_task_runs_after_the_request_has_ended(tmp_path):
    # in a thread the request's store is closed long before the task runs: the task needs a store and client of its own
    client = ScriptedClient()
    c = make_client(tmp_path, client=client)
    runner = TaskRunner()
    c.app.state.skillpose.tasks = runner
    loc = upload(c, "cv.md", CV.encode()).headers["location"]
    assert c.post(loc.replace("/text", "/analyze"), data={}, follow_redirects=False).status_code == 303
    deadline = time.monotonic() + 10
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.current.ok and runner.current.message == "Profile ready: 2 skills, 0 postings matched"
    with Store(tmp_path / "web.sqlite") as st:
        assert st.profile(1) is not None


def test_delete_cv(tmp_path):
    c, _ = analyzed_client(tmp_path)
    r = c.post("/cv/1/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?msg=deleted"
    with Store(tmp_path / "web.sqlite") as st:
        assert st.cvs() == []
    start = c.get(r.headers["location"]).text
    assert "CV deleted." in start and start.index("CV deleted.") < start.index('action="/cv"')
    assert c.get("/cv/1").status_code == 404 and c.post("/cv/1/delete").status_code == 404
    assert "CV deleted" not in c.get("/").text
    assert "Lebenslauf gelöscht." in c.get("/?msg=deleted", headers=DE).text


def test_a_cv_whose_task_runs_cannot_be_deleted(tmp_path):
    own_profile(tmp_path)
    c = make_client(tmp_path)
    with task_running(c, "rematch", "/cv/1"):
        r = c.post("/cv/1/delete", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/cv/1?msg=delete_busy"
    assert c.post("/cv/1/delete", follow_redirects=False).headers["location"] == "/?msg=deleted"


def test_delete_needs_a_second_step_in_the_page(tmp_path):
    own_profile(tmp_path)
    page = make_client(tmp_path).get("/cv/1").text
    assert "<details" in page and 'action="/cv/1/delete"' in page and "Yes, delete for good" in page


def list_row(page, cv_id):
    """The row of one CV in the list of the start page."""
    return re.search(rf'<li id="cv-{cv_id}">.*?</li>', page, re.S)[0]


def is_open(row):
    return re.search(r"<details[^>]*\bopen\b", row) is not None


def two_cvs(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        return st.save_cv("first.md", CV), st.save_cv("second.md", CV + " Other.")


def test_each_cv_of_the_list_can_be_deleted_in_two_steps(tmp_path):
    first, second = two_cvs(tmp_path)
    c = make_client(tmp_path)
    page = c.get("/").text
    for cv_id in (first, second):
        row = list_row(page, cv_id)
        assert re.search(r"<details[^>]*>\s*<summary>Delete", row) and f'action="/cv/{cv_id}/delete"' in row
        assert "Yes, delete for good" in row and '<input type="hidden" name="back" value="start">' in row
        assert not is_open(row)
    assert re.search(r"<summary>Löschen", c.get("/", headers=DE).text)
    r = c.post(f"/cv/{first}/delete", data={"back": "start"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?msg=deleted"
    start = c.get(r.headers["location"]).text
    assert "CV deleted." in start and f'id="cv-{first}"' not in start and "second.md" in start
    with Store(tmp_path / "web.sqlite") as st:
        assert [cv.id for cv in st.cvs()] == [second]


def test_a_cv_of_the_list_whose_task_runs_or_waits_says_so_at_its_row(tmp_path):
    first, second = two_cvs(tmp_path)
    c = make_client(tmp_path)
    busy = "A task for this CV is running"
    with task_running(c, "rematch", f"/cv/{first}"):
        r = c.post(f"/cv/{first}/delete", data={"back": "start"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/?msg=delete_busy&cv={first}#cv-{first}"
        page = c.get(r.headers["location"]).text
        c.post(f"/cv/{second}/analyze", data={"confirm": "1"})   # waits behind the running task
        r = c.post(f"/cv/{second}/delete", data={"back": "start"}, follow_redirects=False)
        assert r.headers["location"] == f"/?msg=delete_busy&cv={second}#cv-{second}"
    assert busy in list_row(page, first) and is_open(list_row(page, first)) and page.count(busy) == 1
    assert busy not in list_row(page, second) and not is_open(list_row(page, second))
    assert "Für diesen Lebenslauf läuft eine Aufgabe" in c.get(f"/?msg=delete_busy&cv={first}", headers=DE).text
    with Store(tmp_path / "web.sqlite") as st:
        assert len(st.cvs()) == 2
    for query in ("?msg=delete_busy&cv=x", "?msg=delete_busy&cv=99", "?msg=delete_busy"):
        page = c.get("/" + query)
        assert page.status_code == 200 and busy not in page.text, query


def test_a_cv_whose_path_task_runs_or_waits_cannot_be_deleted(tmp_path):
    # review 15o: a path task runs under /cv/{id}/paths; its result needs the CV, from the list and from the CV page
    own_profile(tmp_path)
    c = make_client(tmp_path)
    busy = "A task for this CV is running"

    def refused():
        r = c.post("/cv/1/delete", data={"back": "start"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?msg=delete_busy&cv=1#cv-1"
        row = list_row(c.get(r.headers["location"]).text, 1)
        assert busy in row and is_open(row)
        r = c.post("/cv/1/delete", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/cv/1?msg=delete_busy"
        assert busy in c.get(r.headers["location"]).text

    with task_running(c, "path", "/cv/1/paths"):
        refused()
    with task_running(c, "postings", "/postings") as runner:
        assert runner.start("path", "/cv/1/paths", lambda: "", key="test_automation") is Start.QUEUED
        refused()
    with Store(tmp_path / "web.sqlite") as st:
        assert [cv.id for cv in st.cvs()] == [1]
    r = c.post("/cv/1/delete", data={"back": "start"}, follow_redirects=False)
    assert r.headers["location"] == "/?msg=deleted"


def test_the_list_offers_no_delete_without_a_key(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        st.save_cv("own.md", CV)
    page = make_client(tmp_path, key=False).get("/").text   # no key, no demo data: the own list, read-only
    assert "own.md" in page and "/delete" not in page and "<details" not in page
    (tmp_path / "d").mkdir()
    write_fixture_bundles(tmp_path / "d" / "demo")
    demo = make_client(tmp_path / "d", key=False, demo_dir=tmp_path / "d" / "demo").get("/").text
    assert NAME in demo and "/delete" not in demo


# --- demo and no key ---

def test_demo_profile_is_read_only(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    page = c.get("/cv/1", headers=DE).text
    assert NAME in page and "Selenium" in page and "Expertenwissen" in page
    assert "<form" not in page
    assert "Hochladen und Analysieren sind gesperrt" in page
    text = c.get("/cv/1/text", headers=DE).text
    assert "readonly" in text and 'action="/cv/1/analyze"' not in text and 'action="/cv/1/text"' not in text
    assert 'action="/cv/1/delete"' not in text


def test_without_a_key_the_text_page_offers_no_analysis(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        st.save_cv("own.md", CV)
    # no key and no demo data: the own database is shown, but nothing can be changed or analyzed
    c = make_client(tmp_path, key=False)
    page = c.get("/cv/1/text").text
    assert 'action="/cv/1/analyze"' not in page and "No API key set" in page
    assert c.get("/cv/1").status_code == 200
