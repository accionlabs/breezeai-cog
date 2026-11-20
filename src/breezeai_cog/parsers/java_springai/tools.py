"""Additive detector: tools of a Java MCP server built on Spring AI.

::

    @McpTool(name = "weather", description = "Current weather")     → mcp_tool  spring-ai-mcp  TOOL  weather
    public String weather(@McpToolParam(description = "City") String city) { … }

    @Tool(description = "Current weather")                          → mcp_tool  (only in an MCP server)
    public String weather(@ToolParam(description = "City", required = false) String city, ToolContext ctx)

* ``@McpTool`` / ``@McpToolParam`` (Spring AI MCP annotations) are MCP tools by definition.
* ``@Tool`` / ``@ToolParam`` are Spring AI's general function-calling annotations: a chat client
  uses them too. They count only in a module whose build file declares a Spring AI MCP
  **server** dependency (:func:`~..jvm_mcp_common.in_mcp_server_module`); otherwise nothing is
  emitted, so a chat-client module next to an MCP server module stays out. ``@Tool`` must also
  be Spring AI's: imported from ``org.springframework.ai.tool.annotation`` or written fully
  qualified.
* ``endpoint`` — ``name =`` when a literal, else the method name (Spring AI's default).
* ``params`` — ``description`` / ``required`` from the parameter annotation (required by
  default); ``ToolContext`` and MCP exchange / context parameters are injected, not inputs.

Resources and prompts (Spring AI MCP annotations)::

    @McpResource(uri = "users://{id}/profile", name = "Profile",     → mcp_resource  spring-ai-mcp  RESOURCE  users://{id}/profile
                 description = "A user's profile")                                   params id
    public String profile(String id) { … }

    @McpPrompt(name = "review", description = "Review code")        → mcp_prompt  spring-ai-mcp  PROMPT  review
    public GetPromptResult review(@McpArg(name = "code", description = "The code", required = true) String code)

* The annotation must be the annotations library's: imported from
  ``org.springaicommunity.mcp.annotation`` (or Spring AI's ``org.springframework.ai.mcp.annotation``)
  by name or ``*``, or written fully qualified; a same-named annotation of another library is not
  read.
* ``mcp_resource`` — ``uri =`` a non-empty literal, else no record. ``params`` are the URI
  template's simple ``{variables}`` (``[]`` for a fixed URI; None with an RFC 6570 operator).
* ``mcp_prompt`` — ``name =`` when a literal, else the method name. ``params`` are the method's
  parameters, as the annotations library lists them: ``@McpArg`` ``name`` (else the parameter's
  name), ``description``, ``required`` (false by default); a parameter without ``@McpArg`` is an
  optional argument with no description; ``GetPromptRequest``, a ``Map`` and the injected exchange /
  context types are not arguments. A ``name =`` / ``required =`` that is not a literal (a
  constant) → ``params`` None: the argument list is unknown.

Only annotated methods are read here. A tool registered through the MCP Java SDK
(``McpServerFeatures.SyncToolSpecification``) belongs to ``java_mcp``, also in a Spring
application.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...schemas import Parameter
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..jvm_mcp_common import (
    INJECTED,
    MCP_ANNOTATION_PACKAGES,
    SPRING_AI,
    SPRING_TOOL_FQN,
    in_mcp_server_module,
    java_string,
    walk,
)
from ..mcp_common import (
    PROMPT,
    RESOURCE,
    guarded,
    mcp_emit,
    param,
    tool_emit,
    uri_template_params,
)
from ..treesitter import node_text


def _annotations(node: Node, source: bytes) -> dict[str, Node]:
    """Annotations in ``node``'s modifiers, by simple name."""
    out: dict[str, Node] = {}
    mods = next((c for c in node.named_children if c.type == "modifiers"), None)
    for a in mods.named_children if mods is not None else []:
        if a.type in ("annotation", "marker_annotation"):
            name = a.child_by_field_name("name")
            if name is not None:
                out[node_text(name, source).rsplit(".", 1)[-1]] = a
    return out


