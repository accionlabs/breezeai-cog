"""What the AngularJS detector must leave alone.

Written before the detector and green on the code without it: each case is a construct that
looks like an AngularJS one (a `.when(` call, a `.controller(` registration, a routes array) but
is not, or one that is already captured correctly and must not change.
"""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point


def _angularjs(stmts):
    return [s for s in stmts if s.framework == "angularjs" or s.uiRole == "controller"]


def test_when_on_other_receivers_is_not_a_route(analyze_files) -> None:
    st = analyze_files(
        {
            "src/flow.js": (
                "const router = makeRouter();\n"
                "router.when('/x', { controller: 'C' });\n"
                "promise.when('/y', { redirectTo: '/z' });\n"
            ),
        }
    )
    assert not [s for s in st if s.semanticType == "route"]
    assert not _angularjs(st)


def test_vue_global_registrations_are_not_angularjs(analyze_files) -> None:
    st = analyze_files(
        {
            "src/main.js": (
                "import Vue from 'vue';\n"
                "Vue.directive('focus', { inserted(el) { el.focus(); } });\n"
                "Vue.component('app-card', { template: '<div/>' });\n"
                "app.controller('Home', function () {});\n"
            ),
        }
    )
    assert not _angularjs(st)
    assert not [s for s in st if s.uiRole in {"controller", "directive"}]


def test_http_in_a_named_function_stays_an_api_call(analyze_files) -> None:
    st = analyze_files(
        {
            "app/named.js": (
                "angular.module('shop').controller('NamedCtrl', NamedCtrl);\n"
                "function NamedCtrl($scope, $http) {\n"
                "  $http.get('/api/named').then(function () {});\n"
                "}\n"
            ),
        }
    )
    [call] = [s for s in st if s.semanticType == "api_call"]
    assert (call.method, call.endpoint) == ("GET", "/api/named")


def test_express_routes_are_unchanged(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": '{"dependencies": {"express": "4"}}',
            "src/app.js": (
                "const express = require('express');\n"
                "const app = express();\n"
                "app.get('/users', listUsers);\n"
                "function listUsers(req, res) { res.end(); }\n"
            ),
        }
    )
    [r] = [s for s in st if is_entry_point(s)]
    assert (r.method, r.endpoint, r.framework, r.handler) == (
        "GET",
        "/users",
        "express",
        "listUsers",
    )


def test_angular_routes_are_unchanged(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": '{"dependencies": {"@angular/core": "17", "@angular/router": "17"}}',
            "src/app/app.routes.ts": (
                "import { Routes } from '@angular/router';\n"
                "export const routes: Routes = [\n"
                "  { path: 'home', component: HomeComponent },\n"
                "  { path: 'old', redirectTo: 'home' },\n"
                "];\n"
            ),
        }
    )
    got = sorted(
        (s.routeKind, s.endpoint, s.framework, s.handler) for s in st if s.semanticType == "route"
    )
    assert got == [
        ("navigation", "/home", "angular", None),
        ("page", "/home", "angular", "HomeComponent"),
    ]


def test_vue_routes_are_unchanged(analyze_files) -> None:
    st = analyze_files(
        {
            "src/router/index.js": (
                "import { createRouter, createWebHistory } from 'vue-router';\n"
                "const routes = [{ path: '/users', component: UserList }];\n"
                "export default createRouter({ history: createWebHistory(), routes });\n"
            ),
        }
    )
    [r] = [s for s in st if is_entry_point(s)]
    assert (r.endpoint, r.framework, r.handler) == ("/users", "vue", "UserList")


def test_a_long_chain_is_read_without_recursion(analyze_files) -> None:
    whens = "".join(f"  .when('/p{i}', {{ controller: 'C{i}' }})\n" for i in range(400))
    st = analyze_files({"app/routes.js": f"$routeProvider\n{whens};\n"})
    assert len([s for s in st if s.semanticType == "route"]) == 400


def test_hybrid_file_keeps_its_angular_owner_and_gets_angularjs_routes(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": '{"dependencies": {"@angular/core": "17", "angular": "1.8"}}',
            "src/hybrid.ts": (
                "import { downgradeComponent } from '@angular/upgrade/static';\n"
                "angular.module('shop').config(function ($routeProvider) {\n"
                "  $routeProvider.when('/legacy', { controller: 'LegacyCtrl' });\n"
                "});\n"
            ),
        }
    )
    [r] = [s for s in st if s.semanticType == "route"]
    assert (r.framework, r.endpoint, r.handler) == ("angularjs", "/legacy", "LegacyCtrl")


def test_an_imported_controller_variable_is_located_like_any_ts_handler(analyze_files) -> None:
    st = analyze_files(
        {
            "app/routes.js": (
                "import { ListCtrl } from './list';\n"
                "angular.module('shop').config(function ($routeProvider) {\n"
                "  $routeProvider.when('/list', { controller: ListCtrl });\n"
                "});\n"
            ),
            "app/list.js": "export function ListCtrl() {}\n",
        }
    )
    [r] = [s for s in st if s.semanticType == "route"]
    assert (r.handler, r.handlerPath, r.handlerLine) == ("ListCtrl", "app/list.js", 1)
