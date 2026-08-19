"""A terminal response to a request whose body was never read must close the connection.

BaseHTTPRequestHandler frames the next request at the byte after the current response.  When a
handler answers a POST without reading its declared Content-Length body, those body bytes stay in
the socket and the parser reads them as the next request line.  A large leftover body produces a
bogus ``414 Request-URI Too Long``; a small leftover body without a newline hangs the connection
until the peer gives up.  Both are framing defects, not parser-limit defects, so the regression is
the connection state after the response and never a widened request-line limit.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from yolomux_lib import common
from yolomux_lib import http_routes
from yolomux_lib import server_auth
from yolomux_lib.server import TmuxWebtermHTTPServer


pytestmark = pytest.mark.socket

_USERNAME = "framing-admin"
_PASSWORD = "framing-password"

# Larger than the 65,537 bytes BaseHTTPRequestHandler reads before it emits 414, so a leftover body
# of this size reproduces the bogus 414 rather than blocking on a missing newline.
_LARGE_BODY_BYTES = 98_258
# Smaller than that limit and free of newlines, so a leftover body of this size reproduces the hang.
_SMALL_BODY_BYTES = 2_000


def _auth_yaml() -> str:
    return f'''users:
  - username: "{_USERNAME}"
    password: "{_PASSWORD}"
    role: "admin"
'''


def _start_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app: SimpleNamespace | None = None,
    *,
    auth_yaml: str | None = None,
):
    auth_path = tmp_path / "auth.yaml"
    auth_path.write_text(_auth_yaml() if auth_yaml is None else auth_yaml, encoding="utf-8")
    monkeypatch.setattr(common, "AUTH_CONFIG_PATH", auth_path)
    monkeypatch.setattr(server_auth, "current_language_pref", lambda: "system")
    server = TmuxWebtermHTTPServer(
        ("127.0.0.1", 0),
        app if app is not None else SimpleNamespace(sessions=[], dangerously_yolo=False),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server: TmuxWebtermHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _admin_cookie(port: int) -> str:
    body = urlencode({"username": _USERNAME, "password": _PASSWORD, "next": "/api/ping"})
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", "/login", body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        response.read()
        assert response.status == HTTPStatus.SEE_OTHER, response.status
        headers = response.getheaders()
    finally:
        connection.close()
    return next(
        value.split(";", 1)[0]
        for name, value in headers
        if name.lower() == "set-cookie" and value.startswith(f"{common.AUTH_COOKIE_NAME}_{port}=")
    )


def _recv_exactly(client: socket.socket, count: int, buffer: bytearray) -> None:
    while len(buffer) < count:
        chunk = client.recv(65536)
        if not chunk:
            raise AssertionError(f"connection ended after {len(buffer)} of {count} expected bytes")
        buffer.extend(chunk)


def _read_one_response(client: socket.socket) -> tuple[str, dict[str, str]]:
    """Read exactly one HTTP/1.1 response, leaving anything after it on the socket."""
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = client.recv(65536)
        if not chunk:
            raise AssertionError(f"connection ended before response headers: {bytes(buffer)[:200]!r}")
        buffer.extend(chunk)
    head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status_line = lines[0]
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    remainder = bytearray(rest)
    _recv_exactly(client, int(headers.get("content-length", "0")), remainder)
    return status_line, headers


def _trailing_bytes_after_response(client: socket.socket, *, timeout: float = 5.0) -> bytes:
    """Return whatever the server sends after its response, or b"" when it closes the connection.

    A browser does not pipeline: it reads the response, then reuses the idle connection later.  So
    the observable that matters is the connection state once the response is complete.  b"" means
    the server closed and the next request will be framed correctly on a fresh connection.
    """
    client.settimeout(timeout)
    chunks = bytearray()
    while True:
        try:
            chunk = client.recv(65536)
        except socket.timeout as exc:
            raise AssertionError(
                f"connection hung open for {timeout}s with an unread body still in the socket; "
                f"trailing bytes so far: {bytes(chunks)[:200]!r}"
            ) from exc
        except ConnectionResetError:
            # A reset still ends the connection, but it can discard bytes the peer never read.
            return bytes(chunks) + b"<connection reset by peer>"
        if not chunk:
            return bytes(chunks)
        chunks.extend(chunk)


def _post_with_unread_body(
    port: int,
    cookie: str,
    path: str,
    body: bytes,
    *,
    read_delay: float = 0.0,
    content_length: str | None = None,
) -> tuple[str, dict[str, str], bytes]:
    client = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Cookie: {cookie}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body) if content_length is None else content_length}\r\n"
            "\r\n"
        ).encode("ascii") + body
        client.sendall(request)
        if read_delay:
            # Closing a socket that still holds unread bytes can reset the connection and discard a
            # response the peer has not collected yet.  A slow reader must still get the response.
            time.sleep(read_delay)
        status_line, headers = _read_one_response(client)
        return status_line, headers, _trailing_bytes_after_response(client)
    finally:
        client.close()


def test_unread_large_body_on_a_deleted_route_does_not_frame_a_bogus_414(monkeypatch, tmp_path):
    """A 404 for an authenticated POST must not leave a 98 KB body to be parsed as a request line."""
    server, thread = _start_server(monkeypatch, tmp_path)
    port = server.server_address[1]
    try:
        cookie = _admin_cookie(port)
        body = json.dumps({"samples": [{"cpu": index} for index in range(9_000)]}).encode("utf-8")
        body = body + b" " * max(0, _LARGE_BODY_BYTES - len(body))
        assert len(body) >= _LARGE_BODY_BYTES
        assert http_routes.route_for_request("POST", "/api/stats-history") is None
        status_line, headers, trailing = _post_with_unread_body(port, cookie, "/api/stats-history", body)
        assert str(HTTPStatus.NOT_FOUND.value) in status_line, status_line
        assert trailing == b"", f"server framed the leftover body as a request: {trailing[:200]!r}"
        assert headers.get("connection") == "close", headers
    finally:
        _stop_server(server, thread)


def test_unread_large_body_on_the_internal_error_path_does_not_frame_a_bogus_414(monkeypatch, tmp_path):
    """The 500 written by dispatch_route_response also answers before any body read."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("route failed before reading the request body")

    app = SimpleNamespace(sessions=[], dangerously_yolo=False, set_notify=explode)
    server, thread = _start_server(monkeypatch, tmp_path, app=app)
    port = server.server_address[1]
    try:
        cookie = _admin_cookie(port)
        body = b"x" * _LARGE_BODY_BYTES
        status_line, headers, trailing = _post_with_unread_body(port, cookie, "/api/notify?enabled=1", body)
        assert str(HTTPStatus.INTERNAL_SERVER_ERROR.value) in status_line, status_line
        assert trailing == b"", f"server framed the leftover body as a request: {trailing[:200]!r}"
        assert headers.get("connection") == "close", headers
    finally:
        _stop_server(server, thread)


