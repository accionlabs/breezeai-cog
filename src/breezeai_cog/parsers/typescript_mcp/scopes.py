"""Which declaration a name refers to, for the TypeScript MCP detectors.

A name used inside a function or a block is the top-level function of that name only when no
enclosing scope binds it first: a parameter (destructured included), a ``const`` / ``let`` /
``var`` (a ``var`` anywhere in the function body), a nested ``function`` or ``class``
declaration, a ``catch`` parameter or a ``for … of`` / ``for … in`` / ``for (let …)`` variable.
Any such binding means the handler is that local, whose declaration is not read, so it has no
line.

A server receiver is proven by its binding, not its text: the call's nearest binding of the name
must be the one assigned the server (or typed as it), and a ``this.x`` must be a field of the
class around the call (:func:`proven_receivers`, :func:`receiver_binding`).
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from ..treesitter import node_text

FUNCTION_SCOPES = frozenset(
    {
        "function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
        "generator_function_declaration",
        "generator_function",
    }
)
_PARAMETERS = ("required_parameter", "optional_parameter")
_DECLARED = (
    "function_declaration",
    "generator_function_declaration",
    "class_declaration",
    "abstract_class_declaration",
)


def binds(pattern: Node | None, name: str, source: bytes) -> bool:
    """True when the binding pattern ``pattern`` (a name, a destructuring) declares ``name``."""
    return name in bound_names(pattern, source)


def bound_names(pattern: Node | None, source: bytes) -> list[str]:
    """The names a binding pattern (a name, a destructuring) declares (what :func:`binds`
    matches)."""
    out: list[str] = []
    stack = [pattern] if pattern is not None else []
    while stack:
        node = stack.pop()
        if node.type in ("identifier", "shorthand_property_identifier_pattern"):
            out.append(node_text(node, source))
        elif node.type in ("assignment_pattern", "object_assignment_pattern"):
            left = node.child_by_field_name("left")
            if left is not None:
                stack.append(left)
        elif node.type == "pair_pattern":
            value = node.child_by_field_name("value")
            if value is not None:
                stack.append(value)
        elif node.type in ("object_pattern", "array_pattern", "rest_pattern"):
            stack.extend(reversed(node.named_children))
    return out


#: A scope, by its span and node type (a node object is not kept: tree-sitter makes a new one
#: on every access).
ScopeKey = tuple[int, int, str]
#: A receiver's binding: ``(scope, text)`` — the scope that declares a plain name, or the class
#: of a ``this.x``. Two receivers with the same binding are the same object.
Binding = tuple[ScopeKey, str]

_SCOPES = FUNCTION_SCOPES | {
    "program",
    "statement_block",
    "class_static_block",
    "switch_body",
    "for_in_statement",
    "for_statement",
    "catch_clause",
}
_CLASSES = ("class_declaration", "class", "abstract_class_declaration")
#: Functions that give ``this`` a value of their own (an arrow keeps the enclosing one; a
#: method's is its class's instance).
_THIS_FUNCTIONS = FUNCTION_SCOPES - {"arrow_function", "method_definition"}


def scope_key(node: Node) -> ScopeKey:
    return (node.start_byte, node.end_byte, node.type)


class Scopes:
    """Every scope of a file and the names declared directly in it, read in one walk, so a
    lookup costs the depth of the reference and never a re-read of a function or block (a
    function of many registrations would otherwise be read once per registration).

    A ``var`` (``for (var … of …)`` included) belongs to its function, wherever in the body it
    is written; ``let`` / ``const`` and a ``function`` / ``class`` declaration to the block,
    ``switch`` or ``for`` that holds them; a parameter, and a function expression's own name, to
    the function; a ``catch`` parameter to its clause."""

    def __init__(self, root: Node, source: bytes) -> None:
        self.source = source
        self.root = scope_key(root)
        self._names: dict[ScopeKey, set[str]] = {}
        stack: list[tuple[Node, set[str] | None, set[str] | None]] = [(root, None, None)]
        while stack:
            node, scope, fn = stack.pop()
            if scope is not None and fn is not None:
                if node.type in _DECLARED:
                    name = node.child_by_field_name("name")
                    if name is not None:
                        scope.add(node_text(name, source))
                elif node.type == "variable_declarator":
                    parent = node.parent
                    var = parent is not None and parent.type == "variable_declaration"
                    (fn if var else scope).update(
                        bound_names(node.child_by_field_name("name"), source)
                    )
            if node.type in _SCOPES:
                scope = self._names.setdefault(scope_key(node), set())
                if node.type in FUNCTION_SCOPES or node.type in ("program", "class_static_block"):
                    fn = scope
                self._bind_own(node, scope, fn)
            stack.extend((c, scope, fn) for c in node.named_children)

    def _bind_own(self, node: Node, scope: set[str], fn: set[str] | None) -> None:
        """The names a scope node binds itself: parameters, a loop or ``catch`` variable."""
        source = self.source
        if node.type in FUNCTION_SCOPES:
            scope.update(bound_names(node.child_by_field_name("parameter"), source))  # `x => …`
            params = node.child_by_field_name("parameters")
            for p in params.named_children if params is not None else ():
                pattern = p.child_by_field_name("pattern") if p.type in _PARAMETERS else p
                scope.update(bound_names(pattern, source))
            if node.type in ("function_expression", "function", "generator_function"):
                own = node.child_by_field_name("name")  # `function lookup() { … lookup … }`
                if own is not None:
                    scope.add(node_text(own, source))
        elif node.type == "for_in_statement":
            kind = node.child_by_field_name("kind")
            var = kind is not None and node_text(kind, source) == "var"
            (fn if var and fn is not None else scope).update(
                bound_names(node.child_by_field_name("left"), source)
            )
        elif node.type == "catch_clause":
            scope.update(bound_names(node.child_by_field_name("parameter"), source))

    def binding(self, ident: Node) -> ScopeKey:
        """The scope whose declaration of ``ident``'s name is the one ``ident`` refers to: the
        nearest enclosing scope that declares it, else the module."""
        name = node_text(ident, self.source)
        node = ident.parent
        while node is not None:
            if node.type in _SCOPES:
                key = scope_key(node)
                if name in self._names.get(key, ()):
                    return key
            node = node.parent
        return self.root

    def locally_bound(self, ident: Node) -> bool:
        """True when a scope enclosing ``ident`` (below the module) binds its name."""
        return self.binding(ident) != self.root


def this_class(node: Node) -> ScopeKey | None:
    """The class whose instance ``this`` is at ``node``: the class around it, through arrows
    and methods; None outside a class or inside a ``function`` (it has a ``this`` of its own)."""
    cur = node.parent
    while cur is not None:
        if cur.type in _CLASSES:
            return scope_key(cur)
        if cur.type in _THIS_FUNCTIONS:
            return None
        cur = cur.parent
    return None


def receiver_binding(node: Node, source: bytes, scopes: Scopes) -> Binding | None:
    """The binding of a receiver: a plain name (where it is declared), ``this.x`` (its class),
    or a property of either (``mcp.server``, ``this.mcp.server``: the binding of its base, with
    the whole text); None for anything else."""
    if node.type == "identifier":
        return (scopes.binding(node), node_text(node, source))
    if node.type != "member_expression":
        return None
    obj = node.child_by_field_name("object")
    if obj is None:
        return None
    if obj.type == "this":
        cls = this_class(node)
        return (cls, node_text(node, source)) if cls is not None else None
    base = receiver_binding(obj, source, scopes)
    return (base[0], node_text(node, source)) if base is not None else None


def proven_receivers(
    root: Node,
    source: bytes,
    scopes: Scopes,
    typed: Callable[[Node, bytes], bool],
    built: Callable[[Node | None, bytes], bool],
) -> set[Binding]:
    """The bindings proven to hold a server: a name declared or assigned ``built`` (``new
    X(…)``), a parameter ``typed`` as the server class, and a class field (``this.x``) given
    either. Each is the binding itself, so a same-named binding of another scope (a local, a
    parameter, a field of another class) is not proven by it."""
    out: set[Binding] = set()
    for node in _walk(root):
        if node.type == "new_expression" and built(node, source):
            parent = node.parent
            if parent is None:
                continue
            if parent.type == "variable_declarator":
                name = parent.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    out.add((scopes.binding(name), node_text(name, source)))
            elif parent.type == "assignment_expression":
                left = parent.child_by_field_name("left")
                bound = receiver_binding(left, source, scopes) if left is not None else None
                if bound is not None:
                    out.add(bound)
            elif parent.type in ("public_field_definition", "field_definition"):
                field = _field(parent, source)
                if field is not None:
                    out.add(field)
        elif node.type in _PARAMETERS and typed(node, source):
            pattern = node.child_by_field_name("pattern")
            if pattern is not None and pattern.type == "identifier":
                out.add((scopes.binding(pattern), node_text(pattern, source)))
        elif node.type == "public_field_definition" and typed(node, source):
            field = _field(node, source)
            if field is not None:
                out.add(field)
    return out


def _field(definition: Node, source: bytes) -> Binding | None:
    """A class field definition → the binding of its ``this.x``."""
    name = definition.child_by_field_name("name") or definition.child_by_field_name("property")
    cls = this_class(definition) if name is not None else None
    return (cls, f"this.{node_text(name, source)}") if name is not None and cls else None


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def function_line(
    ident: Node, source: bytes, functions: dict[str, int], scopes: Scopes
) -> int | None:
    """The line of the top-level function ``ident`` names (``functions``: name → line), or None
    when an enclosing scope binds the name to something else."""
    line = functions.get(node_text(ident, source))
    if line is None or scopes.locally_bound(ident):
        return None
    return line
