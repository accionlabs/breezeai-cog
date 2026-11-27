"""AWS SQS receive detection for TypeScript.

This is an additive detector.  It records polling calls made through the typed AWS
clients, while deliberately leaving unknown receivers and unknown queue addresses
unclassified/unresolved.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import node_text
from .sdk_calls import _injected_client_fields, _package_imports
from ..typescript_sdk_common import (
    Sdk,
    callee_chain,
    client_identifiers,
    this_member_call,
    walk_calls,
)


_V3_PACKAGE = "@aws-sdk/client-sqs"
_V3_CLIENT_TYPES = frozenset({"SQSClient"})
_V2_PACKAGE = "aws-sdk"
_V2_CLIENT_TYPES = frozenset({"SQS"})


def _string_value(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type != "string":
        return None
    fragment = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(fragment, source) if fragment is not None else ""


def _queue_url_value(value: Node | None, source: bytes, constants: dict[str, str]) -> str | None:
    literal = _string_value(value, source)
    if literal is not None:
        return literal
    if value is not None and value.type == "identifier":
        return constants.get(node_text(value, source))
    return None


def _constants(root: Node, source: bytes) -> dict[str, str]:
    found: dict[str, str] = {}

    def visit(node: Node) -> None:
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if name is not None and name.type == "identifier":
                resolved = _string_value(value, source)
                if resolved is not None:
                    found[node_text(name, source)] = resolved
        for child in node.named_children:
            visit(child)

    visit(root)
    return found


def _namespace_imports(root: Node, source: bytes, package: str) -> set[str]:
    """Return namespace aliases imported from an exact package specifier."""
    aliases: set[str] = set()
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        source_node = node.child_by_field_name("source")
        fragment = (
            next((c for c in source_node.named_children if c.type == "string_fragment"), None)
            if source_node is not None
            else None
        )
        if fragment is None or node_text(fragment, source) != package:
            continue
        namespace = (
            next(
                (c for c in node.named_children[0].named_children if c.type == "namespace_import"),
                None,
            )
            if node.named_children
            else None
        )
        alias = (
            namespace.named_children[-1]
            if namespace is not None and namespace.named_children
            else None
        )
        if alias is not None and alias.type == "identifier":
            aliases.add(node_text(alias, source))
    return aliases


def _qualified_sqs_ids(root: Node, source: bytes, aliases: set[str]) -> set[str]:
    """Find bindings typed as an imported namespace's ``SQS`` member.

    This includes constructor parameter properties, which are represented as parameters
    but are accessed later through ``this.<name>``.
    """
    if not aliases:
        return set()
    qualified = {f"{alias}.SQS" for alias in aliases}
    ids: set[str] = set()

    def type_text(node: Node) -> str | None:
        annotation = next((c for c in node.named_children if c.type == "type_annotation"), None)
        if annotation is None:
            return None
        text = node_text(annotation, source).lstrip(":").strip()
        return text if text in qualified else None

    def add_name(node: Node | None) -> None:
        if node is not None and node.type == "identifier":
            ids.add(node_text(node, source))

    def visit(node: Node) -> None:
        if node.type == "required_parameter" and type_text(node) is not None:
            add_name(
                node.child_by_field_name("pattern")
                or (node.named_children[0] if node.named_children else None)
            )
        elif node.type in ("variable_declarator", "field_definition", "public_field_definition"):
            name = node.child_by_field_name("name")
            if type_text(node) is not None:
                add_name(name)
            else:
                value = node.child_by_field_name("value")
                if value is not None and value.type == "new_expression":
                    constructor = value.child_by_field_name("constructor")
                    if constructor is not None and node_text(constructor, source) in qualified:
                        add_name(name)
        for child in node.named_children:
            visit(child)

    visit(root)
    return ids


def resolve_queue_name(args: Node | None, source: bytes, constants: dict[str, str]) -> str | None:
    """Resolve the queue name from the first object's ``QueueUrl`` property."""
    if args is None:
        return None
    options = next((c for c in args.named_children if c.type == "object"), None)
    if options is None:
        return None
    for pair in options.named_children:
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        if key is None or node_text(key, source) != "QueueUrl":
            continue
        url = _queue_url_value(pair.child_by_field_name("value"), source, constants)
        if url is None:
            return None
        parts = [part for part in url.split("/") if part]
        return parts[-1] if parts else None
    return None


