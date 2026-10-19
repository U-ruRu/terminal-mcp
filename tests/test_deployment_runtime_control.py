"""Local maintenance receipts own idempotence and deployment exclusion."""

import pytest

from terminal_mcp.deployment.runtime_control import (
    RuntimeControl,
    RuntimeControlError,
    deployment_lock,
)


class Backend:
    launches = 0
    restarts = 0
    failure = False

    def snapshot(self):
        return {"restart_targets": ["terminal", "tunnel"]}

    def launch(self):
        self.launches += 1

    def restart(self, target):
        self.restarts += 1
        if self.failure:
            raise OSError("operating system failure")


def test_repeated_request_survives_api_restart_without_launching_again(tmp_path):
    backend = Backend()
    request_id = "request-survives-restart"
    first = RuntimeControl(tmp_path, backend).submit("terminal", request_id)

    repeated = RuntimeControl(tmp_path, backend).submit("terminal", request_id)

    assert repeated == first
    assert backend.launches == 1


def test_reused_request_cannot_restart_a_different_service(tmp_path):
    backend = Backend()
    control = RuntimeControl(tmp_path, backend)
    control.submit("terminal", "same-request-id-123")

    with pytest.raises(RuntimeControlError, match="runtime_request_conflict"):
        control.submit("tunnel", "same-request-id-123")
    assert backend.launches == 1


@pytest.mark.parametrize("failure,status", [(False, "succeeded"), (True, "failed")])
def test_worker_persists_one_final_result(tmp_path, failure, status):
    backend = Backend()
    backend.failure = failure
    control = RuntimeControl(tmp_path, backend)
    control.submit("terminal", "request-completion-123")

    control.run_pending()
    control.run_pending()

    assert backend.restarts == 1
    assert RuntimeControl(tmp_path, backend).operation("request-completion-123")["status"] == status


def test_deployment_lock_blocks_a_new_maintenance_request(tmp_path):
    backend = Backend()
    with deployment_lock(tmp_path):
        with pytest.raises(RuntimeControlError, match="runtime_busy"):
            RuntimeControl(tmp_path, backend).submit("terminal", "deployment-excludes-123")
    assert backend.launches == 0


def test_another_pending_request_blocks_service_restart(tmp_path):
    backend = Backend()
    control = RuntimeControl(tmp_path, backend)
    control.submit("terminal", "first-request-pending")

    with pytest.raises(RuntimeControlError, match="runtime_busy"):
        control.submit("tunnel", "different-request-123")
    assert backend.launches == 1


def test_expired_unconfirmed_request_is_observed_without_automatic_replay(tmp_path):
    backend = Backend()
    control = RuntimeControl(tmp_path, backend, clock=lambda: 1000)
    control.submit("terminal", "request-worker-interrupted")

    recovered = RuntimeControl(tmp_path, backend, clock=lambda: 1200)

    assert recovered.operation("request-worker-interrupted")["status"] == "unknown"
    assert recovered.submit("terminal", "request-worker-interrupted")["status"] == "unknown"
    assert backend.launches == 1


@pytest.mark.parametrize("request_id", ["../outside", "short", "x" * 65])
def test_operation_lookup_bounds_the_durable_request_path(tmp_path, request_id):
    with pytest.raises(RuntimeControlError, match="invalid_runtime_request"):
        RuntimeControl(tmp_path, Backend()).operation(request_id)
