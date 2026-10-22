"""Leading an entry point to its handler (parsers/handler_sites.py, BaseParser.link_handlers):
the rules every language shares."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import BaseParser, HandlerLocator, ParseContext
from breezeai_cog.parsers.handler_sites import handler_location
from breezeai_cog.schemas import FileRecord, Statement


def test_handler_location_own_file_has_no_path() -> None:
    assert handler_location(("a.ts", 3), "a.ts") == (3, None)
    assert handler_location(("b.ts", 7), "a.ts") == (7, "b.ts")
    assert handler_location(None, "a.ts") == (None, None)


def _ctx(capture: bool = True) -> ParseContext:
    return ParseContext(path="a.ts", abs_path=Path("a.ts"), source=b"", repo_root=Path("."),
                        capture_statements=capture)


def _route(handler: str | None, line: int | None = None, sid: str = "a.ts:1:0") -> Statement:
    return Statement(id=sid, nodeType="call_expression", startLine=1, endLine=1, text="", parentId="f",
                     semanticType="route", handler=handler, handlerLine=line)


def _record(*statements: Statement) -> FileRecord:
    return FileRecord(id="f", path="a.ts", type="code", language="typescript", loc=1,
                      statements=list(statements))


class _Parser(BaseParser):
    name = "probe"
    language = "typescript"

    def __init__(self, sites: dict[str, tuple[str | None, tuple[str, int] | None]]) -> None:
        self.sites = sites
        self.asked = 0

    def handler_locator(self, ctx: ParseContext) -> HandlerLocator | None:
        self.asked += 1
        return self.sites.get


class _Plain(BaseParser):
    name = "plain"
    language = "typescript"


def test_default_parser_locates_nothing() -> None:
    record = _record(_route("Home"))
    _Plain().link_handlers(record, _ctx())
    assert (record.statements[0].handler, record.statements[0].handlerLine) == ("Home", None)


def test_loop_fills_line_and_path_only_for_another_file() -> None:
    parser = _Parser({"Home": ("Home", ("pages/Home.ts", 4)), "Local": ("Local", ("a.ts", 9)),
                      "./x": (None, None)})
    home, local, unknown, path_shaped = (_route("Home", sid="1"), _route("Local", sid="2"),
                                         _route("Gone", sid="3"), _route("./x", sid="4"))
    parser.link_handlers(_record(home, local, unknown, path_shaped), _ctx())
    assert (home.handlerLine, home.handlerPath) == (4, "pages/Home.ts")
    assert (local.handlerLine, local.handlerPath) == (9, None)
    assert (unknown.handler, unknown.handlerLine, unknown.handlerPath) == ("Gone", None, None)
    assert (path_shaped.handler, path_shaped.handlerLine) == (None, None)  # never a path


def test_locator_asked_only_when_a_record_needs_it() -> None:
    parser = _Parser({"Home": ("Home", ("b.ts", 1))})
    parser.link_handlers(_record(_route("Home", line=5)), _ctx())  # already located
    parser.link_handlers(_record(_route(None)), _ctx())  # names no handler
    parser.link_handlers(_record(_route("Home")), _ctx(capture=False))
    assert parser.asked == 0
    parser.link_handlers(_record(_route("Home")), _ctx())
    assert parser.asked == 1


def test_typescript_lookup_skips_a_vue_sfc() -> None:
    """A .vue SFC is not a script: the Vue parser (whose extensions add ``.vue``) must not
    re-parse the whole file as TypeScript to look a handler up."""
    from breezeai_cog.parsers.typescript_vue.parser import VueParser

    def ctx(path: str) -> ParseContext:
        return ParseContext(path=path, abs_path=Path(path), source=b"const a = 1;\n",
                            repo_root=Path("."), capture_statements=True)

    assert VueParser().handler_locator(ctx("src/App.vue")) is None
    assert VueParser().handler_locator(ctx("src/router.ts")) is not None
