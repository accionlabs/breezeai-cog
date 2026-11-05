"""Struts2ConfigParser — a Struts2 configuration file (``struts.xml`` and every file it
includes) is a routes file.

Each ``<action>`` of a ``<package>`` is a **binding**: one ``route`` record on the ``<action>``
element, ``method`` ``ANY`` (an action answers every verb), ``endpoint``
``<namespace>/<name><extension>`` (:mod:`.common`), ``handler`` the action method's name
(``method`` defaults to ``execute``), like every other framework's route; the class is reached
through ``handlerPath`` / ``handlerLine``. The record leads to the action method in its Java source:
the class is looked up under the source roots next to the configuration (``src/main/java``,
``src``, ``JavaSource``, …), and the method's declaration line read from that file.

Honest-null: a wildcard class or method (``{1}``) names no handler; an action with no ``class``
runs the framework's ``ActionSupport`` and names none either; a Spring bean id (``class="login"``)
names its method but is not located (the bean id stays in the record's ``text``, as does a
class that is not in the repository); a placeholder extension or namespace leaves the endpoint
empty. A wildcard **name** (``user_*``) is kept in the endpoint as written: it is the address
pattern the action answers. Dynamic method invocation (``login!save.action``) adds no records.

Each ``<result>`` of an action, and of a package's ``<global-results>``, is **navigation**
(:mod:`.views`): a ``route`` record with ``routeKind: navigation`` on the ``<result>`` element,
``name`` = the result name (default ``success``), ``endpoint`` = the view path or the target
action's address. A view file that exists is also an ``importFiles`` edge of the configuration,
so the configuration links to every view its actions render. A package extending
``tiles-default`` (Tiles definitions, not files) gives no untyped-result navigation.

The file is still a configuration file: the record is the one :class:`~..config.parser.ConfigParser`
builds, so it keeps its ``metadata``; it only adds the routes, ``framework`` and the
``<include file>`` and view edges. It outranks ``config`` only for XML whose root is ``<struts>``.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..base import BaseParser, ParseContext
from ..config.parser import ConfigParser
from ..handler_sites import method_sites
from ..treesitter import parse_source
from ..treesitter import node_text
from ..xml_common import child_elements, document_root, element_attr, element_tag
from .common import (
    FRAMEWORK, PROPERTIES_LEVEL, STRUTS_ROOT, WEB_XML_LEVEL, XML_LEVEL, Layered, action_endpoint,
    ActionTable, bare_of, namespace_key, read_properties, suffix_of, web_xml_constants,
    xml_constants,
)
from .views import Target, result_target, web_root

#: Markers that make a ``<struts>`` document a Struts2 configuration.
_CONTENT = (b"<package", b"<include", b"<constant")
#: Where Java sources live relative to a module, legacy layouts included.
_SOURCE_ROOTS = ("src/main/java", "src/java", "src", "java", "JavaSource", "source",
                 "WEB-INF/src", "")


#: Builds the configuration record this parser adds the routes to.
_CONFIG = ConfigParser()


class Struts2ConfigParser(BaseParser):
    name = "java-struts2"
    extensions: tuple[str, ...] = (".xml",)
    priority = 10  # outranks `config` (0) for a Struts2 configuration only
    frameworks = [FRAMEWORK]
    statement_types = ["element"]

    def matches(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() == ".xml"

    def claims(self, path: str, source: bytes) -> bool:
        return STRUTS_ROOT.search(source) is not None and any(m in source for m in _CONTENT)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        record = _CONFIG.parse_file(ctx)
        root = parse_source("xml", ctx.source, ctx.parse_timeout_micros).root_node
        doc = document_root(root)
        if doc is None or element_tag(doc, ctx.source) != "struts":
            return record
        source, path = ctx.source, ctx.path
        children = child_elements(doc)
        for inc in (c for c in children if element_tag(c, source) == "include"):
            included = _sibling(ctx.repo_root, path, element_attr(inc, source, "file"))
            if included is not None and included not in record.importFiles:
                record.importFiles.append(included)
        # Routes and navigation are statements (gated); view edges are core and always kept.
        emit = ctx.capture_statements and not self.is_fixture_file(path)
        suffix, bare = _suffix(ctx.repo_root, path, source)
        web = web_root(ctx.repo_root, posixpath.dirname(path))
        handlers = _Handlers(ctx)
        seen: set[str] = set()
        limit = ctx.statement_text_limit
        packages = [c for c in children if element_tag(c, source) == "package"]
        declared = ActionTable(_declared_actions(packages, source) if bare else ())
        for package in packages:
            namespace: str | None = element_attr(package, source, "namespace") or ""
            if namespace is not None and "${" in namespace:
                namespace = None
            tiles = "tiles" in (element_attr(package, source, "extends") or "")
            for child in child_elements(package):
                tag = element_tag(child, source)
                if tag == "action":
                    if emit:
                        stmt = _route(child, source, path, record.id, seen, namespace, suffix,
                                      handlers, limit)
                        if stmt is not None:
                            record.statements.append(stmt)
                elif tag != "global-results":
                    continue
                for result in child_elements(child):
                    if element_tag(result, source) != "result":
                        continue
                    target = _result(result, source, namespace, suffix, ctx.repo_root, web, tiles)
                    if target is None:
                        continue
                    target = _bare_redirect(target, declared, suffix)
                    if target.file is not None and target.file not in record.importFiles:
                        record.importFiles.append(target.file)
                    if emit and target.endpoint is not None:
                        record.statements.append(_statement(
                            result, source, path, record.id, seen, limit,
                            semanticType="route", routeKind="navigation", framework=FRAMEWORK,
                            name=element_attr(result, source, "name") or "success",
                            endpoint=target.endpoint))
        if record.statements or record.importFiles:
            record.framework = FRAMEWORK
        return record


def _route(action: Node, source: bytes, path: str, parent: str, seen: set[str],
           namespace: str | None, suffix: str | None, handlers: _Handlers,
           limit: int) -> Statement | None:
    name = element_attr(action, source, "name")
    if not name:
        return None
    cls = element_attr(action, source, "class")
    method = element_attr(action, source, "method") or "execute"
    handler = method if cls and "{" not in cls and "{" not in method else None
    line, handler_path = handlers.locate(cls, method) if handler else (None, None)
    return _statement(
        action, source, path, parent, seen, limit,
        semanticType="route",
        framework=FRAMEWORK,
        method="ANY",
        endpoint=action_endpoint(namespace, name, suffix),
        handler=handler,
        handlerLine=line,
        handlerPath=handler_path if handler_path != path else None,
        routeKind="route",
    )


def _statement(node: Node, source: bytes, path: str, parent: str, seen: set[str], limit: int,
               **fields: object) -> Statement:
    """A record on an element; its text is the element's start tag."""
    open_tag = next((c for c in node.named_children if c.type in ("STag", "EmptyElemTag")), node)
    text = node_text(open_tag, source)
    start = node.start_point[0] + 1
    return Statement(
        id=disambiguate(statement_id(path, start, node.start_point[1]), seen),
        parentId=parent,
        nodeType=node.type,
        text=text if len(text) <= limit else text[:limit],
        startLine=start,
        endLine=node.end_point[0] + 1,
        path=path,
        **fields,  # type: ignore[arg-type]
    )


