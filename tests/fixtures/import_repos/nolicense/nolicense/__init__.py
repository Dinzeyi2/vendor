"""Test fixture with no license file: one documented function, to prove a license never blocks an import."""


def nolicense(x: float, y: float, weight: float = 1.0) -> float:
    """Weighted score: x * y * weight + 1."""
    return x * y * weight + 1
