"""The repo's Java class heritage, for inherited-method call resolution.

Each class records its base class and the methods it declares (→ its file), so a call written in
a subclass, ``foo()`` or ``this.foo()``, resolves to the nearest in-repo ancestor that declares
``foo`` (``callresolve.py``'s inheritance tier).

Java names a base class through its imports, so a base is kept only when it is the repository's
own class: ``extends Base`` with ``import com.lib.Base`` (a jar) or a ``java.lang`` class ends the
chain there, even when the repository has an unrelated ``Base`` of its own. Two repository
classes sharing a simple name make that name ambiguous: nothing resolves through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from tree_sitter import Node

from ..index_common import ClassHeritage, record_distinct
from ..treesitter import node_text
from .functions import class_members

_CLASS_DECLS = ("class_declaration", "interface_declaration", "enum_declaration",
                "record_declaration", "annotation_type_declaration")
_TYPE_NODES = ("type_identifier", "scoped_type_identifier", "generic_type")


@dataclass(frozen=True, slots=True)
class HeritageFrag:
    """One class as one file sees it: the FQCNs its base may be (the import, the same package,
    a wildcard package) or ``local`` when the base is declared in the same file."""

    fqcn: str
    base: str | None
    candidates: tuple[str, ...]
    local: bool
    methods: tuple[str, ...]
    returns: tuple[tuple[str, str | None], ...] = ()
    #: Each field a subclass inherits (not ``private``): its name, its declared type as written
    #: (generics dropped) and that type's candidate FQCNs, or ``None`` when the type is declared
    #: in this file. Empty candidates for a primitive or an array.
    fields: tuple[tuple[str, str, tuple[str, ...] | None], ...] = ()


def _base_name(node: Node, source: bytes) -> str | None:
    superclass = node.child_by_field_name("superclass")
    if superclass is None:
        return None
    ti = next((c for c in superclass.named_children if c.type in _TYPE_NODES), None)
    if ti is None:
        return None
    return "".join(node_text(ti, source).split()).split("<", 1)[0]


_NO_RETURN = frozenset({"void", "boolean", "byte", "char", "short", "int", "long", "float",
                        "double"})


def _return_type(method: Node, source: bytes) -> str | None:
    """The simple name of a method's declared return type (``Foo``, ``a.b.Foo``, ``Foo<T>`` →
    ``Foo``); None for void, a primitive or an array (no method of its own to call)."""
    typ = method.child_by_field_name("type")
    if typ is None or typ.type == "array_type":
        return None
    text = "".join(node_text(typ, source).split()).split("<", 1)[0]
    simple = text.rsplit(".", 1)[-1]
    return None if not simple or simple in _NO_RETURN else simple


def _field_types(
    body: Node | None, source: bytes, candidates_of: Callable[[str], tuple[str, ...] | None],
) -> tuple[tuple[str, str, tuple[str, ...] | None], ...]:
    """The fields of a class body that a subclass inherits (any but ``private``): name, declared
    type as written (generics dropped) and that type's candidate FQCNs (``candidates_of``). A
    primitive or an array has no candidates: no call on it can reach a repository class."""
    out: list[tuple[str, str, tuple[str, ...] | None]] = []
    for member in class_members(body) if body is not None else ():
        if member.type != "field_declaration":
            continue
        mods = next((c for c in member.named_children if c.type == "modifiers"), None)
        if mods is not None and any(c.type == "private" for c in mods.children):
            continue
        typ = member.child_by_field_name("type")
        written = ""
        if typ is not None and typ.type in _TYPE_NODES:
            written = "".join(node_text(typ, source).split()).split("<", 1)[0]
        cands = candidates_of(written) if written and written not in _NO_RETURN else ()
        for decl in member.children_by_field_name("declarator"):
            nm = decl.child_by_field_name("name")
            if nm is not None:
                out.append((node_text(nm, source), written, cands))
    return tuple(out)


def collect_heritage(root: Node, source: bytes, package: str) -> list[HeritageFrag]:
    """Every class declared in one file, with its base's candidate FQCNs and its own methods."""
    imports: dict[str, str] = {}  # simple name → FQCN, from single-type imports
    wildcards: list[str] = []
    for node in root.named_children:
        if node.type != "import_declaration" or any(c.type == "static" for c in node.children):
            continue
        scoped = next((c for c in node.named_children
                       if c.type in ("scoped_identifier", "identifier")), None)
        if scoped is None:
            continue
        name = node_text(scoped, source)
        if any(c.type == "asterisk" for c in node.children):
            wildcards.append(name)
        else:
            imports[name.rsplit(".", 1)[-1]] = name

    found: list[tuple[str, Node]] = []

    def walk(nodes: list[Node], outer: str) -> None:
        for child in nodes:
            if child.type in _CLASS_DECLS:
                nm = child.child_by_field_name("name")
                if nm is None:
                    continue
                qualified = f"{outer}.{node_text(nm, source)}" if outer else node_text(nm, source)
                found.append((qualified, child))
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(class_members(body), qualified)

    walk(root.named_children, "")
    local_names = {q.rsplit(".", 1)[-1] for q, _ in found}

    def candidates_of(name: str) -> tuple[str, ...] | None:
        """The FQCNs a type written in this file may be; ``None``: declared in this file."""
        if "." in name:  # written qualified: `com.acme.Base`
            return (name,)
        if name in imports:
            return (imports[name],)
        if name in local_names:
            return None
        return (f"{package}.{name}" if package else name, *(f"{w}.{name}" for w in wildcards))

    out: list[HeritageFrag] = []
    for qualified, node in found:
        base = _base_name(node, source) if node.type == "class_declaration" else None
        candidates: tuple[str, ...] = ()
        local = False
        if base is not None:
            cands = candidates_of(base)
            local = cands is None
            candidates = cands or ()
        body = node.child_by_field_name("body")
        decls = [m for m in (class_members(body) if body else ())
                 if m.type == "method_declaration"]
        returns: dict[str, str | None] = {}
        names: list[str] = []
        for decl in decls:
            nm = decl.child_by_field_name("name")
            if nm is None:
                continue
            name = node_text(nm, source)
            names.append(name)
            ret = _return_type(decl, source)
            # overloads that return different types → ambiguous
            returns[name] = ret if name not in returns or returns[name] == ret else None
        fqcn = f"{package}.{qualified}" if package else qualified
        fields = _field_types(body, source, candidates_of) if node.type == "class_declaration" else ()
        out.append(HeritageFrag(fqcn, base, candidates, local, tuple(names),
                                tuple(returns.items()), fields))
    return out


