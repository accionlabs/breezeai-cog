"""Guards of Angular lazy mounts as a registered fact, ``angular.mount-guards``.

A ``loadChildren`` route is wiring: it has no record of its own, but its guards run before every
route of the module or route file it loads, which usually lives in another file::

    // app.routes.ts
    { path: 'admin', canActivate: [authGuard], loadChildren: () => import('./admin/admin.routes') }
    // admin/admin.routes.ts
    export default [ { path: 'users', component: UserList } ];   // → guards [authGuard]

While the TypeScript index is built, ``_collect`` reads each file's lazy mounts with the guards
that apply to the loaded routes — the guards the mount route inherits from its ancestors, its own
``canActivate`` / ``canMatch``, and its ``canActivateChild`` (the detector's own rule). The target
is a module symbol (``.then(m => m.AdminModule)``, ``'path#AdminModule'``) or, for a default-export
import, the loaded file. ``_resolve`` composes nested lazy mounts (a loaded module that itself
mounts another) and keeps one guard list per target: a target mounted twice with different guards
gets none (only guards that certainly apply). The detector reads it with :func:`incoming_guards`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..treesitter import node_text
from ..typescript.imports import _try_paths

FACT = "angular.mount-guards"

#: A lazy mount's target: ``("symbol", "AdminModule")`` or ``("file", "src/app/admin/x.ts")``.
Target = tuple[str, str]
_FileMounts = tuple[list[str], str, list[tuple[Target, tuple[str, ...]]]]


def incoming_guards(index: Any | None, path: str, classes: list[str]) -> list[str]:
    """Guards a lazy mount elsewhere puts in front of this file's routes (``[]`` if none)."""
    fact: dict[Target, tuple[str, ...]] = index_fact(index, FACT) or {}
    for key in [("file", path), *(("symbol", c) for c in classes)]:
        if key in fact:
            return list(fact[key])
    return []


def _target(load: Node, source: bytes, rel: str, repo_root: Path | None) -> Target | None:
    from .routes import _LAZY_IMPORT_RE, _LAZY_MEMBER_RE, _LAZY_STRING_RE

    text = node_text(load, source)
    m = _LAZY_STRING_RE.search(text) or _LAZY_MEMBER_RE.search(text)
    if m:
        return ("symbol", m.group(1))
    m = _LAZY_IMPORT_RE.search(text)
    if m is None or repo_root is None or not m.group(1).startswith("."):
        return None
    resolved = _try_paths((repo_root / rel).parent / m.group(1), repo_root)
    return ("file", resolved) if resolved is not None else None


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> _FileMounts | None:
    from .routes import declared_classes, lazy_mount_guards

    mounts = [(target, tuple(guards))
              for load, guards in lazy_mount_guards(root, source)
              if (target := _target(load, source, rel, repo_root)) is not None]
    if not mounts:
        return None
    return declared_classes(root, source), rel, mounts


def _resolve(values: list[_FileMounts], index: object) -> dict[Target, tuple[str, ...]] | None:
    own: dict[Target, tuple[str, ...] | None] = {}
    parent: dict[Target, list[Target] | None] = {}  # target → the keys of the file mounting it
    for classes, rel, mounts in values:
        here = [("file", rel), *(("symbol", c) for c in classes)]
        for target, guards in mounts:
            record_distinct(own, target, guards)
            record_distinct(parent, target, here)
    out: dict[Target, tuple[str, ...]] = {}

    def resolve(target: Target, seen: frozenset[Target]) -> tuple[str, ...] | None:
        if target in out:
            return out[target]
        guards = own.get(target)
        if guards is None or target in seen:  # ambiguous, or a mount cycle
            return None
        outer: tuple[str, ...] = ()
        for key in parent.get(target) or []:
            if key in own:  # the mounting file is itself lazily mounted
                found = resolve(key, seen | {target})
                if found is None:
                    return None
                outer = found
                break
        out[target] = outer + tuple(g for g in guards if g not in outer)
        return out[target]

    for target in sorted(own):
        resolve(target, frozenset())
    result = {k: v for k, v in out.items() if v}
    return result or None


register_fact(FactSpec(
    name=FACT, language="typescript", gate=(b"loadChildren",),
    collect=_collect, resolve=_resolve,
))
