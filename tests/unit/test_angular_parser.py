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
export class RouteNames { public static readonly ROOT = ''; static readonly DIAGNOSTICS = 'diagnostics'; }
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


def test_unresolved_paths_are_honest_null(tmp_path) -> None:
    rec = _parse_with_index(
        {"route-names.ts": _CONSTS_SRC, "brand-tab.ts": _CONSTS_SRC, "app-routing.module.ts": _ROUTING_SRC},
        "app-routing.module.ts", tmp_path)
    by_handler = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    # template literal and function-call paths can't be resolved → endpoint None (not garbled)
    assert by_handler["DynComponent"].endpoint is None
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