def foreign_bases(
    frags: list[tuple[str, HeritageFrag]], fqcn_index: dict[str, str | None],
) -> dict[str, tuple[str, ...] | None]:
    """Class simple name → the FQCNs its base class may be, for each class whose base is no Java
    class of the repository (a library class, or one of the repository's Groovy classes:
    ``jvm_related.py`` looks the candidates up in the Groovy index). ``None`` when two classes of
    one simple name name different bases."""
    out: dict[str, tuple[str, ...] | None] = {}
    for _, frag in frags:
        if frag.base is None or frag.local or any(c in fqcn_index for c in frag.candidates):
            continue
        record_distinct(out, frag.fqcn.rsplit(".", 1)[-1], frag.candidates)
    return out


def _field_class(
    written: str, candidates: tuple[str, ...] | None, fqcn_index: dict[str, str | None],
) -> str | None:
    """A field type's simple class name when it is exactly one repository class (or a class of
    the declaring file: ``candidates`` None), else None: a library type, or one the declaring
    file's imports cannot settle, gives the field no type. The resolver still requires the
    simple name to be one class in the index, so two classes sharing it resolve nothing."""
    if candidates is None:
        return written.rsplit(".", 1)[-1]
    in_repo = [c for c in candidates if fqcn_index.get(c) is not None]
    return in_repo[0].rsplit(".", 1)[-1] if len(in_repo) == 1 else None


def build_heritage(
    frags: list[tuple[str, HeritageFrag]], fqcn_index: dict[str, str | None],
) -> dict[str, ClassHeritage | None]:
    """``class_heritage`` keyed by simple class name, from every file's ``(rel, frag)``.

    A base is kept only when exactly one of its candidate FQCNs is a repository class (or it is
    declared in the same file); otherwise the class has no in-repo base and the walk stops."""
    out: dict[str, ClassHeritage | None] = {}
    for rel, frag in frags:
        extends: str | None = None
        if frag.base is not None:
            in_repo = [c for c in frag.candidates if fqcn_index.get(c) is not None]
            if frag.local or len(in_repo) == 1:
                extends = frag.base.rsplit(".", 1)[-1]
        heritage = ClassHeritage(extends=extends, decorators=[],
                                 methods={m: rel for m in frag.methods},
                                 returns=dict(frag.returns),
                                 fields={name: _field_class(written, cands, fqcn_index)
                                         for name, written, cands in frag.fields})
        # The same simple name declared twice in the repo → ambiguous (None).
        record_distinct(out, frag.fqcn.rsplit(".", 1)[-1], heritage, same=lambda a, b: False)
    return out
