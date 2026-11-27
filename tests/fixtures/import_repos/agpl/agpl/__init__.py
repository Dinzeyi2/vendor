"""Test fixture under the AGPL: one documented function, to prove a license never blocks an import."""


def agpl(x: float, y: float, weight: float = 1.0) -> float:
    """Weighted score: x * y * weight + 1."""
    return x * y * weight + 1
