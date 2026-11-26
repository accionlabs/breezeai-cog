"""The class a Python receiver holds, read from how it is bound — for ``x.m()`` / ``self.x.m()``.

A name or attribute has a class only when **every** binding of it says so:

* an annotation: ``repo: OrderRepo``, ``Optional[OrderRepo]``, ``OrderRepo | None``,
  ``"OrderRepo"``, ``self.repo: OrderRepo = …``, a class-level ``repo: OrderRepo``;
* a constructor call: ``repo = OrderRepo(…)``, ``self.repo = mod.OrderRepo(…)``;
* ``self.repo = repo`` with an annotated parameter ``repo`` of that method;
* ``= None`` neither types nor spoils (a lazily set attribute).

Any other binding — ``x = make()``, ``for x in …``, ``with … as x``, a tuple target, ``x += …``,
``global x``, a lambda or comprehension variable of the same name — spoils it: no class, the call
keeps what the resolver gave before. Whether a written name is a class of the repository is
decided later, against the class index (``call_targets.py``).
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ..treesitter import node_text

#: A binding that is no class: the name keeps no type.
SPOILED = ""
#: ``Optional[X]`` → ``X``. ``type[X]`` / ``ClassVar[X]`` are not unwrapped: no instance.
_OPTIONAL = re.compile(r"^(?:typing\.)?Optional\[(.*)\]$", re.DOTALL)
_NAME = re.compile(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*")
#: Scopes of their own inside a function body: their variables are not the function's.
_COMPREHENSIONS = frozenset({"list_comprehension", "set_comprehension",
                             "dictionary_comprehension", "generator_expression"})
_PATTERNS = frozenset({"pattern_list", "tuple_pattern", "list_pattern", "list_splat_pattern"})


def annotation(text: str | None) -> str:
    """The class an annotation names (``Optional[A]``, ``A | None``, ``"A"`` → ``A``;
    ``A[int]`` → ``A``), or :data:`SPOILED` (a union, ``list[A]``, ``type[A]``, …: no instance
    of one class)."""
    t = (text or "").strip().strip("'\"").strip()
    m = _OPTIONAL.match(t)
    if m:
        t = m.group(1).strip().strip("'\"").strip()
    parts = [p.strip() for p in t.split("|")]
    parts = [p for p in parts if p != "None"]
    if len(parts) != 1:
        return SPOILED
    t = parts[0]
    head = t.split("[", 1)[0].strip()  # a generic class of the repository: `Repo[Order]`
    return head if _NAME.fullmatch(head) and head.rsplit(".", 1)[-1][:1].isupper() else SPOILED


def constructed(value: Node | None, source: bytes) -> str | None:
    """``A(…)`` / ``mod.A(…)`` → ``A`` / ``mod.A``; None for ``None``; else :data:`SPOILED`."""
    if value is None:
        return SPOILED
    if value.type == "none":
        return None
    if value.type == "call":
        fn = value.child_by_field_name("function")
        if fn is not None and fn.type in ("identifier", "attribute"):
            name = node_text(fn, source)
            if name.rsplit(".", 1)[-1][:1].isupper():
                return name
    return SPOILED


def _assigned(node: Node, source: bytes) -> str | None:
    """What an ``assignment`` binds its target to: its annotation, else its value."""
    ann = node.child_by_field_name("type")
    if ann is not None:
        return annotation(node_text(ann, source))
    return constructed(node.child_by_field_name("right"), source)


def parameters(params: Node | None, source: bytes) -> dict[str, str]:
    """Parameter name → its class (annotated) or :data:`SPOILED`."""
    out: dict[str, str] = {}
    for p in params.named_children if params is not None else []:
        if p.type == "identifier":
            out[node_text(p, source)] = SPOILED
        elif p.type in ("typed_parameter", "typed_default_parameter"):
            nm = p.child_by_field_name("name") if p.type == "typed_default_parameter" else \
                next((c for c in p.named_children if c.type == "identifier"), None)
            if nm is not None:
                out[node_text(nm, source)] = annotation(
                    node_text(p.child_by_field_name("type"), source))
        elif p.type == "default_parameter":
            nm = p.child_by_field_name("name")
            if nm is not None:
                out[node_text(nm, source)] = SPOILED
        else:  # *args, **kw (typed or not)
            for c in p.named_children:
                if c.type == "identifier":
                    out[node_text(c, source)] = SPOILED
    return out


class _Bindings:
    """name → the classes its bindings name, or None once one binding spoils it."""

    def __init__(self) -> None:
        self.found: dict[str, set[str] | None] = {}

    def add(self, name: str, written: str | None) -> None:
        if written is None:  # `= None`: no say
            self.found.setdefault(name, set())
            return
        have = self.found.get(name, set())
        if have is None:
            return
        if written == SPOILED:
            self.found[name] = None
        else:
            self.found[name] = have | {written}

    def spoil(self, name: str) -> None:
        self.found[name] = None

    def result(self) -> dict[str, tuple[str, ...] | None]:
        return {k: (tuple(sorted(v)) if v else None) for k, v in self.found.items()}


def _identifiers(node: Node, source: bytes) -> list[str]:
    if node.type == "identifier":
        return [node_text(node, source)]
    out: list[str] = []
    for c in node.named_children:
        if c.type in ("attribute", "subscript"):
            continue
        out.extend(_identifiers(c, source))
    return out


#: A line that may give a local name a class: ``x: …`` or ``x = Name(`` / ``x = mod.Name(``.
_TYPED_LINE = re.compile(
    rb"(?m)^[ \t]*([A-Za-z_]\w*)[ \t]*(?::|=[ \t]*(?:[A-Za-z_]\w*\.)*[A-Z]\w*[ \t]*\()")


def maybe_typed(params: Node | None, body: Node | None, source: bytes) -> frozenset[str]:
    """Names that may hold a class — an annotated parameter, or a line binding them with an
    annotation or a call: a cheap superset, so the full walk runs only for these."""
    out = {name for name, written in parameters(params, source).items() if written != SPOILED}
    if body is not None:
        out.update(m.decode() for m in _TYPED_LINE.findall(source[body.start_byte:body.end_byte]))
    return frozenset(out)


def local_types(params: Node | None, body: Node | None, source: bytes
                ) -> dict[str, tuple[str, ...] | None]:
    """A function's local names → the classes every binding names (one or more spellings to
    check against the index), or None when a binding spoils the name."""
    b = _Bindings()
    for name, written in parameters(params, source).items():
        b.add(name, written)

    def spoil_all(n: Node) -> None:
        for name in _identifiers(n, source):
            b.spoil(name)

    def walk(n: Node) -> None:
        for c in n.named_children:
            t = c.type
            if t == "function_definition":
                nm = c.child_by_field_name("name")
                if nm is not None:
                    b.spoil(node_text(nm, source))
                continue  # its own scope
            if t == "class_definition":
                # a nested class: its methods' names may be folded into this function's calls
                nm = c.child_by_field_name("name")
                if nm is not None:
                    b.spoil(node_text(nm, source))
                spoil_all(c)  # every name inside: unsure which are bound
                continue
            if t == "lambda":
                spoil_all(c.child_by_field_name("parameters") or c)
                walk(c)
                continue
            if t in _COMPREHENSIONS:
                for clause in c.named_children:
                    if clause.type == "for_in_clause":
                        left = clause.child_by_field_name("left")
                        if left is not None:
                            spoil_all(left)
                walk(c)
                continue
            if t == "assignment":
                left = c.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    b.add(node_text(left, source), _assigned(c, source))
                elif left is not None:
                    spoil_all(left)
            elif t in ("augmented_assignment", "for_statement", "for_in_clause"):
                left = c.child_by_field_name("left")
                if left is not None:
                    spoil_all(left)
            elif t == "named_expression":
                nm = c.child_by_field_name("name")
                if nm is not None:
                    b.spoil(node_text(nm, source))
            elif t in ("as_pattern_target", "global_statement", "nonlocal_statement",
                       "delete_statement", "case_pattern", "aliased_import"):
                spoil_all(c)
            elif t in ("import_statement", "import_from_statement"):
                for name in c.named_children:
                    if name.type == "dotted_name":
                        b.spoil(node_text(name, source).split(".", 1)[0])
                    elif name.type == "aliased_import":
                        alias = name.child_by_field_name("alias")
                        if alias is not None:
                            b.spoil(node_text(alias, source))
            elif t == "except_clause":
                for x in c.named_children:
                    if x.type == "as_pattern":
                        alias = x.child_by_field_name("alias")
                        if alias is not None:
                            spoil_all(alias)
            walk(c)

    if body is not None:
        walk(body)
    return b.result()


def _first_parameter(fn: Node, source: bytes) -> str | None:
    params = fn.child_by_field_name("parameters")
    first = params.named_children[0] if params is not None and params.named_children else None
    if first is None:
        return None
    if first.type == "identifier":
        return node_text(first, source)
    if first.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
        nm = first.child_by_field_name("name") or next(
            (c for c in first.named_children if c.type == "identifier"), None)
        return node_text(nm, source) if nm is not None else None
    return None


def class_attributes(cls: Node, source: bytes) -> tuple[tuple[str, str], ...]:
    """A class's attributes → the class their bindings name, :data:`SPOILED` when a binding
    spoils it or two name different classes as written. The class body's ``x: A`` / ``x = A()``
    and every ``<first parameter>.x = …`` in its methods (nested functions included, nested
    classes not)."""
    b = _Bindings()
    body = cls.child_by_field_name("body")
    for c in body.named_children if body is not None else []:
        stmt = c.named_children[0] if c.type == "expression_statement" and c.named_children \
            else c
        if stmt.type == "assignment":
            left = stmt.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                b.add(node_text(left, source), _assigned(stmt, source))
        d = c.child_by_field_name("definition") if c.type == "decorated_definition" else c
        if d is None or d.type != "function_definition":
            continue
        decorators = {node_text(x, source).lstrip("@").strip() for x in c.named_children
                      if x.type == "decorator"} if c.type == "decorated_definition" else set()
        if "staticmethod" in decorators:
            continue
        me = _first_parameter(d, source)
        if me is None:
            continue
        own = parameters(d.child_by_field_name("parameters"), source)
        _method_attributes(d.child_by_field_name("body"), me, own, source, b)
    out = []
    for name, classes in b.result().items():
        if classes is None or len(classes) != 1:
            out.append((name, SPOILED))
        else:
            out.append((name, classes[0]))
    return tuple(sorted(out))


def _method_attributes(body: Node | None, me: str, params: dict[str, str], source: bytes,
                       b: _Bindings) -> None:
    """``<me>.x = …`` in one method, statements only (an attribute is never assigned inside an
    expression); nested functions included, nested classes not."""
    prefix = me + "."
    if body is None or prefix.encode() not in source[body.start_byte:body.end_byte]:
        return
    rebound: set[str] = set()  # parameters assigned again in the method: their class is unsure
    pending: list[tuple[str, str, bool]] = []  # `self.x = p`: (x, p, in a nested function)

    def attr(target: Node | None) -> str | None:
        if target is None or target.type != "attribute":
            return None
        text = node_text(target, source)
        name = text[len(prefix):] if text.startswith(prefix) else ""
        return name if name.isidentifier() else None

    def spoil_targets(target: Node | None) -> None:
        if target is None:
            return
        for t in [target, *_attributes_in(target)]:
            if (nm := attr(t)) is not None:
                b.spoil(nm)
        if target.type == "identifier":
            rebound.add(node_text(target, source))
        elif target.type in _PATTERNS:
            rebound.update(_identifiers(target, source))

    def assignment(e: Node, nested: bool) -> None:
        left = e.child_by_field_name("left")
        right = e.child_by_field_name("right")
        if e.type == "assignment" and (name := attr(left)) is not None:
            if e.child_by_field_name("type") is None and right is not None \
                    and right.type == "identifier":
                pending.append((name, node_text(right, source), nested))
            else:
                b.add(name, _assigned(e, source))
        else:
            spoil_targets(left)
        if right is not None and right.type in ("assignment", "augmented_assignment"):
            assignment(right, nested)  # `a = self.x = …`

    def statements(n: Node, nested: bool) -> None:
        for c in n.named_children:
            t = c.type
            if t == "class_definition":
                continue
            if t in ("assignment", "augmented_assignment"):
                assignment(c, nested)
                continue
            if t == "expression_statement":  # older grammars wrap the assignment
                for e in c.named_children:
                    if e.type in ("assignment", "augmented_assignment"):
                        assignment(e, nested)
                continue
            if t == "for_statement":
                spoil_targets(c.child_by_field_name("left"))
            elif t in ("with_statement", "delete_statement"):
                for a in _attributes_in(c):
                    if a.parent is not None and a.parent.type in (
                            "as_pattern_target", "delete_statement", "expression_list"):
                        if (nm := attr(a)) is not None:
                            b.spoil(nm)
            if t == "block" or t == "decorated_definition" \
                    or t.endswith(("_statement", "_clause", "_definition")):
                statements(c, nested or t == "function_definition")

    statements(body, False)
    for name, p, nested in pending:
        b.add(name, params[p] if p in params and p not in rebound and not nested else SPOILED)


def _attributes_in(node: Node) -> list[Node]:
    out = []
    for c in node.named_children:
        if c.type == "attribute":
            out.append(c)
        else:
            out.extend(_attributes_in(c))
    return out
