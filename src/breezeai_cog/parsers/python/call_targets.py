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

#: One file: (its def / class names, ``from`` imports: local name → (file, imported name),
#: files it star-imports).
FileFacts = tuple[frozenset[str], dict[str, tuple[str, str]], tuple[str, ...]]


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


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None
             ) -> tuple[str, FileFacts] | None:
    if repo_root is None:
        return None
    named, stars = from_imports(root, source, rel, repo_root)
    return rel, (frozenset(defined_names(root, source)), named, stars)


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
    names, named, stars = facts
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

    def resolve(name: str, receiver: str | None, owner: str | None = None,
                *args: Any, **kwargs: Any) -> str | None:
        found = base(name, receiver, owner, *args, **kwargs)
        if found is None or found == path:
            return found
        if receiver is not None and receiver in modules:  # `m.fn()`: the module's `fn`
            hit = declaring(files, found, name)
            return hit[0] if hit is not None and not hit[1] else None
        return found if declares(found, name) else None

    return resolve
