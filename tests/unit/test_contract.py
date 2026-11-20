"""The entry-point contract validator (parsers/contract.py)."""

from __future__ import annotations

from breezeai_cog.parsers.contract import validate
from breezeai_cog.schemas import FileRecord, Statement


def _route(line: int = 1, **fields) -> Statement:
    base = dict(
        id=f"a.ts:{line}:0", parentId="a.ts", nodeType="call_expression", text="app.get('/x', h)",
        path="a.ts",
        startLine=line, endLine=line, semanticType="route", framework="express", method="GET",
        routeKind="route", endpoint="/x", handler="h", handlerLine=line,
    )
    base.update(fields)
    return Statement(**base)


def _rules(*statements: Statement) -> list[tuple[str, str]]:
    record = FileRecord(id="a.ts", path="a.ts", type="code", language="typescript", loc=1,
                        statements=list(statements))
    return [(v.rule, v.severity) for v in validate(record)]


def test_a_conforming_route_has_no_violation() -> None:
    assert _rules(_route()) == []


def test_wrong_values_are_errors() -> None:
    assert _rules(_route(method="FETCH")) == [("method", "error")]
    assert _rules(_route(routeKind=None)) == [("routeKind", "error")]
    assert _rules(_route(routeKind="rest")) == [("routeKind", "error")]
    assert _rules(_route(framework=None)) == [("framework", "error")]
    assert _rules(_route(endpoint="${BASE}/x")) == [("endpoint", "error")]
    assert _rules(_route(endpoint="")) == [("endpoint", "error")]
    assert _rules(_route(guards=[])) == [("guards", "error")]
    assert _rules(_route(guards=["auth", "auth"])) == [("guards", "error")]


def test_an_unresolved_endpoint_is_allowed() -> None:
    assert _rules(_route(endpoint=None)) == []


def test_a_missing_handler_is_a_gap() -> None:
    assert _rules(_route(handler=None, handlerLine=None)) == [("handler", "gap")]
    # An anonymous handler: no name, its line.
    assert _rules(_route(handler=None)) == []


def test_a_handler_that_cannot_be_located_is_a_gap() -> None:
    # Named, declared in another file, but neither its file nor its line is known.
    assert _rules(_route(handlerLine=None)) == [("handlerLine", "gap")]
    # Located in another file: its path and its line there.
    assert _rules(_route(handlerPath="src/handlers.ts", handlerLine=12)) == []


def test_a_wrong_handler_path_is_an_error() -> None:
    for bad in ("", "a.ts", "/abs/src/handlers.ts"):  # empty, the record's own file, absolute
        assert ("handlerPath", "error") in _rules(_route(handlerPath=bad))
    # An anonymous default export in another module: no name, its file and line.
    assert _rules(_route(handler=None, handlerPath="src/h.ts", handlerLine=3)) == []


def test_a_framework_served_url_needs_no_handler() -> None:
    served = _route(text='app.MapHealthChecks("/health")', handler=None, handlerLine=None,
                    framework="aspnet")
    assert _rules(served) == []
    for text in ("app.use('/api-docs', swaggerUi.serve, swaggerUi.setup(spec));",
                 "app.use('/static', express.static('public'));",
                 "->      /webjars                webjars.Routes"):
        assert _rules(_route(text=text, handler=None, handlerLine=None, method="ANY")) == []
    # A Next.js MDX page (any suffix case): the file's content is the page.
    for mdx in ("app/about/page.mdx", "app/about/page.MDX"):
        page = _route(id=f"{mdx}:1:0", parentId=mdx, path=mdx, nodeType="synthetic", text="# About",
                      framework="nextjs", method=None, routeKind="page", endpoint="/about",
                      handler=None, handlerLine=None)
        assert _rules(page) == []
    relay = _route(text="node(id: ID!): Node", handler=None, handlerLine=None, framework="graphql",
                   method="QUERY", routeKind="query", endpoint="node", responseDTO="Node")
    assert _rules(relay) == []
    # An ordinary root field with no handler is still a gap.
    assert _rules(_route(text="me: User", handler=None, handlerLine=None, framework="graphql",
                         method="QUERY", routeKind="query", endpoint="me",
                         responseDTO="User")) == [("handler", "gap")]


def test_one_record_per_binding() -> None:
    assert _rules(_route(), _route(id="a.ts:1:0#2")) == [("one-record", "error")]
    assert _rules(_route(), _route(id="a.ts:1:0#2", method="POST")) == []
    # One address, several handlers (a service proxy's methods): several bindings.
    assert _rules(_route(), _route(id="a.ts:1:0#2", handler="other")) == []


def test_only_entry_points_are_checked() -> None:
    assert _rules(_route(routeKind="field_resolver", method="RESOLVE_FIELD")) == []
    # Mounts, layouts and links (the spec's current form) are not entry points.
    assert _rules(_route(routeKind="mount", handler=None)) == []
    assert _rules(_route(routeKind="layout", handler=None)) == []
    assert _rules(_route(semanticType="api_call", routeKind=None)) == []


def test_consumers_and_timers_have_their_own_route_kinds() -> None:
    assert _rules(_route(semanticType="eventbus_consumer", routeKind=None, method=None)) == []
    assert _rules(_route(semanticType="eventbus_consumer", routeKind="message")) == []
    assert _rules(_route(semanticType="timer", routeKind="page")) == [("routeKind", "error")]


