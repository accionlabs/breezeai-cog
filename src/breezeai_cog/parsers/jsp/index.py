"""Repo-level pre-pass for JSP: web applications, tag library descriptors, Java sources.

A JSP page depends on files it cannot see on its own:

* the **web application** it belongs to — the directory holding ``WEB-INF`` — and that
  application's ``WEB-INF/web.xml``: its Servlet version (before 2.4, ``${ }`` is plain text;
  before 2.5, so is ``#{ }``), its ``<jsp-property-group>`` settings (``el-ignored``,
  ``deferred-syntax-allowed-as-literal``, ``include-prelude``, ``include-coda``) and its
  ``<taglib>`` URI mappings (the JSP 1.x way to name a ``.tld``);
* the ``.tld`` descriptors in the repo, keyed by their declared ``<uri>``;
* the ``.java`` sources, so a ``page import`` or a ``jsp:useBean`` class resolves to its file;
* the Struts2 configurations: the action extension, and which namespaces' actions render each
  view (a ``<result>`` naming the JSP), so a ``<s:form action="login">`` on that page resolves
  against the namespace of the action that renders it.

One walk of the repo collects all three; nothing is parsed with tree-sitter. Every lookup is
honest: two candidates for one name resolve to nothing.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from ...utils import repo_relative
from ..java_struts2.common import (
    EXTENSION_KEY, PROPERTIES_LEVEL, STRUTS_ROOT, WEB_XML_LEVEL, XML_LEVEL, Layered, bare_of,
    namespace_key, read_properties, suffix_of, web_xml_constants, xml_constants,
)
from ..java_struts2.facts import module_key
from ..java_struts2.views import VIEW_TYPES, result_target, web_root

#: Directories never walked: VCS, dependencies, IDE state and build output (a built WAR copies
#: the webapp and its sources, which would make every lookup ambiguous).
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".svn", ".hg", ".idea", ".vscode", ".gradle", ".mvn", "node_modules",
    "bower_components", "target", "build", "out", "bin", "dist", ".cache",
})
#: A DTD-declared web.xml version (``web-app_2_3.dtd``), for descriptors with no ``version``.
_DTD_VERSION = re.compile(rb"web-app_(\d+)_(\d+)\.dtd")


@dataclass(frozen=True)
class PropertyGroup:
    """One ``<jsp-property-group>``: the pages it covers and what it sets for them."""

    patterns: tuple[str, ...]
    el_ignored: bool | None = None
    deferred_literal: bool | None = None
    preludes: tuple[str, ...] = ()
    codas: tuple[str, ...] = ()


@dataclass(frozen=True)
class WebApp:
    """A web application: its root (the directory holding ``WEB-INF``) and its ``web.xml``."""

    root: str  # repo-relative, "" for the repo root
    version: float | None = None  # None: no web.xml, or it declares no version
    groups: tuple[PropertyGroup, ...] = ()
    taglibs: dict[str, str] = field(default_factory=dict)  # taglib-uri → context-relative path


@dataclass(frozen=True)
class JspIndex:
    webapps: dict[str, WebApp] = field(default_factory=dict)
    java_by_name: dict[str, tuple[str, ...]] = field(default_factory=dict)  # Foo.java → paths
    tld_by_uri: dict[str, str | None] = field(default_factory=dict)  # None: ambiguous
    #: Module → its Struts2 action extension (``.action``); None when the module's
    #: configurations disagree. A module is the folder above ``src/main/`` (:func:`module_key`).
    struts_suffixes: dict[str, str | None] = field(default_factory=dict)
    #: View file → namespaces of the actions rendering it; None when they disagree.
    struts_views: dict[str, tuple[str, ...] | None] = field(default_factory=dict)
    #: Module → the actions its ``struts.xml`` files declare, ``(namespace_key, name or
    #: wildcard pattern)``, so a link written without an extension is resolved only when it
    #: names one of them.
    struts_actions: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    #: Module → whether its actions also answer without an extension (Struts' default).
    struts_bare: dict[str, bool] = field(default_factory=dict)

    def java_file(self, fqcn: str) -> str | None:
        """The in-repo source of ``com.acme.Foo`` (or of ``com.acme.Outer.Inner``'s outer
        class), or None when there is none or more than one."""
        parts = fqcn.split(".")
        while parts:
            suffix = "/".join(parts) + ".java"
            hits = [p for p in self.java_by_name.get(parts[-1] + ".java", ())
                    if p == suffix or p.endswith("/" + suffix)]
            if len(hits) == 1:
                return hits[0]
            if hits or len(parts) < 2 or not parts[-2][:1].isupper():
                return None
            parts = parts[:-1]  # Outer.Inner → Outer
        return None


def build_jsp_index(repo_root: Path) -> JspIndex:
    webapps: dict[str, WebApp] = {}
    java: dict[str, list[str]] = {}
    tlds: dict[str, str | None] = {}
    struts = _Struts(repo_root)
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        here = Path(dirpath)
        if "WEB-INF" in dirnames:
            root = repo_relative(here, repo_root).replace("\\", "/")
            root = "" if root == "." else root
            webapps[root] = _read_web_xml(here / "WEB-INF" / "web.xml", root)
            struts.read_web_xml(here / "WEB-INF" / "web.xml")
        for name in sorted(filenames):
            if name.endswith(".java"):
                java.setdefault(name, []).append(
                    repo_relative(here / name, repo_root).replace("\\", "/"))
            elif name.lower().endswith(".tld"):
                path = here / name
                uri = _tld_uri(path)
                if uri:
                    rel = repo_relative(path, repo_root).replace("\\", "/")
                    tlds[uri] = rel if tlds.get(uri, rel) == rel else None
            elif name.lower().endswith(".xml"):
                struts.read(here / name)
    return JspIndex(webapps, {k: tuple(v) for k, v in java.items()}, tlds,
                    struts.suffixes(), struts.views(), struts.actions_by_module(),
                    struts.bare())


#: The largest XML file read as a possible Struts2 configuration.
_MAX_XML = 2_000_000


class _Struts:
    """What the Struts2 configurations say about views: extension and rendering namespaces."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.constants = Layered()  # struts.action.extension per module, Struts' precedence
        self.actions: dict[str, set[tuple[str, str]]] = {}
        self.namespaces: dict[str, set[str] | None] = {}

    def read(self, path: Path) -> None:
        try:
            if path.stat().st_size > _MAX_XML:
                return
            raw = path.read_bytes()
        except OSError:
            return
        if STRUTS_ROOT.search(raw) is None:
            return
        try:
            doc = ET.fromstring(raw)
        except ET.ParseError:
            return
        if _local(doc.tag) != "struts":
            return
        module = module_key(repo_relative(path, self.repo_root).replace("\\", "/"))
        self._add(module, XML_LEVEL, xml_constants(raw))
        self._add(module, PROPERTIES_LEVEL, read_properties(path.parent))
        rel_dir = repo_relative(path.parent, self.repo_root).replace("\\", "/")
        web = web_root(self.repo_root, "" if rel_dir == "." else rel_dir)
        for package in _children(doc, "package"):
            ns = package.get("namespace") or ""
            if "${" not in ns:
                self.actions.setdefault(module, set()).update(
                    (namespace_key(ns), a.get("name") or "") for a in _children(package, "action")
                    if a.get("name") and "{" not in (a.get("name") or ""))
            if "tiles" in (package.get("extends") or "") or "${" in ns:
                continue
            for holder in [*_children(package, "action"), *_children(package, "global-results")]:
                for result in _children(holder, "result"):
                    kind = result.get("type") or "dispatcher"
                    if kind not in VIEW_TYPES:
                        continue
                    params = {p.get("name"): (p.text or "").strip()
                              for p in _children(result, "param")}
                    location = params.get("location") or (result.text or "").strip()
                    target = result_target(kind, location, ns, None, None, self.repo_root, web)
                    if target is not None and target.file is not None:
                        found = self.namespaces.setdefault(target.file, set())
                        if found is not None:
                            found.add(ns or "/")

    def read_web_xml(self, path: Path) -> None:
        """The Struts filter's ``struts.action.extension`` init-param, which overrides both."""
        try:
            raw = path.read_bytes()
        except OSError:
            return
        self._add(module_key(repo_relative(path, self.repo_root).replace("\\", "/")),
                  WEB_XML_LEVEL, web_xml_constants(raw))

    def _add(self, module: str, level: int, constants: dict[str, str]) -> None:
        if EXTENSION_KEY in constants:
            self.constants.add(module, level, {EXTENSION_KEY: constants[EXTENSION_KEY]})

    def suffixes(self) -> dict[str, str | None]:
        return {module: suffix_of(found) for module, found in self.constants.resolve().items()}

    def bare(self) -> dict[str, bool]:
        resolved = self.constants.resolve()
        return {module: bare_of(resolved.get(module, {})) for module in self.actions}

    def actions_by_module(self) -> dict[str, tuple[tuple[str, str], ...]]:
        return {module: tuple(sorted(found)) for module, found in self.actions.items()}

    def views(self) -> dict[str, tuple[str, ...] | None]:
        return {k: (tuple(sorted(v)) if v is not None else None)
                for k, v in self.namespaces.items()}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in node if _local(c.tag) == name]


