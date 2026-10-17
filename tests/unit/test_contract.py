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
    # An anonymous handler is always in the record's own file.
    assert ("handlerPath", "error") in _rules(_route(handler=None, handlerPath="src/h.ts"))


def test_a_framework_served_url_needs_no_handler() -> None:
    served = _route(text='app.MapHealthChecks("/health")', handler=None, handlerLine=None,
                    framework="aspnet")
    assert _rules(served) == []
    for text in ("app.use('/api-docs', swaggerUi.serve, swaggerUi.setup(spec));",
                 "app.use('/static', express.static('public'));",
                 "->      /webjars                webjars.Routes"):
        assert _rules(_route(text=text, handler=None, handlerLine=None, method="ANY")) == []
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
