"""The Python repo index (``PythonParser.build_index``): FastAPI wiring that crosses files.

A FastAPI router is usually declared in one file (``router = APIRouter(prefix="/orders")`` in
``app/routers/orders.py``) and mounted in another (``app.include_router(orders.router,
prefix=…)`` in ``app/main.py``). The route address and its guards need both, so each file's
wiring facts (:mod:`.fastapi_facts`) are collected once per repository and composed here:

* every router or app variable gets the prefix its routes are served under, the dependency
  guards it inherits, and the outermost app/router it is reached from;
* a router included at more than one place, or in a cycle, keeps its own prefix (ambiguous);
* a prefix that exists but cannot be resolved makes the address unknown (``None``);
* a prefix given by a module-level string constant (``API_PREFIX = "/api/v1"``, in this file or
  imported) is resolved; a settings attribute (``settings.API_V1_PREFIX``) is not — the
  environment can override it.
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
from .fastapi_facts import FileFacts, PrefixSpec, file_facts, module_constants

#: (file, variable) of a router or app.
Node_ = tuple[str, str]
#: Resolved wiring of one router variable: (served prefix or None when unknown, inherited
#: dependency guards, the outermost (file, variable) it is reached from).
Wiring = tuple[str | None, tuple[str, ...], Node_]


@dataclass(frozen=True)
class PythonIndex:
    #: File → {router / app variable → its wiring} for every FastAPI router in the repo.
    fastapi_wiring: dict[str, dict[str, Wiring]] = field(default_factory=dict)
    #: File → {app variable → global middleware names}.
    fastapi_middleware: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    #: Facts registered with ``register_fact`` (fact name → fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)


def _facts(args: tuple[Path, Path]) -> tuple[str, FileFacts] | None:
    """Picklable per-file worker. Only files that mention FastAPI routers are parsed."""
    repo_root, abs_path = args
    try:
        source = abs_path.read_bytes()
    except OSError:
        return None
    if b"APIRouter" not in source and b"FastAPI" not in source and b"include_router" not in source:
        return None
    try:
        root = parse_source("python", source).root_node
    except ValueError:  # bounded-parse failure — skip this file only
        return None
    rel = abs_path.relative_to(repo_root).as_posix()
    return rel, file_facts(root, source, repo_root, rel)


def _constants(args: tuple[Path, str]) -> tuple[str, dict[str, str]]:
    repo_root, rel = args
    try:
        source = (repo_root / rel).read_bytes()
        root = parse_source("python", source).root_node
    except (OSError, ValueError):
        return rel, {}
    return rel, module_constants(root, source)


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
    index = _fastapi_index(root, py_files, jobs)
    # Registered facts may need the whole index, so they resolve after it is built.
    return replace(index, facts=resolve_additive("python", collected, index))


def _fastapi_index(root: Path, py_files: list[Path], jobs: int) -> PythonIndex:
    per_file = [r for r in parallel_map([(root, f) for f in py_files], _facts, jobs) if r]
    if not per_file:
        return PythonIndex()
    facts = dict(per_file)

    # Constants referenced by a prefix but declared in a file without FastAPI wiring.
    consts: dict[str, dict[str, str]] = {rel: f.constants for rel, f in facts.items()}
    wanted = sorted({s[1] for f in facts.values()
                     for s in [p for p, _ in f.routers.values()] + [inc[2] for inc in f.includes]
                     if s[0] == "const" and s[1] not in consts})
    consts.update(dict(parallel_map([(root, rel) for rel in wanted], _constants, jobs)))

    def value(spec: PrefixSpec) -> str | None:
        if spec[0] == "lit":
            return spec[1]
        if spec[0] == "const":
            return consts.get(spec[1], {}).get(spec[2])
        return None

    own: dict[Node_, tuple[str | None, tuple[str, ...]]] = {}
    for rel, f in facts.items():
        for var, (spec, deps) in f.routers.items():
            own[(rel, var)] = (value(spec), deps)
    sites: dict[Node_, set[tuple[Node_, str | None, tuple[str, ...]]]] = {}
    for rel, f in facts.items():
        for receiver, child, spec, deps in f.includes:
            target: Node_ = (rel, child[1]) if child[0] == "local" else (child[1], child[2])
            if target in own:
                sites.setdefault(target, set()).add(((rel, receiver), value(spec), deps))

    memo: dict[Node_, Wiring] = {}

    def wiring(node: Node_, seen: frozenset[Node_]) -> Wiring:
        if node in memo:
            return memo[node]
        prefix, deps = own.get(node, ("", ()))
        at = sites.get(node, set())
        if len(at) != 1 or node in seen:  # not included anywhere, ambiguous, or a cycle
            result: Wiring = (prefix, deps, node)
        else:
            (parent, mount, mount_deps), = at
            parent_base, parent_deps, top = wiring(parent, seen | {node})
            guards = parent_deps + mount_deps + deps
            if parent_base is None or mount is None or prefix is None:
                result = (None, guards, top)
            else:
                result = (f"{parent_base}{mount}{prefix}", guards, top)
        if not seen:
            memo[node] = result
        return result

    out: dict[str, dict[str, Wiring]] = {}
    for node in own:
        out.setdefault(node[0], {})[node[1]] = wiring(node, frozenset())
    return PythonIndex(
        fastapi_wiring=out,
        fastapi_middleware={rel: f.middleware for rel, f in facts.items() if f.middleware},
    )

