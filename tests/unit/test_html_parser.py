"""Component-template parser: templateUrl resolution, binding/interpolation capture,
standalone-not-claimed, capabilities, schema validity."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.html.parser import HtmlParser
from breezeai_cog.schemas import FileRecord

_COMPONENT_TS = """
import { Component } from '@angular/core';
@Component({ selector: 'app-order', templateUrl: './order.component.html' })
export class OrderComponent { load() {} remove(o) {} }
"""

_TEMPLATE_HTML = """<div *ngFor="let o of orders" (click)="load()" [id]="o.id">
  {{ o.name }}
  <button (click)="remove(o)" [(ngModel)]="q">X</button>
  @if (ready) { <span>{{ title }}</span> }
  <!-- a comment -->
</div>"""


def _build(tmp_path, files: dict[str, str]):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    parser = HtmlParser()
    index = parser.build_index(tmp_path, list(tmp_path.glob("*.html")), jobs=1)
    return parser, index


def _parse(tmp_path, files: dict[str, str], target: str, *, capture=True) -> FileRecord:
    parser, index = _build(tmp_path, files)
    src = files[target].encode()
    ctx = ParseContext(
        path=target,
        abs_path=tmp_path / target,
        source=src,
        repo_root=tmp_path,
        capture_statements=capture,
        resolution_index=index,
    )
    return parser.parse_file(ctx)


def test_templateurl_resolves_to_template(tmp_path) -> None:
    rec = _parse(
        tmp_path,
        {"order.component.ts": _COMPONENT_TS, "order.component.html": _TEMPLATE_HTML},
        "order.component.html",
    )
    assert rec.language == "html"
    assert rec.uiRole == "template"
    assert rec.framework == "angular"
    # the html→component link that drives IMPORTS
    assert rec.importFiles == ["order.component.ts"]


def test_event_binding_handler(tmp_path) -> None:
    rec = _parse(
        tmp_path,
        {"order.component.ts": _COMPONENT_TS, "order.component.html": _TEMPLATE_HTML},
        "order.component.html",
    )
    events = {s.name: s for s in rec.statements if s.nodeType == "event_binding"}
    assert events["click"].handler in {"load", "remove"}
    handlers = {s.handler for s in rec.statements if s.nodeType == "event_binding"}
    assert handlers == {"load", "remove"}


def test_directives_and_interpolation_have_no_semantictype(tmp_path) -> None:
    rec = _parse(
        tmp_path,
        {"order.component.ts": _COMPONENT_TS, "order.component.html": _TEMPLATE_HTML},
        "order.component.html",
    )
    kinds = {s.nodeType for s in rec.statements}
    assert {"structural_directive", "interpolation", "property_binding", "two_way_binding"} <= kinds
    # ordinary markup carries a real nodeType and NO semanticType
    markup = [
        s
        for s in rec.statements
        if s.nodeType
        in {
            "structural_directive",
            "interpolation",
            "property_binding",
            "two_way_binding",
            "event_binding",
            "if_statement",
        }
    ]
    assert markup and all(s.semanticType is None for s in markup)
    # nodeType is a real angular-grammar type, never the "interpolation"-as-nodeType mistake
    # elsewhere — here it IS a genuine grammar node, and never `synthetic`.
    assert all(s.nodeType != "synthetic" for s in rec.statements)
    ngfor = next(s for s in rec.statements if s.nodeType == "structural_directive")
    assert ngfor.name == "ngFor"


def test_angular17_control_flow_captured(tmp_path) -> None:
    rec = _parse(
        tmp_path,
        {"order.component.ts": _COMPONENT_TS, "order.component.html": _TEMPLATE_HTML},
        "order.component.html",
    )
    assert any(s.nodeType == "if_statement" for s in rec.statements)
    # a binding nested inside @if is emitted flat (nesting via line containment)
    assert any(s.nodeType == "interpolation" and s.name == "title" for s in rec.statements)


def test_standalone_html_not_claimed_as_template(tmp_path) -> None:
    rec = _parse(tmp_path, {"index.html": "<html><body><h1>Docs</h1></body></html>"}, "index.html")
    assert rec.language == "html"
    assert rec.uiRole is None
    assert rec.framework is None
    assert rec.statements == []


def test_ambiguous_template_dropped(tmp_path) -> None:
    # Two components claiming the same template → ambiguous → no link (honest-null).
    a = (
        "import { Component } from '@angular/core';\n"
        "@Component({ templateUrl: './shared.html' }) export class A {}\n"
    )
    b = (
        "import { Component } from '@angular/core';\n"
        "@Component({ templateUrl: './shared.html' }) export class B {}\n"
    )
    rec = _parse(
        tmp_path,
        {"a.component.ts": a, "b.component.ts": b, "shared.html": "<p>{{ x }}</p>"},
        "shared.html",
    )
    assert rec.uiRole is None  # dropped — neither owner attributed


def test_non_angular_component_not_claimed(tmp_path) -> None:
    # A `@Component({templateUrl})` WITHOUT an `@angular/` import (AngularJS view, a look-alike
    # decorator) must NOT be claimed as an Angular template — `.html` is not single-framework.
    ts = "@Component({ templateUrl: './view.html' }) export class Widget {}\n"  # no @angular import
    rec = _parse(tmp_path, {"widget.ts": ts, "view.html": "<p>{{ x }}</p>"}, "view.html")
    assert rec.uiRole is None
    assert rec.framework is None


def test_resolved_template_framework_comes_from_resolver(tmp_path) -> None:
    # The stamped framework is the one the index recorded, not a hardcoded literal.
    rec = _parse(
        tmp_path,
        {"order.component.ts": _COMPONENT_TS, "order.component.html": _TEMPLATE_HTML},
        "order.component.html",
    )
    assert rec.framework == "angular"


def test_fallback_angularjs_framework_no_grammar(tmp_path) -> None:
    # An AngularJS view (ng-* markup) with no resolver hit → tagged framework via the in-file
    # fingerprint, uiRole=template, but NO statements (no AngularJS grammar) — honest-null.
    rec = _parse(
        tmp_path, {"view.html": '<div ng-repeat="t in todos" ng-click="go()"></div>'}, "view.html"
    )
    assert rec.framework == "angularjs"
    assert rec.uiRole == "template"
    assert rec.importFiles == []  # fingerprint has no owner link
    assert rec.statements == []  # no angularjs grammar


def test_fallback_angular_without_owner_still_parses(tmp_path) -> None:
    # Angular bindings but no owning .ts (resolver miss) → framework=angular via fingerprint,
    # AND statements (angular grammar exists), but no component link.
    rec = _parse(
        tmp_path, {"orphan.html": '<button (click)="save()">{{ x }}</button>'}, "orphan.html"
    )
    assert rec.framework == "angular" and rec.uiRole == "template"
    assert rec.importFiles == []
    assert any(s.nodeType == "event_binding" and s.handler == "save" for s in rec.statements)


def test_behavior_marker_without_framework(tmp_path) -> None:
    # A plain page enhanced with htmx → behaviors marker, but NOT a rendering framework.
    rec = _parse(
        tmp_path, {"page.html": '<button hx-get="/api" hx-target="#o">Go</button>'}, "page.html"
    )
    assert rec.framework is None
    assert rec.uiRole is None
    assert rec.behaviors == ["htmx"]


def test_rendering_and_behavior_coexist(tmp_path) -> None:
    # Thymeleaf (rendering) + htmx (behavior) on the same file — framework=thymeleaf, and htmx
    # rides as a behavior marker (not a competing framework).
    rec = _parse(tmp_path, {"t.html": '<div th:text="${n}" hx-post="/save"></div>'}, "t.html")
    assert rec.framework == "thymeleaf"
    assert rec.behaviors == ["htmx"]


def test_alpine_not_mislabeled_vue(tmp_path) -> None:
    # Alpine's @click/:class shorthand once collided with Vue — now Alpine is a behavior and
    # Vue needs `v-`, so an Alpine page is behaviors=[alpine], framework=None (never vue).
    rec = _parse(
        tmp_path, {"a.html": '<div x-data="{n:0}" @click="n++" :class="cls"></div>'}, "a.html"
    )
    assert rec.framework is None
    assert rec.behaviors == ["alpine"]


def test_ambiguous_rendering_is_bare(tmp_path) -> None:
    # Two rendering frameworks matched (th: + v-) → ambiguous → honest-null (bare html).
    rec = _parse(tmp_path, {"x.html": '<div th:text="${n}" v-if="ok"></div>'}, "x.html")
    assert rec.framework is None
    assert rec.uiRole is None


_NAV_HTML = """<div>
  <a routerLink="/orders">Orders</a>
  <a [routerLink]="['/orders', id]">Detail</a>
  <a [routerLink]="'/home'">Home</a>
  <a [routerLink]="target">Dynamic</a>
  <a routerLink="/x" routerLinkActive="active">Styled</a>
