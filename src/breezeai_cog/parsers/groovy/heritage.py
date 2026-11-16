"""The repo's Groovy class heritage, for call resolution that checks where a method is declared.

Each class records its in-repo base class and the methods a call on it can reach in the
repository, each mapped to the file that declares it:

* the methods it declares;
* the accessors Groovy generates for its properties. A field with no access modifier is a
  property: ``String name`` gives ``getName`` and ``setName``, a ``final`` one only the getter,
  and a primitive ``boolean`` also ``isName``. A ``private``, ``protected``, ``public`` or
  ``@PackageScope`` field gets none, and nor does an interface's field;
* the methods and property accessors of the in-repo traits it implements (and the traits
  those extend), each mapped to the trait's file; for an interface, those of the in-repo
  interfaces it extends. The class's own methods come first; a name two of its traits (or
  parent interfaces) declare is ambiguous, so it maps to ``None``.

A class whose body has a parse error anywhere may have lost members to it, so its methods are
not known:
it is recorded as ``None``, like an ambiguous name, and so is every class whose in-repo parent
chain reaches it; a call on one keeps the type's file.

``callresolve.py`` then sends ``obj.m()`` on an ``obj`` of this type to the file of the class or
nearest in-repo ancestor that declares ``m``, and leaves it empty when none does: a method from
a library base class, one added at runtime (GORM's ``save``) or by an AST transformation.

A base or trait is kept only when exactly one of its candidate FQCNs is a repository class
(its import, the same package, a wildcard import) or it is declared in the same file. Only
``.groovy`` files are indexed, so a Java base class ends the chain. Two repository classes
sharing a simple name make that name ambiguous: nothing resolves through it.
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node

from ..index_common import ClassHeritage, record_distinct
from ..treesitter import node_text
from .functions import name_text

_CLASS_DECLS = ("class_declaration", "interface_declaration", "enum_declaration",
                "trait_declaration")
_TYPE_NODES = ("type_identifier", "generic_type", "qualified_type", "scoped_type_identifier")
_VISIBILITY = frozenset({"public", "private", "protected"})
_NO_RETURN = frozenset({"void", "boolean", "byte", "char", "short", "int", "long", "float",
                        "double", "def", "var", "Object"})


@dataclass(frozen=True, slots=True)
class HeritageFrag:
    """One class as one file sees it. ``base`` / ``traits`` are each a written name with the
    FQCNs it may be (``candidates``) and whether it is declared in the same file (``local``)."""

    fqcn: str
    kind: str
    base: tuple[str, tuple[str, ...], bool] | None
    traits: tuple[tuple[str, tuple[str, ...], bool], ...]
    methods: tuple[str, ...]
    returns: tuple[tuple[str, str | None], ...] = ()
    #: False when a parse error in the body may have swallowed members
    complete: bool = True


def _type_text(node: Node, source: bytes) -> str:
    """``a.b.Foo<T>`` → ``a.b.Foo``."""
    return "".join(node_text(node, source).split()).split("<", 1)[0]


def _simple(type_name: str | None) -> str | None:
    """The simple name of a declared type, or None where no repository method can be called
    on it (none written, ``def``, a primitive, an array)."""
    if not type_name or type_name.endswith("]"):
        return None
    simple = type_name.split("<", 1)[0].rsplit(".", 1)[-1]
    return None if not simple or simple in _NO_RETURN else simple


def _capitalize(name: str) -> str:
    """Groovy's accessor suffix: ``jobName`` → ``JobName``; a name whose second letter is a
    capital (``aURL``) or that starts with a non-letter (``_x``) is kept as it is."""
    if len(name) > 1 and name[1].isupper():
        return name
    return name[:1].upper() + name[1:]


def property_accessors(field: Node, source: bytes) -> list[tuple[str, str | None]]:
    """``(accessor, return type)`` for each property a ``field_declaration`` declares; empty for a
    field with an access modifier or ``@PackageScope``."""
    tokens = {c.type for c in field.children if not c.is_named}
    if tokens & _VISIBILITY:
        return []
    for ann in (c for c in field.named_children if c.type == "annotation"):
        nm = ann.child_by_field_name("name")
        if nm is not None and node_text(nm, source).rsplit(".", 1)[-1] == "PackageScope":
            return []
    typ = field.child_by_field_name("type")
    type_name = _type_text(typ, source) if typ is not None else None
    out: list[tuple[str, str | None]] = []
    for decl in field.named_children:
        if decl.type != "variable_declarator":
            continue
        nm = decl.child_by_field_name("name")
        if nm is None:
            continue
        suffix = _capitalize(node_text(nm, source))
        out.append((f"get{suffix}", _simple(type_name)))
        if type_name == "boolean":
            out.append((f"is{suffix}", None))
        if "final" not in tokens:
            out.append((f"set{suffix}", None))
    return out


def _named(node: Node, kind: str) -> list[Node]:
    """The type nodes listed under a ``superclass`` / ``super_interfaces`` /
    ``extends_interfaces`` child of a declaration."""
    holder = next((c for c in node.named_children if c.type == kind), None)
    if holder is None:
        return []
    return [c for c in holder.named_children if c.type in _TYPE_NODES]


def _bad(node: Node) -> bool:
    return any(c.type == "ERROR" or c.is_missing for c in node.children)


def collect_heritage(root: Node, source: bytes, package: str) -> list[HeritageFrag]:
    """Every class, interface, enum and trait declared in one file (nested ones too)."""
    imports: dict[str, str] = {}  # simple name or alias → FQCN, from single-type imports
    wildcards: list[str] = []
    for node in root.named_children:
        if node.type != "import_declaration" or any(c.type == "static" for c in node.children):
            continue
        name = node.child_by_field_name("name")
        if name is None:
            continue
        fqcn = node_text(name, source)
        if any(c.type == ".*" for c in node.children):
            wildcards.append(fqcn)
            continue
        alias = node.child_by_field_name("alias")
        imports[node_text(alias, source) if alias is not None else fqcn.rsplit(".", 1)[-1]] = fqcn

    found: list[tuple[str, Node]] = []

    def walk(nodes: list[Node], outer: str) -> None:
        for child in nodes:
            if child.type not in _CLASS_DECLS or _bad(child):
                continue
            nm = child.child_by_field_name("name")
            if nm is None:
                continue
            qualified = f"{outer}.{node_text(nm, source)}" if outer else node_text(nm, source)
            found.append((qualified, child))
            body = child.child_by_field_name("body")
            if body is not None:
                walk(body.named_children, qualified)

    walk(root.named_children, "")
    local_names = {q.rsplit(".", 1)[-1] for q, _ in found}

    def ref(type_node: Node) -> tuple[str, tuple[str, ...], bool]:
        written = _type_text(type_node, source)
        if "." in written:  # written qualified: `extends com.acme.Base`
            return written, (written,), False
        if written in imports:
            return written, (imports[written],), False
        if written in local_names:
            return written, (), True
        return written, (f"{package}.{written}" if package else written,
                         *(f"{w}.{written}" for w in wildcards)), False

    out: list[HeritageFrag] = []
    for qualified, node in found:
        base_nodes = _named(node, "superclass") if node.type == "class_declaration" else []
        base = ref(base_nodes[0]) if base_nodes else None
        trait_nodes = _named(node, "super_interfaces")
        if node.type == "trait_declaration":
            trait_nodes = [*_named(node, "extends_interfaces"), *trait_nodes]
        elif node.type == "interface_declaration":
            trait_nodes = _named(node, "extends_interfaces")
        body = node.child_by_field_name("body")
        # A parse error anywhere in the body can throw the rest of it off (members read as
        # `catch` / `for` constructors, methods swallowed), even with no ERROR among its members.
        complete = body is not None and not body.has_error
        names: list[str] = []
        returns: dict[str, str | None] = {}

        def add(name: str, ret: str | None) -> None:
            names.append(name)
            # overloads that return different types → ambiguous
            returns[name] = ret if name not in returns or returns[name] == ret else None

        own_name = qualified.rsplit(".", 1)[-1]
        for member in (body.named_children if body is not None else ()):
            if _bad(member):
                complete = False
                continue
            if member.type == "constructor_declaration":
                # A constructor not named after its class, or one in an interface or trait, is a
                # method the grammar split silently (`default String b()` → a field typed
                # `default` and a constructor `b`): the member list cannot be trusted.
                nm = member.child_by_field_name("name")
                if (node.type in ("interface_declaration", "trait_declaration")
                        or nm is None or node_text(nm, source) != own_name):
                    complete = False
            elif member.type == "method_declaration":
                nm = member.child_by_field_name("name")
                if nm is None:
                    continue
                rt = member.child_by_field_name("return_type")
                add(name_text(nm, source), _simple(_type_text(rt, source)) if rt else None)
            elif member.type == "field_declaration" and node.type != "interface_declaration":
                for accessor, ret in property_accessors(member, source):
                    add(accessor, ret)
        kind = node.type.removesuffix("_declaration")
        fqcn = f"{package}.{qualified}" if package else qualified
        out.append(HeritageFrag(fqcn, kind, base, tuple(ref(t) for t in trait_nodes),
                                tuple(dict.fromkeys(names)), tuple(returns.items()), complete))
    return out


def _in_repo(ref: tuple[str, tuple[str, ...], bool],
             fqcn_index: dict[str, str | None]) -> str | None:
    """The simple name of a base or trait when it is exactly one repository class."""
    written, candidates, local = ref
    if local or len([c for c in candidates if fqcn_index.get(c) is not None]) == 1:
        return written.rsplit(".", 1)[-1]
    return None


def build_heritage(
    frags: list[tuple[str, HeritageFrag]], fqcn_index: dict[str, str | None],
) -> dict[str, ClassHeritage | None]:
    """``class_heritage`` keyed by simple class name, from every file's ``(rel, frag)``."""
    by_name: dict[str, tuple[str, HeritageFrag] | None] = {}
    for rel, frag in frags:
        record_distinct(by_name, frag.fqcn.rsplit(".", 1)[-1], (rel, frag), same=lambda a, b: False)

    Reached = dict[str, tuple[str | None, str | None]]  # method → (file, return type)

    class _Unknown(Exception):
        """A trait or parent interface whose methods are not known (a parse error in its body)."""

    def trait_methods(frag: HeritageFrag, seen: set[str]) -> Reached:
        """Methods reached through ``frag``'s in-repo traits (and theirs) → the trait's file and
        the method's return type; ``(None, None)`` where two traits declare the name. Raises
        :class:`_Unknown` when one of them has a body whose methods are not known."""
        out: Reached = {}
        for ref in frag.traits:
            name = _in_repo(ref, fqcn_index)
            entry = by_name.get(name) if name is not None else None
            if name is None or name in seen or entry is None:
                continue
            rel, trait = entry
            # a class or trait takes methods from traits; an interface from the interfaces it extends
            if trait.kind != ("interface" if frag.kind == "interface" else "trait"):
                continue
            if not trait.complete:
                raise _Unknown(name)
            rets = dict(trait.returns)
            reached: Reached = {m: (rel, rets.get(m)) for m in trait.methods}
            for m, hit in trait_methods(trait, seen | {name}).items():
                reached.setdefault(m, hit)  # a trait's own method wins over the traits it extends
            for m, hit in reached.items():
                if m in out and out[m] != hit:
                    out[m] = (None, None)
                else:
                    out.setdefault(m, hit)
        return out

    out: dict[str, ClassHeritage | None] = {}
    unknown: set[str] = set()  # classes whose methods are not known
    for rel, frag in frags:
        simple = frag.fqcn.rsplit(".", 1)[-1]
        try:
            inherited = trait_methods(frag, {simple}) if frag.complete else None
        except _Unknown:
            inherited = None
        if inherited is None:  # its methods are not known: a call on it keeps the type's file
            out[simple] = None
            unknown.add(simple)
            continue
        methods: dict[str, str | None] = dict.fromkeys(frag.methods, rel)
        returns = dict(frag.returns)
        for m, (where, ret) in inherited.items():
            if m not in methods:  # the class's own method wins over a trait's
                methods[m], returns[m] = where, ret
        extends = _in_repo(frag.base, fqcn_index) if frag.base is not None else None
        heritage = ClassHeritage(extends=extends, decorators=[], methods=methods,
                                 returns=returns)
        record_distinct(out, simple, heritage, same=lambda a, b: False)

    # A class below one whose methods are not known could inherit any method from it, so its
    # own are not known either.
    known = {name: h for name, h in out.items() if h is not None}
    for name, heritage in known.items():
        base, seen = heritage.extends, {name}
        while base is not None and base not in seen:
            if base in unknown:
                out[name] = None
                break
            seen.add(base)
            parent = known.get(base)
            base = parent.extends if parent is not None else None
    return out
