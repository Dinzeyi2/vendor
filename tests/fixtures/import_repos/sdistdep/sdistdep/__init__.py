"""Fixture: depends on docopt 0.6.2, which PyPI has only as a source release (no wheel)."""
from docopt import docopt

USAGE = """Usage: greet <name> [--shout]"""


def sdistdep(argv: list) -> dict:
    """Parse a command line against the usage pattern."""
    return docopt(USAGE, argv=argv)
