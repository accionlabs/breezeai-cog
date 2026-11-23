"""SQL built up in a local variable over several statements, and the statement that runs it.

A query is often assembled before it is run::

    sql = "SELECT id, total "
    sql += "FROM orders"
    if active_only:
        sql += " WHERE active = 1"
    cur.execute(sql)

No single statement holds the SQL, so the per-statement check (``string_has_query``) sees none.
:func:`built_queries` reads one function body in source order and follows each local whose
value is only string literals: assigned (``=``), appended (``+=``, PHP ``.=``, VB ``&=``,
``x = x + "…"``) or, for a Java/C# ``StringBuilder``, ``append``-ed. Where such a local is the
first argument of a call, the first argument of a database command constructor
(``new SqlCommand(sql, conn)``) or the value of a command's ``CommandText``, its pieces are
joined and must start with SQL. That one statement is then a query; the assignments never are.
For a call, the statement classifier decides whether the call is a data-access call.

Wrong is worse than missing, so a local is left out for the whole body when anything else
could reach its value: a parameter of that name, a value that is not a literal (a call result,
a field, a parameter, another function's string), a write this pass does not recognise (loop
variable, tuple or destructuring target, ``out``/``ref``, ``with … as``), a write inside a
nested function or lambda, or, in PHP and VB, being passed to a helper that could take it by
reference. A local whose first value already holds SQL on its own is left out too: that
statement is the query's record already.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Node

from .detection import text_has_query
from .detection.queries import (
    built_string_has_query,
    is_sql_command_type,
    is_sql_sink,
    sql_argument,
)
from .statements_common import (
    _ADJACENT_STRINGS,
    _STRING_LITERALS,
    _is_concat,
    _string_content,
    node_has_query,
)
from .treesitter import node_text

# A body is read only when its text has a quoted SQL verb; most bodies stop here.
_GATE = re.compile(
    r"""["'`]\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|MERGE|WITH)\b""", re.IGNORECASE
)

#: Past these sizes a local is left out (a generated file building one huge string).
_MAX_PARTS = 200
_MAX_CHARS = 64 * 1024

#: Name nodes: an identifier, a PHP ``$variable``.
_NAMES = frozenset({"identifier", "variable_name"})
#: A destructuring name (``const { sql } = o``) always binds.
_BINDING_NAMES = frozenset({"shorthand_property_identifier_pattern"})
#: Single-child wrappers around a name or a value (VB ``expression``/``literal``, C#/PHP
#: ``argument``, VB ``left_hand_side``).
_WRAPPERS = frozenset({
    "expression", "literal", "parenthesized_expression", "argument", "left_hand_side",
})

#: Nested functions, lambdas and classes across the six grammars. A write inside one is not
#: in the body's order, so it leaves the local out.
_NESTED = frozenset({
    "function_definition", "class_definition", "lambda", "decorated_definition",
    "lambda_expression", "anonymous_method_expression", "local_function_statement",
    "class_declaration", "method_declaration", "constructor_declaration",
    "anonymous_function", "arrow_function", "function_declaration", "function_expression",
    "method_definition", "class", "generator_function", "generator_function_declaration",
    "multi_line_lambda", "single_line_lambda",
    # C# members with a body of their own, and Java initializer blocks and record constructors
    "accessor_declaration", "operator_declaration", "conversion_operator_declaration",
    "destructor_declaration", "indexer_declaration", "static_initializer",
    "compact_constructor_declaration",
})

#: Constants that only add a line break, joined as whitespace.
_NEWLINES = frozenset({
    "vbcrlf", "vbnewline", "vblf", "vbcr", "controlchars.crlf", "controlchars.newline",
    "controlchars.lf", "environment.newline", "system.environment.newline", "php_eol",
})

#: StringBuilder types, the methods that only add text, and those that only read it.
_BUILDERS = frozenset({"StringBuilder", "StringBuffer", "System.Text.StringBuilder"})
_APPEND = frozenset({"append", "Append", "AppendLine"})
_TO_STRING = frozenset({"toString", "ToString"})
_BUILDER_READS = _TO_STRING | {"length", "Length", "isEmpty"}

#: Assignment ops that append; any other compound op on a tracked local leaves it out.
_APPEND_OPS = frozenset({"+=", ".=", "&="})
#: The ADO.NET command property a query string is assigned to.
_COMMAND_TEXT = "CommandText"

