"""Go HTTP and framework route detection."""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import node_text


def _route_from_http_handle(node: Node, source: bytes) -> str | None:
    if node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    text = node_text(fn, source)
    if "HandleFunc" not in text and "Handle" not in text:
        return None
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.named_children:
        if child.type in {"interpreted_string_literal", "raw_string_literal"}:
            return node_text(child, source).strip('"`')
    return None


def _route_from_gin(node: Node, source: bytes) -> str | None:
    if node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    text = node_text(fn, source)
    if not any(name in text for name in {"GET", "POST", "PUT", "PATCH", "DELETE", "Handle"}):
        return None
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.named_children:
        if child.type in {"interpreted_string_literal", "raw_string_literal"}:
            return node_text(child, source).strip('"`')
    return None


def _framework_for_call(function: Node | None, source: bytes) -> str | None:
    """Infer a route framework from the receiver and its Go type/import usage."""
    if function is None or function.type != "selector_expression":
        return None
    operand = function.child_by_field_name("operand")
    if operand is None:
        return None
    receiver = node_text(operand, source).strip()
    receiver_name = receiver.rsplit(".", 1)[-1]
    receiver_types = {
        name: framework
        for name, framework in re.findall(
            r"\b([A-Za-z_]\w*)\s+\*?(gin\.Engine|echo\.Echo|chi\.Router)\b", source.decode("utf-8", "replace")
        )
        for framework in [
            {"gin.Engine": "gin", "echo.Echo": "echo", "chi.Router": "chi"}[framework]
        ]
    }
    if receiver_name in receiver_types:
        return receiver_types[receiver_name]
    if receiver.startswith("gin."):
        return "gin"
    if receiver.startswith("echo."):
        return "echo"
    if receiver.startswith("chi."):
        return "chi"
    return None


def _imported_frameworks(source: bytes) -> set[str]:
    text = source.decode("utf-8", "replace")
    frameworks = set()
    if "github.com/gin-gonic/gin" in text:
        frameworks.add("gin")
    if "github.com/labstack/echo" in text:
        frameworks.add("echo")
    if "github.com/gofiber/fiber" in text:
        frameworks.add("fiber")
    if "github.com/go-chi/chi" in text:
        frameworks.add("chi")
    if "gorilla/mux" in text:
        frameworks.add("gorilla_mux")
    return frameworks


def detect_routes(root: Node, source: bytes, path: str, *, seen_ids: set[str], parent_id: str) -> list[Statement]:
    """Return route statements for common Go frameworks. This is intentionally a light
    detector: it matches common route registration forms without introducing a large new
    framework-specific surface area."""
    out: list[Statement] = []
    def walk(node: Node) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            text = node_text(fn, source) if fn is not None else ""
            method = text.rsplit(".", 1)[-1]
            detected = _framework_for_call(fn, source)
            if method in {"HandleFunc", "Handle"} and text.startswith("http."):
                detected = "net_http"
            elif detected is None:
                candidates = _imported_frameworks(source)
                detected = next(iter(candidates)) if len(candidates) == 1 else None
            framework = detected if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"} or method in {"HandleFunc", "Handle"} else None
            if framework is not None:
                args = node.child_by_field_name("arguments")
                endpoint = None
                handler = None
                if args is not None and args.named_children:
                    first = args.named_children[0]
                    if first.type in {"interpreted_string_literal", "raw_string_literal"}:
                        endpoint = node_text(first, source).strip('"`')
                    if len(args.named_children) > 1:
                        handler = node_text(args.named_children[-1], source)
                line = node.start_point[0] + 1
                out.append(Statement(
                    id=disambiguate(statement_id(path, line, node.start_point[1]), seen_ids),
                    parentId=parent_id, nodeType=node.type, semanticType="route",
                    text=node_text(node, source), startLine=line, endLine=node.end_point[0] + 1,
                    framework=framework, method=method.upper(), endpoint=endpoint,
                    handler=handler, path=path,
                ))
        for child in node.named_children:
            walk(child)
    walk(root)
    return out


def detect_framework(path: str | Path, source: bytes) -> str | None:
    text = source.decode("utf-8", "replace")
    if "github.com/gin-gonic/gin" in text:
        return "gin"
    if "github.com/labstack/echo" in text:
        return "echo"
    if "github.com/gofiber/fiber" in text:
        return "fiber"
    if "github.com/go-chi/chi" in text:
        return "chi"
    if "gorilla/mux" in text:
        return "gorilla_mux"
    if "net/http" in text:
        return "net_http"
    if "grpc" in text:
        return "grpc"
    return None
