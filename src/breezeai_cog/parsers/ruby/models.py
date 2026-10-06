"""ActiveRecord model and receiver evidence for Ruby parsing."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from tree_sitter import Node

from ..treesitter import node_text, parse_source

_MODEL_BASES = {"activerecord::base", "applicationrecord"}
_ASSOCIATIONS = {"belongs_to", "has_one", "has_many", "has_and_belongs_to_many"}
_ASSOCIATION_ITERATORS = {"each", "find_each", "map", "collect"}


def _walk(node: Node) -> Iterable[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _last_constant(name: str) -> str:
    return name.lstrip(":").rsplit("::", 1)[-1]


def _class_parent(node: Node, source: bytes) -> tuple[str, str] | None:
    header = node_text(node, source).splitlines()[0]
    match = re.match(
        r"\s*class\s+([A-Z]\w*(?:::[A-Z]\w*)*)(?:\s*<\s*([A-Z]\w*(?:::[A-Z]\w*)*))?",
        header,
    )
    if match is None:
        return None
    return match.group(1), match.group(2) or ""


def _resolve_models(declarations: list[tuple[str, str]]) -> frozenset[str]:
    known = set(_MODEL_BASES)
    models: set[str] = set()
    pending = declarations
    while pending:
        remaining: list[tuple[str, str]] = []
        changed = False
        for name, parent in pending:
            if parent and (
                parent.lower() in known
                or _last_constant(parent).lower() in known
            ):
                models.add(name)
                models.add(_last_constant(name))
                known.add(name.lower())
                known.add(_last_constant(name).lower())
                changed = True
            else:
                remaining.append((name, parent))
        if not changed:
            break
        pending = remaining
    return frozenset(models)


def active_record_model_names(root: Node, source: bytes) -> frozenset[str]:
    """Return model classes identifiable from ActiveRecord ancestry in this source file."""
    declarations = [
        declaration
        for node in _walk(root)
        if node.type == "class"
        and (declaration := _class_parent(node, source)) is not None
    ]
    return _resolve_models(declarations)


def build_active_record_model_index(
    repo_root: Path, files: Sequence[Path], jobs: int = 1
) -> frozenset[str]:
    """Index ActiveRecord subclasses across Ruby files for resolving parameter hints."""
    del repo_root, jobs
    declarations: list[tuple[str, str]] = []
    for path in files:
        source = path.read_bytes()
        root = parse_source("ruby", source).root_node
        declarations.extend(
            declaration
            for node in _walk(root)
            if node.type == "class"
            and (declaration := _class_parent(node, source)) is not None
        )
    return _resolve_models(declarations)


def is_active_record_model(
    node: Node, source: bytes, model_names: frozenset[str]
) -> bool:
    declaration = _class_parent(node, source)
    if declaration is None:
        return False
    name, parent = declaration
    known = {item.lower() for item in model_names}
    return (
        name.lower() in known
        or _last_constant(name).lower() in known
        or parent.lower() in _MODEL_BASES
    )


def association_receivers_for_class(node: Node, source: bytes) -> frozenset[str]:
    body = next((child for child in node.named_children if child.type == "body_statement"), None)
    if body is None:
        return frozenset()
    associations: dict[str, str] = {}
    for child in body.named_children:
        if child.type != "call":
            continue
        method = child.child_by_field_name("method")
        method_name = node_text(method, source) if method is not None else ""
        if method_name not in _ASSOCIATIONS:
            continue
        match = re.search(r":([a-z_]\w*)", node_text(child, source))
        if match is not None:
            associations[match.group(1)] = method_name

    receivers = set(associations)
    for association, kind in associations.items():
        if kind in {"has_many", "has_and_belongs_to_many"}:
            if association.endswith("ies") and len(association) > 3:
                receivers.add(association[:-3] + "y")
            elif association.endswith(("ses", "xes", "zes", "ches", "shes")):
                receivers.add(association[:-2])
            elif association.endswith("s") and not association.endswith(
                ("ss", "us", "is", "ws")
            ):
                receivers.add(association[:-1])

    for block in _walk(node):
        if block.type != "block":
            continue
        call = next((child for child in block.named_children if child.type == "call"), None)
        if call is None:
            continue
        method = call.child_by_field_name("method")
        receiver = call.child_by_field_name("receiver")
        receiver_name = node_text(receiver, source) if receiver is not None else ""
        method_name = node_text(method, source) if method is not None else ""
        if receiver_name not in associations or method_name not in _ASSOCIATION_ITERATORS:
            continue
        parameters = block.child_by_field_name("parameters")
        if parameters is None:
            parameters = next(
                (child for child in block.named_children if child.type == "block_parameters"),
                None,
            )
        if parameters is not None:
            receivers.update(
                node_text(parameter, source)
                for parameter in _walk(parameters)
                if parameter.type == "identifier"
            )
    return frozenset(receiver.lower() for receiver in receivers)


def _hinted_model_parameters(
    method: Node, source: bytes, model_names: frozenset[str]
) -> frozenset[str]:
    parameters = method.child_by_field_name("parameters")
    if parameters is None:
        return frozenset()
    parameter_names = _method_parameter_names(method, source)
    if not parameter_names:
        return frozenset()

    prefix_lines = source[: method.start_byte].decode("utf-8", "replace").splitlines()
    preceding: list[str] = []
    for line in reversed(prefix_lines[-12:]):
        stripped = line.strip()
        if not stripped:
            if preceding:
                break
            continue
        if stripped.startswith(("#", "sig")):
            preceding.append(stripped)
            continue
        break
    hints = "\n".join(reversed(preceding))
    model_types = {_last_constant(name).lower() for name in model_names}
    typed: set[str] = set()

    for match in re.finditer(
        r"@param\s+(?:\[([^\]]+)\]\s+)?([a-z_]\w*)(?:\s+\[([^\]]+)\])?",
        hints,
    ):
        name = match.group(2)
        type_text = match.group(1) or match.group(3) or ""
        if name in parameter_names and _last_constant(type_text).lower() in model_types:
            typed.add(name.lower())

    for match in re.finditer(
        r"([a-z_]\w*)\s*:\s*(?:T(?:::|\.)\w+\s*\(\s*)?([A-Z]\w*(?:::[A-Z]\w*)*)",
        hints,
    ):
        name, type_name = match.groups()
        if name in parameter_names and _last_constant(type_name).lower() in model_types:
            typed.add(name.lower())
    return frozenset(typed)


def _method_parameter_names(method: Node, source: bytes) -> set[str]:
    parameters = method.child_by_field_name("parameters")
    if parameters is None:
        return set()
    return {
        node_text(child, source)
        for child in _walk(parameters)
        if child.type == "identifier"
    }


def active_record_receivers_for_method(
    method: Node,
    source: bytes,
    model_names: frozenset[str],
    association_receivers: frozenset[str],
    *,
    in_model_scope: bool,
) -> frozenset[str]:
    shadowed_associations = {
        name.lower() for name in _method_parameter_names(method, source)
    }
    receivers = set(association_receivers) - shadowed_associations
    receivers.update(_hinted_model_parameters(method, source, model_names))
    if in_model_scope:
        receivers.add("self")
    return frozenset(receivers)
