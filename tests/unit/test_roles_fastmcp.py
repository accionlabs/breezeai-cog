"""MCP server tools of FastMCP (the standalone ``fastmcp`` package):
one ``mcp_tool`` statement per tool.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``).

Also covers the ``@<server>.tool`` reader shared with the official Python SDK
(``parsers/python_mcp_common.py``): signature, ``Field`` / ``Annotated`` and docstring params.
"""

from __future__ import annotations

import json
import time

import pytest

from breezeai_cog.schemas import Statement


def _tools(statements: list[Statement]) -> dict[str | None, Statement]:
    tools = [s for s in statements if s.semanticType == "mcp_tool"]
    for t in tools:
        assert t.method == "TOOL" and t.nodeType
    return {t.endpoint: t for t in tools}


def _params(tool: Statement) -> list[tuple]:
    return [(p.name, p.type, p.required, p.default, p.description) for p in tool.params or []]


FASTMCP_SERVER = '''
from typing import Annotated
from fastmcp import FastMCP, Context
from pydantic import Field

mcp = FastMCP("breeze")


@mcp.tool()
def add(a: int, b: int = 2) -> int:
    """Add two numbers.

    Returns the sum.
    """
    return a + b


@mcp.tool(name="Code_Graph_Search", description="Semantic search over the code graph")
async def search(
    query: Annotated[str, Field(description="Search text")],
    ctx: Context,
    limit: int = Field(default=10, description="Max hits"),
    labels: list[str] | None = None,
) -> str:
    """Ignored: the explicit description wins."""
    return ""


@mcp.tool
def ping():
    return "pong"
'''


def test_fastmcp_module_server(analyze_files) -> None:
    tools = _tools(analyze_files({"app/server.py": FASTMCP_SERVER}))
    assert set(tools) == {"add", "Code_Graph_Search", "ping"}

    add = tools["add"]
    assert (add.framework, add.handler, add.handlerLine) == ("fastmcp", "add", 10)
    assert add.startLine == 9 and add.nodeType == "synthetic"
    assert add.description == "Add two numbers.\n\nReturns the sum."
    assert _params(add) == [("a", "int", True, None, None), ("b", "int", False, "2", None)]

    search = tools["Code_Graph_Search"]  # renamed; explicit description; Context injected
    assert (
        search.handler == "search" and search.description == "Semantic search over the code graph"
    )
    assert _params(search) == [
        ("query", "str", True, None, "Search text"),
        ("limit", "int", False, "10", "Max hits"),
        ("labels", "list[str] | None", False, "None", None),
    ]

    ping = tools["ping"]  # bare decorator, no docstring
    assert ping.description is None and ping.params == []


def test_fastmcp_typed_register_function_with_stacked_decorators(analyze_files) -> None:
    src = '''
from fastmcp import FastMCP
from app.errors import handle_errors


def register(mcp: FastMCP):
    @mcp.tool(name="Documents", annotations={"readOnlyHint": True})
    @handle_errors("Documents API")
    async def documents(project_uuid: str, page: int = 1) -> str:
        """List documents."""
        return ""
'''
    tools = _tools(
        analyze_files(
            {
                "app/tools/documents.py": src,
                "app/errors.py": "def handle_errors(n):\n    return n\n",
            }
        )
    )
    doc = tools["Documents"]
    assert doc.handler == "documents" and doc.handlerLine == 9 and doc.startLine == 7
    assert doc.description == "List documents."
    assert _params(doc) == [
        ("project_uuid", "str", True, None, None),
        ("page", "int", False, "1", None),
    ]


def test_fastmcp_server_imported_from_another_module(analyze_files) -> None:
    files = {
        "app/server.py": "from fastmcp import FastMCP\n\nmcp = FastMCP('x')\n",
        "app/tools.py": "from app.server import mcp as server\n\n\n@server.tool\ndef hello(name: str) -> str:\n    return name\n",
    }
    tools = _tools(analyze_files(files))
    assert set(tools) == {"hello"}
    # The server's file decides the SDK, not the tools file (which never imports fastmcp).
    assert tools["hello"].framework == "fastmcp"


def test_agent_tools_are_not_mcp_tools(analyze_files) -> None:
    src = """
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio

agent = Agent("model", toolsets=[MCPServerStdio("npx", args=[])])


@agent.tool
def roll(ctx, sides: int = 6) -> int:
    return 4
"""
    assert _tools(analyze_files({"agent.py": src})) == {}


