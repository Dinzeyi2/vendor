"""Test fixture shaped like a typical small algorithm library: one main function named after the
package, untyped point arguments, an Enum option, helpers, and README examples with outputs."""
from enum import Enum
from math import asin, cos, radians, sin, sqrt


class Unit(str, Enum):
    KILOMETERS = "km"
    MILES = "mi"


_RADIUS = {Unit.KILOMETERS: 6371.0088, Unit.MILES: 3958.7613}


def geodist(point1, point2, unit=Unit.KILOMETERS):
    """Great-circle distance between two (lat, lon) points in decimal degrees."""
    lat1, lon1 = point1
    lat2, lon2 = point2
    unit = Unit(unit)
    d = sin(radians(lat2 - lat1) / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * _RADIUS[unit] * asin(sqrt(d))


def geodist_many(points1, points2, unit=Unit.KILOMETERS):
    """Distances for lists of points."""
    return [geodist(a, b, unit) for a, b in zip(points1, points2)]


def save_csv(path, rows):
    """I/O helper that must never be chosen."""
    raise RuntimeError("not allowed in tests")
