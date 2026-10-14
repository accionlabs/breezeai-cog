"""GraphQL root fields' DTOs as a registered fact, ``graphql.sdl-root-fields``.

Schema-first GraphQL splits an operation in two (contract §4): the SDL declares it, the resolver
map implements it, and only the resolver is the entry point::

    # schema.graphql — a plain statement
    type Mutation { addBook(input: BookInput!): Book }
    // resolvers.ts — the binding
    export const resolvers = { Mutation: { addBook: (_, { input }) => … } };

The request and response types live on the SDL side. This fact collects every root field's
DTOs while the TypeScript index is built — from ``.graphql`` files (owned by the GraphQL parser,
read through ``FactSpec.sources``) and from SDL embedded in TS ``gql`` templates — so the
resolver-map record carries ``requestDTO`` / ``responseDTO``. A field declared twice with
different DTOs is left without (honest-null).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..graphql.sdl import root_field_dtos
from ..index_common import record_distinct
from ..treesitter import parse_source

FACT = "graphql.sdl-root-fields"

_SDL_EXTS = (".graphql", ".gql", ".graphqls")
_SDL_MARKERS = (b"type Query", b"type Mutation", b"type Subscription")

#: (root type, field) → (requestDTO, responseDTO)
RootFieldDtos = dict[tuple[str, str], tuple[str | None, str | None]]


def root_field_dto(index: Any | None, type_name: str, field: str) -> tuple[str | None, str | None]:
    """The SDL DTOs of ``type_name.field`` (``(None, None)`` when unknown or ambiguous)."""
    dtos: RootFieldDtos = index_fact(index, FACT) or {}
    return dtos.get((type_name, field), (None, None))


def _embedded(root: Node, source: bytes) -> list[tuple[str, str, str | None, str | None]]:
    """Root fields of SDL embedded in a TS file's template strings."""
    out: list[tuple[str, str, str | None, str | None]] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "string_fragment":
            frag = source[n.start_byte:n.end_byte]
            if any(m in frag for m in _SDL_MARKERS):
                out += root_field_dtos(parse_source("graphql", frag, 0).root_node, frag)
            continue
        stack.extend(n.named_children)
    return out


def _collect(
    root: Node | None, source: bytes, rel: str, repo_root: Path | None
) -> list[tuple[str, str, str | None, str | None]] | None:
    if root is None:
        return None
    found = root_field_dtos(root, source) if rel.lower().endswith(_SDL_EXTS) \
        else _embedded(root, source)
    return found or None


def _resolve(values: list[list[tuple[str, str, str | None, str | None]]], index: object) -> RootFieldDtos | None:
    dtos: dict[tuple[str, str], tuple[str | None, str | None] | None] = {}
    for type_name, field, request, response in sorted(
        (f for per_file in values for f in per_file), key=lambda f: (f[0], f[1])
    ):
        record_distinct(dtos, (type_name, field), (request, response))
    out = {k: v for k, v in dtos.items() if v is not None and v != (None, None)}
    return out or None


register_fact(FactSpec(
    name=FACT, language="typescript", gate=_SDL_MARKERS, sources=_SDL_EXTS, grammar="graphql",
    collect=_collect, resolve=_resolve,
))