def test_unread_small_body_does_not_hang_the_connection(monkeypatch, tmp_path):
    """A leftover body below the 414 threshold and without a newline blocks the parser forever."""
    server, thread = _start_server(monkeypatch, tmp_path)
    port = server.server_address[1]
    try:
        cookie = _admin_cookie(port)
        body = b"y" * _SMALL_BODY_BYTES
        assert b"\n" not in body
        status_line, headers, trailing = _post_with_unread_body(port, cookie, "/api/stats-history", body)
        assert str(HTTPStatus.NOT_FOUND.value) in status_line, status_line
        assert trailing == b"", f"server framed the leftover body as a request: {trailing[:200]!r}"
        assert headers.get("connection") == "close", headers
    finally:
        _stop_server(server, thread)


def test_a_post_that_reads_its_body_keeps_the_connection_alive(monkeypatch, tmp_path):
    """The fix must close only unread-body responses; a consumed body still gets keep-alive."""
    server, thread = _start_server(monkeypatch, tmp_path)
    port = server.server_address[1]
    try:
        cookie = _admin_cookie(port)
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            body = json.dumps({"settings": {}}).encode("utf-8")
            connection.request(
                "POST",
                "/api/settings",
                body=body,
                headers={"Content-Type": "application/json", "Cookie": cookie},
            )
            first = connection.getresponse()
            first.read()
            assert first.status in {HTTPStatus.OK, HTTPStatus.INTERNAL_SERVER_ERROR}, first.status
            assert first.getheader("Connection") != "close", first.getheaders()
        finally:
            connection.close()
    finally:
        _stop_server(server, thread)


