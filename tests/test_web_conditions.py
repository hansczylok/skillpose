"""Conditions in the web app: the form on the profile page, the three blocks of a match and the list
of blocking conditions in the market view. The answers are stored once per database; a save recomputes the matches of
every CV, offline."""

import re
from pathlib import Path

import pytest
from demo_fixture import bundle_with_answers, write_fixture_bundles
from fakes import CV, FailingClient
from test_web_jobs import cells, cv_with_profile, plain, row, seed_match
from test_web_market import block, gap_widths
from webkit import make_client, task_running

from skillpose.core.config import load_settings
from skillpose.core.matching import needed_pairs
from skillpose.core.models import Conditions, JobProfile, Requirement, SkillProposal
from skillpose.core.vocabulary import Vocabulary
from skillpose.store.db import Store
from skillpose.web import __file__ as web_init
from skillpose.web.i18n import LANGS, load_texts

DE = {"accept-language": "de"}
LICENCE = Requirement(skill_id="driving_licence_b", importance="must", min_level=None, min_years=None,
                      kind="condition", quote="Führerschein B", condition="driving_licence_b")
SELENIUM = Requirement(skill_id="selenium", importance="must", min_level=None, min_years=None, kind="tool",
                       quote="Selenium")
CYPRESS = Requirement(skill_id="cypress", importance="nice", min_level=None, min_years=None, kind="tool",
                      quote="Cypress")
ANSWERS = {"driving_licence_b": "0", "travel_share_max": "20", "shift_work": "", "commute_km_max": "35",
           "remote_only": "", "full_time": "1", "notes": "By train only"}
SAVED = Conditions(driving_licence_b=False, travel_share_max=20, commute_km_max=35, full_time=True,
                   notes="By train only")


def conditions_of(tmp_path) -> Conditions:
    with Store(tmp_path / "web.sqlite") as st:
        return st.conditions()


def two_cvs_and_a_posting(tmp_path) -> str:
    """CV 1 and CV 2, each with the profile of Selenium and Java, and one analyzed posting that asks for Selenium
    and a driving licence B, and would like Cypress: a skill whose relation to Selenium a match with a client asks
    Claude. There is no match yet: the first rematch makes them."""
    cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.save_profile(st.save_cv("other.md", CV + " Other."), st.profile(1), "fake")
        h = st.save_posting("Test Automation Engineer\nSelenium and a driving licence B.", "job.md")
        st.save_job_profile(JobProfile(text_hash=h, title="Test Automation Engineer", role_family="test_automation",
                                       seniority="mid", requirements=[SELENIUM, LICENCE, CYPRESS]), "fake")
    return h


def licence_of_each_cv(tmp_path) -> list[tuple[str, int]]:
    """(status of the licence, tier) of the one match of each CV."""
    with Store(tmp_path / "web.sqlite") as st:
        return [(jm.matches[1].status, jm.tier) for cv in st.cvs() for jm in st.matches(cv.id)]


def control(page: str, name: str) -> str:
    """The HTML of the form control of one answer: a select, a text field or the notes."""
    found = re.search(rf'<(select|textarea)[^>]*name="{name}".*?</\1>|<input[^>]*name="{name}"[^>]*>', page, re.S)
    assert found is not None, name
    return found.group(0)


def section(page: str) -> str:
    """The conditions section of the profile page."""
    found = re.search(r'<section id="conditions".*?</section>', page, re.S)
    assert found is not None
    return found.group(0)


def field_error(page: str, name: str) -> str | None:
    """The message next to one field of the form, as text; None: no message."""
    found = re.search(rf'<p class="msg error" role="alert" id="cond-{name}-error">(.*?)</p>', page, re.S)
    return plain(found.group(1)) if found else None


# --- the form on the profile page ---

def test_the_profile_page_has_the_form_with_the_stored_answers(tmp_path):
    c = make_client(tmp_path)
    cv_with_profile(tmp_path)
    page = c.get("/cv/1").text
    form = section(page)
    assert 'action="/cv/1/conditions"' in form and 'method="post"' in form and 'data-busy="Saving …"' in form
    for name in ("driving_licence_b", "shift_work", "remote_only", "full_time"):
        assert 'value="" selected' in control(page, name) and control(page, name).count("<option") == 3
    for name in ("travel_share_max", "commute_km_max"):
        assert 'value=""' in control(page, name)
    assert "Do you have a driving licence B?" in form and "Not answered" in form and "never sent to Claude" in form
    with Store(tmp_path / "web.sqlite") as st:
        st.save_conditions(SAVED)
    page = c.get("/cv/1").text
    assert re.search(r'<option value="0" selected>No</option>', control(page, "driving_licence_b"))
    assert re.search(r'<option value="1" selected>Yes</option>', control(page, "full_time"))
    assert 'value=""' in control(page, "shift_work") and "selected" in control(page, "shift_work")
    assert 'value="20"' in control(page, "travel_share_max") and 'value="35"' in control(page, "commute_km_max")
    assert ">By train only</textarea>" in control(page, "notes")
    de = section(c.get("/cv/1", headers=DE).text)
    assert "Rahmenbedingungen" in de and "Hast du den Führerschein B?" in de and "Antworten speichern" in de
    assert 'data-busy="Speichern …"' in de and "Nicht beantwortet" in de


