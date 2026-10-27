"""Web pages for postings (paste, upload, analyze, Message Batch) and for the matches of a CV."""

import json
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from demo_fixture import NAME, bundle_with_proposals, write_fixture_bundles
from fakes import (
    CV,
    FakeBatchClient,
    FakeClient,
    ScriptedClient,
    api_error,
    current_fingerprint,
    not_found,
    ok,
    refusal,
    req,
)
from markupsafe import escape
from webkit import make_client, record_progress, task_running

from skillpose.core.config import load_settings
from skillpose.core.llm import Usage, estimate_usd
from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    Evidence,
    JobDraft,
    JobMatch,
    JobProfile,
    Requirement,
    RequirementMatch,
    SkillProposal,
    text_hash,
)
from skillpose.pipeline import BatchProgress, analyze_postings, estimate, open_run, plan_postings
from skillpose.store.db import Store
from skillpose.web import __file__ as web_init
from skillpose.web import pages_jobs
from skillpose.web.deps import fmt_time, fmt_usd
from skillpose.web.tasks import Start, TaskRunner

DE = {"accept-language": "de"}
ASK_ALWAYS = load_settings().model_copy(update={"confirm_above_usd": 0.0001})
REFRESH = '<meta http-equiv="refresh"'  # base.html reloads a page every 3 s while a task runs
QUEUED = "Queued – starts when the running task has finished."
QUEUED_DE = "Eingereiht – startet, wenn die laufende Aufgabe fertig ist."
JOB = "Test Automation Engineer\nSelenium and Java required, Kubernetes a plus."
OTHER = "Data Analyst\nSQL and Power BI required."
QUOTE = "Built a Selenium test automation framework in Java"
# an unbalanced quote makes the rest of the file one field, longer than the csv module allows (128 KB)
STRAY_QUOTE_CSV = b'text;ref\n"Posting with a stray quote;a\n' + b"x" * 140_000 + b"\n"
twenty_texts = [f"Test Automation Engineer {i}\nSelenium and Java required, Kubernetes a plus." for i in range(20)]
thirty_texts = [f"Test Automation Engineer {i}\nSelenium and Java required, Kubernetes a plus." for i in range(30)]


def results_for(texts):
    """The result map of a FakeBatchClient: custom_id (hash of the text) -> a succeeded JobDraft."""
    draft = JobDraft(title="Test Automation Engineer", role_family="test_automation", seniority="mid",
                     requirements=[req("selenium"), req("java")])
    return {text_hash(text): ("succeeded", draft.model_dump_json()) for text in texts}


def add_postings(c, texts):
    data = json.dumps([{"text": text} for text in texts]).encode()
    r = c.post("/postings/upload", files=[("files", ("jobs.json", data))], follow_redirects=False)
    assert r.status_code == 303, r.text


def paste(c, text, ref=""):
    return c.post("/postings/text", data={"text": text, "ref": ref}, follow_redirects=False)


def upload(c, *files):
    """files: (name, bytes) pairs, sent in one request."""
    return c.post("/postings/upload", files=[("files", file) for file in files], follow_redirects=False)


