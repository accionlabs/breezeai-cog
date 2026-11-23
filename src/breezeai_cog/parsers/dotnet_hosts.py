"""ASP.NET host files as a registered fact, ``dotnet.host-files`` (see ``parsers/additive.py``).

A ``.svc`` (WCF), ``.asmx`` (ASMX) or ``.ashx`` (generic handler) file is wiring: its one
``<%@ … %>`` directive names the class served at the file's address, and that class's operations
are served under the file's path::

    <%@ ServiceHost Service="Shop.Orders.OrderService"
        Factory="System.ServiceModel.Activation.WebServiceHostFactory" %>   // Services/Orders.svc
    public class OrderService : IOrderService { … }      // Services/Orders.svc.cs, or anywhere
    [ServiceContract] public interface IOrderService {
        [OperationContract, WebGet(UriTemplate = "orders/{id}")] Order Get(string id);
    }                                                    // → GET /Services/Orders.svc/orders/{id}

The host files belong to the ServiceHost parser and the classes to the C# and VB parsers, so the
fact reads the host files once, while the C# and VB indexes are built (``FactSpec.sources``), and
the parsers look hosts up in it instead of opening files:

* ``files``: each host file's path → the class its directive names, and whether a ``.svc`` uses a
  web host factory (it serves every operation over HTTP);
* ``by_fqn``: a ``.svc``'s ``Service=`` type → that ``.svc``, for a class whose host is not beside
  it. A type named by two ``.svc`` files is ambiguous and has no host (honest-null).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from .additive import FactSpec, index_fact, register_fact
from .wiring import Wiring, resolve_wiring

FACT = "dotnet.host-files"

#: Host extension → its directive's class attribute (``Service="Ns.Type"`` / ``Class="Ns.Type"``).
_DIRECTIVES: dict[str, re.Pattern[bytes]] = {
    ".svc": re.compile(
        rb'<%@\s*ServiceHost\b[^%]*?\bService\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE),
    ".asmx": re.compile(
        rb'<%@\s*WebService\b[^%]*?\bClass\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE),
    ".ashx": re.compile(
        rb'<%@\s*WebHandler\b[^%]*?\bClass\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE),
}

#: ``Factory="…WebScriptServiceHostFactory"`` (AJAX-enabled WCF) / ``WebServiceHostFactory``
#: (REST): the host serves every operation over HTTP — ``POST <file>.svc/<Operation>`` unless
#: ``[WebGet]`` / ``[WebInvoke]`` says otherwise (WCF's default for these hosts).
_WEB_FACTORY_RE = re.compile(
    rb'<%@\s*ServiceHost\b[^%]*?\bFactory\s*=\s*["\'][\w.]*\bWeb(?:Script)?ServiceHostFactory["\']',
    re.IGNORECASE)


@dataclass(frozen=True)
class HostFile:
    """A host file's directive: the class it names, as written (``Ns.Type``), and whether it
    serves over HTTP (a ``.svc`` web host factory)."""

    service: str
    web: bool = False


@dataclass(frozen=True)
class HostFiles:
    files: dict[str, HostFile] = field(default_factory=dict)
    #: A ``.svc``'s ``Service=`` type → that ``.svc``. ``Service=`` is a fully-qualified type name,
    #: so a type is matched only by its own fully-qualified name: a same-named class in another
    #: namespace is a different service.
    by_fqn: dict[str, str] = field(default_factory=dict)

    def host(self, fqn: str | None) -> str | None:
        return self.by_fqn.get(fqn) if fqn is not None else None

    def web(self, host: str | None) -> bool:
        """Whether the ``.svc`` at ``host`` serves its operations over HTTP."""
        entry = self.files.get(host) if host is not None else None
        return entry is not None and entry.web

    def code_behind_host(self, path: str, cls_name: str, ext: str) -> str | None:
        """The host file (``X.asmx`` / ``X.svc`` / ``X.ashx``) that serves class ``cls_name``
        declared in ``path`` — the code-behind convention ``X<ext>.cs`` (or ``.vb``) beside
        ``X<ext>`` — when that file is known and its directive names the class (VB names are
        case-insensitive, so a ``.vb`` code-behind matches without case). Anything else → None
        (host unknown)."""
        low = path.lower()
        if not low.endswith((ext + ".cs", ext + ".vb")):
            return None
        host = path[: -len(".cs")]
        entry = self.files.get(host)
        if entry is None:
            return None
        simple = entry.service.rsplit(".", 1)[-1]
        if low.endswith(".vb"):
            return host if simple.lower() == cls_name.lower() else None
        return host if simple == cls_name else None


def host_files(index: Any | None) -> HostFiles:
    """The repo's host files (empty without an index, e.g. single-file parsing)."""
    return index_fact(index, FACT) or HostFiles()


def _collect(
    root: Node | None, source: bytes, rel: str, repo_root: Path | None,
) -> tuple[str, HostFile] | None:
    directive = _DIRECTIVES.get(posixpath.splitext(rel)[1].lower())
    m = directive.search(source) if directive is not None else None
    if m is None:
        return None
    service = m.group(1).decode("utf-8", "replace").strip()
    web = rel.lower().endswith(".svc") and _WEB_FACTORY_RE.search(source) is not None
    return (rel, HostFile(service, web)) if service else None


def _resolve(values: list[tuple[str, HostFile]], index: object) -> HostFiles | None:
    files = dict(values)
    services = [Wiring((host.service,), "host", rel, f"{rel}:1")
                for rel, host in values if rel.lower().endswith(".svc")]
    by_fqn = {scope[0]: path for scope, path in resolve_wiring(services).items()}
    return HostFiles(files, by_fqn) if files else None


for _language in ("csharp", "vb"):
    register_fact(FactSpec(
        name=FACT, language=_language, gate=(b"<%",), sources=tuple(_DIRECTIVES),
        collect=_collect, resolve=_resolve,
    ))
