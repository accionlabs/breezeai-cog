"""JspParser — the language parser owning JSP pages, fragments and tag files.

Covers every JSP version, 1.0 through Jakarta Pages 4.0: standard and XML syntax, ``.jsp``,
``.jspx``, ``.jspf``, ``.tag`` and ``.tagx``, EL and deferred EL, legacy encodings. Nothing is
refused for its version; :mod:`.scanner` lists what is recognised and how.

A JSP file is markup with embedded Java, and no grammar covers it, so it is read in three layers
over one decoded source (offsets are shared, so every line and column is true):

* :mod:`.scanner` finds the JSP constructs: directives, JSP comments, scripting elements, EL.
* the ``html`` grammar parses the markup with the scripting elements blanked out: standard
  actions (``jsp:include``), JSTL and custom tags (any prefixed element), HTML comments.
* the ``java`` grammar parses only the scripting code: ``<%! %>`` methods and classes become
  Functions and Classes parented to the file (like a Razor ``@code`` block), and scriptlet code
  becomes Java statements, so a JDBC call in a scriptlet is a db call like anywhere else.

Edges (not gated by statement capture):

* ``importFiles``: ``<%@ include %>``, ``<jsp:include>``, ``<jsp:forward>``, JSTL
  ``<c:import>`` of a local resource, ``errorPage``, ``web.xml`` include preludes and codas,
  tag files of a ``tagdir`` taglib, in-repo ``.tld`` descriptors, and in-repo Java classes of
  ``page import``, ``extends`` and ``jsp:useBean``. Only literal paths that exist are linked.
* ``externalImports``: taglib URIs not in the repo (``jakarta.tags.core``,
  ``http://java.sun.com/jsp/jstl/core``, ``/struts-tags``) and classes not in the repo.

Navigation (a ``route`` with ``routeKind: navigation``, under statement capture, never in a
fixture) is recorded on the element that leads elsewhere:

* Struts2 tags (taglib ``/struts-tags``): ``<s:form action>``, ``<s:url action>``,
  ``<s:a action>``, ``<s:submit action>``, ``<s:action name>`` — ``framework: struts2``,
  ``endpoint`` = the action's address. The namespace is the tag's ``namespace``, else the one
  namespace whose actions render this page (``struts.xml`` results; a Convention view under
  ``WEB-INF/content/<namespace>/``); the extension is the configured one. A namespace that does
  not declare the action falls back to the default namespace, which answers any namespace, and
  an action only the default namespace declares needs no page namespace. A DMI suffix
  (``save!input``) is dropped: the address is the action's. A URL tag written inside another
  tag's attribute (``<a href="<s:url action='list'/>">``, which the html grammar cannot see) is
  found by a scan of the markup, for bound Struts / JSTL prefixes only.
* links: ``<a href>``, ``<form action>``, ``<jsp:forward page>``, JSTL ``<c:url value>`` /
  ``<c:redirect url>``, ``<s:url value>`` / ``<s:a href>`` — ``framework: jsp`` — when the target
  is an action (its extension, ``.action`` or ``.do``) or a page (``.jsp`` / ``.jspx``) — or,
  written without an extension (``href="bookList"``), when it names an action the module's
  ``struts.xml`` declares and its extension list allows a bare address (Struts' default
  ``action,,``): that action's address, so no address is guessed. A
  leading ``${pageContext.request.contextPath}`` / ``<%= request.getContextPath() %>`` is the
  application's own root and is dropped. A relative target resolves against the page's own URL
  for a page served directly; a page under ``WEB-INF`` is reached at the URL of the action that
  renders it, so its relative links resolve against that action's namespace.

A target that cannot be resolved — anything dynamic (other EL, OGNL ``%{}``, a scriptlet), a
Struts tag with no known namespace, a relative link on a page no single action renders — gives
no navigation record (absent beats wrong: a link is never written with an empty endpoint); a
prefixed tag still keeps its plain ``element`` record.

``language`` is ``jsp``; ``uiRole`` is ``component`` for a tag file and ``template`` otherwise.
A page served directly is an **entry point** (contract: a page reached by URL): a ``.jsp`` /
``.jspx`` inside a web application (the directory holding ``WEB-INF``, or with no ``WEB-INF`` a
conventional web folder: ``src/main/webapp``, ``WebContent``, ``WebRoot``, a NetBeans ``web/``)
and outside ``WEB-INF``
gets one ``route`` record, ``GET``, ``routeKind: page``, ``endpoint`` = its URL from the web
root (``/shop/index.jsp``), ``handler`` = the page itself (its file name, ``handlerLine`` 1).
A page under ``WEB-INF`` has no URL (an action or servlet renders it), a fragment (``.jspf``) and
a tag file are parts of pages, and a page outside any web application has no known address:
none of them is an entry point. Under statement capture, never in a fixture.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any, Sequence

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import SCHEMA_VERSION, Class, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..comments_common import comment_statements_for
from ..java.classes import build_class
from ..java.functions import build_method
from ..java.mappings import COMMENT_TYPES, CONTROL_FLOW
from ..java.statements import extract_statements
from ..java_web_guards import servlet_match
from ..java_struts2.common import ActionTable, action_endpoint, extension
from ..java_struts2.facts import module_key
from ..treesitter import node_text, parse_source
from .index import JspIndex, PropertyGroup, WebApp, build_jsp_index
from .mappings import EXTENSIONS, FRAMEWORKS, STATEMENT_TYPES, TAG_FILE_EXTENSIONS
from .scanner import (
    CODE_KINDS,
    Island,
    Lines,
    decode_source,
    directive_attrs,
    el_spans,
    scan_islands,
    shadow_java,
    shadow_markup,
)

_CLASS_TYPES = ("class_declaration", "interface_declaration", "enum_declaration",
                "record_declaration")
_METHOD_TYPES = ("method_declaration", "constructor_declaration")
#: JSTL core, every generation: 1.0 (and its ``_rt`` twin), 1.1/1.2/2.0, 3.0 (Jakarta).
_JSTL_CORE = frozenset({
    "http://java.sun.com/jstl/core", "http://java.sun.com/jstl/core_rt",
    "http://java.sun.com/jsp/jstl/core", "jakarta.tags.core",
})
#: A taglib bound in an XML-syntax document: ``xmlns:c="…"``.
_XMLNS = re.compile(rb"""\sxmlns:([\w.-]+)\s*=\s*["']([^"']*)["']""")
_TAGDIR_URN, _TLD_URN = "urn:jsptagdir:", "urn:jsptld:"
#: A URL with a scheme (``http:``, ``mailto:``) or protocol-relative: never a local file.
_EXTERNAL_URL = re.compile(r"^(?:[A-Za-z][\w+.-]*:|//)")
#: Bytes that make a tag file a JSP tag file (``.tag`` is also used by Riot.js components).
_JSP_MARKERS = (b"<%", b"jsp:", b"${", b"#{")
#: The Struts2 tag library.
_STRUTS_TAGS = "/struts-tags"
#: Struts2 tags whose ``action`` (``name`` for ``s:action``) names an action.
_STRUTS_ACTION_TAGS = {"form": "action", "url": "action", "a": "action", "submit": "action",
                       "action": "name"}
#: Link-bearing elements and their URL attribute.
_LINKS = {"a": "href", "form": "action", "jsp:forward": "page"}
_JSTL_LINKS = {"url": "value", "redirect": "url"}
_STRUTS_LINKS = {"url": "value", "a": "href"}
#: The application root written in front of an absolute link.
_CONTEXT_PATH = re.compile(
    r"^(?:\$\{\s*pageContext\.request\.contextPath\s*\}|<%=\s*request\.getContextPath\(\)\s*%>)")
_DYNAMIC = ("${", "#{", "%{", "<%")
#: A URL tag written anywhere, an attribute value included: ``<s:url action="x"/>``.
_URL_TAG = re.compile(rb"<([\w.-]+):(url|a)\b((?:[^<>\"']|\"[^\"]*\"|'[^']*')*)/?>")
_TAG_ATTR = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_ACTION_SUFFIXES = (".action", ".do")
_PAGE_SUFFIXES = (".jsp", ".jspx")


class JspParser(BaseParser):
    name = "jsp"
    extensions: tuple[str, ...] = EXTENSIONS
    # All markup: skipped at scan time unless --capture-templates, like the other view files.
    template_extensions: tuple[str, ...] = EXTENSIONS
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> JspIndex:
        return build_jsp_index(Path(repo_root))

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        src = decode_source(ctx.source)
        path = ctx.path.replace("\\", "/")
        fid = file_id(path)
        index = ctx.resolution_index if isinstance(ctx.resolution_index, JspIndex) else None
        tag_file = path.lower().endswith(TAG_FILE_EXTENSIONS)
        page = _Page(path, ctx.repo_root, index)
        lines = Lines(src)
        seen_ids: set[str] = set()
        statements: list[Statement] = []

        if ctx.capture_statements and not self.is_fixture_file(path):
            # First, so the page keeps the file's own `path:1:0` id.
            route = _page_route(src, path, fid, page, seen_ids, ctx.statement_text_limit)
            if route is not None:
                statements.append(route)
        islands = scan_islands(src)
        settings = page.read_directives(src, islands, statements if ctx.capture_statements else
                                        None, lines, fid, seen_ids, ctx.statement_text_limit)
        page.add_xml_taglibs(src)
        page.add_web_xml_includes(tag_file)

        markup = shadow_markup(src, islands)
        root = parse_source("html", markup, ctx.parse_timeout_micros).root_node
        page.navigation = not self.is_fixture_file(path)
        page.read_markup(root, src, statements if ctx.capture_statements else None, fid,
                         seen_ids, ctx.statement_text_limit)
        if ctx.capture_statements and page.navigation:
            page.read_nested_urls(markup, src, statements, lines, fid, seen_ids,
                                  ctx.statement_text_limit)

        functions: list[Function] = []
        classes: list[Class] = []
        if ctx.capture_statements:
            for i in islands:
                if i.kind == "comment":
                    statements.append(_statement(
                        src, lines, i.start, i.end, path, fid, seen_ids, ctx.statement_text_limit,
                        nodeType="jsp_comment", semanticType="comment"))
            if not page.el_ignored(settings, tag_file):
                for start, end in el_spans(markup, deferred=page.deferred(settings)):
                    statements.append(_statement(
                        src, lines, start, end, path, fid, seen_ids, ctx.statement_text_limit,
                        nodeType="el_expression",
                        name=src[start + 2:end - 1].decode("utf-8", "replace").strip()))
        if any(i.kind in CODE_KINDS for i in islands):
            statements.extend(_java(src, islands, path, fid, ctx, seen_ids, functions, classes))
        statements.sort(key=lambda s: s.startLine)  # three passes, one file order

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="jsp",
            framework=None if tag_file and not any(m in src for m in _JSP_MARKERS) else "jsp",
            uiRole="component" if tag_file else "template",
            loc=count_loc(src.decode("utf-8", "replace")),
            importFiles=[f for f in page.files if f != path],
            externalImports=list(page.external),
            functions=functions,
            classes=classes,
            statements=statements,
        )


#: Page files a URL can reach (fragments and tag files are parts of pages).
_PAGE_EXTENSIONS = (".jsp", ".jspx")


def _page_route(src: bytes, path: str, fid: str, page: _Page, seen_ids: set[str],
                limit: int) -> Statement | None:
    """The page's own entry point, when it is served directly (see the module docstring)."""
    if not path.lower().endswith(_PAGE_EXTENSIONS) or page.web_root is None:
        return None
    url = page.url()
    if url.startswith(("/WEB-INF/", "/META-INF/")):
        return None
    first = src.split(b"\n", 1)[0].decode("utf-8", "replace").rstrip()
    return Statement(
        id=disambiguate(statement_id(path, 1, 0), seen_ids),
        parentId=fid,
        nodeType="document",  # the html grammar's root: the record is the whole page
        text=first if len(first) <= limit else first[:limit],
        startLine=1,
        endLine=src.count(b"\n") + 1,
        path=path,
        semanticType="route",
        framework="jsp",
        method="GET",
        endpoint=url,
        handler=posixpath.basename(path),
        handlerLine=1,
        routeKind="page",
    )