def _element(annotation: Node, key: str, source: bytes) -> Node | None:
    args = annotation.child_by_field_name("arguments")
    for a in args.named_children if args is not None else []:
        if a.type == "element_value_pair":
            k = a.child_by_field_name("key")
            if k is not None and node_text(k, source) == key:
                return a.child_by_field_name("value")
        elif key == "value" and a.type != "comment":
            return a  # a lone value: @ToolParam("…")
    return None


def _params(method: Node, source: bytes, param_annotation: str) -> list[Parameter]:
    params = method.child_by_field_name("parameters")
    out: list[Parameter] = []
    for p in params.named_children if params is not None else []:
        if p.type != "formal_parameter":
            continue
        type_node, name_node = p.child_by_field_name("type"), p.child_by_field_name("name")
        if type_node is None or name_node is None:
            continue
        type_text = node_text(type_node, source)
        if type_text.split("<")[0].rsplit(".", 1)[-1] in INJECTED:
            continue
        ann = _annotations(p, source).get(param_annotation)
        description = required = None
        if ann is not None:
            description = java_string(
                _element(ann, "description", source) or _element(ann, "value", source), source
            )
            req = _element(ann, "required", source)
            required = node_text(req, source) != "false" if req is not None else None
        out.append(
            param(
                node_text(name_node, source),
                type_text,
                required=True if required is None else required,
                description=description,
            )
        )
    return out


#: ``@Tool`` is Spring AI's only when imported from its package.
_SPRING_TOOL_IMPORT = re.compile(
    rb"import\s+org\.springframework\.ai\.tool\.annotation\.(?:Tool|\*)\s*;"
)


def _spring_tool(ann: Node, source: bytes, imported: bool) -> bool:
    name = ann.child_by_field_name("name")
    text = node_text(name, source) if name is not None else ""
    return text == SPRING_TOOL_FQN or (imported and text == "Tool")


#: Parameter types the annotations library does not list as prompt arguments; any ``…Map``
#: (``TreeMap``, ``ConcurrentHashMap``, …) is skipped too: it receives the arguments map.
_PROMPT_SKIPPED = INJECTED | {"GetPromptRequest"}


def _prompt_skipped(simple_name: str) -> bool:
    return simple_name in _PROMPT_SKIPPED or simple_name.endswith("Map")


def _prompt_params(method: Node, source: bytes) -> list[Parameter] | None:
    """The prompt's arguments; None when an ``@McpArg`` ``name`` / ``required`` is not a
    literal (a constant): the argument list is then unknown."""
    params = method.child_by_field_name("parameters")
    out: list[Parameter] = []
    for p in params.named_children if params is not None else []:
        if p.type != "formal_parameter":
            continue
        type_node, name_node = p.child_by_field_name("type"), p.child_by_field_name("name")
        if type_node is None or name_node is None:
            continue
        if _prompt_skipped(node_text(type_node, source).split("<")[0].rsplit(".", 1)[-1]):
            continue
        name = node_text(name_node, source)
        ann = _annotations(p, source).get("McpArg")
        description, required = None, False
        if ann is not None:
            named = _element(ann, "name", source)
            if named is not None:
                literal = java_string(named, source)
                if literal is None:
                    return None  # @McpArg(name = ARG): which name, unknown
                name = literal or name  # name = "" → the parameter's name
            description = java_string(_element(ann, "description", source), source)
            req = _element(ann, "required", source)
            req_text = node_text(req, source) if req is not None else "false"
            if req_text not in ("true", "false"):
                return None  # required = FLAG
            required = req_text == "true"
        out.append(param(name, None, required=required, description=description))
    return out


def _annotation_import(simple: str) -> re.Pattern[bytes]:
    packages = b"|".join(re.escape(p.encode()) for p in MCP_ANNOTATION_PACKAGES)
    return re.compile(rb"import\s+(?:" + packages + rb")\.(?:" + simple.encode() + rb"|\*)\s*;")


_ENTRY_IMPORTS = {key: _annotation_import(key) for key in ("McpResource", "McpPrompt")}


