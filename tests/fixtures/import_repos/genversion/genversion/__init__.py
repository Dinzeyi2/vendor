"""Fixture: imports a module that only exists after a build (like a generated _version.py)."""
from ._version import version as __version__


def genversion(x: float) -> float:
    """Double a number."""
    return 2 * x
