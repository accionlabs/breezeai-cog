"""GraphQL client projects as a registered fact on the GraphQL index, ``graphql.client-configs``.

A ``.graphqlrc.json`` marks a GraphQL **client** project (Strawberry Shake, graphql-config): the
``schema.graphql`` next to it is a downloaded copy of a server's schema, used to generate client
code. Its Query fields are the remote server's operations, not this repo's::

    misc/BlazorGettingStarted/.graphqlrc.json      → a client project
    misc/BlazorGettingStarted/schema.graphql       → a copy: its `node` field is not served here

The fact is the set of folders holding one; the GraphQL parser reads it with :func:`is_client_copy`.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from ..additive import FactSpec, index_fact, register_fact

FACT = "graphql.client-configs"
_CONFIG = ".graphqlrc.json"


def is_client_copy(index: Any | None, path: str) -> bool:
    """Whether the SDL file at ``path`` sits in a folder with a ``.graphqlrc.json``."""
    folders: frozenset[str] = index_fact(index, FACT) or frozenset()
    return str(PurePosixPath(path).parent) in folders


def _collect(root: object, source: bytes, rel: str, repo_root: Path | None) -> str | None:
    p = PurePosixPath(rel)
    return str(p.parent) if p.name == _CONFIG else None


def _resolve(values: list[str], index: object) -> frozenset[str] | None:
    return frozenset(values) or None


register_fact(FactSpec(
    name=FACT, language="graphql", gate=(b"{",), sources=(_CONFIG,),
    collect=_collect, resolve=_resolve,
))
