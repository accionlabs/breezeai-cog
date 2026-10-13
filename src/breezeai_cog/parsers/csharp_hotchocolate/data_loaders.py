"""HotChocolate source-generated DataLoaders as a registered fact on the C# index,
``hotchocolate.data-loaders``: generated loader type name → the file declaring its
``[DataLoader]`` method (None when two files would generate the same name).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..csharp.attributes import extract_attributes
from ..index_common import record_distinct
from ..treesitter import node_text

FACT = "hotchocolate.data-loaders"


def generated_loaders(index: Any | None) -> dict[str, str | None]:
    return index_fact(index, FACT) or {}


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> dict[str, str | None] | None:
    """Record the DataLoader types HotChocolate's source generator will emit for this file.

    A ``[DataLoader]`` method produces a loader named after it — ``SessionByIdAsync`` becomes
    ``ISessionByIdDataLoader`` (and the concrete ``SessionByIdDataLoader``). Consumers declare the
    generated *interface* as a parameter type, and that interface exists nowhere in the source, so
    without this map a loader parameter is indistinguishable from any other service. Attribute
    arguments only ever carry options (``MaxBatchSize``, ``ServiceScope``), never a rename.
    """
    found: dict[str, str | None] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "method_declaration":
            continue
        if not any(d.name.split("<", 1)[0] == "DataLoader" for d in extract_attributes(node, source)):
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        base = node_text(name_node, source)
        if base.endswith("Async") and len(base) > len("Async"):
            base = base[: -len("Async")]
        for generated in (f"I{base}DataLoader", f"{base}DataLoader"):
            record_distinct(found, generated, rel)
    return found or None


def _resolve(values: list[dict[str, str | None]], index: Any) -> dict[str, str | None] | None:
    out: dict[str, str | None] = {}
    for per_file in values:
        for name, rel in per_file.items():
            record_distinct(out, name, rel)
    return out or None


register_fact(FactSpec(
    name=FACT, language="csharp", gate=(b"[DataLoader",), collect=_collect, resolve=_resolve,
))
