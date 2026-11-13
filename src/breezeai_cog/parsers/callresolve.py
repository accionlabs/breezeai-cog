"""Shared call-target resolver (``calls[].path`` — drives the CALLS edge).

Precision-first: resolve only when unambiguous, else return ``None`` (spec: null for
external/unresolved; the backend silently skips those edges). Tiers, tried in order —
first hit wins, a tie resolves to ``None``, never a guessed edge:

* **Tier 1 — import binding:** a call to an imported symbol → the module's in-repo file
  (``foo()`` where ``foo`` was imported; ``Foo.bar()`` where ``Foo`` was imported).
* **Tier 2 — same file:** a call to a function/method/class defined in the current file
  (bare ``foo()`` or ``self``/``this`` method call) → the current file.
* **Phase 2 — receiver type:** ``obj.method()`` where ``obj``'s declared type resolves to
  an in-repo file → that file. With ``declared_only`` (and ``heritage``), only when that type
  declares ``method`` — else the nearest in-repo ancestor that does, else ``None``: a method
  the type inherits from a library, or a variable whose type differs in another method of the
  file, has no in-repo target.
* **Extension:** ``recv.M()`` where ``recv``'s type is not an in-repo member call and ``M``
  is an in-repo extension method on that type (``static M(this T …)``) → the defining file,
  via ``ext_index[(M, T)]`` (simple type names). Only fires when Phase 2 found nothing, so
  it never overrides an existing edge.
* **Inheritance:** ``this.M()`` / ``self.M()`` (own class doesn't declare ``M``) or an explicit
  base call ``super.M()`` / ``base.M()`` / ``MyBase.M()`` → the nearest ancestor (up the
  enclosing ``owner``'s heritage chain) that declares ``M``, resolved to that ancestor's file.

* **Call receiver** (with ``declared_only``): ``x.m().n()`` / ``m().n()`` / ``Type.m().n()``
  → ``m``'s declared return type (``heritage`` ``returns``, from ``x``'s type, the enclosing
  class or ``Type``, ancestors included) → the file that declares ``n`` on that type. One call
  deep; a return type two repository classes share a name with → ``None``.

The Extension and Inheritance tiers are inert unless ``ext_index`` / ``heritage`` are
supplied (and, for inheritance, the call passes the ``owner`` class), so callers that omit
them behave exactly as the base tiers.
"""

from __future__ import annotations

import re
from typing import Callable

from .index_common import ClassHeritage, walk_heritage

CallResolver = Callable[..., str | None]

#: A receiver that is one call: ``m(…)``, ``x.m(…)``, ``this.m(…)``, ``Type.m(…)``.
_CALL_RECEIVER = re.compile(r"(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s*\(")


