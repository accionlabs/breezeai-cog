"""The GraphQL repo index (``GraphQLParser.build_index``): the registered facts of the GraphQL
files (``register_fact``), including those read from files other parsers own
(``FactSpec.sources``, e.g. the ``.graphqlrc.json`` of ``graphql.client-configs``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..additive import (
    collect_additive, collect_fact_sources, facts_for, resolve_additive, split_fact_sources,
)
from ..index_common import parallel_map
from ..treesitter import parse_source


@dataclass(frozen=True)
class GraphQLIndex:
    #: Facts registered with ``register_fact`` (fact name → fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)


def _file_facts(args: tuple[Path, Path]) -> dict[str, Any]:
    """Picklable per-file worker: what registered facts collect from one GraphQL file. A file
    that cannot be read or parsed contributes nothing."""
    repo_root, abs_path = args
    try:
        source = abs_path.read_bytes()
    except OSError:
        return {}
    if not any(g in source for spec in facts_for("graphql") if not spec.sources
               for g in spec.gate):
        return {}
    try:
        root = parse_source("graphql", source).root_node
    except ValueError:  # bounded-parse failure — skip this file only
        return {}
    return collect_additive("graphql", root, source,
                            abs_path.relative_to(repo_root).as_posix(), repo_root)


def build_graphql_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> GraphQLIndex | None:
    root = Path(repo_root)
    files, fact_files = split_fact_sources("graphql", list(files))
    # registered fact name → per-file values: fact-source files (another parser's), then our own
    collected: dict[str, list[Any]] = collect_fact_sources("graphql", fact_files, root, jobs)
    if any(not spec.sources for spec in facts_for("graphql")):
        for facts in parallel_map([(root, f) for f in files], _file_facts, jobs):
            for name, value in facts.items():
                collected.setdefault(name, []).append(value)
    facts = resolve_additive("graphql", collected, GraphQLIndex())
    return GraphQLIndex(facts) if facts else None