def _statement(src: bytes, lines: Lines, start: int, end: int, path: str, parent_id: str,
               seen_ids: set[str], limit: int, **fields: object) -> Statement:
    text = src[start:end].decode("utf-8", "replace")
    line = lines.line(start)
    return Statement(
        id=disambiguate(statement_id(path, line, lines.col(start)), seen_ids),
        parentId=parent_id,
        text=text if len(text) <= limit else text[:limit],
        startLine=line,
        endLine=lines.line(max(start, end - 1)),
        path=path,
        **fields,  # type: ignore[arg-type]
    )


def _java(src: bytes, islands: list[Island], path: str, fid: str, ctx: ParseContext,
          seen_ids: set[str], functions: list[Function], classes: list[Class]) -> list[Statement]:
    """The scripting code: declared methods and classes, scriptlet statements, Java comments.
    Functions and Classes are structure (always built); statements only under capture."""
    code = shadow_java(src, islands)
    root = parse_source("java", code, ctx.parse_timeout_micros).root_node
    capture, limit = ctx.capture_statements, ctx.statement_text_limit
    statements: list[Statement] = []
    for child in root.named_children:
        if child.type in _METHOD_TYPES:
            fn, stmts = build_method(child, code, path, parent_id=fid, class_name=None,
                                     seen_ids=seen_ids, capture=capture, limit=limit)
            functions.append(fn)
            statements.extend(stmts)
        elif child.type in _CLASS_TYPES:
            cls_list, methods, stmts = build_class(child, code, path, parent_id=fid,
                                                   seen_ids=seen_ids, capture=capture, limit=limit)
            classes.extend(cls_list)
            functions.extend(methods)
            statements.extend(stmts)
    if not capture:
        return statements
    statements.extend(extract_statements(root, code, path, parent_id=fid, capture=True,
                                         limit=limit, seen_ids=seen_ids))
    statements.extend(comment_statements_for(
        root, code, path, file_id=fid, functions=functions, classes=classes,
        statements=statements, control_flow=CONTROL_FLOW, comment_types=COMMENT_TYPES,
        limit=limit, seen_ids=seen_ids,
    ))
    return statements


