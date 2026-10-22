"""Where an entry point's handler is declared — one place for every language.

An entry point names its handler; ``handlerLine`` says where it is declared, and ``handlerPath``
which file when that is not the record's own (contract §5). A detector that knows enough to look
the handler up does so here:

==========================================  ==================================================
The detector knows …                        Call
==========================================  ==================================================
a class and a method (``ExampleJob`` +      :func:`locate_method` — the ``<lang>.method-lines``
``Execute``, ``users#show``)                fact (C#, Java, Ruby)
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
ambiguous and resolves to None (honest-null). A partial class split over files is fine: each
method keeps its own file.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from tree_sitter import Node

from .additive import FactSpec, index_fact, register_fact
from .index_common import record_distinct
from .treesitter import node_text

#: language → (class-like node types, method node types).
_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "csharp": (frozenset({"class_declaration", "struct_declaration", "record_declaration"}),
               frozenset({"method_declaration"})),
    "java": (frozenset({"class_declaration", "record_declaration", "enum_declaration"}),
             frozenset({"method_declaration"})),
    "ruby": (frozenset({"class"}), frozenset({"method"})),
}

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


def _resolve(values: list[list[tuple[_Key, _Site]]], index: Any) -> dict[_Key, _Site | None] | None:
    out: dict[_Key, _Site | None] = {}
    for file_values in values:
        for key, site in file_values:
            record_distinct(out, key, site)
    return out or None


def _simple(name: str) -> str:
    """A class's simple name: ``Ns.T<X>`` / ``Admin::UsersController`` → ``T`` / ``UsersController``."""
    return name.split("<", 1)[0].rsplit("::", 1)[-1].rsplit(".", 1)[-1].strip()


def fact_name(language: str) -> str:
    return f"{language}.method-lines"


def locate_method(index: Any | None, language: str, cls: str | None,
                  method: str | None) -> _Site | None:
    """(file, line) of ``cls.method``, or None when unknown or ambiguous."""
    if not cls or not method:
        return None
    fact: dict[_Key, _Site | None] = index_fact(index, fact_name(language)) or {}
    return fact.get((_simple(cls), method))


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


for _language in _SHAPES:
    register_fact(FactSpec(
        name=fact_name(_language), language=_language, gate=(b"class",),
        collect=_collector(_language), resolve=_resolve,
    ))
