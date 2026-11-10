"""Additive detector: Struts2 Convention-plugin actions in Java are ``route`` bindings.

An additive detector rather than a selecting parser: an action class is often also a Spring
bean, and a ``java-struts2`` parser would either lose to ``java-springboot`` (same priority,
alphabetical order) or hide its Spring records (higher priority). Running inside whichever Java
parser owns the file keeps both.

========================================  ==================================================
Construct                                 Record
========================================  ==================================================
``@Action("x")`` on a method              ``route`` on the annotation; handler = the method
``@Actions({@Action("a"), @Action("b")})``  one ``route`` per inner ``@Action``
``@Action`` / ``@Actions`` on the class   one ``route`` each; handler = ``execute``
a Convention action class                 one ``route`` for ``execute`` at the Convention
                                          name (``UserProfileAction`` → ``user-profile``),
                                          unless the class or its ``execute`` has an ``@Action``
a Convention class under the REST plugin  one ``route`` per REST method it declares, with its
(``struts.mapper.class=rest``)            verb: ``index`` ``GET /x``, ``show`` ``GET /x/{id}``,
                                          ``create`` ``POST /x``, ``update`` ``PUT /x/{id}``,
                                          ``destroy`` ``DELETE /x/{id}``, ``edit``
                                          ``GET /x/{id}/edit``, ``editNew`` ``GET /x/new``
========================================  ==================================================

Every route is ``method`` ``ANY``, ``routeKind`` ``route``, ``framework`` ``struts2``. The
address is ``<namespace>/<action><extension>`` (:mod:`.common`); the namespace comes from the
class's ``@Namespace`` / ``@Namespaces`` (one record per namespace), else its package's
``package-info.java``, else the Convention rule (the package segments after a package locator).
An action name starting with ``/`` is absolute.

The Convention plugin maps a class's ``execute`` at the Convention name when its module's build
declares the plugin, its package holds a locator (``action``, ``actions``, ``struts``,
``struts2``, or the configured ones), it ends in ``Action`` (or the configured suffix) or extends
``ActionSupport`` / implements ``Action``, it is not abstract, it has ``execute`` (declared, or
inherited: it extends a class; or ``mapAllMatches``), and neither the class nor its ``execute``
carries an ``@Action`` — an ``@Action`` on another method keeps the default mapping
(``register.action`` → ``execute`` beside ``@Action("register-input") input()``).

Results are **navigation** (:mod:`.views`) — a ``route`` with ``routeKind: navigation``,
``name`` = the result name, ``endpoint`` = the view path or the target action:

* an explicit ``@Result`` (in ``@Action(results = …)``, or ``@Result`` / ``@Results`` on the
  class) is a record on that annotation; a relative ``location`` is under the Convention result
  path (``/WEB-INF/content/``, or ``struts.convention.result.path``) and the action's namespace;
* an action with no explicit result gets the Convention plugin's implicit ones: each view file
  in ``<web root>/WEB-INF/content/<namespace>/`` named ``<action>.<ext>`` or
  ``<action>-<result>.<ext>``, a record on the action's own construct.

Honest-null: an ``@Action`` with no value or a non-literal value, a class outside any locator
package with no ``@Namespace``, and an extension two configurations disagree on all leave the
endpoint empty; the record (and its handler) is still written.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path

from tree_sitter import Node

from ...schemas import Function

from ..additive import DetectContext, Detection, Detector, Gate, index_fact, register_detector
from ..emits import Emit
from ..treesitter import node_text
from .common import (
    DEFAULT_LOCATORS, DEFAULT_SUFFIX, FRAMEWORK, LOCATORS_KEY, MAP_ALL_KEY, MAPPER_KEY,
    RESULT_PATH_KEY, SUFFIX_KEY, ActionTable, action_endpoint, bare_of, convention_name,
    convention_namespace, namespace_key, suffix_of,
)
from .facts import (
    CONVENTION, PACKAGE_NAMESPACES, SETTINGS, annotation_name, annotation_values,
    module_settings, namespaces_of, string_value,
)
from .views import CONVENTION_RESULT_PATH, VIEW_EXTENSIONS, VIEW_TYPES, result_target, web_root

#: The Convention annotations' package (Struts 2.1 onwards, unchanged in 7.x).
_ANNOTATIONS = b"org.apache.struts2.convention.annotation"
#: Packages an action base class comes from: XWork (2.0–6.x) and Struts 7.
_BASES = (b"com.opensymphony.xwork2", b"org.apache.struts2")
_ACTION_BASES = frozenset({"ActionSupport", "Action"})
#: The REST plugin's methods: (method, HTTP verb, path after the action).
_REST = (("index", "GET", ""), ("show", "GET", "/{id}"), ("create", "POST", ""),
         ("update", "PUT", "/{id}"), ("destroy", "DELETE", "/{id}"),
         ("edit", "GET", "/{id}/edit"), ("editNew", "GET", "/new"))
_REST_MAPPERS = frozenset({"rest", "org.apache.struts2.rest.RestActionMapper"})


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    annotated = _ANNOTATIONS in source
    index = dc.ctx.resolution_index
    settings = module_settings(index_fact(index, SETTINGS), dc.path)
    # A Convention action may be a plain class with no Struts import (a POJO with `execute`, or
    # one extending such a class): in a module whose build declares the Convention plugin, the
    # class checks below decide. Elsewhere a file without a Struts import holds no action.
    if not annotated and not any(b in source for b in _BASES) \
            and settings.get(CONVENTION) != "true":
        return None
    package = _package(dc.root, source)
    rules = _Rules(settings, index_fact(index, PACKAGE_NAMESPACES) or {}, package)
    rules.web = web_root(dc.ctx.repo_root, posixpath.dirname(dc.path))
    rules.repo_root = dc.ctx.repo_root
    functions = {(f.name, f.startLine): f for f in dc.record.functions}

    emits: list[Emit] = []
    stack = [dc.root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type == "class_declaration":
            emits.extend(_class_routes(node, source, rules, functions, annotated))
    return Detection(emits, label=FRAMEWORK) if emits else None


class _Rules:
    """The address rules for one file: extension, locators, suffix, package namespaces."""

    def __init__(self, settings: dict[str, str | None],
                 packages: dict[str, tuple[str, ...] | None], package: str) -> None:
        self.suffix = suffix_of(settings)  # None: the module's sources disagree
        self.bare = bare_of(settings)  # actions also answer without the extension
        locators = settings.get(LOCATORS_KEY)
        self.locators = (tuple(x.strip() for x in locators.split(",") if x.strip())
                         if locators else DEFAULT_LOCATORS)
        self.action_suffix = settings.get(SUFFIX_KEY) or DEFAULT_SUFFIX
        self.convention = settings.get(CONVENTION) == "true"
        self.map_all = settings.get(MAP_ALL_KEY) == "true"
        self.rest = settings.get(MAPPER_KEY) in _REST_MAPPERS
        self.package = package
        self.package_namespaces = packages.get(package) if package in packages else ()
        self.convention_namespace = convention_namespace(package, self.locators)
        path = settings.get(RESULT_PATH_KEY) or CONVENTION_RESULT_PATH
        self.result_path = "/" + path.strip("/")
        self.web: str | None = None
        self.repo_root = Path(".")
        self._listings: dict[str, list[str]] = {}

    def result_base(self, namespace: str | None) -> str | None:
        """The namespace a relative ``@Result`` location resolves under."""
        return None if namespace is None else self.result_path + namespace.rstrip("/")

    def implicit_views(self, namespace: str | None, action: str) -> list[tuple[str | None, str]]:
        """``(result name, view path)`` of each Convention view file of an action."""
        if self.web is None or namespace is None or not action:
            return []
        folder = self.result_base(namespace) or ""
        found: list[tuple[str | None, str]] = []
        for name in self._listing(folder):
            stem, ext = posixpath.splitext(name)
            if ext.lower() not in VIEW_EXTENSIONS:
                continue
            if stem == action:
                found.append((None, f"{folder}/{name}"))
            elif stem.startswith(action + "-"):
                found.append((stem[len(action) + 1:], f"{folder}/{name}"))
        return found

    def _listing(self, folder: str) -> list[str]:
        if folder not in self._listings:
            directory = self.repo_root / (self.web or "") / folder.lstrip("/")
            try:
                self._listings[folder] = sorted(
                    e.name for e in os.scandir(directory) if e.is_file())
            except OSError:
                self._listings[folder] = []
        return self._listings[folder]

    def namespaces(self, declared: tuple[str, ...]) -> tuple[str | None, ...]:
        if declared:
            return declared
        if self.package_namespaces is None:
            return (None,)  # two package-info files disagree
        if self.package_namespaces:
            return self.package_namespaces
        return (self.convention_namespace,)


def _class_routes(cls: Node, source: bytes, rules: _Rules,
                  functions: dict[tuple[str, int], Function], annotated: bool) -> list[Emit]:
    name_node = cls.child_by_field_name("name")
    body = cls.child_by_field_name("body")
    if name_node is None or body is None:
        return []
    class_name = node_text(name_node, source)
    class_anns = _annotations(cls)
    methods = [m for m in body.named_children if m.type == "method_declaration"]
    namespaces = rules.namespaces(namespaces_of(class_anns, source) if annotated else ())
    execute = next((m for m in methods if _name(m, source) == "execute"
                    and not _has_params(m)), None)
    class_results = _results(class_anns, source) if annotated else []

    out: list[Emit] = []
    if annotated:
        for ann in _actions(class_anns, source):
            out.extend(_action_emits(ann, source, namespaces, rules, "execute", _line(execute),
                                     _fn_id(execute, source, functions), bool(class_results)))
        for method in methods:
            for ann in _actions(_annotations(method), source):
                out.extend(_action_emits(ann, source, namespaces, rules, _name(method, source),
                                         _line(method), _fn_id(method, source, functions),
                                         bool(class_results)))
    if (rules.convention and _convention_action(cls, source, class_name, rules)
            and not _actions(class_anns, source)
            and not (execute is not None and _actions(_annotations(execute), source))):
        action = convention_name(class_name, rules.action_suffix)
        pairs = list(dict.fromkeys((ns, action) for ns in namespaces))
        if rules.rest:
            out.extend(_rest_routes(methods, source, pairs, functions, name_node))
        elif (execute is not None or cls.child_by_field_name("superclass") is not None
              or rules.map_all):
            out.extend(Emit(name_node, _route(action_endpoint(ns, a, rules.suffix), "execute",
                                              _line(execute))) for ns, a in pairs)
            if not class_results:
                out.extend(_implicit(name_node, pairs, rules, None))
    if out:  # class-level results serve every action of the class
        for result in class_results:
            out.extend(_result_emits(result, source, namespaces, rules, None))
    if rules.bare:
        _bare_redirects(out, rules)
    return out


def _bare_redirects(emits: list[Emit], rules: _Rules) -> None:
    """A redirect written without an extension (``location = "list"``) that names one of the
    class's own actions leads to that action's address (``/list.action``)."""
    suffix = rules.suffix or ""
    declared = []
    for emit in emits:
        endpoint = emit.fields.get("endpoint")
        if emit.fields.get("routeKind") == "route" and isinstance(endpoint, str):
            base = endpoint[: -len(suffix)] if suffix and endpoint.endswith(suffix) else endpoint
            namespace, name = posixpath.split(base)
            declared.append((namespace_key(namespace), name))
    table = ActionTable(declared)
    for emit in emits:
        endpoint = emit.fields.get("endpoint")
        if emit.fields.get("routeKind") != "navigation" or not isinstance(endpoint, str):
            continue
        namespace, name = posixpath.split(endpoint)
        if name and "." not in name:
            serving = table.serving(namespace, name)
            if serving is not None:
                emit.fields["endpoint"] = action_endpoint(serving, name, rules.suffix)


def _action_emits(ann: Node, source: bytes, namespaces: tuple[str | None, ...], rules: _Rules,
                  handler: str, line: int | None, parent: str | None,
                  class_results: bool) -> list[Emit]:
    """The routes of one ``@Action`` and the navigation of its results."""
    values = annotation_values(ann, source)
    name = string_value(values[0], source) if len(values) == 1 else None
    pairs: list[tuple[str | None, str | None]]
    if name is not None and name.startswith("/"):  # absolute: its own namespace
        pairs = [(posixpath.dirname(name), posixpath.basename(name))]
    else:
        pairs = list(dict.fromkeys((ns, name) for ns in namespaces))
    out = [Emit(ann, _route(action_endpoint(ns, a, rules.suffix) if a else None, handler, line),
                parent=parent) for ns, a in pairs]
    explicit = _results([ann], source, key="results")
    for result in explicit:
        out.extend(_result_emits(result, source, tuple(ns for ns, _ in pairs), rules, parent))
    if not explicit and not class_results:
        out.extend(_implicit(ann, [(ns, a) for ns, a in pairs if a is not None], rules, parent))
    return out


def _result_emits(result: Node, source: bytes, namespaces: tuple[str | None, ...],
                  rules: _Rules, parent: str | None) -> list[Emit]:
    """One navigation record per distinct target of an explicit ``@Result``."""
    def attr(key: str) -> str | None:
        values = annotation_values(result, source, key)
        return string_value(values[0], source) if len(values) == 1 else None

    kind = attr("type") or "dispatcher"
    location = attr("location") or attr("value")
    targets = []
    for ns in namespaces:
        # Only a view lives under the Convention result folder; a redirect or an action is
        # reached from the action's own URL, so it resolves against the namespace.
        base = rules.result_base(ns) if kind in VIEW_TYPES else ns
        target = result_target(kind, location, base, None, rules.suffix, rules.repo_root,
                               rules.web)
        if target is not None and target.endpoint is not None:
            targets.append(target.endpoint)
    return [Emit(result, _navigation(attr("name") or "success", endpoint), parent=parent)
            for endpoint in dict.fromkeys(targets)]


def _implicit(node: Node, pairs: list[tuple[str | None, str]], rules: _Rules,
              parent: str | None) -> list[Emit]:
    """The Convention plugin's implicit results of an action: its view files."""
    out: list[Emit] = []
    for ns, action in pairs:
        for result, path in rules.implicit_views(ns, action):
            out.append(Emit(node, _navigation(result, path), parent=parent))
    return out


