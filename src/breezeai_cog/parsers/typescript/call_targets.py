"""Which file a TypeScript / JavaScript call goes to — only a file that declares what is called.

``calls[].path`` drives the backend's CALLS edge, which links to a Function of that name in that
file. The shared resolver (``callresolve.py``) gives an imported name its import's file and a
typed receiver its type's file, unchecked. Three cases then name a file with no such function
(BREEZEAI-1653):

* **A re-export (barrel).** ``import { uploadFile } from '../infra'`` binds ``uploadFile`` to
  ``infra/index.ts``, which only re-exports it. → The binding follows the re-exports to the file
  that declares it (:func:`follow_reexports`, the ``typescript.declarations`` fact).
* **A member of an imported value.** ``CHAT_MODES.join(', ')`` on an imported array,
  ``db.transaction()`` on an imported Sequelize instance. → The import's file only when it
  declares a function of that name.
* **An inherited method.** ``this.documentService.getRepository()`` where ``DocumentService``
  extends ``BaseService``, which declares ``getRepository``. → The file of the class, or the
  nearest in-repo ancestor, that declares the method; none when no in-repo class declares it (a
  library base's method, ``find`` on a LoopBack repository).

**What a file declares** is the ``typescript.callables`` fact: every name the file binds to a
function or class, at any depth — a declaration, a method (class or object literal), a class
field or object property holding a function, an assignment of one (``exports.x =``,
``X.prototype.m =``), ``default`` for an anonymous default export. It is a superset of the
Functions the parser emits, so the check never drops a call to a function the file has; at worst
it keeps a path the parser has no Function for, as before.

Unchanged: same-file calls, ``this.m()`` / ``super.m()`` (already resolved through the class
heritage), calls with no path. Precision-first: when the declaring file cannot be proven, the
call keeps no path (missing, not wrong).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..callresolve import CallResolver
from ..index_common import ClassHeritage, record_distinct, walk_heritage
from ..treesitter import node_text

FACT = "typescript.callables"

#: A value that is a function.
_FN_VALUES = frozenset({"arrow_function", "function_expression", "function",
                        "generator_function"})
_FN_DECLS = frozenset({"function_declaration", "generator_function_declaration"})
_CLASSES = frozenset({"class_declaration", "abstract_class_declaration", "class"})
_FIELDS = frozenset({"public_field_definition", "field_definition"})
#: Other nodes whose own name is a function's: methods (class or object literal), signatures (an
#: interface's or an abstract class's — the parser emits Functions for them too), a named function
#: expression (its alias).
_NAMED = frozenset({"method_definition", "method_signature", "abstract_method_signature",
                    "function_signature", "function_expression", "generator_function",
                    "function"})


def _key(node: Node | None, source: bytes) -> str | None:
    """A property or method name as written: ``foo``, ``'foo'`` / ``"foo"`` → ``foo``; a
    computed name (``[x]``) → None."""
    if node is None or node.type == "computed_property_name":
        return None
    text = node_text(node, source)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"`":
        text = text[1:-1]
    return text or None


def _holds_function(value: Node | None) -> bool:
    """A function value. A call wrapping one (``memo(() => …)``, a zustand ``create((set) =>
    …)``) is not: the parser emits no Function under the name it is stored in; a named function
    inside it (``forwardRef(function Input …)``) is collected under its own name."""
    return value is not None and value.type in _FN_VALUES


def callable_names(root: Node, source: bytes) -> frozenset[str]:
    """Every name this file binds to a function or a class, at any depth (see the module
    docstring). One walk, iterative."""
    out: set[str] = set()
    stack = [root]
    while stack:
        n = stack.pop()
        kind = n.type
        if kind in _FN_DECLS or kind in _CLASSES or kind in _NAMED:
            if (name := _key(n.child_by_field_name("name"), source)) is not None:
                out.add(name)
        elif kind == "variable_declarator":
            nm = n.child_by_field_name("name")
            if nm is not None and nm.type == "identifier" \
                    and _holds_function(n.child_by_field_name("value")):
                out.add(node_text(nm, source))
        elif kind in _FIELDS:
            if _holds_function(n.child_by_field_name("value")) \
                    and (name := _key(n.child_by_field_name("name")
                                      or n.child_by_field_name("property"), source)):
                out.add(name)
        elif kind == "pair":
            if _holds_function(n.child_by_field_name("value")) \
                    and (name := _key(n.child_by_field_name("key"), source)):
                out.add(name)
        elif kind == "assignment_expression":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and _holds_function(right):
                target = node_text(left, source)
                out.add("default" if target == "module.exports" else target.rsplit(".", 1)[-1])
        elif kind == "export_statement":
            value = n.child_by_field_name("value")
            decl = n.child_by_field_name("declaration")
            if (value is not None and (_holds_function(value) or value.type in _CLASSES)) or (
                    decl is not None and b"default" in source[n.start_byte:decl.start_byte]):
                out.add("default")
        stack.extend(n.named_children)
    return frozenset(out)


def _classes(root: Node, source: bytes) -> list[str]:
    """The names of the classes declared in this file, at any depth (as the heritage index
    reads them)."""
    out: list[str] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in _CLASSES and (nm := n.child_by_field_name("name")) is not None:
            out.append(node_text(nm, source))
        stack.extend(n.named_children)
    return out


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None
             ) -> tuple[str, frozenset[str], list[str]]:
    return rel, callable_names(root, source), _classes(root, source)


def _resolve(values: list[tuple[str, frozenset[str], list[str]]], index: Any
             ) -> tuple[dict[str, frozenset[str]], dict[str, str | None]]:
    """(file → its callable names, class name → its file; ``None`` for a name two classes
    share, as the heritage index has it)."""
    names: dict[str, frozenset[str]] = {}
    files: dict[str, str | None] = {}
    for rel, found, classes in values:
        names[rel] = found
        for cls in classes:
            record_distinct(files, cls, rel)
    return names, files


register_fact(FactSpec(name=FACT, language="typescript", gate=(b"",),
                       collect=_collect, resolve=_resolve))


def follow_reexports(bindings: dict[str, str], imports: Mapping[str, tuple[str, str]],
                     index: Any | None) -> dict[str, str]:
    """``bindings`` (local name → the import's file) with each named or default import moved to
    the file that declares it, following re-exports (``export { a } from './a'``,
    ``export * from``). An import that cannot be followed keeps its file."""
    from .declarations import resolve_export  # lazy: declarations imports this package

    out = dict(bindings)
    for local, (_spec, imported) in imports.items():
        where = out.get(local)
        if where is None or imported == "*":
            continue
        decl = resolve_export(index, where, imported)
        if decl is not None:
            out[local] = decl.file
    return out


def module_objects(root: Node, source: bytes,
                   imports: Mapping[str, tuple[str, str]]) -> frozenset[str]:
    """Local names bound to a whole module: ``import * as m`` and a top-level
    ``const m = require('./m')`` (not ``require('./m').x``, not a destructuring)."""
    out = {local for local, (_spec, imported) in imports.items() if imported == "*"}
    for node in root.named_children:
        if node.type not in ("lexical_declaration", "variable_declaration"):
            continue
        for d in node.named_children:
            nm = d.child_by_field_name("name") if d.type == "variable_declarator" else None
            value = d.child_by_field_name("value") if nm is not None else None
            if nm is not None and nm.type == "identifier" and value is not None \
                    and value.type == "call_expression":
                fn = value.child_by_field_name("function")
                if fn is not None and node_text(fn, source) == "require":
                    out.add(node_text(nm, source))
    return frozenset(out)


def _lookup(cls: str, name: str, heritage: Mapping[str, ClassHeritage | None]) -> str | None:
    """The file of ``cls`` or its nearest in-repo ancestor that declares ``name``; None when
    none does — the method is a library base's (``find`` on a LoopBack repository), or lies
    beyond a base the index cannot tell apart (missing, not wrong)."""
    own = heritage.get(cls)
    if own is None:
        return None
    if name in own.methods:
        return own.methods[name]
    for anc in walk_heritage(cls, own.extends, heritage).ancestors:
        if name in anc.methods:
            return anc.methods[name]
    return None


def checked_resolver(base: CallResolver, *, path: str, bindings: Mapping[str, str],
                     namespaces: frozenset[str], types: Mapping[str, str],
                     index: Any | None) -> CallResolver:
    """``base`` (the shared resolver, given the followed ``bindings``), keeping a path only
    when that file declares what is called. ``namespaces``: local names bound to a whole module
    (``import * as m``, ``const m = require('./m')``)."""
    from .declarations import resolve_export

    fact = index_fact(index, FACT)
    if not fact:
        return base
    callables, class_files = fact
    heritage: Mapping[str, ClassHeritage | None] = \
        getattr(index, "class_heritage", None) or {}

    def declares(file: str, name: str) -> bool:
        return name in callables.get(file, ())

    def on_class(cls: str | None, file: str, name: str) -> str | None:
        """``name`` called on class ``cls`` (or an instance of it) as bound to ``file``."""
        if cls is not None and class_files.get(cls) == file and heritage.get(cls) is not None:
            return _lookup(cls, name, heritage)
        # not a repo class the index tells apart (an interface, an object, a constant, a
        # class two files declare): the file, when it declares the name (checked by `resolve`)
        return file

    def resolve(name: str, receiver: str | None, owner: str | None = None,
                *args: Any, **kwargs: Any) -> str | None:
        found = target(name, receiver, owner, *args, **kwargs)
        # whatever tier found it, the file must declare the name (a same-file `search` that is
        # a constant, not a function, is no call target)
        return found if found is not None and declares(found, name) else None

    def target(name: str, receiver: str | None, owner: str | None,
               *args: Any, **kwargs: Any) -> str | None:
        found = base(name, receiver, owner, *args, **kwargs)
        if found is None or found == path or receiver is None \
                or receiver in ("this", "self", "super"):
            return found
        if receiver in bindings:  # `Imported.m()`
            if receiver in namespaces:  # a module object: its export `name`
                decl = resolve_export(index, found, name)
                return decl.file if decl is not None else found
            return on_class(receiver, found, name)
        # a typed receiver: `this.repo.find()` → the class of `repo`'s type
        var = receiver[len("this."):] if receiver.startswith("this.") else receiver
        written = types.get(var)
        cls = written.split("<", 1)[0].strip().rstrip("[]").strip().rsplit(".", 1)[-1] \
            if written else None
        return on_class(cls, found, name)

    return resolve
