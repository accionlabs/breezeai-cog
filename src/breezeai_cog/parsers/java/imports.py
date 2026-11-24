"""Java import extraction + FQCN resolution.

Java imports are fully-qualified class names (``com.acme.OrderRepo``). They resolve
to in-repo files via a repo-level **FQCN index** built by ``build_index`` (maps each
file's ``package.ClassName`` → repo-relative path). Wildcard imports stay external.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...utils import repo_relative
from ..additive import (
    collect_additive, collect_fact_sources, resolve_additive, split_fact_sources,
)
from ..constfold import Token, resolve_all
from ..index_common import (
    ClassHeritage,
    methods_where_unknown,
    parallel_map,
    record_distinct,
    seed_same_package,
)
from ..treesitter import node_text, parse_source
from .constants import collect_constants
from .heritage import HeritageFrag, build_heritage, collect_heritage, foreign_bases

_TYPE_DECLS = (
    "class_declaration", "interface_declaration", "enum_declaration",
    "record_declaration", "annotation_type_declaration",
)

#: "com.acme.Foo" → repo-relative path, or ``None`` when >1 file declares the same FQCN
#: (ambiguous → honest-null). Built by :func:`build_fqcn_index`.
FqcnIndex = dict[str, str | None]


@dataclass(frozen=True)
class JavaIndex:
    """Repo-level pre-pass result: the FQCN → path map (import resolution) plus a
    ``Class.FIELD → value`` map of resolved ``static final String`` constants (address
    folding). Both are built from a single parse of each file."""

    fqcn: FqcnIndex = field(default_factory=dict)
    consts: dict[str, str] = field(default_factory=dict)
    #: Simple class name → its in-repo base and declared methods, for inherited-method calls
    #: (``heritage.py``); ``None`` when the name is declared more than once.
    class_heritage: dict[str, ClassHeritage | None] = field(default_factory=dict)
    #: ``(file, class simple name)`` → the methods that class declares there, for each class
    #: ``class_heritage`` maps to ``None`` (``index_common.methods_where_unknown``).
    unknown_methods: dict[tuple[str, str], frozenset[str] | None] = field(default_factory=dict)
    #: Class simple name → the FQCNs its base may be, when that base is no Java class of the
    #: repository (``heritage.foreign_bases``): a Groovy class of the repository, or a library's.
    foreign_bases: dict[str, tuple[str, ...] | None] = field(default_factory=dict)
    #: FQCN (nested types as ``pkg.Outer.Inner``) → its declaration's tree-sitter type
    #: (``class_declaration``, ``interface_declaration`` …); ``None`` when two files declare it.
    #: Tells an anonymous class whether the type it instantiates is a class or an interface.
    type_kinds: dict[str, str | None] = field(default_factory=dict)
    #: Every class of the repository as ``(its file, its name below the package)`` —
    #: ``("…/Getter.java", "Getter")``, ``("…/Getter.java", "Getter.Context")`` — built from the
    #: same declarations as ``type_kinds``. Tells call resolution that a receiver typed
    #: ``Getter.Context`` is the class declared inside ``Getter``'s file, whatever its methods.
    declared_types: frozenset[tuple[str, str]] = frozenset()
    #: Facts registered with ``register_fact`` (fact name → fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)


#: One file's contribution: ``{package.TypeName: rel}``, ``{Class.FIELD: tokens}``, what
#: registered facts collect from it, and its classes' heritage.
_IndexFrag = tuple[dict[str, str], dict[str, list[Token]], dict[str, Any], list[HeritageFrag]]


def _fqcn_index_one(args: tuple[str, str, str]) -> _IndexFrag | None:
    """Parse one Java file (once) → its ``{package.TypeName: rel}`` FQCN entries **and** its
    ``static final String`` constants as ``{Class.FIELD: tokens}`` (qualified only — a bare
    field name is meaningless cross-file). Pure, picklable worker; ``None`` on read failure."""
    file_s, rel, root_s = args
    repo_root = Path(root_s)  # passed in, not derived from the path string ("\\" on Windows)
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        root = parse_source("java", source, 0).root_node
        package = ""
        for node in root.named_children:
            if node.type == "package_declaration":
                nm = next((c for c in node.named_children
                           if c.type in ("scoped_identifier", "identifier")), None)
                package = node_text(nm, source) if nm is not None else ""
                break
        fqcn_frag: dict[str, str] = {}
        for node in root.named_children:
            if node.type in _TYPE_DECLS:
                nm = node.child_by_field_name("name")
                if nm is not None:
                    name = node_text(nm, source)
                    fqcn_frag[f"{package}.{name}" if package else name] = rel
        # Qualified `Class.FIELD` keys only (drop bare names — they collide across files).
        const_frag = {k: v for k, v in collect_constants(root, source).items() if "." in k}
        return (fqcn_frag, const_frag,
                collect_additive("java", root, source, rel, repo_root),
                collect_heritage(root, source, package))
    except Exception as exc:  # parse OR a pathologically deep AST walk (RecursionError) — skip this file
        from ...logging import get_logger
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_s, language="java",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None


def build_fqcn_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> JavaIndex:
    """Repo-level pre-pass (one parse per file): the FQCN → path map for import resolution,
    plus the repo-wide ``Class.FIELD → value`` constant map for address folding. A name
    declared in >1 file with a differing value collapses to ``None`` (ambiguous → honest-null;
    the constant is then simply not folded)."""
    files, fact_files = split_fact_sources("java", list(files))
    rels = [repo_relative(f, repo_root) for f in files]
    args = [(str(f), rel, str(repo_root)) for f, rel in zip(files, rels)]
    # registered fact name → per-file values: fact-source files (another parser's), then our own
    collected: dict[str, list[Any]] = collect_fact_sources("java", fact_files, repo_root, jobs)
    fqcn: FqcnIndex = {}
    raw_consts: dict[str, list[Token] | None] = {}
    heritage: list[tuple[str, HeritageFrag]] = []
    for file_rel, frag in zip(rels, parallel_map(args, _fqcn_index_one, jobs)):
        if frag is None:
            continue
        fqcn_frag, const_frag, facts, classes = frag
        heritage.extend((file_rel, c) for c in classes)
        for name, value in facts.items():
            collected.setdefault(name, []).append(value)
        for name, rel in fqcn_frag.items():
            record_distinct(fqcn, name, rel)
        for name, tokens in const_frag.items():
            record_distinct(raw_consts, name, tokens)
    consts = resolve_all(raw_consts)
    class_heritage = build_heritage(heritage, fqcn)
    type_kinds: dict[str, str | None] = {}
    for _, cls in heritage:
        record_distinct(type_kinds, cls.fqcn, cls.kind)
    declared = frozenset(t for t in (_declared_type(rel, c.fqcn, fqcn) for rel, c in heritage)
                         if t is not None)
    index = JavaIndex(fqcn=fqcn, consts=consts, class_heritage=class_heritage,
                      unknown_methods=methods_where_unknown(
                          ((rel, c.fqcn.rsplit(".", 1)[-1], c.methods) for rel, c in heritage),
                          class_heritage),
                      foreign_bases=foreign_bases(heritage, fqcn), type_kinds=type_kinds,
                      declared_types=declared)
    # Registered facts may need the whole index, so they resolve after it is built.
    return replace(index, facts=resolve_additive("java", collected, index))


def _declared_type(rel: str, fqcn: str, top: FqcnIndex) -> tuple[str, str] | None:
    """``(rel, "Outer.Inner")`` for the class ``pkg.Outer.Inner`` declared in ``rel``: its name
    below the package, found as the longest top-level class of ``rel`` it starts with. None when
    no top-level class of that file encloses it (a local or anonymous class)."""
    parts = fqcn.split(".")
    for k in range(len(parts), 0, -1):
        if top.get(".".join(parts[:k])) == rel:
            return rel, ".".join(parts[k - 1:])
    return None


def _package_of(root: Node, source: bytes) -> str:
    for node in root.named_children:
        if node.type == "package_declaration":
            nm = next((c for c in node.named_children
                       if c.type in ("scoped_identifier", "identifier")), None)
            return node_text(nm, source) if nm is not None else ""
    return ""


def _resolve(fqcn: str, is_static: bool, index: FqcnIndex | None) -> str | None:
    if index is None:
        return None
    hit = index.get(fqcn)  # None = absent or ambiguous → unresolved
    if hit is not None or fqcn in index:
        return hit
    # A nested type (`import a.b.Outer.Inner;`) or a static member (`import static a.b.U.f;`,
    # `a.b.Outer.Inner.f`): the file of the longest enclosing top-level class. The index holds
    # top-level classes only, so a prefix found in it is a class, never a package.
    parts = fqcn.split(".")
    for k in range(len(parts) - 1, 0, -1):
        outer = ".".join(parts[:k])
        if outer in index:
            return index[outer]  # None when two files declare it: unresolved
    return None


def extract_imports(
    root: Node, source: bytes, file_path: str, repo_root: str | Path, index: FqcnIndex | None = None
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    bindings: dict[str, str] = {}  # simple class/member name → in-repo file (calls[].path)
    imported: set[str] = set()  # simple names an in-repo import binds
    shadowed: set[str] = set()  # simple names a library single-type import binds

    for node in root.named_children:
        if node.type != "import_declaration":
            continue
        is_static = any(c.type == "static" for c in node.children)
        is_wildcard = any(c.type == "asterisk" for c in node.children)
        scoped = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
        if scoped is None:
            continue
        fqcn = node_text(scoped, source)
        if is_wildcard:
            external.setdefault(fqcn + ".*", None)
            continue
        resolved = _resolve(fqcn, is_static, index)
        (internal if resolved else external).setdefault(resolved or fqcn, None)
        if resolved:  # `import a.b.Foo` → receiver "Foo"; `import static a.b.U.f` → "f"
            bindings[fqcn.rsplit(".", 1)[-1]] = resolved
            imported.add(fqcn.rsplit(".", 1)[-1])
        elif not is_static:
            shadowed.add(fqcn.rsplit(".", 1)[-1])

    seed_same_package(bindings, _package_of(root, source), index)  # same-package: no import needed
    # A single-type import of a library class hides a same-package class of that name, as
    # in Java: `import org.lib.Report;` means org.lib.Report, never this package's Report.
    for name in shadowed:
        if bindings.get(name) is not None and name not in imported:
            del bindings[name]
    return list(internal), list(external), [], bindings  # Java has no explicit exports
