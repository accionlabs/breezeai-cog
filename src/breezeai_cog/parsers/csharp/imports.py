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

**Project scoping:** when a name resolves to >1 in-scope file, we break the tie the way
the C# compiler does — a type declared in the consumer's **own project (assembly)** wins
over an identically-named one imported from another project (warning CS0436, "source
wins"). Projects are the directories containing a ``.csproj``. If exactly one candidate is
in the consumer's project we bind it; otherwise we still refuse (a tie purely between other
projects is CS0433, which the compiler itself errors on rather than guessing).

``using static Ns.Type`` and ``using Alias = Ns.Type`` name a *type* (fully qualified),
so they resolve by direct lookup. Plain ``using``/``global using`` namespaces are still
recorded as external (the namespace itself is not a single file)."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..index_common import (
    ClassHeritage, merge_heritage, parallel_map, project_heritage, record_distinct,
)
from ..treesitter import node_text, parse_source
from .functions import extract_attributes, flags

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
    #: repo-relative dirs containing a ``.csproj`` (an assembly boundary), sorted
    #: longest-first so the nearest ancestor of a file is its owning project.
    project_roots: list[str] = field(default_factory=list)
    #: simple class name → its heritage (base + decorators + method→file), for cross-file
    #: base-class resolution. Value is ``None`` when the name is declared by >1 class with
    #: differing bases (ambiguous → callers must not resolve through it: honest-null).
    class_heritage: dict[str, ClassHeritage | None] = field(default_factory=dict)
    #: (extension-method name, simple ``this``-param type) → defining file. ``None`` when the
    #: same (name, type) is defined in >1 file (ambiguous → honest-null). Drives the resolver's
    #: extension-method tier (``recv.M()`` where ``M`` is ``static M(this T …)``).
    ext_methods: dict[tuple[str, str], str | None] = field(default_factory=dict)
    #: physical ``.aspx`` repo path (``~/`` stripped) → its friendly ``MapPageRoute`` URLs,
    #: sorted. Web Forms routing (BREEZEAI-765 item 2): the Web Forms parser overrides a page's
    #: physical endpoint with these real routed URLs. Registrations are central (Global.asax /
    #: RouteConfig), so they cross files — hence they ride this repo-level index.
    page_routes: dict[str, list[str]] = field(default_factory=dict)

    def project_of(self, path: str) -> str | None:
        """The owning project (nearest ancestor ``.csproj`` dir) of a repo-relative file."""
        for root in self.project_roots:  # longest-first → nearest ancestor wins
            if root == "" or path == root or path.startswith(root + "/"):
                return root
        return None


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


def _base_name(node: Node, source: bytes) -> str | None:
    """Simple name of the base *class* (not interfaces) in a type's ``base_list``, or
    None. Mirrors the class builder's (first-entry-unless-interface) heuristic and strips
    the namespace qualifier + generics so it keys the simple-name heritage map."""
    base = node.child_by_field_name("base_list") or next(
        (c for c in node.named_children if c.type == "base_list"), None)
    if base is None:
        return None
    names = [node_text(c, source) for c in base.named_children
             if c.type in ("identifier", "qualified_name", "generic_name")]
    if not names:
        return None
    short = names[0].rsplit(".", 1)[-1].split("<", 1)[0]
    if len(short) >= 2 and short[0] == "I" and short[1].isupper():
        return None  # base_list holds only interfaces
    return short


def _record_heritage(
    by_fqn: dict[str, ClassHeritage | None], fqn: str, node: Node, source: bytes,
) -> None:
    """Merge a class declaration's heritage into the FQN-keyed map (partial-class aware via
    :func:`merge_heritage`; projected to simple names after the walk)."""
    merge_heritage(by_fqn, fqn, _base_name(node, source), extract_attributes(node, source))


def _extension_this_type(node: Node, source: bytes) -> str | None:
    """Simple type of a method's ``this`` (extension) parameter, or ``None`` when ``node`` is
    not a ``static M(this T …)`` extension method."""
    _, is_static = flags(node, source)
    if not is_static:
        return None
    params = node.child_by_field_name("parameters")
    first = next((p for p in params.named_children if p.type == "parameter"), None) if params else None
    if first is None:
        return None
    if not any(c.type == "modifier" and node_text(c, source) == "this" for c in first.children):
        return None
    tnode = first.child_by_field_name("type")
    return _simple(node_text(tnode, source)) if tnode is not None else None


def _index_members(
    fqn: str, body: Node, source: bytes, rel: str, index: CSharpIndex,
    method_files: dict[tuple[str, str], str | None],
) -> None:
    """Record the class's declared methods (``(fqn, method) → file``, for inherited-call
    resolution) and any extension methods it defines (``(method, this-type) → file``)."""
    for member in body.named_children:
        if member.type != "method_declaration":
            continue
        mn = member.child_by_field_name("name")
        if mn is None:
            continue
        mname = node_text(mn, source)
        record_distinct(method_files, (fqn, mname), rel)
        this_type = _extension_this_type(member, source)
        if this_type:
            record_distinct(index.ext_methods, (mname, this_type), rel)