def test_a_computed_tool_name_is_not_guessed(analyze_files) -> None:
    src = "from fastmcp import FastMCP\n\nmcp = FastMCP('x')\nNAME = 'a'\n\n\n@mcp.tool(name=NAME)\ndef a() -> str:\n    return ''\n"
    assert _tools(analyze_files({"srv.py": src})) == {}


def test_a_local_class_named_fastmcp_is_not_a_server(analyze_files) -> None:
    """breezeai-agents registers a copy of the Breeze tools on an in-process shim class named
    ``FastMCP``; nothing serves them over MCP, so they are not MCP tools."""
    files = {
        "app/shim.py": "class FastMCP:\n    def tool(self, **kw):\n        return lambda f: f\n",
        "app/tools.py": (
            "from app.shim import FastMCP\n\n\ndef register(mcp: FastMCP):\n"
            "    @mcp.tool(name='Code_Graph_Search')\n    async def search(query: str) -> str:\n        return ''\n"
        ),
        "app/direct.py": "from app import shim\n\nregistry = shim.FastMCP()\n",
    }
    assert _tools(analyze_files(files)) == {}


def test_python_attribute_named_tools_is_not_scanned(analyze_files) -> None:
    src = "from fastmcp import FastMCP\n\nmcp = FastMCP('x')\nprint(mcp.tools)\n"
    assert _tools(analyze_files({"app/server.py": src})) == {}


def test_python_annotated_metadata_and_field_defaults(analyze_files) -> None:
    src = """
from typing import Annotated, Literal
from fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("x")


@mcp.tool
def listing(
    sort: Annotated[Literal["asc", "desc"], "Sort order"],
    order: Annotated[Literal["asc", "desc"], Field(description="Order")],
    size: Annotated[int, Field(default=5)],
    must: int = Field(default=..., description="Must"),
    also: int = Field(...),
) -> str:
    return ""
"""
    tools = _tools(analyze_files({"app/server.py": src}))
    assert _params(tools["listing"]) == [
        ("sort", 'Literal["asc", "desc"]', True, None, "Sort order"),
        ("order", 'Literal["asc", "desc"]', True, None, "Order"),
        ("size", "int", False, "5", None),
        ("must", "int", True, None, "Must"),
        ("also", "int", True, None, None),
    ]


def test_python_literal_type_strings_are_not_a_description(analyze_files) -> None:
    src = (
        "from typing import Annotated, Literal\nfrom fastmcp import FastMCP\n\nmcp = FastMCP('x')\n\n\n"
        "@mcp.tool\ndef f(sort: Annotated[Literal['asc', 'desc'], Field(ge=1)]) -> str:\n    return ''\n"
    )
    [p] = _tools(analyze_files({"app/server.py": src}))["f"].params or []
    assert p.description is None


def _py_tool(body: str) -> str:
    return (
        "from fastmcp import FastMCP, Context\nfrom pydantic import Field\n\nmcp = FastMCP('x')\n\n\n"
        + body
    )


def test_python_param_descriptions_from_google_args(analyze_files) -> None:
    src = _py_tool('''
@mcp.tool
def search(query: str, limit: int = 10, labels: list[str] | None = None) -> str:
    """Search the graph.

    Args:
        query: What to look for,
            across names and descriptions.
        limit (int): Max hits.
        labels (list[str] | None): Labels to keep.

    Returns:
        The hits.
    """
    return ""
''')
    tool = _tools(analyze_files({"app/server.py": src}))["search"]
    assert tool.description.startswith("Search the graph.\n\nArgs:")  # the whole docstring
    assert _params(tool) == [
        ("query", "str", True, None, "What to look for,\nacross names and descriptions."),
        ("limit", "int", False, "10", "Max hits."),
        ("labels", "list[str] | None", False, "None", "Labels to keep."),
    ]


