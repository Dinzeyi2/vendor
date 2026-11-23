def failing(text: str) -> int:
    """Counts words, but the documentation disagrees with the code.

    >>> failing("one two three")
    3
    """
    return len(text.split()) + 1
