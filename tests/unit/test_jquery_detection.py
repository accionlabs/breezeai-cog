"""jQuery AJAX ``api_call`` detection (detection/jquery_calls.py): positive forms, false-positive
guards, and inline ``<script>`` blocks in .cshtml/.html/.aspx through the real markup parsers."""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from breezeai_cog import iter_file_records
from breezeai_cog.cli import app
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
        ('$.ajax({ timeout: 5 });', []),  # an options object with no `url` is not enough
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


# --- AJAX / .load forms: methods, dynamic URLs, event-vs-request ---------------------------

_JQ = 'import $ from "jquery";\n'


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        # literal / variable / member-expression URLs; unresolved ones are null, never the text
        (_JQ + '$.ajax({ url: "/api/a", type: "GET" });', [("GET", "/api/a")]),
        (_JQ + '$.ajax({ url: target, type: "POST" });', [("POST", None)]),
        (_JQ + '$.ajax({ url: form.action, type: "POST" });', [("POST", None)]),
        (_JQ + '$.ajax({ url: "/api/" + id, method: "GET" });', [("GET", None)]),
        # every verb, from `type` and from `method`, any case
        (_JQ + '$.ajax({ url: "/v", type: "put" });', [("PUT", "/v")]),
        (_JQ + '$.ajax({ url: "/v", method: "PATCH" });', [("PATCH", "/v")]),
        (_JQ + '$.ajax({ url: "/v", method: "DELETE" });', [("DELETE", "/v")]),
        (_JQ + '$.ajax({ url: "/v", type: "POST" });', [("POST", "/v")]),
        # omitted verb on a literal options object = jQuery's default GET
        (_JQ + '$.ajax({ url: target });', [("GET", None)]),
        # an unreadable verb is null: a dynamic value, or a spread that may carry it
        (_JQ + '$.ajax({ url: "/v", type: verb });', [(None, "/v")]),
        (_JQ + '$.ajax({ ...defaults, url: "/v" });', [(None, "/v")]),
        # settings that are not visible are not guessed (no GET invented, no record)
        (_JQ + '$.ajax(settings);', []),
        (_JQ + '$.ajax($.extend({}, base, { url: "/v" }));', []),
        # shorthand helpers keep their fixed verbs
        (_JQ + '$.get(target);', [("GET", None)]),
        (_JQ + '$.post(target, data);', [("POST", None)]),
        (_JQ + '$.getJSON(`/api/${id}`);', [("GET", None)]),
        # .load: GET, literal or built URL; the legacy load EVENT and non-selections are not requests
        (_JQ + '$("#c").load("/Products/Details");', [("GET", "/Products/Details")]),
        (_JQ + '$("#c").load("/p/" + id);', [("GET", None)]),
        (_JQ + '$("#c").load(`/p/${id}`, cb);', [("GET", None)]),
        (_JQ + '$("#c").load("?handler=Partial");', [("GET", "?handler=Partial")]),
        (_JQ + '$(window).load(function () {});', []),
        (_JQ + '$(window).load(init);', []),
        (_JQ + '$(document).load("/x");', []),
        (_JQ + '$("img").load(onLoaded);', []),
        (_JQ + '$("#c").load();', []),
        # unrelated `.load` objects, even with jQuery imported
        (_JQ + 'player.load("/media/x.mp3");\nmodule.load("./x");\ngetLoader().load("/x");', []),
    ],
)
def test_ajax_and_load_forms(tmp_path, src: str, expected) -> None:
    assert _src(tmp_path, src) == expected


# --- Razor / template capture through the real pipeline ------------------------------------

_LAYOUT = (
    '<body>@RenderBody()\n<script src="~/lib/jquery/dist/jquery.min.js"></script>\n'
    '<script src="~/js/site.js" asp-append-version="true"></script>\n'
    '@RenderSection("Scripts", required: false)</body>\n'
)
_PAGE = '@page\n<div id="c"></div>\n@section Scripts {\n<script>\n  $("#c").load("?handler=Rows");\n</script>\n}\n'
_SITE_JS = 'function go(form) {\n  $.ajax({ type: "POST", url: form.action });\n}\n'