_CALLS = frozenset({
    "method_invocation", "invocation_expression", "invocation", "call_expression", "call",
    "member_call_expression", "function_call_expression", "scoped_call_expression",
})
_CONSTRUCTORS = frozenset({"object_creation_expression", "new_expression"})

#: Fields holding a value or a read — a name under one is never a binding.
_READ_FIELDS = frozenset({
    "right", "value", "initializer", "condition", "arguments", "object", "function",
    "expression", "body", "consequence", "alternative", "argument", "member", "attribute",
    "property", "index", "subscript", "operand", "receiver", "then_branch", "else_branch",
})
#: Fields where a name is bound (in an assignment, a loop, a declaration…).
_BINDING_FIELDS = frozenset({"left", "name", "alias", "target", "pattern", "variable", "key"})
#: Nodes a name passes through on its way to a binding position (tuple and list targets).
_PATTERN_WRAPPERS = frozenset({
    "expression", "parenthesized_expression", "tuple", "tuple_pattern", "pattern_list",
    "list_pattern", "list", "array_pattern", "object_pattern", "array", "left_hand_side",
    "list_literal", "expression_list", "list_splat_pattern", "pair_pattern",
    "assignment_pattern", "rest_pattern", "tuple_expression", "variable_list",
    "parenthesized_variable_designation", "tuple_element", "pair", "argument",
    "dotted_name",  # a Python ``case`` capture: ``case [sql]:``
})
#: Nodes whose names are bindings wherever they sit (outside their read fields).
_BINDERS = frozenset({
    "as_pattern_target", "global_statement", "nonlocal_statement", "global_declaration",
    "static_variable_declaration", "catch_declaration", "declaration_expression",
    "declaration_pattern", "by_ref", "reference_modifier", "foreach_statement",
    "for_each_statement", "catch_clause", "for_in_clause", "variable_declarator",
    "dim_statement", "named_expression", "enhanced_for_statement", "for_statement",
    "for_in_statement", "reference_assignment_expression", "list_literal",
    "catch_formal_parameter", "implicit_parameter_list", "case_pattern",
})


@dataclass
class BuiltSql:
    """Statements that run SQL built up in a local, by span.

    ``calls`` — a call whose first argument is the local → the joined text, for the call
    classifier to check like a literal argument. ``direct`` — a database command constructor
    or a ``CommandText`` assignment, a query on its own; ``direct_types`` their node types."""

    calls: dict[tuple[int, int], str] = field(default_factory=dict)
    direct: dict[tuple[int, int], str] = field(default_factory=dict)
    direct_types: frozenset[str] = frozenset()


@dataclass
class _Local:
    parts: list[str]
    built: bool = False  # extended at least once, or copied from a local that was
    builder: bool = False
    recorded: bool = False  # a write already holds SQL: that statement is the record
    deps: set[str] = field(default_factory=set)


@dataclass
class _Use:
    name: str
    parts: list[str]
    owner: Node
    direct: bool


def built_queries(body: Node | None, source: bytes, language: str) -> BuiltSql | None:
    """The statements in ``body`` that run SQL built up in a local, or ``None`` when there are
    none. Never raises: an unexpected tree leaves the body as it was."""
    if body is None or not _GATE.search(node_text(body, source)):
        return None
    try:
        return _Pass(source, language).run(body)
    except Exception:  # noqa: BLE001 — never lose a file's statements over this pass
        return None


def _strip(node: Node) -> Node:
    while node.type in _WRAPPERS and node.named_child_count == 1:
        node = node.named_children[0]
    return node


#: Children that put a value into a string literal (an f-string ``{x}``, a template
#: ``${x}``, C#/VB ``$"{x}"``, a PHP ``"…$x…"``).
_INTERPOLATIONS = frozenset({
    "interpolation", "template_substitution", "variable_name", "member_access_expression",
    "subscript_expression", "dynamic_variable_name",
})


def _interpolates(node: Node) -> bool:
    return any(c.type in _INTERPOLATIONS for c in node.named_children)


def _field_of(child: Node) -> str | None:
    parent = child.parent
    if parent is None:
        return None
    for i, c in enumerate(parent.children):
        if c.id == child.id:
            return parent.field_name_for_child(i)
    return None


