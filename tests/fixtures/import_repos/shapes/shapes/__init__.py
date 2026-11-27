"""Fixture: only a class (no plain function), like many object-oriented libraries."""


class Polygon:
    def __init__(self, points):
        self.points = [tuple(p) for p in points]

    @property
    def area(self):
        pts = self.points
        return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2