def _repo(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def _scan(root: Path, *, templates: bool) -> dict[str, list[tuple[int, str | None, str | None]]]:
    got: dict[str, list[tuple[int, str | None, str | None]]] = {}
    for rec in iter_file_records(root, capture_templates=templates):
        calls = [
            (s.startLine, s.method, s.endpoint)
            for s in rec.statements
            if s.semanticType == "api_call" and s.framework == "jquery"
        ]
        if calls:
            got[rec.path] = calls
    return got


def test_layout_evidence_reaches_page_inline_script_and_external_script(tmp_path) -> None:
    root = _repo(tmp_path, {
        "App/Pages/_ViewStart.cshtml": '@{ Layout = "_Layout"; }\n',
        "App/Pages/Shared/_Layout.cshtml": _LAYOUT,
        "App/Pages/Index.cshtml": _PAGE,
        "App/wwwroot/js/site.js": _SITE_JS,
    })
    assert _scan(root, templates=True) == {
        "App/Pages/Index.cshtml": [(5, "GET", "?handler=Rows")],  # line inside the original file
        "App/wwwroot/js/site.js": [(2, "POST", None)],  # form.action → unresolved, not invented
    }


def test_template_capture_off_skips_markup_and_its_evidence(tmp_path) -> None:
    root = _repo(tmp_path, {
        "Pages/_ViewStart.cshtml": '@{ Layout = "_Layout"; }\n',
        "Pages/Shared/_Layout.cshtml": _LAYOUT + '<script src="~/js/helper.js"></script>\n',
        "Pages/Index.cshtml": _PAGE,
        "wwwroot/js/site.js": _SITE_JS,
        "wwwroot/js/helper.js": '$.post(form.action, data);\n',
    })
    # default: markup is not read. Razor's `.load` is absent and the layout's evidence is
    # unavailable, so an evidence-dependent `$.post` stays unclassified; the `$.ajax({ url: … })`
    # options object is recognised from its own shape (dynamic url → null endpoint).
    assert _scan(root, templates=False) == {"wwwroot/js/site.js": [(2, "POST", None)]}
    # with template capture the layout vouches for helper.js too, and the page's `.load` appears
    assert _scan(root, templates=True) == {
        "Pages/Index.cshtml": [(5, "GET", "?handler=Rows")],
        "wwwroot/js/helper.js": [(1, "POST", None)],
        "wwwroot/js/site.js": [(2, "POST", None)],
    }


def test_explicit_layout_and_partial_views(tmp_path) -> None:
    root = _repo(tmp_path, {
        "Views/Shared/_Main.cshtml": _LAYOUT,
        "Views/Shared/_Other.cshtml": "<body>@RenderBody()</body>\n",
        "Views/_ViewStart.cshtml": '@{ Layout = "_Other"; }\n',
        # the view names its own layout, overriding the _ViewStart that has no jQuery
        "Views/Home/Page.cshtml": '@{ Layout = "_Main"; }\n<script>\n$.get("/api/p");\n</script>\n',
        # a partial: no layout of its own, resolved through _ViewStart (→ _Other: no jQuery)
        "Views/Home/_Part.cshtml": '<script>\n$.get("/api/part");\n</script>\n',
        # Layout = null opts out of the layout entirely
        "Views/Home/Bare.cshtml": '@{ Layout = null; }\n<script>\n$.get("/api/bare");\n</script>\n',
    })
    assert _scan(root, templates=True) == {"Views/Home/Page.cshtml": [(3, "GET", "/api/p")]}


def test_partial_inherits_view_start_layout(tmp_path) -> None:
    root = _repo(tmp_path, {
        "Views/Shared/_Layout.cshtml": _LAYOUT,
        "Views/_ViewStart.cshtml": '@{ Layout = "_Layout"; }\n',
        "Views/Home/_Part.cshtml": '<script>\n$.post("/api/part", d);\n</script>\n',
    })
    assert _scan(root, templates=True) == {"Views/Home/_Part.cshtml": [(2, "POST", "/api/part")]}


def test_no_layout_jquery_means_no_evidence(tmp_path) -> None:
    root = _repo(tmp_path, {
        "Pages/_ViewStart.cshtml": '@{ Layout = "_Layout"; }\n',
        "Pages/Shared/_Layout.cshtml": '<body>@RenderBody()<script src="~/js/site.js"></script></body>\n',
        "Pages/Index.cshtml": _PAGE,
        "wwwroot/js/site.js": '$.post(form.action, data);\n',  # listed by the layout, no jQuery loaded
    })
    assert _scan(root, templates=True) == {}


def test_external_script_needs_to_be_loaded_after_jquery(tmp_path) -> None:
    before = ('<script src="~/js/early.js"></script>\n<script src="~/lib/jquery.js"></script>\n'
              '<script src="~/js/late.js"></script>\n')
    root = _repo(tmp_path, {
        "Pages/Shared/_Layout.cshtml": before,
        "wwwroot/js/early.js": '$.get(a);\n',
        "wwwroot/js/late.js": '$.get(a);\n',
        "wwwroot/js/unlisted.js": '$.get(a);\n',
    })
    assert _scan(root, templates=True) == {"wwwroot/js/late.js": [(1, "GET", None)]}


def test_external_script_with_its_own_dollar_is_not_promoted(tmp_path) -> None:
    root = _repo(tmp_path, {
        "Shared/_Layout.cshtml": _LAYOUT,
        "js/site.js": 'const $ = customLibrary;\n$.ajax({ type: "POST", url: form.action });\n',
    })
    assert _scan(root, templates=True) == {}  # layout loads jQuery, but this file's `$` is not it


def test_vendor_style_ajax_wrapper_is_not_an_application_route(tmp_path) -> None:
    # a plugin referenced by the layout that forwards a merged settings object: not statically a request
    plugin = '(function ($) {\n  $.ajax($.extend(true, { mode: "abort" }, param));\n})(jQuery);\n'
    root = _repo(tmp_path, {
        "Shared/_Layout.cshtml": _LAYOUT.replace("js/site.js", "lib/plugin.js"),
        "lib/plugin.js": plugin,
    })
    assert _scan(root, templates=True) == {}


def test_inline_requests_are_not_duplicated_and_keep_distinct_locations(tmp_path) -> None:
    page = ('<script>\n$(function () {\n  $("#a").load("/x");\n  $("#b").load("/x");\n});\n</script>\n')
    root = _repo(tmp_path, {
        "Pages/_ViewStart.cshtml": '@{ Layout = "_Layout"; }\n',
        "Pages/Shared/_Layout.cshtml": _LAYOUT,
        "Pages/Index.cshtml": page,
    })
    assert _scan(root, templates=True) == {"Pages/Index.cshtml": [(3, "GET", "/x"), (4, "GET", "/x")]}


def test_statement_ids_and_parents_are_well_formed(tmp_path) -> None:
    root = _repo(tmp_path, {
        "Shared/_Layout.cshtml": _LAYOUT,
        "js/site.js": _SITE_JS,
        "Index.cshtml": '@model M\n' + _PAGE.split("@page\n", 1)[1],
    })
    for rec in iter_file_records(root, capture_templates=True):
        ids = [s.id for s in rec.statements]
        assert len(ids) == len(set(ids)), rec.path  # no id collisions / duplicate records
        for s in rec.statements:
            if s.framework == "jquery":
                assert s.path == rec.path and s.id.startswith(rec.path + ":")
                assert s.endLine >= s.startLine and s.parentId


# --- the final compressed analysis output (CLI → .ndjson.gz), the way a run produces it -------

def _export(tmp_path: Path, repo: Path, *flags: str) -> list[dict]:
    out = tmp_path / "out"
    result = CliRunner().invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out), *flags]
    )
    assert result.exit_code == 0, result.output
    (export,) = out.glob("*-project-analysis.ndjson.gz")
    return [json.loads(line) for line in gzip.open(export, "rt", encoding="utf-8").read().splitlines()]


