"""Shared building blocks for repo-wide ``build_index`` pre-passes.

The one convention every language index shares is **honest-null on ambiguity**: a
repo-wide name maps to a value only while that value is unambiguous; the moment two
*differing* values are recorded under the same key, the entry collapses to ``None`` so
callers refuse to resolve through it (a wrong cross-file join is worse than a missing
one). :func:`record_distinct` is that rule, factored out of the per-language index
builders (C#/VB class heritage, TS string constants) that each hand-rolled it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Hashable, TypeVar

from ..schemas import Decorator

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


def record_distinct(
    mapping: dict[K, V | None],
    key: K,
    value: V,
    *,
    same: Callable[[V, V], bool] | None = None,
) -> None:
    """Record ``value`` under ``key`` in ``mapping``, collapsing to ``None`` (ambiguous)
    when a **differing** value is already present.

    The first value seen wins on a non-conflict (later equal values are ignored — same
    fact, not a conflict). Once a key is ``None`` it stays ``None``. ``same(existing,
    value)`` decides whether two values are the *same fact* (default: ``==``); pass it to
    compare on the field that matters (e.g. a class's base, ignoring incidental detail).
    """
    if key not in mapping:
        mapping[key] = value
        return
    existing = mapping[key]
    if existing is None:  # already ambiguous — never resolve through it
        return
    equal = same(existing, value) if same is not None else existing == value
    if not equal:
        mapping[key] = None


@dataclass
class ClassHeritage:
    """One class's heritage for cross-file base-class resolution — its base *class* (simple
    name) and the raw attributes/decorators declared on it. Language-agnostic (C#, VB.NET);
    ASP.NET route resolution walks this to inherit a base controller's ``[Route]`` /
    ``[Authorize]``."""

    extends: str | None
    decorators: list[Decorator]


def record_heritage(
    heritage: dict[str, ClassHeritage | None],
    name: str,
    extends: str | None,
    decorators: list[Decorator],
) -> None:
    """Record one class's heritage under its simple ``name``, collapsing to ``None`` when a
    differing declaration of the same name already exists. 'Same fact' is decided by the base
    class only — partial classes with an identical base are kept; differing decorators are not
    a conflict (see :func:`record_distinct`)."""
    record_distinct(
        heritage, name, ClassHeritage(extends=extends, decorators=decorators),
        same=lambda a, b: a.extends == b.extends,
    )


@dataclass
class HeritageChain:
    """Result of walking a class's ``extends`` chain through a repo heritage index.

    ``ancestors`` — the in-repo base classes, **nearest-first** (excludes the start class).
    ``unresolved`` — the simple name of the first base the walk could not follow further
    (declared outside the repo, or ambiguous), or ``None`` when the chain ended at an in-repo
    class with no base (or hit a cycle). ``ambiguous`` — ``True`` only when the walk stopped
    on an ambiguous (honest-null) index entry rather than an out-of-repo base."""

    ancestors: list[ClassHeritage]
    unresolved: str | None
    ambiguous: bool


def walk_heritage(
    name: str, extends: str | None, heritage_map: dict[str, ClassHeritage | None]
) -> HeritageChain:
    """Follow ``extends`` up through a ``class_heritage`` index, collecting each in-repo
    ancestor nearest-first. Cycle-guarded. Stops at the first base that is not an in-repo
    class or is ambiguous, reporting it via ``unresolved`` / ``ambiguous`` so callers stay
    honest-null about what lies beyond it (a base we cannot see may carry facts we would
    otherwise fabricate the absence of)."""
    ancestors: list[ClassHeritage] = []
    seen = {name}
    base = extends
    while base is not None:
        short = base.rsplit(".", 1)[-1].split("<", 1)[0]
        if short in seen:  # inheritance cycle (shouldn't happen in valid code) — stop
            break
        seen.add(short)
        if short not in heritage_map:  # base not declared in the repo
            return HeritageChain(ancestors, short, False)
        heritage = heritage_map[short]
        if heritage is None:  # ambiguous name — do not resolve through it
            return HeritageChain(ancestors, short, True)
        ancestors.append(heritage)
        base = heritage.extends
    return HeritageChain(ancestors, None, False)