</div>"""

_NAV_TS = """
import { Component } from '@angular/core';
@Component({ templateUrl: './nav.component.html' })
export class NavComponent {}
"""


def _nav_routes(tmp_path):
    rec = _parse(
        tmp_path,
        {"nav.component.ts": _NAV_TS, "nav.component.html": _NAV_HTML},
        "nav.component.html",
    )
    return rec, [s for s in rec.statements if s.semanticType == "route"]


def test_router_link_is_navigation_route(tmp_path) -> None:
    rec, routes = _nav_routes(tmp_path)
    assert routes, "routerLink should produce route statements"
    for r in routes:
        assert r.routeKind == "navigation" and r.framework == "angular" and r.name == "routerLink"
    endpoints = {r.endpoint for r in routes}
    assert "/orders" in endpoints  # plain attr + array base both resolve to /orders
    assert "/home" in endpoints  # quoted string literal


def test_router_link_dynamic_is_honest_null(tmp_path) -> None:
    _, routes = _nav_routes(tmp_path)
    # `[routerLink]="target"` is a bare expression → endpoint must be null, never the symbol
    dynamic = [r for r in routes if r.endpoint is None]
    assert dynamic and all("target" not in (r.text or "") or r.endpoint is None for r in dynamic)
    assert not any(r.endpoint == "target" for r in routes)


def test_router_link_active_is_not_navigation(tmp_path) -> None:
    _, routes = _nav_routes(tmp_path)
    # routerLinkActive is a CSS-class directive, not a link — exactly one nav on that <a>
    # (its routerLink="/x"), never a second route for routerLinkActive.
    assert "/x" in {r.endpoint for r in routes}
    assert all(r.name == "routerLink" for r in routes)  # no routerLinkActive route


def test_navigation_skipped_in_fixture(tmp_path) -> None:
    # Route emitters skip fixture files; a .spec.html template emits no route statements.
    ts = "import { Component } from '@angular/core';\n@Component({ templateUrl: './x.spec.html' }) export class X {}\n"
    rec = _parse(tmp_path, {"x.ts": ts, "x.spec.html": '<a routerLink="/o">o</a>'}, "x.spec.html")
    assert not any(s.semanticType == "route" for s in rec.statements)


def test_bootstrap_data_target_not_stimulus(tmp_path) -> None:
    # Bootstrap's data-bs-target / a generic data-action must NOT be read as Stimulus — only
    # data-controller activates Stimulus (a confirmed false positive on real repos).
    rec = _parse(
        tmp_path,
        {"b.html": '<button data-bs-target="#nav" data-action="x">Menu</button>'},
        "b.html",
    )
    assert "stimulus" not in (getattr(rec, "behaviors", None) or [])


def test_stimulus_detected_via_controller(tmp_path) -> None:
    rec = _parse(
        tmp_path, {"s.html": '<div data-controller="hello"><button>x</button></div>'}, "s.html"
    )
    assert rec.behaviors == ["stimulus"]


def test_framework_code_in_docs_not_claimed(tmp_path) -> None:
    # A docs page that merely SHOWS framework syntax inside <pre>/<code> must not be tagged as
    # using that framework (fingerprint ignores example/code regions).
    for html in (
        '<h1>Guide</h1><pre><div *ngFor="let x of xs">{{x}}</div></pre>',
        "<p>Use <code>&lt;div *ngFor=&quot;let x&quot;&gt;</code></p>",
        '<p>Vue: <code>&lt;div v-if="ok"&gt;</code></p>',
    ):
        rec = _parse(tmp_path, {"doc.html": html}, "doc.html")
        assert rec.framework is None, html
        assert rec.uiRole is None


def test_statements_require_capture(tmp_path) -> None:
    rec = _parse(
        tmp_path,
        {"order.component.ts": _COMPONENT_TS, "order.component.html": _TEMPLATE_HTML},
        "order.component.html",
        capture=False,
    )
    # still a resolved template (role/link are structural, not gated)…
    assert rec.uiRole == "template"
    # …but no markup statements without --capture-statements
    assert rec.statements == []


def test_html_in_capabilities() -> None:
    registry.clear()
    registry.register(HtmlParser())
    caps = registry.capabilities()
    assert ".html" in caps["extensions"] and ".htm" in caps["extensions"]
    assert "html" in caps["languages"]
    registry.clear()


def test_output_validates(tmp_path) -> None:
    rec = _parse(
        tmp_path,
        {"order.component.ts": _COMPONENT_TS, "order.component.html": _TEMPLATE_HTML},
        "order.component.html",
    )
    schema = FileRecord.model_json_schema(by_alias=True)
    errors = list(Draft202012Validator(schema).iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
    for s in rec.statements:
        errs = list(
            Draft202012Validator(schema["$defs"]["Statement"]).iter_errors(
                json.loads(s.model_dump_json(by_alias=True, exclude_none=True))
            )
        )
        assert not errs, errs
