"""HTTP handlers registered in ``web.config`` as a registered fact, ``webforms.config-handlers``.

A handler class needs no ``.ashx`` file: ``web.config`` can map a path to it, under
``system.web/httpHandlers`` (classic pipeline) or ``system.webServer/handlers`` (IIS 7
integrated pipeline), optionally inside a ``<location path>``::

    <location path="Api">                                          <!-- Admin/web.config -->
      <system.web><httpHandlers>
        <add verb="*" path="*.ashx" type="Shop.Api.ApiDispatcher, Shop" />
    public class ApiDispatcher : IHttpHandler {                    // Shop/Api/ApiDispatcher.cs
        public void ProcessRequest(HttpContext context) { … }      //   → ANY /Admin/Api/*.ashx

``web.config`` belongs to the config parser, and the class may sit in any ``.cs`` file, so the
fact reads ``web.config`` files from the C# index (``FactSpec.sources``): handler type → its
``(endpoint, method)`` pairs. The endpoint is the config's folder under the application root
(:func:`~..dotnet_common.web_app_root`), then the ``<location path>``, then the ``path`` — kept
as written, so a wildcard (``*.ashx``) stays a pattern. ``verb="*"`` is ``ANY``; a verb list
gives one pair per verb. Build transforms (``Web.Release.config``) are not read: they patch
another file and are not configuration of their own.
"""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..additive import FactSpec, index_fact, register_fact
from ..dotnet_common import web_app_root

FACT = "webforms.config-handlers"

_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
#: The two handler sections: ``(parent section, list element)``.
_SECTIONS = (("system.web", "httpHandlers"), ("system.webServer", "handlers"))


@dataclass(frozen=True)
class ConfigHandlers:
    """``by_fqn``: a handler type ``web.config`` names → its ``(endpoint, method)`` pairs,
    sorted. ``type=`` is a fully-qualified type name, so a class is matched only by its own
    fully-qualified name."""

    by_fqn: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)


def config_handlers(index: Any | None) -> ConfigHandlers:
    """The repo's registered handlers (empty without an index, e.g. single-file parsing)."""
    fact = index_fact(index, FACT)
    return fact if isinstance(fact, ConfigHandlers) else ConfigHandlers()


def _local(tag: object) -> str:
    """An element's name without its XML namespace (``{ns}handlers`` → ``handlers``)."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _methods(verb: str | None) -> list[str]:
    """``*`` → ``ANY``; a verb list → each known HTTP verb. Absent or unknown → none."""
    verbs = [v.strip().upper() for v in (verb or "").split(",") if v.strip()]
    if "*" in verbs:
        return ["ANY"]
    return sorted({v for v in verbs if v in _VERBS})


def _endpoint(*parts: str) -> str | None:
    """The joined path, ``None`` when empty or when it leaves the application."""
    segs = [p.strip().replace("\\", "/").strip("/") for p in parts]
    joined = "/".join(s for s in segs if s and s != ".")
    if not joined:
        return None
    norm = posixpath.normpath(joined)
    return None if norm.startswith("..") else "/" + norm


def _collect(
    root: None, source: bytes, rel: str, repo_root: Path | None
) -> list[tuple[str, str, str]] | None:
    """``(type, endpoint, method)`` for each handler ``<add>`` of one ``web.config``."""
    if posixpath.basename(rel).lower() != "web.config":
        return None
    try:
        config = ET.fromstring(source)
    except ET.ParseError:
        return None
    folder = posixpath.dirname(rel)
    app = web_app_root(rel, repo_root) if repo_root is not None else folder
    base = posixpath.relpath(folder, app) if folder != app else ""
    scopes = [("", config)] + [(loc.get("path") or "", loc) for loc in _child(config, "location")]
    out: list[tuple[str, str, str]] = []
    for location, scope in scopes:
        for section, name in _SECTIONS:
            for parent in _child(scope, section):
                for handlers in _child(parent, name):
                    for add in _child(handlers, "add"):
                        fqn = (add.get("type") or "").split(",", 1)[0].strip()
                        endpoint = _endpoint(base, location, add.get("path") or "")
                        if not fqn or "`" in fqn or endpoint is None:
                            continue
                        out.extend((fqn, endpoint, m) for m in _methods(add.get("verb")))
    return out or None


def _resolve(
    collected: list[list[tuple[str, str, str]]], index: object
) -> ConfigHandlers | None:
    """Every type's distinct ``(endpoint, method)`` pairs: the same mapping under both
    sections, or in two configs of one application, is one pair."""
    by_fqn: dict[str, set[tuple[str, str]]] = {}
    for entries in collected:
        for fqn, endpoint, method in entries:
            by_fqn.setdefault(fqn, set()).add((endpoint, method))
    if not by_fqn:
        return None
    return ConfigHandlers({fqn: tuple(sorted(pairs)) for fqn, pairs in sorted(by_fqn.items())})


register_fact(FactSpec(
    name=FACT, language="csharp", gate=(b"httpHandlers", b"<handlers"), sources=("web.config",),
    collect=_collect, resolve=_resolve,
))
