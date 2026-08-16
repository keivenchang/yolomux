"""W1: pure, mode-aware ownership and listener-identity validation."""

from tools.launcher_probe import validate_identity_payload
from tools.launcher_probe import validate_owner_payload


def _managed_ok(port=7771, pid=556555):
    return {
        "status": "local",
        "owner": True,
        "current_owner": {"port": port, "pid": pid, "priority": 0},
        "refresh_queue": {"recent_pending_count": 0},
        "counters": {"owner_acquired": 0, "owner_released": 0},
    }


def test_managed_local_self_owner_passes():
    ok, reason = validate_owner_payload(_managed_ok(), port=7771, listener_pid=556555, managed=True)
    assert ok, reason


def test_managed_rejects_a_foreign_listener_pid():
    ok, reason = validate_owner_payload(_managed_ok(pid=999999), port=7771, listener_pid=556555, managed=True)
    assert not ok and "pid" in reason


def test_managed_rejects_a_wrong_port_even_on_a_200():
    ok, reason = validate_owner_payload(_managed_ok(port=7772), port=7771, listener_pid=556555, managed=True)
    assert not ok and "port" in reason


def test_managed_rejects_pending_or_released_state():
    payload = _managed_ok()
    payload["refresh_queue"]["recent_pending_count"] = 2
    ok, reason = validate_owner_payload(payload, port=7771, listener_pid=556555, managed=True)
    assert not ok and "pending" in reason


def test_managed_does_not_require_election_priority_or_acquisition():
    # priority 0 and acquisition 0 are correct for a managed local owner
    ok, reason = validate_owner_payload(_managed_ok(), port=7771, listener_pid=556555, managed=True)
    assert ok, reason


def test_shared_default_row_uses_election_contract():
    shared = {
        "status": "shared",
        "owner": True,
        "current_owner": {"port": 7770, "pid": 111, "priority": 5},
        "refresh_queue": {"recent_pending_count": 0},
        "counters": {"owner_acquired": 1, "owner_released": 0},
    }
    ok, reason = validate_owner_payload(shared, port=7770, listener_pid=111, managed=False, primary_port=7770)
    assert ok, reason
    # a follower (non-primary) shared row must NOT self-own with acquisition 1
    ok, reason = validate_owner_payload(shared, port=7772, listener_pid=111, managed=False, primary_port=7770)
    assert not ok


def test_malformed_payload_fails_closed():
    ok, reason = validate_owner_payload("not-an-object", port=7771, listener_pid=1, managed=True)
    assert not ok


def test_listener_identity_accepts_the_exact_process_checkout_and_build():
    payload = {
        "ok": True,
        "pid": 77882,
        "repo_root": "/repo/yolomux.dev8882",
        "version": "0.7.7",
        "client_revision": "123-456",
    }

    ok, reason = validate_identity_payload(
        payload,
        listener_pid=77882,
        expected_repo_root="/repo/yolomux.dev8882",
        expected_version="0.7.7",
        expected_client_revision="123-456",
    )

    assert ok, reason


def test_listener_identity_rejects_a_healthy_response_from_the_wrong_checkout():
    payload = {
        "ok": True,
        "pid": 77882,
        "repo_root": "/repo/unrelated",
        "version": "0.7.7",
        "client_revision": "123-456",
    }

    ok, reason = validate_identity_payload(
        payload,
        listener_pid=77882,
        expected_repo_root="/repo/yolomux.dev8882",
        expected_version="0.7.7",
        expected_client_revision="123-456",
    )

    assert not ok
    assert "repo_root" in reason


def test_listener_identity_rejects_stale_backend_or_bundle_identity():
    expected = {
        "ok": True,
        "pid": 77882,
        "repo_root": "/repo/yolomux.dev8882",
        "version": "0.7.7",
        "client_revision": "123-456",
    }
    for field, stale in (("pid", 1), ("version", "0.7.6"), ("client_revision", "old-bundle")):
        payload = {**expected, field: stale}
        ok, reason = validate_identity_payload(
            payload,
            listener_pid=77882,
            expected_repo_root="/repo/yolomux.dev8882",
            expected_version="0.7.7",
            expected_client_revision="123-456",
        )
        assert not ok
        assert field in reason
