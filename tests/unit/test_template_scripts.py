"""The shared page-script reader (``parsers/template_common/scripts.py``): a template page's
inline ``<script>`` blocks read as one program by every script detector."""

from __future__ import annotations

import re
from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.template_common.script_blocks import script_shadow
from breezeai_cog.parsers.template_common.script_blocks import script_programs
from breezeai_cog.parsers.template_common.scripts import link_page_scripts, read_page_scripts
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
        (5, "fetch", "GET", "/api/stock"),
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
    assert _calls(record) == [(2, "fetch", "GET", None), (3, "fetch", "GET", "/api/ok")]
    assert _by_line(record, 2).text == "fetch('/api/user/<%= id %>');"


def test_server_code_is_not_read_as_javascript() -> None:
    page = '<script>\n<% http.post("/api/server"); %>\nfetch("/api/browser");\n</script>'
    record = _read(page, "page.jsp", server=['<% http.post("/api/server"); %>'])
    assert _calls(record) == [(3, "fetch", "GET", "/api/browser")]


def test_a_server_expression_the_caller_did_not_pass_still_empties_the_endpoint() -> None:
    record = _read("<script>\nfetch('/api/<%= id %>');\n</script>", "page.aspx")
    assert _calls(record) == [(2, "fetch", "GET", None)]


def test_server_expressions_follow_the_page_dialect() -> None:
    twig = _read("<script>\nfetch('/api/{{ id }}');\n</script>", "page.html.twig")
    razor = _read("<script>\nfetch('/api/@Model.Id');\n</script>", "page.cshtml")
    html = _read("<script>\nfetch('/api/user@host');\n</script>", "page.html")
    assert _calls(twig) == [(2, "fetch", "GET", None)]
    assert _calls(razor) == [(2, "fetch", "GET", None)]
    assert _calls(html) == [(2, "fetch", "GET", "/api/user@host")]


def test_a_template_value_in_an_html_page_empties_the_endpoint() -> None:
    # Django / Jinja / Handlebars render {{ }} and {% %} before the browser runs the script, so
    # the URL is only known on the server; a plain URL on the next line is kept.
    page = ("<script>\nfetch('/api/orders/{{ order.id }}');\n"
            "fetch('/api/{% url_for x %}');\nfetch('/api/static');\n</script>")
    assert _calls(_read(page, "orders.html")) == [
        (2, "fetch", "GET", None), (3, "fetch", "GET", None), (4, "fetch", "GET", "/api/static"),
    ]
    record = _read(page, "orders.html")
    assert _by_line(record, 2).text == "fetch('/api/orders/{{ order.id }}');"


def test_shadow_keeps_offsets_and_blanks_everything_else() -> None:
    page = b"<p>x</p>\n<script>var a = 1;</script>\n<script src='x.js'></script>"
    shadow, _ = script_shadow(page)
    assert len(shadow) == len(page)
    assert shadow.split(b"\n")[1].strip() == b"var a = 1;"
    assert shadow.replace(b"var a = 1;", b"").strip() == b""


def test_a_jsp_comment_hides_its_script() -> None:
    page = b"<%-- <script>fetch('/a')</script> --%>\n<script>fetch('/b')</script>"
    assert script_shadow(page, "page.jsp")[0].count(b"fetch") == 1


# ── page constants ───────────────────────────────────────────────────────────────────────────

JQ = '<script src="/js/jquery.min.js"></script>\n'


def test_a_constant_from_another_block_gives_the_endpoint() -> None:
    page = JQ + "<script>var API = '/api/orders';</script>\n<script>$.post(API);</script>"
    assert _calls(_read(page)) == [(3, "jquery", "POST", "/api/orders")]


def test_constants_reach_fetch_axios_and_settings_objects() -> None:
    page = JQ + (
        "<script>\nconst U = '/api/u';\nlet S = \"/api/s\";\n"
        "fetch(U).then(r => r.json());\naxios.get(S);\n"
        "axios({ url: U, method: 'post' });\n$.ajax({ url: S });\n</script>"
    )
    assert [c[3] for c in _calls(_read(page))] == ["/api/u", "/api/s", "/api/u", "/api/s"]


