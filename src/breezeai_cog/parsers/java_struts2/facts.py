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
    STRUTS_ROOT, SUFFIX_KEY, WEB_XML_LEVEL, XML_LEVEL, Layered, read_properties,
    web_xml_constants, xml_constants,
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


_Found = list[tuple[str, int, dict[str, str]]]  # (module, level, constants)


def _collect_settings(root: Node | None, source: bytes, rel: str,
                      repo_root: Path | None) -> _Found | None:
    name = posixpath.basename(rel)
    module = module_key(rel)
    found: _Found = []

    def keep(level: int, constants: dict[str, str]) -> None:
        picked = {k: v for k, v in constants.items() if k in _KEYS}
        if picked:
            found.append((module, level, picked))

    if name == "web.xml":
        keep(WEB_XML_LEVEL, web_xml_constants(source))
    elif rel.endswith(".xml") and STRUTS_ROOT.search(source):
        keep(XML_LEVEL, xml_constants(source))
        if repo_root is not None:
            keep(PROPERTIES_LEVEL, read_properties(repo_root / posixpath.dirname(rel)))
    if name in _BUILD_FILES and _PLUGIN in source:
        found.append((module, XML_LEVEL, {CONVENTION: "true"}))
    return found or None


def _resolve_settings(values: list[_Found], index: Any) -> dict[str, dict[str, str | None]] | None:
    layered = Layered()
    for file_values in values:
        for module, level, constants in file_values:
            layered.add(module, level, constants)
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


register_fact(FactSpec(
    name=SETTINGS, language="java",
    gate=(b"<struts", b"struts.action.extension", b"struts.convention.",
          b"struts.mapper.class", _PLUGIN),
    collect=_collect_settings, resolve=_resolve_settings,
    sources=(".xml", ".gradle"),
))
register_fact(FactSpec(
    name=PACKAGE_NAMESPACES, language="java", gate=(b"@Namespace",),
    collect=_collect_namespaces, resolve=_resolve_namespaces,
))
