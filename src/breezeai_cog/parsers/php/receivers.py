"""What a PHP call chain is called on — from evidence in the file, never from a name.

Evidence-based detectors (``data_access.py``, ``http_calls.py``) ask one question of every
call chain: is its receiver a known kind of handle — a database connection, an Eloquent
model, an HTTP client? :func:`outermost_calls` answers it from what the file proves:

* a **declared type** — a typed parameter, a typed property or a promoted constructor
  parameter (``private EntityManagerInterface $em`` → ``$this->em``);
* a **construction** — ``$client = new Client(...)``;
* an **assignment from a known chain** — ``$builder = $this->db->table('x')``;
* a **static call** on a resolved class — ``Http::get(...)``, ``User::create(...)``;
* a detector's own rules for ``$this``, ``$this->prop`` and plain functions.

Class names resolve by PHP's rules (``use`` imports, then the file's namespace). A variable
assigned two different kinds is dropped; untyped receivers have no kind. Each detector
supplies :class:`Rules` that say which classes, statics and functions are its kinds.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

from tree_sitter import Node

from ..additive import DetectContext
from ..php_route_common import PHP_CALL_TYPES, unwrap
from ..treesitter import node_text
from .imports import resolve_class, use_aliases
from .index import namespace_of

_SCOPES = {"function_definition", "method_declaration", "anonymous_function", "arrow_function"}
_CLASSES = {"class_declaration", "trait_declaration", "enum_declaration"}


def _none2(_a: object, _b: object) -> None:
    return None


def _none1(_a: object) -> None:
    return None


@dataclass(frozen=True)
class Rules:
    """A detector's kinds of handle."""

    #: Fully qualified class → kind, for declared types and ``new X()``.
    types: Mapping[str, str]
    #: ``(class, first method)`` of a static call → kind.
    static: Callable[[str, str], str | None] = field(default=_none2)
    #: A plain function call (``db_connect()``) → kind.
    function: Callable[[str], str | None] = field(default=_none1)
    #: ``$this->name`` with no declared type → kind (``$this->db`` in a CodeIgniter file).
    this_property: Callable[[str], str | None] = field(default=_none1)
    #: ``$this->m()`` inside a class of kind ``types[class]`` → kind.
    this_call: Callable[[str | None, str], str | None] = field(default=_none2)
    #: The kind of handle a chain of ``kind`` returns, by its methods (``getRepository`` → a
    #: repository); None when the result is not a handle.
    chain_result: Callable[[str, list[str]], str | None] = field(default=_none2)
    #: The kind of an *instance* of a class whose ``types`` kind is given.
    instance: Callable[[str], str] = field(default=lambda kind: kind)


def method_name(call: Node, source: bytes) -> str:
    """The lower-cased method (or function) a call invokes."""
    name = call.child_by_field_name("name")
    if name is None and call.type == "function_call_expression":
        name = call.child_by_field_name("function")
    return node_text(name, source).rsplit("\\", 1)[-1].lower() if name is not None else ""


def _type_name(node: Node | None, source: bytes) -> str | None:
    """The single class a declared type names (``?Foo`` → ``Foo``); None for unions etc."""
    node = node.named_children[0] if node is not None and node.type == "optional_type" else node
    if node is None or node.type != "named_type":
        return None
    return node_text(node, source)


def chain(call: Node) -> tuple[Node, list[Node]]:
    """(base expression, calls from the innermost to ``call``)."""
    calls = [call]
    node = call
    while True:
        receiver = unwrap(node.child_by_field_name("object") or node.child_by_field_name("scope"))
        if receiver is not None and receiver.type in PHP_CALL_TYPES - {"function_call_expression"}:
            calls.append(receiver)
            node = receiver
            continue
        calls.reverse()
        return (receiver if receiver is not None else node), calls


def _outermost(call: Node) -> bool:
    parent = call.parent
    while parent is not None and parent.type == "parenthesized_expression":
        parent = parent.parent
    if parent is None or parent.type not in PHP_CALL_TYPES:
        return True
    receiver = unwrap(parent.child_by_field_name("object") or parent.child_by_field_name("scope"))
    return receiver is None or (receiver.start_byte, receiver.end_byte) != (
        call.start_byte, call.end_byte)


def _own_nodes(node: Node | None) -> Iterator[Node]:
    """Every node of a function body, not descending into nested functions or classes."""
    if node is None:
        return
    for child in node.named_children:
        yield child
        if child.type not in _SCOPES | _CLASSES:
            yield from _own_nodes(child)