class _Page:
    """What one page references, and the web application it belongs to."""

    def __init__(self, path: str, repo_root: Path, index: JspIndex | None) -> None:
        self.path = path
        self.repo_root = repo_root
        self.index = index
        self.web_root = _web_root(path, repo_root, index)
        self.app: WebApp | None = (
            index.webapps.get(self.web_root) if index is not None and self.web_root is not None
            else None)
        self.files: dict[str, None] = {}
        self.external: dict[str, None] = {}
        self.tagdirs: dict[str, str] = {}  # prefix → context-relative tag directory
        self.jstl_core: set[str] = set()  # prefixes bound to JSTL core
        self.struts_tags: set[str] = set()  # prefixes bound to the Struts2 tags
        self.navigation = True  # record navigation (not in a fixture)
        self.element_starts: set[int] = set()  # elements the markup walk has seen
        self._actions: ActionTable | None = None  # the module's Struts actions, on first use

    # ── references ──────────────────────────────────────────────────────────────────────

    def resource(self, ref: str) -> str | None:
        """A literal page-relative or context-relative path, resolved to an existing file."""
        ref = ref.strip()
        if not ref or "${" in ref or "#{" in ref or "<%" in ref or _EXTERNAL_URL.match(ref):
            return None
        ref = ref.split("?", 1)[0].split("#", 1)[0]
        if ref.startswith("/"):
            if self.web_root is None:
                return None
            rel = posixpath.join(self.web_root, ref.lstrip("/"))
        else:
            rel = posixpath.join(posixpath.dirname(self.path), ref)
        rel = posixpath.normpath(rel)
        if rel == ".." or rel.startswith("../") or rel.startswith("/"):
            return None
        return rel if (self.repo_root / rel).is_file() else None

    def add_resource(self, ref: str) -> None:
        found = self.resource(ref)
        if found is not None:
            self.files.setdefault(found, None)

    def add_class(self, name: str) -> None:
        name = re.sub(r"<.*>|\[\]", "", name).strip()
        if not name or "${" in name or "<%" in name:
            return
        found = None if name.endswith(".*") or self.index is None else self.index.java_file(name)
        if found is not None:
            self.files.setdefault(found, None)
        else:
            self.external.setdefault(name, None)

    def add_taglib(self, prefix: str, uri: str) -> None:
        uri = uri.strip()
        if not uri:
            return
        if uri.startswith(_TAGDIR_URN):
            self.tagdirs[prefix] = uri[len(_TAGDIR_URN):]
            return
        if uri.startswith(_TLD_URN):
            uri = uri[len(_TLD_URN):]
        if uri in _JSTL_CORE:
            self.jstl_core.add(prefix)
        if uri == _STRUTS_TAGS:
            self.struts_tags.add(prefix)
        found = self._tld(uri)
        if found is not None:
            self.files.setdefault(found, None)
        else:
            self.external.setdefault(uri, None)

    def _tld(self, uri: str) -> str | None:
        """The in-repo descriptor a taglib URI names: a ``web.xml`` mapping, a direct path,
        or a ``.tld`` declaring that ``<uri>``."""
        if self.app is not None and uri in self.app.taglibs:
            loc = self.app.taglibs[uri]
            return self.resource(loc if loc.startswith("/") else "/WEB-INF/" + loc)
        if uri.lower().endswith((".tld", ".jar")) and not _EXTERNAL_URL.match(uri):
            return self.resource(uri)
        return self.index.tld_by_uri.get(uri) if self.index is not None else None

    # ── directives ──────────────────────────────────────────────────────────────────────

    def read_directives(self, src: bytes, islands: list[Island], out: list[Statement] | None,
                        lines: Lines, fid: str, seen_ids: set[str], limit: int) -> dict[str, str]:
        """Follow every directive's references; return the page settings they set."""
        settings: dict[str, str] = {}
        for i in islands:
            if i.kind != "directive":
                continue
            attrs = directive_attrs(src, i)
            if out is not None:
                out.append(_statement(src, lines, i.start, i.end, self.path, fid, seen_ids, limit,
                                      nodeType="jsp_directive", name=i.name))
            if i.name == "include":
                for key, value in attrs:
                    if key == "file":
                        self.add_resource(value)
            elif i.name in ("page", "tag"):
                for key, value in attrs:
                    if key == "import":
                        for name in value.split(","):
                            self.add_class(name)
                    elif key == "extends":
                        self.add_class(value)
                    elif key == "errorPage":
                        self.add_resource(value)
                    elif key in ("isELIgnored", "deferredSyntaxAllowedAsLiteral"):
                        settings[key] = value.strip().lower()
            elif i.name == "taglib":
                found = dict(attrs)
                prefix = found.get("prefix", "")
                if "tagdir" in found:
                    self.tagdirs[prefix] = found["tagdir"]
                elif "uri" in found:
                    self.add_taglib(prefix, found["uri"])
        return settings

    def add_xml_taglibs(self, src: bytes) -> None:
        """Taglibs of an XML-syntax document, bound as namespaces."""
        if b"xmlns:" not in src:
            return
        for m in _XMLNS.finditer(src):
            prefix = m.group(1).decode("utf-8", "replace")
            if prefix != "jsp":
                self.add_taglib(prefix, m.group(2).decode("utf-8", "replace"))

    def add_web_xml_includes(self, tag_file: bool) -> None:
        """``include-prelude`` / ``include-coda`` of the property groups covering this page."""
        if tag_file or self.app is None:
            return
        url = self.url()
        for group in self.groups(url):
            for ref in (*group.preludes, *group.codas):
                self.add_resource(ref)

    # ── page settings ───────────────────────────────────────────────────────────────────

    def url(self) -> str:
        root = self.web_root or ""
        return "/" + (self.path[len(root) + 1:] if root else self.path)

    def groups(self, url: str) -> list[PropertyGroup]:
        if self.app is None:
            return []
        return [g for g in self.app.groups if any(servlet_match(p, url) for p in g.patterns)]

    def el_ignored(self, settings: dict[str, str], tag_file: bool) -> bool:
        """Whether ``${ }`` is plain text here: the page directive, then the property groups,
        then the application version (before Servlet 2.4, EL was off by default)."""
        if "isELIgnored" in settings:
            return settings["isELIgnored"] == "true"
        for group in reversed(self.groups(self.url())):
            if group.el_ignored is not None:
                return group.el_ignored
        if tag_file:
            return False
        version = self.app.version if self.app is not None else None
        return version is not None and version < 2.4

    def deferred(self, settings: dict[str, str]) -> bool:
        """Whether ``#{ }`` is deferred EL here (JSP 2.1, Servlet 2.5) rather than text."""
        if settings.get("deferredSyntaxAllowedAsLiteral") == "true":
            return False
        for group in reversed(self.groups(self.url())):
            if group.deferred_literal is not None:
                return not group.deferred_literal
        version = self.app.version if self.app is not None else None
        return version is None or version >= 2.5

    # ── markup ──────────────────────────────────────────────────────────────────────────

    def read_markup(self, root: Node, src: bytes, out: list[Statement] | None, fid: str,
                    seen_ids: set[str], limit: int) -> None:
        """Prefixed elements (standard actions, JSTL, custom tags), navigation, HTML comments."""
        stack: list[Node] = [root]
        while stack:
            node = stack.pop()
            if node.type == "element":
                start = next((c for c in node.named_children
                              if c.type in ("start_tag", "self_closing_tag")), None)
                tag = _tag_name(start, src) if start is not None else None
                if start is not None and tag:
                    self.element_starts.add(node.start_byte)
                    attrs = _attrs(start, src)
                    if ":" in tag:
                        self._action(tag, attrs)
                    nav = self._navigation(tag, attrs) if self.navigation else None
                    if nav is not None and nav[1] is None:
                        nav = None  # an unresolved link is no link: never a null endpoint
                    if out is not None and (":" in tag or nav is not None):
                        framework, endpoint = nav if nav is not None else (None, None)
                        fields: dict[str, object] = dict(
                            semanticType="route", routeKind="navigation", framework=framework,
                            endpoint=endpoint) if nav is not None else {}
                        out.append(_node_statement(node, src, self.path, fid, seen_ids, limit,
                                                   name=tag, **fields))
            elif node.type == "comment" and out is not None:
                out.append(_node_statement(node, src, self.path, fid, seen_ids, limit,
                                           semanticType="comment"))
            stack.extend(reversed(node.named_children))

    def read_nested_urls(self, markup: bytes, src: bytes, out: list[Statement], lines: Lines,
                         fid: str, seen_ids: set[str], limit: int) -> None:
        """URL tags the markup walk cannot see because they sit inside another tag's attribute
        (``<a href="<s:url action='list'/>">``, the usual Struts idiom): a navigation record for
        each one whose target resolves. Only tags of a bound Struts or JSTL prefix count."""
        if not self.struts_tags and not self.jstl_core:
            return
        for m in _URL_TAG.finditer(markup):
            if m.start() in self.element_starts:
                continue  # an element the walk already recorded
            prefix = m.group(1).decode("utf-8", "replace")
            if prefix not in self.struts_tags and prefix not in self.jstl_core:
                continue
            tag = f"{prefix}:{m.group(2).decode()}"
            attrs = {a.group(1): a.group(2) if a.group(2) is not None else a.group(3)
                     for a in _TAG_ATTR.finditer(m.group(3).decode("utf-8", "replace"))}
            nav = self._navigation(tag, attrs)
            if nav is None or nav[1] is None:
                continue
            out.append(_statement(src, lines, m.start(), m.end(), self.path, fid, seen_ids, limit,
                                  nodeType="element", name=tag, semanticType="route",
                                  routeKind="navigation", framework=nav[0], endpoint=nav[1]))

    def _navigation(self, tag: str, attrs: dict[str, str]) -> tuple[str, str | None] | None:
        """``(framework, endpoint)`` when the element leads to an action or a page."""
        prefix, _, local = tag.rpartition(":")
        if prefix and prefix in self.struts_tags:
            key = _STRUTS_ACTION_TAGS.get(local)
            if key is not None and attrs.get(key):
                return "struts2", self._action_address(attrs[key], attrs.get("namespace"))
            link = _STRUTS_LINKS.get(local)
            return self._link(attrs.get(link, "")) if link else None
        if prefix and prefix in self.jstl_core:
            link = _JSTL_LINKS.get(local)
            return self._link(attrs.get(link, "")) if link and "context" not in attrs else None
        link = _LINKS.get(tag.lower())
        return self._link(attrs.get(link, "")) if link else None

    def _suffix(self) -> str | None:
        """The action extension of this page's module (its own, else the repo-level one)."""
        found = self.index.struts_suffixes if self.index is not None else {}
        for module in (module_key(self.path), ""):
            if module in found:
                return found[module]
        return extension(None)

    def _page_namespaces(self) -> tuple[str, ...] | None:
        """Namespaces of the actions rendering this page (None: they disagree)."""
        if self.index is not None and self.path in self.index.struts_views:
            return self.index.struts_views[self.path]
        content = f"{self.web_root}/WEB-INF/content/" if self.web_root else "WEB-INF/content/"
        if self.web_root is not None and self.path.startswith(content.lstrip("/")):
            folder = posixpath.dirname(self.path[len(content.lstrip("/")):])
            return ("/" + folder if folder else "/",)
        return ()

    def _action_address(self, action: str, namespace: str | None) -> str | None:
        action = action.strip()
        suffix = self._suffix()
        if any(d in action for d in _DYNAMIC) or (namespace and any(
                d in namespace for d in _DYNAMIC)):
            return None
        action = action.split("!", 1)[0].split("?", 1)[0]  # DMI method / query
        if suffix and action.endswith(suffix):
            action = action[: -len(suffix)]
        if namespace is None:
            found = self._page_namespaces()
            namespace = found[0] if found is not None and len(found) == 1 else None
        actions = self._struts_actions()
        if actions:
            # The namespace that really serves it: the default namespace answers any
            # namespace, and an action only the default namespace declares needs no page one.
            serving = actions.serving(namespace, action)
            if serving is not None:
                namespace = serving
        return action_endpoint(namespace, action, suffix)

    def _module_value(self, values: dict[str, Any]) -> Any:
        """This page's module entry of a per-module index map (else the repo-level one)."""
        for module in (module_key(self.path), ""):
            if module in values:
                return values[module]
        return None

    def _struts_actions(self) -> ActionTable:
        """This page's module's actions, built once per page."""
        if self._actions is None:
            found = self._module_value(self.index.struts_actions) if self.index else None
            self._actions = ActionTable(found or ())
        return self._actions

    def _bare_action(self, path: str) -> str | None:
        """A link written without an extension (``bookList``, ``/admin/save``) that names an
        action of this module, when its actions answer without one: that action's address,
        with its extension. Anything else is not resolved: never a guessed action."""
        if self.index is None or not self._module_value(self.index.struts_bare):
            return None
        namespace, name = posixpath.split(path)
        if not name or "." in name:
            return None
        serving = self._struts_actions().serving(namespace, name)
        return action_endpoint(serving, name, self._suffix()) if serving is not None else None

    def _link(self, url: str) -> tuple[str, str | None] | None:
        """A link to an action or a page: ``("jsp", endpoint)``, else None."""
        url = _CONTEXT_PATH.sub("", url.strip())
        if not url or _EXTERNAL_URL.match(url) or url.startswith("#"):
            return None
        target = url.split("?", 1)[0].split("#", 1)[0]
        suffix = self._suffix()
        kinds = (*_ACTION_SUFFIXES, *_PAGE_SUFFIXES, *((suffix,) if suffix else ()))
        if not target.lower().endswith(kinds):
            # No extension: a link only when it names an action of this module.
            if not target or any(d in target for d in _DYNAMIC):
                return None
            resolved = self._resolve(target)
            found = self._bare_action(resolved) if resolved is not None else None
            return ("jsp", found) if found is not None else None
        if any(d in target for d in _DYNAMIC):
            return "jsp", None
        return "jsp", self._resolve(target)

    def _resolve(self, target: str) -> str | None:
        """A link's context-relative path: absolute as written; relative against the page's
        URL, or for a page under ``WEB-INF`` against the one namespace that renders it."""
        if target.startswith("/"):
            return posixpath.normpath(target)
        if self.web_root is None:
            return None
        if "/WEB-INF/" in "/" + self.path:
            found = self._page_namespaces()
            if not found or len(found) != 1:
                return None
            base = found[0]
        else:
            base = posixpath.dirname(self.url())
        resolved = posixpath.normpath(posixpath.join(base, target))
        return resolved if resolved.startswith("/") and not resolved.startswith("/..") else None

    def _action(self, tag: str, attrs: dict[str, str]) -> None:
        prefix, local = tag.split(":", 1)
        if tag in ("jsp:include", "jsp:forward"):
            self.add_resource(attrs.get("page", ""))
        elif tag == "jsp:useBean":
            for key in ("class", "type", "beanName"):
                if key in attrs:
                    self.add_class(attrs[key])
        elif prefix in self.jstl_core and local == "import" and "context" not in attrs:
            self.add_resource(attrs.get("url", ""))
        elif prefix in self.tagdirs:
            base = self.tagdirs[prefix].rstrip("/")
            for ext in (".tag", ".tagx"):
                found = self.resource(f"{base}/{local}{ext}")
                if found is not None:
                    self.files.setdefault(found, None)
                    break


