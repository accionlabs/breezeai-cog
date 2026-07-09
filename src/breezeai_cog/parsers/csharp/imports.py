"""C# import extraction + namespace→file resolution.

C# ``using`` directives name **namespaces** (``System.Text``, ``Acme.Repo``), not
files — a namespace spans many files, so there is no 1:1 namespace→path mapping the
way Java's fully-qualified imports give. We instead resolve **referenced type names**:
a repo-level pre-pass (``build_csharp_index``) maps ``Namespace.TypeName → file(s)``,
and for each type a file references (base types, field/param/local types, ``new X()``)
we look it up under every in-scope namespace (the file's own namespace + its ancestors,
every ``using``, and any ``global using``). Resolution is **precision-first**: a type
binds to a file only when exactly one in-scope namespace declares it (0 or >1 → skip),
so a BCL/NuGet type that merely shares a name with an in-repo type is never mis-bound.

``using static Ns.Type`` and ``using Alias = Ns.Type`` name a *type* (fully qualified),
so they resolve by direct lookup. Plain ``using``/``global using`` namespaces are still
recorded as external (the namespace itself is not a single file)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..treesitter import node_text, parse_source

_NAME_NODES = ("qualified_name", "identifier", "alias_qualified_name", "member_access_expression")
_CLASS_TYPES = (
    "class_declaration", "interface_declaration", "enum_declaration",
    "struct_declaration", "record_declaration",
)
_NAMESPACE_TYPES = ("namespace_declaration", "file_scoped_namespace_declaration")


@dataclass
class CSharpIndex:
    """Repo-wide resolution index (result of ``build_csharp_index``)."""

    #: "Namespace.TypeName" → set of repo-relative files declaring it (a set so a name
    #: declared in >1 file — partial classes, collisions — is detected as ambiguous).
    types: dict[str, set[str]] = field(default_factory=dict)
    #: namespaces brought into scope for *every* file via ``global using`` / ImplicitUsings.
    global_usings: set[str] = field(default_factory=set)


def _decl_name(node: Node, source: bytes) -> str | None:
    """Name of a namespace/type declaration (``name`` field, or last name-type child)."""
    nm = node.child_by_field_name("name")
    if nm is None:
        nm = next((c for c in reversed(node.named_children) if c.type in _NAME_NODES), None)
    return node_text(nm, source) if nm is not None else None


def _join(ns: str, name: str | None) -> str:
    if not name:
        return ns
    return f"{ns}.{name}" if ns else name


def _simple(text: str | None) -> str | None:
    """Simple type name: strip generics/array/nullable/qualifier — ``a.b.Foo<T>[]?`` → ``Foo``."""
    if not text:
        return None
    text = text.split("<", 1)[0].strip().rstrip("?").rstrip("[]").strip().rstrip("?")
    return text.rsplit(".", 1)[-1] or None


def _classify_using(node: Node, source: bytes) -> tuple[str, str | None, str | None]:
    """``(kind, name, alias)`` for a ``using_directive``. ``kind`` ∈ {using, global,
    static, alias}; ``name`` is the namespace (using/global) or fully-qualified type
    (static/alias target); ``alias`` is the local name for ``using X = …``."""
    tokens = {c.type for c in node.children if not c.is_named}
    names = [c for c in node.named_children if c.type in _NAME_NODES]
    if "=" in tokens:  # using Alias = Ns.Type
        alias = node_text(names[0], source) if names else None
        target = node_text(names[-1], source) if names else None
        return "alias", target, alias
    name = node_text(names[-1], source) if names else None
    if "static" in tokens:  # using static Ns.Type
        return "static", name, None
    if "global" in tokens:  # global using Ns
        return "global", name, None
    return "using", name, None


def _index_file(root: Node, source: bytes, rel: str, index: CSharpIndex) -> None:
    """Add one file's ``global using`` namespaces + declared types to ``index``."""
    for child in root.named_children:
        if child.type == "using_directive":
            kind, name, _ = _classify_using(child, source)
            if kind == "global" and name:
                index.global_usings.add(name)

    def walk(node: Node, ns: str) -> None:
        local_ns = ns
        for child in node.named_children:
            t = child.type
            if t == "file_scoped_namespace_declaration":
                local_ns = _join(ns, _decl_name(child, source))  # applies to later siblings
            elif t == "namespace_declaration":
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, _join(local_ns, _decl_name(child, source)))
            elif t in _CLASS_TYPES:
                nm = _decl_name(child, source)
                if nm:
                    index.types.setdefault(_join(local_ns, nm), set()).add(rel)
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, local_ns)  # nested types share the enclosing namespace

    walk(root, "")