def _library_annotation(ann: Node, key: str, source: bytes, imported: bool) -> bool:
    """``ann`` is the annotations library's ``key``: imported from its package (by name or
    ``*``), or written fully qualified."""
    name = ann.child_by_field_name("name")
    text = node_text(name, source) if name is not None else ""
    return (imported and text == key) or text in {f"{p}.{key}" for p in MCP_ANNOTATION_PACKAGES}


def _annotated_entries(dc: DetectContext) -> list[Emit]:
    """Spring AI ``@McpResource`` / ``@McpPrompt`` methods."""
    source = dc.source
    if b"McpResource" not in source and b"McpPrompt" not in source:
        return []
    imported = {key: bool(rx.search(source)) for key, rx in _ENTRY_IMPORTS.items()}
    functions = {(f.name, f.startLine): f for f in dc.record.functions}
    found: list[Emit] = []
    for method in walk(dc.root):
        if method.type != "method_declaration":
            continue
        anns = _annotations(method, source)
        name_node = method.child_by_field_name("name")
        if name_node is None:
            continue
        method_name = node_text(name_node, source)
        line = method.start_point[0] + 1
        owner = functions.get((method_name, line))
        for key, kind in (("McpResource", RESOURCE), ("McpPrompt", PROMPT)):
            ann = anns.get(key)
            if ann is None or not _library_annotation(ann, key, source, imported[key]):
                continue
            if kind == RESOURCE:
                endpoint = java_string(_element(ann, "uri", source), source)
                params = uri_template_params(endpoint) if endpoint else None
            else:
                named = _element(ann, "name", source)
                endpoint = java_string(named, source) if named is not None else method_name
                params = _prompt_params(method, source)
            if not endpoint:
                continue
            found.append(
                mcp_emit(
                    ann,
                    source,
                    kind=kind,
                    endpoint=endpoint,
                    framework=SPRING_AI,
                    handler=method_name,
                    handler_line=line,
                    description=java_string(_element(ann, "description", source), source),
                    params=params,
                    parent=owner.id if owner is not None else None,
                )
            )
    return found


def _annotated_tools(dc: DetectContext) -> list[Emit]:
    """Spring AI ``@McpTool`` / ``@Tool`` methods."""
    source = dc.source
    if b"Tool" not in source:
        return []
    mcp_annotations = b"McpTool" in source
    plain = b"springframework.ai" in source and in_mcp_server_module(
        dc.record.path, dc.ctx.resolution_index
    )
    if not mcp_annotations and not plain:
        return []
    imported = bool(_SPRING_TOOL_IMPORT.search(source))
    functions = {(f.name, f.startLine): f for f in dc.record.functions}
    found: list[Emit] = []
    for method in walk(dc.root):
        if method.type != "method_declaration":
            continue
        anns = _annotations(method, source)
        if "McpTool" in anns:
            ann, param_ann = anns["McpTool"], "McpToolParam"
        elif plain and "Tool" in anns and _spring_tool(anns["Tool"], source, imported):
            ann, param_ann = anns["Tool"], "ToolParam"
        else:
            continue
        name_node = method.child_by_field_name("name")
        if name_node is None:
            continue
        method_name = node_text(name_node, source)
        named = _element(ann, "name", source)
        endpoint = java_string(named, source) if named is not None else method_name
        if not endpoint:
            continue
        line = method.start_point[0] + 1
        owner = functions.get((method_name, line))
        found.append(
            tool_emit(
                ann,
                source,
                endpoint=endpoint,
                framework=SPRING_AI,
                handler=method_name,
                handler_line=line,
                description=java_string(_element(ann, "description", source), source),
                params=_params(method, source, param_ann),
                parent=owner.id if owner is not None else None,
            )
        )
    return found


def _detect(dc: DetectContext) -> Detection | None:
    # A failing resource / prompt reader never drops the file's tools.
    found = _annotated_tools(dc) + guarded(dc.path, _annotated_entries, dc)
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=SPRING_AI)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="java-springai",
        # `Tool`, `McpResource` or `McpPrompt`: each reader checks its own marker.
        gate=Gate("java", order=30, skip_fixtures=True),
        detect=_detect,
        frameworks=(SPRING_AI,),
    )
)