#: Web application folders a build tool or IDE declares by convention, recognised even with no
#: ``WEB-INF`` (a Servlet 3+ application needs no ``web.xml``).
_WEB_FOLDERS = ("WebContent", "WebRoot")


def _web_root(path: str, repo_root: Path, index: JspIndex | None) -> str | None:
    """The web application holding ``path``: the nearest ancestor directory with ``WEB-INF``
    (from the index when there is one, else from the disk); failing that, the nearest
    conventional web folder — Maven's ``src/main/webapp``, Eclipse's ``WebContent`` /
    ``WebRoot``, or a NetBeans project's ``web/`` (beside its ``nbproject/``)."""
    parts = path.split("/")[:-1]
    for depth in range(len(parts), -1, -1):
        root = "/".join(parts[:depth])
        if index is not None:
            if root in index.webapps:
                return root
        elif (repo_root / root / "WEB-INF").is_dir():
            return root
    for depth in range(len(parts), 0, -1):
        name, parent = parts[depth - 1], parts[: depth - 1]
        if (name in _WEB_FOLDERS or (name == "webapp" and parent[-2:] == ["src", "main"])
                or (name == "web" and (repo_root.joinpath(*parent) / "nbproject").is_dir())):
            return "/".join(parts[:depth])
    return None


def _tag_name(start: Node, src: bytes) -> str | None:
    name = next((c for c in start.named_children if c.type == "tag_name"), None)
    return node_text(name, src) if name is not None else None


def _attrs(start: Node, src: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for attr in start.named_children:
        if attr.type != "attribute":
            continue
        name = next((c for c in attr.named_children if c.type == "attribute_name"), None)
        quoted = next((c for c in attr.named_children if c.type == "quoted_attribute_value"), None)
        holder = quoted if quoted is not None else attr
        value = next((c for c in holder.named_children if c.type == "attribute_value"), None)
        if name is not None:
            out[node_text(name, src)] = node_text(value, src) if value is not None else ""
    return out


def _node_statement(node: Node, src: bytes, path: str, parent_id: str, seen_ids: set[str],
                    limit: int, **fields: object) -> Statement:
    start, col = node.start_point[0] + 1, node.start_point[1]
    text = node_text(node, src)
    return Statement(
        id=disambiguate(statement_id(path, start, col), seen_ids),
        parentId=parent_id,
        nodeType=node.type,
        text=text if len(text) <= limit else text[:limit],
        startLine=start,
        endLine=node.end_point[0] + 1,
        path=path,
        **fields,  # type: ignore[arg-type]
    )