# breezeai-mcp-backend's `get_design_nodes_by_label` docstring, verbatim.
DESIGN_NODES_BY_LABEL = '''
@mcp.tool
async def get_design_nodes_by_label(
    uuid: str,
    label: str,
    filters: dict | str | None = None,
    limit: int = 100,
    page: int = 1,
    sort_name: str | None = None,
    sort_order: str | None = None,
) -> str:
    """
    List design nodes of one label, optionally filtered — by id, parent, step,
    action, type, platform or any other attribute.

    Design labels: UserJourney, Flow, Page, Component.

    filters: {field: value} or {field: {op: value}}; ops $eq $eqi $in $ini
    $contains $containsi $gt $gte $lt $lte. Array fields (userJourneyIds,
    flowIds, pageIds, stepIds, actionIds, scenarioIds) match when the value is
    one of the array's elements.

    Examples:
    - By id:                  label="Flow", filters={"id": "flow-123"}
    - Several ids:            filters={"id": {"$in": ["uj-1", "uj-2"]}}
    - Flows of a journey:     label="Flow", filters={"userJourneyIds": "uj-123"}
    - Pages of a flow:        label="Page", filters={"flowIds": "flow-123"}
    - Components of a page:   label="Component", filters={"pageIds": "page-123"}
    - Nodes for a step/action: filters={"stepIds": "step-1"} / {"actionIds": "act-1"}
    - By type / platform:     filters={"type": "ORGANISM", "platform": "customer portal"}

    Each node carries `platform` (part of the uniqueness key: projectUuid +
    nameKey + platform).

    Args:
        uuid: Project UUID (required)
        label: UserJourney, Flow, Page or Component (required)
        filters: object or JSON object string (optional)
        limit / page: paging (default 100 / 1)
        sort_name / sort_order: e.g. "name" / "asc"

    Returns {data, total}; total > len(data) means more pages exist.
    """
    return ""
'''


def test_python_slash_joined_args_entries_match_no_param(analyze_files) -> None:
    tool = _tools(analyze_files({"app/server.py": _py_tool(DESIGN_NODES_BY_LABEL)}))[
        "get_design_nodes_by_label"
    ]
    docs = {p.name: p.description for p in tool.params or []}
    assert docs == {
        "uuid": "Project UUID (required)",
        "label": "UserJourney, Flow, Page or Component (required)",
        "filters": "object or JSON object string (optional)",
        "limit": None,
        "page": None,
        "sort_name": None,
        "sort_order": None,
    }
    assert "limit / page" not in docs["filters"]


def test_python_param_docs_read_when_description_is_explicit(analyze_files) -> None:
    src = _py_tool('''
@mcp.tool(description="x")
def f(q: str) -> str:
    """Ignored as the description.

    Args:
        q: The query.
    """
    return ""
''')
    tool = _tools(analyze_files({"app/server.py": src}))["f"]
    assert tool.description == "x"
    assert _params(tool) == [("q", "str", True, None, "The query.")]


def test_python_param_docs_from_sphinx_and_numpy(analyze_files) -> None:
    src = _py_tool('''
@mcp.tool
def sphinx(a: str, b: str) -> str:
    """Sphinx style.

    :param a: First,
        continued.
    :param str b: Second.
    :type a: str
    :returns: nothing
    """
    return ""


@mcp.tool
def numpy(a: str, b: int = 1) -> str:
    """NumPy style.

    Parameters
    ----------
    a : str
        First,
        continued.
    b : int, optional
        Second.

    Returns
    -------
    str
        The result.
    """
    return ""
''')
    tools = _tools(analyze_files({"app/server.py": src}))
    assert [p.description for p in tools["sphinx"].params or []] == [
        "First,\ncontinued.",
        "Second.",
    ]
    assert [p.description for p in tools["numpy"].params or []] == ["First,\ncontinued.", "Second."]


def test_python_field_description_beats_the_docstring(analyze_files) -> None:
    src = _py_tool('''
@mcp.tool
def f(a: str = Field(description="From Field"), b: str = "", ctx: Context = None, *args, **kwargs) -> str:
    """Doc.

    Args:
        a: From the docstring.
        ctx: The context.
        args: Extra.
        kwargs: More.
    """
    return ""
''')
    assert _params(_tools(analyze_files({"app/server.py": src}))["f"]) == [
        ("a", "str", True, None, "From Field"),
        ("b", "str", False, '""', None),  # not in Args
    ]


def test_python_inline_args_entry_is_one_param(analyze_files) -> None:
    src = _py_tool('''
@mcp.tool
def f(limit: int = 10, page: int = 1) -> str:
    """Doc.

    Args:
        limit: max hits page: the page
    """
    return ""
''')
    assert [
        p.description for p in _tools(analyze_files({"app/server.py": src}))["f"].params or []
    ] == [
        "max hits page: the page",
        None,
    ]


