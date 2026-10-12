"""Angular framework parser: config-object routes, lazy mounts, guards, selection."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_angular.parser import AngularParser
from breezeai_cog.schemas import FileRecord

SRC = b'''import { RouterModule, Routes } from '@angular/router';
import { NgModule } from '@angular/core';

const routes: Routes = [
  { path: 'orders', component: OrderListComponent },
  { path: 'orders/:id', component: OrderDetailComponent, canActivate: [AuthGuard] },
  { path: 'admin', loadChildren: () => import('./admin/admin.module').then(m => m.AdminModule) },
  {
    path: 'settings',
    component: SettingsComponent,
    children: [
      { path: 'profile', component: ProfileComponent }
    ]
  }
];

@NgModule({ imports: [RouterModule.forRoot(routes)] })
export class AppRoutingModule {}
'''


def _parse(tmp_path, *, capture=True) -> FileRecord:
    p = tmp_path / "app-routing.module.ts"
    p.write_text(SRC.decode())
    ctx = ParseContext(path="app-routing.module.ts", abs_path=p, source=SRC, repo_root=tmp_path,
                       capture_statements=capture)
    return AngularParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    # Routes are statements — only emitted with statement capture (spec A4).
    rec = _parse(tmp_path, capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    # framework is the parser's identity (set unconditionally on any @angular/ file), not a
    # route-detection by-product — so it's "angular" even with statements disabled.
    assert rec.framework == "angular"


def test_routes(tmp_path) -> None:
    rec = _parse(tmp_path)
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert {"/orders", "/orders/:id", "/admin", "/settings", "/settings/profile"} <= set(routes)
    assert routes["/orders"].handler == "OrderListComponent" and routes["/orders"].routeKind == "page"
    assert routes["/orders/:id"].guards == ["AuthGuard"]
    assert routes["/admin"].routeKind == "mount"  # loadChildren lazy mount
    assert routes["/settings/profile"].handler == "ProfileComponent"  # nested child path joined
    assert rec.framework == "angular"
    assert any(c.name == "AppRoutingModule" for c in rec.classes)  # base extraction reused


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_claims_selects_angular() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(AngularParser())
    assert registry.select("x.ts", b"import { Component } from '@angular/core';").name == "typescript-angular"
    assert registry.select("x.ts", b"const x = 1;").name == "typescript"  # plain TS -> base
    registry.clear()


def test_mount_captures_lazy_module_link(tmp_path) -> None:
    # Tier 1: a loadChildren mount must record what it loads, so it's a traversable
    # edge in the code graph rather than a dead-end path segment.
    rec = _parse(tmp_path)
    mount = next(s for s in rec.statements
                 if s.semanticType == "route" and s.endpoint == "/admin")
    assert mount.routeKind == "mount"
    assert mount.handler == "AdminModule"


_STANDALONE_SRC = b'''import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: 'catalog', loadChildren: () => import('./catalog.routes').then(m => m.CATALOG_ROUTES) },
  { path: 'user/:id', loadComponent: () => import('./user.component').then(m => m.UserComponent) },
  { path: 'legacy', loadChildren: 'app/legacy/legacy.module#LegacyModule' },
];
'''


def test_lazy_forms_across_angular_versions(tmp_path) -> None:
    # Standalone routes-const mount, lazy standalone component (a page), and the legacy
    # string form — one detector, no cross-version conflict.
    p = tmp_path / "app.routes.ts"
    p.write_text(_STANDALONE_SRC.decode())
    ctx = ParseContext(path="app.routes.ts", abs_path=p, source=_STANDALONE_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = AngularParser().parse_file(ctx)
    by_ep = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert by_ep["/catalog"].routeKind == "mount" and by_ep["/catalog"].handler == "CATALOG_ROUTES"
    assert by_ep["/user/:id"].routeKind == "page" and by_ep["/user/:id"].handler == "UserComponent"
    assert by_ep["/legacy"].routeKind == "mount" and by_ep["/legacy"].handler == "LegacyModule"


# ── Non-literal path resolution (Task #9) ──────────────────────────────────────
from breezeai_cog.parsers.typescript.imports import build_ts_index  # noqa: E402

# defines the constants the routing module references (cross-file)
_CONSTS_SRC = '''
export class RouteNames { public static readonly ROOT = ''; static readonly DIAGNOSTICS = 'diagnostics';
  static readonly ORGANISATION_CONTEXT = 'org'; static readonly PROJECT_CONTEXT = 'project'; }
export class RouteParams { static readonly ORG_ID = 'orgId'; static readonly PROJECT_ID = 'projectId'; }
export enum BrandTab { Overview = 'overview', Products = 'products' }
'''

_ROUTING_SRC = b'''import { RouterModule, Routes } from '@angular/router';
import { RouteNames } from './route-names';
import { BrandTab } from './brand-tab';

const LOCAL = 'admin';

const routes: Routes = [
  { path: 'login', component: LoginComponent },
  { path: LOCAL, component: AdminComponent },
  { path: RouteNames.DIAGNOSTICS, component: DiagComponent },
  { path: RouteNames.ROOT, component: HomeComponent },
  { path: BrandTab.Products, component: ProductsComponent },
  { path: RouteNames.ORGANISATION_CONTEXT + '/:' + RouteParams.ORG_ID, component: OrgComponent },
  { path: `${RouteNames.PROJECT_CONTEXT}/:${RouteParams.PROJECT_ID}`, component: ProjComponent },
  { path: `dyn/${x}`, component: DynComponent },
  { path: buildPath(), component: CalcComponent },
];
'''


def _parse_with_index(files: dict, target: str, tmp_path) -> FileRecord:
    for name, content in files.items():
        (tmp_path / name).write_text(content if isinstance(content, str) else content.decode())
    index = build_ts_index(tmp_path, [tmp_path / n for n in files])
    src = files[target]
    src = src if isinstance(src, bytes) else src.encode()
    ctx = ParseContext(path=target, abs_path=str(tmp_path / target), source=src,
                       repo_root=str(tmp_path), capture_statements=True, resolution_index=index)
    return AngularParser().parse_file(ctx)


def test_const_and_enum_path_resolution(tmp_path) -> None:
    rec = _parse_with_index(
        {"route-names.ts": _CONSTS_SRC, "brand-tab.ts": _CONSTS_SRC, "app-routing.module.ts": _ROUTING_SRC},
        "app-routing.module.ts", tmp_path)
    eps = {(s.endpoint, s.handler) for s in rec.statements if s.semanticType == "route"}
    assert ("/login", "LoginComponent") in eps          # plain literal
    assert ("/admin", "AdminComponent") in eps          # in-file const LOCAL
    assert ("/diagnostics", "DiagComponent") in eps     # cross-file static readonly
    assert ("/", "HomeComponent") in eps                # RouteNames.ROOT = '' → root
    assert ("/products", "ProductsComponent") in eps    # cross-file string enum
    # the garbled symbol text must NOT appear as an endpoint
    assert not any(e and "RouteNames" in e for e, _ in eps)


def test_templated_path_resolution(tmp_path) -> None:
    # A path built from resolvable consts + a literal :param — concatenation and template
    # forms — assembles to a templated endpoint (all pieces resolve), not None.
    rec = _parse_with_index(
        {"route-names.ts": _CONSTS_SRC, "brand-tab.ts": _CONSTS_SRC, "app-routing.module.ts": _ROUTING_SRC},
        "app-routing.module.ts", tmp_path)
    by_handler = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    # RouteNames.ORGANISATION_CONTEXT + '/:' + RouteParams.ORG_ID  ->  org/:orgId
    assert by_handler["OrgComponent"].endpoint == "/org/:orgId"
    # `${RouteNames.PROJECT_CONTEXT}/:${RouteParams.PROJECT_ID}`  ->  project/:projectId
    assert by_handler["ProjComponent"].endpoint == "/project/:projectId"


def test_unresolved_paths_are_honest_null(tmp_path) -> None:
    rec = _parse_with_index(
        {"route-names.ts": _CONSTS_SRC, "brand-tab.ts": _CONSTS_SRC, "app-routing.module.ts": _ROUTING_SRC},
        "app-routing.module.ts", tmp_path)
    by_handler = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    # a template with a dynamic (non-const) substitution `${x}` stays None (never stringified)
    assert by_handler["DynComponent"].endpoint is None
    # a function-call path stays None
    assert by_handler["CalcComponent"].endpoint is None


def test_ambiguous_const_not_resolved(tmp_path) -> None:
    # same symbol declared with DIFFERENT literals in two files → ambiguous → honest-null
    a = "export const DUP = 'one';\n"
    b = "export const DUP = 'two';\n"
    routing = b'''import { RouterModule, Routes } from '@angular/router';
const routes: Routes = [ { path: DUP, component: C } ];
'''
    rec = _parse_with_index({"a.ts": a, "b.ts": b, "app-routing.module.ts": routing},
                            "app-routing.module.ts", tmp_path)
    ep = next(s.endpoint for s in rec.statements if s.semanticType == "route")
    assert ep is None


# ── Lazy loadChildren cross-file path (Tier-2) ─────────────────────────────────
_APP_ROUTING = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'orgs', loadChildren: () => import('./org.module').then(m => m.OrgModule) },
];
export class AppRoutingModule {}
'''
_ORG_ROUTING = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'projects', component: ProjectsComponent },
  { path: 'settings', loadChildren: () => import('./settings.module').then(m => m.SettingsModule) },
];
export class OrgModule {}
'''
_SETTINGS_ROUTING = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'billing', component: BillingComponent },
];
export class SettingsModule {}
'''


def test_lazy_loadchildren_cross_file_prefix(tmp_path) -> None:
    # A child module parsed in its own file gets the parent mount's prefix prepended.
    files = {"app.module.ts": _APP_ROUTING, "org.module.ts": _ORG_ROUTING}
    rec = _parse_with_index(files, "org.module.ts", tmp_path)
    eps = {s.handler: s.endpoint for s in rec.statements if s.semanticType == "route"}
    # OrgModule is mounted at 'orgs' → its own routes compose under it.
    assert eps["ProjectsComponent"] == "/orgs/projects"


def test_lazy_loadchildren_chain_composition(tmp_path) -> None:
    # app → org (orgs) → settings (settings): a grandchild gets the FULL composed chain.
    files = {"app.module.ts": _APP_ROUTING, "org.module.ts": _ORG_ROUTING,
             "settings.module.ts": _SETTINGS_ROUTING}
    rec = _parse_with_index(files, "settings.module.ts", tmp_path)
    eps = {s.handler: s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert eps["BillingComponent"] == "/orgs/settings/billing"


_REDIRECT_SRC = b'''import { Routes } from '@angular/router';
export const routes: Routes = [
  { path: '', redirectTo: 'home', pathMatch: 'full' },
  { path: 'home', component: HomeComponent },
  { path: 'old/:id', redirectTo: 'new/:id' },
  { path: 'admin', children: [
    { path: '', redirectTo: 'users', pathMatch: 'full' },
    { path: 'users', component: UsersComponent },
  ]},
];
'''


def _parse_routes_file(src: bytes, tmp_path) -> FileRecord:
    p = tmp_path / "app.routes.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="app.routes.ts", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    return AngularParser().parse_file(ctx)


def _route_rows(src: bytes, tmp_path) -> list[tuple[int, str | None, str | None, str | None]]:
    rec = _parse_routes_file(src, tmp_path)
    return [(s.startLine, s.routeKind, s.endpoint, s.handler)
            for s in rec.statements if s.semanticType == "route"]


def test_route_output_for_redirect_fixture_page_rows(tmp_path) -> None:
    # Characterization: page/mount rows of a config that also contains redirects.
    pages = [r for r in _route_rows(_REDIRECT_SRC, tmp_path) if r[1] != "navigation"]
    assert pages == [
        (4, "page", "/home", "HomeComponent"),
        (6, "page", "/admin", None),
        (8, "page", "/admin/users", "UsersComponent"),
    ]


def _navs(src: bytes, tmp_path) -> list[tuple[int, str | None]]:
    return [(line, ep) for line, kind, ep, _ in _route_rows(src, tmp_path) if kind == "navigation"]


def _routes_src(*entries: str) -> bytes:
    body = "\n".join(f"  {e}," for e in entries)
    return f"import {{ Routes }} from '@angular/router';\nexport const routes: Routes = [\n{body}\n];\n".encode()


def test_redirect_routes_emit_navigation_to_target(tmp_path) -> None:
    # Test expectation updated: redirectTo used to be skipped; #115 captures it as a
    # navigation edge whose endpoint is the redirect target (never a page/mount row).
    rows = _route_rows(_REDIRECT_SRC, tmp_path)
    assert [r for r in rows if r[1] == "navigation"] == [
        (3, "navigation", "/home", None),
        (5, "navigation", "/new/:id", None),
        (7, "navigation", "/admin/users", None),
    ]
    assert not any(r[1] in ("page", "mount") and r[2] == "/" for r in rows)


def test_redirect_statement_shape(tmp_path) -> None:
    rec = _parse_routes_file(_REDIRECT_SRC, tmp_path)
    nav = next(s for s in rec.statements if s.routeKind == "navigation")
    assert (nav.nodeType, nav.semanticType, nav.framework) == ("synthetic", "route", "angular")
    assert nav.text.startswith("{ path: '', redirectTo: 'home'")  # source path stays visible


def test_redirect_absolute_target_not_prefixed(tmp_path) -> None:
    src = _routes_src("{ path: 'home', component: HomeComponent }",
                      "{ path: 'admin', children: [{ path: 'x', redirectTo: '/home' }] }")
    assert _navs(src, tmp_path) == [(4, "/home")]


def test_redirect_function_form_target_is_null(tmp_path) -> None:
    src = _routes_src("{ path: 'a', redirectTo: () => '/x' }",
                      "{ path: 'b', redirectTo: (snap) => `/y/${snap.params.id}` }")
    assert _navs(src, tmp_path) == [(3, None), (4, None)]


def test_redirect_wildcard_and_empty_target(tmp_path) -> None:
    src = _routes_src("{ path: '**', redirectTo: '' }",
                      "{ path: 'p', children: [{ path: '', redirectTo: '' }] }")
    assert _navs(src, tmp_path) == [(3, "/"), (4, "/p")]  # relative '' → the parent prefix


def test_redirect_const_target_resolved_and_unresolved(tmp_path) -> None:
    routing = _routes_src("{ path: '', redirectTo: RouteNames.DIAGNOSTICS }",
                          "{ path: 'a', redirectTo: UNKNOWN.CONST }").decode()
    rec = _parse_with_index({"route-names.ts": _CONSTS_SRC, "app-routing.module.ts": routing},
                            "app-routing.module.ts", tmp_path)
    navs = [(s.startLine, s.endpoint) for s in rec.statements if s.routeKind == "navigation"]
    assert navs == [(3, "/diagnostics"), (4, None)]


_ORG_REDIRECT_ROUTING = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: '', redirectTo: 'projects', pathMatch: 'full' },
  { path: 'legacy', redirectTo: '/projects' },
  { path: 'projects', component: ProjectsComponent },
];
export class OrgModule {}
'''


def test_redirect_under_lazy_mount_relative_composes_absolute_does_not(tmp_path) -> None:
    files = {"app.module.ts": _APP_ROUTING, "org.module.ts": _ORG_REDIRECT_ROUTING}
    rec = _parse_with_index(files, "org.module.ts", tmp_path)
    navs = [(s.startLine, s.endpoint) for s in rec.statements if s.routeKind == "navigation"]
    assert navs == [(3, "/orgs/projects"), (4, "/projects")]


def test_redirect_under_unresolved_parent_relative_is_null_absolute_survives(tmp_path) -> None:
    # A relative target needs its parent prefix; with the parent unresolved it is unknown (None),
    # never a guessed path. An absolute target does not depend on the parent.
    src = _routes_src("{ path: Unknown.X, component: ShellComponent, children: [",
                      "  { path: '', redirectTo: 'a', pathMatch: 'full' },",
                      "  { path: 'abs', redirectTo: '/root' }] }")
    assert _navs(src, tmp_path) == [(4, None), (5, "/root")]


def test_redirect_breadcrumb_shaped_object_is_ignored(tmp_path) -> None:
    # A nav/breadcrumb object carries a top-level "name"; it must never become a navigation row.
    src = _routes_src("{ name: 'Home', path: '', redirectTo: 'home' }",
                      "{ path: 'home', component: HomeComponent }")
    assert _navs(src, tmp_path) == []


def test_ngmodule_import_propagation(tmp_path) -> None:
    # When BrandModule (loaded via loadChildren) has @NgModule({imports: [BrandRoutingModule]}),
    # the routing module should get the parent prefix applied too.
    app_module = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'brand', loadChildren: () => import('./brand.module').then(m => m.BrandModule) },
];
export class AppRoutingModule {}
'''
    brand_module = b'''import { NgModule } from '@angular/core';
import { BrandRoutingModule } from './brand-routing.module';

@NgModule({ imports: [BrandRoutingModule] })
export class BrandModule {}
'''
    brand_routing = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'products', component: ProductsComponent },
];
export class BrandRoutingModule {}
'''
    files = {
        "app.module.ts": app_module,
        "brand.module.ts": brand_module,
        "brand-routing.module.ts": brand_routing,
    }
    rec = _parse_with_index(files, "brand-routing.module.ts", tmp_path)
    eps = {s.handler: s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert eps["ProductsComponent"] == "/brand/products"


def test_lazy_multi_mount_module_is_honest_null(tmp_path) -> None:
    # A module mounted at TWO different prefixes → ambiguous → child keeps its own bare path
    # (never wrongly attributed to one parent).
    app = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'a', loadChildren: () => import('./shared.module').then(m => m.SharedModule) },
  { path: 'b', loadChildren: () => import('./shared.module').then(m => m.SharedModule) },
];
export class AppRoutingModule {}
'''
    shared = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [ { path: 'x', component: XComponent } ];
export class SharedModule {}
'''
    rec = _parse_with_index({"app.module.ts": app, "shared.module.ts": shared},
                            "shared.module.ts", tmp_path)
    eps = {s.handler: s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert eps["XComponent"] == "/x"  # bare path, not /a/x or /b/x


def test_breadcrumb_objects_not_captured_as_routes(tmp_path) -> None:
    # Breadcrumb/nav config arrays with {name, path, children} must NOT be captured as
    # Angular routes — even when the file imports ActivatedRoute from @angular/router
    # (which triggers the byte guard) the objects lack router-discriminating keys.
    src = b"""import { Injectable } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { BreadcrumbItem } from './breadcrumb.model';