FIELDS = ("driving_licence_b", "travel_share_max", "shift_work", "commute_km_max", "remote_only", "full_time")


def hint_of(page: str, name: str) -> str:
    """The hint under one field of the form, as text."""
    found = re.search(rf'<p class="meta" id="cond-{name}-hint">(.*?)</p>', page, re.S)
    assert found is not None, name
    return plain(found.group(1))


def test_every_field_is_a_question_and_its_hint_says_what_the_answers_do(tmp_path):
    """A user read "Nur Remote-Arbeit" with Yes made every posting with a place of work a knockout, because
    it was read as "working from home is possible too". Each field asks about the user; the hint names the effect."""
    c = make_client(tmp_path)
    cv_with_profile(tmp_path)
    texts = load_texts()
    knockout = {"en": "knockout", "de": "K.-o."}
    for lang, headers in (("en", {}), ("de", DE)):
        page = c.get("/cv/1", headers=headers).text
        assert knockout[lang] in plain(section(page))  # the intro says what a knockout is
        for name in FIELDS:
            label = texts[lang][f"conditions.{name}"]
            assert label.endswith("?"), (lang, name)
            assert re.search(rf'<label for="cond-{name}">{re.escape(label)}</label>', page), (lang, name)
            assert f'aria-describedby="cond-{name}-hint"' in control(page, name), (lang, name)
            hint = hint_of(page, name)
            assert hint and "{" not in hint, (lang, name)
            assert (knockout[lang] in hint) == (name != "commute_km_max"), (lang, name)  # the commute decides no tier
    en, de = hint_of(c.get("/cv/1").text, "commute_km_max"), hint_of(c.get("/cv/1", headers=DE).text, "commute_km_max")
    assert "does not calculate distances" in en and "Die App rechnet noch keine Entfernungen" in de
    assert "A number alone is enough" in en and "Schon mit einer Zahl gilt jeder Arbeitsort" in de
    en_travel = hint_of(c.get("/cv/1").text, "travel_share_max")
    de_travel = hint_of(c.get("/cv/1", headers=DE).text, "travel_share_max")
    assert "without a per cent sign" in en_travel and "the app does not check the travel" in en_travel
    assert "ohne Prozent" in de_travel and "prüft die App die Reisen nicht" in de_travel
    # "only from home" must not be skimmed as "home office is possible too": the hint starts with the warning
    assert hint_of(c.get("/cv/1").text, "remote_only").startswith("Answer Yes only if you can never work on site.")
    german = hint_of(c.get("/cv/1", headers=DE).text, "remote_only")
    assert german.startswith("Antworte nur mit Ja, wenn du nie vor Ort arbeiten kannst.")


def test_the_travel_hint_names_the_default_share_of_the_rules(tmp_path):
    """A posting that names no number asks for rules.conditions_default_travel_share: the hint shows that value."""
    cv_with_profile(tmp_path)
    settings = load_settings()
    other = settings.model_copy(update={"rules": settings.rules.model_copy(
        update={"conditions_default_travel_share": 25})})
    for given, share in ((None, settings.rules.conditions_default_travel_share), (other, 25)):
        c = make_client(tmp_path, settings=given)
        assert f"the app counts {share} %." in hint_of(c.get("/cv/1").text, "travel_share_max")
        assert f"rechnet die App mit {share} %." in hint_of(c.get("/cv/1", headers=DE).text, "travel_share_max")


def test_the_form_is_on_the_profile_page_of_an_analyzed_cv_and_nowhere_else(tmp_path):
    c = make_client(tmp_path)
    cv_with_profile(tmp_path)
    c.post("/cv", files={"file": ("new.md", (CV + " Third.").encode())})  # CV 2: no profile yet
    assert 'id="conditions"' in c.get("/cv/1").text
    for path in ("/cv/2", "/cv/1/matches", "/cv/1/market", "/cv/2/text", "/postings"):
        assert 'id="conditions"' not in c.get(path).text, path


