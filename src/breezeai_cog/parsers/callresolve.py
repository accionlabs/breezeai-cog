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
  the type inherits from a library has no in-repo target. A type ``heritage`` cannot describe
  (a simple name two classes share, or a class whose methods are not all known) keeps the
  type's file only when the class of that name in that file declares ``method``
  (``unknown_methods``). A caller that passes the calling
  method's own variable types (:func:`method_types`) has ``obj`` looked up there, so a
  same-named variable of another method, or a field a local hides, does not lend its type.
* **Extension:** ``recv.M()`` where ``recv``'s type is not an in-repo member call and ``M``
  is an in-repo extension method on that type (``static M(this T …)``) → the defining file,
  via ``ext_index[(M, T)]`` (simple type names). Only fires when Phase 2 found nothing, so
  it never overrides an existing edge.
* **Inheritance:** ``this.M()`` / ``self.M()`` (own class doesn't declare ``M``) or an explicit
  base call ``super.M()`` / ``base.M()`` / ``MyBase.M()`` → the nearest ancestor (up the
  enclosing ``owner``'s heritage chain) that declares ``M``, resolved to that ancestor's file.

* **Inherited field** (with ``declared_only`` and ``inherited_fields``, Java only): ``x.m()`` / ``this.x.m()`` where the calling
  method does not declare ``x`` and the calling class or its nearest in-repo ancestor declares a
  field ``x`` (``heritage`` ``fields``) with a repository class type → the file that declares
  ``m`` on that type. The first class up the chain that declares ``x`` decides; when it gives
  ``x`` a library type the tier adds nothing and the receiver-type tier runs as before. Inside an anonymous class body the caller passes ``field_owner`` (the
  enclosing class) and ``through`` (each enclosing anonymous class's type, innermost first):
  only types in ``fieldless_types`` that are no repository class let the lookup continue, since
  any other type may declare a field of that name itself.

* **Nested type** (with ``nested_types``, Java only): ``x.m()`` where ``x`` is declared
  ``Outer.Inner`` → ``Inner`` as declared in ``Outer``'s file (this file, an import or the same
  package), and only when that class or an in-repo ancestor declares ``m``. ``Outer`` outside the
  repository gives ``None``: a class of this file that shares ``Inner``'s simple name is never it.

* **Call receiver** (with ``declared_only``): ``x.m().n()`` / ``m().n()`` / ``Type.m().n()``
  → ``m``'s declared return type (``heritage`` ``returns``, from ``x``'s type, the enclosing
  class or ``Type``, ancestors included) → the file that declares ``n`` on that type. A longer
  chain (``x.a().b().n()``) goes through each return type in turn; the first link with no
  known return type stops it (``None``). A chain may start at an object built or cast in place
  (``new T(…).n()``, ``((T) x).n()``): its type is ``T``. A return type two repository classes
  share a name with → ``None``.

The Extension and Inheritance tiers are inert unless ``ext_index`` / ``heritage`` are
supplied (and, for inheritance, the call passes the ``owner`` class), so callers that omit
them behave exactly as the base tiers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Callable

from .index_common import ClassHeritage, record_distinct, walk_heritage

CallResolver = Callable[..., str | None]

#: A receiver that is one call: ``m(…)``, ``x.m(…)``, ``this.m(…)``, ``Type.m(…)``.
_CALL_RECEIVER = re.compile(r"(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s*\(")


#: The head of a chain built in place: ``new T(`` (``T`` may be qualified or generic).
_NEW_HEAD = re.compile(r"new\s+([A-Za-z_][\w.]*)\s*(?:<[^()]*>)?\s*\(")
#: A cast of a name: ``(T) x`` / ``(T<U>) this.x``, inside its own parentheses.
_CAST = re.compile(r"\(\s*([A-Za-z_][\w.]*)\s*(?:<[^()]*>)?\s*\)\s*[A-Za-z_][\w.]*")
#: A link of a chain: ``.m(``.
_LINK = re.compile(r"\.\s*([A-Za-z_]\w*)\s*\(")


def _close(text: str, start: int) -> int:
    """The index of the ``)`` closing the ``(`` at ``start``, else -1 (string literals with a
    bracket in them are not told apart: such a chain is then not read)."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _call_chain(receiver: str) -> tuple[str | None, str | None, list[str]] | None:
    """A receiver that is a chain of calls, as ``(qualifier, head type, methods)``, else None:
    ``builder.a().b()`` → ``("builder", None, ["a", "b"])``; ``new T(x).a()`` →
    ``(None, "T", ["a"])``; ``((T) o).a()`` → ``(None, "T", ["a"])``; ``new T()`` →
    ``(None, "T", [])``."""
    text = receiver.strip()
    qual: str | None = None
    head: str | None = None
    methods: list[str] = []
    if (m := _NEW_HEAD.match(text)) is not None:
        head = m.group(1).rsplit(".", 1)[-1]
        end = _close(text, m.end() - 1)
    elif text.startswith("(("):
        end = _close(text, 0)
        cast = _CAST.fullmatch(text[1:end]) if end > 0 else None
        if cast is None:
            return None
        head = cast.group(1).rsplit(".", 1)[-1]
    elif (m := _CALL_RECEIVER.match(text)) is not None:
        qual, methods = m.group(1), [m.group(2)]
        end = _close(text, m.end() - 1)
    else:
        return None
    while end != -1 and end < len(text) - 1:
        link = _LINK.match(text, end + 1)
        if link is None:
            return None
        methods.append(link.group(1))
        end = _close(text, link.end() - 1)
    return (qual, head, methods) if end != -1 else None


def method_types(
    fields: Mapping[str, str | None],
    params: list[tuple[str, str | None]],
    locals_: list[tuple[str, str | None]],
) -> dict[str, str | None]:
    """The variable types one method's calls resolve with: name → written type, or ``None``
    when the name is known but its type is not (untyped, or declared two ways) — the resolver
    then leaves the call unresolved instead of using a type the name does not have here.

    ``fields`` are the enclosing class's (and its outer classes') fields, ``params`` the
    method's own parameters, ``locals_`` every other name declared in its body (locals, loop
    and catch variables, lambda and closure parameters), in source order. A parameter hides
    a field for the whole method; a local whose type differs from the field it shares a name
    with is ambiguous, since calls before its declaration still mean the field. Each field is
    also kept under ``this.<name>``: ``this.repo`` always means the field."""
    out: dict[str, str | None] = {f"this.{n}": t for n, t in fields.items()}
    out.update(fields)
    for name, typ in params:
        out[name] = typ
    declared: dict[str, str | None] = {}
    for name, typ in locals_:
        record_distinct(declared, name, typ)  # two types, or an untyped one → None
    for name, typ in declared.items():
        out[name] = typ if name not in out or out[name] == typ else None
    return out


def _base_type(t: str | None) -> str | None:
    """Simple type name: strip generics/array/qualifier — ``a.b.Foo<Bar>[]`` → ``Foo``."""
    if not t:
        return None
    t = t.split("<", 1)[0].strip().rstrip("[]").strip()
    return t.rsplit(".", 1)[-1] or None


def _nested_name(t: str | None) -> tuple[str, str] | None:
    """``(Outer, Inner)`` for a type written through its enclosing class — ``Outer.Inner``,
    ``Outer.Mid.Inner<T>[]`` — else None: a simple name, or a package-qualified one
    (``com.acme.Foo``, first segment not capitalised)."""
    if not t:
        return None
    parts = t.split("<", 1)[0].strip().rstrip("[]").strip().split(".")
    if len(parts) < 2 or not all(parts) or not parts[0][:1].isupper():
        return None
    return parts[0], parts[-1]


def make_resolver(
    bindings: dict[str, str],
    local_defs: set[str],
    path: str,
    types: Mapping[str, str | None] | None = None,
    *,
    ext_index: dict[tuple[str, str], str | None] | None = None,
    heritage: Mapping[str, ClassHeritage | None] | None = None,
    builtin_methods: frozenset[str] = frozenset(),
    declared_only: bool = False,
    owner_defs: dict[str, set[str]] | None = None,
    unknown_methods: Mapping[tuple[str, str], frozenset[str] | None] | None = None,
    inherited_fields: bool = False,
    fieldless_types: frozenset[str] = frozenset(),
    nested_types: bool = False,
) -> CallResolver:
    """``(name, receiver, owner=None, scope=None) -> repo path | None``. ``receiver`` is the
    callee's qualifier (``None`` for a bare call, ``"self"``/``"this"`` for own methods, or an
    expression); ``owner`` is the simple name of the class the call is written in (needed
    only by the inheritance tier); ``scope`` is the calling method's own variable types
    (:func:`method_types`), used in place of the file-wide ``types`` when given.

    See the module docstring for the tier order. ``ext_index`` maps ``(method, this-param
    type)`` → defining file (``None`` = ambiguous, honest-null); ``heritage`` is the
    repo ``class_heritage`` index walked for inherited-method resolution. ``declared_only``
    makes the receiver-type and imported-name tiers check ``heritage``: the method must be declared by the type or
    an in-repo ancestor (its file), else no edge. ``owner_defs`` (class simple name → the
    methods a bare / ``this`` call written in it can mean in this file) narrows the same-file
    tier to the calling class, so a same-named method of another class in the file (a nested
    or anonymous one) does not shadow an inherited method; without an ``owner`` (or for an
    unknown one) the whole file's ``local_defs`` apply. ``unknown_methods`` (``(file, class
    simple name)`` → the methods that class declares there, for each class ``heritage`` maps to
    ``None``: :func:`~.index_common.methods_where_unknown`) lets such a type keep its file only
    for a method declared there; without it the type's file is kept for any method.
    ``inherited_fields`` turns on the inherited-field tier: a language whose name lookup the
    tier models (Java) opts in, so no other language's calls change with the ``fields`` its
    classes' heritage may carry (Groovy reads the Java classes' heritage too).
    ``fieldless_types`` (simple names of library interfaces known to declare no fields) lets the
    inherited-field tier see through an anonymous class of one of those types.
    ``nested_types`` reads a receiver type written ``Outer.Inner`` as that nested class (Java,
    where a capitalised first segment is a class; in C# or TypeScript it may be a namespace)."""
    types = types or {}

    def _type_of(key: str, scope: Mapping[str, str | None] | None) -> tuple[bool, str | None]:
        """``(known, type)`` of a variable: the calling method's own scope first; a name it does
        not declare (an inherited field) falls back to the file-wide ``types``."""
        if scope is not None and key in scope:
            return True, scope[key]
        bare = key[len("this."):] if key.startswith("this.") else key
        return (True, types[bare]) if bare in types else (False, None)

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

    def _field_type(var: str, owner: str) -> str | None:
        """The repository class type of field ``var`` as the nearest class declaring it (``owner``
        or an in-repo ancestor) declares it; None when no such class declares it, or the nearest
        one gives it a type that is no repository class (a farther ancestor's field of the same
        name is hidden by it, so it is never used)."""
        assert heritage is not None
        own = heritage.get(owner)
        if own is None:
            return None
        for cls in (own, *walk_heritage(owner, own.extends, heritage).ancestors):
            if var in cls.fields:
                return cls.fields[var]
        return None

    def _sees_outer_fields(through: tuple[str | None, ...]) -> bool:
        """Whether a name in an anonymous class body of these types can mean an outer field: only
        when every one is a known field-less library interface, not a repository class."""
        for t in through:
            simple = _base_type(t)
            if simple is None or simple not in fieldless_types or simple in bindings \
                    or (heritage is not None and simple in heritage):
                return False
        return True

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
        A type the index cannot describe (two classes, one simple name; methods not all known)
        keeps the resolved file when the class of that name there declares ``name``."""
        if heritage is None:
            return resolved
        if heritage.get(typ) is None:
            if unknown_methods is None:
                return resolved
            declared = unknown_methods.get((resolved, typ))
            return resolved if declared is not None and name in declared else None
        return _lookup(typ, name, "methods")[1]

    def _outer_file(outer: str) -> str | None:
        """The file declaring class ``outer`` as this file sees it: a class of this file (which
        hides an import of the same name), else an import or same-package binding. None for a
        library class, or a bound name that is no repository class (a static import)."""
        where = path if outer in local_defs else bindings.get(outer)
        if where is None or (heritage is not None and outer not in heritage):
            return None
        return where

    def _placed(inner: str, where: str) -> bool:
        """Whether the one repository class named ``inner`` is declared in ``where``: one of its
        own methods is. A class with no method of its own cannot be placed, so it is not."""
        own = heritage.get(inner) if heritage is not None else None
        return own is not None and where in own.methods.values()

    def _nested(name: str, outer: str, inner: str) -> str | None:
        """``x.name()`` with ``x`` declared ``Outer.Inner``: the class ``inner`` inside ``outer``'s
        file decides, never a class of the same simple name elsewhere (this file's own
        ``Inner``). The method must be declared there or by an in-repo ancestor."""
        where = _outer_file(outer)
        if where is None:
            return None
        if not declared_only or heritage is None:
            return where
        if heritage.get(inner) is None:  # two classes share the name: read the one in `where`
            declared = unknown_methods.get((where, inner)) if unknown_methods is not None else None
            return where if declared is not None and name in declared else None
        return _lookup(inner, name, "methods")[1] if _placed(inner, where) else None

    def _imported(name: str, receiver: str, resolved: str) -> str | None:
        """``Name.name()`` on an imported name: the file declaring ``name`` when ``Name`` is a
        repo class (a method the language generates, such as an enum's ``valueOf``, is declared
        nowhere). ``heritage`` names every repo class, so an imported name it lacks is a
        statically imported member (a constant): no file. A class the index cannot tell apart, or
        no index, keeps the import's file."""
        if not heritage:
            return resolved
        if receiver not in heritage:
            return None
        return _declared(name, receiver, resolved)

    def _via_call(
        name: str, receiver: str, owner: str | None, scope: Mapping[str, str | None] | None,
    ) -> str | None:
        """``x.a().b().name()``: ``name`` on the last call's return type, each link through
        the one before → the file that declares it. ``new T(…)`` / ``((T) x)`` at the head
        start the chain at ``T``."""
        chain = _call_chain(receiver)
        if chain is None or heritage is None:
            return None
        qual, head, methods = chain
        cls: str | None
        if head is not None:
            # as for a variable of that type: only a class this file's imports / package bind
            # to a repository file (a library class of the same simple name is not it)
            resolved = bindings.get(head) or (path if head in local_defs else None)
            if resolved is None:
                return None
            if not methods:
                return _declared(name, head, resolved)
            cls = head
        elif qual is None or qual == "this":
            cls = owner
        else:
            # a variable (even one of unknown type) hides a class of the same name
            known, typ = _type_of(qual, scope)
            nested = _nested_name(typ) if known and nested_types else None
            if nested is not None:  # `Outer.Inner` is the class inside Outer's file, or nothing
                where = _outer_file(nested[0])
                cls = nested[1] if where is not None and _placed(nested[1], where) else None
            else:
                cls = _base_type(typ) if known else (qual if qual in heritage else None)
        for method in methods:
            if cls is None:
                return None
            cls = _lookup(cls, method, "returns")[1]
        return _lookup(cls, name, "methods")[1] if cls else None

    def _own(owner: str | None) -> set[str]:
        if owner_defs is not None and owner is not None and owner in owner_defs:
            return owner_defs[owner]
        return local_defs

    def resolve(
        name: str, receiver: str | None, owner: str | None = None,
        scope: Mapping[str, str | None] | None = None,
        *, field_owner: str | None = None, through: tuple[str | None, ...] = (),
    ) -> str | None:
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
        # `Imported.method()` → Imported's file — unless a variable of the method has that name
        if receiver in bindings and (scope is None or receiver not in scope):
            if declared_only:  # … when it (or an in-repo ancestor) declares the method
                return _imported(name, receiver, bindings[receiver])
            return bindings[receiver]
        # A built-in prototype method (`Array.map`, `String.split`, …) is never declared in the
        # receiver type's own file — resolving it there fabricates a wrong CALLS edge (e.g.
        # `groups.map()` → the type-only module that declares `groups`' type). Leave it
        # unresolved (honest-null). Empty by default, so non-JS languages are unaffected.
        if name in builtin_methods:
            return None
        if declared_only and receiver.endswith(")"):  # `builder.dataApi().get()`
            return _via_call(name, receiver, owner, scope)
        # Phase 2: `repo.method()` / `this.repo.method()` → type of repo → its file. A method
        # scope keeps fields under `this.<name>` too; the file-wide map has bare names only.
        var = receiver[len("this."):] if receiver.startswith("this.") else receiver
        if var and "." not in var:
            # A name the method does not declare is a field it inherits: the class hierarchy
            # says which, before the file-wide map's guess (`this.x` in an anonymous class is
            # that class's own field, so only a bare name looks outward there).
            fowner = owner if owner is not None else (
                field_owner if not receiver.startswith("this.") and _sees_outer_fields(through)
                else None)
            # Only with the method's own scope: without it a local could hide the field unseen.
            if inherited_fields and declared_only and heritage and fowner is not None \
                    and scope is not None and receiver not in scope:
                ftype = _field_type(var, fowner)
                # A type name two repository classes share cannot be looked up by name: the
                # receiver-type tier below decides, as it does for any such type.
                if ftype is not None and heritage.get(ftype) is not None:
                    return _lookup(ftype, name, "methods")[1]
            written = _type_of(receiver, scope)[1]
            nested = _nested_name(written) if nested_types else None
            if nested is not None:  # `Outer.Inner x`: never this file's own `Inner`
                return _nested(name, *nested)
            typ = _base_type(written)
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
    types: dict[str, str | None] | None = None, **_: object,
) -> None:
    return None  # default when unresolved
