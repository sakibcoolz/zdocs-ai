"""Shapes: abstract base, protocol, concrete implementations, composition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol


class Renderer(Protocol):
    """Anything that can render a shape."""

    def render(self, shape: "Shape") -> str:
        ...


class Shape(ABC):
    """Abstract base for every shape."""

    sides: int

    def __init__(self, name: str, renderer: Renderer) -> None:
        self.name = name
        self._renderer = renderer
        self.__secret = "internal"

    @abstractmethod
    def area(self) -> float:
        """Area of the shape."""

    def describe(self) -> str:
        return self._renderer.render(self)


class Circle(Shape):
    """A circle."""

    def __init__(self, radius: float, renderer: Renderer) -> None:
        super().__init__("circle", renderer)
        self.radius = radius
        self.bounds = BoundingBox(radius, radius)

    def area(self) -> float:
        return 3.14159 * self.radius * self.radius


class Square(Shape):
    """A square."""

    def __init__(self, side: float, renderer: Renderer) -> None:
        super().__init__("square", renderer)
        self.side = side

    def area(self) -> float:
        return self.side * self.side


class BoundingBox:
    """Value object owned by a shape."""

    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height


def total_area(shapes: list[Shape]) -> float:
    """Sum the areas of many shapes."""
    return sum(shape.area() for shape in shapes)
