"""Regression tests of cases the owner met in the running app."""

import re
import threading

from fakes import CV, FakeBatchClient, ScriptedClient, req
from webkit import make_client

from skillpose import logs
from skillpose.core.models import CandidateProfile, CandidateSkill, Evidence, JobDraft, text_hash
from skillpose.store.db import Store
from skillpose.web.deps import fmt_time
from skillpose.web.tasks import TaskRunner

DE = {"accept-language": "de"}
QUOTE = "Built a Selenium test automation framework in Java"
POSTING_MARKER = "Geheimprojekt-Kunde-0815"
# ten skills the CVs lack: matching them needs ten relation checks, the first alone, then four at once
NEW_SKILLS = ["playwright", "cypress", "appium", "jmeter", "postman", "robot_framework", "cucumber", "soapui",
              "gatling", "katalon"]


def plain(html):
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def task_line(page):
    return plain(re.search(r'<p class="task.*?</p>', page, re.S).group(0))


def thirty_postings_in_an_ended_batch(tmp_path):
    """30 postings that went out in one batch, which has ended now; each asks for Selenium, Java and three of the new
    skills. Returns the batch results of a FakeBatchClient and the posting texts."""
    texts, results = [], {}
    with Store(tmp_path / "web.sqlite") as st:
        hashes = []
        for i in range(30):
            text = f"Test Automation Engineer {i}\nSelenium, Java and more, for {POSTING_MARKER}."
            hashes.append(st.save_posting(text, f"posting_{i}.md"))
            skills = [NEW_SKILLS[(i + k) % len(NEW_SKILLS)] for k in range(3)]
            draft = JobDraft(title=f"Test Automation Engineer {i}", role_family="test_automation", seniority="mid",
                             requirements=[req("selenium"), req("java"), *(req(s, "nice") for s in skills)])
            results[text_hash(text)] = ("succeeded", draft.model_dump_json())
            texts.append(text)
        st.save_batch("batch_1", hashes)
    return results, texts


def analyzed_cv(tmp_path):
    """CV 1 with a profile (Selenium and Java), as after an earlier analysis."""
    evidence = Evidence(quote=QUOTE, context="Acme", period_from=2016, period_to=None, strength="outcome",
                        verified=True)
    skills = [CandidateSkill(skill_id=s, level="expert", years=10, last_used=2026, evidence=[evidence])
              for s in ("selenium", "java")]
    with Store(tmp_path / "web.sqlite") as st:
        cv_id = st.save_cv("first_cv.md", CV)
        st.save_profile(cv_id, CandidateProfile(language="en", skills=skills, stations=[]), "fake")


def test_cv_analysis_during_batch_collect_is_queued_and_runs(tmp_path):
    # after the 30-posting batch the task "Batch abholen" ran for about five minutes (86
    # relation checks one after the other while matching the CV) and refused the analysis of a second CV
    gate = threading.Event()
    client = ScriptedClient(relation_gate=gate, relation_delay=0.05)
    results, texts = thirty_postings_in_an_ended_batch(tmp_path)
    client.messages.batches = FakeBatchClient(results=results, statuses=["ended"]).messages.batches
    analyzed_cv(tmp_path)
    second = f"{CV} Second CV, with its own words: Lebenslauf von Erika Beispiel."
    with logs.file_log(tmp_path / "skillpose.log"):
        c = make_client(tmp_path, client=client)
        runner = c.app.state.skillpose.tasks = TaskRunner()  # threads, as in the running app
        upload = c.post("/cv", files={"file": ("erika_beispiel.md", second.encode())}, follow_redirects=False)
        assert upload.headers["location"] == "/cv/2/text"
        try:
            c.get("/postings", headers=DE)  # the visit collects the batch, then matches CV 1 with its 30 postings
            assert client.relation_started.wait(5)  # the first relation check runs alone and is held
            started = fmt_time(runner.current.started, "%H:%M")
            assert task_line(c.get("/costs", headers=DE).text) == (
                f"läuft seit {started}: Batch abholen und abgleichen : Abgleich mit Lebenslauf: 0 von 10 geprüft")

            r = c.post("/cv/2/analyze", data={"confirm": "1"}, headers=DE, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/cv/2?msg=queued"
            page = c.get(r.headers["location"], headers=DE).text
            assert "Eingereiht – startet, wenn die laufende Aufgabe fertig ist." in page
            assert "Wartet: Lebenslauf analysieren" in page and "Abgleich mit Lebenslauf: 0 von 10" in page
            assert [w.target for w in runner.waiting] == ["/cv/2"]
            assert not any(call.path == "CvDraft" for call in client.calls)  # it waits: nothing sent yet
        finally:
            gate.set()
            assert runner.join(10)

        # the analysis ran by itself after the collect task
        task = runner.current
        assert (task.name, task.target, task.ok) == ("cv", "/cv/2", True), task
        assert task.message == "Profil fertig: 2 Kompetenzen, 30 Inserate abgeglichen"  # in the language of the click
        paths = [call.path for call in client.calls]
        assert paths.count("CvDraft") == 1 and paths.index("CvDraft") > paths.index("RelationAnswer")
        assert paths.count("RelationAnswer") == 10  # CV 2 has the skills of CV 1: its pairs are known already
        assert client.relations.most == 4  # the checks ran four at once
        with Store(tmp_path / "web.sqlite") as st:
            assert st.profile(2) is not None and st.pending_batches() == [] and st.open_postings() == []
            assert len(st.matches(1)) == 30 and len(st.matches(2)) == 30
        page = c.get("/cv/2?msg=queued", headers=DE).text
        assert "Eingereiht" not in page and "Wartet:" not in page and "Profil fertig" in page

    text = (tmp_path / "skillpose.log").read_text(encoding="utf-8")
    assert "batch batch_1 collected: 30 of 30 postings analyzed" in text
    assert "task collect for /postings ended ok after" in text and "Batch abgeholt – analysiert: 30" in text
    assert "task cv for /cv/2 queued (1 waiting)" in text
    assert text.index("task collect for /postings ended ok") < text.index("task cv for /cv/2 started")
    assert re.search(r"task cv for /cv/2 ended ok after \d+\.\d s: Profil fertig", text)
    assert text.count("api call relation: claude-haiku-5-5") == 10 and text.count("api call cv: ") == 1
    for secret in (CV[:40], QUOTE, "Maria Berger", "Erika Beispiel", "erika_beispiel", POSTING_MARKER, texts[0],
                   "first_cv.md"):
        assert secret not in text, secret
