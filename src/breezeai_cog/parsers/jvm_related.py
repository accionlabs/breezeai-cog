"""What a Java or Groovy file sees of the repository: its own language's classes, then the other's.

Groovy and Java compile together, so each imports, calls and extends the other's classes. The
Java parser declares ``related_languages = ("groovy",)`` and the Groovy parser ``("java",)``; the
pipeline hands each the other's index it already built (``ParseContext.related_indexes``), and
nothing is parsed twice. Both indexes have the same shape (:class:`JvmIndex`). This module
combines them into the view one file resolves with, own language first:

* **imports:** a fully-qualified name is looked up among the own language's classes, then the
  other's (``import com.acme.DataApiClient`` in Groovy → ``DataApiClient.java``); the
  same-package rule applies to both, as neither language needs an import for it.
* **classes:** a simple name declared in only one of the languages takes that language's entry.
  One both declare (``collisions``) means the own language's class, as it did before the other
  index was read — unless the file's import or package binds it to the other language's class:
  in that file it is ambiguous (:func:`file_heritage`), so a call checks only the methods the
  class declares in the file the binding names (``callresolve.py``'s ``unknown_methods``), never
  the own language's class. A class whose parent's name collides ends its chain there.
* **parents:** a class whose base is no class of its own language keeps the base's candidate
  FQCNs (each index's ``foreign_bases``); exactly one of them among the other language's classes
  makes it the class's parent, so ``job.run()`` reaches the Java ``AbstractBatch`` that declares
  ``run`` and a Java class extending a Groovy one reaches the Groovy parent's methods.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Protocol, TypeVar, cast

from .index_common import ClassHeritage

_Key = tuple[str, str]


class JvmIndex(Protocol):
    """The parts of the Java and Groovy indexes the view reads."""

    @property
    def fqcn(self) -> dict[str, str | None]: ...
    @property
    def class_heritage(self) -> dict[str, ClassHeritage | None]: ...
    @property
    def unknown_methods(self) -> dict[_Key, frozenset[str] | None]: ...
    @property
    def foreign_bases(self) -> dict[str, tuple[str, ...] | None]: ...


@dataclass(frozen=True, slots=True)
class JvmView:
    """The indexes one Java or Groovy file resolves with."""

    fqcn: dict[str, str | None]
    heritage: Mapping[str, ClassHeritage | None]
    unknown_methods: Mapping[_Key, frozenset[str] | None]
    #: simple names a class of each language shares
    collisions: frozenset[str] = frozenset()
    #: the other language's file extension (``.java``, ``.groovy``)
    other_ext: str = ""


class _DeclaredIn(Mapping[_Key, "frozenset[str] | None"]):
    """``(file, class simple name)`` → the methods that class declares in that file, answered from
    the index of the file's language: its ``unknown_methods`` for a class that index cannot
    describe, else the methods of the class's entry that map to that file (a name both languages
    declare is described in each). Iterating lists only the indexes' ``unknown_methods`` keys."""

    def __init__(self, own: JvmIndex, other: JvmIndex, other_ext: str) -> None:
        self._own, self._other, self._ext = own, other, other_ext

    def __getitem__(self, key: _Key) -> frozenset[str] | None:
        rel, name = key
        side = self._other if rel.endswith(self._ext) else self._own
        if key in side.unknown_methods:
            return side.unknown_methods[key]
        heritage = side.class_heritage.get(name)
        if heritage is None:
            raise KeyError(key)
        return frozenset(m for m, where in heritage.methods.items() if where == rel)

    def __iter__(self) -> Iterator[_Key]:
        yield from self._own.unknown_methods
        yield from self._other.unknown_methods

    def __len__(self) -> int:
        return len(self._own.unknown_methods) + len(self._other.unknown_methods)


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


def _combine(own: JvmIndex, other: JvmIndex, other_ext: str) -> JvmView:
    fqcn: dict[str, str | None] = dict(other.fqcn)
    fqcn.update(own.fqcn)  # own language first
    collisions = frozenset(own.class_heritage.keys() & other.class_heritage.keys())
    heritage: dict[str, ClassHeritage | None] = {}
    for name, entry in other.class_heritage.items():
        if entry is not None and entry.extends in collisions:  # its parent's name means own's
            entry = replace(entry, extends=None)
        heritage[name] = entry
    heritage.update(own.class_heritage)  # own language first
    for side, peer in ((own, other), (other, own)):
        for name, candidates in side.foreign_bases.items():
            if side is other and name in collisions:  # the entry under that name is own's
                continue
            entry = heritage.get(name)
            if entry is None or entry.extends is not None or candidates is None:
                continue
            hits = [c for c in candidates if peer.fqcn.get(c) is not None]
            base = hits[0].rsplit(".", 1)[-1] if len(hits) == 1 else None
            if base is not None and base not in collisions:
                heritage[name] = replace(entry, extends=base)
    return JvmView(fqcn, heritage, _DeclaredIn(own, other, other_ext), collisions, other_ext)


def file_heritage(
    view: JvmView, bindings: Mapping[str, str], aliases: Mapping[str, str],
) -> Mapping[str, ClassHeritage | None]:
    """The class entries one file resolves with: ``view.heritage``, with each import alias
    (``import a.Foo as Bar`` → ``{"Bar": "Foo"}``) looked up as its class, and each name the file
    binds to the other language's class whose simple name an own-language class shares made
    ambiguous."""
    def other_bound(name: str, real: str) -> bool:
        return (real in view.collisions and view.other_ext != ""
                and bindings.get(name, "").endswith(view.other_ext))

    known = view.heritage
    top: dict[str, ClassHeritage | None] = {
        name: None for name in bindings if other_bound(name, name)}
    for alias, real in aliases.items():
        if real in known:
            top[alias] = None if other_bound(alias, real) else known[real]
    return Overlay(top, known) if top else known


#: The last view built per own language, with the indexes it was built from: a worker parses
#: every file with the same indexes, so each language's view is built once per worker.
_last: dict[str, tuple[JvmIndex, JvmIndex, JvmView]] = {}


def jvm_view(own: JvmIndex, other: object, other_type: type, other_ext: str) -> JvmView:
    """The view of ``own`` alone, or combined with ``other`` when that is the other language's
    index (an instance of ``other_type``, whose files end in ``other_ext``)."""
    if not isinstance(other, other_type):
        return JvmView(own.fqcn, own.class_heritage, own.unknown_methods)
    peer = cast(JvmIndex, other)
    cached = _last.get(other_ext)
    if cached is not None and cached[0] is own and cached[1] is peer:
        return cached[2]
    view = _combine(own, peer, other_ext)
    _last[other_ext] = (own, peer, view)
    return view
