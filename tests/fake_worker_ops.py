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


OPERATIONS = {"echo": echo, "sleep": sleep, "crash": crash, "bad": bad, "boom": boom}
