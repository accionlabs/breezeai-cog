"""The VB ``collect`` of the ``webforms.page-routes`` fact: ``routes.MapPageRoute(name, url,
"~/physical.aspx")`` calls read from the VB tree (the fact itself is
:mod:`~..webforms_common.page_routes`)::

    RouteTable.Routes.MapPageRoute("orders", "shop/orders", "~/Orders.aspx")
        → the page Orders.aspx is served at /shop/orders

Only positional literal arguments are read (a named ``routeUrl:=`` argument, a variable or a
``Const`` gives nothing, and the page keeps its physical address).
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ..additive import FactSpec, register_fact
from ..vb.scopes import iter_calls, string_value
from ..webforms_common.page_routes import FACT, add_page_route, app_root_of, resolve_page_routes


def _collect(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> dict[str, list[str]] | None:
    app_root = app_root_of(rel, repo_root)
    found: dict[str, list[str]] = {}
    for call in iter_calls(root, source):
        if call.method.lower() == "mappageroute" and len(call.args) >= 3:
            add_page_route(found, string_value(call.args[1], source),
                           string_value(call.args[2], source), app_root)
    return found or None


register_fact(FactSpec(
    name=FACT, language="vb", gate=(b"MapPageRoute",), collect=_collect,
    resolve=resolve_page_routes,
))
