"""gorilla/mux routes.

::

    r := mux.NewRouter()
    api := r.PathPrefix("/api").Subrouter()
    api.HandleFunc("/users/{id}", getUser).Methods("GET", "PUT")
        → route GET /api/users/{id} and route PUT /api/users/{id}, handler getUser

A route with no ``.Methods(…)`` serves every verb and is recorded with an empty ``method``."""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..go.routing import RouterSpec, detect_routes, register_router_fact
from ..treesitter import node_text


def _verbs_of(call: Node, source: bytes) -> list[str]:
    args = call.child_by_field_name("arguments")
    out: list[str] = []
    for arg in args.named_children if args is not None else []:
        if arg.type == "interpreted_string_literal":
            out.append(node_text(arg, source)[1:-1].upper())
        elif arg.type == "selector_expression" and node_text(arg, source).startswith("http.Method"):
            out.append(node_text(arg, source)[len("http.Method"):].upper())
    return out


def _methods(call: Node, source: bytes) -> list[str] | None:
    """The verbs of ``.Methods("GET", …)`` on the route's chain, after the route call
    (``r.HandleFunc(p, h).Methods("GET")``) or before it (``r.Path(p).Methods("GET").HandlerFunc(h)``)."""
    verbs: list[str] = []
    node = call
    while (node.parent is not None and node.parent.type == "selector_expression"
           and node.parent.parent is not None and node.parent.parent.type == "call_expression"):
        field = node.parent.child_by_field_name("field")
        node = node.parent.parent
        if field is not None and node_text(field, source) == "Methods":
            verbs.extend(_verbs_of(node, source))
    fn = call.child_by_field_name("function")
    operand = fn.child_by_field_name("operand") if fn is not None and fn.type == "selector_expression" else None
    while operand is not None and operand.type == "call_expression":
        inner = operand.child_by_field_name("function")
        if inner is None or inner.type != "selector_expression":
            break
        if node_text(inner.child_by_field_name("field"), source) == "Methods":
            verbs.extend(_verbs_of(operand, source))
        operand = inner.child_by_field_name("operand")
    return verbs or None


SPEC = RouterSpec(
    name="gorilla-mux",
    import_path="github.com/gorilla/mux",
    router_types=frozenset({"Router"}),
    app_types=frozenset({"Router"}),
    constructors=frozenset({"NewRouter"}),
    verbs={"HandleFunc": None, "Handle": None},
    groups={"PathPrefix": "child", "Path": "child", "Subrouter": "same", "Methods": "same",
            "Host": "same", "Schemes": "same", "Headers": "same"},
    pathless=frozenset({"Handler", "HandlerFunc"}),
    use=frozenset({"Use"}),
    chain_verbs=_methods,
)

register_router_fact(SPEC)


def _detect(dc: DetectContext) -> Detection | None:
    return detect_routes(dc, SPEC)


register_detector(Detector(
    name="go-gorilla-mux", gate=Gate("go", order=60, guard=b"gorilla/mux", skip_fixtures=True),
    detect=_detect, frameworks=(SPEC.name,),
))
