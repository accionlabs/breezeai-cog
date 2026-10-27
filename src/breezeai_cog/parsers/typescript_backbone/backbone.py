"""Backbone.js detection (additive, TypeScript/JavaScript).

Finds two things in a file that uses Backbone (routes live in ``routes.py``):

* **API calls** - ``fetch`` / ``save`` / ``destroy`` / ``sync`` on a model and
  ``fetch`` / ``create`` on a collection, as ``api_call`` with the verb taken from the
  method and the endpoint resolved from ``url`` / ``urlRoot`` where they are literals.
* **View role** - ``View.extend`` / ``extends View`` is tagged ``uiRole: view``.

A detector only finds: every result is an ``Emit`` and the registry writes it. The class
collection helpers here are shared with ``routes.py``.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Iterator

from ..additive import (DetectContext, Detection, Detector, FactSpec, Gate, index_fact,
                        register_detector, register_fact)
from ..emits import Emit

_FRAMEWORK = "backbone"
_ORDER = 61  # must be unique among the TypeScript detectors (routes.py takes 62)
_FACT = "backbone.classes"  # base classes declared in other files

# Longest suffix first, so ``CollectionView`` is a view and ``Collection`` a collection.
_KINDS = ("Collection", "Router", "Model", "View")
_LIBRARY = re.compile(r"backbone|marionette", re.I)  # module specifier of the library

_MODEL_CALLS = {"fetch": "GET", "save": None, "destroy": "DELETE"}  # save: PUT or POST
_COLLECTION_CALLS = {"fetch": "GET", "create": "POST"}
_SYNC_VERBS = {"read": "GET", "create": "POST", "update": "PUT", "patch": "PATCH", "delete": "DELETE"}
_FUNCTIONS = ("function_expression", "function", "arrow_function")


@dataclass
class _Class:
    kind: str
    name: str
    node: object  # the ``.extend(...)`` call or the class declaration
    props: dict = field(default_factory=dict)
    src: bytes = b""
    parent: str = ""  # unresolved base class name, for classes deriving from another class
    inherited: dict = field(default_factory=dict)  # url / urlRoot from a base in another file
    offline: bool = False  # a model whose collection stores locally (``localStorage`` / its own ``sync``)

    def string(self, key: str) -> str | None:
        if key in self.props:
            return _string(self.props[key], self.src)
        return self.inherited.get(key)


@dataclass
class _Target:
    kind: str
    cls: _Class | None
    is_new: bool | None  # None: the code does not settle ``isNew()``


# --------------------------------------------------------------------------- helpers

def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _line(node) -> int:
    return node.start_point[0] + 1


def _walk(root) -> Iterator:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _string(node, src: bytes) -> str | None:
    """The value of a string literal (or substitution-free template); else None."""
    if node is None or node.type not in ("string", "template_string"):
        return None
    raw = _text(node, src)
    if node.type == "template_string" and "${" in raw:
        return None
    return raw[1:-1]


def _key(node, src: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return _string(node, src)
    if node.type in ("property_identifier", "identifier", "number"):
        return _text(node, src)
    return None


def _kind(type_text: str, roots: dict) -> str | None:
    """Backbone kind of a base class written ``Backbone.Router`` or ``Router`` (imported from the library).

    The first name must come from the library: ``express.Router`` and ``UserViewModel`` are not Backbone.
    """
    parts = type_text.strip().split(".")
    if parts[0] not in roots:
        return None
    # a bare import is judged by the name it had in the library: ``{ Router as R }``
    name = roots[parts[0]] if len(parts) == 1 and roots[parts[0]] else parts[-1]
    return next((k for k in _KINDS if name.endswith(k)), None)


def _backbone_names(root, src: bytes) -> dict:
    """Local name -> the library's own name for it (``None`` for a namespace such as ``Backbone``).

    ``Backbone`` / ``Marionette`` / ``Mn`` count even as globals; the rest is what the file imports or
    requires from ``backbone`` / ``backbone.marionette``."""
    names: dict = {"Backbone": None, "Marionette": None, "Mn": None}  # ``Mn``: Marionette's documented alias
    for stmt in root.named_children:
        if stmt.type == "import_statement":
            source = stmt.child_by_field_name("source")
            if source is None or not _LIBRARY.search(_text(source, src)):
                continue
            for node in _walk(stmt):
                if node.type == "import_specifier":  # ``{ Router as R }`` binds R
                    original = node.child_by_field_name("name")
                    local = node.child_by_field_name("alias") or original
                    if local is not None:
                        names[_text(local, src)] = _text(original, src)
                elif node.type == "identifier" and node.parent.type in ("import_clause", "namespace_import"):
                    names[_text(node, src)] = None
        elif stmt.type in ("lexical_declaration", "variable_declaration"):
            for decl in stmt.named_children:  # ``const Backbone = require('backbone')``
                value = _unwrap(decl.child_by_field_name("value")) if decl.type == "variable_declarator" else None
                if value is None or value.type != "call_expression":
                    continue
                callee = value.child_by_field_name("function")  # cheap checks first: ``value`` can be a whole IIFE
                if callee is None or callee.type != "identifier" or _text(callee, src) != "require":
                    continue
                if not _LIBRARY.search(_text(value, src)):
                    continue
                name = decl.child_by_field_name("name")
                if name.type == "identifier":
                    names[_text(name, src)] = None
                else:  # ``const { Router } = require(...)``
                    for n in _walk(name):
                        if n.type == "shorthand_property_identifier_pattern":
                            names[_text(n, src)] = _text(n, src)
    return names


def _args(call) -> list:
    args = call.child_by_field_name("arguments")
    return [a for a in args.named_children if a.type != "comment"] if args else []


def _object_props(obj, src: bytes) -> dict:
    props: dict = {}
    for child in obj.named_children:
        if child.type == "pair":
            key = _key(child.child_by_field_name("key"), src)
            value = child.child_by_field_name("value")
            if key and value is not None:
                props[key] = value
        elif child.type == "method_definition":
            key = _key(child.child_by_field_name("name"), src)
            if key:
                props[key] = child
    return props


def _class_props(body, src: bytes) -> dict:
    props: dict = {}
    for child in body.named_children:
        if child.type == "method_definition":
            key = _key(child.child_by_field_name("name"), src)
            if key:
                props[key] = child
        elif child.type in ("field_definition", "public_field_definition"):
            name = child.child_by_field_name("name") or child.child_by_field_name("property")
            value = child.child_by_field_name("value")
            key = _key(name, src)
            if key and value is not None:
                props[key] = value
    return props


def _binding_name(call, src: bytes) -> str:
    parent = call.parent
    while parent is not None and parent.type in ("parenthesized_expression", "as_expression"):
        parent = parent.parent
    if parent is None:
        return ""
    if parent.type == "variable_declarator":
        name = parent.child_by_field_name("name")
    elif parent.type == "assignment_expression":
        name = parent.child_by_field_name("left")
    elif parent.type == "pair":
        name = parent.child_by_field_name("key")
    else:
        return ""
    return _text(name, src) if name is not None else ""


def _unwrap(node):
    while node is not None and node.type in ("parenthesized_expression", "as_expression"):
        node = node.named_children[0] if node.named_children else None
    return node


# --------------------------------------------------------------------------- values (file-local constants and objects)

class _Values:
    """Constants and object variables of a file, resolved only when unambiguous.

    A name declared twice with different values, or assigned again, resolves to nothing:
    that beats resolving to the wrong literal.
    """

    def __init__(self, root, src: bytes):
        self.root = root
        self.src = src
        self._decls: dict | None = None  # built on first use: most files never resolve a name

    @property
    def decls(self) -> dict:
        if self._decls is None:
            self._decls = self._collect()
        return self._decls

    def _collect(self) -> dict:
        src, decls = self.src, {}
        for node in _walk(self.root):
            if node.type == "variable_declarator":
                name, value = node.child_by_field_name("name"), _unwrap(node.child_by_field_name("value"))
                if name is None or name.type != "identifier" or value is None:
                    continue
                key = _text(name, src)
                old = decls.get(key, value)
                # the length check settles most redeclarations without slicing either value
                if key in decls and (old is None or old.end_byte - old.start_byte != value.end_byte - value.start_byte
                                     or _text(old, src) != _text(value, src)):
                    decls[key] = None
                else:
                    decls[key] = value
            elif node.type == "assignment_expression":
                left = node.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    decls[_text(left, src)] = None
        return decls

    def _declared(self, node, seen: frozenset):
        name = _text(node, self.src)
        return (None, seen) if name in seen else (self.decls.get(name), seen | {name})

    def string(self, node, seen: frozenset = frozenset()) -> str | None:
        """A string written as a literal, a name bound to one, or a ``+`` / template of those."""
        node = _unwrap(node)
        if node is None:
            return None
        if node.type == "identifier":
            target, seen = self._declared(node, seen)
            return self.string(target, seen)
        if node.type == "binary_expression":
            op = node.child_by_field_name("operator")
            left, right = (self.string(node.child_by_field_name(f), seen) for f in ("left", "right"))
            if op is not None and _text(op, self.src) == "+" and left is not None and right is not None:
                return left + right
            return None
        if node.type == "template_string" and "${" in _text(node, self.src):
            parts = []
            for child in node.children:
                if child.type == "`":
                    continue
                if child.type == "template_substitution":
                    inner = self.string(child.named_children[0], seen) if child.named_children else None
                    if inner is None:
                        return None
                    parts.append(inner)
                else:
                    parts.append(_text(child, self.src))
            return "".join(parts)
        return _string(node, self.src)

    def object(self, node, seen: frozenset = frozenset()):
        """The object literal a node is, or a name bound to one."""
        node = _unwrap(node)
        if node is None:
            return None
        if node.type == "object":
            return node
        if node.type == "identifier":
            target, seen = self._declared(node, seen)
            return self.object(target, seen)
        if node.type == "call_expression":  # Object.freeze({...})
            fn = node.child_by_field_name("function")
            args = _args(node)
            if fn is not None and _text(fn, self.src) == "Object.freeze" and args:
                return self.object(args[0], seen)
        return None

    def pairs(self, table, seen: frozenset = frozenset()) -> list:
        """The ``key: value`` pairs of an object, with ``...spread`` of known objects inlined."""
        obj = self.object(table)
        if obj is None or obj.start_byte in seen:
            return []
        seen = seen | {obj.start_byte}
        found = []
        for child in obj.named_children:
            if child.type == "pair":
                found.append(child)
            elif child.type == "spread_element" and child.named_children:
                found.extend(self.pairs(child.named_children[0], seen))
        return found


# --------------------------------------------------------------------------- collection

def _by_name(classes: list) -> dict:
    """name -> class. ``window.Todo`` / ``app.models.Todo`` is also reachable as ``Todo`` (how it is
    used); a short name shared by two different classes is left out."""
    named: dict = {}
    clashes: set = set()
    for c in classes:
        if not c.name:
            continue
        named[c.name] = c
        short = c.name.rsplit(".", 1)[-1]
        if short != c.name:
            if short in named and named[short] is not c:
                clashes.add(short)
            named.setdefault(short, c)
    for short in clashes:
        named.pop(short, None)
    return named


def _collect_classes(root, src: bytes, external: dict | None = None,
                     keep_all: bool = False, roots: dict | None = None,
                     values: _Values | None = None) -> list[_Class]:
    classes: list[_Class] = []
    roots = roots if roots is not None else _backbone_names(root, src)
    values = values or _Values(root, src)  # lazy: only ``extend(name)`` makes it walk
    for node in _walk(root):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                continue
            if _text(fn.child_by_field_name("property"), src) != "extend":
                continue
            kind = _kind(_text(fn.child_by_field_name("object"), src), roots)
            args = _args(node)
            definition = args[0] if args else None
            if definition is not None and definition.type == "identifier":  # extend(definition)
                definition = values.object(definition)
            if definition is not None and definition.type == "object":
                parent = "" if kind else _text(fn.child_by_field_name("object"), src)
                classes.append(_Class(kind or "", _binding_name(node, src), node,
                                      _object_props(definition, src), src, parent))
        elif node.type in ("class_declaration", "class"):
            heritage = next((c for c in node.children if c.type == "class_heritage"), None)
            match = re.search(r"extends\s+([\w$.]+)", _text(heritage, src)) if heritage else None
            kind = _kind(match.group(1), roots) if match else None
            body = node.child_by_field_name("body")
            if match and body is not None:
                name = node.child_by_field_name("name")
                classes.append(_Class(kind or "", _text(name, src) if name else "", node,
                                      _class_props(body, src), src, "" if kind else match.group(1)))
    # Derived classes take the kind and inherited properties of a class declared in the file.
    by_name = _by_name(classes)
    changed = True
    while changed:
        changed = False
        for c in classes:
            name = c.parent.rsplit(".", 1)[-1]
            base = by_name.get(name) if c.parent else None
            if c.kind or base is c:
                continue
            if base is not None and base.kind:
                c.kind, c.props = base.kind, {**base.props, **c.props}
                c.inherited = base.inherited
                changed = True
            elif base is None and c.parent and name in (external or {}):
                c.kind, url, urlroot = external[name]
                c.inherited = {k: v for k, v in (("url", url), ("urlRoot", urlroot)) if v}
                changed = True
    return classes if keep_all else [c for c in classes if c.kind]


def _collect_instances(root, src: bytes, by_name: dict, roots: dict, class_keys: frozenset = frozenset()) -> dict:
    """``x = new Todo(...)`` / ``this.todo = new Todo(...)`` -> target, keyed by the left text.

    A name bound to different classes resolves to nothing (``None``); bound to one class with
    different ``isNew()`` answers, it resolves to that class with the answer unknown.
    """
    instances: dict = {}

    def bind(key: tuple, target: _Target | None) -> None:
        if key not in instances:
            instances[key] = target
            return
        old = instances[key]
        if old is None or target is None or old.kind != target.kind or old.cls is not target.cls:
            instances[key] = None
        elif old.is_new != target.is_new:
            instances[key] = _Target(old.kind, old.cls, None)
    for node in _walk(root):
        if node.type in _FN_SCOPES:  # a parameter shadows an outer instance of the same name
            params = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
            fkey = (node.start_byte, node.end_byte, node.type)
            for n in (_walk(params) if params is not None else ()):
                if n.type == "identifier":
                    bind((fkey, _text(n, src)), None)
            continue
        if node.type == "variable_declarator":
            left, right, declared = node.child_by_field_name("name"), node.child_by_field_name("value"), True
        elif node.type == "assignment_expression":
            left, right, declared = node.child_by_field_name("left"), node.child_by_field_name("right"), False
        else:
            continue
        if left is None or left.type not in ("identifier", "member_expression"):
            continue
        name = _text(left, src)
        # A declared name belongs to its function; an assigned one may be declared anywhere, so it is
        # file-wide (and any scope that declares or receives the name itself still shadows it).
        if declared and left.type == "identifier":
            key = (_scope_key(node), name)
        elif name.startswith("this."):  # ``this.model``: one per class, not one per file
            key = (("class", _class_key(node, class_keys)), name)
        else:
            key = (None, name)
        right = _unwrap(right)
        target = None
        if right is not None and right.type == "identifier":
            alias = _text(right, src)  # ``const M = Todo`` / ``const t = todo``
            if alias in by_name and left.type == "identifier":
                by_name.setdefault(name, by_name[alias])
                continue
            found = _lookup(instances, alias, node)
            target = None if found is _MISSING else found
        elif right is not None and right.type == "new_expression":
            target = _new_target(right, src, by_name, roots)
        if target is not None:
            bind(key, target)
        elif declared and right is not None and left.type == "identifier":
            bind(key, None)  # declared here as something else: it hides an outer instance
    _unsettle_newness(root, src, instances)
    return instances


_NEWNESS_CHANGERS = frozenset({"set", "unset", "clear", "fetch", "parse"})  # may give or drop the id
_ID_DROPPERS = frozenset({"unset", "clear"})  # the only calls that can take the id from a model that has one


def _unsettle_newness(root, src: bytes, instances: dict) -> None:
    """The constructor settles ``isNew()`` only until the model changes: ``m.set('id', 1)``, ``m.fetch()``,
    ``m.id = 1`` or an earlier ``m.save()`` (which assigns the id) make it unknown for that name."""
    changed: set = set()  # any of these makes a "new" model unknown
    dropped: set = set()  # these also make a model constructed with an id unknown
    saves: dict = {}
    for node in _walk(root):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                continue
            prop = _text(fn.child_by_field_name("property"), src)
            if prop in _NEWNESS_CHANGERS or prop == "save":
                obj = _unwrap(fn.child_by_field_name("object"))
                if obj is None:
                    continue
                name = _text(obj, src)
                if prop == "save":
                    saves[name] = saves.get(name, 0) + 1
                else:
                    changed.add(name)
                    if prop in _ID_DROPPERS:
                        dropped.add(name)
        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left is not None and left.type == "member_expression":
                prop = _text(left.child_by_field_name("property"), src)
                if prop in ("id", "attributes", "cid") or prop.endswith("Id"):
                    name = _text(left.child_by_field_name("object"), src)
                    changed.add(name)
                    dropped.add(name)
    changed.update(name for name, n in saves.items() if n > 1)
    for key, target in instances.items():
        if target is not None and target.is_new is not None and key[1] in (changed if target.is_new else dropped):
            instances[key] = _Target(target.kind, target.cls, None)


_MISSING = object()
_IDENT = re.compile(r"[\w$]+")
_FN_SCOPES = ("function_expression", "function", "function_declaration", "generator_function",
              "generator_function_declaration", "arrow_function", "method_definition")
_SCOPES = _FN_SCOPES + ("program",)
_THIS_REBINDERS = ("function_expression", "function", "function_declaration", "generator_function",
                   "generator_function_declaration")  # not arrows, not methods: those keep ``this``
_PROPERTY_PARENTS = ("pair", "field_definition", "public_field_definition")


def _class_key(node, class_keys) -> tuple | None:
    """The Backbone class around a node, as a hashable key."""
    node = node.parent
    while node is not None:
        key = (node.start_byte, node.end_byte, node.type)
        if key in class_keys:
            return key
        node = node.parent
    return None


def _scope_key(node) -> tuple | None:
    """The innermost function (or the program) around a node, as a hashable key."""
    node = node.parent
    while node is not None and node.type not in _SCOPES:
        node = node.parent
    return (node.start_byte, node.end_byte, node.type) if node is not None else None


def _lookup(instances: dict, name: str, node):
    """The instance a name denotes at ``node``: the nearest enclosing function that binds it, else the
    file-wide binding; ``_MISSING`` when none. A bound ``None`` means shadowed or ambiguous."""
    if _IDENT.fullmatch(name):
        parent = node.parent
        while parent is not None:
            if parent.type in _SCOPES:
                key = ((parent.start_byte, parent.end_byte, parent.type), name)
                if key in instances:
                    return instances[key]
            parent = parent.parent
    return instances.get((None, name), _MISSING)


def _new_target(new, src: bytes, by_name: dict, roots: dict) -> _Target | None:
    ctor = _text(new.child_by_field_name("constructor"), src)
    cls = by_name.get(ctor) or by_name.get(ctor.rsplit(".", 1)[-1])
    kind = cls.kind if cls else _kind(ctor, roots)
    if kind is None:
        return None
    return _Target(kind, cls, _newness(_args(new), cls, src))


def _newness(args: list, cls: _Class | None, src: bytes) -> bool | None:
    """``isNew()`` as the constructor arguments settle it: no id attribute -> new, one -> not new.

    ``None`` when they do not: a variable, a spread, or an ``idAttribute`` that is not known.
    """
    if not args:
        return True
    attrs = args[0]
    if attrs.type != "object":
        return None
    id_attr = "id"
    if cls is not None and "idAttribute" in cls.props:
        id_attr = cls.string("idAttribute")
        if id_attr is None:
            return None
    for child in attrs.named_children:
        if child.type == "spread_element":
            return None
        if child.type == "pair":
            key = _key(child.child_by_field_name("key"), src)
        elif child.type == "shorthand_property_identifier":
            key = _text(child, src)
        else:
            key = None
        if key == id_attr:
            return False
    base_elsewhere = cls is not None and (cls.node is None or bool(cls.inherited))  # idAttribute unseen
    return None if base_elsewhere else True


# --------------------------------------------------------------------------- api calls

def _endpoint(target: _Target, verb: str | None) -> str:
    if target.cls is None:
        return ""
    url = target.cls.string("url")
    if url:
        return url
    if "url" in target.cls.props:  # ``url()`` computed at runtime overrides urlRoot
        return ""
    if target.kind == "Collection" or verb is None:  # the id is part of the path only for some verbs
        return ""
    root = target.cls.string("urlRoot")
    if not root:
        return ""
    if verb == "POST" or (verb == "GET" and target.is_new):  # a model with no id is addressed by the root
        return root
    return root.rstrip("/") + "/:id"


def _offline(cls: _Class | None) -> bool:
    """A class that stores locally (``localStorage``) or writes its own ``sync`` sends no request of its own."""
    return cls is not None and (cls.offline or "sync" in cls.props or "localStorage" in cls.props)


_SAVE_VERBS = {True: "POST", False: "PUT", None: None}  # by ``isNew()``


def _save_verb(args: list, target: _Target, src: bytes) -> str | None:
    """``save()``: ``PATCH`` with ``{ patch: true }``, else ``POST`` for a new model, ``PUT`` for one with an id."""
    first = args[0] if args else None
    if first is not None and first.type == "string":
        index = 2  # save(key, value, options)
    elif first is not None and first.type not in ("object", "null", "undefined") and len(args) >= 3:
        index = 2
    else:
        index = 1  # save(attrs, options)
    options = args[index] if len(args) > index else None
    if options is None:
        return _SAVE_VERBS[target.is_new]
    if options.type != "object":
        return None  # options held in a variable: ``patch`` is unknown
    patch = _object_props(options, src).get("patch")
    if patch is None or _text(patch, src) == "false":
        return _SAVE_VERBS[target.is_new]
    return "PATCH" if _text(patch, src) == "true" else None


def _api_call(call, prop: str, objtext: str, args: list, resolve, src: bytes) -> Emit | None:
    if prop == "sync":
        target = resolve(_text(args[1], src) if objtext == "Backbone" and len(args) > 1 else objtext, call)
        verb = _SYNC_VERBS.get(_string(args[0], src) or "") if args else None
        if target is None or target.kind not in ("Model", "Collection"):
            return None
        if objtext != "Backbone" and _offline(target.cls):  # ``Backbone.sync(...)`` is the real one
            return None
    else:
        target = resolve(objtext, call)
        if target is None:
            return None
        table = _MODEL_CALLS if target.kind == "Model" else _COLLECTION_CALLS if target.kind == "Collection" else {}
        if prop not in table or (target.cls is not None and prop in target.cls.props) or _offline(target.cls):
            return None  # not a Backbone call, the class overrides it, or it never reaches a server
        verb = _save_verb(args, target, src) if prop == "save" else table[prop]
        if prop == "destroy" and target.kind == "Model" and target.is_new:
            return None  # a model that was never saved is dropped locally; Backbone sends no request
    fields = {"semanticType": "api_call", "framework": _FRAMEWORK}
    if verb:
        fields["method"] = verb
    endpoint = _endpoint(target, verb)
    if endpoint:
        fields["endpoint"] = endpoint
    # ``own``: a call nested in a larger statement (a handler inside ``Router.extend({...})``) gets its
    # own record instead of tagging the enclosing statement.
    return Emit(call, fields, enrich="own", reclassify=frozenset({"api_call"}))


# --------------------------------------------------------------------------- cross-file base classes

def _collect_fact(root, source: bytes, rel: str, repo_root) -> list | None:
    """Each named class of the file: ``(name, kind, parent, url, urlRoot)``; ``window.Todo`` is ``Todo``."""
    found = [(c.name.rsplit(".", 1)[-1], c.kind, c.parent.rsplit(".", 1)[-1], c.string("url"), c.string("urlRoot"))
             for c in _collect_classes(root, source, keep_all=True) if c.name]
    return found or None


def _resolve_fact(values: list, index) -> dict | None:
    """name -> ``(kind, url, urlRoot)`` for the classes whose Backbone kind is certain.

    A name declared twice with different shapes is ambiguous and left out (honest-null)."""
    seen: dict = {}
    for entries in values:
        for entry in entries:
            seen[entry[0]] = None if seen.get(entry[0], entry) != entry else entry
    resolved: dict = {}
    changed = True
    while changed:
        changed = False
        for name, entry in seen.items():
            if entry is None or name in resolved:
                continue
            _, kind, parent, url, urlroot = entry
            if not kind:
                base = resolved.get(parent)
                if base is None:
                    continue
                kind, url, urlroot = base[0], url or base[1], urlroot or base[2]
            resolved[name] = (kind, url, urlroot)
            changed = True
    return resolved or None


# --------------------------------------------------------------------------- detector

@dataclass
class _Scope:
    """The Backbone classes and instances of one file, as the detectors resolve them."""
    classes: list
    by_name: dict
    instances: dict
    src: bytes
    roots: dict = field(default_factory=dict)
    values: _Values | None = None  # shared with routes.py; walks the file only if a name is resolved

    def __post_init__(self):
        self._by_node = {(c.node.start_byte, c.node.end_byte, c.node.type): c
                         for c in self.classes}

    def enclosing(self, node) -> _Class | None:
        parent = node.parent
        while parent is not None:
            hit = self._by_node.get((parent.start_byte, parent.end_byte, parent.type))
            if hit is not None:
                return hit
            parent = parent.parent
        return None

    def owner(self, node) -> _Class | None:
        """The class ``this`` means at ``node``, or None when a ``function`` rebinds it.

        Methods and arrow functions keep the class's ``this``, and so does the class's own property
        function (``save: function () {}``); a ``function`` nested in one, or passed to ``$.each``, does not.
        """
        encl = self.enclosing(node)
        if encl is None:
            return None
        key = (encl.node.start_byte, encl.node.end_byte, encl.node.type)
        rebinders, parent = [], node.parent
        while parent is not None and (parent.start_byte, parent.end_byte, parent.type) != key:
            if parent.type in _THIS_REBINDERS:
                rebinders.append(parent)
            parent = parent.parent
        if not rebinders or (len(rebinders) == 1 and rebinders[0].parent.type in _PROPERTY_PARENTS):
            return encl
        return None

    def resolve(self, objtext: str, node) -> _Target | None:
        encl = self.owner(node) if objtext == "this" or objtext.startswith("this.") else None
        if objtext == "this":
            return _Target(encl.kind, encl, None) if encl else None
        if objtext.startswith("this."):
            ckey = (encl.node.start_byte, encl.node.end_byte, encl.node.type) if encl else None
            found = self.instances.get((("class", ckey), objtext), _MISSING)
        else:
            found = _lookup(self.instances, objtext, node)
        if found is not _MISSING:
            return found
        if objtext in ("this.model", "this.collection") and encl:
            kind = "Model" if objtext == "this.model" else "Collection"
            ref = encl.props.get(kind.lower())
            cls = self.by_name.get(_text(ref, self.src)) if ref is not None and ref.type == "identifier" else None
            if cls:
                return _Target(cls.kind, cls, None)
            if encl.kind == "View":  # a view holds the model / collection it was given: the call is
                return _Target(kind, None, None)  # certain, the class (so the endpoint) is not
        return None


_THREAD = threading.local()  # the last file's scope: both detectors run on one file back to back


def _scope(dc: DetectContext) -> _Scope:
    """The file's Backbone scope, built once and shared by the ``backbone`` and ``backbone-routes`` detectors."""
    hit = getattr(_THREAD, "hit", None)
    if hit is not None and hit[0] is dc.ctx and hit[1] == dc.root:
        return hit[2]
    scope = _build_scope(dc)
    _THREAD.hit = (dc.ctx, dc.root, scope)
    return scope


