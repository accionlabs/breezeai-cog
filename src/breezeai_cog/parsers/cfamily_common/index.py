"""The repo-wide resolution index C and C++ share, for cross-file call resolution.

C++ ``#include`` pulls in a *header that only declares*; the call site gives a method
name with no binding to which class or which ``.cpp`` defines it (unlike a C# ``using`` or
a Java import, which name a symbol → file). So cross-file call resolution needs a repo-wide
pre-pass over **definitions** — the ``.cpp`` bodies where free functions and ``Class::method``
implementations actually live.

The index is built by parsing every C++ file once (a picklable per-file worker reduced
order-independently via :func:`record_distinct`, so ``--jobs`` parallelises it), and carries:

* ``headers`` — header basename → repo path, for local ``#include`` resolution (the pre-A
  behaviour, unchanged);
* ``funcs`` — **free-function** simple name → defining file. Methods are deliberately
  excluded: a bare ``GetName()`` is an implicit-``this`` member call far more often than a
  free-function call, so binding a bare name to a same-named method on an unrelated class
  would fabricate edges. Free-function names collide with member calls much less;
* ``qual`` — ``Scope::name`` → defining file, recorded under both the full scope
  (``A::B::C::m``) and the trailing ``Class::method`` (``C::m``) so either call style resolves;
* ``c_linkage`` — the free functions defined inside ``extern "C"``, the only ones C can call.

C and C++ each build one over their own files, with their own grammar and registered facts
(``parsers/c/index.py``, ``parsers/cpp/index.py``). A ``static`` free function defined in a
file that is not a header is never a target: no other file can call it. Its name still
collapses to ``None`` in ``funcs``, as any name defined twice does — cog can't see which
files a call can reach, so a same-named function elsewhere is not assumed to be the one meant.

Every value is honest-null (``None``) the moment two differing files claim the same key —
a wrong cross-file edge is worse than a missing one (see the extend-capture skill).
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tree_sitter import Node

from ...emit import class_id
from ...utils import repo_relative
from ..additive import collect_fact_sources, facts_for, split_fact_sources
from ..callresolve import CallResolver
from ..index_common import ClassHeritage, parallel_map, record_distinct, walk_heritage
from ..treesitter import node_text
from .classes import _CLASS_TYPES, _heritage, _unwrap_template, parse_cfamily
from .functions import (
    function_declarator_of, has_declaration_error, is_declared_static, read_declaration,
    read_member,
)
from .imports import FamilyHeaders, HeaderIndex, build_header_index, include_targets
from .mappings import HEADER_EXTENSIONS, TRANSPARENT_SCOPES
from .types import field_types

_MEMBER_FN_TYPES = ("field_declaration", "function_definition", "declaration")


def _class_method_key(scope: str, name: str) -> str | None:
    """Normalise a qualified name to its trailing ``Class::method`` — the last two
    ``::``-components of ``scope::name`` — so a definition and a call site match no matter
    how verbosely the namespace is written. ``None`` when there is no class component
    (a bare function has no ``Class::method`` form)."""
    parts = [p for p in f"{scope}::{name}".split("::") if p]
    if len(parts) < 2:
        return None
    return "::".join(parts[-2:])


@dataclass
class CFamilyIndex:
    """Repo-wide resolution index (result of ``build_cfamily_index``)."""

    #: header basename → repo-relative path (``None`` when >1 file shares the basename),
    #: for local ``#include`` resolution.
    headers: HeaderIndex = field(default_factory=dict)
    #: repo-relative path → its local includes as written (``#include "a/list.h"`` →
    #: ``a/list.h``), for the headers a file can see (:class:`IncludeGraph`).
    includes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: included file's basename → ``(including file, include as written)`` pairs, from
    #: ``includes``: who may include a header, checked with :class:`IncludeGraph`.
    included_by: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: free-function simple name → defining file (``None`` when defined in >1 file).
    funcs: dict[str, str | None] = field(default_factory=dict)
    #: the subset of ``funcs`` defined inside ``extern "C"`` — what a C file can call.
    c_linkage: dict[str, str | None] = field(default_factory=dict)
    #: ``Class::method`` (trailing two ``::`` components) → defining file (``None`` when
    #: ambiguous). Normalising to the last two components makes a call resolve regardless of
    #: how verbosely its scope is written (``N::C::m()`` and ``C::m()`` share the key ``C::m``).
    qual: dict[str, str | None] = field(default_factory=dict)
    #: ``Class::field`` → the field's written class type (``None`` on collision), so a
    #: ``this->member_->method()`` receiver can be typed to look up the method's file.
    fields: dict[str, str | None] = field(default_factory=dict)
    #: simple class name → its heritage (base + method→file), for resolving a call to a
    #: method a class *inherits*. ``None`` when the name is declared with differing bases
    #: (ambiguous → the resolver must not walk through it).
    heritage: dict[str, ClassHeritage | None] = field(default_factory=dict)
    #: **fully-qualified** class name (``Payroll::DataFlow::Judge``) → the declaring class's
    #: node id (``header.h#Judge``), or ``None`` when ambiguous. Lets an out-of-class
    #: ``Scope::method`` definition attach to the class node in the header rather than orphaning
    #: to its own file. Keyed on the *qualified* name (not the simple name) so a definition binds
    #: only when the whole namespace path matches — a same-named class in another namespace, or
    #: one outside the repo, does not draw a false edge. ``None`` when the same qualified name is
    #: declared in >1 file, or when the simple name is declared more than once in the one
    #: declaring file (the real node id is then disambiguated and cannot be reconstructed).
    classdecl: dict[str, str | None] = field(default_factory=dict)
    #: transient (fragment-only): fully-qualified class name → declaring file, before the
    #: reduce resolves it to a node id. Empty on the final index.
    classdecl_raw: dict[str, str | None] = field(default_factory=dict)
    #: Facts registered with ``register_fact`` (fact name → fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)
    #: transient (fragment-only): what registered facts collected from this one file.
    collected: dict[str, Any] = field(default_factory=dict)


#: A language's per-file fact collection, run on the tree the index already parsed:
#: ``(root, source, rel, repo_root) -> {fact name: value}``. Module-level, so it pickles.
FactCollector = Callable[[Node, bytes, str, Path], dict[str, Any]]
#: A language's fact resolution over every file's collection, once the index is built.
FactResolver = Callable[[dict[str, list[Any]], "CFamilyIndex"], dict[str, Any]]


def _has_body(node: Node) -> bool:
    """A definition carries a ``body``; a bare declaration (defined elsewhere) does not."""
    return node.child_by_field_name("body") is not None


def _file_local(node: Node, source: bytes, rel: str) -> bool:
    """A ``static`` free function outside a header: visible to its own file only."""
    if Path(rel).suffix.lower() in HEADER_EXTENSIONS:
        return False
    return is_declared_static(node, source)


def _record_def(
    frag: CFamilyIndex, node: Node, source: bytes, rel: str, class_name: str | None,
    c_linkage: bool = False, name: str | None = None,
) -> None:
    """Record one function/method **definition** into a per-file fragment.

    * free function (declarator is a plain ``identifier``) → ``funcs[name]`` (and
      ``c_linkage[name]`` inside ``extern "C"``); a file-local one only blocks its name
      (``None``), it is never a target;
    * out-of-class ``Scope::name`` (declarator is a ``qualified_identifier``) → ``qual`` under
      the full scope and the trailing ``Class::method``;
    * inline member definition inside ``class_name`` → ``qual[f"{class_name}::{name}"]``.
    """
    fd = function_declarator_of(node.child_by_field_name("declarator"))
    inner = fd.child_by_field_name("declarator") if fd is not None else None
    if inner is None:
        return
    if inner.type == "identifier":
        name = name or node_text(inner, source)  # `name`: one a macro moved (read_declaration)
        if class_name is not None:  # inline member definition
            _record_qual(frag, class_name, name, rel)
        elif name and _file_local(node, source, rel):
            frag.funcs[name] = None
        elif name:
            record_distinct(frag.funcs, name, rel)
            if c_linkage:
                record_distinct(frag.c_linkage, name, rel)
    elif inner.type == "qualified_identifier":
        nm = inner.child_by_field_name("name")
        scope_node = inner.child_by_field_name("scope")
        name = node_text(nm, source) if nm is not None else ""
        scope = node_text(scope_node, source) if scope_node is not None else ""
        if name and scope:
            _record_qual(frag, scope, name, rel)


def _simple(name: str) -> str:
    """Simple (unqualified) class name — drop namespace and template args."""
    return name.rsplit("::", 1)[-1].split("<", 1)[0]


def _join_ns(ns: str, name: str) -> str:
    """Append a name to a ``::``-joined namespace/class path (``"A::B"`` + ``"C"`` → ``"A::B::C"``)."""
    return f"{ns}::{name}" if ns else name


def _ns_name(node: Node, source: bytes) -> str:
    """The name of a ``namespace_definition`` (may be nested, ``A::B``); ``""`` for an
    anonymous namespace (its members keep the enclosing path)."""
    nm = node.child_by_field_name("name")
    return node_text(nm, source) if nm is not None else ""


def _class_heritage(frag: CFamilyIndex, class_name: str) -> ClassHeritage:
    """Get-or-create the heritage record for a class in a fragment."""
    ch = frag.heritage.get(class_name)
    if ch is None:
        ch = ClassHeritage(extends=None, decorators=[], methods={})
        frag.heritage[class_name] = ch
    return ch


def _record_qual(frag: CFamilyIndex, scope: str, name: str, rel: str) -> None:
    """Record a definition under its trailing ``Class::method`` key (honest-null on collision),
    and register it as a method of its class for inherited-call resolution. Both the class and
    the method come from the normalized key — the tree-sitter grammar can split a deep
    ``A::B::C::m`` as scope ``A`` / name ``B::C::m``, so ``scope`` alone is not the class."""
    key = _class_method_key(scope, name)
    if key is not None:
        record_distinct(frag.qual, key, rel)
        cls, _, method = key.partition("::")  # key is exactly "Class::method"
        record_distinct(_class_heritage(frag, cls).methods, method, rel)


def _index_class(frag: CFamilyIndex, node: Node, source: bytes, rel: str, ns: str) -> None:
    """Record a class/struct's declaration (its fully-qualified name → file), its inline
    member-function definitions (those with a body), and its member-variable types. ``ns`` is
    the enclosing namespace/class path, so the class is keyed by its qualified name."""
    name_node = node.child_by_field_name("name")
    body = node.child_by_field_name("body")
    if name_node is None or body is None:
        return
    class_name = node_text(name_node, source)
    fqn = _join_ns(ns, class_name)
    record_distinct(frag.classdecl_raw, fqn, rel)  # this file declares the class body
    field_types(class_name, body, source, frag.fields)
    extends, _ = _heritage(node, source)  # primary base (first) — the chain walked for inheritance
    if extends is not None:
        _class_heritage(frag, class_name).extends = _simple(extends)
    for member in body.named_children:
        member = _unwrap_template(member)
        if read_member(member, source) is None:
            continue
        if member.type in _MEMBER_FN_TYPES and _has_body(member):
            if function_declarator_of(member.child_by_field_name("declarator")) is not None:
                _record_def(frag, member, source, rel, class_name)
        elif member.type in _CLASS_TYPES:  # nested type — qualified under this class
            _index_class(frag, member, source, rel, fqn)


def _is_extern_c(node: Node, source: bytes) -> bool:
    """``extern "C"`` (not ``extern "C++"``)."""
    value = node.child_by_field_name("value")
    return value is not None and node_text(value, source).strip('"') == "C"


def _index_scope(
    frag: CFamilyIndex, scope: Node, source: bytes, rel: str, ns: str = "", c_linkage: bool = False
) -> None:
    """Walk a file/namespace scope, recording every definition. ``ns`` accumulates the
    enclosing namespace path so classes are keyed by their fully-qualified name; ``c_linkage``
    is set inside ``extern "C"``."""
    for child in scope.named_children:
        node = _unwrap_template(child)
        if node.type == "linkage_specification":
            _index_scope(frag, node, source, rel, ns, c_linkage or _is_extern_c(node, source))
        elif node.type in TRANSPARENT_SCOPES:
            _index_scope(frag, node, source, rel, ns, c_linkage)
        elif node.type == "declaration":
            continue  # a prototype or variable: nothing to index
        elif node.type == "function_definition":
            read = read_declaration(node, source)
            if read is not None:
                _record_def(frag, node, source, rel, None, c_linkage, name=read.name)
        elif has_declaration_error(node):
            continue
        elif node.type == "namespace_definition":
            body = node.child_by_field_name("body")
            if body is not None:
                _index_scope(frag, body, source, rel, _join_ns(ns, _ns_name(node, source)))
        elif node.type in _CLASS_TYPES:
            _index_class(frag, node, source, rel, ns)


def _index_one(
    args: tuple[str, str, str, int, FactCollector | None, str],
) -> CFamilyIndex | None:
    """Parse one file into a **partial** index fragment — pure, picklable, worker-safe
    (for :func:`parallel_map`). Returns ``None`` on read/parse error (a skipped file is a
    known gap, never fabricated data). ``collect`` (when facts are registered) runs on the
    same tree, so a fact costs no second parse."""
    file_s, rel, grammar, timeout_micros, collect, repo_root = args
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        root = parse_cfamily(grammar, source, timeout_micros)
        frag = CFamilyIndex(includes={rel: tuple(include_targets(source))})
        _index_scope(frag, root, source, rel)
        if collect is not None:
            frag.collected = collect(root, source, rel, Path(repo_root))
        return frag
    except Exception as exc:  # parse OR a pathologically deep AST walk — skip this file
        from ...logging import get_logger
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_s, language=grammar,
            error_type=type(exc).__name__, error=str(exc),
        )
        return None


def build_cfamily_index(
    repo_root: Path,
    files: Sequence[Path],
    jobs: int,
    *,
    grammar: str,
    language: str,
    collect: FactCollector,
    resolve: FactResolver,
    parse_timeout_micros: int = 0,
) -> CFamilyIndex:
    """Repo-level pre-pass: the ``#include`` header map (no parse) plus a parse over every
    file to map free-function / ``Scope::method`` **definitions** → path. Files are parsed
    into partial fragments across ``jobs`` processes and reduced deterministically
    (``jobs<=1`` → serial), so the result is identical regardless of fragment order.

    ``grammar`` / ``language`` / ``collect`` / ``resolve`` are the language's
    (``parsers/c/index.py``, ``parsers/cpp/index.py``). Registered facts collect on the same
    parse, and from the files another parser owns that a fact reads (``FactSpec.sources``)."""
    repo_root = Path(repo_root)
    own, fact_files = split_fact_sources(language, list(files))
    index = CFamilyIndex(headers=build_header_index(repo_root, own))
    fqn_file: dict[str, str | None] = {}  # fully-qualified class name → declaring file (or None)
    collected: dict[str, list[Any]] = collect_fact_sources(language, fact_files, repo_root, jobs)
    per_file = collect if facts_for(language) else None
    rels = [repo_relative(f, repo_root) for f in own]
    fragments = parallel_map(
        [(str(f), rel, grammar, parse_timeout_micros, per_file, str(repo_root))
         for f, rel in zip(own, rels)],
        _index_one, jobs,
    )
    for frag in fragments:
        if frag is None:
            continue
        for name, value in frag.collected.items():  # in file order: fragments keep it
            collected.setdefault(name, []).append(value)
        index.includes.update(frag.includes)
        for name, rel in frag.funcs.items():
            _merge(index.funcs, name, rel)
        for name, rel in frag.c_linkage.items():
            _merge(index.c_linkage, name, rel)
        for key, rel in frag.qual.items():
            _merge(index.qual, key, rel)
        for key, ftype in frag.fields.items():
            _merge(index.fields, key, ftype)
        for fqn, decl_rel in frag.classdecl_raw.items():
            _merge(fqn_file, fqn, decl_rel)
        for cname, ch in frag.heritage.items():
            if ch is not None:  # a fragment only ever creates real records
                _merge_heritage(index.heritage, cname, ch)
    index.classdecl = _finalize_classdecl(fqn_file)
    for rel, written_all in index.includes.items():
        for written in written_all:
            index.included_by.setdefault(posixpath.basename(written), []).append((rel, written))
    # Registered facts may read the finished index, so they resolve after it is built.
    index.facts = resolve(collected, index)
    return index


def _finalize_classdecl(fqn_file: dict[str, str | None]) -> dict[str, str | None]:
    """Resolve each fully-qualified class name to its node id, honest-null where it cannot be
    resolved unambiguously. A class node's id is ``file#simplename`` (the simple name, even for
    a namespaced/nested class), so the id is reconstructable **only** when that simple name is
    declared once in the file; if the same simple name is declared more than once in one file
    (different namespaces), the real ids are disambiguated (``#2`` …) and cannot be reconstructed
    from the name alone, so those entries collapse to ``None`` rather than draw a wrong edge."""
    per_file_simple: dict[tuple[str, str], set[str]] = {}
    for fqn, rel in fqn_file.items():
        if rel is not None:
            per_file_simple.setdefault((rel, _simple(fqn)), set()).add(fqn)
    out: dict[str, str | None] = {}
    for fqn, rel in fqn_file.items():
        if rel is None:  # same qualified name declared in >1 file — ambiguous
            out[fqn] = None
            continue
        simple = _simple(fqn)
        # unique simple name in this file → id is exactly file#simple; else honest-null
        out[fqn] = class_id(rel, simple) if len(per_file_simple[(rel, simple)]) == 1 else None
    return out


def _merge_heritage(
    dst: dict[str, ClassHeritage | None], name: str, frag_ch: ClassHeritage
) -> None:
    """Fold one fragment's heritage for a class into the shared map. A class's declaration
    (its base) and its out-of-file method definitions arrive in separate fragments, so base
    and methods are merged across them; a differing base collapses the whole record to
    ``None`` (honest-null)."""
    if name not in dst:
        dst[name] = ClassHeritage(
            extends=frag_ch.extends, decorators=[], methods=dict(frag_ch.methods))
        return
    cur = dst[name]
    if cur is None:  # already ambiguous
        return
    if frag_ch.extends is not None:
        if cur.extends is None:
            cur.extends = frag_ch.extends
        elif cur.extends != frag_ch.extends:  # two different bases for one name → refuse
            dst[name] = None
            return
    for m, f in frag_ch.methods.items():
        record_distinct(cur.methods, m, f)


def _merge(dst: dict[str, str | None], key: str, value: str | None) -> None:
    """Fold a fragment entry into the shared map: an already-ambiguous entry stays ``None``;
    a differing value collapses to ``None`` (honest-null); equal values coalesce."""
    if value is None:
        dst[key] = None
        return
    record_distinct(dst, key, value)


class IncludeGraph:
    """The repo's ``#include "…"`` graph across the file's own and related languages'
    indexes, for the headers a file can see.

    An include is looked up the way a compiler looks up ``#include "…"``: next to the file
    that includes it first, else the one repo file with that name (``headers``). One that
    resolves to neither adds nothing."""

    def __init__(self, headers: FamilyHeaders, indexes: Sequence[CFamilyIndex]) -> None:
        self.headers = headers
        self.includes = [i.includes for i in indexes]
        self.included_by = [i.included_by for i in indexes]
        self._closures: dict[str, set[str]] = {}

    def _lookup(self, includer: str, written: str) -> str | None:
        local = posixpath.normpath(posixpath.join(posixpath.dirname(includer), written))
        if any(local in m for m in self.includes):
            return local
        return self.headers.get(posixpath.basename(written))

    def closure(self, path: str, direct: Sequence[str] = ()) -> set[str]:
        """``path`` and every file it includes, directly or through other includes.
        ``direct`` adds includes already resolved for it (its imports)."""
        if path in self._closures and not direct:
            return self._closures[path]
        seen: set[str] = set()
        pending = [path, *direct]
        while pending:
            file = pending.pop()
            if file in seen:
                continue
            seen.add(file)
            for written in (t for m in self.includes for t in m.get(file, ())):
                target = self._lookup(file, written)
                if target is not None and target not in seen:
                    pending.append(target)
        if not direct:
            self._closures[path] = seen
        return seen

    def includers(self, header: str) -> list[str]:
        """The files whose own ``#include`` resolves to ``header``."""
        name = posixpath.basename(header)
        return sorted({
            file
            for m in self.included_by
            for file, written in m.get(name, ())
            if self._lookup(file, written) == header
        })


def header_visibility(
    path: str, direct: Sequence[str], graph: IncludeGraph
) -> Callable[[str], bool]:
    """Whether the file at ``path`` can see a header: the header is in the file's include
    closure (``direct`` are its resolved imports) or, for a header, in the closure of every
    file that includes it — a header is compiled only inside them, and often relies on what
    they included first. Computed on first use."""
    own: list[set[str]] = []
    context: list[list[set[str]]] = []

    def sees(header: str) -> bool:
        if not own:
            own.append(graph.closure(path, direct))
        if header in own[0]:
            return True
        if Path(path).suffix.lower() not in HEADER_EXTENSIONS:
            return False
        if not context:
            context.append([graph.closure(f) for f in graph.includers(path) if f != path])
        return bool(context[0]) and all(header in c for c in context[0])

    return sees


def make_cfamily_resolver(
    local_defs: set[str], path: str, index: CFamilyIndex | None,
    related_funcs: Sequence[Mapping[str, str | None]] = (),
    sees: Callable[[str], bool] | None = None,
) -> CallResolver:
    """``(name, receiver, owner=None, types=None) -> repo path | None`` for C and C++.
    ``owner`` is the class the call is written in (``None`` in a free function); ``types`` maps
    the calling function's local/param names to their written class type. Precision-first:

    * bare call (``receiver`` None) → same-file definition; else a repo-wide **free function**;
      else, inside a method, an implicit-``this`` call to ``owner::name``;
    * ``self``/``this`` method call → same-file method, else ``owner::name`` in the index;
    * qualified ``A::b()`` (``receiver`` is the scope) → the ``Scope::name`` definition;
    * ``obj.method()`` / ``obj->method()`` → type ``obj`` (a local/param, or ``owner``'s member
      field), then resolve ``Type::method``. An untyped/ambiguous receiver stays ``None``;
    * ``super``/``base`` call → ``None`` — resolving these needs the base-class chain, which is
      not walked here.

    ``related_funcs`` are the free functions this file can call in its related languages (C++
    → every C function; C → the C++ functions inside ``extern "C"``). A bare call checks them
    only when its own language has no definition of the name, and binds only when they agree.

    ``sees`` (see :func:`header_visibility`) limits a free function defined in a header to
    the headers the file can see: the one definition cog sees may not be the one the file is
    built with — a vendored copy in an ignored folder is invisible, and its calls would land
    on the first-party copy.

    Unresolved / ambiguous always returns ``None`` — never a guessed edge."""
    funcs = index.funcs if index is not None else {}

    def _visible(target: str | None) -> str | None:
        if target is None or sees is None or target == path:
            return target
        if Path(target).suffix.lower() not in HEADER_EXTENSIONS:
            return target
        return target if sees(target) else None

    def _free(name: str) -> str | None:
        """A repo-wide free function: the file's own language first (an ambiguous ``None``
        there stops the lookup), else the one file its related languages agree on. A header
        target must be one the file includes."""
        if name in funcs:
            return _visible(funcs[name])
        found = {related[name] for related in related_funcs if name in related}
        return _visible(found.pop()) if len(found) == 1 else None
    qual = index.qual if index is not None else {}
    fields = index.fields if index is not None else {}
    heritage = index.heritage if index is not None else {}

    def _method(scope: str | None, name: str) -> str | None:
        """Resolve ``scope::name`` via the trailing ``Class::method`` key (``None`` for a
        variable receiver or an unknown class — never a guessed edge)."""
        if not scope:
            return None
        key = _class_method_key(scope, name)
        return qual.get(key) if key is not None else None

    def _inherited(owner: str | None, name: str) -> str | None:
        """A method ``owner`` inherits: walk its base chain nearest-first, the first ancestor
        that declares ``name`` wins (its file, or ``None`` when that file is ambiguous). Stops
        at a base that is external or ambiguous — never resolves past what it can see."""
        if not owner:
            return None
        simple = owner.rsplit("::", 1)[-1]
        start = heritage.get(simple)
        if start is None:
            return None
        for anc in walk_heritage(simple, start.extends, heritage).ancestors:
            if name in anc.methods:
                return anc.methods[name]
        return None

    def _receiver_type(receiver: str, owner: str | None, types: dict[str, str | None]) -> str | None:
        """Written class type of an ``obj`` / ``this->obj`` receiver: a local/param first,
        else ``owner``'s member field. ``None`` (not resolved) for anything that is not a bare
        variable (a chained ``a.b`` receiver, a call, …) or whose type is unknown/ambiguous."""
        var = receiver[len("this->"):] if receiver.startswith("this->") else receiver
        if not var.isidentifier():  # chained / expression receiver — needs deeper analysis
            return None
        if var in types:  # a known local/param (value may be None = ambiguous → do not resolve)
            return types[var]
        return fields.get(f"{owner}::{var}") if owner else None

    def resolve(
        name: str, receiver: str | None, owner: str | None = None,
        types: dict[str, str | None] | None = None,
    ) -> str | None:
        types = types or {}
        if receiver is None:
            if name in local_defs:  # same-file free function / out-of-class method
                return path
            # implicit-this: own method, then an inherited one (a member hides a free function
            # in C++ name lookup), then a repo-wide free function.
            return _method(owner, name) or _inherited(owner, name) or _free(name)
        if receiver in ("self", "this"):  # explicit this-> : own method, then inherited
            if name in local_defs:
                return path
            return _method(owner, name) or _inherited(owner, name)
        if receiver in ("super", "base", "MyBase"):  # explicit base call → an inherited method
            return _inherited(owner, name)
        # A qualified A::b() has a class scope as receiver; an obj.m() has a variable. Try the
        # scope reading first (harmless miss when the receiver is a variable — no class is named
        # like a lowercase var), then type the receiver and resolve Type::method.
        return _method(receiver, name) or _method(_receiver_type(receiver, owner, types), name)

    return resolve
