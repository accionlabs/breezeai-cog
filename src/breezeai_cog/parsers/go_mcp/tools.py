"""Additive detector: tools of a Go MCP server built with ``github.com/mark3labs/mcp-go``.

::

    tool := mcp.NewTool("hello",                                  → mcp_tool  mcp-go  TOOL  hello
        mcp.WithDescription("Say hello"),
        mcp.WithString("name", mcp.Required(), mcp.Description("Who to greet")),
        mcp.WithNumber("times", mcp.DefaultNumber(1)),
    )
    s.AddTool(tool, helloHandler)                                   handler helloHandler

* The statement sits on ``mcp.NewTool(…)`` (the ``mcp`` package of mcp-go, under any import
  name); a non-literal name → no record.
* ``params`` — ``WithString`` / ``WithNumber`` / ``WithBoolean`` / ``WithArray`` / ``WithObject``
  / ``WithInteger``, with ``Required()``, ``Description(…)`` and ``Default*(…)``.
* ``handler`` — the second argument of the ``AddTool`` call that registers the tool (inline, or
  through the variable it was assigned to); a function literal gives only its line.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import ToolParam
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import param, tool_emit
from ..treesitter import node_text

FRAMEWORK = "mcp-go"
_PACKAGE = "github.com/mark3labs/mcp-go/mcp"
_PARAM_TYPES = {
    "WithString": "string",
    "WithNumber": "number",
    "WithBoolean": "boolean",
    "WithArray": "array",
    "WithObject": "object",
    "WithInteger": "integer",
}
_MAX_DEFAULT = 200


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def _string(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "interpreted_string_literal":
        return node_text(node, source)[1:-1]
    if node.type == "raw_string_literal":
        return node_text(node, source)[1:-1]
    return None


def _package_names(root: Node, source: bytes) -> set[str]:
    """Local names of the mcp-go ``mcp`` package (``mcp``, or an import alias)."""
    out: set[str] = set()
    for spec in _walk(root):
        if spec.type != "import_spec":
            continue
        path = _string(spec.child_by_field_name("path"), source)
        if path != _PACKAGE:
            continue
        alias = spec.child_by_field_name("name")
        out.add(node_text(alias, source) if alias is not None else "mcp")
    return out


def _pkg_call(node: Node, source: bytes, pkgs: set[str]) -> tuple[str, list[Node]] | None:
    """``mcp.X(args…)`` → ``("X", args)``."""
    if node.type != "call_expression":
        return None
    fn, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
    if fn is None or fn.type != "selector_expression":
        return None
    operand, field = fn.child_by_field_name("operand"), fn.child_by_field_name("field")
    if operand is None or field is None or node_text(operand, source) not in pkgs:
        return None
    return node_text(field, source), [
        a for a in (args.named_children if args else []) if a.type != "comment"
    ]


def _params(
    options: list[Node], source: bytes, pkgs: set[str]
) -> tuple[str | None, list[ToolParam]]:
    description: str | None = None
    out: list[ToolParam] = []
    for opt in options:
        call = _pkg_call(opt, source, pkgs)
        if call is None:
            continue
        name, args = call
        if name == "WithDescription" and args:
            description = _string(args[0], source)
        elif name in _PARAM_TYPES and args:
            pname = _string(args[0], source)
            if not pname:
                continue
            required, pdesc, default = False, None, None
            for prop in args[1:]:
                pc = _pkg_call(prop, source, pkgs)
                if pc is None:
                    continue
                if pc[0] == "Required":
                    required = True
                elif pc[0] == "Description" and pc[1]:
                    pdesc = _string(pc[1][0], source)
                elif pc[0].startswith("Default") and pc[1]:
                    text = " ".join(node_text(pc[1][0], source).split())
                    default = text if len(text) <= _MAX_DEFAULT else text[: _MAX_DEFAULT - 1] + "…"
            out.append(
                param(
                    pname, _PARAM_TYPES[name], required=required, default=default, description=pdesc
                )
            )
    return description, out


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    pkgs = _package_names(dc.root, source)
    if not pkgs:
        return None
    nodes = _walk(dc.root)
    # tool expression (the NewTool call, or the variable holding it) → handler node
    handlers: dict[str, Node] = {}
    assigned: dict[int, str] = {}  # NewTool call start byte → variable name
    for n in nodes:
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            field = (
                fn.child_by_field_name("field")
                if fn is not None and fn.type == "selector_expression"
                else None
            )
            args = n.child_by_field_name("arguments")
            if field is not None and node_text(field, source) == "AddTool" and args is not None:
                a = [c for c in args.named_children if c.type != "comment"]
                if len(a) >= 2:
                    key = (
                        node_text(a[0], source)
                        if a[0].type == "identifier"
                        else f"@{a[0].start_byte}"
                    )
                    handlers[key] = a[1]
        elif n.type in ("short_var_declaration", "var_spec", "assignment_statement"):
            left = n.child_by_field_name("left") or n.child_by_field_name("name")
            right = n.child_by_field_name("right") or n.child_by_field_name("value")
            lnames = [
                c
                for c in (
                    left.named_children
                    if left is not None and left.type == "expression_list"
                    else [left]
                )
                if c is not None
            ]
            rvals = (
                right.named_children
                if right is not None and right.type == "expression_list"
                else [right]
            )
            for ln, rv in zip(lnames, rvals):
                if rv is not None and ln.type == "identifier" and rv.type == "call_expression":
                    assigned[rv.start_byte] = node_text(ln, source)
    functions = {f.name: f.startLine for f in dc.record.functions}
    found: list[Emit] = []
    for n in nodes:
        call = _pkg_call(n, source, pkgs)
        if call is None or call[0] != "NewTool" or not call[1]:
            continue
        name = _string(call[1][0], source)
        if not name:
            continue
        description, params = _params(call[1][1:], source, pkgs)
        var = assigned.get(n.start_byte)
        h = handlers.get(var) if var is not None else handlers.get(f"@{n.start_byte}")
        handler, handler_line = None, None
        if h is not None and h.type == "identifier":
            handler = node_text(h, source)
            handler_line = functions.get(handler)
        elif h is not None and h.type == "func_literal":
            handler_line = h.start_point[0] + 1
        elif h is not None and h.type == "selector_expression":
            handler = node_text(h, source)
        found.append(
            tool_emit(
                n,
                source,
                endpoint=name,
                framework=FRAMEWORK,
                handler=handler,
                handler_line=handler_line,
                description=description,
                params=params,
                node_type=None,
            )
        )
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=FRAMEWORK)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="go-mcp",
        gate=Gate("go", order=130, guard=b"mark3labs/mcp-go", skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
