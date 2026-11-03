"""The ``grpc.services`` fact: the service each generated ``RegisterXServer`` function registers,
read from the generated ``*_grpc.pb.go`` code (protoc-gen-go-grpc, any version)::

    var Auth_ServiceDesc = grpc.ServiceDesc{
        ServiceName: "auth.Auth",
        Methods: []grpc.MethodDesc{{MethodName: "Login", Handler: _Auth_Login_Handler}},
        Streams: []grpc.StreamDesc{{StreamName: "Watch", ServerStreams: true}},
    }
    func RegisterAuthServer(s grpc.ServiceRegistrar, srv AuthServer) {
        s.RegisterService(&Auth_ServiceDesc, srv)
    }

    services[("authpb", "RegisterAuthServer")] → ("auth.Auth", ("Login", "Watch"))

The service name is kept as the descriptor declares it — the name the transport matches on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..go.declarations import package_dir
from ..treesitter import node_text

FACT = "grpc.services"

Service = tuple[str, tuple[str, ...]]  # (service name, method names)


def _unwrap(node: Node | None) -> Node | None:
    while node is not None and node.type == "literal_element" and node.named_children:
        node = node.named_children[0]
    return node


def _pairs(literal: Node | None, source: bytes) -> dict[str, Node]:
    """``{Key: value, …}`` → key → value node."""
    out: dict[str, Node] = {}
    if literal is None:
        return out
    for el in literal.named_children:
        if el.type != "keyed_element" or len(el.named_children) < 2:
            continue
        key, value = (_unwrap(c) for c in el.named_children[:2])
        if key is not None and value is not None:
            out[node_text(key, source)] = value
    return out


def _string(node: Node | None, source: bytes) -> str | None:
    if node is not None and node.type in {"interpreted_string_literal", "raw_string_literal"}:
        return node_text(node, source)[1:-1]
    return None


def _names(node: Node | None, key: str, source: bytes) -> list[str]:
    """``[]grpc.MethodDesc{{MethodName: "A"}, …}`` → ``["A", …]``."""
    if node is None or node.type != "composite_literal":
        return []
    body = node.child_by_field_name("body")
    out: list[str] = []
    for el in body.named_children if body is not None else []:
        el = _unwrap(el)
        if el is None:
            continue
        if el.type == "composite_literal":
            el = el.child_by_field_name("body")
        name = _string(_pairs(el, source).get(key), source) if el is not None else None
        if name:
            out.append(name)
    return out


def _descriptors(root: Node, source: bytes) -> dict[str, Service]:
    out: dict[str, Service] = {}
    for decl in root.named_children:
        if decl.type != "var_declaration":
            continue
        specs = [c for c in decl.named_children if c.type == "var_spec"]
        for group in decl.named_children:
            if group.type == "var_spec_list":
                specs.extend(c for c in group.named_children if c.type == "var_spec")
        for spec in specs:
            value = spec.child_by_field_name("value")
            lit = value.named_children[0] if value is not None and value.named_children else None
            if lit is None or lit.type != "composite_literal":
                continue
            if not node_text(lit.child_by_field_name("type"), source).endswith("ServiceDesc"):
                continue
            pairs = _pairs(lit.child_by_field_name("body"), source)
            service = _string(pairs.get("ServiceName"), source)
            if not service:
                continue
            methods = _names(pairs.get("Methods"), "MethodName", source) + \
                _names(pairs.get("Streams"), "StreamName", source)
            for name in spec.children_by_field_name("name"):
                out[node_text(name, source)] = (service, tuple(methods))
    return out


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> list[Any] | None:
    descriptors = _descriptors(root, source)
    if not descriptors:
        return None
    found: list[Any] = []
    for decl in root.named_children:
        if decl.type != "function_declaration":
            continue
        name = node_text(decl.child_by_field_name("name"), source)
        if not (name.startswith("Register") and name.endswith("Server")):
            continue
        stack = [decl.child_by_field_name("body")]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            if node.type == "call_expression" and node_text(node.child_by_field_name("function"), source).endswith(".RegisterService"):
                args = node.child_by_field_name("arguments")
                first = args.named_children[0] if args is not None and args.named_children else None
                desc = node_text(first, source).lstrip("&") if first is not None else ""
                if desc in descriptors:
                    found.append((package_dir(rel), name, descriptors[desc]))
                break
            stack.extend(node.named_children)
    return found or None


def _resolve(values: list[list[Any]], index: Any) -> dict[tuple[str, str], Service | None] | None:
    out: dict[tuple[str, str], Service | None] = {}
    for value in values:
        for pkg, name, service in value:
            out[(pkg, name)] = service if out.get((pkg, name), service) == service else None
    return out or None


def services(index: Any | None) -> dict[tuple[str, str], Service | None]:
    return index_fact(index, FACT) or {}


register_fact(FactSpec(name=FACT, language="go", gate=(b"ServiceDesc",),
                       collect=_collect, resolve=_resolve))
