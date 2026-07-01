"""Shared call-target resolver (``calls[].path``, spec C4.2 / C6 — drives the CALLS edge).

Precision-first: resolve only when unambiguous, else return ``None`` (spec: null for
external/unresolved; the backend silently skips those edges). Two tiers here (Phase 1):

* **Tier 1 — import binding:** a call to an imported symbol → the module's in-repo file
  (``foo()`` where ``foo`` was imported; ``Foo.bar()`` where ``Foo`` was imported).
* **Tier 2 — same file:** a call to a function/method/class defined in the current file
  (bare ``foo()`` or ``self``/``this`` method call) → the current file.
"""

from __future__ import annotations

from typing import Callable

CallResolver = Callable[[str, str | None], str | None]


def _base_type(t: str | None) -> str | None:
    """Simple type name: strip generics/array/qualifier — ``a.b.Foo<Bar>[]`` → ``Foo``."""
    if not t:
        return None
    t = t.split("<", 1)[0].strip().rstrip("[]").strip()
    return t.rsplit(".", 1)[-1] or None


def make_resolver(
    bindings: dict[str, str], local_defs: set[str], path: str, types: dict[str, str] | None = None
) -> CallResolver:
    """``(name, receiver) -> repo path | None``. ``receiver`` is the callee's qualifier
    (``None`` for a bare call, ``"self"``/``"this"`` for own methods, or an expression).

    Tier 1 = imported symbol, Tier 2 = same file, **Phase 2** = ``obj.method()`` where
    ``obj``'s type (from ``types``: field/param/local → type name) resolves to a file."""
    types = types or {}

    def resolve(name: str, receiver: str | None) -> str | None:
        if receiver in ("self", "this"):
            return path if name in local_defs else None  # same-file method
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
                return bindings.get(typ) or (path if typ in local_defs else None)
        return None

    return resolve


def noop_resolver(name: str, receiver: str | None) -> None:  # default when unresolved
    return None
