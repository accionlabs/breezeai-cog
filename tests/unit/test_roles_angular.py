"""Angular role table: docs/entry-point-roles/angular.md — one test per row."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}

PKG = '{"dependencies": {"@angular/core": "17", "@angular/router": "17"}}'
HEAD = "import { Routes } from '@angular/router';\n"


def _routes(analyze_files, body: str, **extra: str):
    files = {"package.json": PKG, "src/app/app.routes.ts": HEAD + body, **extra}
    return analyze_files(files, templates=True)


def _eps(stmts, path: str = "src/app/app.routes.ts"):
    return [
        s
        for s in stmts
        if s.semanticType in EP and s.routeKind != "field_resolver" and s.path == path
    ]


def _at(stmts, endpoint: str, path: str = "src/app/app.routes.ts"):
    return [s for s in _eps(stmts, path) if s.endpoint == endpoint]


def test_path_component_is_binding(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n  { path: 'users', component: UserListComponent },\n];\n",
    )
    [r] = _at(st, "/users")
    assert (r.semanticType, r.routeKind, r.framework, r.handler) == (
        "route",
        "page",
        "angular",
        "UserListComponent",
    )


def test_load_component_is_binding(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n  { path: 'about', loadComponent:"
        " () => import('./about.component').then(m => m.AboutComponent) },\n];\n",
    )
    [r] = _at(st, "/about")
    assert (r.routeKind, r.handler) == ("page", "AboutComponent")


@pytest.mark.xfail(strict=True, reason="default-export loadComponent: handler is the import path")
def test_load_component_default_export_handler_is_not_a_path(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'help', loadComponent: () => import('./help.component') },\n];\n",
    )
    [r] = _at(st, "/help")
    assert r.routeKind == "page"
    assert not r.handler or "/" not in r.handler


@pytest.mark.xfail(strict=True, reason="loadChildren is recorded as a route (routeKind mount)")
def test_load_children_is_wiring(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n  { path: 'admin', loadChildren:"
        " () => import('./admin/admin.module').then(m => m.AdminModule) },\n];\n",
    )
    assert _at(st, "/admin") == []


def test_load_children_prefixes_the_loaded_routes(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n  { path: 'admin', loadChildren:"
        " () => import('./admin/admin.module').then(m => m.AdminModule) },\n];\n",
        **{
            "src/app/admin/admin.module.ts": (
                "import { NgModule } from '@angular/core';\n"
                "import { RouterModule, Routes } from '@angular/router';\n"
                "const routes: Routes = [ { path: 'reports', component: ReportsComponent } ];\n"
                "@NgModule({ imports: [RouterModule.forChild(routes)] })\n"
                "export class AdminModule {}\n"
            )
        },
    )
    [r] = _at(st, "/admin/reports", "src/app/admin/admin.module.ts")
    assert r.handler == "ReportsComponent"


@pytest.mark.xfail(
    strict=True, reason="a path-only parent { path, children } is recorded as a page"
)
def test_children_only_parent_is_wiring(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'home', component: HomeComponent },\n"
        "  { path: 'shop', children: [ { path: 'cart', component: CartComponent } ] },\n];\n",
    )
    assert _at(st, "/shop") == []


def test_children_only_parent_prefixes_children(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'home', component: HomeComponent },\n"
        "  { path: 'shop', children: [ { path: 'cart', component: CartComponent } ] },\n];\n",
    )
    [r] = _at(st, "/shop/cart")
    assert r.handler == "CartComponent"


@pytest.mark.xfail(
    strict=True, reason="a layout (component + empty-path child) is recorded as a page"
)
def test_layout_with_empty_path_child_is_wiring(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'settings', component: SettingsShell, children: [\n"
        "    { path: '', component: SettingsHome },\n"
        "    { path: 'profile', component: Profile },\n  ] },\n];\n",
    )
    assert [r.handler for r in _at(st, "/settings")] == ["SettingsHome"]
    assert [r.handler for r in _at(st, "/settings/profile")] == ["Profile"]


def test_component_with_children_and_no_empty_child_is_also_binding(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'orders', component: OrderList, children: [\n"
        "    { path: ':id', component: OrderDetail },\n  ] },\n];\n",
    )
    assert [r.handler for r in _at(st, "/orders")] == ["OrderList"]
    assert [r.handler for r in _at(st, "/orders/:id")] == ["OrderDetail"]


def test_redirect_is_none(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: '', redirectTo: 'users', pathMatch: 'full' },\n"
        "  { path: 'users', component: UserListComponent },\n];\n",
    )
    assert [r.endpoint for r in _eps(st)] == ["/users"]


def test_can_activate_is_guard(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'users', component: UserListComponent, canActivate: [AuthGuard] },\n];\n",
    )
    [r] = _at(st, "/users")
    assert r.guards == ["AuthGuard"]


@pytest.mark.xfail(strict=True, reason="canMatch guards are not recorded in guards")
def test_can_match_is_guard(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'users', component: UserListComponent, canMatch: [RoleGuard] },\n];\n",
    )
    [r] = _at(st, "/users")
    assert r.guards == ["RoleGuard"]


@pytest.mark.xfail(strict=True, reason="canActivateChild guards are not recorded on the children")
def test_can_activate_child_is_guard_on_children(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'orders', component: OrderList, canActivateChild: [ChildGuard],\n"
        "    children: [ { path: ':id', component: OrderDetail } ] },\n];\n",
    )
    [r] = _at(st, "/orders/:id")
    assert r.guards == ["ChildGuard"]


def test_http_client_call_is_outbound(analyze_files) -> None:
    svc = (
        "import { Injectable } from '@angular/core';\n"
        "import { HttpClient } from '@angular/common/http';\n"
        "@Injectable()\nexport class UserService {\n"
        "  constructor(private http: HttpClient) {}\n"
        "  load() { return this.http.get('/api/users'); }\n}\n"
    )
    st = analyze_files({"package.json": PKG, "src/app/user.service.ts": svc})
    assert _eps(st, "src/app/user.service.ts") == []
    assert [(s.semanticType, s.endpoint) for s in st if s.semanticType == "api_call"] == [
        ("api_call", "/api/users")
    ]


@pytest.mark.xfail(strict=True, reason="routerLink is recorded as route/navigation, not navigation")
def test_router_link_is_outbound_navigation(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": PKG,
            "src/app/nav.component.ts": (
                "import { Component } from '@angular/core';\n"
                "@Component({ selector: 'app-nav', templateUrl: './nav.component.html' })\n"
                "export class NavComponent {}\n"
            ),
            "src/app/nav.component.html": '<a routerLink="/users">Users</a>\n',
        },
        templates=True,
    )
    assert _eps(st, "src/app/nav.component.html") == []
    assert [s.endpoint for s in st if s.semanticType == "navigation"] == ["/users"]


@pytest.mark.xfail(strict=True, reason="router.navigate / navigateByUrl are not captured")
def test_router_navigate_is_outbound_navigation(analyze_files) -> None:
    svc = (
        "import { Injectable } from '@angular/core';\n"
        "import { Router } from '@angular/router';\n"
        "@Injectable()\nexport class Go {\n  constructor(private router: Router) {}\n"
        "  go() { this.router.navigate(['/users']); }\n}\n"
    )
    st = analyze_files({"package.json": PKG, "src/app/go.service.ts": svc})
    assert _eps(st, "src/app/go.service.ts") == []
    assert [s.endpoint for s in st if s.semanticType == "navigation"] == ["/users"]


def test_bootstrap_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": PKG,
            "src/main.ts": (
                "import { bootstrapApplication } from '@angular/platform-browser';\n"
                "bootstrapApplication(AppComponent);\n"
            ),
        }
    )
    assert _eps(st, "src/main.ts") == []
