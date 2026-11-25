"""``Type.M()`` on a repository type → the file declaring ``M``.

A C# ``using`` names a namespace, not a file, so a type is bound to its file only where the file
names it as a type (a base, a declared type, ``new X()``: ``imports._referenced_types``). A type
named only as the receiver of a static call — ``ClauseRequest.Create(id)``,
``Sql.ToGuid(row["ID"])`` — was bound nowhere, and the call kept no path.

The receiver is taken as a type only when nothing else can be meant:

* a bare capitalized identifier (``X.M()``; not ``a.X.M()``, not ``X<T>.M()``);
* no variable, parameter, field, property or event of the file has that name (C# looks those
  up before types; a member inherited from another file's base class is ruled out by the next
  check: that class's type would have to declare ``M`` too);
* it resolves to one file by the namespaces in scope (``imports._resolve``: ``using``s, global
  usings, project references, the same-project tiebreak) — or it is a class of this file;
* that class, as the class index knows it (one class of that name), or one of its in-repo
  ancestors declares ``M`` — the path is the declaring file. For a class of this file, its own
  members declare ``M``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tree_sitter import Node

from ..callresolve import CallResolver
from ..index_common import walk_heritage
from ..treesitter import node_text
from .functions import declaration_name

_TYPES = frozenset({"class_declaration", "struct_declaration", "record_declaration",
                    "interface_declaration", "enum_declaration"})
_METHODS = frozenset({"method_declaration", "local_function_statement"})
#: Declarations whose ``name`` C# looks up before a type of that name.
_NAMED = frozenset({"variable_declarator", "parameter", "property_declaration",
                    "event_declaration", "foreach_statement", "catch_declaration",
                    "single_variable_designation", "implicit_parameter"})


def _receivers_and_names(root: Node, source: bytes
                         ) -> tuple[set[str], set[str], dict[str, set[str] | None]]:
    """(capitalized bare receivers of member calls, names declared as values anywhere in the
    file, this file's types → the methods each declares directly; None for a type declared
    twice in the file)."""
    receivers: set[str] = set()
    values: set[str] = set()
    own: dict[str, set[str] | None] = {}
    stack = [root]
    while stack:
        n = stack.pop()
        t = n.type
        if t == "invocation_expression":
            fn = n.child_by_field_name("function")
            obj = fn.child_by_field_name("expression") \
                if fn is not None and fn.type == "member_access_expression" else None
            if obj is not None and obj.type == "identifier":
                text = node_text(obj, source)
                if text[:1].isupper():
                    receivers.add(text)
        elif t in _NAMED:
            nm = n.child_by_field_name("name") or n.child_by_field_name("left")
            if nm is None and t in ("single_variable_designation", "implicit_parameter"):
                nm = n
            if nm is not None and nm.type == "identifier":
                values.add(node_text(nm, source))
        elif t in _TYPES:
            name = declaration_name(n, source)
            if name is not None:
                body = n.child_by_field_name("body")
                methods = {declaration_name(m, source) or "" for m in
                           (body.named_children if body is not None else [])
                           if m.type in _METHODS}
                own[name] = None if name in own else methods
        stack.extend(n.named_children)
    return receivers, values, own


#: ``Name.method(`` in the source: a capitalized receiver of a call, for the cheap check.
_CANDIDATE = re.compile(rb"(?<![\w.])([A-Z]\w*)\s*\.\s*[A-Za-z_]\w*\s*[(<]")


def static_receivers(root: Node, source: bytes, path: str, bindings: Mapping[str, str],
                     resolve_type: Any, known: Any
                     ) -> tuple[dict[str, str], dict[str, set[str] | None]]:
    """(receiver → the other file its type resolves to, this file's types → their methods),
    for the receivers nothing else in the file can mean. ``resolve_type``: a type name → the
    other file declaring it, or None. ``known(name)``: whether the repository declares a type of
    that name (the class index, or this file) — the tree is walked only when some receiver is
    one."""
    if not any(known(m.decode()) for m in set(_CANDIDATE.findall(source))):
        return {}, {}
    receivers, values, own = _receivers_and_names(root, source)
    out: dict[str, str] = {}
    for name in receivers:
        if name in values or name in bindings or name in own:
            continue
        file = resolve_type(name)
        if file is not None:
            out[name] = file
    return out, {k: v for k, v in own.items() if k in receivers and k not in values}


def with_static_calls(base: CallResolver, *, path: str, others: Mapping[str, str],
                      own: Mapping[str, set[str] | None],
                      heritage: Mapping[str, Any] | None) -> CallResolver:
    """``base``, plus ``Type.M()`` on a receiver of ``others`` (another file's type) or ``own``
    (this file's type) → the file declaring ``M`` in that class or an in-repo ancestor."""
    if not others and not own:
        return base
    index = heritage or {}

    def declaring(typ: str, file: str, name: str) -> str | None:
        cls = index.get(typ)
        if cls is None:  # not in the index, or two classes share the name
            return None
        if cls.methods.get(name) == file:
            return file
        if name in cls.methods:
            # declared, but not in `file`: by another class of that name (the index merges
            # same-named classes with one base, as partial classes), or in two files — unsure
            return None
        if file not in cls.methods.values():
            return None  # the index's class of that name is not the one in `file`
        for anc in walk_heritage(typ, cls.extends, index).ancestors:
            if name in anc.methods:
                return anc.methods[name]
        return None

    def resolve(name: str, receiver: str | None, owner: str | None = None,
                *args: Any, **kwargs: Any) -> str | None:
        if receiver is not None and receiver in others:
            return declaring(receiver, others[receiver], name)
        if receiver is not None and receiver in own:
            methods = own[receiver]
            return path if methods is not None and name in methods else None
        return base(name, receiver, owner, *args, **kwargs)

    return resolve
