"""Resource-name inflection is exact or None — never a guessed parameter name."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.inflect import plural, singular


@pytest.mark.parametrize(("word", "expected"), [
    ("photos", "photo"), ("categories", "category"), ("people", "person"), ("boxes", "box"),
    ("addresses", "address"), ("blog-posts", "blog-post"),
    ("statuses", None), ("cases", None), ("status", None), ("news", "news"), ("data", None),
])
def test_singular(word: str, expected: str | None) -> None:
    assert singular(word) == expected


@pytest.mark.parametrize(("word", "expected"), [
    ("profile", "profiles"), ("category", "categories"), ("person", "people"), ("box", "boxes"),
])
def test_plural(word: str, expected: str) -> None:
    assert plural(word) == expected