def _rest_routes(methods: list[Node], source: bytes, pairs: list[tuple[str | None, str]],
                 functions: dict[tuple[str, int], Function], class_name: Node) -> list[Emit]:
    """REST plugin routes: one per REST method the controller declares, with its verb; a
    controller declaring none still answers at its address (``ANY``, no handler)."""
    declared = {_name(m, source): m for m in methods if not _has_params(m)}
    out: list[Emit] = []
    for ns, action in pairs:
        base = action_endpoint(ns, action, "")
        found = [(declared[m], verb, sub) for m, verb, sub in _REST if m in declared]
        for method, verb, sub in found:
            out.append(Emit(method.child_by_field_name("name") or method, _route(
                base + sub if base is not None else None, _name(method, source), _line(method),
                verb=verb), parent=_fn_id(method, source, functions)))
        if not found:
            out.append(Emit(class_name, dict(_route(base, "", None), handler=None)))
    return out


def _route(endpoint: str | None, handler: str, line: int | None,
           verb: str = "ANY") -> dict[str, object]:
    return dict(semanticType="route", framework=FRAMEWORK, method=verb, endpoint=endpoint,
                handler=handler, handlerLine=line, routeKind="route")


def _navigation(name: str | None, endpoint: str | None) -> dict[str, object]:
    return dict(semanticType="route", routeKind="navigation", framework=FRAMEWORK, name=name,
                endpoint=endpoint)


