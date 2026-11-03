"""Rules shared by the ``struts.xml`` parser and the annotation detector.

Struts2 serves an action at ``<namespace>/<action name>.<extension>``:

* the **namespace** is the ``<package namespace="…">`` (XML) or ``@Namespace`` / the Convention
  plugin's package rule (annotations); the default namespace ``""`` and the root namespace ``/``
  both put the action at the top level;
* the **extension** is the first entry of the ``struts.action.extension`` constant (default
  ``action``; an empty first entry means no extension).

A constant can be set in three places, read by Struts in this order, a later one overriding an
earlier one (:class:`Layered`): a ``<constant>`` in a Struts XML file, ``struts.properties``,
and an ``<init-param>`` of the Struts filter in ``web.xml``.

Every Struts2 version is read the same way: 2.0–2.5 and 6.x (``com.opensymphony.xwork2``) and
7.x (``org.apache.struts2``); the DTD and its version are never checked.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

FRAMEWORK = "struts2"
DEFAULT_EXTENSION = "action"
EXTENSION_KEY = "struts.action.extension"
LOCATORS_KEY = "struts.convention.package.locators"
SUFFIX_KEY = "struts.convention.action.suffix"
RESULT_PATH_KEY = "struts.convention.result.path"
MAP_ALL_KEY = "struts.convention.action.mapAllMatches"
MAPPER_KEY = "struts.mapper.class"
DEFAULT_LOCATORS = ("action", "actions", "struts", "struts2")
DEFAULT_SUFFIX = "Action"

#: ``<constant name="k" value="v"/>`` (attributes in either order).
_XML_CONSTANT = re.compile(
    rb"""<constant\s+(?:name\s*=\s*["']([^"']+)["']\s+value\s*=\s*["']([^"']*)["']"""
    rb"""|value\s*=\s*["']([^"']*)["']\s+name\s*=\s*["']([^"']+)["'])""")
#: ``k=v`` / ``k: v`` / ``k v`` in a ``.properties`` file.
_PROPERTY = re.compile(rb"^[ \t]*([\w.-]+)[ \t]*[=: \t][ \t]*(.*?)[ \t]*$", re.MULTILINE)
#: The root element of a Struts2 configuration (never Struts 1's ``<struts-config>``).
STRUTS_ROOT = re.compile(rb"<struts[\s>]")
#: Where a constant is set, lowest precedence first.
XML_LEVEL, PROPERTIES_LEVEL, WEB_XML_LEVEL = 0, 1, 2
#: The Struts2 dispatcher filters (2.0's ``FilterDispatcher`` to 7.x), by class simple name.
_STRUTS_FILTERS = ("StrutsPrepareAndExecuteFilter", "StrutsPrepareFilter",
                   "StrutsExecuteFilter", "FilterDispatcher")


def xml_constants(source: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _XML_CONSTANT.finditer(source):
        name, value = (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(4), m.group(3))
        out[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def properties_constants(source: bytes) -> dict[str, str]:
    return {
        m.group(1).decode("utf-8", "replace"): m.group(2).decode("utf-8", "replace")
        for m in _PROPERTY.finditer(source)
        if m.group(1).startswith(b"struts.")
    }


def read_properties(directory: Path) -> dict[str, str]:
    """The ``struts.*`` keys of ``struts.properties`` in ``directory`` (empty when absent)."""
    try:
        return properties_constants((directory / "struts.properties").read_bytes())
    except OSError:
        return {}


def web_xml_constants(source: bytes) -> dict[str, str]:
    """The ``struts.*`` init-params of the Struts filter in a ``web.xml`` (empty when none or
    unreadable)."""
    if b"struts." not in source:
        return {}
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for item in root:
        if _local(item.tag) != "filter" or not any(
                f in _text(item, "filter-class") for f in _STRUTS_FILTERS):
            continue
        for param in (c for c in item if _local(c.tag) == "init-param"):
            name = _text(param, "param-name")
            if name.startswith("struts."):
                out[name] = _text(param, "param-value")
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(node: ET.Element, name: str) -> str:
    return next(((c.text or "").strip() for c in node if _local(c.tag) == name), "")


class Layered:
    """Constants per module from several sources, with Struts' precedence: the highest level
    setting a key wins; two different values at that level make the key None (unknown)."""

    def __init__(self) -> None:
        self._found: dict[str, dict[str, tuple[int, str | None]]] = {}

    def add(self, module: str, level: int, constants: dict[str, str]) -> None:
        found = self._found.setdefault(module, {})
        for key, value in constants.items():
            current = found.get(key)
            if current is None or level > current[0]:
                found[key] = (level, value)
            elif level == current[0] and current[1] != value:
                found[key] = (level, None)

    def resolve(self) -> dict[str, dict[str, str | None]]:
        return {module: {k: v for k, (_, v) in found.items()}
                for module, found in self._found.items()}


def suffix_of(settings: dict[str, str | None]) -> str | None:
    """The action suffix a module's settings give: the default when the extension is not set,
    None when its sources disagree or it is a placeholder."""
    if EXTENSION_KEY not in settings:
        return extension(None)
    value = settings[EXTENSION_KEY]
    return None if value is None else extension(value)


def extension(value: str | None) -> str | None:
    """The suffix (``.action``, ``""``) the ``struts.action.extension`` value gives, or None
    when the value is a placeholder that cannot be read."""
    if value is None:
        return "." + DEFAULT_EXTENSION
    if "${" in value:
        return None
    first = value.split(",", 1)[0].strip()
    return "." + first if first else ""


def action_endpoint(namespace: str | None, name: str, suffix: str | None) -> str | None:
    """``/admin/login.action`` for namespace ``/admin``, action ``login``, suffix ``.action``. A
    name starting with ``/`` is absolute; a namespace written without its leading ``/``
    (``namespace="admin"``) is the same namespace; an unknown namespace or suffix gives None."""
    if suffix is None or not name or "${" in name:
        return None
    if name.startswith("/"):
        return name + suffix
    if namespace is None:
        return None
    return "/" + namespace.strip("/") + "/" + name + suffix if namespace.strip("/") else \
        "/" + name + suffix


def convention_name(class_name: str, suffix: str = DEFAULT_SUFFIX) -> str:
    """The Convention plugin's action name for a class: the suffix dropped, then each capital
    after the first preceded by ``-``, all lower case (``UserProfileAction`` → ``user-profile``)."""
    stem = class_name[: -len(suffix)] if suffix and class_name.endswith(suffix) and \
        class_name != suffix else class_name
    out = []
    for i, ch in enumerate(stem):
        if ch.isupper() and i > 0:
            out.append("-")
        out.append(ch.lower())
    return "".join(out)


def convention_namespace(package: str, locators: tuple[str, ...]) -> str | None:
    """The namespace the Convention plugin gives a package: the segments after the first
    package locator found (``com.shop.actions.admin`` → ``/admin``; ``com.shop.actions`` →
    ``""``), or None when no locator is in the package (the plugin does not scan it)."""
    segments = package.split(".")
    for locator in locators:
        if locator in segments:
            rest = segments[segments.index(locator) + 1:]
            return "/" + "/".join(rest) if rest else ""
    return None
