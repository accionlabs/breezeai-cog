"""Play controller action detection and SIRD routing.

A Play action is a ``Function`` whose body is a call rooted at ``Action`` —
``Action { ... }`` / ``Action.async { ... }`` / ``Action(parse.json) { ... }`` /
``Action.async(parse.json) { ... }``. The verb and path live in ``conf/routes`` (see
``PlayRoutesParser`` in :mod:`.routes`), which records the entry point with the action as its
handler; the action itself gets no record.

Play SIRD (String Interpolating Routing DSL) defines routes directly in code:
``override def routes: Routes = { case GET(p"/posts/$id") => postController.show(id) }``.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from tree_sitter import Node

from ...schemas import FileRecord, Function
from ..emits import Emit
from ..handler_sites import handler_location, locate_method
from ..scala.imports import ScalaIndex, _package_of, _resolve, extract_imports, visible_names
from ..scala.statements import find_enclosing_parent_id
from ..treesitter import node_text
from .includes import join, router_prefix

_FN_TYPES = ("function_definition", "function_declaration")


def _callee_root(node: Node | None) -> Node | None:
    """The leftmost identifier of a (possibly curried / field-access) call chain:
    ``Action.async(parse.json)`` -> the ``Action`` identifier."""
    if node is None:
        return None
    if node.type == "identifier":
        return node
    if node.type == "field_expression":
        return _callee_root(node.child_by_field_name("value"))
    if node.type in ("call_expression", "generic_function"):
        return _callee_root(node.child_by_field_name("function"))
    return None


def _is_action_call(body: Node | None, source: bytes) -> bool:
    if body is None or body.type != "call_expression":
        return False
    root = _callee_root(body.child_by_field_name("function"))
    return root is not None and node_text(root, source) == "Action"


def _is_play_action(fn_node: Node, source: bytes) -> bool:
    body = fn_node.child_by_field_name("body")
    if _is_action_call(body, source):
        return True
    ret_node = fn_node.child_by_field_name("return_type")
    if ret_node is None:
        return False
    ret = node_text(ret_node, source)
    return ret == "Action" or ret.startswith("Action[")


def detect_play_actions(root: Node, source: bytes, record: FileRecord) -> list[Function]:
    """The controller functions that are Play actions (``def index = Action { … }``).

    An action is the **handler** of a binding, not a binding: its verb and path live in
    ``conf/routes`` (or a SIRD case), which records the entry point and names the action as its
    handler. So no statement is emitted for an action — a second record with no address would
    count each binding twice. The caller uses the result to label the file ``play``."""
    if b"Action" not in source:
        return []
    action_lines: set[int] = set()

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type in _FN_TYPES and _is_play_action(c, source):
                action_lines.add(c.start_point[0] + 1)
            walk(c)

    walk(root)
    by_start = {fn.startLine: fn for fn in record.functions}
    return [by_start[line] for line in sorted(action_lines) if line in by_start]


_SIRD_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
_SIRD_REGEX = re.compile(r"\$[^/]+<[^>]+>")


def _sird_path(node: Node, source: bytes) -> tuple[str, bool] | None:
    """Return the normalized path body and isRegex flag of a ``p"..."`` SIRD interpolator."""
    # B6: Must verify the interpolator prefix is specifically 'p'
    prefix = next((child for child in node.children if child.type == "identifier"), None)
    if prefix is None or node_text(prefix, source) != "p":
        return None
    literal = next(
        (child for child in node.named_children if child.type == "interpolated_string"),
        None,
    )
    raw = node_text(literal or node, source)
    if raw.startswith('"""') and raw.endswith('"""'):
        raw = raw[3:-3]
    elif raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    else:
        return None

    is_regex = bool(_SIRD_REGEX.search(raw))
    # C3: Normalize SIRD path parameters to {param}
    norm = re.sub(r"\$\{?([a-zA-Z_]\w*)\}?<[^>]+>", r"{\1}", raw)
    norm = re.sub(r"\$\{?([a-zA-Z_]\w*)\}?", r"{\1}", norm)
    norm = re.sub(r"\*([a-zA-Z_]\w*)", r"{\1}", norm)
    return norm, is_regex


def _router_fqn(node: Node, source: bytes) -> str | None:
    """``package.Name`` of the class / object enclosing ``node`` (nested: ``package.Outer.Inner``)."""
    names: list[str] = []
    in_block_package = False  # `package a.b { … }` — else the package is a `package a.b` header
    curr, root = node.parent, node
    while curr is not None:
        if curr.type in ("class_definition", "object_definition", "package_clause"):
            name = curr.child_by_field_name("name")
            if name is None:
                return None
            names.append(node_text(name, source).strip())
            in_block_package |= curr.type == "package_clause"
        root, curr = curr, curr.parent
    package = "" if in_block_package else _package_of(root, source)
    return ".".join(([package] if package else []) + names[::-1]) or None


def _find_router_prefix(node: Node, source: bytes) -> str | None:
    """Find ``val prefix = "/v1/posts"`` on the router class/object."""
    curr = node.parent
    while curr is not None:
        if curr.type in ("class_definition", "object_definition"):
            body = curr.child_by_field_name("body")
            if body is not None:
                for member in body.named_children:
                    if member.type in ("val_definition", "var_definition"):
                        pat = member.child_by_field_name("pattern")
                        val = member.child_by_field_name("value")
                        if pat is not None and node_text(pat, source) == "prefix" and val is not None:
                            if val.type == "string":
                                return node_text(val, source).strip('"\'')
            break
        curr = curr.parent
    return None


def _sird_body(clause: Node) -> Node | None:
    """The expression after ``=>`` in a SIRD case."""
    body = clause.child_by_field_name("body")
    if body is not None:
        return body
    found_arrow = False
    for c in clause.children:
        if c.type == "=>":
            found_arrow = True
        elif found_arrow and c.is_named:
            return c
    return None


def _extract_sird_handler(clause: Node, source: bytes) -> str | None:
    """Extract handler name from SIRD case body: ``case GET(p"/") => controller.index`` -> ``index``."""
    body = _sird_body(clause)
    if body is None:
        return None
    if body.type == "call_expression":
        fn = body.child_by_field_name("function")
        if fn is not None:
            return node_text(fn, source).rsplit(".", 1)[-1]
    if body.type == "field_expression":
        field = body.child_by_field_name("field")
        if field is not None:
            return node_text(field, source)
        return node_text(body, source).rsplit(".", 1)[-1]
    if body.type == "identifier":
        return node_text(body, source)
    return None


def _sird_receiver(clause: Node, source: bytes) -> str | None:
    """``controller`` in ``controller.index`` / ``controller.show(id)``; None for any other shape."""
    body = _sird_body(clause)
    if body is not None and body.type == "call_expression":
        body = body.child_by_field_name("function")
    if body is None or body.type != "field_expression":
        return None
    value = body.child_by_field_name("value")
    return node_text(value, source) if value is not None and value.type == "identifier" else None


_SCOPES = ("class_definition", "object_definition", "trait_definition")


def _declared_types(scope: Node, source: bytes) -> dict[str, set[str]]:
    """Every type each name is declared with inside ``scope`` (parameters, vals, vars)."""
    found: dict[str, set[str]] = {}
    stack = [scope]
    while stack:
        node = stack.pop()
        name = type_node = None
        if node.type in ("val_definition", "var_definition"):
            name, type_node = node.child_by_field_name("pattern"), node.child_by_field_name("type")
        elif node.type in ("parameter", "class_parameter"):
            name, type_node = node.child_by_field_name("name"), node.child_by_field_name("type")
        if name is not None and name.type == "identifier" and type_node is not None:
            written = node_text(type_node, source).split("[", 1)[0].strip()
            found.setdefault(node_text(name, source), set()).add(written)
        stack.extend(node.named_children)
    return found


class _ControllerSites:
    """Where a SIRD case's controller method is declared — or None whenever that isn't certain.

    The receiver's type is read from the router class alone (two routers in one file may each
    have a ``controller``), and must be the only type that name is declared with there. The type
    resolves as Scala resolves it: fully qualified; else an explicit import; else the one class
    of that name among the file's package and its wildcard imports (a wildcard import outranks
    another file of the same package, so two candidates are ambiguous). The method is then
    looked up in ``scala.method-lines`` within that one file."""

    def __init__(self, root: Node, source: bytes, path: str, repo_root: Path | None,
                 index: Any | None) -> None:
        self._index = index
        self._fqcn = index.fqcn if isinstance(index, ScalaIndex) else None
        self._bindings = extract_imports(root, source, path, repo_root or Path("."), self._fqcn)[3]
        self._visible = visible_names(root, source)
        self._scopes: dict[tuple[int, int], dict[str, set[str]]] = {}

    def _type_in(self, scope: Node, receiver: str, source: bytes) -> str | None:
        key = (scope.start_byte, scope.end_byte)
        if key not in self._scopes:
            self._scopes[key] = _declared_types(scope, source)
        types = self._scopes[key].get(receiver, set())
        return next(iter(types)) if len(types) == 1 else None

    def _file_of(self, written: str) -> str | None:
        if "." in written:
            return _resolve(written, self._fqcn)
        if written in self._visible.names:
            return self._bindings.get(written)
        candidates = {self._bindings.get(written)} | {
            _resolve(f"{package}.{written}", self._fqcn) for package in self._visible.packages
        }
        candidates.discard(None)
        return candidates.pop() if len(candidates) == 1 else None

    def find(self, scope: Node, source: bytes, receiver: str | None,
             method: str | None) -> tuple[str, int] | None:
        written = self._type_in(scope, receiver, source) if receiver else None
        file = self._file_of(written) if written else None
        if written is None or file is None:
            return None
        return locate_method(self._index, "scala", written.rsplit(".", 1)[-1], method,
                             within={file})


def _router_scope(node: Node, root: Node) -> Node:
    """The class / object / trait a ``routes`` method belongs to (the file for a script)."""
    current = node.parent
    while current is not None and current.type not in _SCOPES:
        current = current.parent
    return current or root


def detect_play_sird_routes(
    root: Node, source: bytes, record: FileRecord, *, index: Any | None = None,
    repo_root: Path | None = None,
) -> list[Emit]:
    """Detect Play SIRD ``Routes`` methods (``case GET(p"/path") => ...``). The router's prefix
    is the one a routes file includes it under (``-> /v1/posts v1.post.PostRouter``, from the
    Scala index); failing that, its own ``val prefix``."""
    # D2: cheap byte guard
    if b"p\"" not in source and b"Routes" not in source and b"SimpleRouter" not in source:
        return []
    routes: list[Emit] = []
    sites: _ControllerSites | None = None

    def walk(node: Node) -> None:
        nonlocal sites
        if node.type in _FN_TYPES:
            name = node.child_by_field_name("name")
            ret = node.child_by_field_name("return_type")
            body = node.child_by_field_name("body")
            # C4: Optional return type (: Routes or inferred on override)
            if (
                name is not None
                and node_text(name, source) == "routes"
                and (ret is None or node_text(ret, source).strip() == "Routes")
                and body is not None
                and body.type == "case_block"
            ):
                fqn = _router_fqn(node, source)
                included = router_prefix(index, fqn) if fqn is not None else None
                # C5: Mount prefix from class
                mount_prefix = None if included is not None else _find_router_prefix(node, source)
                for clause in body.named_children:
                    if clause.type != "case_clause":
                        continue
                    pattern = clause.child_by_field_name("pattern")
                    if pattern is None or pattern.type != "case_class_pattern":
                        continue
                    children = list(pattern.named_children)
                    if len(children) < 2:
                        continue
                    verb = node_text(children[0], source).upper()
                    path_node = children[1]
                    if verb not in _SIRD_VERBS or path_node.type != "interpolated_string_expression":
                        continue
                    res = _sird_path(path_node, source)
                    if res is None:
                        continue
                    endpoint, is_regex = res
                    if not endpoint.startswith("/"):
                        endpoint = "/" + endpoint
                    if included:
                        endpoint = join(included, endpoint)
                    elif mount_prefix:
                        p = "/" + mount_prefix.strip("/")
                        endpoint = p + ("/" if endpoint == "/" else endpoint)
                    # C6: Populate handler from case body
                    handler = _extract_sird_handler(clause, source)
                    if sites is None:
                        sites = _ControllerSites(root, source, record.path, repo_root, index)
                    line, path = handler_location(
                        sites.find(_router_scope(node, root), source,
                                   _sird_receiver(clause, source), handler),
                        record.path,
                    )
                    routes.append(
                        Emit(clause, dict(
                            semanticType="route",
                            framework="play",
                            handler=handler,
                            handlerLine=line,
                            handlerPath=path,
                            method=verb,
                            endpoint=endpoint,
                            routeKind="route",
                            isRegex=is_regex,
                        ), parent=find_enclosing_parent_id(clause.start_point[0] + 1, record),
                            node_type="synthetic", text=node_text(clause, source))
                    )
        for child in node.named_children:
            walk(child)

    walk(root)
    return routes