def build_csharp_index(repo_root: Path, files) -> CSharpIndex:
    """Repo-level pre-pass: parse each ``.cs`` file and map declared types → path."""
    index = CSharpIndex()
    for file in files:
        try:
            source = Path(file).read_bytes()
        except OSError:
            continue
        root = parse_source("csharp", source, 0).root_node
        _index_file(root, source, repo_relative(file, repo_root), index)
    return index


def _file_scopes(root: Node, source: bytes) -> set[str]:
    """Namespaces in scope from the file's own declarations, including every ancestor
    (a type in ``A.B`` can reference siblings in ``A``) plus the global namespace ("")."""
    scopes: set[str] = set()

    def walk(node: Node, ns: str) -> None:
        local_ns = ns
        for child in node.named_children:
            t = child.type
            if t == "file_scoped_namespace_declaration":
                local_ns = _join(ns, _decl_name(child, source))
                scopes.add(local_ns)
            elif t == "namespace_declaration":
                full = _join(local_ns, _decl_name(child, source))
                scopes.add(full)
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, full)
            else:
                walk(child, local_ns)

    walk(root, "")
    expanded = {""}  # always try the global namespace
    for s in scopes:
        parts = s.split(".")
        for i in range(1, len(parts) + 1):
            expanded.add(".".join(parts[:i]))
    return expanded


def _referenced_types(root: Node, source: bytes) -> set[str]:
    """Simple type names referenced in the file — the candidates we try to resolve to a
    declaring file (base/implements, field/param/local types, ``new X()``, properties)."""
    names: set[str] = set()

    def add(node: Node | None) -> None:
        if node is not None:
            s = _simple(node_text(node, source))
            if s:
                names.add(s)

    def walk(node: Node) -> None:
        for child in node.named_children:
            t = child.type
            if t == "base_list":
                for b in child.named_children:
                    if b.type in ("identifier", "qualified_name", "generic_name"):
                        add(b)
            elif t in ("variable_declaration", "parameter", "property_declaration"):
                add(child.child_by_field_name("type"))
            elif t == "object_creation_expression":
                add(child.child_by_field_name("type")
                    or next((c for c in child.named_children if c.type in
                             ("identifier", "qualified_name", "generic_name")), None))
            walk(child)

    walk(root)
    return names


def _resolve(name: str, scopes: set[str], index: CSharpIndex | None, self_path: str) -> str | None:
    """A fully-qualified or simple type name → its declaring file, or ``None`` when
    external / unresolved / **ambiguous** (declared in >1 in-repo file)."""
    if index is None or not name:
        return None
    if "." in name and name in index.types:  # already fully qualified (static/alias target)
        hits = set(index.types[name])
    else:
        hits = set()
        for ns in scopes:
            hits |= index.types.get(_join(ns, name), set())
    hits.discard(self_path)  # cross-file edges only
    return next(iter(hits)) if len(hits) == 1 else None


def extract_imports(
    root: Node, source: bytes, file_path: str, index: CSharpIndex | None = None
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    external: dict[str, None] = {}
    internal: dict[str, None] = {}
    bindings: dict[str, str] = {}  # simple type name → in-repo file (drives calls[].path)

    scopes = _file_scopes(root, source)
    if index is not None:
        scopes = scopes | index.global_usings

    for child in root.named_children:
        if child.type != "using_directive":
            continue
        kind, name, alias = _classify_using(child, source)
        if not name:
            continue
        if kind in ("using", "global"):
            scopes.add(name)
            external.setdefault(name, None)
        else:  # static / alias — name is a fully-qualified TYPE
            resolved = _resolve(name, scopes, index, file_path)
            simple = _simple(name)
            if resolved:
                internal.setdefault(resolved, None)
                if simple:
                    bindings[simple] = resolved
                if kind == "alias" and alias:
                    bindings[alias] = resolved
            else:
                external.setdefault(name.rsplit(".", 1)[0] if "." in name else name, None)

    for name in _referenced_types(root, source):
        if name in bindings:
            continue
        resolved = _resolve(name, scopes, index, file_path)
        if resolved:
            bindings[name] = resolved
            internal.setdefault(resolved, None)

    return list(internal), list(external), [], bindings