def _result(result: Node, source: bytes, namespace: str | None, suffix: str | None,
            repo_root: Path, web: str | None, tiles: bool) -> Target | None:
    """Where a ``<result>`` leads. Its location is the ``location`` / ``actionName`` param, else
    the element's text."""
    kind = element_attr(result, source, "type") or ("tiles" if tiles else "dispatcher")
    params = {element_attr(p, source, "name"): _text(p, source)
              for p in child_elements(result) if element_tag(p, source) == "param"}
    location = params.get("location") or params.get("actionName") or _text(result, source)
    target_ns = params.get("namespace")
    return result_target(kind, location, namespace, target_ns, suffix, repo_root, web)


def _text(node: Node, source: bytes) -> str:
    """An element's own character data (CDATA included), trimmed."""
    content = next((c for c in node.named_children if c.type == "content"), None)
    if content is None:
        return ""
    parts = []
    for c in content.named_children:
        if c.type == "CharData":
            parts.append(node_text(c, source))
        elif c.type == "CDSect":
            data = next((d for d in c.named_children if d.type == "CData"), None)
            parts.append(node_text(data, source) if data is not None else "")
    return "".join(parts).strip()


def _sibling(repo_root: Path, path: str, ref: str | None) -> str | None:
    """An ``<include file>`` beside the including file (both are on the classpath root)."""
    if not ref or "${" in ref:
        return None
    rel = posixpath.normpath(posixpath.join(posixpath.dirname(path), ref.lstrip("/")))
    if rel.startswith("../") or rel == path:
        return None
    return rel if (repo_root / rel).is_file() else None


