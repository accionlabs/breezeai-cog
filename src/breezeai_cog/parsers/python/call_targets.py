"""Which file a Python call goes to — only a file that declares what is called (BREEZEAI-1653).

The shared resolver gives an imported name its import's file, unchecked:

* **A package re-export.** ``from petruk.common.llm import run_structured_llm_call`` binds it to
  ``llm/__init__.py``, which only imports it from a submodule. → The binding follows the
  package's own imports (``from .client import run_structured_llm_call``, ``from .x import *``)
  to the file that declares it.
* **A submodule.** ``from pkg import a`` with ``pkg/a.py`` and nothing named ``a`` in
  ``pkg/__init__.py``: ``a`` is the module ``pkg/a.py``, so ``a.save()`` → ``pkg/a.py``.
* **A member of an imported object.** ``from utils.logger import logger``; ``logger.info()``:
  ``info`` is the standard library logger's, not a function of ``logger.py``. → The file only when
  it declares the name.

**What a file declares** is every ``def`` and ``class`` name in it, at any depth — the names the
parser emits Functions and classes for (``defined_names``). Collected once per file into the
``python.callables`` fact, with each file's ``from … import`` bindings for the re-export walk.

Unchanged: same-file calls, ``self.m()`` (already the file's own names), calls with no path.
When the declaring file cannot be proven, the call keeps no path (missing, not wrong).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..callresolve import CallResolver
from ..treesitter import node_text
from .functions import defined_names
from .imports import _Roots, _dotted, _resolve

FACT = "python.callables"
#: Nodes a statement can sit in, besides ``*_statement`` / ``*_clause`` / ``*_definition``.
_HOLDS_STATEMENTS = frozenset({"module", "block", "decorated_definition", "case_clause"})
#: How many re-exports deep a name is followed.
_DEPTH = 8

#: A class of a file: (its name, its bases as written, the methods it declares).
ClassFacts = tuple[str, tuple[str, ...], frozenset[str]]
#: One file: (its def / class names, ``from`` imports: local name → (file, imported name),
#: files it star-imports, its classes).
FileFacts = tuple[frozenset[str], dict[str, tuple[str, str]], tuple[str, ...],
                  tuple[ClassFacts, ...]]
#: Bases that add no methods a call could mean: ``class A(object)``, ``Generic[T]``, ``ABC``.
_TRANSPARENT = frozenset({"object", "ABC", "Generic", "Protocol", "typing.Generic",
                          "typing.Protocol", "abc.ABC"})
#: A base outside the repository (a library class, an unresolved name) in a resolution order.
_LIBRARY = ("", "")


def from_imports(root: Node, source: bytes, rel: str, repo_root: Path
                 ) -> tuple[dict[str, tuple[str, str]], tuple[str, ...]]:
    """This file's ``from m import a, b as c`` → ``{a: (m's file, a), c: (m's file, b)}``, and
    the files of its ``from m import *``. In-repo modules only."""
    named: dict[str, tuple[str, str]] = {}
    stars: list[str] = []
    roots = _Roots(rel, repo_root)
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type != "import_from_statement":
            # an import is a statement: only statements and the blocks holding them are read,
            # never an expression's subtree (the bulk of a file)
            stack.extend(c for c in node.named_children if c.type in _HOLDS_STATEMENTS
                         or c.type.endswith(("_statement", "_clause", "_definition")))
            continue
        module_node = node.child_by_field_name("module_name")
        if module_node is None:
            continue
        level, module = 0, ""
        if module_node.type == "relative_import":
            for c in module_node.named_children:
                if c.type == "import_prefix":
                    level = node_text(c, source).count(".")
                elif c.type == "dotted_name":
                    module = _dotted(c, source)
        else:
            module = _dotted(module_node, source)
        resolved = _resolve(module, level, rel, repo_root, roots)
        if resolved is None:
            continue
        for item in node.named_children:
            if item is module_node:
                continue
            if item.type == "wildcard_import":
                stars.append(resolved)
            elif item.type == "dotted_name":
                name = node_text(item, source).split(".")[-1]
                named[name] = (resolved, name)
            elif item.type == "aliased_import":
                orig = item.child_by_field_name("name")
                alias = item.child_by_field_name("alias")
                if orig is not None and alias is not None:
                    named[node_text(alias, source)] = \
                        (resolved, node_text(orig, source).split(".")[-1])
    return named, tuple(stars)


def classes_of(root: Node, source: bytes) -> tuple[ClassFacts, ...]:
    """This file's classes, at any depth: name, bases as written (``Base``, ``mod.Base``;
    ``Generic[T]`` → ``Generic``; keyword arguments such as ``metaclass=`` left out) and the
    methods its body declares (decorated ones included)."""
    out: list[ClassFacts] = []
    stack = [root]
    while stack:
        n = stack.pop()
        stack.extend(c for c in n.named_children if c.type in _HOLDS_STATEMENTS
                     or c.type.endswith(("_statement", "_clause", "_definition")))
        if n.type != "class_definition":
            continue
        sup = n.child_by_field_name("superclasses")
        bases = tuple(node_text(a, source).split("[", 1)[0]
                      for a in (sup.named_children if sup is not None else [])
                      if a.type in ("identifier", "attribute", "subscript"))
        methods: set[str] = set()
        body = n.child_by_field_name("body")
        for c in body.named_children if body is not None else []:
            d = c.child_by_field_name("definition") if c.type == "decorated_definition" else c
            if d is not None and d.type == "function_definition":
                methods.add(node_text(d.child_by_field_name("name"), source))
        out.append((node_text(n.child_by_field_name("name"), source), bases, frozenset(methods)))
    return tuple(out)


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None
             ) -> tuple[str, FileFacts] | None:
    if repo_root is None:
        return None
    named, stars = from_imports(root, source, rel, repo_root)
    return rel, (frozenset(defined_names(root, source)), named, stars, classes_of(root, source))


def _resolve_fact(values: list[tuple[str, FileFacts]], index: Any) -> dict[str, FileFacts]:
    return dict(values)


register_fact(FactSpec(name=FACT, language="python", gate=(b"",),
                       collect=_collect, resolve=_resolve_fact))


def _submodule(package_init: str, name: str, files: Mapping[str, FileFacts]) -> str | None:
    """``pkg/name.py`` or ``pkg/name/__init__.py`` beside ``pkg/__init__.py``."""
    if not package_init.endswith("__init__.py"):
        return None
    folder = package_init[: -len("__init__.py")]
    for candidate in (f"{folder}{name}.py", f"{folder}{name}/__init__.py"):
        if candidate in files:
            return candidate
    return None


def declaring(files: Mapping[str, FileFacts], file: str, name: str,
              depth: int = _DEPTH) -> tuple[str, bool] | None:
    """``(file, is_module)``: where ``name`` of module ``file`` is declared — the file itself,
    the file a ``from … import`` in it takes it from (followed), a unique ``import *`` source, or
    a submodule of a package (``is_module``). None when it cannot be followed."""
    facts = files.get(file)
    if facts is None or depth == 0:
        return None
    names, named, stars = facts[0], facts[1], facts[2]
    if name in names:
        return file, False
    if name in named:
        src, orig = named[name]
        return declaring(files, src, orig, depth - 1)
    hits = {d for star in stars if (d := declaring(files, star, name, depth - 1)) is not None}
    if len(hits) == 1:
        return next(iter(hits))
    if hits:
        return None
    sub = _submodule(file, name, files)
    return (sub, True) if sub is not None else None


def follow_bindings(bindings: Mapping[str, str], root: Node, source: bytes, rel: str,
                    repo_root: Path, index: Any | None) -> tuple[dict[str, str], frozenset[str]]:
    """(``bindings`` with each ``from … import`` name moved to its declaring file, the local
    names bound to a whole module). ``import m`` names are modules already; a name that cannot
    be followed keeps its file."""
    files: Mapping[str, FileFacts] = index_fact(index, FACT) or {}
    out = dict(bindings)
    # this file's `from … import` bindings were read once, for the fact
    facts = files.get(rel)
    named = facts[1] if facts is not None else (
        from_imports(root, source, rel, repo_root)[0] if files else {})
    modules = {local for local in bindings if local not in named}  # `import m` / `import a.b`
    for local, (src, orig) in named.items():
        found = declaring(files, src, orig)
        if found is not None:
            out[local] = found[0]
            if found[1]:
                modules.add(local)
    return out, frozenset(modules)


def _class(files: Mapping[str, FileFacts], file: str, name: str) -> ClassFacts | None:
    """The class ``name`` declared once in ``file``."""
    facts = files.get(file)
    hits = [c for c in facts[3] if c[0] == name] if facts is not None else []
    return hits[0] if len(hits) == 1 else None


def class_of(files: Mapping[str, FileFacts], file: str, written: str
             ) -> tuple[str, str] | None:
    """``(file, class)`` a class name written in ``file`` means: a class of the file, a
    ``from … import`` followed to the file declaring it, or ``module.Class`` on a module imported
    that way. None when it is no class of the repository (a library class, unresolved)."""
    head, _, rest = written.partition(".")
    if not rest:
        if _class(files, file, written) is not None:
            return file, written
        named = files[file][1] if file in files else {}
        if written in named:
            hit = declaring(files, *named[written])
            orig = named[written][1]
            if hit is not None and not hit[1] and _class(files, hit[0], orig) is not None:
                return hit[0], orig
        return None
    named = files[file][1] if file in files else {}
    if head in named and "." not in rest:
        hit = declaring(files, *named[head])
        if hit is not None and hit[1] and _class(files, hit[0], rest) is not None:
            return hit[0], rest
    return None


def _mro(files: Mapping[str, FileFacts], key: tuple[str, str],
         cache: dict[tuple[str, str], list[tuple[str, str]] | None],
         active: frozenset[tuple[str, str]] = frozenset()) -> list[tuple[str, str]] | None:
    """Python's method resolution order (C3) of class ``key``; a base outside the repository is
    :data:`_LIBRARY` (it may declare any method). None when C3 fails or a cycle."""
    if key in cache:
        return cache[key]
    if key in active:
        return None
    cls = _class(files, *key)
    if cls is None:
        cache[key] = None
        return None
    seqs: list[list[tuple[str, str]]] = []
    direct: list[tuple[str, str]] = []
    for written in cls[1]:
        if written in _TRANSPARENT:
            continue
        base = class_of(files, key[0], written)
        if base is None:
            seqs.append([_LIBRARY]); direct.append(_LIBRARY)
            continue
        sub = _mro(files, base, cache, active | {key})
        if sub is None:
            cache[key] = None
            return None
        seqs.append(list(sub)); direct.append(base)
    seqs.append(direct)
    out = [key]
    while any(seqs):
        seqs = [q for q in seqs if q]
        head = next((q[0] for q in seqs
                     if q[0] == _LIBRARY or not any(q[0] in r[1:] for r in seqs)), None)
        if head is None:
            cache[key] = None  # no consistent order: Python would refuse the class too
            return None
        out.append(head)
        seqs = [q[1:] if q[0] == head else q for q in seqs]
    cache[key] = out
    return out


def method_file(files: Mapping[str, FileFacts], key: tuple[str, str], name: str, skip: int,
                cache: dict[tuple[str, str], list[tuple[str, str]] | None]) -> str | None:
    """The file declaring ``name`` for an instance of class ``key``: the first class of its
    resolution order (after ``skip`` of them — 1 for ``super()``) that declares it. None when
    none does, or a library base comes first (it may declare it)."""
    mro = _mro(files, key, cache)
    for cls in (mro or [])[skip:]:
        if cls == _LIBRARY:
            return None
        facts = _class(files, *cls)
        if facts is not None and name in facts[2]:
            return cls[0]
    return None


def checked_resolver(base: CallResolver, *, path: str, bindings: Mapping[str, str],
                     modules: frozenset[str], index: Any | None) -> CallResolver:
    """``base`` (given the followed ``bindings``), keeping a path only when that file declares
    what is called. ``m.fn()`` on a module ``m`` follows ``fn`` through ``m``'s re-exports."""
    files: Mapping[str, FileFacts] = index_fact(index, FACT) or {}
    if not files:
        return base

    def declares(file: str, name: str) -> bool:
        facts = files.get(file)
        return facts is not None and name in facts[0]

    orders: dict[tuple[str, str], list[tuple[str, str]] | None] = {}

    def resolve(name: str, receiver: str | None, owner: str | None = None,
                *args: Any, **kwargs: Any) -> str | None:
        # A method call on the class itself, its parent (`super()`) or a repository class:
        # Python's resolution order over the repository's classes decides (a library base
        # first → no path).
        if receiver is not None and owner is not None \
                and (receiver in ("self", "cls") or receiver.startswith("super(")) \
                and _class(files, path, owner) is not None:
            return method_file(files, (path, owner), name,
                               1 if receiver.startswith("super(") else 0, orders)
        if receiver is not None and receiver not in modules and receiver[:1].isupper() \
                and receiver.replace(".", "").isidentifier():
            key = class_of(files, path, receiver)
            if key is not None:
                return method_file(files, key, name, 0, orders)
        found = base(name, receiver, owner, *args, **kwargs)
        if found is None or found == path:
            return found
        if receiver is not None and receiver in modules:  # `m.fn()`: the module's `fn`
            hit = declaring(files, found, name)
            return hit[0] if hit is not None and not hit[1] else None
        return found if declares(found, name) else None

    return resolve