def test_outbound_addresses_are_written_like_entry_points() -> None:
    assert _rules(_route(semanticType="api_call", routeKind=None, endpoint="/users/{id}")) == []
    assert _rules(_route(routeKind="navigation", endpoint="${BASE}/x")) == [("outbound", "error")]
    assert _rules(_route(semanticType="eventbus_send", endpoint="")) == [("outbound", "error")]


def test_django_views_used_directly_are_framework_served() -> None:
    for handler in ("TemplateView", "RedirectView", "serve", "PasswordResetView"):
        assert _rules(_route(handler=handler, handlerLine=None, framework="django")) == [], handler
    # A repo view, or a library's that is not Django's, still leads somewhere: a gap when it can't.
    assert _rules(_route(handler="HomeView", handlerLine=None, framework="django")) == [("handlerLine", "gap")]
    assert _rules(_route(handler=None, handlerLine=None, framework="django")) == [("handler", "gap")]
    # Only for Django records.
    assert _rules(_route(handler="serve", handlerLine=None)) == [("handlerLine", "gap")]


def _mcp(semantic_type: str, method: str, **fields) -> Statement:
    base = dict(semanticType=semantic_type, method=method, routeKind=None, framework="fastmcp",
                endpoint="x", handler="h", handlerLine=1)
    base.update(fields)
    return _route(**base)


def test_every_entry_point_type_has_its_route_kinds() -> None:
    from breezeai_cog.parsers.contract import ENTRY_POINT_TYPES, ROUTE_KINDS

    assert set(ROUTE_KINDS) == ENTRY_POINT_TYPES


def test_mcp_resources_and_prompts_are_entry_points() -> None:
    assert _rules(_mcp("mcp_resource", "RESOURCE", endpoint="users://{id}/profile")) == []
    assert _rules(_mcp("mcp_resource", "RESOURCE", endpoint="file:///logs/app.log")) == []
    assert _rules(_mcp("mcp_prompt", "PROMPT", endpoint="review_code")) == []
    # Held to the entry-point contract like a tool.
    assert _rules(_mcp("mcp_resource", "RESOURCE", endpoint="${BASE}/x")) == [("endpoint", "error")]
    assert _rules(_mcp("mcp_prompt", "PROMPT", routeKind="route")) == [("routeKind", "error")]
    assert _rules(_mcp("mcp_prompt", "PROMPT", framework=None)) == [("framework", "error")]
    assert _rules(_mcp("mcp_prompt", "FETCH")) == [("method", "error")]
    assert _rules(_mcp("mcp_resource", "RESOURCE", handler=None, handlerLine=None)) == [
        ("handler", "gap")
    ]


def _call(**fields) -> Statement:
    base = dict(semanticType="mcp_call", method="TOOL", routeKind=None, framework="python-mcp",
                endpoint="add", handler=None, handlerLine=None)
    base.update(fields)
    return _route(**base)


def test_an_mcp_call_names_what_it_calls() -> None:
    for method in ("TOOL", "RESOURCE", "PROMPT"):
        assert _rules(_call(method=method)) == []
    assert _rules(_call(method="RESOURCE", endpoint="users://{id}")) == []
    assert _rules(_call(method="GET")) == [("outbound", "error")]
    assert _rules(_call(method=None)) == [("outbound", "error")]
    assert _rules(_call(framework=None)) == [("outbound", "error")]
    assert _rules(_call(endpoint=None)) == [("outbound", "error")]
    assert _rules(_call(endpoint="")) == [("outbound", "error")]
    assert _rules(_call(endpoint="${tool}")) == [("outbound", "error")]
    assert _rules(_call(handler="h")) == [("outbound", "error")]
    assert _rules(_call(handlerLine=3)) == [("outbound", "error")]
    # Not an entry point: a call is never one record of a binding.
    assert _rules(_call(), _call(id="a.ts:1:0#2")) == []


def test_a_resource_uri_template_is_read_without_its_expressions() -> None:
    """RFC 6570: ``page://doc#{anchor}`` is a literal ``#`` then an expression, not a ``#{…}``
    placeholder. A ``${…}`` or a backtick outside an expression is still one."""
    for uri in (
        "page://doc#{anchor}",
        "page://doc{#anchor}",
        "files://{+path}",
        "users://{ids*}/x#{a,b}",
        "q://x{?page,size}",
    ):
        assert _rules(_mcp("mcp_resource", "RESOURCE", endpoint=uri)) == [], uri
        assert _rules(_call(method="RESOURCE", endpoint=uri)) == [], uri
    for uri in ("${BASE}/x", "page://`x`", "page://{id}/${rest}", "page://#{a"):
        assert _rules(_mcp("mcp_resource", "RESOURCE", endpoint=uri)) == [("endpoint", "error")]
        assert _rules(_call(method="RESOURCE", endpoint=uri)) == [("outbound", "error")]
    # Only a resource URI: a tool, a prompt, a route keep every placeholder.
    assert _rules(_call(method="TOOL", endpoint="a#{b}")) == [("outbound", "error")]
    assert _rules(_call(method="PROMPT", endpoint="a#{b}")) == [("outbound", "error")]
    assert _rules(_mcp("mcp_prompt", "PROMPT", endpoint="a#{b}")) == [("endpoint", "error")]
    assert _rules(_mcp("mcp_tool", "TOOL", endpoint="a#{b}")) == [("endpoint", "error")]
    assert _rules(_route(endpoint="/x#{y}")) == [("endpoint", "error")]
    assert _rules(_route(semanticType="api_call", routeKind=None, endpoint="/x#{y}")) == [
        ("outbound", "error")
    ]