def _text(node: ET.Element, name: str) -> str:
    return next(((c.text or "").strip() for c in node if _local(c.tag) == name), "")


def _flag(node: ET.Element, name: str) -> bool | None:
    value = _text(node, name).lower()
    return True if value == "true" else False if value == "false" else None


def _read_web_xml(path: Path, root: str) -> WebApp:
    """The application's settings; a missing or unreadable ``web.xml`` gives the defaults."""
    try:
        raw = path.read_bytes()
        doc = ET.fromstring(raw)
    except (OSError, ET.ParseError):
        return WebApp(root)
    version = _version(doc.get("version"), raw)
    containers = [doc, *_children(doc, "jsp-config")]  # 2.4+ nests taglibs in <jsp-config>
    taglibs = {
        uri: loc
        for c in containers for t in _children(c, "taglib")
        if (uri := _text(t, "taglib-uri")) and (loc := _text(t, "taglib-location"))
    }
    groups = tuple(
        PropertyGroup(
            patterns=tuple((p.text or "").strip() for p in _children(g, "url-pattern")),
            el_ignored=_flag(g, "el-ignored"),
            deferred_literal=_flag(g, "deferred-syntax-allowed-as-literal"),
            preludes=tuple((p.text or "").strip() for p in _children(g, "include-prelude")),
            codas=tuple((p.text or "").strip() for p in _children(g, "include-coda")),
        )
        for c in _children(doc, "jsp-config") for g in _children(c, "jsp-property-group")
    )
    return WebApp(root, version, groups, taglibs)


def _version(declared: str | None, raw: bytes) -> float | None:
    if declared:
        try:
            return float(declared)
        except ValueError:
            return None
    dtd = _DTD_VERSION.search(raw)
    return float(f"{dtd.group(1).decode()}.{dtd.group(2).decode()}") if dtd else None


def _tld_uri(path: Path) -> str | None:
    try:
        doc = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    return _text(doc, "uri") or None