def _results(annotations: list[Node], source: bytes, key: str = "value") -> list[Node]:
    """Each ``@Result`` among ``annotations`` — inside ``@Results``, or (``key="results"``)
    inside an ``@Action``'s ``results``."""
    out: list[Node] = []
    for ann in annotations:
        name = annotation_name(ann, source)
        if key == "value" and name == "Result":
            out.append(ann)
        elif (key == "value" and name == "Results") or (key == "results" and name == "Action"):
            out.extend(a for a in annotation_values(ann, source, key)
                       if annotation_name(a, source) == "Result")
    return out


def _convention_action(cls: Node, source: bytes, class_name: str, rules: _Rules) -> bool:
    """Would the Convention plugin map this class with no annotation?"""
    if rules.convention_namespace is None:
        return False
    modifiers = next((c for c in cls.named_children if c.type == "modifiers"), None)
    if modifiers is not None and b"abstract" in source[modifiers.start_byte:modifiers.end_byte]:
        return False
    if class_name.endswith(rules.action_suffix):
        return True
    heritage = [cls.child_by_field_name("superclass"), cls.child_by_field_name("interfaces")]
    names = {node_text(n, source).rsplit(".", 1)[-1]
             for h in heritage if h is not None for n in _type_names(h)}
    return bool(names & _ACTION_BASES)


