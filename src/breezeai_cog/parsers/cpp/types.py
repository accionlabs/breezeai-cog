"""C++ receiver-type inference — the variable/member → class-type map that lets an
``obj.method()`` / ``obj->method()`` call resolve to the class that declares the method.

Precision-first (the extend-capture reliability rule: a wrong type is a wrong edge):

* only an **explicitly written** type is inferred — ``Foo`` / ``Foo*`` / ``const Foo&`` /
  ``shared_ptr<Foo>``; a smart pointer unwraps to its element (``shared_ptr<Foo>`` → ``Foo``)
  but a container does **not** (``vector<Foo>`` stays ``vector`` — ``.size()`` is the
  container's method, not ``Foo``'s);
* ``auto`` is inferred only when the initializer literally carries the type
  (``make_shared<Foo>()``, ``make_unique<Foo>()``, ``new Foo()``) — never from a call's
  return type, which we cannot see;
* a variable **reassigned** after its declaration is demoted to unknown (its later type may
  differ from the declared one), and a name declared with two differing types collapses to
  unknown. Both are honest-null: no edge rather than a possibly-wrong one.

Every value is ``str`` (a written type) or ``None`` (unknown → the resolver must not guess).
"""

from __future__ import annotations

from tree_sitter import Node

from ..index_common import record_distinct
from ..treesitter import node_text

#: Smart pointers whose element type is the real receiver (``p->m()`` calls ``T::m``).
#: Containers are deliberately excluded — unwrapping them would mis-attribute their own
#: methods (``vector<T>::size``) to the element type.
_SMART_PTRS = frozenset({"shared_ptr", "unique_ptr", "weak_ptr"})
_MAKE_FNS = frozenset({"make_shared", "make_unique"})
_DECLARATORS = ("init_declarator", "pointer_declarator", "reference_declarator",
                "array_declarator", "identifier", "field_identifier", "qualified_identifier")


def _template_args(node: Node) -> Node | None:
    return next((c for c in node.named_children if c.type == "template_argument_list"), None)


def _first_type_arg(arglist: Node, source: bytes) -> str | None:
    """The first type argument of a ``template_argument_list`` → its base class name."""
    for a in arglist.named_children:
        if a.type == "type_descriptor":
            inner = a.child_by_field_name("type") or (a.named_children[0] if a.named_children else None)
            return type_name(inner, source)
        if a.type in ("type_identifier", "qualified_identifier", "template_type"):
            return type_name(a, source)
    return None


def type_name(tnode: Node | None, source: bytes) -> str | None:
    """Base class name written in a type node, or ``None`` when it is not a class type
    (a primitive, ``auto``, or an unresolved shape). Smart-pointer templates unwrap to their
    element; other templates keep the template's own name."""
    if tnode is None:
        return None
    t = tnode.type
    if t == "type_identifier":
        return node_text(tnode, source)
    if t == "qualified_identifier":  # N::Foo or std::shared_ptr<Foo> — resolve the last part
        last = tnode.named_children[-1] if tnode.named_children else None
        return type_name(last, source) if last is not None else None
    if t == "template_type":
        head = next((c for c in tnode.named_children if c.type == "type_identifier"), None)
        base = node_text(head, source) if head is not None else None
        if base in _SMART_PTRS:
            args = _template_args(tnode)
            return _first_type_arg(args, source) if args is not None else None
        return base  # container / other template → its own name (not an in-repo class)
    return None  # primitive_type, sized_type_specifier, placeholder (auto) — not a class


def _auto_type(value: Node | None, source: bytes) -> str | None:
    """The class type of an ``auto`` initializer, only when it is literally type-bearing:
    ``new T(...)``, ``make_shared<T>()``, ``make_unique<T>()``. Any other initializer
    (a call whose return type we cannot see, a conditional, …) → ``None``."""
    if value is None:
        return None
    if value.type == "new_expression":
        return type_name(value.child_by_field_name("type"), source)
    if value.type == "call_expression":
        fn = value.child_by_field_name("function")
        if fn is not None and fn.type == "qualified_identifier":  # std::make_shared<T>
            fn = fn.named_children[-1] if fn.named_children else None
        if fn is not None and fn.type == "template_function":
            nm = next((c for c in fn.named_children if c.type == "identifier"), None)
            if nm is not None and node_text(nm, source) in _MAKE_FNS:
                args = _template_args(fn)
                return _first_type_arg(args, source) if args is not None else None
    return None


def _declared_name(declarator: Node | None, source: bytes) -> str | None:
    """The variable/field name inside a (possibly pointer/reference/init) declarator."""
    node = declarator
    while node is not None:
        if node.type in ("identifier", "field_identifier"):
            return node_text(node, source)
        nxt = node.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in node.named_children
                        if c.type in _DECLARATORS or c.type in ("identifier", "field_identifier")), None)
        if nxt is None or nxt is node:
            return None
        node = nxt
    return None


def _record_declaration(decl: Node, source: bytes, out: dict[str, str | None]) -> None:
    """Record every ``name → written class type`` in a ``declaration`` (``auto`` via its
    initializer). Non-class / unknown types are skipped (honest-null by omission)."""
    tnode = decl.child_by_field_name("type")
    is_auto = tnode is not None and tnode.type == "placeholder_type_specifier"
    for d in decl.named_children:
        if d is tnode or d.type not in _DECLARATORS:
            continue
        name = _declared_name(d, source)
        if name is None:
            continue
        if is_auto:
            value = d.child_by_field_name("value") if d.type == "init_declarator" else None
            bt = _auto_type(value, source)
        else:
            bt = type_name(tnode, source)
        if bt:
            record_distinct(out, name, bt)


def build_type_map(body: Node | None, params: Node | None, source: bytes) -> dict[str, str | None]:
    """Variable/param name → written class type for one function body. ``None`` value means
    the name is known but its type is not trustworthy (reassigned, or declared two ways) —
    the resolver must not resolve through it."""
    types: dict[str, str | None] = {}
    if params is not None:
        for p in params.named_children:
            if p.type in ("parameter_declaration", "optional_parameter_declaration"):
                bt = type_name(p.child_by_field_name("type"), source)
                name = _declared_name(p.child_by_field_name("declarator"), source)
                if name is not None and bt:
                    record_distinct(types, name, bt)
    reassigned: set[str] = set()

    def walk(node: Node) -> None:
        for c in node.named_children:
            if c.type == "declaration":
                _record_declaration(c, source, types)
            elif c.type == "assignment_expression":  # guard: reassigned var's type is unreliable
                left = c.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    reassigned.add(node_text(left, source))
            walk(c)

    if body is not None:
        walk(body)
    for name in reassigned:  # demote after the walk so a later `x = …` still overrides a decl
        types[name] = None
    return types


def field_types(class_name: str, body: Node, source: bytes, out: dict[str, str | None]) -> None:
    """Record ``Class::field → written class type`` for a class body's member variables
    (used to type ``this->member_->method()`` receivers). A member function is skipped —
    only data members. Honest-null on any ``Class::field`` collision."""
    from .functions import function_declarator_of  # local import: avoid a module cycle

    for member in body.named_children:
        if member.type != "field_declaration":
            continue
        if function_declarator_of(member.child_by_field_name("declarator")) is not None:
            continue  # a member function, not a variable
        bt = type_name(member.child_by_field_name("type"), source)
        name = _declared_name(member.child_by_field_name("declarator"), source)
        if name is not None and bt:
            record_distinct(out, f"{class_name}::{name}", bt)
