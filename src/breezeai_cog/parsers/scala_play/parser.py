"""``ScalaPlayParser`` — Play controller actions, layered over ``ScalaParser`` (reuses its
single-parse ``extract``; follows the pattern in ``java_springboot/parser.py``).

``PlayRoutesParser`` — ``conf/routes``, Play's line-based routing DSL. Not Scala source,
so it claims by path tail (not extension) and is its own base parser: nothing else
claims ``conf/routes``, so emitting ``type="config"`` keeps its parser name out of
``analyzedLanguages`` (``pipeline`` excludes ``type == "config"`` records there).
"""

from __future__ import annotations

from pathlib import Path

from ...emit import file_id
from ...schemas import FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..scala.parser import ScalaParser
from ..treesitter import parse_source
from .actions import detect_play_actions
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
            routes = detect_play_actions(root, ctx.source, record)
            if routes:
                record.statements.extend(routes)
                record.framework = "play"
        return record


class PlayRoutesParser(BaseParser):
    name = "scala-play-routes"
    priority = 0

    def matches(self, path: str | Path) -> bool:
        return Path(path).as_posix().endswith("conf/routes")

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        text = ctx.source.decode("utf-8", "replace")
        statements = extract_routes(text, ctx.path) if ctx.capture_statements else []
        return FileRecord(
            id=file_id(ctx.path),
            path=ctx.path,
            type="config",
            language="scala",
            loc=count_loc(text),
            statements=statements,
        )
