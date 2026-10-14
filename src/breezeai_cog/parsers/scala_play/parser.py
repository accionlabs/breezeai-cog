"""``ScalaPlayParser`` — Play controller actions, layered over ``ScalaParser`` (reuses its
single-parse ``extract``; follows the pattern in ``java_springboot/parser.py``).

``PlayRoutesParser`` — ``conf/routes`` and ``conf/<name>.routes``, Play's line-based routing
DSL. Not Scala source, so it claims by path (not extension) and is its own base parser: nothing
else claims these files, so emitting ``type="config"`` keeps its parser name out of
``analyzedLanguages`` (``pipeline`` excludes ``type == "config"`` records there). Being its own
base, its ``build_index`` pre-pass sees every routes file and resolves the ``->`` includes
(:mod:`.includes`), so a route is recorded under the prefix its router is served at.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...emit import file_id
from ...schemas import FileRecord
from ...utils import count_loc, repo_relative
from ..base import BaseParser, ParseContext
from ..emits import apply
from ..scala.parser import ScalaParser
from ..treesitter import parse_source
from .actions import detect_play_actions, detect_play_sird_routes
from .includes import (
    FACT, collect_includes, is_routes_file, resolve_includes, router_name, router_prefix,
)
from .routes import extract_routes


class ScalaPlayParser(ScalaParser):
    name = "scala-play"
    priority = 10
    frameworks = ["play"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"play.api" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("scala", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited Scala extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            sird_routes = detect_play_sird_routes(
                root, ctx.source, record, index=ctx.resolution_index,
            )
            apply(record, sird_routes, ctx.source, ctx.path)
            # Actions are handlers of conf/routes / SIRD bindings — they label the file, no record.
            if sird_routes or detect_play_actions(root, ctx.source, record):
                record.framework = "play"
        return record


@dataclass(frozen=True)
class PlayRoutesIndex:
    """The routes files' pre-pass: ``facts[FACT]`` is the resolved router includes."""

    facts: dict[str, Any] = field(default_factory=dict)


class PlayRoutesParser(BaseParser):
    name = "scala-play-routes"
    priority = 0

    def matches(self, path: str | Path) -> bool:
        return is_routes_file(path)

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> PlayRoutesIndex:
        values = []
        for f in files:
            try:
                found = collect_includes(f.read_bytes(), repo_relative(f, repo_root))
            except OSError:
                continue
            if found is not None:
                values.append(found)
        fact = resolve_includes(values)
        return PlayRoutesIndex({FACT: fact} if fact is not None else {})

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        text = ctx.source.decode("utf-8", "replace")
        prefix = router_prefix(ctx.resolution_index, router_name(ctx.path))
        statements = extract_routes(text, ctx.path, prefix) if ctx.capture_statements else []
        return FileRecord(
            id=file_id(ctx.path),
            path=ctx.path,
            type="config",
            language="scala",
            loc=count_loc(text),
            statements=statements,
        )
