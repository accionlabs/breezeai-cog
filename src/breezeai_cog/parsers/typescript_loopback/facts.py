"""LoopBack application base paths as a registered fact on the TypeScript index,
``loopback.base-paths``: the ``rest.basePath`` option or ``app.basePath(...)`` call of an
application, by the directory of the file that sets it (:mod:`..typescript.base_paths`, shared
with Next.js)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..typescript.base_paths import BasePathRaw, collect_base_path, resolve_base_paths

BASE_PATHS = "loopback.base-paths"


def loopback_base_paths(index: Any | None) -> dict[str, str | None]:
    return index_fact(index, BASE_PATHS) or {}


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> BasePathRaw | None:
    if b"@loopback" not in source or rel.rsplit("/", 1)[-1].startswith("next.config."):
        return None
    return collect_base_path(root, source, rel, "loopback")


register_fact(FactSpec(
    name=BASE_PATHS, language="typescript", gate=(b"basePath",),
    collect=_collect, resolve=resolve_base_paths,
))
