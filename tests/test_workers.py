import logging
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


def _wait_for(predicate, seconds=10):
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end and not predicate():
        time.sleep(0.05)


def test_secrets_not_passed_to_worker_and_redacted_in_logs(worker, monkeypatch, caplog):
    secret = "s3cr3t-value-0123456789"
    monkeypatch.setenv("API_KEY", secret)
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    out = worker.call("env_probe", {"secret": secret}, timeout=30)
    assert out["api_key_visible"] is False
    _wait_for(lambda: "stderr line with secret" in caplog.text)
    assert "stderr line with secret [REDACTED]" in caplog.text
    assert secret not in caplog.text


def test_clean_exit_logs_code_reason_and_stderr(worker, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    with pytest.raises(WorkerFailure) as e:
        worker.call("exit_clean", {}, timeout=30)
    assert e.value.error_type == "worker_crashed"
    assert "code 0" in str(e.value) and "SystemExit" in str(e.value)
    _wait_for(lambda: "exited unexpectedly" in caplog.text)
    assert "worker fake exited unexpectedly with exit code 0" in caplog.text
    assert "worker tests.fake_worker_ops: exiting: SystemExit" in caplog.text


def test_reader_survives_nonblocking_pipe():
    """Reproduces Julia/libuv switching the request pipe to non-blocking mode."""
    import os
    import threading
    import time

    from app.worker_main import _read_lines

    r, w = os.pipe()
    os.set_blocking(r, False)  # what libuv does to fds it adopts
    notes = []

    def writer():
        time.sleep(0.3)  # request arrives after the worker is already waiting
        os.write(w, b'{"a": 1}\n{"b": 2}\n')
        os.close(w)

    threading.Thread(target=writer).start()
    assert list(_read_lines(r, notes.append)) == [b'{"a": 1}', b'{"b": 2}']
    assert notes and "non-blocking" in notes[0]
    os.close(r)


def test_crash_is_reported_and_recovered(worker):
    with pytest.raises(WorkerFailure) as e:
        worker.call("crash", {}, timeout=30)
    assert e.value.error_type == "worker_crashed"
    assert worker.call("echo", {"ok": True}, timeout=30)["echo"] == {"ok": True}
