import base64

from webkit import make_client

from skillpose.web.security import bind_error, host_names

# a writing route that changes nothing without open postings (303 to the postings): the guard answers before it
PROBE = "/postings/analyze"


def test_foreign_host_is_rejected(tmp_path):
    assert make_client(tmp_path).get("/", headers={"host": "evil.example"}).status_code == 400


def test_cross_site_post_is_rejected(tmp_path):
    c = make_client(tmp_path)
    assert c.post(PROBE, headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post(PROBE, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.post(PROBE, headers={"origin": "http://testserver"}, follow_redirects=False).status_code == 303


def test_origin_must_match_host_and_port(tmp_path):
    c = make_client(tmp_path)
    for origin in ["null", "http://testserver:8080", "http://testserver.evil.example", "file://", "testserver"]:
        assert c.post(PROBE, headers={"origin": origin}).status_code == 403, origin
    for origin in ["http://testserver:80", "HTTP://TESTSERVER"]:
        assert c.post(PROBE, headers={"origin": origin}, follow_redirects=False).status_code == 303, origin
    ok = c.post(PROBE, headers={"sec-fetch-site": "same-origin"}, follow_redirects=False)
    assert ok.status_code == 303


def test_origin_without_port_in_host_uses_the_default_port_of_the_origin(tmp_path):
    # behind a TLS proxy the Host header has no port and the origin is https
    c = make_client(tmp_path)
    r = c.post(PROBE, headers={"origin": "https://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.post(PROBE, headers={"origin": "https://testserver:8443"}, follow_redirects=False)
    assert r.status_code == 403


def test_password_protects_everything_but_healthz(tmp_path):
    c = make_client(tmp_path, password="pw")
    r = c.get("/")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert c.get("/", auth=("x", "pw")).status_code == 200 and c.get("/healthz").status_code == 200


def test_wrong_or_broken_credentials_are_refused(tmp_path):
    c = make_client(tmp_path, password="pw")
    assert c.get("/", auth=("x", "wrong")).status_code == 401
    assert c.get("/static/style.css").status_code == 401
    assert c.post(PROBE).status_code == 401
    for value in ["Basic !!!", "Bearer pw", "Basic " + base64.b64encode(b"no-colon").decode()]:
        assert c.get("/", headers={"authorization": value}).status_code == 401, value


def test_large_body_is_rejected(tmp_path):
    r = make_client(tmp_path).post(PROBE, content=b"x" * (7 * 1024 * 1024))
    assert r.status_code == 413


def test_body_without_length_is_rejected(tmp_path):
    c = make_client(tmp_path)
    assert c.post(PROBE, content=(part for part in [b"x", b"y"])).status_code == 411
    assert c.post(PROBE, headers={"content-length": "abc"}).status_code == 400


def test_pages_cannot_be_framed_by_another_site(tmp_path):
    r = make_client(tmp_path).get("/")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_bind_rules():
    assert bind_error("127.0.0.1", None, True) is None and bind_error("0.0.0.0", None, False) is None
    assert "APP_PASSWORD" in bind_error("0.0.0.0", None, True) and bind_error("0.0.0.0", "pw", True) is None
    assert bind_error("::1", None, True) is None and bind_error("localhost", None, True) is None


def test_host_names_of_a_bind_address():
    assert host_names("127.0.0.1") == ["127.0.0.1"] and host_names("::1") == ["[::1]"]
    assert host_names("0.0.0.0") == [] and host_names("::") == [] and host_names("myhost.example") == []