class _Pass:
    def __init__(self, source: bytes, language: str) -> None:
        self.source = source
        self.language = language
        self.locals: dict[str, _Local] = {}
        self.out: set[str] = set()  # left out for the whole body
        self.uses: list[_Use] = []
        self.handled: set[int] = set()  # name nodes a recognised write or use accounts for
        self.chained: set[int] = set()  # inner calls of an ``sb.append(a).append(b)`` chain
        self.fields: dict[int, str | None] = {}  # node id → its field in its parent
        self.unknowable = False  # PHP ``extract()`` can set any local

    def text(self, node: Node) -> str:
        return node_text(node, self.source)

    def key(self, node: Node) -> str:
        return self.text(node).casefold() if self.language == "vb" else self.text(node)

    def name_of(self, node: Node | None) -> str | None:
        if node is None:
            return None
        node = _strip(node)
        return self.key(node) if node.type in _NAMES else None

    # ── the pass ─────────────────────────────────────────────────────────────
    def run(self, body: Node) -> BuiltSql | None:
        self.leave_out_parameters(body)
        # Pre-order over the body, not into nested functions: each has its own pass, and a
        # local mentioned in one is left out (it may be written there, out of order).
        nodes: list[Node] = []
        nested: list[Node] = []
        stack = [body]
        while stack:
            node = stack.pop()
            if node.id != body.id and node.type in _NESTED:
                nested.append(node)
                continue
            nodes.append(node)
            for i, c in enumerate(node.children):
                if c.is_named:
                    self.fields[c.id] = node.field_name_for_child(i)
            stack.extend(reversed(node.named_children))
        # A write takes effect at its end, after the uses inside its value.
        keyed = []
        for order, node in enumerate(nodes):
            late = self.write_of(node) is not None or self.append_chain(node) is not None
            keyed.append((node.end_byte if late else node.start_byte, order, node))
        keyed.sort(key=lambda e: (e[0], e[1]))
        for _, _, node in keyed:
            self.visit(node)
        for node in nodes:
            if node.type in _BINDING_NAMES:
                self.out.add(self.key(node))
            elif node.type in _NAMES and (self.referenced(node) or (
                    node.id not in self.handled and self.binds(node))):
                self.out.add(self.key(node))
        self.leave_out_mentioned(nested)
        return self.result()

    def leave_out_mentioned(self, nested: list[Node]) -> None:
        names = {u.name for u in self.uses} - self.out
        if not names or not nested:
            return
        flags = re.IGNORECASE if self.language == "vb" else 0
        texts = [self.text(n) for n in nested]
        for name in names:
            mention = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?!\w)", flags)
            if any(mention.search(t) for t in texts):
                self.out.add(name)

    def leave_out_parameters(self, body: Node) -> None:
        owner = body.parent
        if owner is None:
            return
        for child in owner.named_children:
            if child.id == body.id or "parameter" not in child.type:
                continue
            todo = [child]
            while todo:
                n = todo.pop()
                if n.type in _NAMES:
                    self.out.add(self.key(n))
                todo.extend(n.named_children)

    def visit(self, node: Node) -> None:
        write = self.write_of(node)
        if write is not None:
            self.apply_write(*write)
            return
        chain = self.append_chain(node)
        if chain is not None:
            self.apply_appends(*chain)
            return
        if node.id in self.chained:
            return
        if node.type in _CALLS or node.type in _CONSTRUCTORS:
            self.check_call(node)

    # ── writes ───────────────────────────────────────────────────────────────
    def write_of(self, node: Node) -> tuple[Node, str, Node | None] | None:
        """``(target, op, value)`` for an assignment or a declaration in any of the six
        grammars: op is ``=``, ``let`` (a declaration), an append op, another compound op,
        or ``decl`` (a declaration without a value)."""
        t = node.type
        if t in ("variable_declarator", "dim_statement"):
            name = node.child_by_field_name("name")
            if name is None:
                return None
            value = node.child_by_field_name("value") or node.child_by_field_name("initializer")
            if value is None and t == "variable_declarator":  # C#: no field on the value
                value = next((c for c in node.named_children if c.id != name.id
                              and c.type != "bracketed_argument_list"), None)
            return (name, "let", value) if value is not None else (name, "decl", None)
        if t in ("assignment", "assignment_expression", "augmented_assignment",
                 "augmented_assignment_expression", "compound_assignment_statement"):
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is None or right is None:
                return None
            return left, self.op(node, left, right), right
        if t == "binary_expression" and self.language == "vb" and self.vb_statement(node):
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is None or right is None or self.op(node, left, right) != "=":
                return None
            inner = _strip(left)
            # VB reads ``sql &= "x"`` as ``(sql &) = "x"``: an append to ``sql``.
            if inner.type == "binary_expression":
                ileft, iright = inner.child_by_field_name("left"), inner.child_by_field_name("right")
                if (ileft is not None and iright is not None and not self.text(iright).strip()
                        and self.op(inner, ileft, iright) in ("&", "+")):
                    return ileft, "&=", right
                return None
            return left, "=", right
        return None

    def op(self, node: Node, left: Node, right: Node) -> str:
        op = node.child_by_field_name("operator")
        if op is not None:
            return self.text(op).strip()
        return self.source[left.end_byte:right.start_byte].decode("utf-8", "replace").strip()

    @staticmethod
    def vb_statement(node: Node) -> bool:
        """A VB ``=`` that is an assignment (a statement or an ``If … Then`` branch), not a
        comparison."""
        child, parent = node, node.parent
        while parent is not None and parent.type == "expression":
            child, parent = parent, parent.parent
        if parent is None:
            return False
        return parent.type == "call_statement" or _field_of(child) in ("then_branch", "else_branch")

    def apply_write(self, target: Node, op: str, value: Node | None) -> None:
        name = self.name_of(target)
        if name is None:
            self.check_command_text(target, value)
            return  # a field, an element, a property — not a local
        self.handled.add(_strip(target).id)
        if name in self.out:
            return  # already left out: nothing more to follow
        if op in ("let", "decl") and name in self.locals:
            self.out.add(name)  # declared again (a JS block scope): a different local
            return
        if op == "decl":
            return
        if value is None:
            self.out.add(name)
            return
        local = self.locals.get(name)
        if op in ("=", "let"):
            builder = self.new_builder(value)
            if builder is not None:
                self.locals[name] = _Local(parts=builder, builder=True,
                                           recorded=self.holds_query(value))
                return
            got = self.parts_of(value)
            if got is None:
                self.out.add(name)
                return
            parts, deps, built = got
            built = built or bool(deps)  # ``sql = base + "FROM t"``: base was set before
            if local is None:
                local = self.locals[name] = _Local(parts=parts, built=built, deps=deps,
                                                   recorded=self.holds_query(value))
            elif local.builder:
                self.out.add(name)
                return
            else:
                local.parts, local.built = parts, True
                local.deps |= deps
                local.recorded |= self.holds_query(value)
        elif op in _APPEND_OPS:
            got = self.parts_of(value)
            if local is None or local.builder or got is None:
                self.out.add(name)
                return
            local.parts, local.built = local.parts + got[0], True
            local.deps |= got[1]
            local.recorded |= self.holds_query(value)
        else:
            self.out.add(name)
            return
        self.cap(name, local)

    def holds_query(self, value: Node) -> bool:
        """The statement's own check: does this value already hold SQL in its literals?"""
        return text_has_query(self.text(value)) and node_has_query(value, self.source)

    def cap(self, name: str, local: _Local) -> None:
        if len(local.parts) > _MAX_PARTS or sum(map(len, local.parts)) > _MAX_CHARS:
            self.out.add(name)
            local.parts = []

    def parts_of(self, value: Node) -> tuple[list[str], set[str], bool] | None:
        """``(pieces, locals read, built)`` of a value, or ``None`` when a piece is not a
        literal, a newline constant or a tracked local (incl. a builder's ``toString()``)."""
        parts: list[str] = []
        deps: set[str] = set()
        built = False
        todo = [value]
        while todo:
            n = _strip(todo.pop())
            if n.type in _STRING_LITERALS:
                if _interpolates(n):
                    return None  # f"…{uid}", `…${id}`, $"…{x}", "…$x…": an outside value
                parts.append(_string_content(n, self.source))
            elif n.type in _ADJACENT_STRINGS:
                parts.extend(_string_content(c, self.source) for c in n.named_children)
            elif _is_concat(n, self.source):
                todo += [n.named_children[-1], n.named_children[0]]
            elif self.text(n).replace(" ", "").casefold() in _NEWLINES:
                parts.append(" ")
            else:
                name = self.name_of(n)
                if name is not None:
                    self.handled.add(n.id)
                else:
                    name = self.builder_read(n)
                local = self.locals.get(name) if name is not None else None
                if name is None or local is None:
                    return None
                parts.extend(local.parts)
                deps.add(name)
                built = built or local.built
        return parts, deps, built

    # ── StringBuilder ────────────────────────────────────────────────────────
    def new_builder(self, value: Node) -> list[str] | None:
        n = _strip(value)
        if n.type != "object_creation_expression":
            return None
        type_node = n.child_by_field_name("type")
        if type_node is None or self.text(type_node) not in _BUILDERS:
            return None
        args = n.child_by_field_name("arguments")
        named = [_strip(a) for a in args.named_children] if args is not None else []
        if not named:
            return []
        if len(named) == 1 and named[0].type in ("integer_literal", "decimal_integer_literal"):
            return []  # a capacity
        got = self.parts_of(named[0]) if len(named) == 1 else None
        return got[0] if got is not None else None  # anything else: not a builder we follow

    def member_call(self, call: Node) -> tuple[Node, str, Node | None] | None:
        """``(receiver, method name, arguments)`` for ``x.m(…)`` in the six grammars."""
        if call.type not in _CALLS:
            return None
        obj = call.child_by_field_name("object") or call.child_by_field_name("scope")  # PHP ``A::m``
        name = call.child_by_field_name("name")
        if obj is None or name is None:
            fn = call.child_by_field_name("function") or call.child_by_field_name("target")
            if fn is None:
                return None
            obj = fn.child_by_field_name("expression") or fn.child_by_field_name("object")
            name = (fn.child_by_field_name("name") or fn.child_by_field_name("member")
                    or fn.child_by_field_name("property") or fn.child_by_field_name("attribute"))
            if obj is None or name is None:
                return None
        return obj, self.text(name), call.child_by_field_name("arguments")

    def builder_read(self, node: Node) -> str | None:
        """The builder's name for ``sb.toString()`` / ``sb.ToString()``, else ``None``."""
        call = self.member_call(_strip(node))
        if call is None or call[1] not in _TO_STRING:
            return None
        name = self.name_of(call[0])
        local = self.locals.get(name) if name is not None else None
        if local is None or not local.builder:
            return None
        self.handled.add(_strip(call[0]).id)
        return name

    def append_chain(self, node: Node) -> tuple[str, list[tuple[Node, bool]]] | None:
        """``sb.append(a).append(b);`` as a statement → (``sb``, [(a, line?), (b, line?)])."""
        if node.type not in _CALLS or node.parent is None or node.parent.type not in ("expression_statement", "expression"):
            return None
        args: list[tuple[Node, bool]] = []
        inner: list[int] = []
        n = node
        while True:
            call = self.member_call(n)
            if call is None or call[1] not in _APPEND:
                break
            named = list(call[2].named_children) if call[2] is not None else []
            if len(named) != 1:
                return None
            args.append((named[0], call[1] == "AppendLine"))
            inner.append(n.id)
            n = _strip(call[0])
        if not args or n.type not in _NAMES:
            return None
        self.chained.update(inner[1:])
        self.handled.add(n.id)
        return self.key(n), list(reversed(args))

    def apply_appends(self, name: str, args: list[tuple[Node, bool]]) -> None:
        if name in self.out:
            return
        local = self.locals.get(name)
        if local is None or not local.builder:
            self.out.add(name)
            return
        for arg, line in args:
            got = self.parts_of(arg)
            if got is None:
                self.out.add(name)
                return
            local.parts = local.parts + got[0] + ([" "] if line else [])
            local.deps |= got[1]
        local.built = True
        self.cap(name, local)

    # ── uses ─────────────────────────────────────────────────────────────────
    def check_call(self, node: Node) -> None:
        """Record a call or command constructor whose SQL argument (the first, or the one a PHP
        ``mysqli_query($conn, $sql)``-style function takes) is a tracked local, and leave out a
        local passed where it could be changed."""
        args = node.child_by_field_name("arguments")
        if args is None:
            args = next((c for c in node.named_children if c.type == "argument_list"), None)
        if args is None:
            return
        call = self.member_call(node)
        if call is not None:  # a builder method other than append / a read changes it
            receiver = self.name_of(call[0])
            local = self.locals.get(receiver) if receiver is not None else None
            if receiver is not None and local is not None and local.builder:
                self.handled.add(_strip(call[0]).id)
                if call[1] not in _BUILDER_READS:
                    self.out.add(receiver)
        if node.type in _CONSTRUCTORS:
            type_node = node.child_by_field_name("type")
            sink = type_node is not None and is_sql_command_type(self.text(type_node))
        else:
            fn = node.child_by_field_name("function") or node.child_by_field_name("target")
            callee = self.text(fn) if fn is not None else (
                f"{self.text(call[0])}.{call[1]}" if call is not None else "")
            method = call[1] if call is not None else callee.rsplit(".", 1)[-1]
            sink = is_sql_sink(method, callee) or is_sql_command_type(method)
            if self.language == "php" and method.lower() == "extract":
                self.unknowable = True
        named = args.named_children
        at = sql_argument(method if node.type not in _CONSTRUCTORS else "", len(named))
        for i, arg in enumerate(named):
            value = _strip(arg)
            name = self.name_of(value)
            if name is not None:
                self.handled.add(value.id)
                local = self.locals.get(name)
                if local is not None and local.builder:
                    self.out.add(name)  # a builder handed to a helper may be appended to
                    continue
            else:
                name = self.builder_read(value)
                local = self.locals.get(name) if name is not None else None
            if name is None:
                continue
            if self.text(arg).lstrip().startswith(("ref ", "out ", "&")):
                self.out.add(name)
                continue
            if self.language in ("php", "vb") and not (sink and i == at):
                self.out.add(name)  # may be taken by reference
            if i == at and local is not None and (sink or node.type in _CALLS):
                self.uses.append(_Use(name, list(local.parts), node,
                                      direct=node.type in _CONSTRUCTORS))

    def check_command_text(self, target: Node, value: Node | None) -> None:
        target = _strip(target)
        prop = target.child_by_field_name("name") or target.child_by_field_name("member")
        if prop is None or self.text(prop) != _COMMAND_TEXT or value is None:
            return
        value = _strip(value)
        name = self.name_of(value)
        if name is not None:
            self.handled.add(value.id)
        else:
            name = self.builder_read(value)
        local = self.locals.get(name) if name is not None else None
        if name is not None and local is not None:
            owner = target.parent
            while owner is not None and owner.type in ("expression", "left_hand_side"):
                owner = owner.parent
            if owner is not None:
                self.uses.append(_Use(name, list(local.parts), owner, direct=True))

    @staticmethod
    def referenced(node: Node) -> bool:
        """A PHP reference to the name (``$r = &$sql``, ``[&$sql]``, ``f(&$sql)``,
        ``foreach (… as &$sql)``): anything may change it through the alias."""
        parent = node.parent
        if parent is None:
            return False
        if parent.type in ("by_ref", "reference_assignment_expression"):
            return True
        return parent.type == "argument" and any(
            c.type == "reference_modifier" for c in parent.named_children)

    # ── bindings the pass did not recognise ──────────────────────────────────
    def binds(self, node: Node) -> bool:
        """Whether a name sits where it is bound (a target, a parameter, a loop or ``as``
        variable…) rather than read."""
        child, parent = node, node.parent
        while parent is not None:
            fld = self.fields[child.id] if child.id in self.fields else _field_of(child)
            if parent.type == "pair_pattern" and fld == "value":
                return True  # ``({ q: sql } = o)``
            if fld in _READ_FIELDS:
                return False
            if "parameter" in parent.type or parent.type in _BINDERS:
                return True
            if fld in _BINDING_FIELDS:
                return "assign" in parent.type or "for" in parent.type
            if parent.type not in _PATTERN_WRAPPERS:
                return False
            child, parent = parent, parent.parent
        return False

    # ── result ───────────────────────────────────────────────────────────────
    def skipped(self, name: str) -> bool:
        """Out, or already recorded by its first value — itself or any local it was built
        from (transitively)."""
        seen: set[str] = set()
        todo = [name]
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            local = self.locals.get(n)
            if n in self.out or (local is not None and local.recorded):
                return True
            if local is not None:
                todo.extend(local.deps)
        return False

    def result(self) -> BuiltSql | None:
        built = BuiltSql()
        types: set[str] = set()
        for use in self.uses:
            local = self.locals.get(use.name)
            if (local is None or not local.built or self.skipped(use.name)
                    or not built_string_has_query(use.parts)):
                continue
            span = (use.owner.start_byte, use.owner.end_byte)
            if use.direct:
                built.direct[span] = " ".join(use.parts)
                types.add(use.owner.type)
            else:
                built.calls[span] = " ".join(use.parts)
        if self.unknowable or (not built.calls and not built.direct):
            return None
        built.direct_types = frozenset(types)
        return built
