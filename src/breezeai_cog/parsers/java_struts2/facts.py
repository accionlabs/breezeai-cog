"""Struts2 facts on the Java index: what an action class cannot see from its own file.

* ``struts2.settings`` — per module, the ``struts.*`` constants that shape an address (the
  action extension, the Convention package locators, action suffix, result path and
  ``mapAllMatches``, and the action mapper — ``rest`` for the REST plugin), from the
  module's Struts2 XML files, the ``struts.properties`` beside them and the Struts filter's
  init-params in its ``web.xml`` — a later source overriding an earlier one, as Struts reads
  them (:class:`.common.Layered`) — plus whether the module's build declares the Convention
  plugin (``struts2-convention-plugin`` in its ``pom.xml`` / ``build.gradle`` / ``ivy.xml``).
  A module is the folder above ``src/main/`` (``""`` for a repo laid out otherwise); see
  :func:`module_key`. Two sources of one level setting different values make that key None:
  unknown, never one of them. Modules never mix: a repository of many example applications
  each with its own extension resolves each one.
* ``struts2.package-namespaces`` — ``@Namespace`` / ``@Namespaces`` on a ``package-info.java``,
  the namespace of every action class in that package.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, register_fact
from ..index_common import record_distinct
from ..treesitter import node_text
from .common import (
    EXTENSION_KEY, LOCATORS_KEY, MAP_ALL_KEY, MAPPER_KEY, PROPERTIES_LEVEL, RESULT_PATH_KEY,
    STRUTS_FILTERS, STRUTS_ROOT, SUFFIX_KEY, WEB_XML_LEVEL, XML_LEVEL, Layered, included_files, read_properties,
    web_xml_constants, web_xml_lists_configs, xml_constants,
)

SETTINGS = "struts2.settings"
PACKAGE_NAMESPACES = "struts2.package-namespaces"
#: The constant keys the detector reads, and the build-file flag.
_KEYS = (EXTENSION_KEY, LOCATORS_KEY, SUFFIX_KEY, RESULT_PATH_KEY, MAP_ALL_KEY, MAPPER_KEY)
CONVENTION = "convention-plugin"
_PLUGIN = b"struts2-convention-plugin"
_BUILD_FILES = ("pom.xml", "build.gradle", "ivy.xml")


def module_key(rel: str) -> str:
    """The module a file belongs to: the folder above ``src/main/`` (``shop/src/main/java/…`` →
    ``shop``), a build file's own folder, else ``""`` (one module for the whole repo)."""
    path = "/" + rel.replace("\\", "/")
    i = path.find("/src/main/")
    if i >= 0:
        return path[1:i] if i > 0 else ""
    if posixpath.basename(rel) in _BUILD_FILES:
        return posixpath.dirname(rel)
    return ""


def module_settings(fact: dict[str, dict[str, str | None]] | None, rel: str) -> dict[str, str | None]:
    """The settings of the module ``rel`` belongs to (the repo-level ones when it has none)."""
    if not fact:
        return {}
    return fact.get(module_key(rel)) or fact.get("") or {}


#: What one file contributes. ``("level", module, level, constants)``: constants at one
#: precedence level (``web.xml``, a build file's plugin flag). ``("struts", rel, includes,
#: constants, properties)``: a Struts XML file, the files it includes, its ``<constant>``s and
#: the ``struts.properties`` beside it. ``("custom", module)``: the module's Struts filter names
#: its own configuration files.
_Found = list[tuple[Any, ...]]
_ROOT_NAME = "struts.xml"


def _collect_settings(root: Node | None, source: bytes, rel: str,
                      repo_root: Path | None) -> _Found | None:
    name = posixpath.basename(rel)
    module = module_key(rel)
    found: _Found = []

    def keep(constants: dict[str, str]) -> dict[str, str]:
        return {k: v for k, v in constants.items() if k in _KEYS}

    if name == "web.xml":
        web = keep(web_xml_constants(source))
        if web:
            found.append(("level", module, WEB_XML_LEVEL, web))
        if web_xml_lists_configs(source):
            found.append(("custom", module))
    elif rel.endswith(".xml") and STRUTS_ROOT.search(source):
        properties = keep(read_properties(repo_root / posixpath.dirname(rel))) \
            if repo_root is not None else {}
        found.append(("struts", rel, included_files(rel, source), keep(xml_constants(source)),
                      properties))
    if name in _BUILD_FILES and _PLUGIN in source:
        found.append(("level", module, XML_LEVEL, {CONVENTION: "true"}))
    return found or None


