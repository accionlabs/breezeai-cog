"""vue-router role table: docs/entry-point-roles/vue.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point


EP = {"route", "eventbus_consumer", "timer"}

PKG = '{"dependencies": {"vue": "3", "vue-router": "4"}}'
ROUTER = "src/router/index.js"


def _routes(analyze_files, body: str, tail: str = "", **extra: str):
    src = (
        "import { createRouter, createWebHistory } from 'vue-router';\n"
        f"const routes = [\n{body}];\n"
        "const router = createRouter({ history: createWebHistory(), routes });\n"
        f"{tail}export default router;\n"
    )
    return analyze_files({"package.json": PKG, ROUTER: src, **extra}, templates=True)


def _eps(stmts, path: str = ROUTER):
    return [
        s
        for s in stmts
        if is_entry_point(s) and s.path == path
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


def test_lazy_component_is_binding(analyze_files) -> None:
    # A lazily imported component module: its default export, in its file.
    st = _routes(
        analyze_files, "  { path: '/reports', component: () => import('../views/Reports') },\n",
        **{"src/views/Reports.js": "export default function Reports() { return null; }\n"},
    )
    [r] = _at(st, "/reports")
    assert (r.routeKind, r.handler, r.handlerPath, r.handlerLine) == (
        "page", "Reports", "src/views/Reports.js", 1)
    # A single-file component: its `name` option, else (Vue's rule) its file name.
    st = _routes(
        analyze_files, "  { path: '/a', component: () => import('../views/About.vue') },\n",
        **{"src/views/About.vue": "<template><p/></template>\n<script setup>\n"
           "defineOptions({ name: 'AboutPage' })\n</script>\n"},
    )
    [r] = _at(st, "/a")
    assert (r.handler, r.handlerPath, r.handlerLine) == ("AboutPage", "src/views/About.vue", 2)
    # A module that can't be read: no name rather than the import path.
    st = _routes(analyze_files, "  { path: '/x', component: () => import('../views/X.vue') },\n")
    [r] = _at(st, "/x")
    assert not r.handler


def test_named_views_default_component_is_handler(analyze_files) -> None:
    st = _routes(
        analyze_files, "  { path: '/home', components: { default: Home, sidebar: Side } },\n"
    )
    [r] = _at(st, "/home")
    assert r.handler == "Home"


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


def test_router_link_is_outbound_navigation(analyze_files) -> None:
    sfc = (
        '<template>\n  <nav><router-link to="/users">Users</router-link></nav>\n</template>\n'
        "<script>\nexport default {};\n</script>\n"
    )
    st = analyze_files({"package.json": PKG, "src/views/Nav.vue": sfc}, templates=True)
    assert _eps(st, "src/views/Nav.vue") == []
    assert [s.endpoint for s in st if s.routeKind == "navigation"] == ["/users"]


def test_router_push_is_outbound_navigation(analyze_files) -> None:
    src = (
        "import { useRouter } from 'vue-router';\n"
        "export function useGo() {\n  const router = useRouter();\n"
        "  return () => router.push('/reports');\n}\n"
    )
    st = analyze_files({"package.json": PKG, "src/go.js": src})
    assert _eps(st, "src/go.js") == []
    assert [s.endpoint for s in st if s.routeKind == "navigation"] == ["/reports"]


def test_fetch_is_outbound(analyze_files) -> None:
    src = "export async function loadUsers() { return fetch('/api/users'); }\n"
    st = analyze_files({"package.json": PKG, "src/api.js": src})
    assert _eps(st, "src/api.js") == []
    assert [s.endpoint for s in st if s.semanticType == "api_call"] == ["/api/users"]


def test_create_app_is_none(analyze_files) -> None:
    src = "import { createApp } from 'vue';\nimport App from './App.vue';\ncreateApp(App).mount('#app');\n"
    st = analyze_files({"package.json": PKG, "src/main.js": src})
    assert _eps(st, "src/main.js") == []


def test_parent_before_enter_guards_its_children(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  { path: '/admin', component: AdminShell, beforeEnter: requireAdmin, children: [\n"
        "    { path: '', component: AdminHome },\n"
        "    { path: 'users', component: AdminUsers, beforeEnter: audit },\n  ] },\n",
    )
    # The shell is a layout (an empty-path child): no record; entering a child enters it first.
    assert [(r.handler, r.guards) for r in _at(st, "/admin")] == [("AdminHome", ["requireAdmin"])]
    assert [(r.handler, r.guards) for r in _at(st, "/admin/users")] == [
        ("AdminUsers", ["requireAdmin", "audit"]),
    ]


def test_route_record_is_its_route_object(analyze_files) -> None:
    st = _routes(analyze_files, "  { path: '/users', component: UserList },\n")
    [r] = _at(st, "/users")
    assert r.nodeType == "object"  # the route object's tree-sitter type, not "synthetic"