def test_python_args_entry_is_capped(analyze_files) -> None:
    long = "word " * 1000
    src = _py_tool(f'''
@mcp.tool
def f(a: str, b: int = 1) -> str:
    """Doc.

    Args:
        a: {long}
        b: Short.
    """
    return ""
''')
    a, b = _tools(analyze_files({"app/server.py": src}))["f"].params or []
    assert (a.name, a.type, b.name, b.type, b.description) == ("a", "str", "b", "int", "Short.")
    assert len(a.description) == 1024 and a.description.endswith("…")


def test_python_args_scan_is_linear() -> None:
    from breezeai_cog.parsers.python_mcp_common import _docstring_param_docs

    def doc(n: int) -> str:
        return "Doc.\n\nArgs:\n" + "".join(
            f"    p{i}: Param {i}.\n        more\n" for i in range(n)
        )

    start = time.perf_counter()
    found = _docstring_param_docs(doc(10_000))
    assert len(found) == 10_000 and found["p9999"] == "Param 9999.\nmore"
    assert time.perf_counter() - start < 2.0


@pytest.mark.parametrize(
    "doc",
    [
        "Doc.\n\n\tArgs:\n\t\tq:\tThe query.\n\t\t\tmore\n",
        "Doc.\n\nParameters\n----------\n",
        "Doc.\n\nParameters\n----------\n\nReturns\n-------\n",
        "Doc.\n\n:param: no name\n    continued\n:param q:\n",
        "Doc.\n\nArgs:\n",
        "Doc.\n\nArgs:\n    (broken: x\n    : nothing\n",
    ],
)
def test_python_malformed_docstring_keeps_the_tool(analyze_files, doc: str) -> None:
    src = _py_tool(f'@mcp.tool\ndef f(q: str) -> str:\n    """{doc}"""\n    return ""\n')
    tool = _tools(analyze_files({"app/server.py": src}))["f"]
    assert [p.name for p in tool.params or []] == ["q"]


def test_python_numpy_rule_line_inside_a_description(analyze_files) -> None:
    src = _py_tool('''
@mcp.tool
def f(query: str, limit: int = 1) -> str:
    """Doc.

    Parameters
    ----------
    query : str
        The query.
        ---
        more
    limit : int
        L.
    """
    return ""
''')
    assert [
        p.description for p in _tools(analyze_files({"app/server.py": src}))["f"].params or []
    ] == [
        "The query.\n---\nmore",
        "L.",
    ]


def test_python_sphinx_aliases_and_numpy_shared_entry(analyze_files) -> None:
    src = _py_tool('''
@mcp.tool
def sphinx(a: str, b: str, c: str, d: str, e: str) -> str:
    """Doc.

    :parameter a: A.
    :arg str b: B.
    :argument c: C.
    :keyword int d: D.
    :key e: not a param field.
    """
    return ""


@mcp.tool
def numpy(x: int, y: int) -> str:
    """Doc.

    Parameters
    ----------
    x, y : int
        Coordinates.
    """
    return ""
''')
    tools = _tools(analyze_files({"app/server.py": src}))
    assert [p.description for p in tools["sphinx"].params or []] == ["A.", "B.", "C.", "D.", None]
    assert [p.description for p in tools["numpy"].params or []] == ["Coordinates.", "Coordinates."]


def test_python_last_docstring_entry_of_a_name_wins(analyze_files) -> None:
    src = _py_tool('''
@mcp.tool
def f(q: str) -> str:
    """Doc.

    Args:
        q: First.
        q: Second.
    """
    return ""
''')
    assert _params(_tools(analyze_files({"app/server.py": src}))["f"]) == [
        ("q", "str", True, None, "Second.")
    ]


def test_python_google_section_without_valid_names_stops_the_search(analyze_files) -> None:
    src = _py_tool('''
@mcp.tool
def f(limit: int = 1, page: int = 1) -> str:
    """Doc.

    Args:
        limit / page: paging

    :param limit: From Sphinx.
    """
    return ""
''')
    tool = _tools(analyze_files({"app/server.py": src}))["f"]
    assert [p.description for p in tool.params or []] == [None, None]


