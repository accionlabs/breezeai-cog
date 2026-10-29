"""Syntax helpers for the AngularJS detector: calls in a chain, object literals, a controller
written as a name, a function or an annotated array."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Node

from ..emits import Emit
from ..treesitter import entry_text, node_text
from ..typescript.navigation import literal

FRAMEWORK = "angularjs"

FUNCTIONS = frozenset({"function_expression", "function", "arrow_function", "method_definition"})
_NAME = re.compile(r"^[A-Za-z_$][\w$.]*$")
_ALIAS = re.compile(r"^\s*([A-Za-z_$][\w$.]*)\s+as\s+[A-Za-z_$][\w$]*\s*$")


@dataclass(frozen=True, slots=True)
class MethodCall:
    """``receiver.method(args)``."""

    call: Node
    receiver: Node
    method: Node
    args: Node

    def name(self, source: bytes) -> str:
        return node_text(self.method, source)

    @property
    def arguments(self) -> list[Node]:
        return self.args.named_children


@dataclass(slots=True)
class Scan:
    """What one walk of a file finds: its method calls, in source order, and the line of every
    function, class or object it declares by name (``None`` when the name is declared twice)."""

    calls: list[MethodCall] = field(default_factory=list)
    declared: dict[str, int | None] = field(default_factory=dict)
    #: Variables assigned from ``angular.module(…)`` (or a chain on it) in this file.
    modules: set[str] = field(default_factory=set)
    #: Calls of the injected ``$resource`` service: ``$resource('/api/users/:id')``.
    resources: list[Node] = field(default_factory=list)


def scan(root: Node, source: bytes) -> Scan:
    """Walk ``root`` once. Iterative: a chain of a hundred ``.state(…)`` calls is a tree a
    hundred calls deep."""
    found = Scan()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            mc = _method_call(node)
            if mc is not None:
                found.calls.append(mc)
            else:
                fn = node.child_by_field_name("function")
                if fn is not None and is_identifier(fn, source, "$resource"):
                    found.resources.append(node)
        elif node.type in (
            "function_declaration",
            "generator_function_declaration",
            "class_declaration",
        ):
            _declare(found, node.child_by_field_name("name"), node, source)
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in (
                "function_expression",
                "arrow_function",
                "class",
                "object",
            ):
                _declare(found, node.child_by_field_name("name"), node, source)
            elif value is not None and starts_at_angular_module(value, source):
                _bind_module(found, node.child_by_field_name("name"), source)
        elif node.type == "assignment_expression":
            value = node.child_by_field_name("right")
            if value is not None and starts_at_angular_module(value, source):
                _bind_module(found, node.child_by_field_name("left"), source)
        stack.extend(reversed(node.named_children))
    return found


def _method_call(node: Node) -> MethodCall | None:
    fn = node.child_by_field_name("function")
    args = node.child_by_field_name("arguments")
    if fn is None or args is None or fn.type != "member_expression":
        return None
    receiver = fn.child_by_field_name("object")
    method = fn.child_by_field_name("property")
    if receiver is None or method is None or method.type != "property_identifier":
        return None
    return MethodCall(node, receiver, method, args)


def _bind_module(found: Scan, name: Node | None, source: bytes) -> None:
    if name is not None and name.type == "identifier":
        found.modules.add(node_text(name, source))


def _declare(found: Scan, name: Node | None, node: Node, source: bytes) -> None:
    if name is None or name.type not in ("identifier", "type_identifier"):
        return
    key, line = node_text(name, source), node.start_point[0] + 1
    found.declared[key] = line if found.declared.get(key, line) == line else None


def chain_root(node: Node) -> Node:
    """What a call chain starts from: ``$routeProvider`` in ``$routeProvider.when(a).when(b)``."""
    while True:
        if node.type == "call_expression":
            inner = node.child_by_field_name("function")
        elif node.type == "member_expression":
            inner = node.child_by_field_name("object")
        else:
            return node
        if inner is None:
            return node
        node = inner


def starts_at_angular_module(node: Node, source: bytes) -> bool:
    """Whether a call chain starts at ``angular.module(…)``."""
    while True:
        if node.type == "call_expression":
            inner = node.child_by_field_name("function")
        elif node.type == "member_expression":
            inner = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if (
                inner is not None
                and prop is not None
                and is_identifier(inner, source, "angular")
                and node_text(prop, source) == "module"
            ):
                return True
        else:
            return False
        if inner is None:
            return False
        node = inner


def is_identifier(node: Node, source: bytes, name: str) -> bool:
    return node.type == "identifier" and node_text(node, source) == name


def object_pairs(obj: Node, source: bytes) -> dict[str, Node]:
    """``{ key: value }`` members by key; a method ``controller() {}`` maps to itself. Spreads
    and computed keys are skipped."""
    out: dict[str, Node] = {}
    for member in obj.named_children:
        if member.type == "pair":
            key, value = member.child_by_field_name("key"), member.child_by_field_name("value")
        elif member.type == "method_definition":
            key, value = member.child_by_field_name("name"), member
        else:
            continue
        if (
            key is not None
            and value is not None
            and key.type in ("property_identifier", "string", "identifier")
        ):
            out[node_text(key, source).strip("'\"")] = value
    return out


def unwrap(node: Node) -> Node:
    """The function of an annotated array — ``['$scope', function ($scope) {}]`` — else the node."""
    return node.named_children[-1] if node.type == "array" and node.named_children else node


@dataclass(frozen=True, slots=True)
class Handler:
    """A controller or component as the code writes it."""

    name: str | None = None
    #: Where an inline function starts (it has no name of its own).
    line: int | None = None
    #: The name is a registered name in a string (``'ListCtrl'``), found through the
    #: registration, not a variable the language can look up.
    registered: bool = False


def handler_of(node: Node, source: bytes) -> Handler:
    """``'Name'`` / ``'Name as vm'`` → the registered name; a function (also the last element of
    an annotated array) is anonymous, so only its line; a variable → its name; anything else →
    nothing."""
    node = unwrap(node)
    if node.type in FUNCTIONS:
        return Handler(line=node.start_point[0] + 1)
    if node.type == "identifier":
        return Handler(name=node_text(node, source))
    text = literal(node, source)
    if text is None:
        return Handler()
    alias = _ALIAS.match(text)
    name = alias.group(1) if alias else text.strip()
    return Handler(name=name, registered=True) if _NAME.match(name) else Handler()


def cut_query(path: str) -> str:
    """The address without a query declaration or fragment (``/users?page`` → ``/users``)."""
    return re.split(r"[?#]", path, maxsplit=1)[0]


def call_emit(mc: MethodCall, source: bytes, fields: dict[str, object]) -> Emit:
    """One record for one call of a chain, anchored at the method name (``when`` / ``state`` /
    ``controller``) so its line, column and text — ``when('/x', { … })`` on one line — are its
    own, not the chain's."""
    return Emit(
        mc.method,
        {"framework": FRAMEWORK, **fields},
        text=node_text(mc.method, source) + entry_text(mc.args, source, nested=()),
        node_type="call_expression",
        end_line=mc.call.end_point[0] + 1,
    )
