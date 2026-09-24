"""Go HTTP and framework route detection."""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import node_text


def _receiver_types(source_text: str) -> dict[str, str]:
    receiver_types = {
        name: {
            "http.Server": "net-http", "gin.Engine": "gin", "echo.Echo": "echo",
            "fiber.App": "fiber", "chi.Router": "chi",
        }[type_name]
        for name, type_name in re.findall(
            r"\b([A-Za-z_]\w*)\s+\*?(http\.Server|gin\.Engine|echo\.Echo|fiber\.App|chi\.Router)\b",
            source_text,
        )
    }
    receiver_types.update(
        {
            name: "gorilla-mux"
            for name in re.findall(r"\b([A-Za-z_]\w*)\s*:?=\s*mux\.NewRouter\s*\(", source_text)
        }
    )
    receiver_types.update(
        {
            name: framework
            for name, framework in re.findall(
                r"\b([A-Za-z_]\w*)\s*:?=\s*(gin\.(?:New|Default)|echo\.New|fiber\.New|chi\.NewRouter)\s*\(",
                source_text,
            )
            for framework in [
                "gin" if framework.startswith("gin.") else
                "echo" if framework.startswith("echo.") else
                "fiber" if framework.startswith("fiber.") else "chi"
            ]
        }
    )
    return receiver_types


def _framework_for_call(
    function: Node | None,
    source: bytes,
    source_text: str,
    receiver_types: dict[str, str],
) -> str | None:
    """Infer a route framework from the receiver and its Go type/import usage."""
    if function is None or function.type != "selector_expression":
        return None
    operand = function.child_by_field_name("operand")
    if operand is None:
        return None
    receiver = node_text(operand, source).strip()
    receiver_name = receiver.rsplit(".", 1)[-1]
    if receiver_name == "http":
        return "net-http"
    if receiver_name == "mux" and "gorilla/mux" in source_text:
        return "gorilla-mux"
    if receiver_name in receiver_types:
        return receiver_types[receiver_name]
    if receiver.startswith("gin."):
        return "gin"
    if receiver.startswith("echo."):
        return "echo"
    if receiver.startswith("fiber."):
        return "fiber"
    if receiver.startswith("chi."):
        return "chi"
    return None


def detect_routes(
    root: Node,
    source: bytes,
    path: str,
    *,
    seen_ids: set[str],
    parent_id: str,
    owners: list[tuple[int, int, str]] | None = None,
) -> list[Statement]:
    """Return route statements for common Go frameworks. This is intentionally a light
    detector: it matches common route registration forms without introducing a large new
    framework-specific surface area."""
    source_text = source.decode("utf-8", "replace")
    receiver_types = _receiver_types(source_text)
    out: list[Statement] = []
    def walk(node: Node) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            text = node_text(fn, source) if fn is not None else ""
            method = text.rsplit(".", 1)[-1]
            detected = _framework_for_call(fn, source, source_text, receiver_types)
            if (
                detected is None
                and method.startswith("Register")
                and "google.golang.org/grpc" in source_text
            ):
                detected = "grpc"
            if method in {"HandleFunc", "Handle"} and detected is None:
                detected = "net-http" if text.startswith("http.") else "gorilla-mux" if text.startswith("mux.") else None
            if detected == "net-http" and method not in {"HandleFunc", "Handle"}:
                detected = None
            framework = detected if (
                method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
                or method in {"HandleFunc", "Handle"}
                or detected == "grpc"
            ) else None
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
                owner_id = parent_id
                for owner_start, owner_end, candidate_id in owners or []:
                    if owner_start <= line <= owner_end:
                        owner_id = candidate_id
                        break
                out.append(Statement(
                    id=disambiguate(statement_id(path, line, node.start_point[1]), seen_ids),
                    parentId=owner_id, nodeType=node.type, semanticType="route",
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
        return "gorilla-mux"
    if "net/http" in text:
        return "net-http"
    if "grpc" in text:
        return "grpc"
    return None
