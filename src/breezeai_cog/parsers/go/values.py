"""Typed values of a Go function, for the detectors of libraries that are not routers (message
brokers, schedulers): which local names hold a value of a library type, and what the value was
built with. Like the router analysis (``routing.py``), a value is known from how it is bound,
never from its name:

- a parameter, receiver field or struct field declared with the type (``nc *nats.Conn``,
  ``s.reader`` where ``reader *kafka.Reader``);
- a constructor of the package (``nats.Connect(…)``, ``kafka.NewReader(cfg)``), also with an
  ``err`` beside it (``nc, err := nats.Connect(url)``);
- a composite literal of the type (``&kafka.Writer{Topic: "orders"}``);
- a method of a typed value that returns another library type (``nc.JetStream()``).

A :class:`ValueSpec` says which types and constructors a library has; the detector gets, for
every call ``x.M(args)`` whose receiver is such a value, the value and its construction node (to
read a ``Topic`` from a config literal, say)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from tree_sitter import Node

from ..treesitter import node_text
from .routing import _Analyzer


@dataclass(frozen=True)
class ValueSpec:
    #: Package import path; versioned paths (``…/v2``) match too.
    import_path: str
    types: frozenset[str]
    #: Package function → the type it returns.
    constructors: dict[str, str] = field(default_factory=dict)
    #: (type, method) → the type that method returns (``("Conn", "JetStream")`` →
    #: ``JetStreamContext``).
    derived: dict[tuple[str, str], str] = field(default_factory=dict)

    def owns(self, import_path: str) -> bool:
        if import_path == self.import_path:
            return True
        if not import_path.startswith(self.import_path + "/"):
            return False
        rest = import_path[len(self.import_path) + 1:]
        return rest[:1] == "v" and rest[1:].isdigit()


@dataclass(frozen=True)
class Value:
    type: str
    #: What built it: the constructor call or composite literal, when the function shows it.
    made_by: Node | None = None


@dataclass(frozen=True)
class TypedCall:
    call: Node
    value: Value
    method: str
    args: list[Node]
    #: The named function or method the call is in (None at package level).
    function: Node | None


def _args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return [a for a in args.named_children if a.type != "comment"] if args is not None else []


class _Values:
    def __init__(self, analyzer: _Analyzer, spec: ValueSpec) -> None:
        self.a, self.spec = analyzer, spec
        self.aliases = {alias for alias, ip in analyzer.aliases.items() if spec.owns(ip)}

    def type_of_text(self, text: str) -> str | None:
        alias, dot, name = text.lstrip("*").partition(".")
        name = name.split("[", 1)[0]
        return name if dot and alias in self.aliases and name in self.spec.types else None

    def field_type(self, struct: str | None, fname: str) -> str | None:
        if struct is None:
            return None
        if (struct, fname) in self.a.local_fields:
            return self.type_of_text(self.a.local_fields[(struct, fname)])
        qualified = self.a.decls.fields.get((self.a.pkg, struct, fname))
        if qualified is not None and self.spec.owns(qualified[0]) and qualified[1] in self.spec.types:
            return qualified[1]
        return None

    def eval(self, node: Node | None, env: dict) -> Value | None:
        if node is None:
            return None
        kind = node.type
        if kind == "parenthesized_expression" and node.named_children:
            return self.eval(node.named_children[0], env)
        if kind == "unary_expression":
            inner = node.child_by_field_name("operand")
            return self.eval(inner, env)
        if kind == "identifier":
            value = env.get(node_text(node, self.a.source))
            return value if isinstance(value, Value) else None
        if kind == "selector_expression":
            operand, fld = node.child_by_field_name("operand"), node.child_by_field_name("field")
            if operand is None or fld is None or operand.type != "identifier":
                return None
            typ = self.field_type(env.get(("type", node_text(operand, self.a.source))),
                                  node_text(fld, self.a.source))
            return Value(typ) if typ else None
        if kind == "composite_literal":
            typ = self.type_of_text(node_text(node.child_by_field_name("type"), self.a.source))
            return Value(typ, node) if typ else None
        if kind != "call_expression":
            return None
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "selector_expression":
            return None
        operand = fn.child_by_field_name("operand")
        name = node_text(fn.child_by_field_name("field"), self.a.source)
        if operand is not None and operand.type == "identifier" and node_text(operand, self.a.source) in self.aliases:
            typ = self.spec.constructors.get(name)
            return Value(typ, node) if typ else None
        base = self.eval(operand, env)
        if base is not None and (base.type, name) in self.spec.derived:
            return Value(self.spec.derived[(base.type, name)], node)
        return None

    def bind_params(self, params: Node | None, env: dict) -> None:
        for decl in params.named_children if params is not None else []:
            if decl.type not in {"parameter_declaration", "variadic_parameter_declaration"}:
                continue
            tnode = decl.child_by_field_name("type")
            text = node_text(tnode, self.a.source) if tnode is not None else ""
            typ = self.type_of_text(text)
            for name in decl.children_by_field_name("name"):
                pname = node_text(name, self.a.source)
                env[("type", pname)] = text.lstrip("*").split("[", 1)[0]
                if typ:
                    env[pname] = Value(typ)

    def walk(self, node: Node, env: dict, function: Node | None) -> Iterator[TypedCall]:
        for child in node.named_children:
            kind = child.type
            if kind in {"short_var_declaration", "assignment_statement"}:
                yield from self.walk(child, env, function)
                left, right = child.child_by_field_name("left"), child.child_by_field_name("right")
                lefts = list(left.named_children) if left is not None else []
                rights = list(right.named_children) if right is not None else []
                if len(rights) == 1 and len(lefts) > 1:  # `nc, err := nats.Connect(url)`
                    lefts = lefts[:1]
                for lhs, rhs in zip(lefts, rights):
                    if lhs.type == "identifier":
                        value = self.eval(rhs, env)
                        name = node_text(lhs, self.a.source)
                        if value is not None:
                            env[name] = value
                        else:
                            env.pop(name, None)
                continue
            if kind == "var_spec":
                yield from self.walk(child, env, function)
                value = child.child_by_field_name("value")
                tnode = child.child_by_field_name("type")
                values = list(value.named_children) if value is not None else []
                for i, name in enumerate(child.children_by_field_name("name")):
                    found = self.eval(values[i], env) if i < len(values) else None
                    if found is None and tnode is not None:
                        typ = self.type_of_text(node_text(tnode, self.a.source))
                        found = Value(typ) if typ else None
                    if found is not None:
                        env[node_text(name, self.a.source)] = found
                continue
            if kind == "func_literal":
                inner = dict(env)
                self.bind_params(child.child_by_field_name("parameters"), inner)
                body = child.child_by_field_name("body")
                if body is not None:
                    yield from self.walk(body, inner, function)
                continue
            if kind == "call_expression":
                fn = child.child_by_field_name("function")
                if fn is not None and fn.type == "selector_expression":
                    value = self.eval(fn.child_by_field_name("operand"), env)
                    if value is not None:
                        yield TypedCall(child, value, node_text(fn.child_by_field_name("field"), self.a.source),
                                        _args(child), function)
            yield from self.walk(child, env, function)


def typed_calls(analyzer: _Analyzer, spec: ValueSpec) -> Iterator[TypedCall]:
    """Every call on a value of ``spec``'s types, in document order, function by function."""
    values = _Values(analyzer, spec)
    if not values.aliases:
        return
    globals_: dict = {}
    for decl in analyzer.root.named_children:
        if decl.type == "var_declaration":
            for _ in values.walk(decl, globals_, None):
                pass
    for decl in analyzer.root.named_children:
        if decl.type not in {"function_declaration", "method_declaration"}:
            continue
        env = dict(globals_)
        recv = decl.child_by_field_name("receiver")
        values.bind_params(recv, env)
        values.bind_params(decl.child_by_field_name("parameters"), env)
        body = decl.child_by_field_name("body")
        if body is not None:
            yield from values.walk(body, env, decl)


