"""A registry that depends on shapes via constructor injection."""

from __future__ import annotations

from shapes import Circle, Shape


class ShapeRegistry:
    """Holds shapes and reports on them."""

    def __init__(self, default_shape: Shape) -> None:
        self.default_shape = default_shape
        self.items: list[Shape] = []

    def add(self, shape: Shape) -> None:
        self.items.append(shape)

    def biggest(self) -> Shape:
        return max(self.items, key=lambda shape: shape.area())


def build_default() -> ShapeRegistry:
    """Factory used by the tests."""
    return ShapeRegistry(Circle(1.0, None))