def _exported_jquery(rows: list[dict]) -> list[tuple]:
    return sorted(
        (r["path"], s["startLine"], s.get("method"), s.get("endpoint"))  # null fields are omitted
        for r in rows if r.get("path")
        for s in r.get("statements") or []
        if s.get("semanticType") == "api_call" and s.get("framework") == "jquery"
    )


def test_ndjson_gz_output_has_each_call_once_with_and_without_templates(tmp_path) -> None:
    repo = _repo(tmp_path / "repo", {
        "Pages/_ViewStart.cshtml": '@{ Layout = "_Layout"; }\n',
        "Pages/Shared/_Layout.cshtml": _LAYOUT,
        "Pages/Index.cshtml": (
            '@page\n<script>\n$(function () {\n  $("#a").load("?handler=Rows");\n'
            '  $("#b").load("?handler=Rows");\n  $(window).load(function () {});\n});\n</script>\n'),
        "wwwroot/js/site.js": (
            '$(document).ready(function () {\n'
            '  go = (url) => { $.ajax({ type: "GET", url: url, success: ok }); };\n'
            '  post = form => { $.ajax({ type: "POST", url: form.action, data: d }); };\n'
            '  jQuery.ajax({ url: "/api/lit", method: "PUT" });\n'
            '});\n'),
        "wwwroot/lib/vendor/plugin.js": '(function ($) {\n  $.ajax($.extend(true, { mode: "abort" }, param));\n})(jQuery);\n',
    })
    site = [
        ("wwwroot/js/site.js", 2, "GET", None),  # dynamic url → null, verb and line kept
        ("wwwroot/js/site.js", 3, "POST", None),  # form.action → null
        ("wwwroot/js/site.js", 4, "PUT", "/api/lit"),  # literal URL retained, jQuery alias
    ]
    # default run: Razor is skipped by design, so only the self-evident `$.ajax({url})` forms appear
    default = _exported_jquery(_export(tmp_path / "a", repo))
    assert default == site
    # --capture-templates: the Razor inline `.load` calls join them, once each, at their own lines;
    # the legacy `$(window).load(fn)` and the vendor settings-merge wrapper add nothing
    with_templates = _exported_jquery(_export(tmp_path / "b", repo, "--capture-templates"))
    assert with_templates == sorted(site + [
        ("Pages/Index.cshtml", 4, "GET", "?handler=Rows"),
        ("Pages/Index.cshtml", 5, "GET", "?handler=Rows"),
    ])


