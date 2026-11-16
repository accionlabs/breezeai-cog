"""What a Groovy file sees of the repository: its Groovy classes, then its Java classes.

Groovy and Java compile together, so Groovy code imports, calls and extends the repository's Java
classes. The Groovy parser declares ``related_languages = ("java",)``, and the pipeline hands it
the Java index it already built (``ParseContext.related_indexes``); nothing is parsed twice. This
module combines the two indexes into the view one Groovy file resolves with, own language first:

* **imports:** a fully-qualified name is looked up among the Groovy classes, then the Java ones
  (``import com.acme.DataApiClient`` → ``DataApiClient.java``); the same-package rule applies to
  both, as Groovy needs no import for either.
* **classes:** a simple name declared in only one of the languages takes that language's entry.
  One both declare (``collisions``) means the Groovy class, as it did before Java was read —
  unless the file's import or package binds it to the Java class: in that file it is ambiguous
  (:func:`file_heritage`), so a call checks only the methods the class declares in the file the
  binding names (``callresolve.py``'s ``unknown_methods``), never the other language's class. A
  Java class whose parent's name collides ends its chain there.
* **parents:** a Groovy class whose base is no Groovy class keeps the base's candidate FQCNs
  (``heritage.foreign_bases``); exactly one of them among the Java classes makes it the class's
  parent, so ``job.run()`` reaches the Java ``AbstractBatch`` that declares ``run``.

A Java class never sees the Groovy ones: the relation is one-way.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from typing import TypeVar

from ..index_common import ClassHeritage
from ..java.imports import JavaIndex
from .imports import GroovyIndex

_Key = tuple[str, str]


@dataclass(frozen=True, slots=True)
class GroovyView:
    """The indexes one Groovy file resolves with."""

    fqcn: dict[str, str | None]
    heritage: Mapping[str, ClassHeritage | None]
    unknown_methods: Mapping[_Key, frozenset[str] | None]
    #: simple names a Groovy and a Java class of the repository share
    collisions: frozenset[str] = frozenset()


class _DeclaredIn(Mapping[_Key, "frozenset[str] | None"]):
    """``(file, class simple name)`` → the methods that class declares in that file, answered from
    the index of the file's language: its ``unknown_methods`` for a class that index cannot
    describe, else the methods of the class's entry that map to that file (a name both languages
    declare is described in each). Iterating lists only the indexes' ``unknown_methods`` keys."""

    def __init__(self, groovy: GroovyIndex, java: JavaIndex) -> None:
        self._groovy, self._java = groovy, java

    def __getitem__(self, key: _Key) -> frozenset[str] | None:
        rel, name = key
        side: GroovyIndex | JavaIndex = self._java if rel.endswith(".java") else self._groovy
        if key in side.unknown_methods:
            return side.unknown_methods[key]
        heritage = side.class_heritage.get(name)
        if heritage is None:
            raise KeyError(key)
        return frozenset(m for m, where in heritage.methods.items() if where == rel)

    def __iter__(self) -> Iterator[_Key]:
        yield from self._groovy.unknown_methods
        yield from self._java.unknown_methods

    def __len__(self) -> int:
        return len(self._groovy.unknown_methods) + len(self._java.unknown_methods)


K = TypeVar("K")
V = TypeVar("V")


class Overlay(Mapping[K, V]):
    """A read-only view of ``base`` with ``top``'s entries in front (a file's import aliases over
    the repository index), without copying ``base``."""

    def __init__(self, top: Mapping[K, V], base: Mapping[K, V]) -> None:
        self._top, self._base = top, base

    def __getitem__(self, key: K) -> V:
        return self._top[key] if key in self._top else self._base[key]

    def __contains__(self, key: object) -> bool:
        return key in self._top or key in self._base

    def __iter__(self) -> Iterator[K]:
        yield from self._top
        yield from (k for k in self._base if k not in self._top)

    def __len__(self) -> int:
        return sum(1 for _ in self)


def _combine(groovy: GroovyIndex, java: JavaIndex) -> GroovyView:
    fqcn: dict[str, str | None] = dict(java.fqcn)
    fqcn.update(groovy.fqcn)  # own language first
    collisions = frozenset(groovy.class_heritage.keys() & java.class_heritage.keys())
    heritage: dict[str, ClassHeritage | None] = {}
    for name, entry in java.class_heritage.items():
        if entry is not None and entry.extends in collisions:  # its parent's name means Groovy's
            entry = replace(entry, extends=None)
        heritage[name] = entry
    heritage.update(groovy.class_heritage)  # own language first
    for name, candidates in groovy.foreign_bases.items():
        entry = heritage.get(name)
        if entry is None or entry.extends is not None or candidates is None:
            continue
        hits = [c for c in candidates if java.fqcn.get(c) is not None]
        base = hits[0].rsplit(".", 1)[-1] if len(hits) == 1 else None
        if base is not None and base not in collisions:
            heritage[name] = replace(entry, extends=base)
    return GroovyView(fqcn, heritage, _DeclaredIn(groovy, java), collisions)


def file_heritage(
    view: GroovyView, bindings: Mapping[str, str], aliases: Mapping[str, str],
) -> Mapping[str, ClassHeritage | None]:
    """The class entries one file resolves with: ``view.heritage``, with each import alias
    (``import a.Foo as Bar`` → ``{"Bar": "Foo"}``) looked up as its class, and each name the file
    binds to a Java class whose simple name a Groovy class shares made ambiguous."""
    def java_bound(name: str, real: str) -> bool:
        return real in view.collisions and bindings.get(name, "").endswith(".java")

    known = view.heritage
    top: dict[str, ClassHeritage | None] = {
        name: None for name in bindings if java_bound(name, name)}
    for alias, real in aliases.items():
        if real in known:
            top[alias] = None if java_bound(alias, real) else known[real]
    return Overlay(top, known) if top else known


#: The last view built, with the indexes it was built from: a worker parses every file with the
#: same indexes, so the view is built once per worker.
_last: tuple[GroovyIndex, JavaIndex, GroovyView] | None = None


def groovy_view(groovy: GroovyIndex, java: object) -> GroovyView:
    """The view of ``groovy`` alone, or combined with ``java`` when that is the Java index."""
    global _last
    if not isinstance(java, JavaIndex):
        return GroovyView(groovy.fqcn, groovy.class_heritage, groovy.unknown_methods)
    if _last is None or _last[0] is not groovy or _last[1] is not java:
        _last = (groovy, java, _combine(groovy, java))
    return _last[2]
