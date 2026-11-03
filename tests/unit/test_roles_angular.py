"""Angular role table: docs/entry-point-roles/angular.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point


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
        if is_entry_point(s) and s.path == path
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


def test_load_component_default_export_handler_is_not_a_path(analyze_files) -> None:
    # The handler is the loaded component's name, with its file and line — never the path.
    body = ("export const routes: Routes = [\n"
            "  { path: 'help', loadComponent: () => import('./help.component') },\n];\n")
    st = _routes(analyze_files, body, **{"src/app/help.component.ts": (
        "import { Component } from '@angular/core';\n"
        "@Component({ selector: 'app-help', template: '' })\n"
        "export default class HelpComponent {}\n")})
    [r] = _at(st, "/help")
    assert (r.routeKind, r.handler, r.handlerPath, r.handlerLine) == (
        "page", "HelpComponent", "src/app/help.component.ts", 3)
    # The module is not in the repo: the name is unknown, so the handler is empty.
    missing = body.replace("./help.component", "./missing.component").replace("'help'", "'gone'")
    [r] = _at(_routes(analyze_files, missing), "/gone")
    assert (r.handler, r.handlerPath) == (None, None)


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
    # The mount is wiring: its loaded routes are in the repo and carry its prefix.
    assert _at(st, "/admin") == []


def test_load_children_outside_the_repo_keeps_a_mount_record(analyze_files) -> None:
    # Nothing in the repo is served under these prefixes, so the mount is the only record of
    # them (the Target Spec's current form). A bare import path is a file, not a handler name.
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'billing', loadChildren: () => import('@acme/billing').then(m => m.BillingModule) },\n"
        "  { path: 'help', loadChildren: () => import('./help/help.routes') },\n];\n",
    )
    got = [(s.endpoint, s.routeKind, s.handler) for s in st
           if s.semanticType == "route" and s.path == "src/app/app.routes.ts"]
    assert got == [("/billing", "mount", "BillingModule"), ("/help", "mount", None)]
    assert _eps(st) == []  # a mount is not an entry point


def test_load_children_of_a_file_with_no_route_records_keeps_a_mount_record(analyze_files) -> None:
    # The loaded array is built from imported route objects, which give no records of their
    # own: the mount is the only record of `/account`.
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'account', loadChildren: () => import('./account/account.route') },\n];\n",
        **{"src/app/account/account.route.ts": (
            "import { Routes } from '@angular/router';\n"
            "import settingsRoute from './settings.route';\n"
            "const accountRoutes: Routes = [settingsRoute];\nexport default accountRoutes;\n")},
    )
    got = [(s.endpoint, s.routeKind) for s in st
           if s.semanticType == "route" and s.path == "src/app/app.routes.ts"]
    assert got == [("/account", "mount")]


def test_shell_with_a_redirect_child_is_wiring(analyze_files) -> None:
    # The '' child redirects, so the shell's own URL always shows a child page.
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'auth', component: AuthShell, children: [\n"
        "    { path: '', redirectTo: 'login', pathMatch: 'full' },\n"
        "    { path: 'login', component: LoginComponent },\n  ] },\n];\n",
    )
    assert [(r.endpoint, r.handler) for r in _eps(st)] == [("/auth/login", "LoginComponent")]


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


def test_can_match_is_guard(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n"
        "  { path: 'users', component: UserListComponent, canMatch: [RoleGuard] },\n];\n",
    )
    [r] = _at(st, "/users")
    assert r.guards == ["RoleGuard"]


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
    assert [s.endpoint for s in st if s.routeKind == "navigation"] == ["/users"]


def test_router_navigate_is_outbound_navigation(analyze_files) -> None:
    svc = (
        "import { Injectable } from '@angular/core';\n"
        "import { Router } from '@angular/router';\n"
        "@Injectable()\nexport class Go {\n  constructor(private router: Router) {}\n"
        "  go() { this.router.navigate(['/users']); }\n}\n"
    )
    st = analyze_files({"package.json": PKG, "src/app/go.service.ts": svc})
    assert _eps(st, "src/app/go.service.ts") == []
    assert [s.endpoint for s in st if s.routeKind == "navigation"] == ["/users"]


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


def test_lazy_mount_guards_reach_the_loaded_routes(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n  { path: 'admin', canActivate: [authGuard], loadChildren:"
        " () => import('./admin/admin.routes') },\n];\n",
        **{
            "src/app/admin/admin.routes.ts": (
                "import { Routes } from '@angular/router';\n"
                "const routes: Routes = [ { path: 'reports', component: ReportsComponent } ];\n"
                "export default routes;\n"
            )
        },
    )
    [r] = _eps(st, "src/app/admin/admin.routes.ts")
    assert (r.handler, r.guards) == ("ReportsComponent", ["authGuard"])


def test_route_records_are_their_route_objects(analyze_files) -> None:
    # nodeType is the tree-sitter type of the route object, not "synthetic": the node exists
    st = _routes(
        analyze_files,
        "export const routes: Routes = [\n  { path: 'users', component: UserListComponent },\n"
        "  { path: 'admin', loadChildren: () => import('./admin').then(m => m.AdminModule) },\n"
        "  { path: 'old', redirectTo: 'users' },\n];\n",
    )
    got = {(s.routeKind, s.nodeType) for s in st
           if s.semanticType == "route" and s.path == "src/app/app.routes.ts"}
    assert got == {("page", "object"), ("mount", "object"), ("navigation", "object")}
