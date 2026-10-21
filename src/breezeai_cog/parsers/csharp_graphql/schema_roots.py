"""graphql-dotnet schema roots as a registered fact, ``graphql-dotnet.schema-roots``.

The schema names its root types, whatever they are called::

    public class BookSchema : Schema {                                   // BookSchema.cs
        public BookSchema(IServiceProvider sp) : base(sp) {
            Query = sp.GetRequiredService<RootQueries>();                // RootQueries is the query root
            Mutation = new Writes();
        }
    }
    var schema = new Schema { Query = new RootQueries() };

The assignment is wiring (contract §4): it makes the class's fields operations. Without it only
the name convention (``…Query`` / ``…Mutations``) finds a root. Collected while the C# index is
built — each ``Query`` / ``Mutation`` / ``Subscription`` assignment in a class deriving from
``Schema``, or in a ``new Schema { … }`` initializer, naming its type by ``new T()`` or a generic
service lookup (``GetRequiredService<T>()``, ``GetService<T>()``, ``Resolve<T>()``) — and resolved
to the file declaring ``T`` the way the compiler binds it. A class assigned two different kinds
is dropped (one is wrong and nothing says which).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..csharp.imports import CSharpIndex, in_scope_namespaces, type_files
from ..treesitter import node_text

FACT = "graphql-dotnet.schema-roots"

_ROOTS = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}
_LOOKUPS = frozenset({"GetRequiredService", "GetService", "Resolve"})


def schema_roots(index: Any | None, path: str) -> dict[str, str]:
    """Root classes declared in ``path`` → their kind (empty without an index)."""
    roots: dict[str, dict[str, str]] | None = index_fact(index, FACT)
    return (roots or {}).get(path, {})


def _type_named(expr: Node, source: bytes) -> str | None:
    """``new T()`` / ``x.GetRequiredService<T>()`` → ``T``."""
    if expr.type == "object_creation_expression":
        typ = expr.child_by_field_name("type")
        return node_text(typ, source) if typ is not None and typ.type != "generic_name" else None
    if expr.type != "invocation_expression":
        return None
    fn = expr.child_by_field_name("function")
    name = fn.child_by_field_name("name") if fn is not None and \
        fn.type == "member_access_expression" else fn
    if name is None or name.type != "generic_name":
        return None
    ident = next((c for c in name.named_children if c.type == "identifier"), None)
    targs = next((c for c in name.named_children if c.type == "type_argument_list"), None)
    if ident is None or node_text(ident, source) not in _LOOKUPS or targs is None \
            or len(targs.named_children) != 1:
        return None
    text = node_text(targs.named_children[0], source)
    return None if "<" in text else text


def _in_schema(node: Node, source: bytes) -> bool:
    """Inside a class deriving from ``Schema``, or a ``new Schema { … }`` initializer."""
    n = node.parent
    while n is not None:
        if n.type == "initializer_expression":
            creation = n.parent
            typ = creation.child_by_field_name("type") if creation is not None and \
                creation.type == "object_creation_expression" else None
            return typ is not None and node_text(typ, source).rsplit(".", 1)[-1] == "Schema"
        if n.type == "class_declaration":
            bases = next((c for c in n.named_children if c.type == "base_list"), None)
            return bases is not None and any(
                node_text(b, source).rsplit(".", 1)[-1] == "Schema" for b in bases.named_children)
        n = n.parent
    return False


def _collect(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> list[tuple[str, str, str, tuple[str, ...]]] | None:
    """``(file, kind, type name, namespaces in scope)`` per root assignment."""
    found: list[tuple[str, str, str, tuple[str, ...]]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "assignment_expression":
            continue
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or right is None:
            continue
        kind = _ROOTS.get(node_text(left, source).removeprefix("this."))
        name = _type_named(right, source) if kind is not None else None
        if kind is not None and name is not None and _in_schema(node, source):
            found.append((rel, kind, name, tuple(sorted(in_scope_namespaces(root, source)))))
    return found or None


def _resolve(collected: list[Any], index: CSharpIndex) -> dict[str, dict[str, str]] | None:
    """Declaring file → {class: kind}."""
    kinds: dict[tuple[str, str], str | None] = {}
    for rel, kind, name, scopes in sorted(r for per_file in collected for r in per_file):
        simple = name.rsplit(".", 1)[-1]
        for decl in type_files(name, set(scopes) | index.global_usings, index, rel):
            prev = kinds.get((decl, simple), kind)
            kinds[(decl, simple)] = kind if prev == kind else None
    out: dict[str, dict[str, str]] = {}
    for (decl, simple), kind in kinds.items():
        if kind is not None:
            out.setdefault(decl, {})[simple] = kind
    return out or None


register_fact(FactSpec(
    name=FACT, language="csharp", gate=(b"Schema",), collect=_collect, resolve=_resolve,
))
