"""Additive detector: Apache HttpClient requests are ``api_call`` records.

A request names its verb by its class and is sent by a generic ``execute``::

    HttpGet get = new HttpGet(url);          // api_call, method GET
    CloseableHttpResponse res = client.execute(get);

so the record sits where the request is built: ``new HttpGet(…)`` → ``GET``. The class must be
imported from Apache's package (4.x ``org.apache.http.client.methods``, 5.x
``org.apache.hc.client5.http.classic.methods``) or written fully qualified, so a class of the
repository's own named ``HttpGet`` is not one. ``endpoint`` is the first argument when it is a
string or a concatenation holding one (Java's usual URL rendering); a variable leaves it empty.
"""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..java.statements import render_url
from ..java.syntax import imports_and_package
from ..java.http_helpers import defining_requests
from ..outbound_helpers import Request, RequestSource, register_requests
from ..statements_common import url_or_value
from ..treesitter import first_line, node_text

#: The packages a request class is imported from.
_PACKAGES = ("org.apache.http.client.methods", "org.apache.hc.client5.http.classic.methods")
#: Request class → the HTTP verb it sends.
_VERBS = {
    "HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT", "HttpDelete": "DELETE",
    "HttpPatch": "PATCH", "HttpHead": "HEAD", "HttpOptions": "OPTIONS",
}


def _imported(external_imports: list[str]) -> set[str]:
    """The request classes the file imports from Apache's packages."""
    names: set[str] = set()
    for imp in external_imports:
        package, _, name = imp.rpartition(".")
        if package in _PACKAGES:
            names.update(_VERBS if name == "*" else {name} & _VERBS.keys())
    return names


def _verb(node: Node, source: bytes, imported: set[str]) -> str | None:
    """The verb of ``new HttpX(…)`` when ``HttpX`` is Apache's request class, else None."""
    type_node = node.child_by_field_name("type")
    if type_node is None:
        return None
    if type_node.type == "generic_type":  # not generic, but tolerate ``new HttpGet<>()``
        type_node = type_node.named_children[0] if type_node.named_children else type_node
    text = "".join(node_text(type_node, source).split())
    package, _, name = text.rpartition(".")
    if name not in _VERBS:
        return None
    if package:
        return _VERBS[name] if package in _PACKAGES else None
    return _VERBS[name] if name in imported else None


def _endpoint(node: Node, source: bytes) -> str | None:
    args = node.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    return url_or_value(first, source, render_url) if first is not None else None


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "object_creation_expression":
            yield current
        stack.extend(reversed(current.named_children))


def requests(root: Node, source: bytes, imports: list[str]) -> Iterator[tuple[Node, str, Node | None]]:
    """Each ``new HttpX(…)`` of Apache's request classes under ``root``: (the creation, its
    verb, its URI argument or None). ``imports``: the file's imports as written (``a.b.C``,
    ``a.b.*``). One matcher for capture and the HTTP-helper index (``java/http_helpers.py``)."""
    imported = _imported(imports)
    for node in _walk(root):
        verb = _verb(node, source, imported)
        if verb is None:
            continue
        args = node.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        yield node, verb, first


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    defining = defining_requests(dc.path, dc.ctx.resolution_index)
    found: list[Emit] = []
    for node, verb, _uri in requests(dc.root, source, dc.record.externalImports):
        if (node.start_byte, node.end_byte) in defining:
            continue  # inside an HTTP helper, on its own URL parameter: its callers make it
        found.append(Emit(
            node, {"semanticType": "api_call", "method": verb},
            fill={"endpoint": _endpoint(node, source)}, enrich="unclassified",
            text=first_line(node_text(node, source))[:120],
        ))
    return Detection(found) if found else None


# Requests in test files go to a stub server, so those files are skipped.
register_detector(Detector(
    name="apache-httpclient",
    gate=Gate("java", order=60, guard=b"org.apache.h", skip_fixtures=True),
    detect=_detect,
))


def _helper_requests(root: Node, source: bytes) -> Iterator[Request]:
    """The requests an application's own HTTP helper may make with Apache's client: the same
    matcher, for the helper rule (``outbound_helpers.register_requests``)."""
    singles, wildcards, _ = imports_and_package(root, source)
    imports = [*singles, *(f"{w}.*" for w in wildcards)]
    for node, verb, uri in requests(root, source, imports):
        yield Request(node=node, url=uri, verb=verb)


register_requests(RequestSource("java-apache-httpclient", "java", (b"org.apache.h",),
                                _helper_requests))