def _build_scope(dc: DetectContext) -> _Scope:
    """Classes, prototype assignments and instances of the file.

    Classes declared in other files (from the cross-file fact) are bound too, so
    ``new ImportedRouter()`` resolves to a router and ``new ImportedTodo()`` to a model.
    """
    root, src = dc.root, dc.source
    external = index_fact(dc.ctx.resolution_index, _FACT)
    roots = _backbone_names(root, src)
    values = _Values(root, src)
    classes = _collect_classes(root, src, external, roots=roots, values=values)
    by_name = _by_name(classes)
    for name, (kind, url, urlroot) in (external or {}).items():
        if kind in ("Model", "Collection", "Router") and name not in by_name:
            by_name[name] = _Class(kind, name, None, {}, src,
                                   inherited={k: v for k, v in (("url", url), ("urlRoot", urlroot)) if v})
    for node in _walk(root):  # ``Todo.prototype.urlRoot = '/api'``
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            m = re.fullmatch(r"([\w$.]+)\.prototype\.(\w+)", _text(left, src)) if left is not None else None
            cls = by_name.get(m.group(1).rsplit(".", 1)[-1]) if m else None
            value = node.child_by_field_name("right")
            if cls is not None and value is not None:
                cls.props[m.group(2)] = value
    for c in classes:  # the models of a collection that stores locally make no request either
        if c.kind == "Collection" and _offline(c):
            ref = c.props.get("model")
            model = by_name.get(_text(ref, src)) if ref is not None and ref.type == "identifier" else None
            if model is not None:
                model.offline = True
    class_keys = frozenset((c.node.start_byte, c.node.end_byte, c.node.type) for c in classes)
    return _Scope(classes, by_name, _collect_instances(root, src, by_name, roots, class_keys), src, roots, values)