def test_saving_stores_one_record_and_recomputes_every_cv_without_a_call(tmp_path):
    api = FailingClient()  # any call fails, and is recorded
    c = make_client(tmp_path, client=api)
    h = two_cvs_and_a_posting(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:  # with a client, the match would ask Claude about Cypress
        assert needed_pairs(st.job_profile(h), st.profile(1), Vocabulary.load()) == {"cypress": ["java", "selenium"]}
    r = c.post("/cv/1/conditions", data=ANSWERS, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cv/1?msg=conditions_saved&form=conditions#conditions"
    assert conditions_of(tmp_path) == SAVED
    assert licence_of_each_cv(tmp_path) == [("gap", 3), ("gap", 3)]  # the answers apply to every CV
    task = c.app.state.skillpose.tasks.current
    assert task.name == "rematch" and task.target == "/cv/1" and task.ok
    assert task.message == "Matches recomputed with your answers: 2 matches"
    assert api.calls == []
    page = c.get(r.headers["location"]).text
    assert "Saved. The matches of all CVs follow these answers" in plain(section(page))
    assert "Matches recomputed with your answers: 2 matches" in page
    # another answer from the page of the other CV: one record, every match follows
    c.post("/cv/2/conditions", data={**ANSWERS, "driving_licence_b": "1"})
    assert conditions_of(tmp_path).driving_licence_b is True
    assert licence_of_each_cv(tmp_path) == [("met", 1), ("met", 1)] and api.calls == []
    assert c.app.state.skillpose.tasks.current.target == "/cv/2" and c.app.state.skillpose.tasks.current.ok
    with Store(tmp_path / "web.sqlite") as st:  # Cypress got no relation answer: nothing was asked
        assert [jm.matches[2].reason for cv in st.cvs() for jm in st.matches(cv.id)] == ["not in the CV"] * 2


def test_the_notice_and_the_task_line_speak_german_on_a_german_page(tmp_path):
    c = make_client(tmp_path)
    two_cvs_and_a_posting(tmp_path)
    r = c.post("/cv/1/conditions?lang=de", data=ANSWERS, follow_redirects=False)
    page = c.get(r.headers["location"], headers=DE).text
    assert "Gespeichert. Der Abgleich aller Lebensläufe folgt diesen Antworten" in plain(section(page))
    assert "Abgleich mit deinen Antworten neu berechnet: 2 Abgleiche" in page


def test_an_answer_can_be_taken_back_and_zero_is_an_answer(tmp_path):
    c = make_client(tmp_path)
    cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        st.save_conditions(SAVED)
    c.post("/cv/1/conditions", data={"travel_share_max": " 0 ", "notes": "  \r\n  "})
    assert conditions_of(tmp_path) == Conditions(travel_share_max=0)  # empty fields: not answered; no travel: 0
    assert 'value="0"' in control(c.get("/cv/1").text, "travel_share_max")


@pytest.mark.parametrize("name, value, message, message_de", [
    ("travel_share_max", "101", "Enter a whole number from 0 to 100.", "Bitte eine ganze Zahl von 0 bis 100."),
    ("travel_share_max", "-1", "Enter a whole number from 0 to 100.", "Bitte eine ganze Zahl von 0 bis 100."),
    ("travel_share_max", "1.5", "Enter a whole number from 0 to 100.", "Bitte eine ganze Zahl von 0 bis 100."),
    ("travel_share_max", "abc", "Enter a whole number from 0 to 100.", "Bitte eine ganze Zahl von 0 bis 100."),
    ("travel_share_max", "²", "Enter a whole number from 0 to 100.", "Bitte eine ganze Zahl von 0 bis 100."),
    ("commute_km_max", "-5", "Enter a whole number of kilometres, 0 or more.",
     "Bitte eine ganze Zahl in Kilometern, 0 oder mehr."),
    ("commute_km_max", "x", "Enter a whole number of kilometres, 0 or more.",
     "Bitte eine ganze Zahl in Kilometern, 0 oder mehr."),
    ("commute_km_max", "100000", "Enter a whole number of kilometres, 0 or more.",
     "Bitte eine ganze Zahl in Kilometern, 0 oder mehr."),
    ("notes", "x" * 1001, "At most 1000 characters.", "Höchstens 1000 Zeichen.")])
def test_a_wrong_answer_is_refused_next_to_its_field_and_nothing_is_saved(tmp_path, name, value, message, message_de):
    c = make_client(tmp_path)
    cv_with_profile(tmp_path)
    others = {n: v for n, v in ANSWERS.items() if n != name}
    for headers, text in (({}, message), (DE, message_de)):
        r = c.post("/cv/1/conditions", data={**others, name: value}, headers=headers)
        assert r.status_code == 400 and field_error(r.text, name) == text
        assert [n for n in ANSWERS if field_error(r.text, n)] == [name]  # only the wrong field has a message
        assert conditions_of(tmp_path) == Conditions()  # not one answer of the form was kept
        assert c.app.state.skillpose.tasks.current is None
        shown = control(r.text, name)  # what the user typed stays in the form
        assert (value in shown if name != "notes" else "x" * 1001 in shown) and 'aria-invalid="true"' in shown
        assert 'value="35"' in control(r.text, "commute_km_max") or name == "commute_km_max"
        assert "running since" not in r.text and '<meta http-equiv="refresh"' not in r.text


def test_a_choice_that_the_form_cannot_send_is_refused(tmp_path):
    c = make_client(tmp_path)
    cv_with_profile(tmp_path)
    assert c.post("/cv/1/conditions", data={"driving_licence_b": "maybe"}).status_code == 400
    assert c.post("/cv/99/conditions", data=ANSWERS).status_code == 404
    assert conditions_of(tmp_path) == Conditions()


def test_a_save_while_another_task_runs_is_saved_and_its_rematch_waits(tmp_path):
    c = make_client(tmp_path)
    two_cvs_and_a_posting(tmp_path)
    with task_running(c, "postings", "/postings"):
        r = c.post("/cv/1/conditions", data=ANSWERS, follow_redirects=False)
        assert r.headers["location"] == "/cv/1?msg=queued&form=conditions#conditions"
        page = c.get(r.headers["location"]).text
        assert "Queued – starts when the running task has finished." in plain(section(page))
        assert "Waiting: Recompute matches" in page
        assert conditions_of(tmp_path) == SAVED
    task = c.app.state.skillpose.tasks.current
    assert task.name == "rematch" and task.ok
    assert licence_of_each_cv(tmp_path) == [("gap", 3), ("gap", 3)]


def test_a_save_while_a_task_of_this_cv_runs_changes_nothing(tmp_path):
    # the running task may have read the answers already, and the same task is never queued twice
    c = make_client(tmp_path)
    two_cvs_and_a_posting(tmp_path)
    with task_running(c, "rematch", "/cv/1"):
        r = c.post("/cv/1/conditions", data=ANSWERS, follow_redirects=False)
        assert r.headers["location"] == "/cv/1?msg=busy&form=conditions#conditions"
        assert "Another task is running" in plain(section(c.get(r.headers["location"]).text))
    assert conditions_of(tmp_path) == Conditions()


def test_a_save_when_five_tasks_wait_changes_nothing(tmp_path):
    c = make_client(tmp_path)
    cv_with_profile(tmp_path)
    with task_running(c, "postings", "/postings") as runner:
        for i in range(5):
            runner.start("cv", f"/cv/{i + 2}", lambda: "")
        r = c.post("/cv/1/conditions", data=ANSWERS, follow_redirects=False)
        assert r.headers["location"] == "/cv/1?msg=busy&form=conditions#conditions"
    assert conditions_of(tmp_path) == Conditions()


def test_no_cv_can_be_deleted_while_a_save_recomputes_every_cv(tmp_path):
    """The task of a save recomputes the matches of every CV: a CV deleted meanwhile would end it with an error."""
    c = make_client(tmp_path)
    two_cvs_and_a_posting(tmp_path)
    busy, busy_start = "/cv/2?msg=delete_busy", "/?msg=delete_busy&cv=2#cv-2"
    with task_running(c, "rematch", "/cv/1", key="conditions"):  # the task of a save on the page of CV 1 runs
        assert c.post("/cv/2/delete", follow_redirects=False).headers["location"] == busy
        assert c.post("/cv/2/delete", data={"back": "start"}, follow_redirects=False).headers["location"] == busy_start
    with task_running(c, "postings", "/postings"):  # it waits
        assert c.post("/cv/1/conditions", data=ANSWERS, follow_redirects=False).headers["location"].startswith(
            "/cv/1?msg=queued")
        assert c.post("/cv/2/delete", follow_redirects=False).headers["location"] == busy
    with Store(tmp_path / "web.sqlite") as st:
        assert [cv.id for cv in st.cvs()] == [2, 1]
    assert licence_of_each_cv(tmp_path) == [("gap", 3), ("gap", 3)]  # the save recomputed both
    with task_running(c, "rematch", "/cv/1"):  # a skill edit of CV 1 recomputes CV 1 only
        assert c.post("/cv/2/delete", follow_redirects=False).headers["location"] == "/?msg=deleted"


def test_typed_notes_and_numbers_are_shown_escaped(tmp_path):
    markup = '<script>alert("x")</script> & <b>bold</b>'
    escaped = "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;"
    c = make_client(tmp_path)
    cv_with_profile(tmp_path)
    r = c.post("/cv/1/conditions", data={**ANSWERS, "travel_share_max": "<b>9</b>", "notes": markup})
    assert r.status_code == 400 and field_error(r.text, "travel_share_max")  # shown again as typed, escaped
    assert 'value="&lt;b&gt;9&lt;/b&gt;"' in control(r.text, "travel_share_max")
    assert f">{escaped}</textarea>" in control(r.text, "notes")
    assert "<script>alert" not in r.text and "<b>bold" not in r.text and "<b>9" not in r.text
    c.post("/cv/1/conditions", data={**ANSWERS, "notes": markup})
    assert conditions_of(tmp_path).notes == markup  # stored as typed
    page = c.get("/cv/1").text
    assert f">{escaped}</textarea>" in control(page, "notes") and "<script>alert" not in page
    write_fixture_bundles(tmp_path / "demo", bundle_with_answers(SAVED.model_copy(update={"notes": markup})))
    (tmp_path / "d").mkdir()
    demo = make_client(tmp_path / "d", key=False, demo_dir=tmp_path / "demo")  # the read-only list
    page = demo.get("/cv/1").text
    assert f"<dd>{escaped}</dd>" in section(page) and "<script>alert" not in page and "<b>bold" not in page


def test_the_notice_of_the_form_shows_at_the_form_only(tmp_path):
    c = make_client(tmp_path)
    cv_with_profile(tmp_path)
    page = c.get("/cv/1?msg=busy&form=conditions").text
    assert page.count("Another task is running") == 1 and "Another task is running" in section(page)
    saved = c.get("/cv/1?msg=conditions_saved&form=conditions").text
    assert "Saved. The matches of all CVs" in section(saved) and saved.count("Saved. The matches") == 1
    for query in ("?msg=busy", "?msg=conditions_saved", "?form=conditions", "?msg=busy&skill=java"):
        page = c.get("/cv/1" + query).text
        assert "Another task is running" not in section(page) and "Saved. The matches" not in page, query


# --- locked: the demo and the own data without a key ---

def facts(html: str) -> dict[str, str]:
    """The pairs of the read-only list: label -> shown answer."""
    return {plain(label): plain(value) for label, value in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", html, re.S)}


def demo_client(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    return make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")


def test_the_demo_shows_the_answers_of_its_bundles_read_only(tmp_path):
    write_fixture_bundles(tmp_path / "demo", bundle_with_answers(SAVED))
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    en, de = c.get("/cv/1").text, c.get("/cv/1", headers=DE).text
    for page in (en, de):
        assert "<form" not in page and "/cv/1/conditions" not in page and "<select" not in page
        assert 'id="conditions"' in page
    assert facts(section(en)) == {
        "Do you have a driving licence B?": "No",
        "Up to what share of your working time can you travel, in per cent?": "up to 20 %",
        "Can you work in shifts?": "Not answered",
        "How far can you commute to work at most, in kilometres?": "up to 35 km",
        "Do you have to work exclusively from home – never on site?": "Not answered",
        "Can you work full-time?": "Yes", "Notes": "By train only"}
    assert facts(section(de)) == {
        "Hast du den Führerschein B?": "Nein",
        "Bis zu wie viel Prozent deiner Arbeitszeit kannst du reisen?": "bis 20 %",
        "Kannst du in Schichten arbeiten?": "Nicht beantwortet",
        "Wie weit kannst du höchstens zur Arbeit pendeln (in Kilometern)?": "bis 35 km",
        "Musst du ausschließlich von zu Hause arbeiten – nie vor Ort?": "Nicht beantwortet",
        "Kannst du in Vollzeit arbeiten?": "Ja", "Notizen": "By train only"}


def test_the_demo_shows_unanswered_conditions_when_the_bundle_has_no_answers(tmp_path):
    shown = facts(section(demo_client(tmp_path).get("/cv/1").text))
    assert list(shown.values()).count("Not answered") == 6 and shown["Notes"] == "–"


def test_without_a_form_the_text_does_not_ask_for_answers(tmp_path):
    """The demo, and the own data without a key, show the answers but offer no form."""
    demo = demo_client(tmp_path)
    en, de = plain(section(demo.get("/cv/1").text)), plain(section(demo.get("/cv/1", headers=DE).text))
    assert "you answer them here" not in en and "Du beantwortest sie hier" not in de
    assert ("so they are answered once in the profile, for every CV of the database" in en
            and "Without an API key the answers can only be read." in en)
    assert ("Darum werden sie einmal im Profil beantwortet, für jeden Lebenslauf der Datenbank." in de
            and "Ohne API-Schlüssel kann man die Antworten nur lesen." in de)
    (tmp_path / "own").mkdir()
    cv_id = cv_with_profile(tmp_path / "own")
    with Store(tmp_path / "own" / "web.sqlite") as st:
        h = seed_match(st, cv_id, "Mixed", 2, 0.5, MIXED_ROWS, requirement_fields=MIXED_FIELDS)
        seed_blocked(st, cv_id, "test_automation", 1)
    locked, keyed = make_client(tmp_path / "own", key=False), make_client(tmp_path / "own")
    assert "can only be read" in plain(section(locked.get("/cv/1").text))
    assert "you answer them here" in plain(section(keyed.get("/cv/1").text))
    for page in (f"/cv/1/matches/{h}", "/cv/1/market"):
        en, de = locked.get(page).text, locked.get(page, headers=DE).text
        assert "Change your answers" not in en and "Antworten ändern" not in de, page
        assert '<a href="/cv/1#conditions">See the answers</a>' in en, page
        assert '<a href="/cv/1#conditions">Antworten ansehen</a>' in de, page
        with_key = keyed.get(page).text
        assert '<a href="/cv/1#conditions">Change your answers</a>' in with_key and "See the answers" not in with_key


def test_without_a_key_the_conditions_cannot_be_saved(tmp_path):
    c = demo_client(tmp_path)
    r = c.post("/cv/1/conditions", data=ANSWERS)
    assert r.status_code == 403 and "No API key set" in r.text
    with Store(c.app.state.skillpose.demo_db) as st:
        assert st.conditions() == Conditions()
    assert c.app.state.skillpose.tasks.current is None


def test_a_page_of_another_site_cannot_change_the_answers(tmp_path):
    api = FailingClient()
    c = make_client(tmp_path, client=api)
    two_cvs_and_a_posting(tmp_path)
    for headers in ({"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"},
                    {"origin": "http://testserver:8080"}):
        assert c.post("/cv/1/conditions", data=ANSWERS, headers=headers).status_code == 403, headers
    assert conditions_of(tmp_path) == Conditions() and c.app.state.skillpose.tasks.current is None
    assert c.post("/cv/1/conditions", data=ANSWERS, headers={"origin": "http://testserver"},
                  follow_redirects=False).status_code == 303
    assert conditions_of(tmp_path) == SAVED and api.calls == []


# --- the three blocks of a match ---

MIXED_ROWS = [
    ("java", "must", "met", None, "evidenced"),
    ("docker", "must", "partial", "weeks", "only listed, no evidence of use"),
    ("kubernetes", "nice", "gap", "months", "not in the CV"),
    ("teamwork", "must", "unverifiable", None, "soft skill: not verifiable from a CV"),
    ("communication", "nice", "unverifiable", None, "soft skill: not verifiable from a CV"),
    ("driving_licence_b", "must", "gap", None, "profile: no driving licence B"),
    ("travel_willingness", "must", "unanswered", None, "not answered in the profile"),
    ("work_location", "nice", "met", None, "profile: not only remote")]
MIXED_FIELDS = {
    "teamwork": {"kind": "soft", "quote": "Teamfähigkeit"},
    "communication": {"kind": "soft", "quote": "Kommunikationsstärke"},
    "driving_licence_b": {"kind": "condition", "condition": "driving_licence_b", "quote": "Führerschein B"},
    "travel_willingness": {"kind": "condition", "condition": "travel", "condition_value": "ca. 30 %",
                           "quote": "Reisebereitschaft ca. 30 %"},
    "work_location": {"kind": "condition", "condition": "location", "condition_value": "Linz",
                      "quote": "Arbeitsort Linz"}}


def mixed_match(tmp_path) -> tuple[str, str]:
    """The pages of a match with every kind of requirement: (the English page, the German page)."""
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_match(st, cv_id, "Mixed", 2, 0.5, MIXED_ROWS, requirement_fields=MIXED_FIELDS)
    return c.get(f"/cv/1/matches/{h}").text, c.get(f"/cv/1/matches/{h}", headers=DE).text


def part(page: str, name: str) -> str:
    """The HTML of one of the blocks of a match page: requirements, unverifiable or conditions."""
    found = re.search(rf'<div class="block" id="block-{name}">.*?</div>\s*(?=<div class="block"|</section>)', page,
                      re.S)
    assert found is not None, name
    return found.group(0)


def names_in(html: str) -> list[str]:
    return re.findall(r'<strong>(.*?)</strong>', html)


def test_a_match_shows_three_blocks(tmp_path):
    en, de = mixed_match(tmp_path)
    assert [name for name in ("requirements", "unverifiable", "conditions") if f'id="block-{name}"' in en] == [
        "requirements", "unverifiable", "conditions"]
    assert en.index('id="block-requirements"') < en.index('id="block-unverifiable"') < en.index('id="block-conditions"')
    assert names_in(part(en, "requirements")) == ["Docker", "Java", "Kubernetes"]  # as before: must, open ones first
    assert names_in(part(en, "unverifiable")) == ["Teamwork", "Communication skills"]
    assert names_in(part(en, "conditions")) == ["Driving licence class B", "travel_willingness", "work_location"]
    assert names_in(part(de, "unverifiable")) == ["Teamfähigkeit", "Kommunikationsfähigkeit"]
    for page, heads in ((en, ("Requirements", "Not verifiable from the CV", "Conditions")),
                        (de, ("Anforderungen", "Nicht aus dem Lebenslauf prüfbar", "Rahmenbedingungen"))):
        assert re.findall(r"<h2>(.*?)</h2>", page) == list(heads)


def test_soft_skills_show_the_quote_of_the_posting_and_no_status(tmp_path):
    en, de = mixed_match(tmp_path)
    soft = part(en, "unverifiable")
    assert "<q>Teamfähigkeit</q>" in soft and "<q>Kommunikationsstärke</q>" in soft
    assert "status-" not in soft and "In the CV" not in soft and "Reason" not in soft
    assert "must" in soft and "nice to have" in soft and "count in no coverage and no tier" in plain(soft)
    assert "zählen in keiner Abdeckung und keiner Stufe" in plain(part(de, "unverifiable"))


def test_conditions_show_the_answer_the_reason_and_the_link_to_the_form(tmp_path):
    en, de = mixed_match(tmp_path)
    conditions = part(en, "conditions")
    licence, travel, location = re.split(r'(?=<li class="skill")', conditions)[1:]
    assert re.search(r'class="mark status-gap">\s*not met\s*<', licence) and "<q>Führerschein B</q>" in licence
    assert "profile: no driving licence B" in plain(licence) and "Driving licence B" in plain(licence)
    assert re.search(r'class="mark status-unanswered">\s*unanswered\s*<', travel)
    assert "Travel: ca. 30 %" in plain(travel) and "not answered in the profile" in plain(travel)
    assert re.search(r'class="mark status-met">\s*met\s*<', location) and "Place of work: Linz" in plain(location)
    assert 'href="/cv/1#conditions"' in conditions and "Change your answers" in conditions
    german = part(de, "conditions")
    assert re.search(r'class="mark status-gap">\s*nicht erfüllt\s*<', german)
    assert re.search(r'class="mark status-unanswered">\s*unbeantwortet\s*<', german)
    assert re.search(r'class="mark status-met">\s*erfüllt\s*<', german)
    assert "Profil: kein Führerschein B" in german and "Reisebereitschaft: ca. 30 %" in plain(german)
    assert 'href="/cv/1#conditions"' in german and "Antworten ändern" in german


def test_the_main_gaps_of_the_match_list_leave_out_the_conditions(tmp_path):
    """A condition that is not met has its own line on the page of the match, in the export and in the market view;
    the list of main gaps names what can be learned."""
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    rows = [*MIXED_ROWS, ("shift_work", "nice", "gap", None, "profile: no shift work")]
    fields = MIXED_FIELDS | {"shift_work": {"kind": "condition", "condition": "shift_work"}}
    with Store(tmp_path / "web.sqlite") as st:
        seed_match(st, cv_id, "Mixed", 3, 0.5, rows, requirement_fields=fields)
    assert cells(row(c.get("/cv/1/matches").text, "Mixed"))[5] == "Docker, Kubernetes"
    assert cells(row(c.get("/cv/1/matches", headers=DE).text, "Mixed"))[5] == "Docker, Kubernetes"


def test_a_block_without_items_is_not_shown(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        plain_match = seed_match(st, cv_id, "Plain", 1, 1.0, [("java", "must", "met", None, "evidenced")])
        soft_only = seed_match(st, cv_id, "Soft only", 1, 1.0, MIXED_ROWS[3:5], requirement_fields=MIXED_FIELDS)
    page = c.get(f"/cv/1/matches/{plain_match}").text
    assert 'id="block-requirements"' in page and "block-unverifiable" not in page and "block-conditions" not in page
    page = c.get(f"/cv/1/matches/{soft_only}").text
    assert 'id="block-unverifiable"' in page and "block-requirements" not in page and "block-conditions" not in page
    assert "No requirements were read" not in page and page.count('class="ai-note"') == 1


def test_a_soft_skill_that_an_older_run_stored_as_a_gap_still_shows_as_not_verifiable(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    rows = [("teamwork", "must", "gap", "months", "not in the CV"), ("java", "must", "met", None, "evidenced")]
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_match(st, cv_id, "Old", 3, 0.5, rows, requirement_fields=MIXED_FIELDS)
    page = c.get(f"/cv/1/matches/{h}").text
    assert names_in(part(page, "unverifiable")) == ["Teamwork"] and names_in(part(page, "requirements")) == ["Java"]


def test_no_colour_belongs_to_a_soft_skill_or_an_unanswered_condition():
    css = (Path(web_init).parent / "static" / "style.css").read_text(encoding="utf-8")
    assert "status-unverifiable" not in css and "status-unanswered" not in css


def test_every_label_of_the_conditions_exists_in_both_languages():
    texts = load_texts()
    names = ["driving_licence_b", "travel_share_max", "shift_work", "commute_km_max", "remote_only", "full_time",
             "notes"]
    keys = [f"conditions.{name}" for name in names]
    keys += [f"conditions.key.{key}" for key in ("driving_licence_b", "travel", "shift_work", "location",
                                                  "working_time")]
    keys += [f"matches.condition_{status}" for status in ("met", "gap", "unanswered")]
    keys += ["conditions.up_to_travel_share_max", "conditions.up_to_commute_km_max"]
    for lang in LANGS:
        assert not [k for k in keys if k not in texts[lang]], lang


# --- the market view ---

def seed_blocked(st, cv_id, family, count, importance="must"):
    """count postings of a family, each with a driving licence B that the answers do not meet."""
    for i in range(count):
        rows = [("java", "must", "met", None, "evidenced"),
                ("driving_licence_b", importance, "gap", None, "profile: no driving licence B")]
        seed_match(st, cv_id, f"{family} blocked {i}", 3 if importance == "must" else 1, 1.0, rows, family=family,
                   requirement_fields={"driving_licence_b": {"kind": "condition",
                                                             "condition": "driving_licence_b"}})


def test_the_market_view_lists_a_blocking_condition_apart_from_the_gaps(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_blocked(st, cv_id, "test_automation", 2)
        seed_blocked(st, cv_id, "qa_management", 2, importance="nice")  # a nice condition blocks nothing
    page = c.get("/cv/1/market").text
    family = block(page, "test_automation")
    assert "Blocked by conditions" in family and "Postings: 2" in plain(family.split("Blocked by conditions")[1])
    assert names_in(family.split("Blocked by conditions")[1]) == ["Driving licence class B"]
    assert gap_widths(family) == {} and "No gaps." in family  # it is no gap
    assert 'href="/cv/1#conditions"' in family and "Change your answers" in family
    assert "Blocked by conditions" not in block(page, "qa_management")
    de = block(c.get("/cv/1/market", headers=DE).text, "test_automation")
    assert "Blockiert durch Bedingungen" in de and "Inserate: 2" in plain(de) and "Antworten ändern" in de


def test_a_condition_stays_in_its_list_when_the_family_has_gaps_too(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        seed_blocked(st, cv_id, "test_automation", 1)
        seed_match(st, cv_id, "Gap", 2, 0.5, [("docker", "must", "gap", "months", "not in the CV")])
    family = block(c.get("/cv/1/market").text, "test_automation")
    assert list(gap_widths(family)) == ["Docker"] and "licence" not in family.split("Blocked by")[0]


def test_a_condition_whose_name_is_its_kind_shows_only_what_the_posting_asks(tmp_path):
    c = make_client(tmp_path)
    cv_id = cv_with_profile(tmp_path)
    with Store(tmp_path / "web.sqlite") as st:
        h = seed_match(st, cv_id, "Mixed", 2, 0.5, MIXED_ROWS, requirement_fields=MIXED_FIELDS)
        job = st.job_profile(h)
        proposal = SkillProposal(id="travel_willingness", name="Travel", category="soft")
        st.save_job_profile(job.model_copy(update={"proposed_skills": [proposal]}), "fake")
    travel = part(c.get(f"/cv/1/matches/{h}").text, "conditions").split('id="req-travel_willingness"')[1]
    travel = travel.split("</li>")[0]
    assert "<strong>Travel</strong>" in travel and '<p class="meta">ca. 30 %</p>' in travel
