#!/usr/bin/env python3
"""Thin authenticated launcher probes (owner, sessions).

This assumes its process environment is ALREADY the resolved row environment
(applied through `tools/instance_isolation.py exec`); it does not resolve, scrub,
or mutate the environment, and it holds no second root resolver. It exposes two
subcommands used by the supported launcher after a server is up:

  owner   --port P --listener-pid N   verify this row owns itself correctly
  sessions --port P                   authenticated session/transcript discovery
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import sys
from typing import Any, Mapping

# This script runs as `python3 tools/launcher_probe.py ...` (its own directory is
# sys.path[0], not the repo root), so put the repo root ahead of that before the
# product imports. The launcher invokes it through `instance_isolation.py exec`,
# which has already applied this row's resolved environment; these imports then
# read the auth config INSIDE that row's root, never the operator's.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from yolomux_lib.common import auth_cookie_value
from yolomux_lib.common import current_auth_users
from yolomux_lib.common import PROJECT_ROOT
from yolomux_lib.common import YOLOMUX_VERSION
from yolomux_lib.common import yolomux_client_revision
from tools.instance_isolation import is_managed_instance_port


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def validate_owner_payload(payload: Mapping[str, Any], *, port: int, listener_pid: int, managed: bool, primary_port: int | None = None) -> tuple[bool, str]:
    """Pure, mode-aware ownership check over an /api/background/status payload.

    Managed rows are their own local owner (DisabledBackgroundOwner): require
    status=local, owner=true, current_owner.port==port, current_owner.pid==the
    unique listener PID, and zero pending/release; priority and acquisition
    counters do NOT apply. Shared/default rows keep the election contract against
    the configured primary. A 200 with the wrong identity is still a failure."""
    if not isinstance(payload, Mapping):
        return False, "owner status payload must be an object"
    owner = payload.get("current_owner") if isinstance(payload.get("current_owner"), Mapping) else {}
    queue = payload.get("refresh_queue") if isinstance(payload.get("refresh_queue"), Mapping) else {}
    counters = payload.get("counters") if isinstance(payload.get("counters"), Mapping) else {}

    if managed:
        checks = (
            (payload.get("status") == "local", "managed row status must be local"),
            (payload.get("owner") is True, "managed row must be its own owner"),
            (_int(owner.get("port")) == port, f"current_owner.port must be {port}"),
            (_int(owner.get("pid")) == listener_pid, f"current_owner.pid must equal the unique listener pid {listener_pid}"),
            (_int(queue.get("recent_pending_count")) == 0, "managed row must have no pending refreshes"),
            (_int(counters.get("owner_released")) == 0, "managed row must have no owner releases"),
        )
    else:
        expected_primary = int(primary_port) if primary_port is not None else port
        self_owner = payload.get("owner") is True
        expected_self = port == expected_primary
        checks = (
            (_int(owner.get("port")) == expected_primary, f"shared row owner.port must be {expected_primary}"),
            (_int(owner.get("priority")) > 0, "shared row owner priority must be positive"),
            (self_owner == expected_self, f"shared row self-ownership must be {expected_self}"),
            (_int(counters.get("owner_acquired")) == (1 if expected_self else 0), "shared row acquisition count is wrong"),
            (_int(counters.get("owner_released")) == 0, "shared row must have no owner releases"),
            (_int(queue.get("recent_pending_count")) == 0, "shared row must have no pending refreshes"),
        )
    for ok, reason in checks:
        if not ok:
            return False, reason
    return True, "ok"


def validate_identity_payload(
    payload: Mapping[str, Any],
    *,
    listener_pid: int,
    expected_repo_root: str,
    expected_version: str,
    expected_client_revision: str,
) -> tuple[bool, str]:
    """Require the protected ping to identify the exact listener checkout and browser build."""
    if not isinstance(payload, Mapping):
        return False, "identity payload must be an object"
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    checks = (
        (data.get("ok") is True, "ok must be true"),
        (_int(data.get("pid")) == listener_pid, f"pid must equal the unique listener pid {listener_pid}"),
        (str(data.get("repo_root") or "") == expected_repo_root, f"repo_root must be {expected_repo_root}"),
        (str(data.get("version") or "") == expected_version, f"version must be {expected_version}"),
        (
            str(data.get("client_revision") or "") == expected_client_revision,
            f"client_revision must be {expected_client_revision}",
        ),
    )
    for ok, reason in checks:
        if not ok:
            return False, reason
    return True, "ok"


def _row_cookie_header(port: int) -> dict[str, str]:
    """Mint an admin cookie from THIS row's own auth config (the one inside the
    resolved root the exec step applied). When the row runs with the test auth
    bypass and has no configured users, there is nothing to mint and the request
    proceeds without a cookie; a live row always has a user, so this fails visibly
    at the endpoint if authentication is actually required and no user exists."""
    users = [user for user in current_auth_users() if user.role == "admin"] or list(current_auth_users())
    if not users:
        return {}
    user = users[0]
    return {"Cookie": f"yolomux_auth_{port}={auth_cookie_value(user.username, user.password)}"}


def _get_json(scheme: str, host: str, port: int, path: str, headers: Mapping[str, str], *, timeout: float = 5.0) -> Any:
    url = f"{scheme}://{host}:{port}{path}"
    request = urllib.request.Request(url, headers=dict(headers))
    context = ssl._create_unverified_context() if scheme == "https" else None
    with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
        return json.load(response)


def _get_bytes(scheme: str, host: str, port: int, path: str, headers: Mapping[str, str], *, timeout: float = 5.0) -> bytes:
    url = f"{scheme}://{host}:{port}{path}"
    request = urllib.request.Request(url, headers=dict(headers))
    context = ssl._create_unverified_context() if scheme == "https" else None
    with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
        return response.read()


def _probe_identity(args: argparse.Namespace) -> int:
    """Prove the unique listener is this checkout and serves this checkout's browser bundle."""
    headers = _row_cookie_header(args.port)
    expected_bundle = (_REPO_ROOT / "static" / "yolomux.js").read_bytes()
    try:
        served_bundle = _get_bytes(args.scheme, args.host, args.port, "/static/yolomux.js", headers)
    except OSError as error:
        print(f"identity {args.port} bundle read failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    if hashlib.sha256(served_bundle).digest() != hashlib.sha256(expected_bundle).digest():
        print(f"identity {args.port} rejected: served bundle does not match {_REPO_ROOT}", file=sys.stderr)
        return 1
    try:
        payload = _get_json(args.scheme, args.host, args.port, "/api/ping", headers)
    except urllib.error.HTTPError as error:
        if error.code == 401 and not headers:
            print(f"identity {args.port}: bundle verified; protected identity pending auth setup")
            return 0
        print(f"identity {args.port} ping failed: HTTP {error.code}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"identity {args.port} ping failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    ok, reason = validate_identity_payload(
        payload,
        listener_pid=args.listener_pid,
        expected_repo_root=str(PROJECT_ROOT),
        expected_version=YOLOMUX_VERSION,
        expected_client_revision=yolomux_client_revision(),
    )
    if not ok:
        print(f"identity {args.port} rejected: {reason}", file=sys.stderr)
        return 1
    print(f"identity {args.port}: pid {args.listener_pid}, version {YOLOMUX_VERSION}, checkout and bundle verified")
    return 0


def _probe_owner(args: argparse.Namespace) -> int:
    """Authenticated ownership verification for one row, evaluated through the one
    shared `validate_owner_payload`. Managed rows are read from the applied row
    descriptor (never a second flag), and the check is bound to the exact unique
    listener PID the launcher measured. Polls for three stable acceptances so a
    momentary election/startup transient is not read as a healthy owner."""
    managed = is_managed_instance_port(args.port)
    primary_port = args.primary_port if args.primary_port is not None else args.port
    headers = _row_cookie_header(args.port)
    stable = 0
    last = ""
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            payload = _get_json(args.scheme, args.host, args.port, "/api/background/status", headers)
        except (OSError, ValueError) as error:
            stable = 0
            last = f"{type(error).__name__}: {error}"
            time.sleep(2)
            continue
        ok, reason = validate_owner_payload(
            payload, port=args.port, listener_pid=args.listener_pid, managed=managed, primary_port=primary_port,
        )
        last = ("ok: " if ok else "reject: ") + reason
        stable = stable + 1 if ok else 0
        if stable >= 3:
            print(f"owner {args.port} ({'managed' if managed else 'shared'}): {reason}")
            return 0
        time.sleep(2)
    print(f"owner {args.port} did not stabilize: {last}", file=sys.stderr)
    return 1


def _probe_sessions(args: argparse.Namespace) -> int:
    """Authenticated session discovery for one row: query the read-only
    `/api/tmux-session-exists` for every expected tmux session and fail loudly if
    any is missing. Never calls `/api/transcripts?force=1`, which would multiply
    transcript/repository warmups across every restarted port."""
    expected = [name for name in args.sessions.split(",") if name]
    if not expected:
        print("sessions: no expected sessions supplied", file=sys.stderr)
        return 2
    headers = _row_cookie_header(args.port)
    last = ""
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            missing = []
            for session in expected:
                query = urllib.parse.urlencode({"session": session})
                payload = _get_json(args.scheme, args.host, args.port, f"/api/tmux-session-exists?{query}", headers)
                if payload.get("exists") is not True:
                    missing.append(session)
            if not missing:
                print(f"sessions {args.port}: all {len(expected)} visible")
                return 0
            last = "missing " + ",".join(missing)
        except (OSError, ValueError) as error:
            last = f"{type(error).__name__}: {error}"
        time.sleep(1)
    print(f"sessions {args.port} incomplete: {last}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="launcher_probe", description="authenticated launcher row probes")
    parser.add_argument("--scheme", choices=("https", "http"), default="https")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--timeout", type=float, default=60.0)
    subparsers = parser.add_subparsers(dest="command", required=True)

    owner = subparsers.add_parser("owner", help="verify this row owns itself correctly")
    owner.add_argument("--port", type=int, required=True)
    owner.add_argument("--listener-pid", type=int, required=True)
    owner.add_argument("--primary-port", type=int, default=None)
    owner.set_defaults(handler=_probe_owner)

    identity = subparsers.add_parser("identity", help="verify the listener checkout, version, and served bundle")
    identity.add_argument("--port", type=int, required=True)
    identity.add_argument("--listener-pid", type=int, required=True)
    identity.set_defaults(handler=_probe_identity)

    sessions = subparsers.add_parser("sessions", help="authenticated tmux-session discovery")
    sessions.add_argument("--port", type=int, required=True)
    sessions.add_argument("--sessions", required=True, help="comma-separated expected session names")
    sessions.set_defaults(handler=_probe_sessions)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