class _Receivers:
    def __init__(self, dc: DetectContext, rules: Rules) -> None:
        self.source = dc.source
        self.rules = rules
        self.aliases = use_aliases(dc.root, dc.source)
        self.namespace = namespace_of(dc.root, dc.source)

    def fqcn(self, written: str) -> str | None:
        return resolve_class(written, self.aliases, self.namespace)

    def type_kind(self, written: str | None) -> str | None:
        fqcn = self.fqcn(written) if written else None
        kind = self.rules.types.get(fqcn) if fqcn else None
        return self.rules.instance(kind) if kind is not None else None

    def class_kind(self, node: Node) -> str | None:
        name = node.child_by_field_name("name")
        if name is None:
            return None
        own = node_text(name, self.source)
        return self.rules.types.get(f"{self.namespace}\\{own}" if self.namespace else own)

    def base_kind(self, base: Node, calls: list[Node], scope: Mapping[str, str],
                  props: Mapping[str, str], this_kind: str | None) -> str | None:
        source, first = self.source, calls[0]
        scope_node = first.child_by_field_name("scope") if first.type == "scoped_call_expression" else None
        if scope_node is not None and (base.start_byte, base.end_byte) == (
                scope_node.start_byte, scope_node.end_byte):
            fqcn = self.fqcn(node_text(base, source))
            return self.rules.static(fqcn, method_name(first, source)) if fqcn else None
        if first.type == "function_call_expression":
            return self.rules.function(method_name(first, source))
        if base.type == "variable_name":
            name = node_text(base, source)
            if name == "$this":
                return self.rules.this_call(this_kind, method_name(first, source))
            return scope.get(name)
        if base.type == "member_access_expression":
            owner, prop = base.child_by_field_name("object"), base.child_by_field_name("name")
            if owner is None or prop is None or node_text(owner, source) != "$this":
                return None
            prop_name = node_text(prop, source)
            return props.get(prop_name) or self.rules.this_property(prop_name)
        return None

    def assigned_kind(self, value: Node, scope: Mapping[str, str], props: Mapping[str, str],
                      this_kind: str | None) -> str | None:
        value = unwrap(value) or value
        if value.type == "object_creation_expression":
            cls = next((c for c in value.named_children if c.type in {"name", "qualified_name"}), None)
            return self.type_kind(node_text(cls, self.source)) if cls is not None else None
        if value.type in PHP_CALL_TYPES:
            base, calls = chain(value)
            kind = self.base_kind(base, calls, scope, props, this_kind)
            if kind is not None:
                return self.rules.chain_result(kind, [method_name(c, self.source) for c in calls])
        return None

    def _params(self, fn: Node, *, promoted_only: bool = False) -> dict[str, str]:
        out: dict[str, str] = {}
        params = fn.child_by_field_name("parameters")
        for param in params.named_children if params is not None else []:
            if promoted_only and param.type != "property_promotion_parameter":
                continue
            name = param.child_by_field_name("name")
            kind = self.type_kind(_type_name(param.child_by_field_name("type"), self.source))
            if name is not None and kind is not None:
                out[node_text(name, self.source)] = kind
        return out

    def properties(self, cls: Node) -> dict[str, str]:
        """``$this->x`` → kind, from typed properties and promoted constructor parameters."""
        out: dict[str, str] = {}
        body = cls.child_by_field_name("body")
        for member in body.named_children if body is not None else []:
            if member.type == "property_declaration":
                kind = self.type_kind(_type_name(member.child_by_field_name("type"), self.source))
                for element in member.named_children:
                    name = element.child_by_field_name("name") if element.type == "property_element" else None
                    if kind is not None and name is not None:
                        out[node_text(name, self.source).lstrip("$")] = kind
            elif member.type == "method_declaration":
                out.update({k.lstrip("$"): v for k, v in self._params(member, promoted_only=True).items()})
        return out

    def local_kinds(self, fn: Node, inherited: Mapping[str, str], props: Mapping[str, str],
                    this_kind: str | None) -> dict[str, str]:
        """Variable → kind inside one function: typed parameters plus its own assignments."""
        scope = {**inherited, **self._params(fn)}
        assigned: dict[str, str | None] = {}
        for node in _own_nodes(fn.child_by_field_name("body")):
            if node.type != "assignment_expression":
                continue
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is None or right is None or left.type != "variable_name":
                continue
            name = node_text(left, self.source)
            kind = self.assigned_kind(right, scope, props, this_kind)
            assigned[name] = kind if assigned.get(name, kind) == kind else None
        for name, kind in assigned.items():
            if kind is None:
                scope.pop(name, None)
            else:
                scope[name] = kind
        return scope


def outermost_calls(dc: DetectContext, rules: Rules) -> Iterator[tuple[Node, list[Node], str]]:
    """Every outermost call chain of the file whose receiver is one of ``rules``' kinds:
    ``(outermost call, calls from the innermost, kind)``."""
    rx = _Receivers(dc, rules)
    found: list[tuple[Node, list[Node], str]] = []

    def visit(node: Node, scope: Mapping[str, str], props: Mapping[str, str],
              this_kind: str | None) -> None:
        for child in node.named_children:
            if child.type in _CLASSES:
                visit(child, {}, rx.properties(child), rx.class_kind(child))
                continue
            if child.type in _SCOPES:
                inherited = scope if child.type in {"anonymous_function", "arrow_function"} else {}
                visit(child, rx.local_kinds(child, inherited, props, this_kind), props, this_kind)
                continue
            if child.type in PHP_CALL_TYPES and _outermost(child):
                base, calls = chain(child)
                kind = rx.base_kind(base, calls, scope, props, this_kind)
                if kind is not None:
                    found.append((child, calls, kind))
            visit(child, scope, props, this_kind)

    visit(dc.root, {}, {}, None)
    return iter(found)
