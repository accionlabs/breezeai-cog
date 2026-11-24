"""Additive detector: calls of a Go MCP **client** built with the official SDK
(``github.com/modelcontextprotocol/go-sdk``).

::

    client := mcp.NewClient(&mcp.Implementation{Name: "app"}, nil)
    session, err := client.Connect(ctx, transport, nil)       # or: session *mcp.ClientSession
    session.CallTool(ctx, &mcp.CallToolParams{Name: "greet"})  → mcp_call  go-mcp  TOOL      greet
    session.ReadResource(ctx, &mcp.ReadResourceParams{URI: "embedded:info"})
                                                               → mcp_call  go-mcp  RESOURCE  embedded:info
    session.GetPrompt(ctx, &mcp.GetPromptParams{Name: "review"}) → mcp_call  go-mcp  PROMPT  review

* **Client session** — a name whose only binding in its function is ``<c>.Connect(…)`` where
  ``<c>``'s only binding there is ``mcp.NewClient(…)`` (or a parameter typed ``*mcp.Client``), or
  a parameter typed ``*mcp.ClientSession``.
* ``endpoint`` — ``Name`` (``URI``) of the ``&mcp.CallToolParams{…}`` (``ReadResourceParams`` /
  ``GetPromptParams``) literal passed inline, a string literal; anything else → no record.
* The record is the call's own statement, with no handler; a client-only file gets no file-level
  framework.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..go_mcp_common import (
    Binding,
    call_args,
    call_target,
    function_bindings,
    go_string,
    keyed_fields,
    literal_type,
    package_names,
    selector,
    top_function,
    walk,
)
from ..mcp_common import call_emit

FRAMEWORK = "go-mcp"
_PACKAGE = "github.com/modelcontextprotocol/go-sdk/mcp"
#: Session methods → (method, params type, field naming what they reach).
_CALLS = {
    "CallTool": ("TOOL", "CallToolParams", "Name"),
    "ReadResource": ("RESOURCE", "ReadResourceParams", "URI"),
    "GetPrompt": ("PROMPT", "GetPromptParams", "Name"),
}


def _pointer_to(type_node: Node | None, source: bytes, pkgs: set[str], name: str) -> bool:
    if type_node is None or type_node.type != "pointer_type" or not type_node.named_children:
        return False
    target = selector(type_node.named_children[0], source)
    return target is not None and target[0] in pkgs and target[1] == name


def _detect(dc: DetectContext) -> Detection | None:
    source, root = dc.source, dc.root
    pkgs = package_names(root, source, _PACKAGE)
    if not pkgs:
        return None

    def only(bindings: dict[str, list[Binding]], name: str) -> Binding | None:
        found = bindings.get(name, [])
        return found[0] if len(found) == 1 else None

    def is_client(bindings: dict[str, list[Binding]], name: str) -> bool:
        binding = only(bindings, name)
        if binding is None:
            return False
        value, type_node = binding
        if value is not None and value.type == "call_expression":
            target = selector(value.child_by_field_name("function"), source)
            return target is not None and target[0] in pkgs and target[1] == "NewClient"
        return _pointer_to(type_node, source, pkgs, "Client")

    def is_session(bindings: dict[str, list[Binding]], name: str) -> bool:
        binding = only(bindings, name)
        if binding is None:
            return False
        value, type_node = binding
        if value is not None and value.type == "call_expression":
            target = call_target(value, source)
            return target is not None and target[1] == "Connect" and is_client(bindings, target[0])
        return _pointer_to(type_node, source, pkgs, "ClientSession")

    cache: dict[tuple[int, int], dict[str, list[Binding]]] = {}
    emits: list[Emit] = []
    for call in walk(root):
        if call.type != "call_expression":
            continue
        target = call_target(call, source)
        kind = _CALLS.get(target[1]) if target is not None else None
        fn = top_function(call)
        if kind is None or target is None or fn is None:
            continue
        key = (fn.start_byte, fn.end_byte)
        if key not in cache:
            cache[key] = function_bindings(fn, source)
        if not is_session(cache[key], target[0]):
            continue
        args = call_args(call)
        if len(args) != 2 or literal_type(args[1], source) not in {(p, kind[1]) for p in pkgs}:
            continue
        fields = keyed_fields(args[1], source)
        endpoint = go_string(fields.get(kind[2]), source) if fields is not None else None
        if endpoint:
            emits.append(
                call_emit(call, source, method=kind[0], endpoint=endpoint, framework=FRAMEWORK)
            )
    return Detection(emits) if emits else None


register_detector(
    Detector(
        name="go-mcp-client",
        gate=Gate("go", order=145, guard=b"modelcontextprotocol/go-sdk"),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
