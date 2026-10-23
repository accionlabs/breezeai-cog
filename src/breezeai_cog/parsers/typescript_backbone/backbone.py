"""Backbone.js detection (additive, TypeScript/JavaScript).

Finds three things in a file that uses Backbone:

* **Routes** - ``routes: {...}`` of a ``Router.extend`` (or ES6 ``extends Router``) and
  ``router.route(...)`` calls. One ``route`` record per binding, prefixed with the
  ``root`` of ``Backbone.history.start({ root })`` when the file declares one.
* **API calls** - ``fetch`` / ``save`` / ``destroy`` / ``sync`` on a model and
  ``fetch`` / ``create`` on a collection, as ``api_call`` with the verb taken from the
  method and the endpoint resolved from ``url`` / ``urlRoot`` where they are literals.
* **View role** - ``View.extend`` / ``extends View`` is tagged ``uiRole: view``.

A detector only finds: every result is an ``Emit`` and the registry writes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

from ..additive import (DetectContext, Detection, Detector, FactSpec, Gate, index_fact,
                        register_detector, register_fact)
from ..emits import Emit

_FRAMEWORK = "backbone"
_ORDER = 61  # must be unique among the TypeScript detectors
_FACT = "backbone.classes"  # base classes declared in other files

# Longest suffix first, so ``CollectionView`` is a view and ``Collection`` a collection.
_KINDS = ("Collection", "Router", "Model", "View")

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

    def string(self, key: str) -> str | None:
        if key in self.props:
            return _string(self.props[key], self.src)
        return self.inherited.get(key)


@dataclass
class _Target:
    kind: str
    cls: _Class | None
    is_new: bool


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


def _kind(type_text: str) -> str | None:
    last = type_text.rsplit(".", 1)[-1].strip()
    return next((k for k in _KINDS if last.endswith(k)), None)


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


# --------------------------------------------------------------------------- collection

def _collect_classes(root, src: bytes, external: dict | None = None,
                     keep_all: bool = False) -> list[_Class]:
    classes: list[_Class] = []
    for node in _walk(root):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                continue
            if _text(fn.child_by_field_name("property"), src) != "extend":
                continue
            kind = _kind(_text(fn.child_by_field_name("object"), src))
            args = _args(node)
            if args and args[0].type == "object":
                parent = "" if kind else _text(fn.child_by_field_name("object"), src)
                classes.append(_Class(kind or "", _binding_name(node, src), node,
                                      _object_props(args[0], src), src, parent))
        elif node.type in ("class_declaration", "class"):
            heritage = next((c for c in node.children if c.type == "class_heritage"), None)
            match = re.search(r"extends\s+([\w$.]+)", _text(heritage, src)) if heritage else None
            kind = _kind(match.group(1)) if match else None
            body = node.child_by_field_name("body")
            if match and body is not None:
                name = node.child_by_field_name("name")
                classes.append(_Class(kind or "", _text(name, src) if name else "", node,
                                      _class_props(body, src), src, "" if kind else match.group(1)))
    # Derived classes take the kind and inherited properties of a class declared in the file.
    by_name = {c.name: c for c in classes if c.name}
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


def _collect_instances(root, src: bytes, by_name: dict) -> dict:
    """``x = new Todo(...)`` / ``this.todo = new Todo(...)`` -> target, keyed by the left text."""
    instances: dict = {}
    for node in _walk(root):
        if node.type == "variable_declarator":
            left, right = node.child_by_field_name("name"), node.child_by_field_name("value")
        elif node.type == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        else:
            continue
        right = _unwrap(right)
        if left is not None and right is not None and right.type == "identifier":
            alias = _text(right, src)  # ``const M = Todo`` / ``const t = todo``
            if alias in by_name and left.type == "identifier":
                by_name.setdefault(_text(left, src), by_name[alias])
            elif alias in instances:
                instances[_text(left, src)] = instances[alias]
            continue
        if left is None or right is None or right.type != "new_expression":
            continue
        target = _new_target(right, src, by_name)
        if target is not None:
            instances[_text(left, src)] = target
    return instances


def _new_target(new, src: bytes, by_name: dict) -> _Target | None:
    ctor = _text(new.child_by_field_name("constructor"), src)
    cls = by_name.get(ctor) or by_name.get(ctor.rsplit(".", 1)[-1])
    kind = cls.kind if cls else _kind(ctor)
    if kind is None:
        return None
    args = new.child_by_field_name("arguments")
    has_id = bool(args and re.search(r"\bid\b", _text(args, src)))
    return _Target(kind, cls, is_new=not has_id)


def _collect_constants(root, src: bytes) -> dict:
    """``const PATH = 'users'`` -> ``{'PATH': 'users'}``.

    A name declared twice with different values, or assigned again, is dropped: resolving
    to nothing beats resolving to the wrong literal.
    """
    values: dict = {}
    for node in _walk(root):
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is None or name.type != "identifier":
                continue
            value = _string(_unwrap(node.child_by_field_name("value")), src)
            key = _text(name, src)
            if value is None:
                values[key] = None
            elif key in values and values[key] != value:
                values[key] = None
            else:
                values.setdefault(key, value)
        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                values[_text(left, src)] = None
    return {k: v for k, v in values.items() if v is not None}


def _path_of(node, src: bytes, constants: dict) -> str | None:
    """A route path written as a literal or as a name bound to one; else None."""
    node = _unwrap(node)
    if node is None:
        return None
    if node.type == "identifier":
        return constants.get(_text(node, src))
    return _string(node, src)


def _history_base(root, src: bytes) -> str:
    """``Backbone.history.start({ root: '/app/' })`` -> ``/app``; empty when absent or ambiguous."""
    roots = set()
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        if fn is None or not _text(fn, src).endswith("history.start"):
            continue
        args = _args(node)
        if args and args[0].type == "object":
            value = _string(_object_props(args[0], src).get("root"), src)
            if value is not None:
                roots.add(value.strip("/"))
    only = next(iter(roots)) if len(roots) == 1 else ""
    return "/" + only if only else ""


def _join(base: str, path: str) -> str:
    tail = path.lstrip("/")
    if not tail:
        return base or "/"
    return f"{base}/{tail}"


# --------------------------------------------------------------------------- routes

def _handler_fields(value, cls: _Class | None, src: bytes) -> dict:
    """The handler a route points at, as the code writes it, plus its line when local."""
    name = _string(value, src)
    if name is not None:
        fields: dict = {"handler": name}
        target = cls.props.get(name) if cls else None
        if target is not None:
            fields["handlerLine"] = _line(target)
        return fields
    if value.type in _FUNCTIONS:
        return {"handlerLine": _line(value)}
    if value.type == "identifier":
        return {"handler": _text(value, src)}
    if value.type == "member_expression":
        return {"handler": _text(value.child_by_field_name("property"), src)}
    return {}


def _routes_of(cls: _Class, base: str, src: bytes, constants: dict) -> list[Emit]:
    table = cls.props.get("routes")
    if table is not None and table.type == "method_definition":  # get routes() { return {...} }
        body = table.child_by_field_name("body")
        ret = next((n for n in _walk(body) if n.type == "return_statement"), None) if body else None
        table = _unwrap(ret.named_children[0]) if ret is not None and ret.named_children else None
    if table is None or table.type != "object":
        return []
    found = []
    for pair in table.named_children:
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        if key is not None and key.type == "computed_property_name":  # [PATH]: 'handler'
            path = _path_of(key.named_children[0], src, constants) if key.named_children else None
        else:
            path = _key(key, src)
        value = pair.child_by_field_name("value")
        if path is None or value is None:
            continue
        fields = {
            "semanticType": "route",
            "framework": _FRAMEWORK,
            "routeKind": "page",
            "endpoint": _join(base, path),
        }
        fields.update(_handler_fields(value, cls, src))
        found.append(Emit(pair, fields))
    return found


def _route_call(call, args: list, cls: _Class | None, base: str, src: bytes,
                constants: dict) -> Emit | None:
    """``router.route(path, name, callback)`` / ``router.route(path, callback)``."""
    if args[0].type == "regex":  # a RegExp route has no address to record
        return None
    path = _path_of(args[0], src, constants)
    # A path that is not a literal or a name bound to one is still a binding: record it
    # with an empty endpoint (honest-null) rather than guessing the address.
    fields = {
        "semanticType": "route",
        "framework": _FRAMEWORK,
        "routeKind": "page",
        "endpoint": _join(base, path) if path is not None else "",
    }
    if len(args) >= 3:
        fields.update(_handler_fields(args[2], cls, src))
    elif len(args) == 2 and _string(args[1], src) is None:
        fields.update(_handler_fields(args[1], cls, src))
    elif len(args) == 2:  # route name only: the callback is this[name]
        fields.update(_handler_fields(args[1], cls, src))
    return Emit(call, fields, enrich="same")


# --------------------------------------------------------------------------- api calls

def _endpoint(target: _Target, verb: str | None) -> str:
    if target.cls is None:
        return ""
    url = target.cls.string("url")
    if url:
        return url
    if target.kind == "Collection":
        return ""
    root = target.cls.string("urlRoot")
    if not root:
        return ""
    return root if verb == "POST" else root.rstrip("/") + "/:id"


def _api_call(call, prop: str, objtext: str, args: list, resolve, src: bytes) -> Emit | None:
    if prop == "sync":
        target = resolve(_text(args[1], src) if objtext == "Backbone" and len(args) > 1 else objtext, call)
        verb = _SYNC_VERBS.get(_string(args[0], src) or "") if args else None
        if target is None or target.kind not in ("Model", "Collection"):
            return None
    else:
        target = resolve(objtext, call)
        if target is None:
            return None
        table = _MODEL_CALLS if target.kind == "Model" else _COLLECTION_CALLS if target.kind == "Collection" else {}
        if prop not in table or (target.cls is not None and prop in target.cls.props):
            return None  # not a Backbone call, or the class overrides it
        verb = table[prop] or ("POST" if target.is_new else "PUT")
    fields = {"semanticType": "api_call", "framework": _FRAMEWORK}
    if verb:
        fields["method"] = verb
    endpoint = _endpoint(target, verb)
    if endpoint:
        fields["endpoint"] = endpoint
    return Emit(call, fields, enrich="unclassified", reclassify=frozenset({"api_call"}))


# --------------------------------------------------------------------------- cross-file base classes

def _collect_fact(root, source: bytes, rel: str, repo_root) -> list | None:
    """Each named class of the file: ``(name, kind, parent, url, urlRoot)``."""
    found = [(c.name, c.kind, c.parent.rsplit(".", 1)[-1], c.string("url"), c.string("urlRoot"))
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

def _detect_backbone(dc: DetectContext) -> Detection | None:
    root, src = dc.root, dc.source
    classes = _collect_classes(root, src, index_fact(dc.ctx.resolution_index, _FACT))
    by_name = {c.name: c for c in classes if c.name}
    by_node = {(c.node.start_byte, c.node.end_byte, c.node.type): c for c in classes}
    for node in _walk(root):  # ``Todo.prototype.urlRoot = '/api'``
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            m = re.fullmatch(r"([\w$.]+)\.prototype\.(\w+)", _text(left, src)) if left is not None else None
            cls = by_name.get(m.group(1).rsplit(".", 1)[-1]) if m else None
            value = node.child_by_field_name("right")
            if cls is not None and value is not None:
                cls.props[m.group(2)] = value
    instances = _collect_instances(root, src, by_name)
    base = _history_base(root, src)
    constants = _collect_constants(root, src)

    def enclosing(node) -> _Class | None:
        parent = node.parent
        while parent is not None:
            hit = by_node.get((parent.start_byte, parent.end_byte, parent.type))
            if hit is not None:
                return hit
            parent = parent.parent
        return None

    def resolve(objtext: str, node) -> _Target | None:
        encl = enclosing(node)
        if objtext == "this":
            return _Target(encl.kind, encl, False) if encl else None
        if objtext in instances:
            return instances[objtext]
        if objtext in ("this.model", "this.collection") and encl:
            ref = encl.props.get(objtext[len("this."):])
            cls = by_name.get(_text(ref, src)) if ref is not None and ref.type == "identifier" else None
            return _Target(cls.kind, cls, False) if cls else None
        return None

    found: list[Emit] = []
    for cls in classes:
        if cls.kind == "View":
            found.append(Emit(cls.node, {"uiRole": "view", "framework": _FRAMEWORK}, enrich="starts"))
        elif cls.kind == "Router":
            found.extend(_routes_of(cls, base, src, constants))

    for node in _walk(root):
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            continue
        prop = _text(fn.child_by_field_name("property"), src)
        obj = _unwrap(fn.child_by_field_name("object"))
        objtext = _text(obj, src) if obj is not None else ""
        if obj is not None and obj.type == "new_expression" and objtext not in instances:
            made = _new_target(obj, src, by_name)  # ``new Todo({ id }).fetch()``
            if made is not None:
                instances[objtext] = made
        args = _args(node)
        if prop == "route" and args:
            target = resolve(objtext, node)
            if target is not None and target.kind == "Router":
                emit = _route_call(node, args, target.cls, base, src, constants)
                if emit is not None:
                    found.append(emit)
        elif prop in ("fetch", "save", "destroy", "create", "sync"):
            emit = _api_call(node, prop, objtext, args, resolve, src)
            if emit is not None:
                found.append(emit)

    return Detection(found, label=_FRAMEWORK) if found else None


register_fact(FactSpec(name=_FACT, language="typescript", gate=(b"ackbone",),
                       collect=_collect_fact, resolve=_resolve_fact))

register_detector(Detector(
    name="backbone",
    gate=Gate("typescript", order=_ORDER,
              guard=b"ackbone",  # matches both ``Backbone`` and ``from 'backbone'``
              skip_fixtures=True),
    detect=_detect_backbone,
    frameworks=(_FRAMEWORK,),
))
