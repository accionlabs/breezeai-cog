"""Where an entry point's handler is declared — one place for every language.

An entry point names its handler; ``handlerLine`` says where it is declared, and ``handlerPath``
which file when that is not the record's own (contract §5). A detector that knows enough to look
the handler up does so here:

==========================================  ==================================================
The detector knows …                        Call
==========================================  ==================================================
a class and a method (``ExampleJob`` +      :func:`locate_method` — the ``<lang>.method-lines``
``Execute``, ``users#show``)                fact (C#, Java, PHP, Ruby)
a module and an exported name               :func:`locate_export` — the
(``import * as queries``)                   ``typescript.declarations`` fact (TS / JS)
only a name written in this file            nothing — name it; the parser's ``link_handlers``
(``<ProjectList/>``)                        locates it after parsing
==========================================  ==================================================

Whatever found the site, :func:`handler_location` turns it into the two fields, so the "own file
→ no ``handlerPath``" rule lives once.

``<lang>.method-lines`` — where each class's methods are declared. A Quartz job's ``Execute``, a
Spring functional route's ``handler::list``, a HotChocolate resolver class, a Rails controller
action: the detector knows the class and the method; the fact says where that method is::

    // ExampleJob.cs
    public class ExampleJob : IJob {
        public async ValueTask Execute(IJobExecutionContext context) { … }   // line 19
    }

    locate_method(index, "csharp", "ExampleJob", "Execute") → ("…/ExampleJob.cs", 19)

The key is (class simple name, method name); the value is the declaring file and the method's
start line — the line its ``Function`` record starts at, attributes / annotations included. The
same pair declared in two places (two classes of one name, overloads on different lines) is
ambiguous and resolves to None (honest-null) — unless the detector knows which files the class
binds to (``within``: two examples each declaring their own ``SimpleJob``, told apart by the
caller's namespace). A partial class split over files is fine: each method keeps its own file.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..schemas import FileRecord, Statement
from .additive import FactSpec, index_fact, register_fact
from .treesitter import node_text

#: language → (class-like node types, method node types).
_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "csharp": (frozenset({"class_declaration", "struct_declaration", "record_declaration"}),
               frozenset({"method_declaration"})),
    "java": (frozenset({"class_declaration", "record_declaration", "enum_declaration"}),
             frozenset({"method_declaration"})),
    "php": (frozenset({"class_declaration"}), frozenset({"method_declaration"})),
    "ruby": (frozenset({"class"}), frozenset({"method"})),
    "scala": (frozenset({"class_definition", "object_definition", "trait_definition"}),
              frozenset({"function_definition", "function_declaration"})),
}
#: Languages with a repo-wide ``<lang>.method-lines`` fact. Scala has none yet: its one user, the
#: Play routes lookup, reads the few controller files a routes file names (:func:`method_sites`).
_FACT_LANGUAGES = ("csharp", "java", "php", "ruby")

_Key = tuple[str, str]  # (class simple name, method name)
_Site = tuple[str, int]  # (file, line)


_Collect = Callable[[Node, bytes, str, Path | None], list[tuple[_Key, _Site]] | None]


def _collector(language: str) -> _Collect:
    classes, methods = _SHAPES[language]

    def collect(root: Node, source: bytes, rel: str,
                repo_root: Path | None) -> list[tuple[_Key, _Site]] | None:
        found: list[tuple[_Key, _Site]] = []

        def walk(node: Node, cls: str | None) -> None:
            if node.type in classes:
                nm = node.child_by_field_name("name")
                cls = _simple(node_text(nm, source)) if nm is not None else None
            elif node.type in methods and cls is not None:
                nm = node.child_by_field_name("name")
                if nm is not None:
                    found.append(((cls, node_text(nm, source)), (rel, node.start_point[0] + 1)))
                return  # a method's body declares no methods of this class
            for c in node.named_children:
                walk(c, cls)

        walk(root, None)
        return found or None

    return collect


def _resolve(values: list[list[tuple[_Key, _Site]]],
             index: Any) -> dict[_Key, tuple[_Site, ...]] | None:
    # Every site of a key, so a caller that knows the class's files can still pick one.
    out: dict[_Key, set[_Site]] = {}
    for file_values in values:
        for key, site in file_values:
            out.setdefault(key, set()).add(site)
    return {key: tuple(sorted(sites)) for key, sites in out.items()} or None


def _simple(name: str) -> str:
    """A class's simple name: ``Ns.T<X>`` / ``Admin::UsersController`` / ``App\\Http\\UserController``
    → ``T`` / ``UsersController`` / ``UserController``."""
    return name.split("<", 1)[0].rsplit("::", 1)[-1].rsplit(".", 1)[-1].rsplit("\\", 1)[-1].strip()


def fact_name(language: str) -> str:
    return f"{language}.method-lines"


def locate_method(index: Any | None, language: str, cls: str | None, method: str | None,
                  *, within: Collection[str] | None = None) -> _Site | None:
    """(file, line) of ``cls.method``, or None when unknown or ambiguous. ``within`` — the files
    the class binds to, when the detector knows them — keeps only the sites in those files."""
    if not cls or not method:
        return None
    fact: dict[_Key, tuple[_Site, ...]] = index_fact(index, fact_name(language)) or {}
    sites = fact.get((_simple(cls), method), ())
    if within is not None:
        sites = tuple(s for s in sites if s[0] in within)
    return sites[0] if len(sites) == 1 else None


def locate_export(index: Any | None, module_file: str, name: str) -> Any | None:
    """The :class:`~.typescript.declarations.Declaration` behind export ``name`` of a TS / JS
    module (re-exports followed; ``name`` may be ``"default"``), or None when it can't be
    followed."""
    from .typescript.declarations import resolve_export  # the TS package imports this module
    return resolve_export(index, module_file, name)


def handler_location(site: _Site | None, own_path: str) -> tuple[int | None, str | None]:
    """``(handlerLine, handlerPath)`` for a found declaration ``(file, line)``: ``handlerPath``
    only when the file is not the record's own; ``(None, None)`` when not found."""
    if site is None:
        return None, None
    file, line = site
    return line, (file if file != own_path else None)


#: Statements that point at a handler.
HANDLED = frozenset({"route", "eventbus_consumer", "timer"})


def unlocated(s: Statement) -> bool:
    """An entry point that names its handler but not where it is declared."""
    return (s.semanticType in HANDLED and bool(s.handler) and s.handlerLine is None
            and s.handlerPath is None)


def needs_location(record: FileRecord) -> bool:
    return any(unlocated(s) for s in record.statements)


def method_sites(language: str, root: Node, source: bytes, rel: str) -> list[tuple[_Key, _Site]]:
    """Every ``((class, method), (file, line))`` declared in one parsed file."""
    return _collector(language)(root, source, rel, None) or []


for _language in _FACT_LANGUAGES:
    register_fact(FactSpec(
        name=fact_name(_language), language=_language, gate=(b"class",),
        collect=_collector(_language), resolve=_resolve,
    ))