def _detect_backbone(dc: DetectContext) -> Detection | None:
    root, src = dc.root, dc.source
    # Several markers (a Marionette-only file never says "Backbone"), so the gate cannot guard alone.
    if b"ackbone" not in src and b"arionette" not in src and b"Mn." not in src:
        return None
    scope = _scope(dc)
    instances = scope.instances

    found: list[Emit] = []
    for cls in scope.classes:
        if cls.kind == "View":
            found.append(Emit(cls.node, {"uiRole": "view", "framework": _FRAMEWORK}, enrich="starts"))

    for node in _walk(root):
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            continue
        prop = _text(fn.child_by_field_name("property"), src)
        if prop not in ("fetch", "save", "destroy", "create", "sync"):
            continue
        obj = _unwrap(fn.child_by_field_name("object"))
        objtext = _text(obj, src) if obj is not None else ""
        if obj is not None and obj.type == "new_expression" and (None, objtext) not in instances:
            made = _new_target(obj, src, scope.by_name, scope.roots)  # ``new Todo({ id }).fetch()``
            if made is not None:
                instances[(None, objtext)] = made
        emit = _api_call(node, prop, objtext, _args(node), scope.resolve, src)
        if emit is not None:
            found.append(emit)

    return Detection(found, label=_FRAMEWORK) if found else None


register_fact(FactSpec(name=_FACT, language="typescript", gate=(b"ackbone",),
                       collect=_collect_fact, resolve=_resolve_fact))

register_detector(Detector(
    name="backbone",
    gate=Gate("typescript", order=_ORDER,
              guard=None,  # ``_detect_backbone`` checks the byte markers itself
              skip_fixtures=True),
    detect=_detect_backbone,
    frameworks=(_FRAMEWORK,),
))
