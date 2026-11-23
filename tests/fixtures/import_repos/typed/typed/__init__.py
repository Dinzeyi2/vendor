"""Fully typed function with defaults and no README examples."""
from typing import Literal


def typed(text: str, repeat: int = 2, sep: str = "-", case: Literal["upper", "lower", "keep"] = "keep") -> dict:
    """Repeat text with a separator and report its length."""
    out = sep.join([text] * repeat)
    out = out.upper() if case == "upper" else out.lower() if case == "lower" else out
    return {"text": out, "length": len(out)}
