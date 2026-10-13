"""The Python repo index (``PythonParser.build_index``): the registered facts of the Python
files (``register_fact``), collected once per repository before parsing — FastAPI router wiring
(``fastapi.wiring``), Celery task names (``celery.tasks``), …. Only files matching a fact's gate
are parsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from ..additive import (
    collect_additive, collect_fact_sources, facts_for, resolve_additive, split_fact_sources,
)
from ..index_common import parallel_map
from ..treesitter import parse_source

@dataclass(frozen=True)
class PythonIndex:
    #: Facts registered with ``register_fact`` (fact name → fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)


def _registered(args: tuple[Path, Path]) -> dict[str, Any]:
    """Picklable per-file worker: what registered facts collect from one file. Only files
    matching a fact's gate are parsed."""
    repo_root, abs_path = args
    try:
        source = abs_path.read_bytes()
    except OSError:
        return {}
    if not any(g in source for spec in facts_for("python") for g in spec.gate):
        return {}
    try:
        root = parse_source("python", source).root_node
    except ValueError:  # bounded-parse failure — skip this file only
        return {}
    return collect_additive("python", root, source, abs_path.relative_to(repo_root).as_posix(),
                            repo_root)


def build_python_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> PythonIndex:
    root = Path(repo_root)
    files, fact_files = split_fact_sources("python", list(files))
    py_files = [f for f in files if f.suffix == ".py"]
    # registered fact name → per-file values: fact-source files (another parser's), then our own
    collected: dict[str, list[Any]] = collect_fact_sources("python", fact_files, root, jobs)
    if facts_for("python"):
        for per_file_facts in parallel_map([(root, f) for f in py_files], _registered, jobs):
            for name, value in per_file_facts.items():
                collected.setdefault(name, []).append(value)
    index = PythonIndex()
    # Registered facts may need the whole index, so they resolve after it is built.
    return replace(index, facts=resolve_additive("python", collected, index))