def stored(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        return st.postings()


def row(page, text):
    """The one table row of the page that contains text."""
    found = [r for r in re.findall(r"<tr[ >].*?</tr>", page, re.S) if text in r]
    assert len(found) == 1, (text, len(found))
    return found[0]


def plain(html):
    """The text of some HTML, tags gone, white space collapsed."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def cells(html):
    return [re.sub(r"<[^>]+>", "", cell).strip() for cell in re.findall(r"<td[ >].*?</td>", html, re.S)]


def cv_with_profile(tmp_path):
    """CV 1 with a profile of two evidenced skills."""
    evidence = Evidence(quote=QUOTE, context="Acme", period_from=2016, period_to=None, strength="outcome",
                        verified=True)
    skills = [CandidateSkill(skill_id=s, level="expert", years=10, last_used=2026, evidence=[evidence])
              for s in ("selenium", "java")]
    with Store(tmp_path / "web.sqlite") as st:
        cv_id = st.save_cv("cv.md", CV)
        st.save_profile(cv_id, CandidateProfile(language="en", skills=skills, stations=[]), "fake")
    return cv_id


def pending_batch(tmp_path, text="Pending posting"):
    with Store(tmp_path / "web.sqlite") as st:
        st.save_batch("batch_1", [st.save_posting(text, "pending.md")])


def split_client(texts, statuses, **kwargs):
    """A ScriptedClient with a FakeBatchClient (kwargs) whose batches answer succeeded for every text; the batch
    client logs each batch it creates into client.calls."""
    client = ScriptedClient()
    batches = FakeBatchClient(results=results_for(texts), statuses=statuses, log=client.calls, **kwargs)
    client.messages.batches = batches.messages.batches
    return client, batches


def pending_four(tmp_path):
    """batch_1 of an earlier visit with four postings."""
    with Store(tmp_path / "web.sqlite") as st:
        st.save_batch("batch_1", [st.save_posting(f"Pending posting {i}", f"pending{i}.md") for i in range(4)])


def wait_idle(c):
    runner, deadline = c.app.state.skillpose.tasks, time.monotonic() + 5
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.01)


def checked(c, batch_id):
    """The time of the last check of a batch, as the page shows it."""
    return fmt_time(c.app.state.skillpose.batch_progress[batch_id].checked, "%H:%M")


def waiting_client(asked):
    """An API client whose batch never ends; asked collects the ids of the status requests."""
    client = ScriptedClient()
    client.messages.batches = SimpleNamespace(retrieve=lambda batch_id: asked.append(batch_id) or SimpleNamespace(
        id=batch_id, processing_status="in_progress"))
    return client


def seed_match(st, cv_id, title, tier, must_coverage, rows, *, nice_coverage=0.5, family="test_automation",
               requirement_fields=None):
    """A posting with a job profile and a match; rows: (skill id, importance, status, gap type, reason).
    requirement_fields: skill id -> fields of its Requirement other than the default kind "tool" and quote."""
    text = f"{title}\nposting text"
    h = st.save_posting(text, f"{title}.md")
    requirements = [Requirement(**{"skill_id": s, "importance": i, "min_level": None, "min_years": None,
                                   "kind": "tool", "quote": f"need {s}", **(requirement_fields or {}).get(s, {})})
                    for s, i, *_ in rows]
    st.save_job_profile(JobProfile(text_hash=h, title=title, role_family=family, seniority="mid",
                                   requirements=requirements), "fake")
    matches = [RequirementMatch(requirement=r, status=status, reason=reason, evidence=None, gap_type=gap,
                                effort_weeks={"weeks": 4, "months": 12}.get(gap))
               for r, (_, _, status, gap, reason) in zip(requirements, rows, strict=True)]
    st.save_match(cv_id, JobMatch(text_hash=h, matches=matches, must_coverage=must_coverage,
                                  nice_coverage=nice_coverage, tier=tier), current_fingerprint())
    return h


# --- paste and upload ---

def test_paste_and_upload_store_each_posting_once(tmp_path):
    c = make_client(tmp_path)
    first = paste(c, JOB)
    assert first.status_code == 303 and first.headers["location"] == "/postings?msg=posting_new"
    assert "Posting saved." in c.get(first.headers["location"]).text
    again = paste(c, JOB.replace("\n", "\r\n"), ref="same text, other line breaks")
    assert again.headers["location"] == "/postings?msg=posting_known"
    assert "already present" in c.get(again.headers["location"]).text
    items = [{"ref": "from json", "text": JOB}, {"ref": "second", "text": OTHER}, {"text": "Third posting\nPython"}]
    csv_data = f'text;ref\n"{OTHER}";dup\n"New CSV posting\nRust";csv ref\n'.encode()
    r = upload(c, ("jobs.json", json.dumps(items).encode()), ("more.csv", csv_data), ("one.md", JOB.encode()))
    assert r.status_code == 303
    assert r.headers["location"] == "/postings?msg=uploaded&new=3&present=3&skipped=0"
    page = c.get(r.headers["location"]).text
    assert "3 new, 3 already present, 0 skipped" in page
    assert "3 neu, 3 schon vorhanden, 0 übersprungen" in c.get(r.headers["location"], headers=DE).text
    rows = stored(tmp_path)
    assert len(rows) == 4 and sum(row.status == "open" for row in rows) == 4
    refs = {row.hash: row.refs for row in rows}
    assert refs[text_hash(JOB)] == ["Test Automation Engineer", "same text, other line breaks", "from json", "one.md"]
    assert refs[text_hash(OTHER)] == ["second", "dup"]
    assert refs[text_hash("Third posting\nPython")] == ["jobs.json#3"]  # named after the file the user sent
    assert refs[text_hash("New CSV posting\nRust")] == ["csv ref"]


def test_paste_takes_the_first_line_as_reference_and_keeps_a_given_one(tmp_path):
    c = make_client(tmp_path)
    paste(c, "\n\n  " + "x" * 100 + "\nsecond line")
    paste(c, "Posting with a name", ref="  My   own\tname \x00 ")
    refs = sorted(row.refs[0] for row in stored(tmp_path))
    assert refs == ["My own name", "x" * 60]


def test_paste_without_text_or_with_a_text_that_is_too_long_is_refused_next_to_the_form(tmp_path):
    c = make_client(tmp_path)
    empty = c.post("/postings/text", data={"text": " \r\n ", "ref": ""})
    assert empty.status_code == 400 and "Paste the text of a posting first." in empty.text
    assert empty.text.index('action="/postings/text"') < empty.text.index("Paste the text") < empty.text.index(
        'action="/postings/upload"')
    long_text = "Very long posting <b>x</b>\n" + "x" * 30001
    refused = c.post("/postings/text", data={"text": long_text, "ref": "long one"})
    assert refused.status_code == 400
    assert f"Skipped “long one”: {len(long_text.strip())} characters, at most 30000 are allowed." in refused.text
    assert "Very long posting &lt;b&gt;x&lt;/b&gt;" in refused.text  # what the user pasted stays in the field
    assert "Zeichen, erlaubt sind höchstens 30000" in c.post("/postings/text", data={"text": long_text},
                                                             headers=DE).text
    assert stored(tmp_path) == []


def test_wrong_upload_type_is_rejected(tmp_path):
    c = make_client(tmp_path)
    r = upload(c, ("setup.exe", b"MZ" * 200))
    assert r.status_code == 400 and "setup.exe: file type .exe is not supported" in r.text
    assert r.text.index('action="/postings/upload"') < r.text.index("setup.exe") < r.text.index('id="analyze-title"')
    assert stored(tmp_path) == []
    mixed = upload(c, ("evil.exe", b"MZ"), ("good.md", JOB.encode()), ("no_suffix", b"x"))
    assert mixed.status_code == 200 and "evil.exe: file type .exe" in mixed.text
    assert "no_suffix: file type (none)" in mixed.text and "1 new, 0 already present, 0 skipped" in mixed.text
    assert [row.refs for row in stored(tmp_path)] == [["good.md"]]
    assert "Dateityp .exe" in c.post("/postings/upload", files=[("files", ("a.exe", b"x"))], headers=DE).text


def test_upload_without_a_file_says_so(tmp_path):
    r = make_client(tmp_path).post("/postings/upload", files=[("files", ("", b""))])
    assert r.status_code == 400 and "Choose at least one file first." in r.text
    assert make_client(tmp_path).post("/postings/upload", data={}).status_code == 400


def test_upload_limits_are_stated_and_checked(tmp_path):
    c = make_client(tmp_path)
    page = c.get("/postings").text
    assert "at most 5 MB" in page and "together at most 6 MB" in page and 'accept=".md,.txt,.json,.csv"' in page
    assert "multiple" in page and "zusammen höchstens 6 MB" in c.get("/postings", headers=DE).text
    big = upload(c, ("big.txt", b"a" * (5 * 1024 * 1024 + 1)), ("ok.txt", JOB.encode()))
    assert "big.txt: file larger than 5 MB" in big.text and big.status_code == 200
    assert [row.refs for row in stored(tmp_path)] == [["ok.txt"]]
    assert "größer als 5 MB" in c.post("/postings/upload", headers=DE,
                                       files=[("files", ("big.txt", b"a" * (5 * 1024 * 1024 + 1)))]).text
    too_much = upload(c, ("a.txt", b"a" * 3_500_000), ("b.txt", b"b" * 3_500_000))
    assert too_much.status_code == 413  # the guard: the whole request is capped at 6 MB


def test_upload_never_writes_under_the_given_name(tmp_path):
    # a device name such as CON.md would not be a file on Windows; the bytes go to a fixed name in a temporary folder
    c = make_client(tmp_path)
    r = upload(c, ("CON.md", JOB.encode()), ("nul.txt", OTHER.encode()), ("..\\..\\evil.md", b"Evil posting\nx"))
    assert r.status_code == 303
    assert sorted(row.refs[0] for row in stored(tmp_path)) == ["CON.md", "evil.md", "nul.txt"]


def test_broken_csv_and_json_are_reported_with_the_file_name(tmp_path):
    c = make_client(tmp_path)
    cases = [("jobs.csv", b"name;ref\nfoo;bar\n", "CSV needs a"),
             ("jobs.csv", b"text;ref\nfoo;bar;baz\n", "jobs.csv line 2: more fields than columns"),
             ("jobs.json", b"{not json", "Expecting property name"),
             ("jobs.json", b'{"text": "a dict, not a list"}', "unexpected structure"),
             ("jobs.json", b'[{"ref": "no text"}]', "unexpected structure"),
             ("jobs.csv", STRAY_QUOTE_CSV, "field larger than field limit"),
             ("jobs.json", b"[" * 200_000, "unexpected structure")]  # nested deeper than Python can read
    for name, data, detail in cases:
        r = upload(c, (name, data))
        assert r.status_code == 400, (name, data[:40])
        assert f"{name}: the file could not be read" in r.text and detail in r.text, (name, data)
        assert r.text.index('action="/postings/upload"') < r.text.index(f"{name}: the file") < r.text.index(
            'id="analyze-title"')
        assert "posting.csv" not in r.text and "posting.json" not in r.text  # the fixed name stays inside
    assert stored(tmp_path) == []


def test_a_broken_file_does_not_cost_the_other_files_of_the_upload(tmp_path):
    c = make_client(tmp_path)
    r = upload(c, ("jobs.csv", STRAY_QUOTE_CSV),("deep.json", b"[" * 200_000), ("good.md", JOB.encode()))
    assert r.status_code == 200 and "jobs.csv: the file could not be read" in r.text
    assert "deep.json: the file could not be read" in r.text and "1 new, 0 already present, 0 skipped" in r.text
    assert [row.refs for row in stored(tmp_path)] == [["good.md"]]


def test_upload_reports_postings_that_are_too_long_and_empty_ones(tmp_path):
    c = make_client(tmp_path)
    items = [{"ref": "huge", "text": "x" * 30001}, {"ref": "fine", "text": JOB}, {"ref": "blank", "text": "  "}]
    r = upload(c, ("jobs.json", json.dumps(items).encode()))
    assert r.status_code == 200 and "1 new, 0 already present, 2 skipped" in r.text
    assert "Skipped “huge”: 30001 characters, at most 30000 are allowed." in r.text
    assert [row.refs for row in stored(tmp_path)] == [["fine"]]
    nothing = upload(c, ("jobs.json", json.dumps([items[0]]).encode()))
    assert nothing.status_code == 400  # nothing at all could be stored


def test_user_text_is_escaped_on_the_postings_pages(tmp_path):
    evil = "<script>alert(1)</script>"
    c = make_client(tmp_path)
    paste(c, f"{evil} Engineer\nSelenium", ref="<img src=x onerror=alert(1)>")
    with Store(tmp_path / "web.sqlite") as st:
        st.set_posting_error(st.open_postings()[0], f"{evil} reason")
    page = c.get("/postings").text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page and "&lt;script&gt;alert(1)&lt;/script&gt; reason" in page
    assert "<img src=x" not in page and page.count("<script") == 1  # only /static/app.js
    bad_file = upload(c, ("<b>bold.exe", b"x")).text
    assert "&lt;b&gt;bold.exe" in bad_file and "<b>bold" not in bad_file


# --- the list ---

def test_the_list_shows_title_role_status_reason_and_the_time_seen(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        done = st.save_posting(JOB, "job.md")
        st.save_job_profile(JobProfile(text_hash=done, title="QA <Lead>", role_family="qa_management",
                                       seniority="lead", requirements=[]), "fake")
        failed = st.save_posting(OTHER, "other.md")
        st.set_posting_error(failed, "refusal")
        st.save_batch("batch_1", [st.save_posting("In the batch", "batch.md")])
    page = c.get("/postings").text
    analyzed, failing, batched = row(page, "QA &lt;Lead&gt;"), row(page, "other.md"), row(page, "batch.md")
    assert "QA management" in analyzed and "analyzed" in analyzed and "job.md" in analyzed
    assert re.search(r"\d\d\.\d\d\.\d{4} \d\d:\d\d", analyzed)
    assert "open" in failing and "refusal" in failing and "failed" in failing
    assert "in batch" in batched and "3 postings" in page
    assert page.index("batch.md") < page.index("other.md") < page.index("QA &lt;Lead&gt;")  # newest first
    de = c.get("/postings", headers=DE).text
    assert "QA-Management" in de and "analysiert" in de and "im Batch" in de and "refusal" in de
    assert "Gesehen am" in de and "Rollenfamilie" in de


def test_one_posting_is_singular(tmp_path):
    c = make_client(tmp_path)
    paste(c, JOB)
    assert "(1 posting)" in c.get("/postings").text and "(1 Inserat)" in c.get("/postings", headers=DE).text


def test_an_empty_list_says_so_and_has_no_analyze_button(tmp_path):
    page = make_client(tmp_path).get("/postings").text
    assert "No postings yet." in page and "No open postings." in page and 'action="/postings/analyze"' not in page
    assert all(f'action="{a}"' in page for a in ("/postings/text", "/postings/upload"))
    assert page.count("data-busy=") >= 2


def test_the_analyze_button_shows_the_number_the_cost_and_the_batch_rule(tmp_path):
    c = make_client(tmp_path)
    paste(c, JOB), paste(c, OTHER)
    page = c.get("/postings").text
    # from 20, the first goes at once at the single price and the rest in batches
    assert re.search(r"Analyze open \(2, about \d+ cents; from 20: the first at once, the rest in batches at half "
                     r"price\)", page)
    assert 'action="/postings/analyze"' in page and "data-busy=" in page
    de = c.get("/postings", headers=DE).text
    assert re.search(r"Offene analysieren \(2, ca\. \d+ Cent; ab 20: das erste sofort, der Rest in Batches zum "
                     r"halben Preis\)", de)
    assert "as a batch at half price" not in page and "als Batch zum halben Preis" not in de
    add_postings(c, twenty_texts)  # 22 open: the estimate counts a batch
    assert "Analyze open (22," in c.get("/postings").text


# --- analysis ---

def test_analyze_and_show_matches(tmp_path):
    api = ScriptedClient()
    c = make_client(tmp_path, client=api)
    cv_with_profile(tmp_path)
    paste(c, JOB), paste(c, OTHER)
    r = c.post("/postings/analyze", data={"confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/postings"
    task = c.app.state.skillpose.tasks.current
    assert task.name == "postings" and task.target == "/postings" and task.ok
    assert task.message == "Postings analyzed: 2, still open: 0"
    page = c.get("/postings").text
    assert "Postings analyzed: 2, still open: 0" in page and "analyzed" in row(page, "Data Analyst")
    matches = c.get("/cv/1/matches").text
    assert "Test Automation Engineer" in matches and "Data Analyst" in matches and 'class="mark tier-1"' in matches
    assert "1 · fits" in matches and "1 · passt" in c.get("/cv/1/matches", headers=DE).text
    assert len([x for x in api.calls if x.path == "JobDraft"]) == 2


def test_analysis_asks_first_above_the_threshold_and_names_the_cost(tmp_path):
    api = ScriptedClient()
    c = make_client(tmp_path, client=api, settings=ASK_ALWAYS)
    paste(c, JOB)
    r = c.post("/postings/analyze", data={})
    assert r.status_code == 200 and api.calls == [] and c.app.state.skillpose.tasks.current is None
    assert re.search(r"Yes, spend about \d+\.\d\d USD", r.text) and 'name="confirm" value="1"' in r.text
    assert 'action="/postings/analyze"' in r.text and 'href="/postings"' in r.text and "Open postings: 1" in r.text
    assert re.search(r"Ja, ca\. \d+,\d\d USD ausgeben", c.post("/postings/analyze", data={}, headers=DE).text)
    assert 'name="count" value="1"' in r.text  # the confirmation names what it confirms
    done = c.post("/postings/analyze", data={"confirm": "1", "count": "1"}, follow_redirects=False)
    assert done.status_code == 303 and len(api.calls) == 1


def test_the_confirmation_holds_only_for_the_number_of_postings_asked(tmp_path):
    api = ScriptedClient()
    c = make_client(tmp_path, client=api, settings=ASK_ALWAYS)
    paste(c, JOB)
    assert 'name="count" value="1"' in c.post("/postings/analyze", data={}).text
    paste(c, OTHER)  # another tab adds a posting before "Yes"
    again = c.post("/postings/analyze", data={"confirm": "1", "count": "1"})
    assert again.status_code == 200 and api.calls == [] and c.app.state.skillpose.tasks.current is None
    assert "Open postings: 2" in again.text and 'name="count" value="2"' in again.text
    assert "The number of open postings has changed" in again.text
    assert "Die Zahl der offenen Inserate hat sich seit der Frage geändert" in c.post(
        "/postings/analyze", data={"confirm": "1", "count": "1"}, headers=DE).text
    unbound = c.post("/postings/analyze", data={"confirm": "1"})  # a confirmation without a number confirms nothing
    assert unbound.status_code == 200 and api.calls == []
    done = c.post("/postings/analyze", data={"confirm": "1", "count": "2"}, follow_redirects=False)
    assert done.status_code == 303 and len(api.calls) == 2


def test_the_task_sends_no_more_postings_than_the_request_counted(tmp_path, monkeypatch):
    api = ScriptedClient()
    c = make_client(tmp_path, client=api)
    paste(c, JOB)
    captured = []
    monkeypatch.setattr(pages_jobs, "start_task", lambda request, name, target, work: captured.append(work) or True)
    assert c.post("/postings/analyze", data={}, follow_redirects=False).status_code == 303
    paste(c, OTHER)  # between the request and the task
    with Store(tmp_path / "web.sqlite") as st:
        message = captured[0](open_run(load_settings(), st, "en", client=api))
        assert len(st.open_postings()) == 2
    assert api.calls == [] and message == ("Nothing was sent: there are 2 open postings now, 1 when you started. "
                                           "Start the analysis again.")


def test_analysis_without_open_postings_starts_nothing(tmp_path):
    api = ScriptedClient()
    c = make_client(tmp_path, client=api)
    r = c.post("/postings/analyze", data={"confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/postings?msg=no_open"
    assert api.calls == [] and c.app.state.skillpose.tasks.current is None
    assert "There are no open postings." in c.get(r.headers["location"]).text


def test_analysis_while_another_task_runs_waits_and_runs_after_it(tmp_path):
    api = ScriptedClient()
    c = make_client(tmp_path, client=api)
    paste(c, JOB)
    with task_running(c, "cv", "/cv/1"):
        r = c.post("/postings/analyze", data={"confirm": "1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/postings?msg=queued"
        page = c.get(r.headers["location"]).text
        assert QUEUED in page and page.index('action="/postings/analyze"') < page.index(QUEUED)
        assert "Waiting: Analyze postings" in page and api.calls == []
        de = c.get(r.headers["location"], headers=DE).text
        assert QUEUED_DE in de and "Wartet: Inserate analysieren" in de
    assert [x.path for x in api.calls] == ["JobDraft"]
    page = c.get("/postings?msg=queued").text  # the address of the redirect, reloaded after the task has run
    assert "Queued –" not in page and "Waiting:" not in page


def test_analysis_is_refused_when_five_tasks_wait(tmp_path):
    api = ScriptedClient()
    c = make_client(tmp_path, client=api)
    paste(c, JOB)
    with task_running(c, "cv", "/cv/1") as runner:
        for i in range(5):
            assert runner.start("cv", f"/cv/{i + 2}", lambda: "") is Start.QUEUED
        r = c.post("/postings/analyze", data={"confirm": "1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/postings?msg=busy"
        page = c.get(r.headers["location"]).text
        assert "Another task is running" in page and page.index('action="/postings/analyze"') < page.index(
            "Another task")
    assert api.calls == []


def test_a_double_click_queues_the_analysis_once(tmp_path):
    api = ScriptedClient()
    c = make_client(tmp_path, client=api)
    paste(c, JOB)
    with task_running(c, "cv", "/cv/1") as runner:
        for _ in range(2):
            r = c.post("/postings/analyze", data={"confirm": "1"}, follow_redirects=False)
            assert r.headers["location"] == "/postings?msg=queued"
        assert [w.name for w in runner.waiting] == ["postings"]
    assert [x.path for x in api.calls] == ["JobDraft"]


def test_a_waiting_analysis_that_no_longer_fits_the_monthly_limit_sends_nothing(tmp_path):
    # the estimate fitted at the click; the task before it spent most of the rest while the analysis waited
    api = ScriptedClient()
    settings = load_settings().model_copy(update={"monthly_limit_usd": 0.05})
    c = make_client(tmp_path, client=api, settings=settings)
    paste(c, JOB)
    with Store(tmp_path / "web.sqlite") as st:
        run = open_run(settings, st, "de")
        cost = estimate(run, plan=plan_postings(run, st.open_postings()))
    spent = 0.05 - cost / 2
    with task_running(c, "cv", "/cv/1", spend=spent):
        r = c.post("/postings/analyze", data={"confirm": "1"}, headers=DE, follow_redirects=False)
        assert r.headers["location"] == "/postings?msg=queued"
    task = c.app.state.skillpose.tasks.current
    assert (task.name, task.ok) == ("postings", False) and api.calls == []
    assert task.message == (f"Monatsgrenze: {fmt_usd(spent, 'de')} von 0,05 USD ausgegeben, diese Analyse kostet "
                            f"etwa {fmt_usd(cost, 'de')} USD. Es wurde nichts gesendet.")  # the language of the click
    with Store(tmp_path / "web.sqlite") as st:
        assert len(st.open_postings()) == 1 and st.pending_batches() == []
        assert st.month_total_usd() == pytest.approx(spent)


def test_failed_posting_shows_its_reason(tmp_path):
    c = make_client(tmp_path, client=FakeClient([refusal()]))
    paste(c, JOB)
    c.post("/postings/analyze", data={"confirm": "1"})
    page = c.get("/postings").text
    failed = row(page, "Test Automation Engineer")
    assert "refusal" in failed and "open" in failed and "Postings analyzed: 0, still open: 1" in page
    assert c.app.state.skillpose.tasks.current.ok  # a refusal is a result, not a crash


def limit_run(tmp_path, headers=None):
    """Two open postings; the estimate fits into the monthly limit, but the first answer costs more than all of it."""
    draft = JobDraft(title="Big one", role_family="test_automation", seniority="mid", requirements=[req("selenium")])
    client = FakeClient([ok(draft, usage=Usage(200_000, 100_000, 0, 0))])
    settings = load_settings().model_copy(update={"monthly_limit_usd": 0.05})
    c = make_client(tmp_path, client=client, settings=settings)
    paste(c, JOB), paste(c, OTHER)
    r = c.post("/postings/analyze", data={"confirm": "1"}, headers=headers, follow_redirects=False)
    assert r.status_code == 303
    return c


def test_monthly_limit_message(tmp_path):
    c = limit_run(tmp_path)
    page = c.get("/postings").text
    assert "Monthly limit reached – 1 posting stays open" in page
    assert page.count("Monthly limit reached") == 2  # the task line and the message next to the analyze button
    assert page.index('id="analyze-title"') < page.rindex("Monthly limit reached")
    assert [row.status for row in stored(tmp_path)] == ["analyzed", "open"]  # what was paid for is kept, the rest stays
    assert c.app.state.skillpose.tasks.current.ok
    (tmp_path / "de").mkdir()
    de = limit_run(tmp_path / "de", DE).get("/postings", headers=DE).text
    assert "Monatsgrenze erreicht – 1 Inserat bleibt offen" in de
    paste(c, "Third posting", ref="third")
    refused = c.post("/postings/analyze", data={"confirm": "1"})  # nothing fits any more: refused before the task
    assert refused.status_code == 400 and "Monthly limit" in refused.text


def test_analysis_over_the_monthly_limit_is_refused_before_anything_is_sent(tmp_path):
    api = ScriptedClient()
    settings = load_settings().model_copy(update={"monthly_limit_usd": 0.01})
    c = make_client(tmp_path, client=api, settings=settings)
    paste(c, JOB)
    r = c.post("/postings/analyze", data={"confirm": "1"})
    assert r.status_code == 400 and api.calls == [] and c.app.state.skillpose.tasks.current is None
    assert "Monthly limit" in r.text and "nothing was sent" in r.text.lower()
    assert r.text.index('action="/postings/analyze"') < r.text.index("Monthly limit")
    assert "Monatsgrenze" in c.post("/postings/analyze", data={}, headers=DE).text


def test_the_plan_is_made_inside_the_task(tmp_path, monkeypatch):
    # a plan made in the request and run later could pay twice for postings another task analyzed in between
    api = ScriptedClient()
    c = make_client(tmp_path, client=api)
    paste(c, JOB), paste(c, OTHER)
    captured = []
    monkeypatch.setattr(pages_jobs, "start_task", lambda request, name, target, work: captured.append(work) or True)
    assert c.post("/postings/analyze", data={"confirm": "1"}, follow_redirects=False).status_code == 303
    assert len(captured) == 1 and api.calls == []
    with Store(tmp_path / "web.sqlite") as st:  # meanwhile another run analyzes the first posting
        run = open_run(load_settings(), st, "en", client=api)
        analyze_postings(run, plan_postings(run, st.open_postings()[:1], no_batch=True))
    assert len(api.calls) == 1
    with Store(tmp_path / "web.sqlite") as st:
        message = captured[0](open_run(load_settings(), st, "en", client=api))
        assert st.open_postings() == []
    assert len(api.calls) == 2 and message == "Postings analyzed: 1, still open: 0"  # one more posting, not two


def test_analysis_matches_every_cv_with_a_profile(tmp_path):
    api = ScriptedClient()
    c = make_client(tmp_path, client=api)
    cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.save_cv("no profile.md", CV + " x")
        second = st.save_cv("second.md", CV + " y")
        profile = st.profile(1)
        st.save_profile(second, profile.model_copy(update={"skills": profile.skills[:1]}), "fake")
    paste(c, JOB)
    c.post("/postings/analyze", data={"confirm": "1"})
    with Store(tmp_path / "web.sqlite") as st:
        assert [len(st.matches(cv_id)) for cv_id in (1, 2, 3)] == [1, 0, 1]


def test_skills_claude_proposed_reach_the_vocabulary_after_the_analysis(tmp_path):
    api = ScriptedClient(extra_requirement="zz_brand_new_skill")
    c = make_client(tmp_path, client=api)
    paste(c, JOB)
    c.post("/postings/analyze", data={"confirm": "1"})
    with Store(tmp_path / "web.sqlite") as st:
        assert "zz_brand_new_skill" in [skill.id for skill in st.added_skills()]


# --- Message Batch ---

def test_batch_is_collected_on_a_later_visit_even_after_restart(tmp_path):
    batches = FakeBatchClient(results=results_for(twenty_texts), statuses=["in_progress"] * 3 + ["ended"] * 3)
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    add_postings(c, twenty_texts)
    c.post("/postings/analyze", data={"confirm": "1"})
    assert "Batch" in c.get("/postings").text                       # erste Prüfung: in_progress
    c2 = make_client(tmp_path, client=client)                       # Neustart: neue App, gleiche DB
    c2.get("/postings")                                             # zweite Prüfung: ended → abgeholt
    assert len(batches.created) == 19                               # einmal abgeschickt, nie doppelt
    with Store(tmp_path / "web.sqlite") as st:
        assert st.open_postings() == []


def test_batch_survives_a_restart_between_two_checks(tmp_path):
    batches = FakeBatchClient(results=results_for(twenty_texts), statuses=["in_progress"] * 3 + ["ended"] * 3)
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    add_postings(c, twenty_texts)
    assert c.post("/postings/analyze", data={"confirm": "1"}, follow_redirects=False).status_code == 303
    assert "3 batches running" in c.get("/postings").text  # first check: in_progress
    with Store(tmp_path / "web.sqlite") as st:
        assert len(st.open_postings()) == 19  # the first posting was analyzed at once
    c2 = make_client(tmp_path, client=client)  # restart: a new app on the same database
    page = c2.get("/postings").text  # second check: ended, collected
    assert "Batch collected – analyzed: 19, without result: 0" in page and "batches running" not in page
    assert len(batches.created) == 19
    with Store(tmp_path / "web.sqlite") as st:
        assert st.open_postings() == [] and st.pending_batches() == []


@pytest.mark.parametrize("split, message, running, sizes", [
    ({}, "1 Inserat analysiert, 19 in 3 Batches zum halben Preis abgeschickt.",
     "3 Batches laufen – Stand wird alle 30 s geprüft", [7, 6, 6]),
    ({"batch_single_first": 0, "batch_groups": 1}, "Batch abgeschickt: 20 Inserate zum halben Preis. Diese Seite "
     "holt das Ergebnis selbst ab.", "Batch läuft – Stand wird alle 30 s geprüft", [20]),
    ({"batch_single_first": 0}, "20 Inserate in 3 Batches zum halben Preis abgeschickt. Diese Seite holt die "
     "Ergebnisse selbst ab.", "3 Batches laufen – Stand wird alle 30 s geprüft", [7, 7, 6]),
])
def test_batches_are_submitted_from_twenty_postings_and_the_page_says_so(tmp_path, split, message, running, sizes):
    batches = FakeBatchClient(results=results_for(twenty_texts), statuses=["in_progress"] * 3)
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client, settings=load_settings().model_copy(update=split))
    add_postings(c, twenty_texts)
    c.post("/postings/analyze", data={"confirm": "1"}, headers=DE, follow_redirects=False)
    assert c.app.state.skillpose.tasks.current.message == message
    assert len(client.calls) == 20 - sum(sizes)  # one single call per posting outside the batches
    page = c.get("/postings", headers=DE).text
    assert running in page
    assert page.count("im Batch") == sum(sizes) and "Offene analysieren (0" not in page
    with Store(tmp_path / "web.sqlite") as st:
        assert st.open_postings() and [len(h) for _, h in st.pending_batches()] == sizes


def test_postings_in_a_batch_are_not_analyzed_again(tmp_path):
    batches = FakeBatchClient(results=results_for(twenty_texts), statuses=["in_progress"] * 5)
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    add_postings(c, twenty_texts)
    c.post("/postings/analyze", data={"confirm": "1"})
    paste(c, OTHER)
    page = c.get("/postings").text
    assert "Analyze open (1," in page  # the first is analyzed and the 19 in the batches are paid already
    c.post("/postings/analyze", data={"confirm": "1"})
    assert [x.path for x in client.calls] == ["JobDraft"] * 2 and len(batches.created) == 19


def test_collected_batch_is_matched_with_the_cv(tmp_path):
    batches = FakeBatchClient(results=results_for(twenty_texts), statuses=["ended"] * 3)
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    cv_with_profile(tmp_path)
    add_postings(c, twenty_texts)
    c.post("/postings/analyze", data={"confirm": "1"})
    page = c.get("/postings").text
    task = c.app.state.skillpose.tasks.current
    assert task.name == "collect" and task.target == "/postings" and task.ok
    assert task.message == "Batch collected – analyzed: 19, without result: 0"  # the first came at once
    assert "Batch collected – analyzed: 19" in page and "data-refresh" not in page
    assert "Batch running" not in page and "batches running" not in page
    with Store(tmp_path / "web.sqlite") as st:
        assert len(st.matches(1)) == 20
    assert len(re.findall(r'href="/cv/1/matches/', c.get("/cv/1/matches").text)) == 20


def test_a_batch_posting_without_a_result_keeps_its_reason(tmp_path):
    results = results_for(twenty_texts)
    results[text_hash(twenty_texts[3])] = ("errored", None)
    batches = FakeBatchClient(results=results, statuses=["ended"] * 3)
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    add_postings(c, twenty_texts)
    c.post("/postings/analyze", data={"confirm": "1"})
    page = c.get("/postings").text
    assert "analyzed: 18, without result: 1" in page and "no result in batch" in row(page, "jobs.json#4")
    with Store(tmp_path / "web.sqlite") as st:
        assert st.pending_batches() == [] and len(st.open_postings()) == 1


def test_a_lost_batch_is_given_up_and_its_postings_are_open_again(tmp_path):
    # the id of another key or workspace (404), or results older than 29 days: the check must not fail for ever
    batches = FakeBatchClient(results={}, statuses=[], retrieve_error=not_found())
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    pending_batch(tmp_path)
    page = c.get("/postings").text
    task = c.app.state.skillpose.tasks.current
    assert task.name == "collect" and task.ok and task.message == "Batch collected – analyzed: 0, without result: 1"
    lost = row(page, "pending.md")
    assert "batch batch_1 lost" in lost and "open" in lost and "Batch running" not in page
    assert "Analyze open (1," in page
    with Store(tmp_path / "web.sqlite") as st:
        assert st.pending_batches() == [] and len(st.open_postings()) == 1
    c.get("/postings")
    assert c.app.state.skillpose.tasks.current is task  # nothing is in a batch any more: no new check


def test_a_batch_that_has_not_ended_changes_nothing_and_leaves_no_trace(tmp_path):
    asked = []
    c = make_client(tmp_path, client=waiting_client(asked))
    pending_batch(tmp_path)
    page = c.get("/postings").text
    assert asked == ["batch_1"]  # the visit asked
    assert c.app.state.skillpose.tasks.current is None  # a check that finds nothing is no result
    assert "Batch running – status is checked every 30 s" in page and 'class="msg ok"' not in page
    assert "Collect batch" not in page
    with Store(tmp_path / "web.sqlite") as st:
        assert len(st.pending_batches()) == 1


@pytest.mark.parametrize("headers, message", [
    ({}, "Batch collected – analyzed: 3, without result: 0; the page checks the other batches again on the next "
         "visit (error: status 500)"),
    (DE, "Batch abgeholt – analysiert: 3, ohne Ergebnis: 0; die übrigen Batches prüft die Seite beim nächsten "
         "Besuch wieder (Fehler: status 500)"),
])
def test_a_batch_taken_before_an_api_error_is_matched_and_keeps_its_proposals(tmp_path, headers, message):
    # batch 1 has ended, then the status request of batch 2 fails (500, 529, no connection).
    # Batch 1 is taken and finished before the error, so the next check no longer sees it: its matches and the
    # skills Claude proposed for its postings come in this check, and the task line says that it was collected.
    first = [f"First batch {i}\nSelenium and Java required." for i in range(3)]
    draft = JobDraft(title="Test Automation Engineer", role_family="test_automation", seniority="mid",
                     requirements=[req("selenium"), req("java"), req("zz_brand_new_skill")])
    batches = FakeBatchClient(results={text_hash(text): ("succeeded", draft.model_dump_json()) for text in first},
                              statuses={"batch_1": ["ended"]}, retrieve_error={"batch_2": api_error(500)})
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.save_batch("batch_1", [st.save_posting(text, f"first{i}.md") for i, text in enumerate(first)])
        st.save_batch("batch_2", [st.save_posting("Second batch\nSQL required.", "second.md")])
    page = c.get("/postings", headers=headers).text
    task = c.app.state.skillpose.tasks.current
    assert batches.retrieved == ["batch_1", "batch_2"]
    assert task.name == "collect" and task.ok and task.message == message
    assert str(escape(message)) in page
    with Store(tmp_path / "web.sqlite") as st:
        assert [batch_id for batch_id, _ in st.pending_batches()] == ["batch_2"]
        assert len(st.matches(1)) == 3
        assert "zz_brand_new_skill" in [skill.id for skill in st.added_skills()]
    # the next check takes nothing and fails again: the error alone, as before, and the matches stay
    c.get("/postings", headers=headers)
    task = c.app.state.skillpose.tasks.current
    assert not task.ok and task.message == "status 500" and batches.retrieved[2:] == ["batch_2"]
    with Store(tmp_path / "web.sqlite") as st:
        assert len(st.matches(1)) == 3 and [batch_id for batch_id, _ in st.pending_batches()] == ["batch_2"]


def test_a_batch_without_counts_among_several_says_that_it_has_no_status_yet(tmp_path):
    # among several open batches, a status answer without request counts keeps the place of its batch in the list
    client, _ = split_client(thirty_texts, ["in_progress"] * 6,
                             counts={"batch_1": {"processing": 10}, "batch_3": {"processing": 3, "succeeded": 6}})
    c = make_client(tmp_path, client=client)
    add_postings(c, thirty_texts)
    c.post("/postings/analyze", data={"confirm": "1"}, follow_redirects=False)
    page = plain(c.get("/postings").text)
    assert f"Batch 1 of 3: 0 of 10 done (as of {checked(c, 'batch_1')})" in page
    assert "Batch 2 of 3: no status yet" in page
    assert f"Batch 3 of 3: 6 of 9 done (as of {checked(c, 'batch_3')})" in page
    de = plain(c.get("/postings", headers=DE).text)
    assert "Batch 2 von 3: noch kein Stand" in de and "Batch 1 von 3: 0 von 10 fertig" in de


# --- progress of the task line (progress while matching) ---

PLAYWRIGHT_TEXTS = [f"Playwright posting {i}\nSelenium, Playwright and Cypress required." for i in range(3)]


def ended_batch_with_two_new_skills(tmp_path, **client_args):
    """CV 1 with a profile (selenium, java) and batch_1 of an earlier visit, ended, whose three postings ask for
    playwright and cypress: matching them needs two relation checks."""
    draft = JobDraft(title="Test Automation Engineer", role_family="test_automation", seniority="mid",
                     requirements=[req("selenium"), req("playwright"), req("cypress")])
    client = ScriptedClient(**client_args)
    batches = FakeBatchClient(results={text_hash(t): ("succeeded", draft.model_dump_json()) for t in PLAYWRIGHT_TEXTS},
                              statuses=["ended"])
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.save_batch("batch_1", [st.save_posting(text, f"p{i}.md") for i, text in enumerate(PLAYWRIGHT_TEXTS)])
    return c, client


@pytest.mark.parametrize("headers, phases, label", [
    ({}, ["Saving results", "Matching with the CV: 0 of 2 checked", "Matching with the CV: 1 of 2 checked",
          "Matching with the CV: 2 of 2 checked", "Saving matches"], "Collect and match batch"),
    (DE, ["Ergebnisse speichern", "Abgleich mit Lebenslauf: 0 von 2 geprüft",
          "Abgleich mit Lebenslauf: 1 von 2 geprüft", "Abgleich mit Lebenslauf: 2 von 2 geprüft",
          "Abgleich speichern"], "Batch abholen und abgleichen"),
])
def test_the_collect_task_reports_saving_matching_and_saving_the_matches(tmp_path, headers, phases, label):
    c, _ = ended_batch_with_two_new_skills(tmp_path)
    seen = record_progress(c)
    page = c.get("/postings", headers=headers).text
    task = c.app.state.skillpose.tasks.current
    assert seen == phases and task.name == "collect" and task.ok and task.progress == ""
    assert label in page
    with Store(tmp_path / "web.sqlite") as st:
        assert len(st.matches(1)) == 3


def test_the_task_line_shows_how_far_the_matching_of_a_collected_batch_is(tmp_path):
    gate = threading.Event()
    c, client = ended_batch_with_two_new_skills(tmp_path, relation_gate=gate)
    runner = c.app.state.skillpose.tasks = TaskRunner()  # threads, as in the running app
    try:
        c.get("/postings")
        assert client.relation_started.wait(5)  # the first check runs alone and holds
        line = plain(re.search(r'<p class="task running".*?</p>', c.get("/costs").text, re.S).group(0))
        assert line.endswith("Collect and match batch : Matching with the CV: 0 of 2 checked")
    finally:
        gate.set()
        deadline = time.monotonic() + 5
        while runner.running and time.monotonic() < deadline:
            time.sleep(0.01)
    assert runner.current.ok and "Matching with" not in c.get("/costs").text


def test_the_postings_task_reports_the_matching_of_its_first_result(tmp_path):
    c = make_client(tmp_path, client=ScriptedClient(extra_requirement="playwright"))
    cv_with_profile(tmp_path)
    paste(c, JOB)
    seen = record_progress(c)
    c.post("/postings/analyze", data={"confirm": "1"}, headers=DE)
    # kubernetes (nice) and playwright are missing: two checks
    assert seen == ["Abgleich mit Lebenslauf: 0 von 2 geprüft", "Abgleich mit Lebenslauf: 1 von 2 geprüft",
                    "Abgleich mit Lebenslauf: 2 von 2 geprüft", "Abgleich speichern"]


def test_checks_do_not_wipe_the_message_of_the_last_task(tmp_path):
    # after the batches went out, the page checks them again and again; the message of the click must stay readable
    batches = FakeBatchClient(results=results_for(twenty_texts), statuses=["in_progress"] * 11)
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    add_postings(c, twenty_texts)
    c.post("/postings/analyze", data={"confirm": "1"}, follow_redirects=False)
    for _ in range(3):
        page = c.get("/postings").text
        # task line and next to the button
        assert page.count("1 posting analyzed, 19 sent in 3 batches at half price.") == 2
    assert len(batches._statuses) == 2  # three checks of three batches were made


def test_visiting_postings_collects_only_when_nothing_else_runs(tmp_path):
    batches = FakeBatchClient(results={}, statuses=[])  # a status request would raise
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    pending_batch(tmp_path)
    with task_running(c, "cv", "/cv/1") as runner:
        assert "Batch running" in c.get("/postings").text
        assert runner.current.name == "cv"


def test_visiting_postings_never_collects_without_a_key(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")   # the demo
    pending_batch(tmp_path)
    with Store(c.app.state.skillpose.demo_db) as demo:
        demo.save_batch("batch_demo", [demo.postings()[0].hash])
    page = c.get("/postings").text
    assert c.app.state.skillpose.tasks.current is None and "data-refresh" not in page
    (tmp_path / "k").mkdir()
    pending_batch(tmp_path / "k")
    no_key = make_client(tmp_path / "k", key=False)
    page = no_key.get("/postings").text
    assert no_key.app.state.skillpose.tasks.current is None and "data-refresh" not in page
    assert "No API key set" in page and "<form" not in page


def test_a_page_of_another_site_cannot_start_the_collection(tmp_path):
    asked = []
    c = make_client(tmp_path, client=waiting_client(asked))
    pending_batch(tmp_path)
    page = c.get("/postings", headers={"sec-fetch-site": "cross-site"}).text  # e.g. an <img> on a foreign page
    assert asked == [] and "Batch running" in page
    assert c.get("/postings", headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert asked == ["batch_1"]


@pytest.mark.parametrize("headers, message", [
    (DE, "1 Inserat analysiert, 29 in 3 Batches zum halben Preis abgeschickt."),
    ({}, "1 posting analyzed, 29 sent in 3 batches at half price."),
])
def test_thirty_postings_give_one_result_at_once_and_go_in_three_batches(tmp_path, headers, message):
    # one posting at once, the rest in three batches - the first result about a minute after the click
    client, batches = split_client(thirty_texts, ["in_progress"] * 3)
    c = make_client(tmp_path, client=client)
    cv_with_profile(tmp_path)
    add_postings(c, thirty_texts)
    c.post("/postings/analyze", data={"confirm": "1"}, headers=headers, follow_redirects=False)
    task = c.app.state.skillpose.tasks.current
    assert task.name == "postings" and task.ok and task.message == message
    # the batches went out first (their clock starts), then the single call
    sent = [x for x in client.calls if x.path in ("batches.create", "JobDraft")]
    assert [x.path for x in sent] == ["batches.create"] * 3 + ["JobDraft"]
    assert [x.kwargs["requests"] for x in sent[:3]] == [10, 10, 9] and batches.retrieved == []
    first = text_hash(thirty_texts[0])  # the first posting as stored
    with Store(tmp_path / "web.sqlite") as st:
        assert st.job_profile(first) is not None and [m.text_hash for m in st.matches(1)] == [first]
        assert [len(h) for _, h in st.pending_batches()] == [10, 10, 9] and len(st.open_postings()) == 29
    assert len(re.findall(r'href="/cv/1/matches/', c.get("/cv/1/matches").text)) == 1  # readable now


def test_the_confirmation_prices_one_single_call_and_three_batches(tmp_path):
    c = make_client(tmp_path, settings=ASK_ALWAYS)
    add_postings(c, thirty_texts)
    page = c.post("/postings/analyze").text
    cost = estimate_usd(ASK_ALWAYS, "job", 1) + sum(estimate_usd(ASK_ALWAYS, "job", n, batch=True) for n in (10, 10, 9))
    assert f"Open postings: 30. This analysis costs about {fmt_usd(cost, 'en')} USD." in page


def test_the_page_shows_one_progress_line_per_open_batch_in_the_order_they_went_out(tmp_path):
    counts = {"batch_1": {"processing": 6, "succeeded": 4}, "batch_2": {"processing": 10},
              "batch_3": {"processing": 1, "succeeded": 6, "errored": 1, "expired": 1}}
    client, batches = split_client(thirty_texts, {"batch_1": ["in_progress"] * 3,
                                                  "batch_2": ["in_progress", "in_progress", "ended"],
                                                  "batch_3": ["in_progress"] * 3}, counts=counts)
    c = make_client(tmp_path, client=client)
    add_postings(c, thirty_texts)
    c.post("/postings/analyze", data={"confirm": "1"}, follow_redirects=False)
    page = plain(c.get("/postings").text)  # the visit checks the batches (inline in tests), then shows the result
    one, two, three = (checked(c, f"batch_{i}") for i in (1, 2, 3))
    assert f"Batch 1 of 3: 4 of 10 done (as of {one})" in page and f"Batch 2 of 3: 0 of 10 done (as of {two})" in page
    assert f"Batch 3 of 3: 8 of 9 done (as of {three}) – 2 failed" in page
    assert page.index("Batch 1 of 3") < page.index("Batch 2 of 3") < page.index("Batch 3 of 3")
    assert "status is checked every 30 s" not in page
    de = plain(c.get("/postings", headers=DE).text)
    one, two, three = (checked(c, f"batch_{i}") for i in (1, 2, 3))
    assert f"Batch 1 von 3: 4 von 10 fertig (Stand {one})" in de
    assert f"Batch 2 von 3: 0 von 10 fertig (Stand {two})" in de
    assert f"Batch 3 von 3: 8 von 9 fertig (Stand {three}) – 2 fehlgeschlagen" in de
    # batch 2 ends first: it is collected on its own, and the two open batches are numbered anew
    page = plain(c.get("/postings").text)
    assert "Batch collected – analyzed: 10, without result: 0" in page
    one, three = checked(c, "batch_1"), checked(c, "batch_3")
    assert f"Batch 1 of 2: 4 of 10 done (as of {one})" in page and f"Batch 2 of 2: 8 of 9 done (as of {three})" in page
    assert "Batch 1 of 3" not in page and "Batch 3 of 3" not in page
    assert set(c.app.state.skillpose.batch_progress) == {"batch_1", "batch_3"}  # the collected one is dropped
    with Store(tmp_path / "web.sqlite") as st:
        assert [batch_id for batch_id, _ in st.pending_batches()] == ["batch_1", "batch_3"]
    assert batches.retrieved == ["batch_1", "batch_2", "batch_3"] * 3  # one status request per batch and check


def test_before_the_first_check_the_page_shows_the_plain_line_then_the_counts(tmp_path):
    entered, gate = threading.Event(), threading.Event()

    def retrieve(batch_id):
        entered.set()
        gate.wait(5)
        counts = SimpleNamespace(processing=2, succeeded=1, errored=1, canceled=0, expired=0)
        return SimpleNamespace(id=batch_id, processing_status="in_progress", request_counts=counts)

    client = ScriptedClient()
    client.messages.batches = SimpleNamespace(retrieve=retrieve)
    c = make_client(tmp_path, client=client)
    runner = c.app.state.skillpose.tasks = TaskRunner()  # threads, as in the running app
    pending_four(tmp_path)
    try:
        first = c.get("/postings").text  # starts the first check; the page does not wait for it
        assert entered.wait(5) and "Batch running – status is checked every 30 s" in first and "of 4 done" not in first
    finally:
        gate.set()
        deadline = time.monotonic() + 5
        while runner.running and time.monotonic() < deadline:
            time.sleep(0.01)
    page = plain(c.get("/postings").text)
    wait_idle(c)
    assert f"Batch running: 2 of 4 done (as of {checked(c, 'batch_1')}) – 1 failed" in page
    assert "status is checked every 30 s" not in page


def test_one_open_batch_shows_its_counts_in_german(tmp_path):
    client = ScriptedClient()
    batches = FakeBatchClient(results={}, statuses=["in_progress"],
                              counts={"batch_1": {"processing": 1, "succeeded": 2, "expired": 1}})
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    pending_four(tmp_path)
    page = plain(c.get("/postings", headers=DE).text)
    assert f"Batch läuft: 3 von 4 fertig (Stand {checked(c, 'batch_1')}) – 1 fehlgeschlagen" in page
    assert "Batch läuft – Stand wird" not in page and batches.retrieved == ["batch_1"]


def test_the_progress_needs_no_status_request_of_its_own(tmp_path):
    client = ScriptedClient()
    batches = FakeBatchClient(results={}, statuses=["in_progress"] * 2, counts={"batch_1": {"processing": 1}})
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    pending_four(tmp_path)
    c.get("/postings")
    assert batches.retrieved == ["batch_1"]  # the check that the visit starts anyway
    with task_running(c, "cv", "/cv/1"):  # another task runs: no check, the page shows what the last one found
        page = plain(c.get("/postings").text)
        assert batches.retrieved == ["batch_1"] and "Batch running: 3 of 4 done (as of" in page
    c.get("/postings")
    wait_idle(c)  # the runner of task_running runs the check in a thread
    assert batches.retrieved == ["batch_1", "batch_1"]


def test_the_demo_never_shows_batch_progress(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    pending_four(tmp_path)
    with Store(c.app.state.skillpose.demo_db) as demo:
        demo.save_batch("batch_demo", [demo.postings()[0].hash])
    now = datetime.now(UTC)
    c.app.state.skillpose.batch_progress.update({"batch_1": BatchProgress("batch_1", 4, 2, 1, now),
                                                  "batch_demo": BatchProgress("batch_demo", 1, 1, 0, now)})
    page = plain(c.get("/postings").text)
    assert "of 4 done" not in page and "of 1 done" not in page and "Batch running" not in page
    assert "1 failed" not in page


def test_collect_task_opens_its_own_client(tmp_path):
    batches = FakeBatchClient(results={}, statuses=["in_progress"])
    client, made = ScriptedClient(), []
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path)
    c.app.state.skillpose.config.client_factory = lambda: made.append(1) or client
    pending_batch(tmp_path)
    c.get("/postings")
    assert made == [1]


# --- reload while a batch is open ---

def test_the_page_asks_for_a_reload_only_while_a_batch_is_open(tmp_path):
    batches = FakeBatchClient(results={}, statuses=["in_progress", "in_progress"])
    client = ScriptedClient()
    client.messages.batches = batches.messages.batches
    c = make_client(tmp_path, client=client)
    quiet = c.get("/postings").text
    assert "data-refresh" not in quiet and REFRESH not in quiet
    pending_batch(tmp_path)
    page = c.get("/postings").text
    assert '<body data-refresh="30">' in page and REFRESH not in page  # a form page: never the meta reload
    assert "<textarea" in page and 'type="file"' in page


def test_the_check_of_a_batch_does_not_make_the_page_reload_every_3_seconds(tmp_path):
    # the visit itself starts the task collect; if that counted as "a task runs", the page would reload after 3 s, and
    # the reload would start the next check, for ever
    entered, gate = threading.Event(), threading.Event()

    def retrieve(batch_id):
        entered.set()
        gate.wait(5)
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    client = ScriptedClient()
    client.messages.batches = SimpleNamespace(retrieve=retrieve)
    c = make_client(tmp_path, client=client)
    runner = c.app.state.skillpose.tasks = TaskRunner()  # threads, as in the running app
    pending_batch(tmp_path)
    try:
        first = c.get("/postings").text
        assert entered.wait(5) and runner.running
        assert '<body data-refresh="30">' in first
        assert '<body data-refresh="3">' in c.get("/postings").text  # a later visit sees a task that is still running
    finally:
        gate.set()
        deadline = time.monotonic() + 5
        while runner.running and time.monotonic() < deadline:
            time.sleep(0.01)
    assert '<body data-refresh="30">' in c.get("/postings").text


def test_the_page_reloads_faster_while_a_task_runs_but_not_after_a_post(tmp_path):
    c = make_client(tmp_path, settings=ASK_ALWAYS)
    paste(c, JOB)
    with task_running(c, "cv", "/cv/1"):
        page = c.get("/postings").text
        assert '<body data-refresh="3">' in page and REFRESH not in page and "running since" in page
        confirm = c.post("/postings/analyze", data={})
        refused = c.post("/postings/text", data={"text": " "})
        bad = upload(c, ("a.exe", b"x"))
        for answer in (confirm, refused, bad):
            assert "running since" in answer.text and "data-refresh" not in answer.text
            assert REFRESH not in answer.text
    assert "data-refresh" not in c.get("/postings").text


def test_the_script_reloads_only_when_no_field_holds_input():
    script = (Path(web_init).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "dataset.refresh" in script and "location.reload" in script
    assert "document.activeElement" in script and "defaultValue" in script and "files.length" in script
    assert "eval(" not in script and "innerHTML" not in script


# --- matches ---

def test_matches_are_sorted_and_show_coverage_and_the_three_main_gaps(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_match(st, cv_id, "Beta tier two", 2, 0.6, [
            ("test_automation", "must", "gap", "months", "not in the CV"),
            ("selenium", "nice", "gap", "weeks", "not in the CV"),
            ("docker", "must", "partial", "weeks", "only listed, no evidence of use"),
            ("kubernetes", "nice", "partial", "weeks", "related: docker"),
            ("java", "must", "met", None, "evidenced"),
            ("python", "must", "met", None, "evidenced")], family="qa_management")
        seed_match(st, cv_id, "Alpha tier two", 2, 0.6, [("java", "must", "met", None, "evidenced")])
        seed_match(st, cv_id, "Gamma tier one", 1, 0.9, [("java", "must", "met", None, "evidenced")])
        seed_match(st, cv_id, "Delta tier one", 1, 1.0, [("java", "must", "met", None, "evidenced")])
        seed_match(st, cv_id, "Omega tier three", 3, 0.9, [("java", "must", "met", None, "evidenced")])
    page = c.get("/cv/1/matches").text
    titles = re.findall(r'<a href="/cv/1/matches/[0-9a-f]+">([^<]+)</a>', page)
    assert titles == ["Delta tier one", "Gamma tier one", "Alpha tier two", "Beta tier two", "Omega tier three"]
    beta = cells(row(page, "Beta tier two"))
    assert beta[1] == "QA management" and beta[2] == "2 · learnable" and beta[3] == "60 %" and beta[4] == "50 %"
    assert beta[5] == "Test automation, Docker, Selenium"  # must gap, must partial, then nice gap; at most three
    assert "Kubernetes" not in beta[5]
    de = cells(row(c.get("/cv/1/matches", headers=DE).text, "Beta tier two"))
    assert de[1] == "QA-Management" and de[2] == "2 · lernbar" and de[5] == "Testautomatisierung, Docker, Selenium"
    assert cells(row(page, "Delta tier one"))[5] == "–"  # no gaps: a dash, not an empty cell
    for tier in (1, 2, 3):
        assert f'class="mark tier-{tier}"' in page


def test_nice_coverage_shows_dash_without_nice_requirements(tmp_path):
    # The matcher computes the must coverage over all requirements when a posting has no must requirement (tier and
    # sort order use that number, so the must column always shows it), and then nice coverage only repeats it. The
    # numbers below are the ones match_job would store.
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_match(st, cv_id, "Only must", 1, 1.0, [("java", "must", "met", None, "evidenced"),
                                                      ("selenium", "must", "met", None, "evidenced")],
                   nice_coverage=1.0)  # no nice requirement: the coverage of nothing is 1.0
        seed_match(st, cv_id, "Only nice", 2, 0.5, [("java", "nice", "met", None, "evidenced"),
                                                      ("selenium", "nice", "gap", "weeks", "not in the CV")],
                   nice_coverage=0.5)  # no must requirement: both numbers count the same requirements
        seed_match(st, cv_id, "Both", 1, 1.0, [("java", "must", "met", None, "evidenced"),
                                                 ("selenium", "nice", "gap", "weeks", "not in the CV")],
                   nice_coverage=0.0)
    page = c.get("/cv/1/matches").text
    only_must, only_nice, both = (cells(row(page, t)) for t in ("Only must", "Only nice", "Both"))
    assert (only_must[3], only_must[4]) == ("100 %", "–")
    assert (only_nice[3], only_nice[4]) == ("50 %", "–")  # must shows the number that drives tier and order
    assert (both[3], both[4]) == ("100 %", "0 %")  # a real 0 % is not a dash
    assert "A dash under “Nice to have” means" in page
    de = c.get("/cv/1/matches", headers=DE).text
    assert "Ein Strich bei „Kann“ heißt" in de
    link = re.search(r'href="(/cv/1/matches/[0-9a-f]+)">Only nice', page).group(1)
    assert "Must: 50 % · Nice to have: –" in plain(c.get(link).text)  # the detail page follows the same rule


def test_matches_page_states(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.save_cv("none.md", CV)
    page = c.get("/cv/1/matches").text
    assert "This CV has not been analyzed yet." in page and 'href="/cv/1/text"' in page
    cv_with_profile(tmp_path)
    page = c.get("/cv/2/matches", headers=DE).text
    assert "Noch kein Abgleich" in page and 'href="/postings"' in page
    for link in ["/cv/2", "/cv/2/matches", "/cv/2/market", "/cv/2/paths"]:
        assert f'href="{link}"' in page


def test_unknown_cv_or_posting_is_a_404(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_match(st, cv_id, "Known", 1, 1.0, [("java", "must", "met", None, "evidenced")])
    assert c.get(f"/cv/{cv_id}/matches/{h}").status_code == 200
    for path in ["/cv/99/matches", f"/cv/99/matches/{h}", f"/cv/{cv_id}/matches/nosuchhash"]:
        r = c.get(path, headers=DE)
        assert r.status_code == 404 and "Diese Seite gibt es nicht." in r.text, path
    with Store(tmp_path / "web.sqlite") as st:
        other = st.save_posting("Not matched\nyet", "n.md")
    assert c.get(f"/cv/{cv_id}/matches/{other}").status_code == 404


def test_match_detail_shows_lights_and_evidence(tmp_path):
    api = ScriptedClient(extra_requirement="zz_unknown_skill")
    c = make_client(tmp_path, client=api)
    cv_with_profile(tmp_path)
    paste(c, JOB)
    c.post("/postings/analyze", data={"confirm": "1"})
    link = re.search(r'href="(/cv/1/matches/[0-9a-f]+)"', c.get("/cv/1/matches").text).group(1)
    page = c.get(link).text
    assert page.count("status-met") == 2 and page.count("status-gap") == 2 and "status-partial" not in page
    assert re.search(r'class="mark status-met">\s*met\s*<', page)
    assert re.search(r'class="mark status-gap">\s*gap\s*<', page)
    selenium = page[page.index('id="req-selenium"'):]
    selenium = selenium[:selenium.index("</li>")]
    assert "Selenium" in selenium and "must" in selenium and QUOTE in selenium and "In the CV" in selenium
    assert "In the posting" in selenium and "<q>selenium</q>" in selenium and "evidenced" in selenium
    kube = page[page.index('id="req-kubernetes"'):]
    kube = kube[:kube.index("</li>")]
    assert "nice to have" in kube and "not in the CV" in kube and "no evidence in the CV" in kube
    assert "Gap: months" in kube and "about 12 weeks" in kube
    assert "zz_unknown_skill" in page  # a skill the vocabulary does not know shows as its id
    assert page.index('id="req-zz_unknown_skill"') < page.index('id="req-selenium"') < page.index('id="req-kubernetes"')
    assert 'href="/cv/1/matches"' in page and "2 · learnable" in page and "Must: 67 %" in plain(page)
    de = c.get(link, headers=DE).text
    assert "erfüllt" in de and "Lücke" in de and "belegt" in de and "nicht im Lebenslauf" in de
    assert "Muss" in de and "Kann" in de and "Lücke: Monate" in de and "ca. 12 Wochen" in de
    assert "Beleg im Lebenslauf" in de and "Im Inserat" in de


def test_match_detail_marks_partial_and_levels_in_the_ui_language(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_match(st, cv_id, "Partial one", 2, 0.5, [
            ("docker", "must", "partial", "weeks", "level basic, working required; last used 2012"),
            ("java", "must", "met", None, "evidenced")])
    en, de = c.get(f"/cv/1/matches/{h}").text, c.get(f"/cv/1/matches/{h}", headers=DE).text
    assert "status-partial" in en and "level basic, working required; last used 2012" in en
    assert "teilweise" in de and "Niveau Grundkenntnisse, verlangt Praxiswissen; zuletzt genutzt 2012" in de
    assert en.index('id="req-docker"') < en.index('id="req-java"')  # open requirements first


NOT_COUNTED_ROWS = [
    ("teamwork", "must", "unverifiable", None, "soft skill: not verifiable from a CV"),
    ("driving_licence_b", "must", "unanswered", None, "not answered in the profile"),
    ("java", "must", "met", None, "evidenced"),
    ("docker", "must", "partial", "weeks", "only listed, no evidence of use"),
    ("kubernetes", "must", "gap", "months", "not in the CV")]
NOT_COUNTED_FIELDS = {"teamwork": {"kind": "soft"},
                      "driving_licence_b": {"kind": "condition", "condition": "driving_licence_b"}}


def test_the_match_pages_show_soft_skills_and_unanswered_conditions_without_a_gap_colour(tmp_path):
    """A soft skill is not verifiable from a CV, a condition without an answer is unanswered; both
    count in no sum, so the list of gaps leaves them out. The page of the match has a block for each of them."""
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_match(st, cv_id, "Soft and conditions", 2, 0.5, NOT_COUNTED_ROWS,
                       requirement_fields=NOT_COUNTED_FIELDS)
    listing = cells(row(c.get("/cv/1/matches").text, "Soft and conditions"))
    assert listing[5] == "Kubernetes, Docker"
    en, de = c.get(f"/cv/1/matches/{h}").text, c.get(f"/cv/1/matches/{h}", headers=DE).text
    assert re.search(r'class="mark status-unanswered">\s*unanswered\s*<', en)
    assert re.search(r'class="mark status-unanswered">\s*unbeantwortet\s*<', de)
    assert "status-unverifiable" not in en  # a soft skill has no status of its own on the page: its block says it
    assert "im Profil nicht beantwortet" in de
    order = [en.index(f'id="req-{skill}"') for skill in ("kubernetes", "docker", "java", "teamwork",
                                                          "driving_licence_b")]
    assert order == sorted(order)  # the requirements first (open ones first), then the soft skills, then the conditions
    css = (Path(web_init).parent / "static" / "style.css").read_text(encoding="utf-8")
    assert "status-unverifiable" not in css and "status-unanswered" not in css  # no colour of met, partial or gap


def test_a_posting_without_requirements_says_so(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_match(st, cv_id, "Empty one", 3, 0.0, [])
    cells_ = cells(row(c.get("/cv/1/matches").text, "Empty one"))
    assert cells_[2] == "3 · big gap" and cells_[3:] == ["0 %", "–", "–"]  # the must column always shows the number
    assert "No requirements were read from this posting." in c.get(f"/cv/1/matches/{h}").text
    assert "keine Anforderungen gelesen" in c.get(f"/cv/1/matches/{h}", headers=DE).text


def test_posting_text_is_escaped_on_the_detail_page(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    evil = "<script>alert(1)</script>"
    with Store(tmp_path / "web.sqlite") as st:
        text = f"{evil} posting"
        h = st.save_posting(text, "evil.md")
        req_ = Requirement(skill_id="zz<b>id", importance="must", min_level=None, min_years=None, kind="tool",
                           quote=f"{evil} quote")
        evidence = Evidence(quote=f"{evil} cv quote", context="<i>ctx</i>", period_from=None, period_to=None,
                            strength="claim", verified=False)
        st.save_job_profile(JobProfile(text_hash=h, title=f"{evil} title", role_family="other", seniority="mid",
                                       requirements=[req_]), "fake")
        st.save_match(cv_id, JobMatch(text_hash=h, tier=1, must_coverage=1.0, nice_coverage=0.0, matches=[
            RequirementMatch(requirement=req_, status="met", reason=f"{evil} reason", evidence=evidence,
                             gap_type=None, effort_weeks=None)]))
    for page in (c.get("/cv/1/matches").text, c.get(f"/cv/1/matches/{h}").text):
        assert "<script>alert" not in page and page.count("<script") == 1 and "&lt;script&gt;alert(1)" in page
        assert "<b>" not in page and "<i>" not in page
    detail = c.get(f"/cv/1/matches/{h}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; cv quote" in detail and "zz&lt;b&gt;id" in detail


# --- the page of one posting ---

EVIL = "<script>alert(1)</script>"
FULL_TEXT = (f"Senior QA <Engineer>\n\nYou bring:\n  - Selenium & Java (5+ years) {EVIL}\n  - Kubernetes is a plus\n\n"
             'Offer: 4.500 EUR "gross"')
MODEL = "claude-sonnet-5-5"


def seed_full_posting(st, *, text=FULL_TEXT, ref="senior_qa.md"):
    """A posting with a job profile: two must and two nice requirements, one id only Claude proposed, one nobody
    knows."""
    h = st.save_posting(text, ref)
    requirements = [
        Requirement(skill_id="selenium", importance="must", min_level="strong", min_years=5, kind="tool",
                    quote="Selenium und Java, mindestens 5 Jahre"),
        Requirement(skill_id="zz_flink", importance="nice", min_level=None, min_years=None, kind="tool",
                    quote=f"Flink {EVIL} is a plus"),
        Requirement(skill_id="project_management", importance="must", min_level=None, min_years=2.5,
                    kind="experience", quote="Projektleitung"),
        Requirement(skill_id="zz_unknown", importance="nice", min_level="working", min_years=None, kind="soft",
                    quote="team player")]
    proposed = [SkillProposal(id="zz_flink", name="Apache Flink", category="tool")]
    st.save_job_profile(JobProfile(text_hash=h, title="Senior QA <Engineer>", role_family="test_automation",
                                   seniority="senior", requirements=requirements, proposed_skills=proposed), MODEL)
    return h


def requirement_items(page):
    return re.findall(r'<li class="skill".*?</li>', page, re.S)


def requirement_names(page):
    return [re.search(r"<strong>(.*?)</strong>", item).group(1) for item in requirement_items(page)]


def test_posting_page_shows_the_full_text_escaped_with_its_line_breaks(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_full_posting(st)
    page = c.get(f"/postings/{h}").text
    assert f'<div class="posting-text">{escape(FULL_TEXT)}</div>' in page  # nothing around the text: pre-wrap shows it
    assert "\n  - Kubernetes is a plus\n\nOffer" in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert" not in page and "<Engineer>" not in page and page.count("<script") == 1
    css = (Path(web_init).parent / "static" / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.posting-text\s*\{[^}]*white-space:\s*pre-wrap", css)
    assert "<form" not in page and "<h1>Senior QA &lt;Engineer&gt;</h1>" in page
    assert "Full text" in page and "Vollständiger Text" in c.get(f"/postings/{h}", headers=DE).text


def test_posting_page_shows_title_references_role_status_and_time(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_full_posting(st)
        st.save_posting(FULL_TEXT, "second_name.md")  # the same text under another name: one more reference
    page = plain(c.get(f"/postings/{h}").text)
    assert "senior_qa.md, second_name.md" in page and "Test automation" in page and "Senior" in page
    assert "analyzed" in page and re.search(r"Seen on \d\d\.\d\d\.\d{4} \d\d:\d\d", page)
    de = plain(c.get(f"/postings/{h}", headers=DE).text)
    assert "Testautomatisierung" in de and "analysiert" in de and "Gesehen am" in de and "Quellen" in de


def test_posting_page_lists_the_requirements_with_quotes_and_labels(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_full_posting(st)
    page = c.get(f"/postings/{h}").text
    items = requirement_items(page)
    assert requirement_names(page) == ["Selenium", "Project management", "Apache Flink", "zz_unknown"]  # must first
    selenium, project, flink, unknown = (plain(item) for item in items)
    assert "must" in selenium and "Tool" in selenium and "Minimum level: Strong" in selenium
    assert "Minimum years: 5" in selenium and "<q>Selenium und Java, mindestens 5 Jahre</q>" in items[0]
    assert "must" in project and "Experience" in project and "Minimum years: 2.5" in project
    assert "Minimum level" not in project and "Minimum years" not in flink
    assert "nice to have" in flink and "Tool" in flink
    assert "<q>Flink &lt;script&gt;alert(1)&lt;/script&gt; is a plus</q>" in items[2]
    assert "nice to have" in unknown and "Soft skill" in unknown and "Minimum level: Working" in unknown
    assert "zz_flink" not in plain(page) and "<q>team player</q>" in items[3]
    assert "Quotes are verbatim, in the original language." in page
    assert f'data-ai-generated="true" data-ai-model="{MODEL}"' in page and "Created by Claude (AI)" in page
    de = c.get(f"/postings/{h}", headers=DE).text
    items = [plain(item) for item in requirement_items(de)]
    assert requirement_names(de) == ["Selenium", "Projektmanagement", "Apache Flink", "zz_unknown"]
    assert "Muss" in items[0] and "Werkzeug" in items[0] and "Mindestniveau: Vertieftes Wissen" in items[0]
    assert "Mindestjahre: 5" in items[0] and "Mindestjahre: 2,5" in items[1] and "Erfahrung" in items[1]
    assert "Kann" in items[2] and "Persönliche Kompetenz" in items[3]
    assert "Zitate stehen wörtlich in der Originalsprache." in de and "Von Claude (KI) erstellt" in de


def test_posting_page_of_a_posting_without_requirements_says_so(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = st.save_posting(JOB, "empty.md")
        st.save_job_profile(JobProfile(text_hash=h, title="Empty", role_family="other", seniority="mid",
                                       requirements=[]), MODEL)
    page = c.get(f"/postings/{h}").text
    assert "No requirements were read from this posting." in page and not requirement_items(page)
    assert "Selenium and Java required" in page


def test_posting_page_of_an_open_posting_shows_the_text_and_says_it_is_not_analyzed(tmp_path):
    c = make_client(tmp_path)
    paste(c, JOB, ref="open.md")
    with Store(tmp_path / "web.sqlite") as st:
        h = st.open_postings()[0]
        st.set_posting_error(h, f"refusal {EVIL}")
    page = c.get(f"/postings/{h}").text
    assert f'<div class="posting-text">{escape(JOB)}</div>' in page and "<h1>open.md</h1>" in page
    assert "not analyzed yet" in plain(page) and "open" in plain(page) and "failed" in page
    assert "refusal &lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert" not in page
    assert "data-ai-generated" not in page and "Quotes are verbatim" not in page and "Minimum" not in page
    de = c.get(f"/postings/{h}", headers=DE).text
    assert "noch nicht analysiert" in de and "offen" in de and "fehlgeschlagen" in de


def test_posting_page_of_a_posting_in_a_batch(tmp_path):
    c = make_client(tmp_path)
    pending_batch(tmp_path, "Pending posting\nPython")
    with Store(tmp_path / "web.sqlite") as st:
        h = st.postings()[0].hash
    page = c.get(f"/postings/{h}").text
    assert "in batch" in plain(page) and "not analyzed yet" in plain(page)
    assert '<div class="posting-text">Pending posting\nPython</div>' in page


def test_an_unknown_posting_is_a_404(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_full_posting(st)
    for path in ["/postings/nosuchhash", "/postings/" + "0" * 64, "/postings/text", "/postings/%3Cscript%3E"]:
        r = c.get(path, headers=DE)
        assert r.status_code == 404 and "Diese Seite gibt es nicht." in r.text and "<script>" not in r.text, path
        assert c.get(path).status_code == 404 and "This page does not exist." in c.get(path).text


def test_the_postings_list_links_every_row_to_its_page(tmp_path):
    c = make_client(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        done = seed_full_posting(st)
        open_ = st.save_posting(OTHER, "other.md")
    page = c.get("/postings").text
    assert f'<a href="/postings/{done}">Senior QA &lt;Engineer&gt;</a>' in row(page, "senior_qa.md")
    assert f'<a href="/postings/{open_}">other.md</a>' in row(page, "other.md")
    assert f'href="/postings/{open_}"' in c.get("/postings", headers=DE).text


def test_the_match_page_links_to_the_full_posting_and_the_posting_page_back(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_match(st, cv_id, "Matched", 1, 1.0, [("java", "must", "met", None, "evidenced")])
        other = seed_full_posting(st)
    detail = c.get(f"/cv/{cv_id}/matches/{h}").text
    assert f'<a href="/postings/{h}?cv={cv_id}">Show the full posting</a>' in detail
    de_detail = c.get(f"/cv/{cv_id}/matches/{h}", headers=DE).text
    assert f'href="/postings/{h}?cv={cv_id}"' in de_detail and "Ganzes Inserat ansehen" in de_detail
    assert f'href="/postings/{h}' not in c.get(f"/cv/{cv_id}/matches").text  # the table keeps its one link per row
    page = c.get(f"/postings/{h}?cv={cv_id}").text
    assert f'<a href="/cv/{cv_id}/matches/{h}">Back to the match</a>' in page and 'href="/postings"' in page
    assert f'href="/cv/{cv_id}/market"' in page  # the navigation of the CV
    assert "Zurück zum Abgleich" in c.get(f"/postings/{h}?cv={cv_id}", headers=DE).text
    # no CV context, an unknown CV, a garbage value, a CV without a match for this posting: the page works, no back link
    for query in ["", "?cv=99", "?cv=abc", "?cv=", f"?cv={cv_id}&lang=de"]:
        r = c.get(f"/postings/{other}{query}")
        assert r.status_code == 200 and "/matches/" not in r.text, query
    assert c.get(f"/postings/{h}").status_code == 200 and "/matches/" not in c.get(f"/postings/{h}").text


# --- demo and no key ---

def test_demo_postings_and_matches_are_read_only(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    page = c.get("/postings", headers=DE).text
    assert "Test Automation Engineer 0" in page and "<form" not in page
    assert "Kein API-Schlüssel eingetragen" in page and page.count("analysiert") >= 5
    matches = c.get("/cv/1/matches", headers=DE).text
    assert NAME in matches and "1 · passt" in matches and "3 · große Lücke" in matches and "<form" not in matches
    link = re.search(r'href="(/cv/1/matches/[0-9a-f]+)"', matches).group(1)
    detail = c.get(link, headers=DE).text
    assert "Selenium" in detail and "Built a Selenium test automation framework in Java" in detail
    assert c.app.state.skillpose.tasks.current is None
    for path in ["/postings/text", "/postings/upload", "/postings/analyze"]:
        assert c.post(path, data={}).status_code == 403, path


def test_demo_posting_pages_show_text_and_requirements_without_a_time(tmp_path):
    write_fixture_bundles(tmp_path / "demo", bundle_with_proposals())
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    links = re.findall(r'href="(/postings/[0-9a-f]+)"', c.get("/postings").text)
    assert len(links) == 5
    for headers in ({}, DE):
        page = c.get(links[0], headers=headers).text
        text = plain(page)
        assert "Test Automation Engineer" in text and "Selenium required, Kubernetes" in text
        assert "Apache Flink" in text and "apache_flink" not in text and "<form" not in page
        assert not re.search(r"\d\d\.\d\d\.\d{4} \d\d:\d\d", text) and "Seen on" not in text
        assert "Gesehen am" not in text and 'class="posting-text"' in page
        assert "data-ai-generated" in page and "data-ai-model" not in page  # the demo has no real model name
    match = re.search(r'href="(/cv/1/matches/[0-9a-f]+)"', c.get("/cv/1/matches").text).group(1)
    hash_ = match.rsplit("/", 1)[1]
    assert f'href="/postings/{hash_}?cv=1"' in c.get(match).text
    assert f'href="{match}"' in c.get(f"/postings/{hash_}?cv=1").text
    assert c.app.state.skillpose.tasks.current is None
    (tmp_path / "k").mkdir()
    with_key = make_client(tmp_path / "k", demo_dir=tmp_path / "demo")   # with a key: the own data only
    assert with_key.get(links[0]).status_code == 404 and with_key.get(match).status_code == 404


def test_without_a_key_the_postings_page_offers_no_form(tmp_path):
    with Store(tmp_path / "web.sqlite") as st:
        st.save_posting(JOB, "job.md")
    page = make_client(tmp_path, key=False).get("/postings").text
    assert "job.md" in page and "<form" not in page and "No API key set" in page
    assert REFRESH not in page and "data-refresh" not in page


# --- the lists in pages ---

def many(tmp_path, count=78):
    """A client with count stored postings, "Posting 00" the oldest; the list shows them newest first."""
    c = make_client(tmp_path)
    add_postings(c, [f"Posting {i:02d}\nSome text {i}" for i in range(count)])
    return c


def has(page, i):
    """Is posting i of many() on the page?"""
    return f'id="posting-{text_hash(f"Posting {i:02d}" + chr(10) + f"Some text {i}")}"' in page


def shown(page, prefix="posting-"):
    return re.findall(rf'<tr id="{prefix}[0-9a-f]+"', page)


def sizes_of(page):
    """(size, is current) of the size buttons."""
    return [(int(n), 'aria-current="true"' in attrs)
            for attrs, n in re.findall(r'<a class="button secondary pager-size"([^>]*)>(\d+)</a>', page)]


def test_the_list_shows_30_postings_by_default_with_the_size_buttons_and_the_range(tmp_path):
    page = many(tmp_path).get("/postings").text
    assert len(shown(page)) == 30 and "(78 postings)" in page
    assert sizes_of(page) == [(10, False), (30, True), (50, False), (100, False)]
    assert "Per page" in page and "Postings 1–30 of 78" in page
    assert has(page, 77) and has(page, 48) and not has(page, 47)  # newest first


@pytest.mark.parametrize("per, count", [(10, 10), (30, 30), (50, 50), (100, 78)])
def test_each_size_button_sets_the_page_size(tmp_path, per, count):
    c = many(tmp_path)
    page = c.get(f"/postings?per={per}").text
    assert len(shown(page)) == count and (per, True) in sizes_of(page)
    assert f'href="/postings?per={per}&amp;page=1"' in page


def test_page_two_shows_the_next_postings_and_marks_the_current_page(tmp_path):
    c = many(tmp_path)
    page = c.get("/postings?page=2").text
    assert len(shown(page)) == 30 and has(page, 47) and has(page, 18) and not has(page, 17)
    assert "Postings 31–60 of 78" in page
    nav = re.search(r'<nav class="pager-pages" aria-label="Pages">(.*?)</nav>', page, re.S).group(1)
    assert re.findall(r'<a href="[^"]*page=2[^"]*" aria-current="page"', nav)
    assert 'rel="prev"' in nav and 'rel="next"' in nav
    last = c.get("/postings?page=3").text
    assert len(shown(last)) == 18 and "Postings 61–78 of 78" in last and 'rel="next"' not in last


def test_invalid_size_and_page_fall_back_to_30_rows_on_page_one(tmp_path):
    c = many(tmp_path)
    for query in ("per=7", "per=abc", "per=-5", "per=", "page=0", "page=x", "page=-1", "per=99999999999&page=1e9"):
        r = c.get(f"/postings?{query}")
        assert r.status_code == 200, query
        assert len(shown(r.text)) == 30 and "Postings 1–30 of 78" in r.text, query


def test_a_page_past_the_end_shows_the_last_page(tmp_path):
    page = many(tmp_path).get("/postings?page=99").text
    assert len(shown(page)) == 18 and "Postings 61–78 of 78" in page


def test_a_refused_form_keeps_working_page_links(tmp_path):
    c = many(tmp_path, 25)
    r = c.post("/postings/text", data={"text": "", "ref": ""})
    assert r.status_code == 400
    links = re.findall(r'<a [^>]*href="([^"]+)"', r.text)
    pager = [h for h in links if "per=" in h]
    assert pager and all(h.startswith("/postings?") for h in pager)


def test_page_links_keep_the_size_and_other_parameters_and_drop_the_message(tmp_path):
    c = many(tmp_path)
    page = c.get("/postings?per=10&cv=3&msg=busy&lang=en&page=4").text
    assert 'href="/postings?cv=3&amp;per=10&amp;page=5"' in page
    assert "msg=" not in re.search(r'<nav class="pager-pages".*?</nav>', page, re.S).group(0)
    assert 'href="/postings?cv=3&amp;per=50&amp;page=1"' in page  # another size starts at page 1
    nav = re.search(r'<nav class="pager-pages".*?</nav>', page, re.S).group(0)
    assert "…" in nav  # 8 pages: gaps next to the first and the last page


def test_page_numbers_have_gaps_only_where_pages_are_left_out():
    from skillpose.web.paging import Page
    def numbers(number, pages):
        return Page(items=[], total=0, per=10, number=number, pages=pages, first=0, last=0, path="/x").numbers
    assert numbers(1, 3) == [1, 2, 3]
    assert numbers(1, 8) == [1, 2, None, 8]
    assert numbers(4, 8) == [1, 2, 3, 4, 5, None, 8]
    assert numbers(5, 12) == [1, None, 4, 5, 6, None, 12]
    assert numbers(7, 8) == [1, None, 6, 7, 8]
    assert numbers(1, 1) == [1]


def test_the_range_and_the_buttons_in_german(tmp_path):
    c = many(tmp_path)
    page = c.get("/postings?page=2", headers=DE).text
    assert "Inserate 31–60 von 78" in page and "Pro Seite" in page and "(78 Inserate)" in page
    assert 'aria-label="Seiten"' in page and "Zurück" in page and "Weiter" in page


def test_a_short_list_has_no_page_links_and_a_very_short_one_no_size_buttons(tmp_path):
    c = many(tmp_path, 12)
    page = c.get("/postings").text
    assert len(shown(page)) == 12 and "pager-pages" not in page and "Postings 1–" not in page
    assert sizes_of(page)  # more than 10: the size can be chosen
    assert "pager-pages" in c.get("/postings?per=10").text  # but it pages as soon as the size is smaller
    (tmp_path / "few").mkdir()
    assert "pager" not in many(tmp_path / "few", 5).get("/postings").text


def test_a_redirect_that_names_a_posting_shows_the_page_that_holds_it(tmp_path):
    c = many(tmp_path)
    oldest = text_hash("Posting 00\nSome text 0")
    page = c.get(f"/postings?posting={oldest}&msg=no_open").text
    assert f'<tr id="posting-{oldest}"' in page and "Postings 61–78 of 78" in page
    all_in_one = c.get(f"/postings?posting={oldest}&per=100").text
    assert len(shown(all_in_one)) == 78 and "pager-pages" not in all_in_one and "Postings 1–" not in all_in_one
    assert "Postings 1–30 of 78" in c.get("/postings?posting=unknown").text  # an unknown value is ignored
    assert 'href="/postings?per=30&amp;page=1"' in page


def test_the_anchor_of_each_row_is_its_hash(tmp_path):
    c = make_client(tmp_path)
    paste(c, JOB)
    assert f'<tr id="posting-{text_hash(JOB)}"' in c.get("/postings").text


def test_the_matches_of_a_cv_are_paged_the_same_way(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    hashes = []
    with Store(tmp_path / "web.sqlite") as st:
        for i in range(45):
            rows = [("java", "must", "met", None, "ok")]
            hashes.append(seed_match(st, cv_id, f"Match {i:02d}", 1, 1.0 - i / 100, rows))
    page = c.get(f"/cv/{cv_id}/matches").text
    assert len(shown(page)) == 30 and "Matches 1–30 of 45" in page and sizes_of(page)[1] == (30, True)
    second = c.get(f"/cv/{cv_id}/matches?page=2").text
    assert len(shown(second)) == 15 and "Match 44" in second and "Matches 31–45 of 45" in second
    assert f'href="/cv/{cv_id}/matches?per=30&amp;page=1"' in second
    assert "Abgleiche 31–45 von 45" in c.get(f"/cv/{cv_id}/matches?page=2", headers=DE).text
    on_two = c.get(f"/cv/{cv_id}/matches?posting={hashes[40]}").text
    assert f'<tr id="posting-{hashes[40]}"' in on_two
    assert len(shown(c.get(f"/cv/{cv_id}/matches?per=100").text)) == 45


def test_the_task_line_links_to_its_page_only_from_other_pages(tmp_path):
    c = make_client(tmp_path)
    with task_running(c, "postings", "/postings"):
        own = c.get("/postings?per=30").text
        line_own = re.search(r'<p class="task.*?</p>', own, re.S).group(0)
        assert 'href="/postings"' not in line_own and "Analyze" in plain(line_own)
        other = c.get("/").text
        assert 'href="/postings"' in re.search(r'<p class="task.*?</p>', other, re.S).group(0)
