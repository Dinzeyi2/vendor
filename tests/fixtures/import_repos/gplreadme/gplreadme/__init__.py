"""Test fixture shaped like a GPL-2.0-or-later project whose copyright holders are named in its README."""


def gplreadme(x: float, y: float, weight: float = 1.0) -> float:
    """Weighted score: x * y * weight + 1."""
    return x * y * weight + 1
