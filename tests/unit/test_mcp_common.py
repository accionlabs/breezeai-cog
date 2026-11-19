"""Shared MCP emit helpers (``parsers/mcp_common.py``)."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.mcp_common import (
    PROMPT,
    RESOURCE,
    TOOL,
    call_emit,
    fit_params,
    mcp_emit,
    param,
    params_json_bytes,
    tool_emit,
    uri_template_params,
)
from breezeai_cog.parsers.treesitter import parse_source


def _node():
    source = b"server.tool('x', h)\n"
    tree = parse_source("typescript", source)
    return tree.root_node.named_children[0], source


def test_tool_emit_is_mcp_emit_with_tool() -> None:
    node, source = _node()
    kwargs = dict(
        endpoint="x",
        framework="typescript-mcp",
        handler="h",
        handler_line=1,
        description="  Does x.  ",
        params=[param("a", "string", required=True)],
    )
    tool = tool_emit(node, source, **kwargs)
    same = mcp_emit(node, source, kind=TOOL, **kwargs)
    assert tool.fields == same.fields
    assert (tool.node_type, tool.text, tool.enrich, tool.parent) == (
        same.node_type,
        same.text,
        same.enrich,
        same.parent,
    )
    assert tool.fields["semanticType"] == "mcp_tool" and tool.fields["method"] == "TOOL"
    assert tool.fields["name"] == "x" and tool.fields["description"] == "Does x."


@pytest.mark.parametrize(
    ("kind", "semantic_type", "method"),
    [
        (RESOURCE, "mcp_resource", "RESOURCE"),
        (PROMPT, "mcp_prompt", "PROMPT"),
    ],
)
def test_mcp_emit_kinds(kind, semantic_type, method) -> None:
    node, source = _node()
    emit = mcp_emit(
        node,
        source,
        kind=kind,
        endpoint="e",
        framework="f",
        handler=None,
        handler_line=2,
        description=None,
        params=[],
    )
    assert (emit.fields["semanticType"], emit.fields["method"]) == (semantic_type, method)
    assert emit.fields["name"] == "e" and emit.fields["params"] == []


def test_call_emit_enriches_the_call_statement() -> None:
    node, source = _node()
    emit = call_emit(node, source, method="TOOL", endpoint="x", framework="typescript-mcp")
    assert emit.enrich == "unclassified"
    assert emit.fields == {
        "semanticType": "mcp_call",
        "method": "TOOL",
        "endpoint": "x",
        "framework": "typescript-mcp",
        "name": "x",
    }


@pytest.mark.parametrize(
    ("uri", "names"),
    [
        ("users://{id}", ["id"]),
        ("users://{id}/posts/{post_id}", ["id", "post_id"]),
        ("repo://{owner.name}/{id}/{id}", ["owner.name", "id"]),
        ("file:///logs/app.log", []),
        ("config://app", []),
    ],
)
def test_uri_template_simple_variables(uri, names) -> None:
    params = uri_template_params(uri)
    assert params is not None
    assert [(p.name, p.type, p.required) for p in params] == [(n, "string", True) for n in names]


@pytest.mark.parametrize(
    "uri",
    [
        "search://{?q}",
        "docs://{+path}",
        "a://{#frag}",
        "a://x{/seg}",
        "a://x{.ext}",
        "a://x{;p}",
        "a://x?a=1{&b}",
        "a://{list*}",
        "a://{name:3}",
        "a://{x,y}",
        "a://{}",
        "a://{id",
        "a://id}",
        "a://{a b}",
    ],
)
def test_uri_template_with_operators_is_unknown(uri) -> None:
    assert uri_template_params(uri) is None


def test_fit_params_measures_json_bytes() -> None:
    # Multi-byte text counts by its UTF-8 bytes, as the backend measures.
    params = [param(f"p{i}", "string", required=True, description="é" * 2000) for i in range(6)]
    fitted = fit_params(params)
    assert fitted is not None and params_json_bytes(fitted) <= 15 * 1024
    assert [p.description is not None for p in fitted] == [True] * 3 + [False] * 3


def test_tool_resource_and_prompt_on_one_position_get_unique_ids() -> None:
    """Every emit goes through ``emits.apply``: three records on one ``path:line:col`` get
    ``#2`` / ``#3``."""
    from breezeai_cog.parsers.emits import apply
    from breezeai_cog.schemas import FileRecord

    node, source = _node()
    record = FileRecord(id="a.ts", path="a.ts", type="code", language="typescript", loc=1)
    common = dict(
        endpoint="x",
        framework="typescript-mcp",
        handler=None,
        handler_line=1,
        description=None,
        params=[],
    )
    apply(
        record,
        [mcp_emit(node, source, kind=k, **common) for k in (TOOL, RESOURCE, PROMPT)],
        source,
        "a.ts",
    )
    assert [s.id for s in record.statements] == ["a.ts:1:0", "a.ts:1:0#2", "a.ts:1:0#3"]
    assert [s.semanticType for s in record.statements] == ["mcp_tool", "mcp_resource", "mcp_prompt"]
