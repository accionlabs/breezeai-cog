"""The shared page-script reader (``parsers/template_common/scripts.py``): a template page's
inline ``<script>`` blocks read as one program by every script detector."""

from __future__ import annotations

import re
from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.template_common.script_blocks import script_shadow
from breezeai_cog.parsers.template_common.scripts import read_page_scripts
from breezeai_cog.schemas import FileRecord, Statement


def _read(page: str, path: str = "page.html", *, server: list[str] = (), capture: bool = True) -> FileRecord:
    """Run the reader over ``page``; ``server`` lists the server-code snippets whose spans the
    caller (the page's own parser) would pass."""
    source = page.encode()
    spans = [(m.start(), m.end()) for snippet in server
             for m in re.finditer(re.escape(snippet.encode()), source)]
    record = FileRecord(id=path, path=path, type="code", language="html", loc=page.count("\n") + 1)
    ctx = ParseContext(path=path, abs_path=Path(path), source=source, repo_root=Path("."),
                       capture_statements=capture)
    read_page_scripts(record, ctx, server_code=spans)
    return record


def _calls(record: FileRecord) -> list[tuple[int, str | None, str | None, str | None]]:
    return sorted((s.startLine, s.framework, s.method, s.endpoint)
                  for s in record.statements if s.semanticType == "api_call")


def _by_line(record: FileRecord, line: int) -> Statement:
    (stmt,) = [s for s in record.statements if s.startLine == line]
    return stmt


PAGE = """<html>
<script src="/js/jquery.min.js"></script>
<script>
  var $j = jQuery;
  function load() { fetch('/api/stock'); }
</script>
<p>Orders</p>
<script>
  $.get('/shop/api/orders');
  axios.post('/api/c', { a: 1 });
</script>
<script type="text/template"><div>{{ fetch('/api/template') }}</div></script>
<!-- <script>fetch('/api/commented')</script> -->
"""


def test_every_detector_runs_over_every_script_block() -> None:
    assert _calls(_read(PAGE)) == [
        (5, None, "GET", "/api/stock"),
        (9, "jquery", "GET", "/shop/api/orders"),
        (10, None, "POST", "/api/c"),
    ]


def test_lines_point_into_the_page() -> None:
    lines = PAGE.splitlines()
    for stmt in _read(PAGE).statements:
        assert stmt.text.strip() in lines[stmt.startLine - 1]


def test_a_call_inside_a_script_function_hangs_off_the_page() -> None:
    record = _read(PAGE)
    assert record.functions == []
    assert {s.parentId for s in record.statements} == {"page.html"}


def test_only_detected_statements_are_kept() -> None:
    record = _read("<script>\n// load the stock\nvar n = 1;\nfetch('/api/stock');\n</script>")
    assert [(s.startLine, s.semanticType) for s in record.statements] == [(4, "api_call")]


def test_one_call_is_one_statement() -> None:
    record = _read('<script src="jquery.js"></script>\n<script>\njQuery.get("/a");\n</script>')
    assert _calls(record) == [(3, "jquery", "GET", "/a")]


def test_a_page_without_scripts_is_unchanged() -> None:
    assert _read("<p>no scripts</p>\n<script src='app.js'></script>").statements == []


def test_nothing_without_statement_capture() -> None:
    assert _read(PAGE, capture=False).statements == []


def test_server_code_inside_a_url_leaves_the_endpoint_empty() -> None:
    page = "<script>\nfetch('/api/user/<%= id %>');\nfetch('/api/ok');\n</script>"
    record = _read(page, "page.jsp", server=["<%= id %>"])
    assert _calls(record) == [(2, None, "GET", None), (3, None, "GET", "/api/ok")]
    assert _by_line(record, 2).text == "fetch('/api/user/<%= id %>');"


def test_server_code_is_not_read_as_javascript() -> None:
    page = '<script>\n<% http.post("/api/server"); %>\nfetch("/api/browser");\n</script>'
    record = _read(page, "page.jsp", server=['<% http.post("/api/server"); %>'])
    assert _calls(record) == [(3, None, "GET", "/api/browser")]


def test_a_server_expression_the_caller_did_not_pass_still_empties_the_endpoint() -> None:
    record = _read("<script>\nfetch('/api/<%= id %>');\n</script>", "page.aspx")
    assert _calls(record) == [(2, None, "GET", None)]


def test_server_expressions_follow_the_page_dialect() -> None:
    twig = _read("<script>\nfetch('/api/{{ id }}');\n</script>", "page.html.twig")
    razor = _read("<script>\nfetch('/api/@Model.Id');\n</script>", "page.cshtml")
    html = _read("<script>\nfetch('/api/user@host');\n</script>", "page.html")
    assert _calls(twig) == [(2, None, "GET", None)]
    assert _calls(razor) == [(2, None, "GET", None)]
    assert _calls(html) == [(2, None, "GET", "/api/user@host")]


def test_shadow_keeps_offsets_and_blanks_everything_else() -> None:
    page = b"<p>x</p>\n<script>var a = 1;</script>\n<script src='x.js'></script>"
    shadow, _ = script_shadow(page)
    assert len(shadow) == len(page)
    assert shadow.split(b"\n")[1].strip() == b"var a = 1;"
    assert shadow.replace(b"var a = 1;", b"").strip() == b""


def test_a_jsp_comment_hides_its_script() -> None:
    page = b"<%-- <script>fetch('/a')</script> --%>\n<script>fetch('/b')</script>"
    assert script_shadow(page, "page.jsp")[0].count(b"fetch") == 1