def test_a_slow_client_still_receives_the_response_before_the_unread_body_close(monkeypatch, tmp_path):
    """Closing on an unread body must send a clean FIN, never a reset that eats the response."""
    server, thread = _start_server(monkeypatch, tmp_path)
    port = server.server_address[1]
    try:
        cookie = _admin_cookie(port)
        body = b"z" * _LARGE_BODY_BYTES
        status_line, headers, trailing = _post_with_unread_body(
            port, cookie, "/api/stats-history", body, read_delay=0.5
        )
        assert str(HTTPStatus.NOT_FOUND.value) in status_line, status_line
        assert trailing == b"", f"connection was not closed cleanly: {trailing[:200]!r}"
        assert headers.get("connection") == "close", headers
    finally:
        _stop_server(server, thread)


def test_unread_body_on_a_successful_query_only_post_closes_the_connection(monkeypatch, tmp_path):
    """A 200 desyncs exactly like an error: post_notify answers from the query string alone."""
    app = SimpleNamespace(
        sessions=[],
        dangerously_yolo=False,
        set_notify=lambda enabled: {"notify": bool(enabled)},
    )
    server, thread = _start_server(monkeypatch, tmp_path, app=app)
    port = server.server_address[1]
    try:
        cookie = _admin_cookie(port)
        status_line, headers, trailing = _post_with_unread_body(
            port, cookie, "/api/notify?enabled=1", b"w" * _LARGE_BODY_BYTES
        )
        assert str(HTTPStatus.OK.value) in status_line, status_line
        assert trailing == b"", f"server framed the leftover body as a request: {trailing[:200]!r}"
        assert headers.get("connection") == "close", headers
    finally:
        _stop_server(server, thread)


def test_invalid_content_length_rejection_closes_the_connection(monkeypatch, tmp_path):
    """read_request_body rejects an unparsable Content-Length without taking the bytes off the socket."""
    server, thread = _start_server(monkeypatch, tmp_path)
    port = server.server_address[1]
    try:
        cookie = _admin_cookie(port)
        status_line, headers, trailing = _post_with_unread_body(
            port, cookie, "/api/settings", b"v" * _LARGE_BODY_BYTES, content_length="not-a-number"
        )
        assert " 4" in status_line, status_line
        assert trailing == b"", f"server framed the leftover body as a request: {trailing[:200]!r}"
        assert headers.get("connection") == "close", headers
    finally:
        _stop_server(server, thread)


def test_auth_setup_page_for_an_unread_post_body_closes_the_connection(monkeypatch, tmp_path):
    """require_auth's auth-setup branch answers 200 HTML before any handler reads the body."""
    server, thread = _start_server(monkeypatch, tmp_path, auth_yaml="users: []\n")
    port = server.server_address[1]
    try:
        status_line, headers, trailing = _post_with_unread_body(
            port, "", "/api/settings", b"u" * _LARGE_BODY_BYTES
        )
        assert str(HTTPStatus.OK.value) in status_line, status_line
        assert "text/html" in headers.get("content-type", ""), headers
        assert trailing == b"", f"server framed the leftover body as a request: {trailing[:200]!r}"
        assert headers.get("connection") == "close", headers
    finally:
        _stop_server(server, thread)