def _resolve_settings(values: list[_Found], index: Any) -> dict[str, dict[str, str | None]] | None:
    """Each module's constants as Struts loads them: ``<constant>``s of the module's
    ``struts.xml`` and of every file its ``<include>`` chain reaches (a constant applies to the
    whole configuration), the ``struts.properties`` beside that ``struts.xml``, the Struts
    filter's init-params. A Struts XML file nothing includes is not loaded, so its constants are
    not the module's.

    Which files are loaded is unknown in a module whose filter names its own configuration
    files, or that has no ``struts.xml``: there every Struts XML file of the module counts, and
    two that disagree leave the key unknown."""
    layered = Layered()
    files: dict[str, tuple[tuple[str, ...], dict[str, str], dict[str, str]]] = {}
    custom: set[str] = set()
    for file_values in values:
        for entry in file_values:
            if entry[0] == "level":
                layered.add(entry[1], entry[2], entry[3])
            elif entry[0] == "custom":
                custom.add(entry[1])
            else:
                files[entry[1]] = (entry[2], entry[3], entry[4])
    roots = sorted(rel for rel in files if posixpath.basename(rel) == _ROOT_NAME)
    rooted = {module_key(rel) for rel in roots} - custom
    for root in roots:  # the configuration each `struts.xml` loads, for that file's module
        module = module_key(root)
        if module not in rooted:
            continue
        layered.add(module, PROPERTIES_LEVEL, files[root][2])
        seen, queue = {root}, [root]
        while queue:
            includes, constants, _ = files[queue.pop()]
            layered.add(module, XML_LEVEL, constants)
            for target in includes:
                if target in files and target not in seen:
                    seen.add(target)
                    queue.append(target)
    for rel, (_, constants, properties) in files.items():
        module = module_key(rel)
        # A file outside any `src/main/` but inside a module that has its `struts.xml` (`shop/conf/
        # extra.xml`) is that module's stray file, not the repository's configuration. A nested
        # module's file (`shop/sub/src/main/…`) is that module's own.
        stray = module == "" and any(m and rel.startswith(m + "/") for m in rooted)
        if module not in rooted and not stray:  # the loaded files are not known: all count
            layered.add(module, XML_LEVEL, constants)
            layered.add(module, PROPERTIES_LEVEL, properties)
    return layered.resolve() or None


def _collect_namespaces(root: Node | None, source: bytes, rel: str,
                        repo_root: Path | None) -> tuple[str, tuple[str, ...]] | None:
    if root is None or posixpath.basename(rel) != "package-info.java":
        return None
    decl = next((c for c in root.named_children if c.type == "package_declaration"), None)
    if decl is None:
        return None
    name = next((c for c in decl.named_children
                 if c.type in ("scoped_identifier", "identifier")), None)
    namespaces = namespaces_of(list(decl.named_children), source)
    if name is None or not namespaces:
        return None
    return node_text(name, source), namespaces


def _resolve_namespaces(values: list[tuple[str, tuple[str, ...]]],
                        index: Any) -> dict[str, tuple[str, ...] | None] | None:
    out: dict[str, tuple[str, ...] | None] = {}
    for package, namespaces in values:
        record_distinct(out, package, namespaces)
    return out or None


def annotation_name(node: Node, source: bytes) -> str | None:
    if node.type not in ("annotation", "marker_annotation"):
        return None
    name = node.child_by_field_name("name")
    return node_text(name, source).rsplit(".", 1)[-1] if name is not None else None


def annotation_values(node: Node, source: bytes, key: str = "value") -> list[Node]:
    """The value nodes of ``key`` (positional or ``key = …``), arrays flattened."""
    args = node.child_by_field_name("arguments")
    if args is None:
        return []
    out: list[Node] = []
    for arg in args.named_children:
        if arg.type == "element_value_pair":
            k = arg.child_by_field_name("key")
            if k is None or node_text(k, source) != key:
                continue
            arg = arg.child_by_field_name("value") or arg
        elif key != "value":
            continue
        if arg.type == "element_value_array_initializer":
            out.extend(arg.named_children)
        else:
            out.append(arg)
    return out


def string_value(node: Node, source: bytes) -> str | None:
    """A plain string literal's value; None for anything else (a constant, a concatenation)."""
    if node.type != "string_literal":
        return None
    parts = node.named_children
    if any(p.type != "string_fragment" for p in parts):
        return None
    return "".join(node_text(p, source) for p in parts)


def namespaces_of(annotations: list[Node], source: bytes) -> tuple[str, ...]:
    """Namespaces an ``@Namespace`` / ``@Namespaces`` among ``annotations`` declares."""
    out: list[str] = []
    for ann in annotations:
        name = annotation_name(ann, source)
        inner = [ann] if name == "Namespace" else (
            [a for a in annotation_values(ann, source) if annotation_name(a, source) == "Namespace"]
            if name == "Namespaces" else [])
        for item in inner:
            for value in annotation_values(item, source):
                text = string_value(value, source)
                if text is not None:
                    out.append(text)
    return tuple(dict.fromkeys(out))


_SETTINGS_GATE = (b"<struts", b"struts.action.extension", b"struts.convention.",
                  b"struts.mapper.class", _PLUGIN, *(f.encode() for f in STRUTS_FILTERS))
register_fact(FactSpec(
    name=SETTINGS, language="java", gate=_SETTINGS_GATE,
    collect=_collect_settings, resolve=_resolve_settings,
    sources=(".xml", ".gradle"),
))
# The same settings on the ``config`` index, for the ``struts.xml`` parser: a configuration file
# is not Java, so it never sees the Java index. One collect and resolve, so the XML actions and
# the annotated ones follow the same module-wide rules.
register_fact(FactSpec(
    name=SETTINGS, language="config", gate=_SETTINGS_GATE,
    collect=_collect_settings, resolve=_resolve_settings,
    sources=(".xml",),  # Struts files and web.xml; `struts.properties` is read beside them
))
register_fact(FactSpec(
    name=PACKAGE_NAMESPACES, language="java", gate=(b"@Namespace",),
    collect=_collect_namespaces, resolve=_resolve_namespaces,
))
