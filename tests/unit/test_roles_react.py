"""React Router role table: docs/entry-point-roles/react.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point


EP = {"route", "eventbus_consumer", "timer"}

PKG = '{"dependencies": {"react": "18", "react-router-dom": "6"}}'
V7_PKG = '{"dependencies": {"react": "19", "react-router": "7", "@react-router/dev": "7"}}'
HEAD = "import React from 'react';\nimport { Routes, Route, Navigate } from 'react-router-dom';\n"


def _jsx(analyze_files, routes: str):
    src = (
        HEAD
        + f"export function App() {{\n  return (\n    <Routes>\n{routes}    </Routes>\n  );\n}}\n"
    )
    return analyze_files({"package.json": PKG, "src/App.tsx": src})


def _eps(stmts, path: str = "src/App.tsx"):
    return [
        s
        for s in stmts
        if is_entry_point(s) and s.path == path
    ]


def _at(stmts, endpoint: str, path: str = "src/App.tsx"):
    return [s for s in _eps(stmts, path) if s.endpoint == endpoint]


def test_route_with_element_is_binding(analyze_files) -> None:
    st = _jsx(analyze_files, '      <Route path="/users" element={<Users />} />\n')
    [r] = _at(st, "/users")
    assert (r.semanticType, r.routeKind, r.framework, r.handler) == (
        "route",
        "page",
        "react",
        "Users",
    )


def test_wrapped_element_handler_is_the_page(analyze_files) -> None:
    st = _jsx(
        analyze_files,
        '      <Route path="/dash" element={<Suspense fallback={<Spinner />}>'
        "<Dashboard /></Suspense>} />\n",
    )
    [r] = _at(st, "/dash")
    assert r.handler == "Dashboard"


def test_lazy_component_variable_is_binding(analyze_files) -> None:
    src = (
        HEAD + "const Reports = React.lazy(() => import('./Reports'));\n"
        'export function App() {\n  return <Routes><Route path="/reports" '
        "element={<Reports />} /></Routes>;\n}\n"
    )
    st = analyze_files({"package.json": PKG, "src/App.tsx": src})
    [r] = _at(st, "/reports")
    assert (r.routeKind, r.handler) == ("page", "Reports")


def test_navigate_redirect_is_none(analyze_files) -> None:
    st = _jsx(analyze_files, '      <Route path="/old" element={<Navigate to="/" replace />} />\n')
    assert _at(st, "/old") == []


def test_guard_layout_element_is_guard(analyze_files) -> None:
    st = _jsx(
        analyze_files,
        "      <Route element={<ProtectedRoute />}>\n"
        '        <Route path="/account" element={<Account />} />\n'
        "      </Route>\n",
    )
    [r] = _at(st, "/account")
    assert r.guards == ["ProtectedRoute"]
    assert [s.handler for s in _eps(st)] == ["Account"]


def test_layout_with_index_child_is_wiring(analyze_files) -> None:
    st = _jsx(
        analyze_files,
        '      <Route path="/settings" element={<SettingsLayout />}>\n'
        "        <Route index element={<SettingsHome />} />\n"
        '        <Route path="profile" element={<Profile />} />\n'
        "      </Route>\n",
    )
    assert [r.handler for r in _at(st, "/settings")] == ["SettingsHome"]
    assert [r.handler for r in _at(st, "/settings/profile")] == ["Profile"]


def test_element_with_children_and_no_index_is_also_binding(analyze_files) -> None:
    st = _jsx(
        analyze_files,
        '      <Route path="/orders" element={<OrderList />}>\n'
        '        <Route path=":id" element={<OrderDetail />} />\n'
        "      </Route>\n",
    )
    assert [r.handler for r in _at(st, "/orders")] == ["OrderList"]
    assert [r.handler for r in _at(st, "/orders/:id")] == ["OrderDetail"]


def test_path_only_parent_prefixes_children(analyze_files) -> None:
    st = _jsx(
        analyze_files,
        '      <Route path="/admin">\n'
        '        <Route path="users" element={<AdminUsers />} />\n'
        "      </Route>\n",
    )
    assert [r.handler for r in _at(st, "/admin/users")] == ["AdminUsers"]


def test_lazy_route_module_is_binding(analyze_files) -> None:
    st = _jsx(analyze_files, "      <Route path=\"/lazy\" lazy={() => import('./LazyPage')} />\n")
    [r] = _at(st, "/lazy")
    assert r.routeKind == "page"


def test_config_object_route_is_binding(analyze_files) -> None:
    src = (
        "import { createBrowserRouter } from 'react-router-dom';\n"
        "export const router = createBrowserRouter([\n"
        "  { path: '/team', element: <Team /> },\n]);\n"
    )
    st = analyze_files({"package.json": PKG, "src/router.tsx": src})
    [r] = _at(st, "/team", "src/router.tsx")
    assert (r.routeKind, r.handler) == ("page", "Team")


def test_config_object_component_reference_is_binding(analyze_files) -> None:
    src = (
        "import { createBrowserRouter } from 'react-router-dom';\n"
        "export const router = createBrowserRouter([\n"
        "  { path: '/about', Component: About },\n]);\n"
    )
    st = analyze_files({"package.json": PKG, "src/router.tsx": src})
    [r] = _at(st, "/about", "src/router.tsx")
    assert r.handler == "About"


def _v7(analyze_files, body: str, **extra: str):
    src = (
        "import { type RouteConfig, route, index, layout, prefix } from "
        "'@react-router/dev/routes';\nexport default [\n" + body + "] satisfies RouteConfig;\n"
    )
    return analyze_files({"package.json": V7_PKG, "app/routes.ts": src, **extra})


def test_v7_route_is_binding(analyze_files) -> None:
    # The route module's default export (the file is relative to app/routes.ts).
    st = _v7(analyze_files, "  route('about', 'routes/about.tsx'),\n",
             **{"app/routes/about.tsx": "export default function About() { return null; }\n"})
    [r] = _at(st, "/about", "app/routes.ts")
    assert (r.routeKind, r.handler, r.handlerPath, r.handlerLine) == (
        "page", "About", "app/routes/about.tsx", 1)


def test_v7_layout_is_wiring(analyze_files) -> None:
    st = _v7(
        analyze_files, "  layout('routes/shell.tsx', [ route('login', 'routes/login.tsx') ]),\n"
    )
    assert [r.endpoint for r in _eps(st, "app/routes.ts")] == ["/login"]


def test_v7_prefix_is_wiring(analyze_files) -> None:
    st = _v7(analyze_files, "  ...prefix('projects', [ route(':id', 'routes/project.tsx') ]),\n")
    assert [r.endpoint for r in _eps(st, "app/routes.ts")] == ["/projects/:id"]


def test_link_is_outbound_navigation(analyze_files) -> None:
    src = (
        "import React from 'react';\nimport { Link } from 'react-router-dom';\n"
        'export function Nav() { return <Link to="/orders">Orders</Link>; }\n'
    )
    st = analyze_files({"package.json": PKG, "src/Nav.tsx": src})
    assert _eps(st, "src/Nav.tsx") == []
    assert [s.endpoint for s in st if s.routeKind == "navigation"] == ["/orders"]


def test_navigate_call_is_outbound_navigation(analyze_files) -> None:
    src = (
        "import React from 'react';\nimport { useNavigate } from 'react-router-dom';\n"
        "export function Go() {\n  const navigate = useNavigate();\n"
        "  return <button onClick={() => navigate('/reports')}>Go</button>;\n}\n"
    )
    st = analyze_files({"package.json": PKG, "src/Go.tsx": src})
    assert _eps(st, "src/Go.tsx") == []
    assert [s.endpoint for s in st if s.routeKind == "navigation"] == ["/reports"]


def test_fetch_is_outbound(analyze_files) -> None:
    src = (
        "import React from 'react';\n"
        "export async function loadUsers() { return fetch('/api/users'); }\n"
    )
    st = analyze_files({"package.json": PKG, "src/api.tsx": src})
    assert _eps(st, "src/api.tsx") == []
    assert [s.endpoint for s in st if s.semanticType == "api_call"] == ["/api/users"]


def test_create_root_is_none(analyze_files) -> None:
    src = (
        "import React from 'react';\nimport { createRoot } from 'react-dom/client';\n"
        "createRoot(document.getElementById('root')!).render(<App />);\n"
    )
    st = analyze_files({"package.json": PKG, "src/main.tsx": src})
    assert _eps(st, "src/main.tsx") == []


def test_path_only_parent_is_wiring(analyze_files) -> None:
    st = _jsx(
        analyze_files,
        '      <Route path="/admin">\n'
        '        <Route path="users" element={<AdminUsers />} />\n'
        "      </Route>\n",
    )
    assert _at(st, "/admin") == []
    assert [r.handler for r in _at(st, "/admin/users")] == ["AdminUsers"]


def test_plain_pathless_layout_is_not_a_guard(analyze_files) -> None:
    st = _jsx(
        analyze_files,
        "      <Route element={<AppLayout />}>\n"
        '        <Route path="/home" element={<Home />} />\n'
        "      </Route>\n",
    )
    [r] = _at(st, "/home")
    assert (r.handler, r.guards) == ("Home", None)


def test_guard_layout_is_named_by_whole_words(analyze_files) -> None:
    from breezeai_cog.parsers.typescript_react.routes import _is_guard_element
    for name in ("ProtectedRoute", "PrivateOutlet", "AuthGuard", "RequireAuth", "RequireRole",
                 "AuthenticatedRoute", "guards.ProtectedRoute"):
        assert _is_guard_element(name), name
    for name in ("RequiredFieldsNotice", "RequirementsBanner", "RequireJsLoader",
                 "UnprotectedRoute", "NonAuthorizedRedirect", "ProtectedLayout", "AppLayout"):
        assert not _is_guard_element(name), name
    st = _jsx(
        analyze_files,
        "      <Route path=\"/form\" element={<RequiredFieldsNotice />}>\n"
        '        <Route path="edit" element={<EditForm />} />\n'
        "      </Route>\n",
    )
    assert [r.guards for r in _at(st, "/form/edit")] == [None]


def test_config_guard_layout_is_guard(analyze_files) -> None:
    src = (
        "import { createBrowserRouter } from 'react-router-dom';\n"
        "export const router = createBrowserRouter([\n"
        "  { element: <RequireAuth />, children: [ { path: '/billing', element: <Billing /> } ] },\n"
        "]);\n"
    )
    st = analyze_files({"package.json": PKG, "src/router.tsx": src})
    [r] = _at(st, "/billing", "src/router.tsx")
    assert (r.handler, r.guards) == ("Billing", ["RequireAuth"])


def test_guard_layout_with_a_path_guards_its_children(analyze_files) -> None:
    src = (
        "import { createBrowserRouter } from 'react-router-dom';\n"
        "export const router = createBrowserRouter([\n"
        "  { path: '/', element: <ProtectedRoute />, children: [\n"
        "    { index: true, element: <Dashboard /> },\n"
        "    { path: 'orders', element: <Orders /> },\n  ] },\n]);\n"
    )
    st = analyze_files({"package.json": PKG, "src/router.tsx": src})
    got = sorted((r.endpoint, r.handler, tuple(r.guards or ())) for r in _eps(st, "src/router.tsx"))
    assert got == [("/", "Dashboard", ("ProtectedRoute",)), ("/orders", "Orders", ("ProtectedRoute",))]



def test_breadcrumb_array_in_a_router_file_is_not_routes(analyze_files) -> None:
    # `path` alone is not a route: a breadcrumb list has one too.
    src = (HEAD + "import { createBrowserRouter } from 'react-router-dom';\n"
           "export const crumbs = [{ path: '/c', label: 'C' }, { path: '/d', title: 'D', icon: 'x' }];\n"
           "export const r = createBrowserRouter([{ path: '/x', element: <X /> }]);\n")
    st = analyze_files({"package.json": PKG, "src/App.tsx": src})
    assert [r.endpoint for r in _eps(st)] == ["/x"]


def test_jsx_route_node_type_is_the_real_one(analyze_files) -> None:
    st = _jsx(analyze_files,
              '      <Route path="u" element={<U />} />\n'
              '      <Route path="p" element={<P />}><Route path="q" element={<Q />} /></Route>\n')
    got = {r.endpoint: r.nodeType for r in _eps(st)}
    assert got == {"/u": "jsx_self_closing_element", "/p": "jsx_element",
                   "/p/q": "jsx_self_closing_element"}
