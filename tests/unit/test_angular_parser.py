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
    # Routes are statements — only emitted with --capture-statements (spec A4).
    rec = _parse(tmp_path, capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


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


def test_redirect_to_routes_are_skipped(tmp_path) -> None:
    # redirectTo routes must not be emitted as page/mount routes (they are not endpoints).
    src = b'''import { Routes } from '@angular/router';
export const routes: Routes = [
  { path: '', redirectTo: '/home', pathMatch: 'full' },
  { path: 'home', component: HomeComponent },
];
'''
    p = tmp_path / "app.routes.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="app.routes.ts", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = AngularParser().parse_file(ctx)
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    # The redirectTo route must NOT appear
    assert not any(s.endpoint == "/" for s in rec.statements if s.semanticType == "route")
    # The real route must still appear
    assert "/home" in routes


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