def _type_names(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.type in ("type_identifier", "scoped_type_identifier"):
            out.append(n)
            continue
        stack.extend(n.named_children)
    return out


def _actions(annotations: list[Node], source: bytes) -> list[Node]:
    """Each ``@Action``, the inner ones of an ``@Actions`` included, in source order."""
    out: list[Node] = []
    for ann in annotations:
        name = annotation_name(ann, source)
        if name == "Action":
            out.append(ann)
        elif name == "Actions":
            out.extend(a for a in annotation_values(ann, source)
                       if annotation_name(a, source) == "Action")
    return out


def _annotations(decl: Node) -> list[Node]:
    modifiers = next((c for c in decl.named_children if c.type == "modifiers"), None)
    return [c for c in modifiers.named_children
            if c.type in ("annotation", "marker_annotation")] if modifiers is not None else []


def _package(root: Node, source: bytes) -> str:
    decl = next((c for c in root.named_children if c.type == "package_declaration"), None)
    name = next((c for c in decl.named_children
                 if c.type in ("scoped_identifier", "identifier")), None) if decl else None
    return node_text(name, source) if name is not None else ""


def _name(method: Node, source: bytes) -> str:
    name = method.child_by_field_name("name")
    return node_text(name, source) if name is not None else ""


def _has_params(method: Node) -> bool:
    params = method.child_by_field_name("parameters")
    return params is not None and bool(params.named_children)


def _line(method: Node | None) -> int | None:
    return method.start_point[0] + 1 if method is not None else None


def _fn_id(method: Node | None, source: bytes, functions: dict[tuple[str, int], Function]) -> str | None:
    if method is None:
        return None
    fn = functions.get((_name(method, source), method.start_point[0] + 1))
    return fn.id if fn is not None else None


# An entry-point emitter, so fixture/test files are skipped.
register_detector(Detector(
    name="struts2",
    gate=Gate("java", order=20, guard=None, skip_fixtures=True),
    detect=_detect, frameworks=(FRAMEWORK,),
))
