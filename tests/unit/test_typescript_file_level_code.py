"""Code at file level outside any Function or Class (anonymous-functions Rules 2b and 3, JS/TS):
a named function expression in it is a Function of the file; everything else — nameless
callbacks included — is the file's own: its statements are the file's, its calls ``File.calls``."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser


def _parse(tmp_path: Path, src: str, name: str = "app.js"):
    p = tmp_path / name
    p.write_text(src)
    ctx = ParseContext(path=name, abs_path=p, source=p.read_bytes(), repo_root=tmp_path,
                       capture_statements=True)
    return TypeScriptParser().parse_file(ctx)


def _file_calls(rec) -> list[tuple[str, str | None]]:
    return [(c.name, c.path) for c in rec.calls or []]


def _file_statements(rec) -> list[tuple[int, str]]:
    return sorted((s.startLine, s.text) for s in rec.statements
                  if s.parentId == rec.id and s.nodeType != "comment")


def test_nameless_callbacks_are_the_files_code(tmp_path: Path) -> None:
    rec = _parse(tmp_path, """const express = require('express');
setInterval(function tick() { refresh(); }, 1000);
process.on('SIGTERM', () => {
  logger.info('bye');
  exit(0);
});
""")
    # `require` is an import, not a call the file makes
    assert _file_calls(rec) == [("setInterval", None), ("on", None), ("info", None),
                                ("exit", None)]
    assert {f.name: [c.name for c in f.calls] for f in rec.functions} == {"tick": ["refresh"]}
    assert [t for _, t in _file_statements(rec)][-2:] == ["logger.info('bye');", "exit(0);"]


def test_a_handler_bound_in_a_callback_is_called_by_the_file(tmp_path: Path) -> None:
    rec = _parse(tmp_path, """$('#saveBtn').click(function () {
  saveForm();
});
var saveForm = function () { post(); };
""")
    assert ("saveForm", "app.js") in _file_calls(rec)
    assert [f.name for f in rec.functions] == ["saveForm"]


def test_an_inline_route_handlers_body_is_inside_its_line_range(tmp_path: Path) -> None:
    rec = _parse(tmp_path, """const app = express();
app.get('/users/:id', async (req, res) => {
  const u = await repo.find(req.params.id);
  res.json(u);
});
""")
    assert _file_statements(rec)[-2:] == [(3, "const u = await repo.find(req.params.id);"),
                                          (4, "res.json(u);")]


def test_functions_classes_namespaces_and_decorators_keep_their_code(tmp_path: Path) -> None:
    rec = _parse(tmp_path, """@Component({ selector: 'x' })
export class Widget {
  count = compute();
  render() { draw(); }
}
function helper() { inner(); }
const f = () => other();
namespace NS { export function g() { deep(); } }
start();
""", "app.ts")
    # only the file's own call; the decorator, the class, the functions and the namespace keep theirs
    assert _file_calls(rec) == [("start", None)]
    assert sum(1 for s in rec.statements if s.text.startswith("deep(")) <= 1  # never twice


def test_a_one_line_named_handler_leaves_the_route_on_the_outer_call(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "app.post('/users', function createUser(req, res) { save(); });\n")
    outer, inner = sorted((s for s in rec.statements if s.startLine == 1),
                          key=lambda s: len(s.text), reverse=True)
    # the file's own statement is listed before the nested function's on the same line
    assert rec.statements.index(outer) < rec.statements.index(inner)
    assert inner.parentId.endswith("createUser@1")


def test_a_named_inline_route_handler_names_the_route_handler(analyze_files) -> None:
    st = analyze_files({"package.json": '{"dependencies": {"express": "4"}}',
                        "app.js": """const express = require('express');
const app = express();
app.post('/users', function createUser(req, res) { save(); });
app.get('/a', (req, res) => res.end());
function reg() { app.put('/x', function updateX(req, res) { saveX(); }); }
"""})
    routes = {s.endpoint: (s.handler, s.handlerLine) for s in st if s.semanticType == "route"}
    assert routes == {"/users": ("createUser", 3), "/a": (None, 4), "/x": ("updateX", 5)}