def _one_call(receiver: str) -> tuple[str | None, str] | None:
    """``(qualifier, method)`` when ``receiver`` is exactly one call (its argument list closes at
    the end), else None: ``builder.dataApi()`` → ``("builder", "dataApi")``."""
    m = _CALL_RECEIVER.fullmatch(receiver[: receiver.find("(") + 1]) if "(" in receiver else None
    if m is None or not receiver.endswith(")"):
        return None
    depth = 0
    for i in range(m.end() - 1, len(receiver)):
        ch = receiver[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i != len(receiver) - 1:
                return None  # `a.m().n()` — more than one call
    return (m.group(1), m.group(2)) if depth == 0 else None


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
    builtin_methods: frozenset[str] = frozenset(),
    declared_only: bool = False,
    owner_defs: dict[str, set[str]] | None = None,
) -> CallResolver:
    """``(name, receiver, owner=None) -> repo path | None``. ``receiver`` is the callee's
    qualifier (``None`` for a bare call, ``"self"``/``"this"`` for own methods, or an
    expression); ``owner`` is the simple name of the class the call is written in (needed
    only by the inheritance tier).

    See the module docstring for the tier order. ``ext_index`` maps ``(method, this-param
    type)`` → defining file (``None`` = ambiguous, honest-null); ``heritage`` is the
    repo ``class_heritage`` index walked for inherited-method resolution. ``declared_only``
    makes the receiver-type tier check ``heritage``: the method must be declared by the type or
    an in-repo ancestor (its file), else no edge. ``owner_defs`` (class simple name → the
    methods a bare / ``this`` call written in it can mean in this file) narrows the same-file
    tier to the calling class, so a same-named method of another class in the file (a nested
    or anonymous one) does not shadow an inherited method; without an ``owner`` (or for an
    unknown one) the whole file's ``local_defs`` apply."""
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

    def _lookup(typ: str, name: str, table: str) -> tuple[bool, str | None]:
        """``(found, value)`` for ``name`` in ``typ``'s ``methods`` / ``returns``, else in its
        nearest in-repo ancestor's."""
        assert heritage is not None
        own = heritage.get(typ)
        if own is None:
            return False, None
        for cls in (own, *walk_heritage(typ, own.extends, heritage).ancestors):
            entries = getattr(cls, table)
            if name in entries:
                return True, entries[name]
        return False, None

    def _declared(name: str, typ: str, resolved: str) -> str | None:
        """The file declaring ``typ.name``: ``typ`` itself, else its nearest in-repo ancestor.
        A type the index cannot tell apart (two classes, one simple name) is left as resolved."""
        if heritage is None or heritage.get(typ) is None:
            return resolved
        return _lookup(typ, name, "methods")[1]

    def _via_call(name: str, receiver: str, owner: str | None) -> str | None:
        """``x.m().name()``: ``name`` on ``m``'s return type → the file that declares it."""
        call = _one_call(receiver)
        if call is None or heritage is None:
            return None
        qual, method = call
        if qual is None or qual == "this":
            cls = owner
        else:
            cls = _base_type(types.get(qual)) or (qual if qual in heritage else None)
        if cls is None:
            return None
        ret = _lookup(cls, method, "returns")[1]
        return _lookup(ret, name, "methods")[1] if ret else None

    def _own(owner: str | None) -> set[str]:
        if owner_defs is not None and owner is not None and owner in owner_defs:
            return owner_defs[owner]
        return local_defs

    def resolve(name: str, receiver: str | None, owner: str | None = None) -> str | None:
        if receiver in ("self", "this"):
            if name in _own(owner):  # same-file method
                return path
            if owner is not None:  # inherited base-class method
                return _inherited(name, owner)
            return None
        if receiver in ("super", "base", "MyBase"):  # explicit base call → an ancestor's method
            return _inherited(name, owner) if owner is not None else None
        if receiver is None:
            if name in bindings:  # imported function
                return bindings[name]
            if name in _own(owner):  # same-file function/class
                return path
            return None
        if receiver in bindings:  # `Imported.method()` → Imported's file
            return bindings[receiver]
        # A built-in prototype method (`Array.map`, `String.split`, …) is never declared in the
        # receiver type's own file — resolving it there fabricates a wrong CALLS edge (e.g.
        # `groups.map()` → the type-only module that declares `groups`' type). Leave it
        # unresolved (honest-null). Empty by default, so non-JS languages are unaffected.
        if name in builtin_methods:
            return None
        if declared_only and receiver.endswith(")"):  # `builder.dataApi().get()`
            return _via_call(name, receiver, owner)
        # Phase 2: `repo.method()` / `this.repo.method()` → type of repo → its file
        var = receiver[len("this."):] if receiver.startswith("this.") else receiver
        if var and "." not in var:
            typ = _base_type(types.get(var))
            if typ:
                resolved = bindings.get(typ) or (path if typ in local_defs else None)
                if resolved is not None:
                    return _declared(name, typ, resolved) if declared_only else resolved
                # Extension: `M` is an in-repo extension method on `typ`
                if ext_index is not None:
                    return ext_index.get((name, typ))
        return None

    return resolve


def noop_resolver(
    name: str, receiver: str | None, owner: str | None = None,
    types: dict[str, str | None] | None = None,
) -> None:
    return None  # default when unresolved
