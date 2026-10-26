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


def _methods(call: Node, source: bytes) -> list[str] | None:
    """The verbs of ``.Methods("GET", …)`` anywhere on the chain the route call starts."""
    verbs: list[str] = []
    node = call
    while (node.parent is not None and node.parent.type == "selector_expression"
           and node.parent.parent is not None and node.parent.parent.type == "call_expression"):
        selector, outer = node.parent, node.parent.parent
        field = selector.child_by_field_name("field")
        if field is not None and node_text(field, source) == "Methods":
            args = outer.child_by_field_name("arguments")
            for arg in args.named_children if args is not None else []:
                if arg.type == "interpreted_string_literal":
                    verbs.append(node_text(arg, source)[1:-1].upper())
                elif arg.type == "selector_expression" and node_text(arg, source).startswith("http.Method"):
                    verbs.append(node_text(arg, source)[len("http.Method"):].upper())
        node = outer
    return verbs or None


SPEC = RouterSpec(
    name="gorilla-mux",
    import_path="github.com/gorilla/mux",
    router_types=frozenset({"Router"}),
    app_types=frozenset({"Router"}),
    constructors=frozenset({"NewRouter"}),
    verbs={"HandleFunc": None, "Handle": None},
    groups={"PathPrefix": "child", "Subrouter": "same", "Methods": "same", "Host": "same",
            "Schemes": "same", "Headers": "same"},
    chain_verbs=_methods,
)

register_router_fact(SPEC)


def _detect(dc: DetectContext) -> Detection | None:
    return detect_routes(dc, SPEC)


register_detector(Detector(
    name="go-gorilla-mux", gate=Gate("go", order=60, guard=b"gorilla/mux", skip_fixtures=True),
    detect=_detect, frameworks=(SPEC.name,),
))
