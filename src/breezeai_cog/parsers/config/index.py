"""The ``config`` repo index: the registered facts read from configuration files.

Configuration has no symbols to index, so the index holds facts only — data a framework parser
of a configuration file needs from other configuration files (a Struts configuration's action
extension comes from every Struts file of its module, ``struts.properties`` and ``web.xml``).
Built once before parsing like every index, so no parser opens another file to get it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..additive import collect_additive, facts_for, resolve_additive
from ..index_common import parallel_map


@dataclass(frozen=True, slots=True)
class ConfigIndex:
    """Fact name → fact (:func:`~..additive.index_fact`)."""

    facts: dict[str, Any] = field(default_factory=dict)


def _file_facts(args: tuple[str, str]) -> dict[str, Any]:
    """Every ``config`` fact's value for one file (picklable worker). A file that cannot be read,
    or that a fact fails on, contributes nothing: one bad file never takes the index down."""
    repo_root_s, file_s = args
    repo_root, path = Path(repo_root_s), Path(file_s)
    rel = path.relative_to(repo_root).as_posix()
    try:
        source = path.read_bytes()
    except OSError:
        return {}
    try:
        return collect_additive("config", None, source, rel, repo_root)
    except Exception as exc:
        from ...logging import get_logger

        get_logger("breezeai_cog.index").warning("index.fact.skipped", path=rel, error=str(exc))
        return {}


def build_config_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> ConfigIndex | None:
    """The facts registered for ``config`` over its files; None only when no fact is registered
    (then no file is read). An index that found nothing is still built, so a parser can tell
    "no setting anywhere" from "no index". Only the file types a fact reads are read
    (``FactSpec.sources``; a fact that names none reads every config file)."""
    specs = facts_for("config")
    if not specs:
        return None
    if all(spec.sources for spec in specs):
        exts = tuple({ext for spec in specs for ext in spec.sources})
        files = [f for f in files if f.name.lower().endswith(exts)]
    collected: dict[str, list[Any]] = {}
    args = [(str(repo_root), str(f)) for f in files]
    for per_file in parallel_map(args, _file_facts, jobs):
        for name, value in per_file.items():
            collected.setdefault(name, []).append(value)
    return ConfigIndex(resolve_additive("config", collected, None))
