"""vue-router role table: docs/entry-point-roles/vue.md — one test per row."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}

PKG = '{"dependencies": {"vue": "3", "vue-router": "4"}}'
ROUTER = "src/router/index.js"


def _routes(analyze_files, body: str, tail: str = ""):
    src = (
        "import { createRouter, createWebHistory } from 'vue-router';\n"
        f"const routes = [\n{body}];\n"
        "const router = createRouter({ history: createWebHistory(), routes });\n"
        f"{tail}export default router;\n"
    )
    return analyze_files({"package.json": PKG, ROUTER: src}, templates=True)


def _eps(stmts, path: str = ROUTER):
    return [
        s
        for s in stmts
        if s.semanticType in EP and s.routeKind != "field_resolver" and s.path == path
    ]


def _at(stmts, endpoint: str, path: str = ROUTER):
    return [s for s in _eps(stmts, path) if s.endpoint == endpoint]


def test_path_component_is_binding(analyze_files) -> None:
    st = _routes(analyze_files, "  { path: '/users', name: 'users', component: UserList },\n")
    [r] = _at(st, "/users")
    assert (r.semanticType, r.routeKind, r.framework, r.handler) == (
        "route",
        "page",
        "vue",
        "UserList",
    )


@pytest.mark.xfail(strict=True, reason="a lazy component import: handler is the import path")
def test_lazy_component_is_binding(analyze_files) -> None:
    st = _routes(
        analyze_files, "  { path: '/reports', component: () => import('../views/Reports.vue') },\n"
    )
    [r] = _at(st, "/reports")
    assert r.routeKind == "page"
    assert not r.handler or "/" not in r.handler


@pytest.mark.xfail(strict=True, reason="named views (components: { default }) give no handler")
def test_named_views_default_component_is_handler(analyze_files) -> None:
    st = _routes(
        analyze_files, "  { path: '/home', components: { default: Home, sidebar: Side } },\n"
    )
    [r] = _at(st, "/home")
    assert r.handler == "Home"


@pytest.mark.xfail(
    strict=True, reason="a layout (component + empty-path child) is recorded as a page"
)
def test_layout_with_empty_path_child_is_wiring(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  { path: '/settings', component: SettingsShell, children: [\n"
        "    { path: '', component: SettingsHome },\n"
        "    { path: 'profile', component: Profile },\n  ] },\n",
    )
    assert [r.handler for r in _at(st, "/settings")] == ["SettingsHome"]
    assert [r.handler for r in _at(st, "/settings/profile")] == ["Profile"]


def test_component_with_children_and_no_empty_child_is_also_binding(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  { path: '/orders', component: OrderList, children: [\n"
        "    { path: ':id', component: OrderDetail },\n  ] },\n",
    )
    assert [r.handler for r in _at(st, "/orders")] == ["OrderList"]
    assert [r.handler for r in _at(st, "/orders/:id")] == ["OrderDetail"]


@pytest.mark.xfail(
    strict=True, reason="a path-only parent { path, children } is recorded as a page"
)
def test_children_only_parent_is_wiring(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  { path: '/home', component: Home },\n"
        "  { path: '/admin', children: [ { path: 'users', component: AdminUsers } ] },\n",
    )
    assert _at(st, "/admin") == []


def test_children_only_parent_prefixes_children(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  { path: '/home', component: Home },\n"
        "  { path: '/admin', children: [ { path: 'users', component: AdminUsers } ] },\n",
    )
    assert [r.handler for r in _at(st, "/admin/users")] == ["AdminUsers"]


def test_redirect_is_none(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  { path: '/', redirect: '/users' },\n  { path: '/users', component: UserList },\n",
    )
    assert [r.endpoint for r in _eps(st)] == ["/users"]


@pytest.mark.xfail(strict=True, reason="beforeEnter guards are not recorded in guards")
def test_before_enter_is_guard(analyze_files) -> None:
    st = _routes(
        analyze_files, "  { path: '/users', component: UserList, beforeEnter: requireAuth },\n"
    )
    [r] = _at(st, "/users")
    assert r.guards == ["requireAuth"]


def test_global_before_each_is_guard(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  { path: '/users', component: UserList },\n",
        tail="router.beforeEach((to, from) => true);\n",
    )
    assert [r.endpoint for r in _eps(st)] == ["/users"]


@pytest.mark.xfail(
    strict=True, reason="router-link is recorded as route/navigation; a static to has no endpoint"
)
def test_router_link_is_outbound_navigation(analyze_files) -> None:
    sfc = (
        '<template>\n  <nav><router-link to="/users">Users</router-link></nav>\n</template>\n'
        "<script>\nexport default {};\n</script>\n"
    )
    st = analyze_files({"package.json": PKG, "src/views/Nav.vue": sfc}, templates=True)
    assert _eps(st, "src/views/Nav.vue") == []
    assert [s.endpoint for s in st if s.semanticType == "navigation"] == ["/users"]


@pytest.mark.xfail(strict=True, reason="router.push is not captured as navigation")
def test_router_push_is_outbound_navigation(analyze_files) -> None:
    src = (
        "import { useRouter } from 'vue-router';\n"
        "export function useGo() {\n  const router = useRouter();\n"
        "  return () => router.push('/reports');\n}\n"
    )
    st = analyze_files({"package.json": PKG, "src/go.js": src})
    assert _eps(st, "src/go.js") == []
    assert [s.endpoint for s in st if s.semanticType == "navigation"] == ["/reports"]


def test_fetch_is_outbound(analyze_files) -> None:
    src = "export async function loadUsers() { return fetch('/api/users'); }\n"
    st = analyze_files({"package.json": PKG, "src/api.js": src})
    assert _eps(st, "src/api.js") == []
    assert [s.endpoint for s in st if s.semanticType == "api_call"] == ["/api/users"]


def test_create_app_is_none(analyze_files) -> None:
    src = "import { createApp } from 'vue';\nimport App from './App.vue';\ncreateApp(App).mount('#app');\n"
    st = analyze_files({"package.json": PKG, "src/main.js": src})
    assert _eps(st, "src/main.js") == []
