"""A JAX-RS application's base path from configuration, per module.

Besides ``@ApplicationPath`` (:mod:`.prefixes`), the base every resource of a module is served
under can be set outside the code:

* ``src/main/webapp/WEB-INF/web.xml`` — the ``<url-pattern>`` of the servlet that runs the JAX-RS
  application (Jersey's ``ServletContainer``, RESTEasy's dispatcher, CXF's servlet, or a servlet
  named or initialised with ``javax/jakarta.ws.rs.Application``), and RESTEasy's
  ``resteasy.servlet.mapping.prefix`` context parameter::

      <servlet-mapping><servlet-name>jersey</servlet-name><url-pattern>/rest/*</url-pattern>
      → /rest

* the module's base ``application.properties`` / ``.yml`` (:func:`..java.config.spring_properties`):
  Quarkus ``quarkus.rest.path`` / ``quarkus.resteasy.path``, Spring Boot's
  ``spring.jersey.application-path``.

Each source gives a value, or None when it is set but cannot be read (two patterns, a pattern
that is not ``/x/*``, a ``${…}`` placeholder).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from ..java.config import spring_properties

_WEB_XML = "src/main/webapp/WEB-INF/web.xml"
_JAXRS_SERVLETS = frozenset({
    "org.glassfish.jersey.servlet.ServletContainer",
    "com.sun.jersey.spi.container.servlet.ServletContainer",
    "org.jboss.resteasy.plugins.server.servlet.HttpServletDispatcher",
    "org.apache.cxf.jaxrs.servlet.CXFNonSpringJaxrsServlet",
})
_APPLICATION = frozenset({"javax.ws.rs.Application", "jakarta.ws.rs.Application",
                          "javax.ws.rs.core.Application", "jakarta.ws.rs.core.Application"})
#: Property keys that set the base path, and whether they join an ``@ApplicationPath`` (Quarkus)
#: rather than replace it.
PROPERTY_KEYS = {"quarkus.rest.path": True, "quarkus.resteasy.path": True,
                 "spring.jersey.application-path": False}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _text(el: ET.Element, name: str) -> str:
    found = _children(el, name)
    return (found[0].text or "").strip() if found else ""


def _base(pattern: str) -> str | None:
    """``/rest/*`` → ``/rest``; ``/*`` → ``""``; anything else → None."""
    if pattern in ("/*", "/"):
        return ""
    if pattern.startswith("/") and pattern.endswith("/*") and "*" not in pattern[:-2]:
        return pattern[:-2]
    return None


def web_xml_bases(module_dir: Path) -> list[str | None]:
    """The JAX-RS base paths a module's ``web.xml`` sets (see the module docstring)."""
    try:
        root = ET.parse(module_dir / _WEB_XML).getroot()
    except (OSError, ET.ParseError):
        return []
    out: list[str | None] = []
    for param in _children(root, "context-param"):
        if _text(param, "param-name") == "resteasy.servlet.mapping.prefix":
            value = _text(param, "param-value")
            out.append(value.rstrip("/") if value.startswith("/") or value == "" else None)
    servlets = set()
    for servlet in _children(root, "servlet"):
        name = _text(servlet, "servlet-name")
        params = {_text(p, "param-name") for p in _children(servlet, "init-param")}
        if _text(servlet, "servlet-class") in _JAXRS_SERVLETS or name in _APPLICATION \
                or params & _APPLICATION:
            servlets.add(name)
    for name in _APPLICATION:  # a servlet 3 mapping for the default application, no <servlet>
        servlets.add(name)
    for mapping in _children(root, "servlet-mapping"):
        if _text(mapping, "servlet-name") not in servlets:
            continue
        patterns = [(p.text or "").strip() for p in _children(mapping, "url-pattern")]
        if len(patterns) != 1:
            out.append(None)  # served at several patterns: no single base
        else:
            out.append(_base(patterns[0]))
    return out


def property_bases(repo_root: Path, module: str) -> list[tuple[str, str | None]]:
    """``(key, value)`` for each base-path key the module's base config sets."""
    props = spring_properties(repo_root, [f"{module}/src/main/java/_.java" if module
                                          else "src/main/java/_.java"]).get(module, {})
    out: list[tuple[str, str | None]] = []
    for key in PROPERTY_KEYS:
        if key in props:
            value = props[key]
            out.append((key, None if value is None or "${" in value else "/" + value.strip("/")
                        if value.strip("/") else ""))
    return out