def _called_name(inv: Node, source: bytes) -> str | None:
    """Simple method name of an ``invocation_expression`` (``x.MapPageRoute(…)`` → ``MapPageRoute``)."""
    fn = inv.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "member_access_expression":
        nm = fn.child_by_field_name("name")
        return node_text(nm, source) if nm is not None else None
    if fn.type == "identifier":
        return node_text(fn, source)
    return None


def _positional_args(inv: Node) -> list[Node]:
    """Argument value expressions of an invocation, in order (named args unwrapped to value)."""
    arglist = inv.child_by_field_name("arguments")
    if arglist is None:
        return []
    out: list[Node] = []
    for a in arglist.named_children:
        if a.type == "argument":
            if a.named_children:
                out.append(a.named_children[-1])  # value is the last child (after any `name:`)
        else:
            out.append(a)
    return out


def _string_literal(node: Node | None, source: bytes) -> str | None:
    """Unquoted text of a plain/verbatim C# string literal; ``None`` for interpolated or
    non-literal args (honest-null — never a guessed value)."""
    if node is None or node.type not in ("string_literal", "verbatim_string_literal"):
        return None
    txt = node_text(node, source)
    if txt.startswith("@"):  # verbatim  @"…"
        txt = txt[1:]
    if len(txt) >= 2 and txt[0] == '"' and txt[-1] == '"':
        return txt[1:-1]
    return None


def _physical_aspx_key(physical: str) -> str | None:
    """A ``MapPageRoute`` physical arg → repo-relative ``.aspx`` key (``~/CMS/E.aspx`` →
    ``CMS/E.aspx``). Assumes app-root = repo-root: a subdir app is a false-negative (page
    keeps its physical endpoint), never a wrong match."""
    p = physical.strip().replace("\\", "/")
    if p.startswith("~/"):
        p = p[2:]
    elif p.startswith("/"):
        p = p[1:]
    p = posixpath.normpath(p)
    return p if p.lower().endswith(".aspx") and not p.startswith("..") else None


def _index_map_page_routes(root: Node, source: bytes, index: CSharpIndex) -> None:
    """Collect ``routes.MapPageRoute(name, url, "~/physical.aspx")`` mappings from the AST
    (not raw text — so matches in comments/strings are excluded). Only string-literal
    ``url``/``physical`` args are recorded; the physical ``.aspx`` keys the friendly url.
    Iterative walk (no recursion — deep ASTs can't blow the stack)."""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "invocation_expression" and _called_name(n, source) == "MapPageRoute":
            args = _positional_args(n)
            if len(args) >= 3:
                url = _string_literal(args[1], source)
                key = _physical_aspx_key(_string_literal(args[2], source) or "")
                if url is not None and key is not None:
                    urls = index.page_routes.setdefault(key, [])
                    if url not in urls:
                        urls.append(url)
        stack.extend(n.named_children)


def _index_file(
    root: Node, source: bytes, rel: str, index: CSharpIndex,
    by_fqn: dict[str, ClassHeritage | None],
    method_files: dict[tuple[str, str], str | None],
) -> None:
    """Add one file's ``global using`` namespaces + declared types + members to ``index``.
    Heritage is accumulated into ``by_fqn`` (fully-qualified names) for later projection."""
    for child in root.named_children:
        if child.type == "using_directive":
            kind, name, _ = _classify_using(child, source)
            if kind == "global" and name:
                index.global_usings.add(name)
    if b"MapPageRoute" in source:  # cheap gate — full-tree scan only where it can match
        _index_map_page_routes(root, source, index)

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
                body = child.child_by_field_name("body")
                if nm:
                    fqn = _join(local_ns, nm)
                    index.types.setdefault(fqn, set()).add(rel)
                    _record_heritage(by_fqn, fqn, child, source)
                    if body is not None:
                        _index_members(fqn, body, source, rel, index, method_files)
                if body is not None:
                    walk(body, local_ns)  # nested types share the enclosing namespace

    walk(root, "")


def _discover_project_roots(repo_root: Path, live_dirs: set[str]) -> list[str]:
    """Repo-relative dirs holding a ``.csproj`` (assembly boundaries), longest-first.

    Only projects that actually own scanned source are kept: a ``.csproj`` counts only
    when a parsed ``.cs`` file lives at or below its directory. Since ``files`` is already
    ignore-filtered by the scanner, this transitively honours every ignore layer (global
    defaults, the language ``ignore.txt``, per-dir ``.gitignore``/``.repoignore``, and
    include overrides) — so build output (``bin``/``obj``) and vendored ``packages`` drop
    out on their own, with no separate skip list to keep in sync."""
    roots: set[str] = set()
    try:
        for csproj in repo_root.rglob("*.csproj"):
            d = repo_relative(csproj.parent, repo_root)
            if d in live_dirs:  # a scanned .cs file lives under this project dir
                roots.add(d)
    except OSError:
        pass
    return sorted(roots, key=len, reverse=True)


