"""Two equally plausible entry points, neither named after the package."""


def mean_of(values: list[float]) -> float:
    """Arithmetic mean of a list of numbers.

    >>> mean_of([1.0, 2.0, 3.0])
    2.0
    """
    return sum(values) / len(values)


def median_of(values: list[float]) -> float:
    """Median of a list of numbers.

    >>> median_of([3.0, 1.0, 2.0])
    2.0
    """
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
