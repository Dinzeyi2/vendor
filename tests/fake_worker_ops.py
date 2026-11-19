"""Operations used only by tests/test_workers.py to exercise worker failure handling."""
import os
import time

from app.errors import OperationError


def echo(inputs):
    print("noise on stdout must not break the protocol")
    return {"echo": inputs, "pid": os.getpid()}


def sleep(inputs):
    time.sleep(inputs["seconds"])
    return {}


def crash(_inputs):
    os._exit(3)


def bad(_inputs):
    raise OperationError("bad input")


def boom(_inputs):
    raise RuntimeError("library blew up")


def env_probe(inputs):
    import sys
    print(f"stderr line with secret {inputs['secret']}", file=sys.stderr, flush=True)
    return {"api_key_visible": "API_KEY" in os.environ}


def exit_clean(_inputs):
    raise SystemExit(0)


OPERATIONS = {"echo": echo, "sleep": sleep, "crash": crash, "bad": bad, "boom": boom,
              "env_probe": env_probe, "exit_clean": exit_clean}
# Names used when tests register these as API operations (tests/test_billing.py).
OPERATIONS.update({"test.echo": echo, "test.sleep": sleep, "test.crash": crash})