@Injectable({ providedIn: 'root' })
export class BreadcrumbService {
  constructor(private route: ActivatedRoute) {}
  getBreadcrumbs() {
    return [
      { name: 'Home', path: '/' },
      { name: 'Orders', path: '/orders', children: [
        { name: 'Detail', path: '/orders/:id' },
      ]},
    ];
  }
}
"""
    p = tmp_path / "breadcrumb.service.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="breadcrumb.service.ts", abs_path=p, source=src,
                       repo_root=tmp_path, capture_statements=True)
    rec = AngularParser().parse_file(ctx)
    assert not any(s.semanticType == "route" for s in rec.statements)


def test_breadcrumb_name_key_excluded_even_with_router_discriminating_keys(tmp_path) -> None:
    # If a nav object has BOTH a "name" key AND a router-discriminating key (e.g. pathMatch),
    # the "name" key takes precedence: it is not an Angular route (the route discriminating
    # keys check ensures the array is processed, but each element with "name" is skipped).
    src = b"""import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'home', component: HomeComponent },
];
export class AppRoutingModule {}
"""
    # Mix: same file also has a nav array with "name" + "path" + "pathMatch" in objects
    src2 = b"""import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'home', component: HomeComponent },
];
const nav = [
  { name: 'Home', path: '/home', pathMatch: 'full' },
];
export class AppRoutingModule {}
"""
    for s, expected_count in [(src, 1), (src2, 1)]:
        p = tmp_path / "app.ts"
        p.write_bytes(s)
        ctx = ParseContext(path="app.ts", abs_path=p, source=s, repo_root=tmp_path,
                           capture_statements=True)
        rec = AngularParser().parse_file(ctx)
        route_nodes = [st for st in rec.statements if st.semanticType == "route"]
        assert len(route_nodes) == expected_count, (
            f"expected {expected_count} route(s), got {len(route_nodes)}: {route_nodes}"
        )


def test_ngmodule_chain_3level_prefix(tmp_path) -> None:
    # 3-level NgModule chain: AppModule mounts BrandModule (loadChildren) → BrandModule's
    # @NgModule imports BrandRoutingModule → BrandRoutingModule mounts ProductsModule
    # (loadChildren) → ProductsModule's @NgModule imports ProductsRoutingModule.
    # ProductsRoutingModule's routes must carry the full composed prefix.
    app = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'brand', loadChildren: () => import('./brand.module').then(m => m.BrandModule) },
];
export class AppRoutingModule {}
'''
    brand_module = b'''import { NgModule } from '@angular/core';
import { BrandRoutingModule } from './brand-routing.module';
@NgModule({ imports: [BrandRoutingModule] })
export class BrandModule {}
'''
    brand_routing = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'products', loadChildren: () => import('./products.module').then(m => m.ProductsModule) },
];
export class BrandRoutingModule {}
'''
    products_module = b'''import { NgModule } from '@angular/core';
import { ProductsRoutingModule } from './products-routing.module';
@NgModule({ imports: [ProductsRoutingModule] })
export class ProductsModule {}
'''
    products_routing = b'''import { RouterModule, Routes } from '@angular/router';
export const routes: Routes = [
  { path: 'edit/:id', component: EditComponent },
];
export class ProductsRoutingModule {}
'''
    files = {
        "app.module.ts": app,
        "brand.module.ts": brand_module,
        "brand-routing.module.ts": brand_routing,
        "products.module.ts": products_module,
        "products-routing.module.ts": products_routing,
    }
    rec = _parse_with_index(files, "products-routing.module.ts", tmp_path)
    eps = {s.handler: s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert eps.get("EditComponent") == "/brand/products/edit/:id", (
        f"expected /brand/products/edit/:id, got {eps}"
    )


# ---- uiRole: @Component / @Directive / @Pipe -------------------------------

UIROLE_SRC = b'''import { Component, Directive, Pipe, Injectable } from '@angular/core';

@Component({ selector: 'app-user', template: '<p>u</p>' })
export class UserComponent {}

@Directive({ selector: '[appHi]' })
export class HighlightDirective {}

@Pipe({ name: 'money' })
export class MoneyPipe {}

@Injectable({ providedIn: 'root' })
export class UserService {}

export class PlainHelper {}
'''


def _parse_src(tmp_path, src: bytes, name: str) -> FileRecord:
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    return AngularParser().parse_file(ctx)


def test_decorator_ui_roles(tmp_path) -> None:
    rec = _parse_src(tmp_path, UIROLE_SRC, "widgets.ts")
    # Three orthogonal axes: framework identity, source language, and per-node uiRole.
    assert rec.framework == "angular"
    assert rec.language == "typescript"
    roles = {c.name: c.uiRole for c in rec.classes}
    assert roles["UserComponent"] == "component"
    assert roles["HighlightDirective"] == "directive"
    assert roles["MoneyPipe"] == "pipe"
    assert roles["UserService"] is None  # @Injectable is a service, not a UI role
    assert roles["PlainHelper"] is None


def test_ui_roles_without_capture_statements(tmp_path) -> None:
    # Classes are always captured, so uiRole marking does not depend on statement capture.
    p = tmp_path / "widgets.ts"
    p.write_bytes(UIROLE_SRC)
    ctx = ParseContext(path="widgets.ts", abs_path=p, source=UIROLE_SRC, repo_root=tmp_path,
                       capture_statements=False)
    rec = AngularParser().parse_file(ctx)
    roles = {c.name: c.uiRole for c in rec.classes}
    assert roles["UserComponent"] == "component"
    assert roles["MoneyPipe"] == "pipe"
