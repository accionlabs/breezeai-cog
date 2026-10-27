"""jQuery AJAX ``api_call`` detection (detection/jquery_calls.py): positive forms, false-positive
guards, and inline ``<script>`` blocks in .cshtml/.html/.aspx through the real markup parsers."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from breezeai_cog import iter_file_records
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp_webforms.parser import WebFormsParser
from breezeai_cog.parsers.html.parser import HtmlParser
from breezeai_cog.parsers.razor.parser import RazorParser
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.schemas import FileRecord

FIXTURES = Path(__file__).parent / "fixtures" / "jquery_ajax"


def _parse(parser, name: str, *, capture: bool = True) -> FileRecord:
    p = FIXTURES / name
    src = p.read_bytes()
    ctx = ParseContext(
        path=name, abs_path=p, source=src, repo_root=FIXTURES, capture_statements=capture
    )
    return parser.parse_file(ctx)


def _jquery(rec: FileRecord) -> list[tuple[str | None, str | None]]:
    """(method, endpoint) of every jquery api_call — assertions target the framework, not
    'no api_call at all', so unrelated calls in a fixture cannot mask a result."""
    return [
        (s.method, s.endpoint)
        for s in rec.statements
        if s.semanticType == "api_call" and s.framework == "jquery"
    ]


def _js(name: str) -> list[tuple[str | None, str | None]]:
    return _jquery(_parse(TypeScriptParser(), name))


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("fixture_ajax_options.js", ("POST", "/api/data")),
        ("fixture_ajax_method_key.js", ("PUT", "/api/x")),
        ("fixture_ajax_default_verb.js", ("GET", "/api/y")),  # options-object exception, no import
        ("fixture_ajax_type_wins.js", ("DELETE", "/api/both")),  # `type` beats `method`
        ("fixture_get.js", ("GET", "/api/items")),
        ("fixture_post.js", ("POST", "/api/save")),
        ("fixture_getjson.js", ("GET", "/api/config")),
        ("fixture_getscript.js", ("GET", "/api/script.js")),
        ("fixture_load.js", ("GET", "/partial")),
        ("fixture_jquery_alias.js", ("GET", "/api/x")),  # `jQuery` receiver needs no import
        ("fixture_jquery_import.js", ("GET", "/api/required")),  # const $ = require('jquery')
        ("fixture_iife.js", ("POST", "/api/iife")),  # (function($){…})(jQuery)
    ],
)
def test_supported_form_is_one_jquery_api_call(fixture: str, expected: tuple[str, str]) -> None:
    assert _js(fixture) == [expected]  # exactly one record — no duplicate for a single call


def test_jquery_framework_labels_the_file() -> None:
    assert _parse(TypeScriptParser(), "fixture_get.js").framework == "jquery"


def test_unrelated_call_beside_a_jquery_call_is_not_tagged() -> None:
    rec = _parse(TypeScriptParser(), "fixture_mixed.js")
    assert _jquery(rec) == [("GET", "/api/real")]
    assert not any(s.endpoint == "/api/not-jquery" and s.framework == "jquery" for s in rec.statements)


def test_somelib_get_is_not_jquery() -> None:
    assert _js("fixture_neg_somelib.js") == []


def test_custom_dollar_is_not_jquery() -> None:
    # `const $ = customLibrary` rebinds `$`: nothing for it is jQuery — not even the
    # options-object exception, because the receiver is no longer an unknown global.
    assert _js("fixture_neg_custom_dollar.js") == []


def test_dollar_without_evidence_is_not_jquery_except_ajax_options() -> None:
    # No import / script ref: get, post, .load and a non-literal $.ajax stay unclassified.
    assert _js("fixture_neg_no_evidence.js") == []


def test_non_selection_load_is_not_jquery() -> None:
    # jquery IS imported here, yet loader.load / module.load / $.load (no selection) are not requests.
    assert _js("fixture_neg_load.js") == []


def test_generic_detection_is_unchanged_for_http_clients(tmp_path) -> None:
    # axios keeps its generic classification (no framework from this detector); a bare `$.get`
    # with no jQuery evidence is not promoted to an api_call by the generic classifier either.
    src = b"import axios from 'axios';\naxios.get('/api/a');\n$.get('/api/b');\n"
    p = tmp_path / "a.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="a.ts", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    api = [s for s in rec.statements if s.semanticType == "api_call"]
    assert [(s.startLine, s.method, s.framework) for s in api] == [(2, "GET", None)]
    assert _jquery(rec) == []


# --- inline <script> blocks through the real markup parsers (template capture on) ----------

@pytest.mark.parametrize(
    ("parser", "fixture", "expected"),
    [
        (RazorParser, "fixture_inline_script.cshtml",
         [("GET", "/api/orders"), ("POST", "/api/orders/save")]),
        (HtmlParser, "fixture_inline_script.html", [("GET", "/api/results")]),
        (WebFormsParser, "fixture_inline_script.aspx", [("GET", "/api/config")]),
    ],
)
def test_inline_script_in_markup(parser, fixture: str, expected: list[tuple[str, str]]) -> None:
    rec = _parse(parser(), fixture)
    assert _jquery(rec) == expected  # each call once, none duplicated
    assert all(s.startLine >= 1 for s in rec.statements if s.framework == "jquery")


def test_inline_script_lines_point_into_the_markup_file() -> None:
    rec = _parse(RazorParser(), "fixture_inline_script.cshtml")
    lines = {s.endpoint: s.startLine for s in rec.statements if s.framework == "jquery"}
    assert lines == {"/api/orders": 5, "/api/orders/save": 6}


def test_markup_without_statement_capture_emits_nothing() -> None:
    assert _jquery(_parse(RazorParser(), "fixture_inline_script.cshtml", capture=False)) == []


def test_dynamic_url_and_verb_are_honest_null(tmp_path) -> None:
    src = b"import $ from 'jquery';\n$.get(base + '/x');\n$.ajax({ url: u, type: verb });\n"
    p = tmp_path / "d.js"
    p.write_bytes(src)
    ctx = ParseContext(path="d.js", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True)
    # nothing is invented: the endpoint / verb that cannot be read statically stay null
    assert _jquery(TypeScriptParser().parse_file(ctx)) == [("GET", None), (None, None)]


def test_non_javascript_script_block_is_ignored(tmp_path) -> None:
    src = (b'<script src="/jquery.js"></script>\n'
           b'<script type="text/template">$.get("/api/tpl");</script>\n')
    p = tmp_path / "t.html"
    p.write_bytes(src)
    ctx = ParseContext(path="t.html", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True)
    assert _jquery(HtmlParser().parse_file(ctx)) == []



# --- review regressions: each of these was a real false positive / wrong value ---------------

def _src(tmp_path, src: str, name: str = "a.js", parser=None) -> list[tuple[str | None, str | None]]:
    p = tmp_path / name
    b = src.encode()
    p.write_bytes(b)
    ctx = ParseContext(path=name, abs_path=p, source=b, repo_root=tmp_path, capture_statements=True)
    return _jquery((parser or TypeScriptParser()).parse_file(ctx))


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        # the no-evidence exception is an options OBJECT with a literal url — not a bare string
        ('$.ajax("/api/x");', []),
        ('$.ajax(buildOptions());', []),
        ('$.ajax({ url: base + "/x" });', []),
        # a string url + options object is fine once jQuery is evidenced, and reads the options
        ('import $ from "jquery";\n$.ajax("/api/data", { type: "POST" });', [("POST", "/api/data")]),
        # `const $ = jQuery` is an alias of jQuery
        ('const $ = jQuery;\n$.get("/api/alias");', [("GET", "/api/alias")]),
        # $(window).load(fn) is the legacy load EVENT, not a request; a non-literal url is not static
        ('import $ from "jquery";\n$(window).load(function () { init(); });', []),
        ('import $ from "jquery";\n$("#a").load(url);', []),
        # a lone arrow parameter `$` shadows the global
        ('import jq from "jquery";\nconst f = $ => $.get("/api/arrow");', []),
        # "jquery.json" is not a jQuery script reference
        ('var s = "jquery.json";\n$.get("/api/z");', []),
        # a jQuery file that also rebinds `$` to something else: untrusted (conservative)
        ('import $ from "jquery";\nfunction f() { const $ = custom; $.get("/a"); }\n$.get("/b");', []),
        # the options-object exception never overrides an explicit custom `$`
        ('const $ = customLibrary;\n$.ajax({ url: "/api/data" });', []),
        # a wrapper IIFE fed something else is not jQuery
        ('(function ($) { $.get("/api/x"); })(customLib);', []),
        # method precedence: `type` wins over `method`, in either property order
        ('$.ajax({ url: "/api/d", type: "POST", method: "PUT" });', [("POST", "/api/d")]),
        ('$.ajax({ url: "/api/d", method: "PUT", type: "POST" });', [("POST", "/api/d")]),
        # a dynamic endpoint is null, never the expression text
        ('import $ from "jquery";\n$.get(`/api/${id}`);', [("GET", None)]),
    ],
)
def test_recognition_edges(tmp_path, src: str, expected) -> None:
    assert _src(tmp_path, src) == expected


def test_razor_expression_url_is_null_not_a_literal(tmp_path) -> None:
    src = ('<script src="/lib/jquery.min.js"></script>\n<script>\n'
           '$.get(\'@Url.Action("Index", "Home")\');\n</script>\n')
    assert _src(tmp_path, src, "v.cshtml", RazorParser()) == [("GET", None)]


@pytest.mark.parametrize("parser", [RazorParser, HtmlParser, WebFormsParser])
def test_markup_external_script_and_no_capture_emit_nothing(tmp_path, parser) -> None:
    ext = {RazorParser: "cshtml", HtmlParser: "html", WebFormsParser: "aspx"}[parser]
    name = f"p.{ext}"
    p = tmp_path / name
    src = b'<script src="/jquery.js"></script>\n<script src="/app.js">$.get("/api/ignored");</script>\n'
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path, capture_statements=True)
    assert _jquery(parser().parse_file(ctx)) == []  # a <script src> body is not inline code
    inline = b'<script src="/jquery.js"></script>\n<script>$.get("/api/in");</script>\n'
    p.write_bytes(inline)
    off = ParseContext(path=name, abs_path=p, source=inline, repo_root=tmp_path, capture_statements=False)
    assert _jquery(parser().parse_file(off)) == []  # statements are only captured on request


def test_template_capture_pipeline_end_to_end(tmp_path) -> None:
    """The real walk: markup is skipped without --capture-templates, and with it each inline
    jQuery call is exactly one api_call."""
    for f in ("fixture_inline_script.cshtml", "fixture_inline_script.html", "fixture_inline_script.aspx"):
        shutil.copy(FIXTURES / f, tmp_path / f)
    assert list(iter_file_records(tmp_path)) == []  # templates off → markup not parsed at all
    got = {r.path: _jquery(r) for r in iter_file_records(tmp_path, capture_templates=True)}
    assert got == {
        "fixture_inline_script.cshtml": [("GET", "/api/orders"), ("POST", "/api/orders/save")],
        "fixture_inline_script.html": [("GET", "/api/results")],
        "fixture_inline_script.aspx": [("GET", "/api/config")],
    }