_Fragment = tuple[CSharpIndex, dict[str, ClassHeritage | None], dict[tuple[str, str], str | None]]


def _index_one(args: tuple[str, str]) -> _Fragment | None:
    """Parse one file into a **partial** index fragment — pure, no shared state, so it is
    safe to run in a worker process (module-level + picklable, for :func:`parallel_map`).
    Returns ``None`` on read error."""
    file_s, rel = args
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        root = parse_source("csharp", source, 0).root_node
        frag = CSharpIndex()
        by_fqn: dict[str, ClassHeritage | None] = {}
        method_files: dict[tuple[str, str], str | None] = {}
        _index_file(root, source, rel, frag, by_fqn, method_files)
        return frag, by_fqn, method_files
    except Exception as exc:  # parse OR a pathologically deep AST walk (RecursionError) — skip this file
        from ...logging import get_logger
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_s, language="csharp",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None


def _merge_fragment(
    index: CSharpIndex,
    by_fqn: dict[str, ClassHeritage | None],
    method_files: dict[tuple[str, str], str | None],
    frag: _Fragment,
) -> None:
    """Reduce one file fragment into the shared index using the same collapse rules as the
    serial build (so the result is identical regardless of fragment order)."""
    fidx, fby, fmf = frag
    index.global_usings |= fidx.global_usings
    for tname, tfiles in fidx.types.items():
        index.types.setdefault(tname, set()).update(tfiles)
    for pkey, purls in fidx.page_routes.items():
        dst = index.page_routes.setdefault(pkey, [])
        dst.extend(u for u in purls if u not in dst)
    for ekey, efile in fidx.ext_methods.items():
        record_distinct(index.ext_methods, ekey, efile)
    for mkey, mfile in fmf.items():
        record_distinct(method_files, mkey, mfile)
    for fqn, ch in fby.items():
        if ch is None:
            by_fqn[fqn] = None  # ambiguous within the file → refuse
        else:
            merge_heritage(by_fqn, fqn, ch.extends, ch.decorators)


def build_csharp_index(repo_root: Path, files, jobs: int = 1) -> CSharpIndex:
    """Repo-level pre-pass: parse each ``.cs`` file and map declared types → path, and record
    project (``.csproj``) roots for the same-project tiebreak. Files are parsed into partial
    fragments across ``jobs`` processes and reduced deterministically (``jobs<=1`` → serial)."""
    repo_root = Path(repo_root)
    rels = [repo_relative(f, repo_root) for f in files]
    live_dirs = {""}  # every ancestor dir of a scanned file (its owning-project candidates)
    for rel in rels:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            live_dirs.add("/".join(parts[:i]))
    index = CSharpIndex(project_roots=_discover_project_roots(repo_root, live_dirs))
    by_fqn: dict[str, ClassHeritage | None] = {}   # partials merged per fully-qualified name
    method_files: dict[tuple[str, str], str | None] = {}
    # MAP: parse each file into a pure partial fragment (parallel over ``jobs`` processes).
    fragments = parallel_map([(str(f), rel) for f, rel in zip(files, rels)], _index_one, jobs)
    # REDUCE: fold fragments into the shared index (deterministic, order-independent).
    for frag in fragments:
        if frag is not None:
            _merge_fragment(index, by_fqn, method_files, frag)
    # attach each class's method→file map to its (unambiguous) heritage record, then project
    # fully-qualified heritage down to simple names (distinct types sharing a name → None)
    for (fqn, mname), mfile in method_files.items():
        heritage = by_fqn.get(fqn)
        if heritage is not None:
            heritage.methods[mname] = mfile
    index.class_heritage = project_heritage(by_fqn)
    # canonicalise friendly-url lists so output is fragment-order-independent (deterministic).
    index.page_routes = {k: sorted(set(v)) for k, v in index.page_routes.items()}
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
    external / unresolved / **ambiguous** (a tie no rule can break)."""
    if index is None or not name:
        return None
    if "." in name and name in index.types:  # already fully qualified (static/alias target)
        hits = set(index.types[name])
    else:
        hits = set()
        for ns in scopes:
            hits |= index.types.get(_join(ns, name), set())
    hits.discard(self_path)  # cross-file edges only
    if len(hits) == 1:
        return next(iter(hits))
    if len(hits) > 1:
        # Same-project wins (C# CS0436: a type in the consumer's own assembly
        # beats an identically-named one imported from another project). A tie purely
        # between *other* projects is CS0433, which the compiler errors on — so we refuse.
        proj = index.project_of(self_path)
        if proj is not None:
            same = [h for h in hits if index.project_of(h) == proj]
            if len(same) == 1:
                return same[0]
    return None


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
