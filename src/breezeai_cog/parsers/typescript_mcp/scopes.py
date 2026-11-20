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

from bisect import bisect_right
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
#: Every node :class:`Scopes` records: a scope, or a class (where ``this`` is its instance).
_FRAMES = _SCOPES | set(_CLASSES)
#: Functions that give ``this`` a value of their own (an arrow keeps the enclosing one; a
#: class method's is its class's instance, an object literal's method has its own).
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
    the function; a ``catch`` parameter to its clause. A parameter of a signature without a
    body (an interface method, an overload, a function type) binds nothing.

    The walk also keeps each scope's (and class's) enclosing one and their spans, so a lookup
    finds the innermost one around a node by its position and follows the stored chain: a
    tree-sitter ``.parent`` re-descends from the root, so walking up by it costs the depth
    squared."""

    def __init__(self, root: Node, source: bytes) -> None:
        self.source = source
        self.root = scope_key(root)
        self._names: dict[ScopeKey, set[str]] = {}
        #: Each frame (a scope or a class) → the frame around it (None for the module).
        self._outer: dict[ScopeKey, ScopeKey | None] = {}
        #: The frames that decide ``this``: a class (its key) or a function with its own (None).
        self._this: dict[ScopeKey, ScopeKey | None] = {}
        #: The parameters of a function scope (what a typed-parameter proof may bind).
        self.parameters: set[ScopeKey] = set()
        frames: list[tuple[int, int, int, ScopeKey]] = []
        Item = tuple[Node, str, set[str] | None, set[str] | None, ScopeKey | None, int]
        stack: list[Item] = [(root, "", None, None, None, 0)]
        while stack:
            node, parent_type, scope, fn, frame, depth = stack.pop()
            kind = node.type
            if scope is not None and fn is not None:
                if kind in _DECLARED:
                    name = node.child_by_field_name("name")
                    if name is not None:
                        scope.add(node_text(name, source))
                elif kind == "variable_declarator":
                    var = parent_type == "variable_declaration"
                    (fn if var else scope).update(
                        bound_names(node.child_by_field_name("name"), source)
                    )
            if kind in _FRAMES:
                key = scope_key(node)
                self._outer[key] = frame
                frames.append((key[0], -key[1], depth, key))
                frame = key
                if kind in _CLASSES:
                    self._this[key] = key
                elif kind in _THIS_FUNCTIONS or (
                    kind == "method_definition" and parent_type != "class_body"
                ):
                    self._this[key] = None
            if kind in _SCOPES:
                scope = self._names.setdefault(scope_key(node), set())
                if kind in FUNCTION_SCOPES or kind in ("program", "class_static_block"):
                    fn = scope
                self._bind_own(node, scope, fn)
            stack.extend((c, kind, scope, fn, frame, depth + 1) for c in node.named_children)
        frames.sort()
        self._starts = [f[0] for f in frames]
        self._frames = [f[3] for f in frames]

    def _bind_own(self, node: Node, scope: set[str], fn: set[str] | None) -> None:
        """The names a scope node binds itself: parameters, a loop or ``catch`` variable."""
        source = self.source
        if node.type in FUNCTION_SCOPES:
            scope.update(bound_names(node.child_by_field_name("parameter"), source))  # `x => …`
            params = node.child_by_field_name("parameters")
            for p in params.named_children if params is not None else ():
                pattern = p.child_by_field_name("pattern") if p.type in _PARAMETERS else p
                scope.update(bound_names(pattern, source))
                self.parameters.add(scope_key(p))
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

    def _frame(self, node: Node) -> ScopeKey | None:
        """The innermost frame (scope or class) whose span holds ``node``. Spans nest, so it is
        the last frame starting at or before the node, or one of the frames around that one."""
        start, end = node.start_byte, node.end_byte
        i = bisect_right(self._starts, start) - 1
        frame = self._frames[i] if i >= 0 else None
        while frame is not None and not (frame[0] <= start and end <= frame[1]):
            frame = self._outer[frame]
        return frame

    def binding(self, ident: Node) -> ScopeKey:
        """The scope whose declaration of ``ident``'s name is the one ``ident`` refers to: the
        nearest enclosing scope that declares it, else the module."""
        return self._lookup(node_text(ident, self.source), self._frame(ident))

    def outer_binding(self, name: str, scope: Node) -> ScopeKey:
        """The scope whose declaration of ``name`` is seen just outside the scope node
        ``scope`` (what a ``for (s of …)`` without ``let`` / ``const`` / ``var`` writes)."""
        return self._lookup(name, self._outer.get(scope_key(scope)))

    def _lookup(self, name: str, frame: ScopeKey | None) -> ScopeKey:
        while frame is not None:
            if name in self._names.get(frame, ()):
                return frame
            frame = self._outer[frame]
        return self.root

    def locally_bound(self, ident: Node) -> bool:
        """True when a scope enclosing ``ident`` (below the module) binds its name."""
        return self.binding(ident) != self.root

    def this_class(self, node: Node) -> ScopeKey | None:
        """The class whose instance ``this`` is at ``node``: the class around it, through
        arrows and its own methods; None outside a class, or inside a ``function`` or an object
        literal's method (each has a ``this`` of its own)."""
        frame = self._frame(node)
        while frame is not None:
            if frame in self._this:
                return self._this[frame]
            frame = self._outer[frame]
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
        cls = scopes.this_class(node)
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
    parameter, a field of another class) is not proven by it.

    A binding is proven only when every write to it is a proof: ``s = other``, ``s ||= x``,
    ``s++``, a destructuring target (``[s] = xs``, ``({ s } = o)``), a ``for (s of …)`` or
    another declaration of anything else, wherever the binding is seen, makes it unproven
    (which object a call runs on is not read). A declaration without a value (``let s;``)
    writes nothing."""
    out: set[Binding] = set()
    #: Bindings written anything that is not a server.
    spoiled: set[Binding] = set()
    for node in _walk(root):
        t = node.type
        if t == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name is not None and value is not None:
                proof = name.type == "identifier" and built(value, source)
                (out if proof else spoiled).update(_written(name, source, scopes))
        elif t == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None:
                proof = left.type in ("identifier", "member_expression") and built(right, source)
                (out if proof else spoiled).update(_written(left, source, scopes))
        elif t == "augmented_assignment_expression":
            left = node.child_by_field_name("left")
            spoiled.update(_written(left, source, scopes) if left is not None else ())
        elif t == "update_expression":
            arg = node.child_by_field_name("argument")
            spoiled.update(_written(arg, source, scopes) if arg is not None else ())
        elif t == "for_in_statement":
            # `for (s of xs)` declares nothing: it writes the `s` around the loop (a `var` one
            # writes its function's, a `let` / `const` one a binding of the loop's own).
            left = node.child_by_field_name("left")
            loop = node if node.child_by_field_name("kind") is None else None
            spoiled.update(_written(left, source, scopes, loop) if left is not None else ())
        elif t in _PARAMETERS and scope_key(node) in scopes.parameters and typed(node, source):
            pattern = node.child_by_field_name("pattern")
            if pattern is not None and pattern.type == "identifier":
                out.add((scopes.binding(pattern), node_text(pattern, source)))
        elif t in ("public_field_definition", "field_definition"):
            value = node.child_by_field_name("value")
            proof = (t == "public_field_definition" and typed(node, source)) or (
                value is not None and built(value, source)
            )
            field = _field(node, source, scopes) if proof or value is not None else None
            if field is not None:
                (out if proof else spoiled).add(field)
    return out - spoiled


#: Destructuring and wrapper shapes of an assignment target that hold the written targets.
_TARGET_GROUPS = (
    "object_pattern",
    "array_pattern",
    "rest_pattern",
    "parenthesized_expression",
    "non_null_expression",
    "as_expression",
    "satisfies_expression",
)


def _written(
    target: Node, source: bytes, scopes: Scopes, loop: Node | None = None
) -> list[Binding]:
    """The bindings an assignment target writes: a name, a member (``this.x``, ``a.b``), or each
    of those in a destructuring. ``loop``: a ``for … of`` / ``in`` without a declaration, whose
    names are looked up outside it."""
    out: list[Binding] = []
    stack = [target]
    while stack:
        node = stack.pop()
        t = node.type
        if t in ("identifier", "shorthand_property_identifier_pattern"):
            name = node_text(node, source)
            scope = scopes.outer_binding(name, loop) if loop is not None else scopes.binding(node)
            out.append((scope, name))
        elif t == "member_expression":
            bound = receiver_binding(node, source, scopes)
            if bound is not None:
                out.append(bound)
        elif t in ("assignment_pattern", "object_assignment_pattern"):
            left = node.child_by_field_name("left")
            stack.extend([left] if left is not None else [])
        elif t == "pair_pattern":
            value = node.child_by_field_name("value")
            stack.extend([value] if value is not None else [])
        elif t in _TARGET_GROUPS:
            stack.extend(node.named_children)
    return out


def _field(definition: Node, source: bytes, scopes: Scopes) -> Binding | None:
    """A class field definition → the binding of its ``this.x``."""
    name = definition.child_by_field_name("name") or definition.child_by_field_name("property")
    cls = scopes.this_class(definition) if name is not None else None
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
