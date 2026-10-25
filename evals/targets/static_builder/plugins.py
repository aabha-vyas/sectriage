"""Text transforms that content files can request by name."""

from collections.abc import Callable

REGISTRY: dict[str, Callable[[str], str]] = {
    "upper": str.upper,
    "lower": str.lower,
    "strip": str.strip,
    "title": str.title,
}


def apply(name: str, text: str) -> str:
    return REGISTRY[name](text)