def test_a_name_that_is_not_a_page_constant_stays_a_runtime_value() -> None:
    page = (
        "<script>\n"
        "var A = '/a'; A = '/a2';\n"           # reassigned
        "var B = '/b'; var B = '/b2';\n"       # declared twice
        "var C = '/c'; function f(C) { fetch(C); }\n"  # a parameter of that name
        "function g() { var D = '/d'; fetch(D); }\n"   # not at the top of the program
        "var E = base + '/e';\n"                # not a plain string
        "var F = '/f'; F++;\n"
        "fetch(A);\nfetch(B);\nfetch(E);\nfetch(F);\n"
        "</script>"
    )
    # each stays the name of a value read at runtime, never the string it was first given
    assert [c[3] for c in _calls(_read(page))] == ["{C}", "{D}", "{A}", "{B}", "{E}", "{F}"]


def test_a_call_passed_as_an_argument_is_not_the_url() -> None:
    page = "<script>\nvar K = '/k';\nfetch(cache.get(K));\n</script>"
    assert _calls(_read(page)) == [(3, "fetch", "GET", "{param}")]  # not '/k'


def test_a_module_block_has_its_own_scope() -> None:
    # A module's constant is not seen by the classic blocks. A module could read a classic
    # block's global var, but each program is read on its own, so that stays empty.
    page = (
        "<script type=\"module\">const M = '/api/m'; fetch(M);</script>\n"
        "<script>fetch(M);</script>\n"
        "<script>var C = '/api/c';</script>\n"
        "<script type=\"module\">fetch(C);</script>"
    )
    assert _calls(_read(page)) == [
        (1, "fetch", "GET", "/api/m"), (2, "fetch", "GET", "{M}"), (4, "fetch", "GET", "{C}"),
    ]


def test_a_module_block_is_a_program_of_its_own() -> None:
    page = b"<script>a()</script><script type=module>b()</script><script>c()</script>"
    classic, module = script_programs(page)
    assert b"a()" in classic and b"c()" in classic and b"b()" not in classic
    assert b"b()" in module and b"a()" not in module


def test_a_constant_holding_server_code_is_not_folded() -> None:
    page = "<script>\nvar API = '/api/<%= id %>';\nfetch(API);\n</script>"
    assert _calls(_read(page, "page.aspx", server=["<%= id %>"])) == [(3, "fetch", "GET", "{API}")]
    twig = "<script>\nvar API = '/api/{{ id }}';\nfetch(API);\n</script>"
    assert _calls(_read(twig, "page.html.twig")) == [(3, "fetch", "GET", "{API}")]


# ── script files ─────────────────────────────────────────────────────────────────────────────


def _linked(tmp_path: Path, files: list[str], page_path: str, page: str) -> list[str]:
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x();")
    record = FileRecord(id=page_path, path=page_path, type="code", language="html", loc=1)
    ctx = ParseContext(path=page_path, abs_path=tmp_path / page_path, source=page.encode(),
                       repo_root=tmp_path)
    link_page_scripts(record, ctx)
    return record.importFiles


def test_a_script_file_of_the_repository_is_a_file_link(tmp_path: Path) -> None:
    page = ('<script src="js/app.js"></script>\n<script src="../lib/util.js?v=2"></script>\n'
            '<script src="https://cdn.x/jq.js"></script><script src="//cdn/x.js"></script>\n'
            '<script src="@Url.Content("~/js/a.js")"></script><script src="missing.js"></script>\n'
            '<!-- <script src="js/old.js"></script> -->')
    files = ["web/js/app.js", "lib/util.js", "web/js/old.js"]
    assert _linked(tmp_path, files, "web/index.html", page) == ["web/js/app.js", "lib/util.js"]


def test_a_rooted_script_path_links_only_when_one_file_matches(tmp_path: Path) -> None:
    page = '<script src="/js/app.js"></script>'
    assert _linked(tmp_path, ["public/js/app.js"], "public/views/index.html", page) == [
        "public/js/app.js"
    ]


def test_a_rooted_script_path_with_two_matches_gets_no_link(tmp_path: Path) -> None:
    page = '<script src="/js/app.js"></script>'
    files = ["site/js/app.js", "js/app.js"]
    assert _linked(tmp_path, files, "site/index.html", page) == []


def test_a_script_path_outside_the_repository_gets_no_link(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (tmp_path / "x.js").write_text("x();")
    repo.mkdir()
    assert _linked(repo, [], "index.html", '<script src="../x.js"></script>') == []