# --- D1: jQuery loaded through a server-generated script reference ---------------------------

def _calls(tmp_path, src: str, name: str, parser) -> list[tuple[int, str | None, str | None]]:
    """(startLine, method, endpoint) of every jquery api_call parsed from ``src``."""
    p = tmp_path / name
    b = src.encode()
    p.write_bytes(b)
    ctx = ParseContext(path=name, abs_path=p, source=b, repo_root=tmp_path, capture_statements=True)
    rec = parser.parse_file(ctx)
    return [(s.startLine, s.method, s.endpoint) for s in rec.statements
            if s.semanticType == "api_call" and s.framework == "jquery"]


_SHORTHANDS = (
    '<script>\n$.get("/a");\n$.post("/b");\n$.getJSON("/c");\n$("#x").load("/d");\n</script>\n'
)
_SHORTHAND_CALLS = [(2, "GET", "/a"), (3, "POST", "/b"), (4, "GET", "/c"), (5, "GET", "/d")]
_P = {"aspx": WebFormsParser, "cshtml": RazorParser, "razor": RazorParser, "html": HtmlParser}


@pytest.mark.parametrize(
    ("ext", "reference"),
    [
        ("aspx", '<script src="<%= ResolveUrl("~/Scripts/jquery.min.js") %>"></script>'),
        ("aspx", '<script src=\'<%= ResolveUrl("~/Scripts/jquery-3.6.0.js") %>\'></script>'),
        ("aspx", '<script src="<%: Scripts.Render("~/bundles/jquery") %>"></script>'),
        ("aspx", '<%: Scripts.Render("~/bundles/jquery") %>'),
        ("aspx", '<asp:ScriptManager runat="server"><Scripts>'
                 '<asp:ScriptReference Path="~/Scripts/jquery-3.6.0.js" /></Scripts></asp:ScriptManager>'),
        ("cshtml", '<script src="@Url.Content("~/Scripts/jquery.min.js")"></script>'),
        ("cshtml", '@Scripts.Render("~/bundles/jquery")'),
        ("cshtml", '@Scripts.Render("~/bundles/modernizr", "~/bundles/jquery")'),
        ("html", '<script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>'),  # literal: unchanged
        ("aspx", '<script src="Scripts/jquery.min.js"></script>'),  # literal: unchanged
    ],
)
def test_server_generated_jquery_reference_is_evidence(tmp_path, ext: str, reference: str) -> None:
    got = _calls(tmp_path, reference + "\n" + _SHORTHANDS, f"p.{ext}", _P[ext]())
    assert got == [(l + 1, m, e) for l, m, e in _SHORTHAND_CALLS]  # lines stay true to the file


