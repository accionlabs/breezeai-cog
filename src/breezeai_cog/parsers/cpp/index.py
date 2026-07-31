"""Repo-wide C++ resolution index for cross-file call resolution.

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
  (``A::B::C::m``) and the trailing ``Class::method`` (``C::m``) so either call style resolves.

Every value is honest-null (``None``) the moment two differing files claim the same key —
a wrong cross-file edge is worse than a missing one (see the extend-capture skill).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...utils import repo_relative
from ..callresolve import CallResolver
from ..index_common import ClassHeritage, parallel_map, record_distinct, walk_heritage
from ..treesitter import node_text, parse_source
from .classes import _CLASS_TYPES, _heritage, _unwrap_template
from .functions import function_declarator_of, has_declaration_error
from .imports import HeaderIndex, build_header_index
from .types import field_types

_TRANSPARENT_SCOPES = frozenset({
    "preproc_ifdef", "preproc_if", "preproc_ifndef", "preproc_else", "preproc_elif",
    "linkage_specification",
})
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
class CppIndex:
    """Repo-wide resolution index (result of ``build_cpp_index``)."""

    #: header basename → repo-relative path (``None`` when >1 file shares the basename),
    #: for local ``#include`` resolution.
    headers: HeaderIndex = field(default_factory=dict)
    #: free-function simple name → defining file (``None`` when defined in >1 file).
    funcs: dict[str, str | None] = field(default_factory=dict)
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


def _has_body(node: Node) -> bool:
    """A definition carries a ``body``; a bare declaration (defined elsewhere) does not."""
    return node.child_by_field_name("body") is not None


def _record_def(frag: CppIndex, node: Node, source: bytes, rel: str, class_name: str | None) -> None:
    """Record one function/method **definition** into a per-file fragment.

    * free function (declarator is a plain ``identifier``) → ``funcs[name]``;
    * out-of-class ``Scope::name`` (declarator is a ``qualified_identifier``) → ``qual`` under
      the full scope and the trailing ``Class::method``;
    * inline member definition inside ``class_name`` → ``qual[f"{class_name}::{name}"]``.
    """
    fd = function_declarator_of(node.child_by_field_name("declarator"))
    inner = fd.child_by_field_name("declarator") if fd is not None else None
    if inner is None:
        return
    if inner.type == "identifier":
        name = node_text(inner, source)
        if class_name is not None:  # inline member definition
            _record_qual(frag, class_name, name, rel)
        elif name:
            record_distinct(frag.funcs, name, rel)
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


def _class_heritage(frag: CppIndex, class_name: str) -> ClassHeritage:
    """Get-or-create the heritage record for a class in a fragment."""
    ch = frag.heritage.get(class_name)
    if ch is None:
        ch = ClassHeritage(extends=None, decorators=[], methods={})
        frag.heritage[class_name] = ch
    return ch


def _record_qual(frag: CppIndex, scope: str, name: str, rel: str) -> None:
    """Record a definition under its trailing ``Class::method`` key (honest-null on collision),
    and register it as a method of its class for inherited-call resolution. Both the class and
    the method come from the normalized key — the tree-sitter grammar can split a deep
    ``A::B::C::m`` as scope ``A`` / name ``B::C::m``, so ``scope`` alone is not the class."""
    key = _class_method_key(scope, name)
    if key is not None:
        record_distinct(frag.qual, key, rel)
        cls, _, method = key.partition("::")  # key is exactly "Class::method"
        record_distinct(_class_heritage(frag, cls).methods, method, rel)


def _index_class(frag: CppIndex, node: Node, source: bytes, rel: str) -> None:
    """Record a class/struct's inline member-function definitions (those with a body) and
    its member-variable types (for receiver typing)."""
    name_node = node.child_by_field_name("name")
    body = node.child_by_field_name("body")
    if name_node is None or body is None:
        return
    class_name = node_text(name_node, source)
    field_types(class_name, body, source, frag.fields)
    extends, _ = _heritage(node, source)  # primary base (first) — the chain walked for inheritance
    if extends is not None:
        _class_heritage(frag, class_name).extends = _simple(extends)
    for member in body.named_children:
        member = _unwrap_template(member)
        if has_declaration_error(member):
            continue
        if member.type in _MEMBER_FN_TYPES and _has_body(member):
            if function_declarator_of(member.child_by_field_name("declarator")) is not None:
                _record_def(frag, member, source, rel, class_name)
        elif member.type in _CLASS_TYPES:
            _index_class(frag, member, source, rel)


def _index_scope(frag: CppIndex, scope: Node, source: bytes, rel: str) -> None:
    """Walk a file/namespace scope, recording every function/method definition."""
    for child in scope.named_children:
        node = _unwrap_template(child)
        if has_declaration_error(node):
            continue
        if node.type in _TRANSPARENT_SCOPES:
            _index_scope(frag, node, source, rel)
        elif node.type == "namespace_definition":
            body = node.child_by_field_name("body")
            if body is not None:
                _index_scope(frag, body, source, rel)
        elif node.type in _CLASS_TYPES:
            _index_class(frag, node, source, rel)
        elif node.type == "function_definition":
            _record_def(frag, node, source, rel, None)


def _index_one(args: tuple[str, str]) -> CppIndex | None:
    """Parse one file into a **partial** index fragment — pure, picklable, worker-safe
    (for :func:`parallel_map`). Returns ``None`` on read/parse error (a skipped file is a
    known gap, never fabricated data)."""
    file_s, rel = args
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        root = parse_source("cpp", source, 0).root_node
        frag = CppIndex()
        _index_scope(frag, root, source, rel)
        return frag
    except Exception as exc:  # parse OR a pathologically deep AST walk — skip this file
        from ...logging import get_logger
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_s, language="cpp",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None


def build_cpp_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> CppIndex:
    """Repo-level pre-pass: the ``#include`` header map (no parse) plus a parse over every
    file to map free-function / ``Scope::method`` **definitions** → path. Files are parsed
    into partial fragments across ``jobs`` processes and reduced deterministically
    (``jobs<=1`` → serial), so the result is identical regardless of fragment order."""
    repo_root = Path(repo_root)
    index = CppIndex(headers=build_header_index(repo_root, files))
    rels = [repo_relative(f, repo_root) for f in files]
    fragments = parallel_map([(str(f), rel) for f, rel in zip(files, rels)], _index_one, jobs)
    for frag in fragments:
        if frag is None:
            continue
        for name, rel in frag.funcs.items():
            _merge(index.funcs, name, rel)
        for key, rel in frag.qual.items():
            _merge(index.qual, key, rel)
        for key, ftype in frag.fields.items():
            _merge(index.fields, key, ftype)
        for cname, ch in frag.heritage.items():
            if ch is not None:  # a fragment only ever creates real records
                _merge_heritage(index.heritage, cname, ch)
    return index


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


def make_cpp_resolver(
    local_defs: set[str], path: str, index: CppIndex | None
) -> CallResolver:
    """``(name, receiver, owner=None, types=None) -> repo path | None`` for C++. ``owner`` is
    the class the call is written in (``None`` in a free function); ``types`` maps the calling
    function's local/param names to their written class type. Precision-first:

    * bare call (``receiver`` None) → same-file definition; else a repo-wide **free function**;
      else, inside a method, an implicit-``this`` call to ``owner::name``;
    * ``self``/``this`` method call → same-file method, else ``owner::name`` in the index;
    * qualified ``A::b()`` (``receiver`` is the scope) → the ``Scope::name`` definition;
    * ``obj.method()`` / ``obj->method()`` → type ``obj`` (a local/param, or ``owner``'s member
      field), then resolve ``Type::method``. An untyped/ambiguous receiver stays ``None``;
    * ``super``/``base`` call → ``None`` — resolving these needs the base-class chain, which is
      not walked here.

    Unresolved / ambiguous always returns ``None`` — never a guessed edge."""
    funcs = index.funcs if index is not None else {}
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
            return _method(owner, name) or _inherited(owner, name) or funcs.get(name)
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
