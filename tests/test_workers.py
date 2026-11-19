import sys

import pytest

from app.workers import WorkerFailure, WorkerProcess


@pytest.fixture
def worker():
    w = WorkerProcess("fake", [sys.executable, "-m", "app.worker_main", "tests.fake_worker_ops"])
    yield w
    w.kill()


def test_echo_survives_stdout_noise(worker):
    out = worker.call("echo", {"x": 1}, timeout=30)
    assert out["echo"] == {"x": 1}


def test_input_and_library_errors_keep_worker_alive(worker):
    pid = worker.call("echo", {}, timeout=30)["pid"]
    with pytest.raises(WorkerFailure) as e:
        worker.call("bad", {}, timeout=10)
    assert e.value.error_type == "invalid_input"
    with pytest.raises(WorkerFailure) as e:
        worker.call("boom", {}, timeout=10)
    assert e.value.error_type == "operation_failed"
    assert worker.call("echo", {}, timeout=10)["pid"] == pid


def test_timeout_kills_and_next_call_restarts(worker):
    pid = worker.call("echo", {}, timeout=30)["pid"]
    with pytest.raises(WorkerFailure) as e:
        worker.call("sleep", {"seconds": 30}, timeout=1)
    assert e.value.error_type == "timeout"
    assert not worker.alive
    assert worker.call("echo", {}, timeout=30)["pid"] != pid


def test_crash_is_reported_and_recovered(worker):
    with pytest.raises(WorkerFailure) as e:
        worker.call("crash", {}, timeout=30)
    assert e.value.error_type == "worker_crashed"
    assert worker.call("echo", {"ok": True}, timeout=30)["echo"] == {"ok": True}
