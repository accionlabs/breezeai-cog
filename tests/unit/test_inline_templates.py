"""Markup written as a ``template: '…'`` string in a code file (Angular, AngularJS, Vue)."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.parsers.typescript_angular.parser import AngularParser
from breezeai_cog.parsers.typescript_vue.parser import VueParser
from breezeai_cog.schemas import FileRecord


def _parse(parser, tmp_path, name: str, source: bytes, *, capture=True) -> FileRecord:
    p = tmp_path / name
    p.write_bytes(source)
    ctx = ParseContext(
        path=name, abs_path=p, source=source, repo_root=tmp_path, capture_statements=capture
    )
    return parser.parse_file(ctx)


def _found(rec: FileRecord) -> list[tuple[str | None, str | None, str | None, int]]:
    return [
        (s.name, s.handler, s.endpoint, s.startLine)
        for s in rec.statements
        if s.handler is not None or s.semanticType in ("route", "api_call")
    ]


_ANGULAR = b"""import { Component } from '@angular/core';
@Component({
  selector: 'app-todo',
  template: `
    <form (ngSubmit)="save()">
      <a routerLink="/help">Help</a>
      <button (click)="remove()">Delete</button>
    </form>
  `
})
export class TodoComponent { save() {} remove() {} }
"""


def test_angular_inline_template_is_read_at_its_lines(tmp_path) -> None:
    rec = _parse(AngularParser(), tmp_path, "todo.component.ts", _ANGULAR)
    assert ("ngSubmit", "save", None, 5) in _found(rec)
    assert ("click", "remove", None, 7) in _found(rec)
    nav = next(s for s in rec.statements if s.semanticType == "route")
    assert (nav.endpoint, nav.startLine) == ("/help", 6)
    assert [f.name for f in rec.functions] == ["save", "remove"]  # the code is unchanged


def test_template_with_a_runtime_part_is_not_read(tmp_path) -> None:
    src = _ANGULAR.replace(b"Delete</button>", b"${label}</button>")
    rec = _parse(AngularParser(), tmp_path, "todo.component.ts", src)
    assert not [s for s in rec.statements if s.handler in ("save", "remove")]


def test_template_url_component_is_not_walked(tmp_path) -> None:
    src = b"""import { Component } from '@angular/core';
@Component({ selector: 'app-x', templateUrl: './x.html' })
export class X { go() {} }
"""
    rec = _parse(AngularParser(), tmp_path, "x.component.ts", src)
    assert not [s for s in rec.statements if s.handler == "go"]


def test_string_without_a_tag_is_not_markup(tmp_path) -> None:
    src = _ANGULAR.replace(b"template: `", b"template: `plain text`, other: `", 1)
    rec = _parse(AngularParser(), tmp_path, "todo.component.ts", src)
    assert not [s for s in rec.statements if s.handler in ("save", "remove")]


def test_no_markup_records_without_statement_capture(tmp_path) -> None:
    rec = _parse(AngularParser(), tmp_path, "todo.component.ts", _ANGULAR, capture=False)
    assert rec.statements == []


def test_vue_template_option_in_a_js_component(tmp_path) -> None:
    src = b"""import { defineComponent } from 'vue'
export default defineComponent({
  template: `<button @click="inc">Add</button><router-link to="/help">Help</router-link>`,
  methods: { inc() {} }
})
"""
    rec = _parse(VueParser(), tmp_path, "Counter.js", src)
    assert ("click", "inc", None, 3) in _found(rec)
    nav = next(s for s in rec.statements if s.semanticType == "route")
    assert (nav.endpoint, nav.startLine) == ("/help", 3)


def test_angularjs_component_template_string(tmp_path) -> None:
    src = b"""angular.module('app', []).component('cartBadge', {
  template: '<span ng-click="vm.open()"><a href="/cart">Cart</a></span>',
  controller: function () { this.open = function () {}; }
});
"""
    rec = _parse(TypeScriptParser(), tmp_path, "badge.js", src)
    assert ("ng-click", "open", None, 2) in _found(rec)
    link = next(s for s in rec.statements if s.endpoint == "/cart")
    assert (link.semanticType, link.startLine) == ("route", 2)