def call_type(analyzer: _Analyzer, spec: ValueSpec, call: Node) -> str | None:
    """``pkg.F(…)`` where ``pkg`` is the spec's package → ``F``; else None."""
    fn = call.child_by_field_name("function") if call.type == "call_expression" else None
    if fn is None or fn.type != "selector_expression":
        return None
    operand = fn.child_by_field_name("operand")
    if operand is None or operand.type != "identifier":
        return None
    ip = analyzer.aliases.get(node_text(operand, analyzer.source))
    return node_text(fn.child_by_field_name("field"), analyzer.source) if ip and spec.owns(ip) else None


def handler_fill(analyzer: _Analyzer, call: Node, handler: Node | None,
                 function: Node | None) -> dict:
    """``handler`` / ``handlerLine`` / ``handlerPath`` of a binding: the callback argument when
    there is one (located like a router handler), else the function the call is in — the code
    that receives (a ``for { r.ReadMessage(…) }`` loop, a ticker's ``select``)."""
    from .routing import _locate, types_by_function

    if handler is not None:
        name, line, path = _locate(analyzer, handler, types_by_function(analyzer)(call))
        if name or line:
            return {"handler": name, "handlerLine": line, "handlerPath": path}
        return {}
    if function is None:
        return {}
    fname = function.child_by_field_name("name")
    return {"handler": node_text(fname, analyzer.source) if fname is not None else None,
            "handlerLine": function.start_point[0] + 1}