@pytest.mark.parametrize(
    ("ext", "reference"),
    [
        ("aspx", '<script src="<%= ResolveUrl("~/Scripts/site.js") %>"></script>'),
        ("aspx", '<%: Scripts.Render("~/bundles/jqueryval") %>'),  # validation bundle, not jQuery
        ("aspx", '<%: Scripts.Render("~/bundles/site") %>'),
        ("aspx", '<asp:ScriptReference Path="~/Scripts/app.js" />'),
        ("aspx", '<%= ResolveUrl("~/Scripts/jquery.min.js") %>'),  # a bare expression, not a script load
        ("cshtml", '<script src="@Url.Content("~/js/site.js")"></script>'),
        ("cshtml", '@Scripts.Render("~/bundles/bootstrap")'),
        ("cshtml", '@* <script src="@Url.Content("~/Scripts/jquery.min.js")"></script> *@'),
        ("aspx", '<%-- <%: Scripts.Render("~/bundles/jquery") %> --%>'),
        ("html", '<!-- <script src="jquery.min.js"></script> -->'),
    ],
)
def test_unrelated_or_dead_script_reference_is_not_evidence(tmp_path, ext: str, reference: str) -> None:
    assert _calls(tmp_path, reference + "\n" + _SHORTHANDS, f"p.{ext}", _P[ext]()) == []


def test_server_expression_in_script_tag_does_not_swallow_the_inline_script(tmp_path) -> None:
    # `%>` inside the open tag must not end it early: the body is the next <script>, intact
    page = ('<script src="<%= ResolveUrl("~/Scripts/jquery.min.js") %>" type="text/javascript"></script>\n'
            '<script type="text/javascript">\n$.ajax({ type: "POST", url: "Default.aspx/GetData" });\n</script>\n')
    assert _calls(tmp_path, page, "p.aspx", WebFormsParser()) == [(3, "POST", "Default.aspx/GetData")]


def test_layout_bundle_is_evidence_for_the_view(tmp_path) -> None:
    root = _repo(tmp_path, {
        "Views/_ViewStart.cshtml": '@{ Layout = "_Layout"; }\n',
        "Views/Shared/_Layout.cshtml": '@RenderBody()\n@Scripts.Render("~/bundles/jquery")\n',
        "Views/Home/Index.cshtml": '<script>\n$.get("/api/x");\n</script>\n',
        "Views/Home/Other.cshtml": '@{ Layout = null; }\n<script>\n$.get("/api/y");\n</script>\n',
    })
    assert _scan(root, templates=True) == {"Views/Home/Index.cshtml": [(2, "GET", "/api/x")]}


# --- D2: a server expression is never an endpoint --------------------------------------------

@pytest.mark.parametrize(
    ("ext", "js", "expected"),
    [
        ("cshtml", 'jQuery.get("/api/@Model.Id/edit");', [(1, "GET", None)]),
        ("cshtml", 'jQuery.get("/api/@Model.Id");', [(1, "GET", None)]),
        ("cshtml", 'jQuery.get("/api/@id");', [(1, "GET", None)]),
        ("cshtml", 'jQuery.get("@Url.Action("A", "B")");', [(1, "GET", None)]),
        ("cshtml", 'jQuery.get("/api/@(Model.Id)/x");', [(1, "GET", None)]),
        ("cshtml", 'jQuery.get("/api/" + @Model.Id);', [(1, "GET", None)]),  # parse recovery: prefix only
        ("cshtml", 'jQuery.post("/api/" + @Model.Id, data);', [(1, "POST", None)]),
        ("cshtml", 'jQuery.ajax({ url: "/api/" + @Model.Id, type: "PUT" });', [(1, "PUT", None)]),
        ("cshtml", 'jQuery.ajax({ url: "/api/@Model.Id", type: "DELETE" });', [(1, "DELETE", None)]),
        ("aspx", 'jQuery.get("/api/<%= Id %>/x");', [(1, "GET", None)]),
        ("aspx", 'jQuery.get("/api/" + <%= Id %>);', [(1, "GET", None)]),
        ("aspx", 'jQuery.get("/api/" + <%= Id %>, cb);', [(1, "GET", None)]),
        ("aspx", 'jQuery.ajax({ url: "/api/" + <%= Id %>, type: "POST" });', [(1, "POST", None)]),
        # valid static URLs survive — including an `@` that is not a Razor expression
        ("cshtml", 'jQuery.get("/api/students");', [(1, "GET", "/api/students")]),
        ("cshtml", 'jQuery.get("/api/items?id=1&x=2#top");', [(1, "GET", "/api/items?id=1&x=2#top")]),
        ("cshtml", 'jQuery.get("/contact/a@b.com");', [(1, "GET", "/contact/a@b.com")]),
        ("cshtml", 'jQuery.get("https://user@host.example/x");', [(1, "GET", "https://user@host.example/x")]),
        ("cshtml", 'jQuery.ajax({ url: "/api/s", type: "POST" });', [(1, "POST", "/api/s")]),
        ("html", 'jQuery.get("/api/@Model.Id");', [(1, "GET", None)]),  # plain markup: same honest-null
        # a Razor/WebForms expression elsewhere (a callback) does not null a literal URL
        ("cshtml", 'jQuery.get("/api/ok", function () { var x = @Model.Id; });', [(1, "GET", "/api/ok")]),
        ("aspx", 'jQuery.get("/api/ok", function () { var x = <%= Id %>; });', [(1, "GET", "/api/ok")]),
    ],
)
def test_server_expression_is_never_an_endpoint(tmp_path, ext: str, js: str, expected) -> None:
    page = "<script>\n" + js + "\n</script>\n"
    assert [(l - 1, m, e) for l, m, e in _calls(tmp_path, page, f"p.{ext}", _P[ext]())] == expected


