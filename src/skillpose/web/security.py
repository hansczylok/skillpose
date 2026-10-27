"""Guards of the web app: who may listen where, and which requests reach a route.

The app runs on the user's machine with a paid API key. A page of another site must not make the browser send a form
to it (cross-site request), frame it to steal a click (clickjacking), or reach it under a foreign host name (DNS
rebinding, checked before this guard by Starlette's TrustedHostMiddleware). This module needs no web library, so the
command line can check the bind rule before the web extra is loaded.
"""

import base64
import binascii
import ipaddress
import re
import secrets
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import urlsplit

Scope = MutableMapping[str, Any]
ASGIApp = Callable[[Scope, Callable, Callable], Awaitable[None]]
Lock = Callable[[Scope], ASGIApp | None]  # a response to send instead of the route, or None: the request may pass

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_BODY_BYTES = 6 * 1024 * 1024  # uploads are limited to 5 MB; the rest is room for the multipart framing
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DEFAULT_PORTS = {"http": 80, "https": 443}
# scripts only from /static (no inline script); inline styles stay allowed for the width of HTML bars
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'; "
       "base-uri 'none'; object-src 'none'")
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),  # also: no request to another host
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
]


def bind_error(host: str, password: str | None, has_key: bool) -> str | None:
    """Why the app must not listen on host: reachable from other machines with a key and without a password."""
    if host in LOOPBACK or not has_key or password:
        return None
    return (f"refusing to listen on {host}: with an API key, every address other than 127.0.0.1 needs a password - "
            "set APP_PASSWORD (or use --host 127.0.0.1)")


def hosts_error(hosts: list[str], password: str | None) -> str | None:
    """Why the app must not accept these extra host names: a wildcard ("*", "*.example.org") lets foreign names pass
    the host check, so without a password DNS rebinding reaches the app again (with or without a key: the database
    may hold own CVs)."""
    wild = [h for h in hosts if "*" in h]
    if not wild or password:
        return None
    return (f"refusing SKILLPOSE_ALLOWED_HOSTS {', '.join(wild)}: a wildcard lets foreign host names through (DNS "
            "rebinding) - name each host, or set APP_PASSWORD")


def host_names(bind: str) -> list[str]:
    """The Host header names of a bind address that may be allowed without SKILLPOSE_ALLOWED_HOSTS.

    Only an IP literal: DNS cannot point it at another server. A wildcard address and host names give none.
    """
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError:
        return []
    if ip.is_unspecified:
        return []
    return [f"[{ip}]" if ip.version == 6 else str(ip)]


def no_lock(scope: Scope) -> None:
    """The lock that lets every request pass."""
    return None


def writing_request(scope: Scope) -> bool:
    """A request the demo lock looks at: every method but GET/HEAD/OPTIONS."""
    return scope["method"] not in SAFE_METHODS


def _values(scope: Scope, name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == name]


def _authorized(scope: Scope, password: str) -> bool:
    """HTTP Basic auth with any user name and this password."""
    for value in _values(scope, b"authorization"):
        scheme, _, token = value.strip().partition(" ")
        if scheme.lower() != "basic":
            continue
        try:
            decoded = base64.b64decode(token.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        _, colon, given = decoded.partition(b":")
        return bool(colon) and secrets.compare_digest(given, password.encode("utf-8"))
    return False


def _same_origin(origin: str, host: str) -> bool:
    """Origin names the host and port of the Host header. A Host without port means the default port of the origin's
    scheme, so the check also holds behind a TLS proxy that talks plain HTTP to the app."""
    try:
        o, h = urlsplit(origin), urlsplit("//" + host)
        default = DEFAULT_PORTS.get(o.scheme)
        if default is None or not o.hostname or not h.hostname:
            return False
        return o.hostname == h.hostname and (o.port or default) == (h.port or default)
    except ValueError:  # a port that is no number
        return False


def _refusal(scope: Scope, password: str | None) -> tuple[int, str, list[tuple[bytes, bytes]]] | None:
    if password is not None and scope["path"] != "/healthz" and not _authorized(scope, password):
        return 401, "password required", [(b"www-authenticate", b'Basic realm="skillpose"')]
    if scope["method"] not in SAFE_METHODS:
        if "cross-site" in (v.strip().lower() for v in _values(scope, b"sec-fetch-site")):
            return 403, "cross-site request refused", []
        hosts = _values(scope, b"host")
        if any(not hosts or not _same_origin(origin.strip(), hosts[0]) for origin in _values(scope, b"origin")):
            return 403, "request from another origin refused", []
    close = [(b"connection", b"close")]
    lengths = _values(scope, b"content-length")
    if any(not re.fullmatch(r"[0-9]+", v.strip()) for v in lengths):
        return 400, "invalid content-length", close
    if any(int(v) > MAX_BODY_BYTES for v in lengths):
        return 413, f"request body larger than {MAX_BODY_BYTES // (1024 * 1024)} MB", close
    if _values(scope, b"transfer-encoding"):
        # a body without a declared length: browsers always declare it, and counting a streamed body would need
        # this guard to read it
        return 411, "content-length required", close
    return None


def _with_security_headers(send: Callable) -> Callable:
    async def wrapped(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            present = {key.lower() for key, _ in headers}
            headers += [(key, value) for key, value in SECURITY_HEADERS if key not in present]
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


class GuardMiddleware:
    """Pure ASGI middleware; it reads headers only, never the body. In this order: (a) the password (all but
    /healthz), (b) for every method but GET/HEAD/OPTIONS: no cross-site request, the Origin matches the Host,
    (c) the declared body size, (d) the lock (the demo lock), which may answer instead of the route."""

    def __init__(self, app: ASGIApp, *, password: str | None = None, lock: Lock = no_lock):
        self.app = app
        self.password = password or None
        self.lock = lock

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        send = _with_security_headers(send)
        if (refusal := _refusal(scope, self.password)) is not None:
            status, text, headers = refusal
            body = text.encode("utf-8")
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode()), *headers]})
            await send({"type": "http.response.body", "body": body})
            return
        if (response := self.lock(scope)) is not None:
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