def test_python_docstring_param_docs_stay_under_the_params_limit(analyze_files) -> None:
    names = [f"p{i}" for i in range(20)]
    args = "".join(f"        {n}: {'x' * 1000}\n" for n in names)
    sig = ", ".join(f"{n}: str" for n in names)
    src = _py_tool(
        f'@mcp.tool\ndef f({sig}, q: str = Field(description="{"y" * 1000}")) -> str:\n'
        f'    """Doc.\n\n    Args:\n{args}    """\n    return ""\n'
    )
    params = _tools(analyze_files({"app/server.py": src}))["f"].params or []
    assert [p.name for p in params] == [*names, "q"]
    assert all(p.type == "str" for p in params)
    documented = [bool(p.description) for p in params[:-1]]
    assert 0 < sum(documented) < len(names)  # added in order until the budget runs out
    assert documented == sorted(documented, reverse=True)
    assert params[-1].description == "y" * 1000  # a Field description is always kept
    size = len(json.dumps([p.model_dump() for p in params], separators=(",", ":")).encode())
    assert size <= 15 * 1024


def test_python_docstring_param_docs_never_push_field_descriptions_past_the_limit(
    analyze_files,
) -> None:
    # Field descriptions count towards the budget: 8 × 1.2 KB declared in code plus 10 × 1 KB
    # from Args used to make 21 KB, past the backend's 16 KB, which then dropped every param.
    fields = "".join(
        f'    f{i}: Annotated[str, Field(description="{"x" * 1200}")],\n' for i in range(8)
    )
    plain = "".join(f"    d{i}: str,\n" for i in range(10))
    args = "".join(f"        d{i}: {'y' * 1000}\n" for i in range(10))
    src = _py_tool(
        "from typing import Annotated\nfrom pydantic import Field\n"
        f'@mcp.tool\ndef big(\n{fields}{plain}) -> str:\n    """Big.\n\n    Args:\n{args}    """\n    return ""\n'
    )
    params = _tools(analyze_files({"app/server.py": src}))["big"].params or []
    assert len(params) == 18
    assert all(p.description == "x" * 1200 for p in params[:8])
    assert 0 < sum(1 for p in params[8:] if p.description) < 10
    size = len(json.dumps([p.model_dump() for p in params], separators=(",", ":")).encode())
    assert size <= 15 * 1024


def test_fastmcp_nullable_context_disabled_tools_and_excluded_args(analyze_files) -> None:
    src = """
from typing import Optional, Union
import mcp.server.fastmcp
from fastmcp import FastMCP, Context

mcp = FastMCP("demo")


@mcp.tool()
def a(q: str, ctx: Context | None = None) -> str:
    return q


@mcp.tool()
def b(q: str, ctx: Optional[Context] = None, c2: Union[Context, None] = None) -> str:
    return q


@mcp.tool()
def c(q: str, ctx: mcp.server.fastmcp.Context, maybe: str | None = None) -> str:
    return q


@mcp.tool(enabled=False)
def hidden(q: str) -> str:
    return q


@mcp.tool(exclude_args=["user_id"])
def scoped(q: str, user_id: str = "") -> str:
    return q


@mcp.tool(exclude_args=EXCLUDED)
def computed(q: str, user_id: str = "") -> str:
    return q
"""
    tools = _tools(analyze_files({"srv.py": src}))
    assert set(tools) == {"a", "b", "c", "scoped", "computed"}
    for name in ("a", "b"):
        assert _params(tools[name]) == [("q", "str", True, None, None)], name
    assert _params(tools["c"]) == [
        ("q", "str", True, None, None),
        ("maybe", "str | None", False, "None", None),
    ]
    assert _params(tools["scoped"]) == [("q", "str", True, None, None)]
    assert tools["computed"].params is None


@pytest.mark.parametrize("jobs", [1, 2])
def test_fastmcp_server_in_another_module_for_any_jobs(capture_mcp_tools, jobs) -> None:
    files = {
        "app/server.py": "from fastmcp import FastMCP\n\nmcp = FastMCP('x')\n",
        "app/tools.py": "from app.server import mcp as server\n\n\n@server.tool\ndef hello(name: str) -> str:\n    return name\n",
    }
    tools = capture_mcp_tools(files, jobs)
    assert set(tools) == {"hello"}
    assert tools["hello"]["framework"] == "fastmcp"


# ── Resources and prompts (`@<server>.resource` / `@<server>.prompt`) ────────


