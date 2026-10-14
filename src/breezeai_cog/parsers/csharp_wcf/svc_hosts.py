"""WCF ``.svc`` hosts as a registered fact, ``wcf.svc-hosts`` (see ``parsers/additive.py``).

A ``.svc`` file is wiring: its directive names the class that serves an address, and the class's
REST operations are served under that file's path::

    <%@ ServiceHost Service="Shop.Orders.OrderService" %>      // Services/Orders.svc
    public class OrderService : IOrderService { … }             // Shop/Orders/OrderService.cs
    [ServiceContract] public interface IOrderService {
        [OperationContract, WebGet(UriTemplate = "orders/{id}")] Order Get(string id);
    }                                                          // → GET /Services/Orders.svc/orders/{id}

The ``.svc`` file belongs to the ServiceHost parser, and the contract may sit in any ``.cs`` file
(beside the ``.svc`` as ``X.svc.cs``, elsewhere, or on an interface the class implements). So
the fact reads ``.svc`` files from the C# index (``FactSpec.sources``): service type → host path.
A type named by two ``.svc`` files is ambiguous and has no host (honest-null).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..wiring import Wiring, resolve_wiring

FACT = "wcf.svc-hosts"

#: ``Service="Ns.Type"`` inside a ``.svc`` ``<%@ ServiceHost … %>`` directive.
SVC_SERVICE_RE = re.compile(
    rb'<%@\s*ServiceHost\b[^%]*?\bService\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE
)


@dataclass(frozen=True)
class SvcHosts:
    """``by_fqn``: the service type a ``.svc`` names → that ``.svc`` path. ``Service=`` is a
    fully-qualified type name, so a type is matched only by its own fully-qualified name: a
    same-named class in another namespace is a different service."""

    by_fqn: dict[str, str] = field(default_factory=dict)

    def host(self, fqn: str | None) -> str | None:
        return self.by_fqn.get(fqn) if fqn is not None else None


def svc_hosts(index: Any | None) -> SvcHosts:
    """The repo's ``.svc`` hosts (empty without an index, e.g. single-file parsing)."""
    return index_fact(index, FACT) or SvcHosts()


def _collect(root: Node | None, source: bytes, rel: str, repo_root: Path | None) -> Wiring | None:
    m = SVC_SERVICE_RE.search(source)
    if m is None:
        return None
    service = m.group(1).decode("utf-8", "replace").strip()
    return Wiring((service,), "host", rel, f"{rel}:1") if service else None


def _resolve(values: list[Wiring], index: object) -> SvcHosts | None:
    by_fqn = {scope[0]: path for scope, path in resolve_wiring(values).items()}
    return SvcHosts(by_fqn) if by_fqn else None


register_fact(FactSpec(
    name=FACT, language="csharp", gate=(b"ServiceHost",), sources=(".svc",),
    collect=_collect, resolve=_resolve,
))