def _declared_actions(packages: list[Node], source: bytes) -> tuple[tuple[str, str], ...]:
    """``(namespace_key, name or pattern)`` of every action this configuration declares."""
    out = []
    for package in packages:
        namespace = element_attr(package, source, "namespace") or ""
        if "${" in namespace:
            continue
        for action in child_elements(package):
            name = element_attr(action, source, "name")
            if element_tag(action, source) == "action" and name and "{" not in name:
                out.append((namespace_key(namespace), name))
    return tuple(out)


def _bare_redirect(target: Target, declared: ActionTable, suffix: str | None) -> Target:
    """A redirect written without an extension (``bookList``) that names an action declared
    here leads to that action's address (``/bookList.action``); anything else is kept."""
    if not declared or target.endpoint is None:
        return target
    namespace, name = posixpath.split(target.endpoint)
    if not name or "." in name:
        return target
    serving = declared.serving(namespace, name)
    if serving is None:
        return target
    return Target(action_endpoint(serving, name, suffix), target.file)


def _suffix(repo_root: Path, path: str, source: bytes) -> tuple[str | None, bool]:
    """``(suffix, bare)``: the action suffix for this configuration's actions, from
    ``struts.action.extension`` as
    Struts resolves it (:class:`.common.Layered`): ``<constant>`` in this file and in the
    classpath root's ``struts.xml`` (the nearest one at or above it), overridden by
    ``struts.properties`` there, overridden by the Struts filter's init-param in the web
    application's ``web.xml``."""
    layered = Layered()
    layered.add("", XML_LEVEL, xml_constants(source))
    here = (repo_root / path).parent
    classpath = here
    for directory in (here, *here.parents):
        main = directory / "struts.xml"
        if main.is_file():
            classpath = directory
            if main != repo_root / path:
                layered.add("", XML_LEVEL, xml_constants(_read(main)))
            break
        if directory == repo_root:
            break
    layered.add("", PROPERTIES_LEVEL, read_properties(classpath))
    web = web_root(repo_root, posixpath.dirname(path))
    if web is not None:
        layered.add("", WEB_XML_LEVEL, web_xml_constants(_read(repo_root / web / "WEB-INF" / "web.xml")))
    settings = layered.resolve().get("", {})
    return suffix_of(settings), bare_of(settings)


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


class _Handlers:
    """Action class → its Java source and method lines, read once per class."""

    def __init__(self, ctx: ParseContext) -> None:
        self.ctx = ctx
        self.files: dict[str, str | None] = {}
        self.sites: dict[str, dict[tuple[str, str], list[int]]] = {}

    def locate(self, cls: str | None, method: str) -> tuple[int | None, str | None]:
        """``(handlerLine, handlerPath)`` of ``cls.method``, or ``(None, None)``."""
        if not cls:
            return None, None
        fqcn = cls.split("$", 1)[0]
        if fqcn not in self.files:
            self.files[fqcn] = self._source_of(fqcn)
        rel = self.files[fqcn]
        if rel is None:
            return None, None
        if rel not in self.sites:
            self.sites[rel] = self._method_lines(rel)
        lines = self.sites[rel].get((cls.replace("$", ".").rsplit(".", 1)[-1], method), [])
        return (lines[0], rel) if len(lines) == 1 else (None, None)

    def _source_of(self, fqcn: str) -> str | None:
        """The nearest module source root (walking up from the configuration) holding
        ``<fqcn>.java``; None when not found or when two roots at that level hold it."""
        if "." not in fqcn:
            return None  # a Spring bean id, not a class
        relative = fqcn.replace(".", "/") + ".java"
        repo = self.ctx.repo_root
        here = Path(posixpath.dirname(self.ctx.path))
        for directory in (here, *here.parents):
            hits = {
                posixpath.normpath(posixpath.join(directory.as_posix(), root, relative))
                for root in _SOURCE_ROOTS
                if (repo / directory / root / relative).is_file()
            }
            if hits:
                return hits.pop() if len(hits) == 1 else None
        return None

    def _method_lines(self, rel: str) -> dict[tuple[str, str], list[int]]:
        try:
            source = (self.ctx.repo_root / rel).read_bytes()
            root = parse_source("java", source, self.ctx.parse_timeout_micros).root_node
        except (OSError, ValueError):
            return {}
        out: dict[tuple[str, str], list[int]] = {}
        for key, (_, line) in method_sites("java", root, source, rel):
            out.setdefault(key, []).append(line)
        return out
