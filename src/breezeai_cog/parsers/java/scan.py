"""Java classes as one file declares them, and how a name written in that file resolves to a
fully-qualified class, as Java resolves it — shared by the Java helper rule and the Vert.x
forwarders (``java_vertx/forwarders.py``).

* :class:`Names` — a written class name → its fully-qualified candidates (single-type import,
  the file's package, on-demand imports); :func:`pick` chooses among them.
* :func:`scan_classes` — one walk of a file: each class with its methods and constructors, each
  method's calls and the names assigned in it.
* :func:`callee` — the owner a call on the class itself, a class name, ``this(…)`` or
  ``super(…)`` means.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from tree_sitter import Node

from .syntax import arguments, imports_and_package, name_of, plain


class Names:
    """How a class name written in one file resolves to a fully-qualified one, as Java does: a
    single-type import; else a type of the file's own package (it shadows every on-demand
    import, forwarder or not — checked against the repo's types, ``JavaIndex.fqcn``); else the
    one ``known`` class among the on-demand (``.*``) imports. Anything else → None."""

    def __init__(self, root: Node, source: bytes, types: object | None = None) -> None:
        self.singles, self.wildcards, self.package = imports_and_package(root, source)
        self.types: dict[str, Any] = getattr(types, "fqcn", None) or {}

    def qualify(self, *chain: str) -> str:
        return ".".join((self.package, *chain)) if self.package else ".".join(chain)

    def candidates(self, written: str) -> tuple[tuple[str, ...], bool]:
        """(fully-qualified candidates, exact): exact for a single-type import or a name written
        fully qualified; else this package's name first, then each on-demand import's."""
        written = written.split("<", 1)[0].strip()
        head, _, rest = written.partition(".")
        tail = f".{rest}" if rest else ""
        single = next((i for i in self.singles if i.rsplit(".", 1)[-1] == head), None)
        if single is not None:
            return (single + tail,), True
        if rest and head[:1].islower():  # com.acme.Util as written
            return (written,), True
        return (self.qualify(written), *(f"{w}.{written}" for w in self.wildcards)), False

    def resolve(self, written: str, known: Collection[str]) -> str | None:
        found, exact = self.candidates(written)
        return pick(found, exact, known, self.types)


def pick(found: tuple[str, ...], exact: bool, known: Collection[str],
         types: dict[str, Any]) -> str | None:
    """The class a written name means, from :meth:`Names.candidates` (see :class:`Names`)."""
    if exact:
        return found[0] if found[0] in known else None
    own, wildcards = found[0], found[1:]
    if own in known:
        return own
    if _declared(own, types):  # a same-package type shadows every on-demand import
        return None
    hits = [c for c in wildcards if c in known]
    return hits[0] if len(hits) == 1 else None


def _declared(fqn: str, types: dict[str, Any]) -> bool:
    """A type of the repo declares ``fqn`` or its outer class: the repo's type index lists
    top-level types only (``pkg.Outer`` for ``pkg.Outer.Inner``)."""
    parts = fqn.split(".")
    return any(".".join(parts[:i]) in types for i in range(len(parts), 1, -1))


def class_of(key: str) -> str:
    """``pkg.Class.method/arity`` → ``pkg.Class``."""
    return key.rsplit("/", 1)[0].rsplit(".", 1)[0]


def callee(call: Node, here: str, sup: str | None, names: Names, source: bytes
            ) -> tuple[tuple[str, ...], bool, str] | None:
    """(owner candidates, exact, ``method/arity``) of a call on the class itself (unqualified
    or ``this.``) or on a class name (static), or of a ``this(…)`` / ``super(…)`` (``<init>``
    of the class itself / of its superclass ``sup``, as written); None otherwise."""
    obj = call.child_by_field_name("object")
    if call.type == "explicit_constructor_invocation":
        member = f"<init>/{len(arguments(call))}"
        kind = name_of(call.child_by_field_name("constructor"), source)
        if obj is not None:  # outer.super(…): an inner class's, not followed
            return None
        if kind == "this":
            return (here,), True, member
        if kind == "super" and sup:
            found, exact = names.candidates(sup)
            return found, exact, member
        return None
    method = name_of(call.child_by_field_name("name"), source)
    member = f"{method}/{len(arguments(call))}"
    if not method:
        return None
    if obj is None or name_of(obj, source) == "this":
        return (here,), True, member
    if obj.type in ("identifier", "field_access") and name_of(obj, source)[:1].isalpha():
        text = name_of(obj, source)
        if text.rsplit(".", 1)[-1][:1].isupper():
            found, exact = names.candidates(text)
            return found, exact, member
    return None


class Decl:
    """A method or constructor of a scanned class: its calls and the names assigned in it."""

    __slots__ = ("node", "calls", "assigned")

    def __init__(self, node: Node) -> None:
        self.node = node
        self.calls: list[Node] = []
        self.assigned: dict[str, list[Node]] = {}


class ScannedClass:
    """A scanned class: its fully-qualified name, its members and every name assigned anywhere
    in its body (nested classes included)."""

    __slots__ = ("name", "body", "superclass", "decls", "assigned")

    def __init__(self, name: str, body: Node, superclass: str | None) -> None:
        self.name = name
        self.body = body
        self.superclass = superclass
        self.decls: list[Decl] = []
        self.assigned: dict[str, list[Node]] = {}


#: Type declarations :func:`scan_classes` hands back in ``types`` (every one, local ones included).
TYPE_DECLARATIONS = frozenset({"class_declaration", "interface_declaration", "enum_declaration",
                               "record_declaration"})


def scan_classes(root: Node, source: bytes, names_of: Names,
          types: list[Node] | None = None) -> list[ScannedClass]:
    """One walk of the file: each class reached outside a method (a top-level one, or a member
    of a scanned class, named with its outer classes) with its methods and constructors, each
    method's calls (those in its anonymous / local classes too) and the assignments of each
    method and class (``this.`` dropped). ``types``, when given, also gets every type
    declaration of the file (:data:`TYPE_DECLARATIONS`) and every anonymous class creation
    (``new X() { … }``), in the same walk."""
    classes: list[ScannedClass] = []

    def walk(n: Node, stack: tuple[ScannedClass, ...], chain: tuple[str, ...],
             decl: Decl | None) -> None:
        kind = n.type
        if types is not None and (kind in TYPE_DECLARATIONS or (
                kind == "object_creation_expression"
                and any(c.type == "class_body" for c in n.named_children))):
            types.append(n)
        if kind == "class_declaration" and decl is None \
                and (not stack or n.parent == stack[-1].body):
            simple = name_of(n.child_by_field_name("name"), source)
            body = n.child_by_field_name("body")
            if simple and body is not None:
                inner = (*chain, simple) if stack else (simple,)
                cls = ScannedClass(names_of.qualify(*inner), body, superclass_of(n, source))
                classes.append(cls)
                for c in n.named_children:
                    walk(c, (*stack, cls), inner, None)
                return
        elif kind in ("method_declaration", "constructor_declaration") and decl is None:
            decl = Decl(n)
            if stack and n.parent == stack[-1].body:
                stack[-1].decls.append(decl)
            # else a method of a class not scanned (an interface's, an enum's): read for its
            # class's assignments only, and a class inside it is not scanned
        elif kind in ("method_invocation", "explicit_constructor_invocation") \
                and decl is not None:
            decl.calls.append(n)
        elif kind == "assignment_expression" and stack:
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and right is not None:
                name = plain(name_of(left, source))
                for cls in stack:
                    cls.assigned.setdefault(name, []).append(right)
                if decl is not None:
                    decl.assigned.setdefault(name, []).append(right)
        for c in n.named_children:
            walk(c, stack, chain, decl)

    walk(root, (), (), None)
    return classes


def superclass_of(decl: Node, source: bytes) -> str | None:
    """The superclass of a ``class_declaration`` as written (``Base``, ``a.b.Base<T>``)."""
    sup = decl.child_by_field_name("superclass")
    typ = sup.named_children[0] if sup is not None and sup.named_children else None
    return name_of(typ, source) if typ is not None else None
