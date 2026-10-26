"""gRPC service bindings: where the repo registers a server implementation, one ``route`` per
RPC of the service::

    authpb.RegisterAuthServer(gs, api)
        → route RPC auth.Auth/Login, routeKind rpc, handler api.Login
        → route RPC auth.Auth/Watch, routeKind rpc, handler api.Watch

The service's methods come from the generated code (``grpc.services``, ``services.py``); the
handler is the implementation's method, located like a router handler. Generated plumbing
(``s.RegisterService(…)`` inside ``*_grpc.pb.go``) and ``reflection.Register`` are not bindings."""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..go.imports import GoIndex
from ..go.routing import file_analyzer, locate_member, types_by_function
from ..handler_sites import handler_location
from ..treesitter import node_text
from .services import services


def _register_call(call: Node, source: bytes, analyzer) -> tuple[str, str] | None:
    """``pb.RegisterXServer(…)`` / ``RegisterXServer(…)`` → (package dir, function name)."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        name, pkg = node_text(fn, source), analyzer.pkg
    elif fn.type == "selector_expression":
        operand = fn.child_by_field_name("operand")
        alias = node_text(operand, source) if operand is not None else ""
        if alias not in analyzer.aliases or analyzer.index is None:
            return None
        pkg = analyzer.index.package_dir(analyzer.aliases[alias])
        name = node_text(fn.child_by_field_name("field"), source)
    else:
        return None
    if pkg is None or not (name.startswith("Register") and name.endswith("Server")):
        return None
    return pkg, name


def _impl(node: Node | None, source: bytes) -> tuple[str | None, str | None]:
    """The implementation argument → (handler qualifier, its type when written inline)."""
    if node is None:
        return None, None
    if node.type == "identifier" or node.type == "selector_expression":
        return node_text(node, source), None
    inner = node.child_by_field_name("operand") if node.type == "unary_expression" else node
    if inner is not None and inner.type == "composite_literal":
        tname = node_text(inner.child_by_field_name("type"), source)
        return tname.rsplit(".", 1)[-1], tname
    return None, None


def detect_grpc(dc: DetectContext) -> Detection | None:
    index = dc.ctx.resolution_index if isinstance(dc.ctx.resolution_index, GoIndex) else None
    known = services(index)
    if not known:
        return None
    source = dc.source
    analyzer = file_analyzer(dc.root, source, dc.path, index)
    types = types_by_function(analyzer)
    found: list[Emit] = []
    stack = [dc.root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type != "call_expression":
            continue
        target = _register_call(node, source, analyzer)
        service = known.get(target) if target is not None else None
        if service is None:
            continue
        args = node.child_by_field_name("arguments")
        impl_node = args.named_children[1] if args is not None and len(args.named_children) > 1 else None
        qualifier, inline_type = _impl(impl_node, source)
        env = dict(types(node))
        if inline_type is not None and qualifier is not None:
            env[qualifier] = inline_type
        name, methods = service
        for method in methods:
            fill = {}
            if qualifier is not None:
                site = locate_member(analyzer, qualifier, method, env)
                line, path = handler_location(site, dc.path)
                fill = {"handler": f"{qualifier}.{method}", "handlerLine": line, "handlerPath": path}
            found.append(Emit(
                node,
                {"semanticType": "route", "framework": "grpc", "routeKind": "rpc",
                 "method": "RPC", "endpoint": f"{name}/{method}"},
                fill=fill, enrich="any",
            ))
    return Detection(found, label="grpc") if found else None


register_detector(Detector(
    name="go-grpc", gate=Gate("go", order=70, guard=b"Register", skip_fixtures=True),
    detect=detect_grpc, frameworks=("grpc",),
))