def _handler_name(call: Node, source: bytes) -> str | None:
    current = call.parent
    while current is not None:
        if current.type in (
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
            "function_expression",
        ):
            name = current.child_by_field_name("name")
            if name is not None:
                return node_text(name, source)
        if current.type == "arrow_function":
            parent = current.parent
            if parent is not None and parent.type == "variable_declarator":
                name = parent.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    return node_text(name, source)
        current = current.parent
    return None


def _emit(call: Node, source: bytes, endpoint: str | None, out: list[Emit]) -> None:
    out.append(
        Emit(
            call,
            {
                "semanticType": "eventbus_consumer",
                "framework": "aws-sqs",
                "method": "CONSUMER",
                "endpoint": endpoint,
                "handler": _handler_name(call, source),
            },
            enrich="unclassified",
        )
    )


def _client_ids(root: Node, source: bytes, types: frozenset[str]) -> tuple[set[str], set[str]]:
    sdk = Sdk(b"", "aws-sqs", types, frozenset())
    return client_identifiers(root, source, sdk), _injected_client_fields(root, source, types)


def detect_sqs_receive_v3(
    root: Node, source: bytes, constants: dict[str, str], out: list[Emit]
) -> bool:
    imported = _package_imports(root, source, _V3_PACKAGE)
    if "ReceiveMessageCommand" not in imported:
        return False
    clients, fields = _client_ids(root, source, _V3_CLIENT_TYPES)
    if not clients and not fields:
        return False
    found = False
    for call in walk_calls(root):
        chain = callee_chain(call, source)
        receiver = None
        if chain is not None and chain[2] == "send":
            receiver = chain[1] if chain[1] in clients else None
        if receiver is None:
            member = this_member_call(call, source)
            if member is None or member[0] not in fields or member[1] != "send":
                continue
        args = call.child_by_field_name("arguments")
        first = (
            next((c for c in args.named_children if c.type != "comment"), None) if args else None
        )
        if first is None or first.type != "new_expression":
            continue
        ctor = first.child_by_field_name("constructor")
        if ctor is None or node_text(ctor, source) != "ReceiveMessageCommand":
            continue
        _emit(
            call,
            source,
            resolve_queue_name(first.child_by_field_name("arguments"), source, constants),
            out,
        )
        found = True
    return found


def detect_sqs_receive_v2(
    root: Node, source: bytes, constants: dict[str, str], out: list[Emit]
) -> bool:
    imported = _package_imports(root, source, _V2_PACKAGE)
    namespace_aliases = _namespace_imports(root, source, _V2_PACKAGE)
    if "SQS" not in imported and not namespace_aliases:
        return False
    clients, fields = _client_ids(root, source, _V2_CLIENT_TYPES)
    qualified = _qualified_sqs_ids(root, source, namespace_aliases)
    clients.update(qualified)
    fields.update(qualified)
    if not clients and not fields:
        return False
    found = False
    for call in walk_calls(root):
        chain = callee_chain(call, source)
        valid = chain is not None and chain[2] == "receiveMessage" and chain[1] in clients
        member = this_member_call(call, source)
        valid = valid or (
            member is not None and member[0] in fields and member[1] == "receiveMessage"
        )
        if not valid:
            continue
        _emit(
            call,
            source,
            resolve_queue_name(call.child_by_field_name("arguments"), source, constants),
            out,
        )
        found = True
    return found


def _detect(dc: DetectContext) -> Detection | None:
    constants = _constants(dc.root, dc.source)
    found: list[Emit] = []
    detect_sqs_receive_v3(dc.root, dc.source, constants, found)
    detect_sqs_receive_v2(dc.root, dc.source, constants, found)
    return Detection(found) if found else None


register_detector(
    Detector(
        name="aws-sqs-consumer",
        gate=Gate("typescript", order=22),
        detect=_detect,
        frameworks=("aws-sqs",),
    )
)
