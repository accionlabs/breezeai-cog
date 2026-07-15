"""Shared call-target resolver (``calls[].path`` — drives the CALLS edge).

Precision-first: resolve only when unambiguous, else return ``None`` (spec: null for
external/unresolved; the backend silently skips those edges). Tiers, tried in order —
first hit wins, a tie resolves to ``None``, never a guessed edge:

* **Tier 1 — import binding:** a call to an imported symbol → the module's in-repo file
  (``foo()`` where ``foo`` was imported; ``Foo.bar()`` where ``Foo`` was imported).
* **Tier 2 — same file:** a call to a function/method/class defined in the current file
  (bare ``foo()`` or ``self``/``this`` method call) → the current file.
* **Phase 2 — receiver type:** ``obj.method()`` where ``obj``'s declared type resolves to
  an in-repo file → that file.
* **Extension:** ``recv.M()`` where ``recv``'s type is not an in-repo member call and ``M``
  is an in-repo extension method on that type (``static M(this T …)``) → the defining file,
  via ``ext_index[(M, T)]`` (simple type names). Only fires when Phase 2 found nothing, so
  it never overrides an existing edge.
* **Inheritance:** ``this.M()`` / ``self.M()`` where ``M`` is declared on an in-repo base
  class → the nearest ancestor (up the enclosing ``owner``'s heritage chain) that declares
  ``M``, resolved to that ancestor's file.

The Extension and Inheritance tiers are inert unless ``ext_index`` / ``heritage`` are
supplied (and, for inheritance, the call passes the ``owner`` class), so callers that omit
them behave exactly as the base tiers.
"""

from __future__ import annotations

from typing import Callable

from .index_common import ClassHeritage, walk_heritage

CallResolver = Callable[..., str | None]


def _base_type(t: str | None) -> str | None:
    """Simple type name: strip generics/array/qualifier — ``a.b.Foo<Bar>[]`` → ``Foo``."""
    if not t:
        return None
    t = t.split("<", 1)[0].strip().rstrip("[]").strip()
    return t.rsplit(".", 1)[-1] or None


def make_resolver(
    bindings: dict[str, str],
    local_defs: set[str],
    path: str,
    types: dict[str, str] | None = None,
    *,
    ext_index: dict[tuple[str, str], str | None] | None = None,
    heritage: dict[str, ClassHeritage | None] | None = None,
) -> CallResolver:
    """``(name, receiver, owner=None) -> repo path | None``. ``receiver`` is the callee's
    qualifier (``None`` for a bare call, ``"self"``/``"this"`` for own methods, or an
    expression); ``owner`` is the simple name of the class the call is written in (needed
    only by the inheritance tier).

    See the module docstring for the tier order. ``ext_index`` maps ``(method, this-param
    type)`` → defining file (``None`` = ambiguous, honest-null); ``heritage`` is the
    repo ``class_heritage`` index walked for inherited-method resolution."""
    types = types or {}

    def _inherited(name: str, owner: str) -> str | None:
        """Nearest in-repo ancestor of ``owner`` that declares ``name`` → its file."""
        if not heritage:
            return None
        start = heritage.get(owner)
        if start is None:  # owner unknown or ambiguous — do not resolve through it
            return None
        for anc in walk_heritage(owner, start.extends, heritage).ancestors:
            if name in anc.methods:  # nearest ancestor declaring it wins (matches C# binding)
                return anc.methods[name]  # its file, or None when that name is file-ambiguous
        return None

    def resolve(name: str, receiver: str | None, owner: str | None = None) -> str | None:
        if receiver in ("self", "this"):
            if name in local_defs:  # same-file method
                return path
            if owner is not None:  # inherited base-class method
                return _inherited(name, owner)
            return None
        if receiver is None:
            if name in bindings:  # imported function
                return bindings[name]
            if name in local_defs:  # same-file function/class
                return path
            return None
        if receiver in bindings:  # `Imported.method()` → Imported's file
            return bindings[receiver]
        # Phase 2: `repo.method()` / `this.repo.method()` → type of repo → its file
        var = receiver[len("this."):] if receiver.startswith("this.") else receiver
        if var and "." not in var:
            typ = _base_type(types.get(var))
            if typ:
                resolved = bindings.get(typ) or (path if typ in local_defs else None)
                if resolved is not None:
                    return resolved
                # Extension: `M` is an in-repo extension method on `typ`
                if ext_index is not None:
                    return ext_index.get((name, typ))
        return None

    return resolve


def noop_resolver(name: str, receiver: str | None, owner: str | None = None) -> None:
    return None  # default when unresolved