def _of(statements: list[Statement], semantic_type: str) -> dict[str | None, Statement]:
    found = [s for s in statements if s.semanticType == semantic_type]
    method = {"mcp_resource": "RESOURCE", "mcp_prompt": "PROMPT"}[semantic_type]
    for s in found:
        assert s.method == method and s.name == s.endpoint and s.routeKind is None
    return {s.endpoint: s for s in found}


RESOURCES_ONLY = '''
from fastmcp import FastMCP

mcp = FastMCP("docs")


@mcp.resource("config://app", description="App configuration")
def config() -> str:
    return "{}"


@mcp.resource(uri="users://{user_id}/profile")
def profile(user_id: str) -> str:
    """A user's profile."""
    return user_id


@mcp.resource("search://{?q}")
def search(q: str) -> str:
    return q


@mcp.resource(URI)
def computed() -> str:
    return ""


@mcp.resource("off://x", enabled=False)
def off() -> str:
    return ""
'''


def test_resources_in_a_file_with_no_tool(analyze_files) -> None:
    resources = _of(analyze_files({"srv.py": RESOURCES_ONLY}), "mcp_resource")
    assert set(resources) == {"config://app", "users://{user_id}/profile", "search://{?q}"}
    config = resources["config://app"]
    assert (config.framework, config.description, config.handler, config.handlerLine) == (
        "fastmcp", "App configuration", "config", 8)
    assert config.params == [] and config.nodeType == "synthetic" and config.startLine == 7
    profile = resources["users://{user_id}/profile"]
    assert profile.description == "A user's profile."  # the docstring, as for a tool
    assert _params(profile) == [("user_id", "string", True, None, None)]
    # An RFC 6570 operator: what the template accepts is not a list of required strings.
    assert resources["search://{?q}"].params is None


PROMPTS_ONLY = '''
from mcp.server.fastmcp import FastMCP

server = FastMCP("prompts")


@server.prompt()
def review_code(code: str, focus: str = "bugs") -> str:
    """Review a piece of code."""
    return code


@server.prompt(name="Explain", description="Explain a concept")
def explain(topic: str) -> str:
    return topic


@server.prompt("short")
def short_name() -> str:
    return ""


@server.prompt(name=PROMPT_NAME)
def computed() -> str:
    return ""
'''


def test_prompts_in_a_file_with_no_tool(analyze_files) -> None:
    prompts = _of(analyze_files({"srv.py": PROMPTS_ONLY}), "mcp_prompt")
    assert set(prompts) == {"review_code", "Explain", "short"}
    review = prompts["review_code"]
    assert (review.framework, review.description, review.handler, review.handlerLine) == (
        "python-mcp", "Review a piece of code.", "review_code", 8)
    assert _params(review) == [
        ("code", "str", True, None, None),
        ("focus", "str", False, '"bugs"', None),
    ]
    assert prompts["Explain"].description == "Explain a concept"
    assert prompts["short"].params == []


def test_tool_resource_and_prompt_of_one_server(analyze_files) -> None:
    src = '''
from fastmcp import FastMCP

mcp = FastMCP("all")


@mcp.tool()
@mcp.resource("data://one")
@mcp.prompt()
def one() -> str:
    return ""
'''
    statements = analyze_files({"srv.py": src})
    kinds = sorted((s.semanticType, s.endpoint) for s in statements if s.semanticType)
    assert kinds == [("mcp_prompt", "one"), ("mcp_resource", "data://one"), ("mcp_tool", "one")]
    ids = [s.id for s in statements]
    assert len(ids) == len(set(ids))


def test_resource_and_prompt_look_alikes(analyze_files) -> None:
    """Not on a proven server: an agent, a CLI's prompt, a local class named FastMCP."""
    files = {
        "agent.py": '''
from pydantic_ai import Agent

agent = Agent("openai:gpt-4o")


@agent.resource("x://y")
def r() -> str:
    return ""


@agent.prompt()
def p() -> str:
    return ""
''',
        "cli.py": '''
import click


@click.prompt("Name")
def ask() -> str:
    return ""
''',
        "shim.py": '''
class FastMCP:
    pass


mcp = FastMCP()


@mcp.resource("x://shim")
def r() -> str:
    return ""


@mcp.prompt()
def p() -> str:
    return ""
''',
    }
    statements = analyze_files(files)
    assert not [s for s in statements if s.semanticType in ("mcp_resource", "mcp_prompt")]