# --- D3: commented-out markup is dead --------------------------------------------------------

_JQ = '<script src="jquery.min.js"></script>\n'


@pytest.mark.parametrize(
    ("ext", "body", "expected"),
    [
        ("html", '<script>$.get("/before");</script>\n<!-- <script>$.get("/c")</script> -->\n'
                 '<script>$.get("/after");</script>\n',
         [(2, "GET", "/before"), (4, "GET", "/after")]),
        ("html", '<!--\n<script>\n$.get("/c");\n</script>\n-->\n<script>\n$.get("/after");\n</script>\n',
         [(8, "GET", "/after")]),
        ("html", '<!-- <script>$.get("/c")</script>\n<script>$.get("/also-dead");</script>\n',
         []),  # an unclosed comment runs to the end of the file
        ("cshtml", '<script>\n@* $.get("/c"); *@\n$.get("/real");\n</script>\n', [(4, "GET", "/real")]),
        ("cshtml", '@* <script>$.get("/c")</script> *@\n<script>$.get("/real");</script>\n', [(3, "GET", "/real")]),
        ("cshtml", '<script>\n$.get("/before");\n@*\n$.get("/c1");\n$.get("/c2");\n*@\n$.get("/after");\n</script>\n',
         [(3, "GET", "/before"), (8, "GET", "/after")]),
        ("razor", '<script>@* $.get("/c") *@ $.get("/same-line");</script>\n', [(2, "GET", "/same-line")]),
        ("aspx", '<%-- <script>$.get("/c")</script> --%>\n<script>$.get("/real");</script>\n', [(3, "GET", "/real")]),
        ("aspx", '<script>\n<%-- $.get("/c"); --%>\n$.get("/real");\n</script>\n', [(4, "GET", "/real")]),
        # comment-like text in JavaScript strings is not a comment, and does not corrupt the call
        ("html", '<script>var a = "<!-- x"; $.get("/ok"); var b = "-->";</script>\n', [(2, "GET", "/ok")]),
        ("html", '<script>var re = "@* not razor *@"; $.get("/ok");</script>\n', [(2, "GET", "/ok")]),
        ("aspx", '<script>var s = "<!-- hi -->"; $.get("/ok");</script>\n', [(2, "GET", "/ok")]),
        ("cshtml", '<script>var s = "<!-- hi -->"; $.get("/ok");</script>\n', [(2, "GET", "/ok")]),
    ],
)
def test_commented_out_markup_is_not_detected(tmp_path, ext: str, body: str, expected) -> None:
    assert _calls(tmp_path, _JQ + body, f"p.{ext}", _P[ext]()) == expected


def test_pathological_unterminated_script_tags_stay_fast(tmp_path) -> None:
    import time
    page = ('<script src="jquery.js"></script>\n' + '<script a="x" ' * 3000 + '\n<script>$.get("/ok");</script>\n')
    t0 = time.monotonic()
    got = _calls(tmp_path, page, "p.html", HtmlParser())
    assert time.monotonic() - t0 < 10  # was minutes: the open-tag search was quadratic+
    assert got == [] or got == [(3, "GET", "/ok")]  # the junk run swallows the tail; never a crash
