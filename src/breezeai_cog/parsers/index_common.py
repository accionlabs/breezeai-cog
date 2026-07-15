"""Shared building blocks for repo-wide ``build_index`` pre-passes.

The one convention every language index shares is **honest-null on ambiguity**: a
repo-wide name maps to a value only while that value is unambiguous; the moment two
*differing* values are recorded under the same key, the entry collapses to ``None`` so
callers refuse to resolve through it (a wrong cross-file join is worse than a missing
one). :func:`record_distinct` is that rule, factored out of the per-language index
builders (C#/VB class heritage, TS string constants) that each hand-rolled it.
"""

from __future__ import annotations

import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Hashable, Sequence, TypeVar

from ..schemas import Decorator

try:  # fork lets workers inherit the parent's already-imported modules (no costly re-import).
    _CTX = mp.get_context("fork")  # POSIX
except ValueError:  # pragma: no cover - Windows / no fork
    _CTX = mp.get_context("spawn")

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(items: Sequence[T], fn: Callable[[T], R], jobs: int = 1) -> list[R]:
    """Map ``fn`` over ``items`` across a process pool, preserving order. Falls back to a
    plain serial map when ``jobs <= 1`` or there is at most one item (so ``--jobs 1`` stays
    single-process and deterministic). ``fn`` and every item must be picklable, so pass a
    module-level function and plain-data items. Runs before the parse pool (never concurrent
    with it), so reusing the same ``jobs`` count does not oversubscribe."""
    if jobs <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ProcessPoolExecutor(max_workers=jobs, mp_context=_CTX) as pool:
        return list(pool.map(fn, items))

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
    """One class's heritage for cross-file resolution — its base *class* (simple name), the
    raw attributes/decorators declared on it, and (for call resolution) the methods it
    declares mapped to their defining file. Language-agnostic (C#, VB.NET): ASP.NET route
    resolution walks the base chain to inherit a controller's ``[Route]`` / ``[Authorize]``,
    and the call resolver walks it to resolve inherited-method calls to the declaring file.

    ``methods`` maps method name → declaring file, or ``None`` when the name is declared in
    >1 file for this class (partial-class split / overload ambiguity → honest-null). It is
    empty for builders that don't support inherited-call resolution, so the inheritance tier
    stays inert for them."""

    extends: str | None
    decorators: list[Decorator]
    methods: dict[str, str | None] = field(default_factory=dict)


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


def merge_heritage(
    heritage: dict[str, ClassHeritage | None],
    key: str,
    extends: str | None,
    decorators: list[Decorator],
) -> None:
    """Merge one class *declaration* into ``heritage[key]``, **partial-class aware**.

    A partial declaration that omits the base clause carries ``extends is None`` — that means
    "this part didn't restate the shared base", so it **yields** to a part that names a
    concrete base rather than conflicting with it. Two parts naming *different* concrete bases
    collapse to ``None`` (genuine ambiguity, honest-null). Decorators are unioned across parts.

    ``key`` must be the class's **fully-qualified** name so that only real partials of one
    type merge; distinct types sharing a simple name are separated by :func:`project_heritage`."""
    if key not in heritage:
        heritage[key] = ClassHeritage(extends=extends, decorators=list(decorators))
        return
    existing = heritage[key]
    if existing is None:  # already ambiguous
        return
    if existing.extends is None:
        existing.extends = extends  # adopt the concrete base a later part states
    elif extends is not None and extends != existing.extends:
        heritage[key] = None  # two different concrete bases → genuine ambiguity
        return
    for d in decorators:
        if d not in existing.decorators:
            existing.decorators.append(d)


def project_heritage(by_fqn: dict[str, ClassHeritage | None]) -> dict[str, ClassHeritage | None]:
    """Project a fully-qualified-name heritage map to **simple** class names (what the resolver
    and :func:`walk_heritage` key on). Distinct types sharing a simple name are *merged*, not
    blindly dropped: they keep a shared ``extends`` (differing bases → ``None``) and per-method
    files (a method name declared in >1 file → ``None``). So same-named classes that agree
    (e.g. many controls extending one base) still resolve through that base, while genuine
    conflicts stay honest-null. (Real partials of one type are already merged within their FQN
    by :func:`merge_heritage` before this step.)"""
    out: dict[str, ClassHeritage | None] = {}
    for fqn, heritage in by_fqn.items():
        simple = fqn.rsplit(".", 1)[-1]
        if simple not in out:
            out[simple] = heritage
            continue
        cur = out[simple]
        if cur is None or heritage is None or cur.extends != heritage.extends:
            out[simple] = None  # different base (or already ambiguous) → refuse
            continue
        for m, f in heritage.methods.items():  # per-method: agree → keep, conflict → None
            cur.methods[m] = f if (m not in cur.methods or cur.methods[m] == f) else None
        for d in heritage.decorators:
            if d not in cur.decorators:
                cur.decorators.append(d)
    return out


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
