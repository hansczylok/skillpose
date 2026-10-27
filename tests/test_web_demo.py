import re
import tempfile

from demo_fixture import NAME, bundle_with_proposals, fixture_bundle, write_fixture_bundles
from fakes import FailingClient
from webkit import make_client

from skillpose.core.llm import Usage
from skillpose.store.db import Store
from skillpose.web.deps import person_label

DE = {"accept-language": "de"}
# paths of writing routes that later tasks add; the lock answers before routing, so they are locked already
WRITES = [("POST", "/cv"), ("POST", "/cv/1/analyze"), ("POST", "/postings"), ("POST", "/cv/1/paths/test_automation"),
          ("POST", "/cv/1/conditions"), ("PUT", "/cv/1"), ("PATCH", "/cv/1/skills/selenium"), ("DELETE", "/cv/1")]


def demo_client(tmp_path, **kwargs):
    write_fixture_bundles(tmp_path / "demo")
    return make_client(tmp_path, demo_dir=tmp_path / "demo", **kwargs)


def upload_disabled(html: str) -> bool:
    return re.search(r'<input id="cv-file"[^>]* disabled', html) is not None


def test_without_key_the_demo_person_is_shown(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    assert "Maria Demo" in make_client(tmp_path, key=False, demo_dir=tmp_path / "demo").get("/").text


def test_demo_start_page_links_the_persons_and_locks_the_upload(tmp_path):
    text = demo_client(tmp_path, key=False).get("/", headers=DE).text
    assert f'<a href="/cv/1">{NAME}</a>' in text and "erfundene Personen" in text and upload_disabled(text)
    assert "/view/" not in text and 'class="views"' not in text  # without a key there is only the demo


def post_paths(routes, prefix: str = "") -> list[str]:
    """Every POST route of the app. FastAPI keeps an included router as one entry that holds its routes, so a look at
    app.routes alone finds none of the routes of pages_cv, pages_jobs and pages_market."""
    found = []
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found += post_paths(inner.routes, prefix + route.include_context.prefix)
        elif "POST" in getattr(route, "methods", set()):
            found.append(prefix + route.path)
    return found


def test_every_post_route_is_blocked_in_demo(tmp_path):
    c = demo_client(tmp_path, key=False)   # without a key: the demo only, and no API client at all
    posts = post_paths(c.app.routes)
    assert {"/cv", "/cv/{cv_id}/delete", "/postings/analyze", "/cv/{cv_id}/paths/{role_family}",
            "/cv/{cv_id}/conditions"} <= set(posts)
    assert not [path for path in posts if path.startswith("/view/")]   # the view switch is gone
    for cookie in (None, "own", "demo"):   # a view cookie left from the switch of older versions changes nothing
        if cookie:
            c.cookies.set("view", cookie)
        for path in posts:
            r = c.post(re.sub(r"\{[^}]+\}", "1", path), data={})
            assert r.status_code == 403 and "No API key set" in r.text, (cookie, path)
        for method, path in [*WRITES, ("POST", "/view/own"), ("POST", "/view/demo")]:
            assert c.request(method, path, data={}).status_code == 403, (cookie, method, path)
        assert NAME in c.get("/").text
    assert c.app.state.skillpose.tasks.current is None
    with Store(c.app.state.skillpose.demo_db) as st:
        assert [cv.id for cv in st.cvs()] == [1]   # the delete never ran


def test_lock_texts_in_both_languages(tmp_path):
    c = demo_client(tmp_path, key=False)
    r = c.post("/cv")
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")
    assert "No API key set" in r.text and r.headers["x-frame-options"] == "DENY"
    assert "Kein API-Schlüssel eingetragen" in c.post("/cv", headers=DE).text
    (tmp_path / "k").mkdir()
    no_demo = make_client(tmp_path / "k", key=False)   # no demo data: the own data, read-only
    r = no_demo.post("/cv?lang=de")
    assert r.status_code == 403 and "Kein API-Schlüssel eingetragen" in r.text
    assert "No API key set" in no_demo.post("/cv").text


def test_lock_lets_reading_pass(tmp_path):
    c = demo_client(tmp_path, key=False)
    assert c.get("/").status_code == 200 and c.get("/cv/1").status_code == 200 and c.get("/costs").status_code == 200


def test_with_a_key_nothing_is_locked(tmp_path):
    c = demo_client(tmp_path, client=FailingClient())
    assert c.post("/cv").status_code == 400   # the upload form answers: no file chosen
    c.cookies.set("view", "demo")              # a cookie left from the switch of older versions
    assert c.post("/cv").status_code == 400
    assert c.post("/view/demo", follow_redirects=False).status_code == 404   # the switch is gone


def test_with_a_key_the_app_shows_only_the_own_data(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    api = FailingClient()
    c = demo_client(tmp_path, client=api)
    assert c.app.state.skillpose.demo_db is None and list((tmp_path / "tmp").iterdir()) == []   # no demo store
    with Store(tmp_path / "web.sqlite") as st:
        st.save_cv("own.md", "x" * 300)
    demo_posting = fixture_bundle().postings[0].hash
    for cookie in (None, "demo"):   # a view cookie left from the switch of older versions changes nothing
        if cookie:
            c.cookies.set("view", cookie)
        for headers, button in (({}, "View demo"), (DE, "Demo ansehen")):
            page = c.get("/", headers=headers).text
            assert "own.md" in page and NAME not in page and not upload_disabled(page), (cookie, headers)
            assert button not in page and "/view/" not in page and 'class="views"' not in page, (cookie, headers)
            assert "Demo data missing" not in page and "Demo-Daten fehlen" not in page, (cookie, headers)
        assert "own.md" in c.get("/cv/1/text").text and NAME not in c.get("/cv/1").text   # CV 1 of the own data
        assert c.get("/cv/2").status_code == 404 and c.get(f"/postings/{demo_posting}").status_code == 404
        assert "Test Automation Engineer" not in c.get("/postings").text
    assert api.calls == []


def test_start_page_says_when_demo_data_are_missing(tmp_path):
    c = make_client(tmp_path, key=False)
    assert "Demo data missing" in c.get("/").text and "Demo-Daten fehlen" in c.get("/", headers=DE).text
    with_key = make_client(tmp_path).get("/").text   # with a key there is no demo to miss
    assert "Demo data missing" not in with_key and "/view/" not in with_key


def test_header_month_cost_only_with_a_key(tmp_path):
    write_fixture_bundles(tmp_path / "demo")
    with Store(tmp_path / "web.sqlite") as st:
        st.record("cv", "claude-opus-5-5", Usage(1000, 500, 0, 0), 0.12, False)
    no_key = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo").get("/").text
    assert NAME in no_key and 'class="cost"' not in no_key and "USD" not in no_key
    c = make_client(tmp_path, demo_dir=tmp_path / "demo")
    c.cookies.set("view", "demo")
    own = c.get("/").text
    assert NAME not in own and "0.12 / 5.00 USD" in own


def test_demo_store_lives_in_a_temporary_folder_until_shutdown(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    c = demo_client(tmp_path, key=False)
    db = c.app.state.skillpose.demo_db
    assert db.is_file() and db.parent.parent == tmp_path / "tmp"
    with c:
        assert NAME in c.get("/").text
    assert not db.parent.exists()


def test_no_demo_store_without_bundles(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    assert make_client(tmp_path, key=False).app.state.skillpose.demo_db is None
    assert list((tmp_path / "tmp").iterdir()) == []


def plain(html: str) -> str:
    """The visible text of a page: tags gone (an id in an attribute is not shown), white space collapsed."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_demo_shows_the_names_of_proposed_skills(tmp_path):
    # the demo store has no vocabulary additions: a proposed skill must show its proposed name, not its id
    write_fixture_bundles(tmp_path / "demo", bundle_with_proposals())
    c = make_client(tmp_path, key=False, demo_dir=tmp_path / "demo")
    profile = c.get("/cv/1").text
    assert '<h2 class="category">Other</h2>' not in profile
    tool = profile.index('<h2 class="category">Tool</h2>')
    assert tool < profile.index("<strong>DAX</strong>") < profile.index('<h2 class="category">Programming language')
    hash_ = re.search(r'href="/cv/1/matches/([0-9a-f]+)"', c.get("/cv/1/matches").text)[1]
    pages = {"matches": "/cv/1/matches", "match": f"/cv/1/matches/{hash_}", "market": "/cv/1/market",
             "paths": "/cv/1/paths"}
    for name, path in pages.items():
        for headers in ({}, DE):
            text = plain(c.get(path, headers=headers).text)
            assert "Apache Flink" in text and "apache_flink" not in text, (name, headers)
    assert "dax" not in plain(profile)


def test_demo_shows_no_time_of_its_own_store(tmp_path):
    # the demo store is made at start: a "created" or "uploaded" time there would be the start time of the app
    c = demo_client(tmp_path, key=False)
    for path in ("/cv/1", "/cv/1/text", "/cv/1/paths", "/postings"):
        for headers in ({}, DE):
            text = plain(c.get(path, headers=headers).text)
            assert not re.search(r"\d\d\.\d\d\.\d{4} \d\d:\d\d", text), (path, headers)
    assert "profile created" not in plain(c.get("/cv/1").text) and "Seen on" not in c.get("/postings").text


def test_person_label():
    assert person_label("maria_berger.md") == "Maria Berger" and person_label(NAME) == NAME
