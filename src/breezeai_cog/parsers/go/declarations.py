"""The ``go.declarations`` fact: where each function and method of the repo is declared, and the
package-qualified type of each struct field — read by the Go route detectors to locate a handler
(``handlerLine`` / ``handlerPath``) and to know what ``s.router`` is when ``Server`` is declared
in another file of the package.

Go has no classes to scope a lookup by, so keys are package-scoped: a package is its directory.

::

    // users/routers.go                         // server.go
    package users                               type Server struct { router *gin.Engine }
    func UsersLogin(c *gin.Context) { … }       func (s *Server) health(c *gin.Context) { … }

    functions[("users", "", "UsersLogin")]  → ("users/routers.go", 3)
    functions[("", "Server", "health")]     → ("server.go", 2)
    fields[("", "Server", "router")]        → ("github.com/gin-gonic/gin", "Engine")

A name declared twice in one package (build-tag variants) is ambiguous and locates to None.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..treesitter import node_text
from .imports import import_aliases

FACT = "go.declarations"

Site = tuple[str, int]  # (file, line)
FuncKey = tuple[str, str, str]  # (package dir, receiver type or "", name)


@dataclass(frozen=True)
class Declarations:
    functions: dict[FuncKey, Site | None] = field(default_factory=dict)
    #: Method name → every site, for a receiver whose type is not known.
    methods: dict[str, tuple[Site, ...]] = field(default_factory=dict)
    #: (package dir, struct, field) → (import path, type name) for package-qualified field types.
    fields: dict[tuple[str, str, str], tuple[str, str]] = field(default_factory=dict)


def package_dir(rel: str) -> str:
    return rel.rpartition("/")[0]


def receiver_type(node: Node, source: bytes) -> str:
    """``func (s *Server[T]) …`` → ``Server``; a function → ``""``."""
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return ""
    for param in receiver.named_children:
        type_node = param.child_by_field_name("type")
        if type_node is not None:
            return node_text(type_node, source).lstrip("*").split("[", 1)[0].rsplit(".", 1)[-1]
    return ""


def qualified_type(type_text: str, aliases: dict[str, str]) -> tuple[str, str] | None:
    """``*gin.Engine`` → (``github.com/gin-gonic/gin``, ``Engine``); unqualified → None."""
    alias, dot, name = type_text.lstrip("*").partition(".")
    if not dot or alias not in aliases:
        return None
    return aliases[alias], name.split("[", 1)[0]


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> dict[str, Any] | None:
    pkg = package_dir(rel)
    aliases = import_aliases(root, source)
    functions: list[tuple[FuncKey, Site]] = []
    fields: list[tuple[tuple[str, str, str], tuple[str, str]]] = []
    for child in root.named_children:
        if child.type in {"function_declaration", "method_declaration"}:
            name = child.child_by_field_name("name")
            if name is not None:
                key = (pkg, receiver_type(child, source), node_text(name, source))
                functions.append((key, (rel, child.start_point[0] + 1)))
        elif child.type == "type_declaration":
            for spec in child.named_children:
                type_node = spec.child_by_field_name("type") if spec.type == "type_spec" else None
                if type_node is None or type_node.type != "struct_type":
                    continue
                struct = node_text(spec.child_by_field_name("name"), source)
                for body in type_node.named_children:
                    for decl in body.named_children:
                        if decl.type != "field_declaration":
                            continue
                        ftype = decl.child_by_field_name("type")
                        qualified = qualified_type(node_text(ftype, source), aliases) if ftype else None
                        if qualified is None:
                            continue
                        for fname in decl.children_by_field_name("name"):
                            fields.append(((pkg, struct, node_text(fname, source)), qualified))
    return {"functions": functions, "fields": fields} if functions or fields else None


def _resolve(values: list[dict[str, Any]], index: Any) -> Declarations | None:
    functions: dict[FuncKey, Site | None] = {}
    methods: dict[str, list[Site]] = {}
    fields: dict[tuple[str, str, str], tuple[str, str]] = {}
    for value in values:
        for key, site in value["functions"]:
            functions[key] = site if key not in functions else None
            if key[1]:
                methods.setdefault(key[2], []).append(site)
        for key, qualified in value["fields"]:
            fields[key] = qualified
    if not functions and not fields:
        return None
    return Declarations(functions, {n: tuple(sorted(s)) for n, s in methods.items()}, fields)


def declarations(index: Any | None) -> Declarations:
    return index_fact(index, FACT) or Declarations()


register_fact(FactSpec(name=FACT, language="go", gate=(b"func", b"struct"),
                       collect=_collect, resolve=_resolve))
