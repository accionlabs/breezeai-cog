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

from tree_sitter import Node

from ..index_common import ClassHeritage, record_distinct
from ..treesitter import node_text

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


def _base_name(node: Node, source: bytes) -> str | None:
    superclass = node.child_by_field_name("superclass")
    if superclass is None:
        return None
    ti = next((c for c in superclass.named_children if c.type in _TYPE_NODES), None)
    if ti is None:
        return None
    return "".join(node_text(ti, source).split()).split("<", 1)[0]


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

    def walk(node: Node, outer: str) -> None:
        for child in node.named_children:
            if child.type in _CLASS_DECLS:
                nm = child.child_by_field_name("name")
                if nm is None:
                    continue
                qualified = f"{outer}.{node_text(nm, source)}" if outer else node_text(nm, source)
                found.append((qualified, child))
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, qualified)

    walk(root, "")
    local_names = {q.rsplit(".", 1)[-1] for q, _ in found}
    out: list[HeritageFrag] = []
    for qualified, node in found:
        base = _base_name(node, source) if node.type == "class_declaration" else None
        candidates: tuple[str, ...] = ()
        local = False
        if base is not None:
            if "." in base:  # written qualified: `extends com.acme.Base`
                candidates = (base,)
            elif base in imports:
                candidates = (imports[base],)
            elif base in local_names:
                local = True
            else:
                candidates = (f"{package}.{base}" if package else base,
                              *(f"{w}.{base}" for w in wildcards))
        body = node.child_by_field_name("body")
        names = (m.child_by_field_name("name") for m in (body.named_children if body else ())
                 if m.type == "method_declaration")
        methods = tuple(node_text(n, source) for n in names if n is not None)
        fqcn = f"{package}.{qualified}" if package else qualified
        out.append(HeritageFrag(fqcn, base, candidates, local, methods))
    return out


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
                                 methods={m: rel for m in frag.methods})
        # The same simple name declared twice in the repo → ambiguous (None).
        record_distinct(out, frag.fqcn.rsplit(".", 1)[-1], heritage, same=lambda a, b: False)
    return out
