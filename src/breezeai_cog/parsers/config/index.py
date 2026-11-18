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
    """Every ``config`` fact's value for one file (picklable worker). A file that cannot be read
    contributes nothing."""
    repo_root_s, file_s = args
    repo_root, path = Path(repo_root_s), Path(file_s)
    try:
        source = path.read_bytes()
    except OSError:
        return {}
    return collect_additive("config", None, source, path.relative_to(repo_root).as_posix(),
                            repo_root)


def build_config_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> ConfigIndex | None:
    """The facts registered for ``config`` over its files; None when there are none to build
    or none found. With no fact registered no file is read."""
    if not facts_for("config"):
        return None
    collected: dict[str, list[Any]] = {}
    args = [(str(repo_root), str(f)) for f in files]
    for per_file in parallel_map(args, _file_facts, jobs):
        for name, value in per_file.items():
            collected.setdefault(name, []).append(value)
    facts = resolve_additive("config", collected, None)
    return ConfigIndex(facts) if facts else None
