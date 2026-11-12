"""The C# lookups for WCF / ASMX operations (the rules are :mod:`..wcf_common`): a ``UriTemplate``
read from the C# tree, so a ``const string`` resolves; the method implementing an interface
operation, from the C# index (``implementers``, ``class_heritage``, ``csharp.method-lines``); and a
contract's ``.svc``, beside the class or anywhere through the ``wcf.svc-hosts`` fact."""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ...schemas import Class, Decorator, FileRecord, Function
from ..aspnet_areas import attribute_args, attribute_node, namespace_of, string_value
from ..emits import Emit
from ..handler_sites import handler_location, locate_method
from ..wcf_common import (
    UNKNOWN, WEB_GET, WEB_INVOKE, Handler, WcfLookups, detect_asmx_services, svc_host,
)
from ..wcf_common import detect_wcf_services as _detect_wcf_services
from .svc_hosts import svc_hosts

__all__ = ["detect_asmx_services", "detect_wcf_services"]


def _web_methods(root: Node) -> dict[int, Node]:
    """Method declarations by their first line (attributes included, as ``Function.startLine``)."""
    out: dict[int, Node] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "method_declaration":
            out[node.start_point[0] + 1] = node
        else:
            stack.extend(node.named_children)
    return out


def _uri_template(method: Node | None, source: bytes, index: object | None) -> object:
    """The ``UriTemplate`` of a method's WebGet / WebInvoke: its value — a literal or a constant
    (``Routes.GetOrder``, :func:`..aspnet_areas.string_value`) — None when it has none (WCF then
    serves the operation name), :data:`~..wcf_common.UNKNOWN` when it is set but cannot be read
    (a concatenation, a call, an unknown or ambiguous constant)."""
    if method is None:
        return UNKNOWN
    for name in (WEB_GET, WEB_INVOKE):
        attr = attribute_node(method, name, source)
        if attr is not None:
            value = attribute_args(attr, source)[1].get("UriTemplate")
            if value is None:
                return None
            got = string_value(value, source, index, namespace_of(method, source))
            return UNKNOWN if got is None else got
    return UNKNOWN


def _implementing_handler(
    cls_name: str, op: str, record: FileRecord, index: object | None
) -> Handler | None:
    """For a contract declared on an interface: the method with a body that runs —
    ``(Class.Method, handlerLine, handlerPath)`` when exactly one type in the repo implements the
    interface and declares that method; else None (the interface method stays the handler). In
    another file it is found through ``csharp.method-lines`` among the implementing type's own
    files; not found there (overloads, a base class's method) → no line, never a guess."""
    implementers = getattr(index, "implementers", None)
    heritage_map = getattr(index, "class_heritage", None)
    if not isinstance(implementers, dict) or not isinstance(heritage_map, dict):
        return None
    impl = implementers.get(cls_name)
    if impl is None:
        return None
    simple = impl.rsplit(".", 1)[-1]
    heritage = heritage_map.get(simple)
    if heritage is None or op not in heritage.methods:
        return None
    impl_cls = next((c for c in record.classes if c.name == simple), None)
    line = next((f.startLine for f in record.functions
                 if impl_cls is not None and f.parentId == impl_cls.id and f.name == op), None)
    if line is not None:
        return f"{simple}.{op}", line, None
    types = getattr(index, "types", None)
    files = types.get(impl) if isinstance(types, dict) else None
    site = locate_method(index, "csharp", simple, op, within=files) if files else None
    return f"{simple}.{op}", *handler_location(site, record.path)


def _registered_host(cls: Class, path: str, index: object | None) -> str | None:
    """The ``.svc`` serving ``cls`` from the repo's ``wcf.svc-hosts`` fact: a class by its own
    type, an interface contract by its one implementing type (``index.implementers``)."""
    hosts = svc_hosts(index)
    if not hosts.by_fqn:
        return None
    if cls.type == "interface":
        implementers = getattr(index, "implementers", None)
        impl = implementers.get(cls.name) if isinstance(implementers, dict) else None
        return hosts.host(impl)
    types = getattr(index, "types", None)
    fqns = [fqn for fqn, files in (types or {}).items()
            if fqn.rsplit(".", 1)[-1] == cls.name and path in files] if isinstance(types, dict) else []
    return hosts.host(fqns[0] if len(fqns) == 1 else None)


def detect_wcf_services(
    record: FileRecord, root: Node, source: bytes, repo_root: Path | None = None,
    index: object | None = None,
) -> list[Emit]:
    """:func:`~..wcf_common.detect_wcf_services` with the C# lookups."""
    methods = _web_methods(root)

    def uri_template(fn: Function, web: Decorator) -> object:
        return _uri_template(methods.get(fn.startLine), source, index)

    lookups = WcfLookups(
        host=lambda cls: svc_host(cls.path or record.path, repo_root, cls.name)
        or _registered_host(cls, record.path, index),
        uri_template=uri_template,
        implementer=lambda iface, fn: _implementing_handler(iface, fn.name, record, index),
    )
    return _detect_wcf_services(record, root, lookups, repo_root)
