"""React framework parser: JSX + config route detection, nesting, base reuse, override."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import BaseParser, ParseContext
from breezeai_cog.parsers.typescript_react.parser import ReactParser
from breezeai_cog.schemas import FileRecord

# Declarative JSX <Route> form (with nesting).
JSX_SRC = b'''import { Routes, Route } from 'react-router-dom';

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Home/>} />
      <Route path="users" element={<Users/>}>
        <Route path=":id" element={<UserDetail/>} />
      </Route>
    </Routes>
  );
}
'''

# Data-router config-object form (with nesting + lazy mount).
CONFIG_SRC = b'''import { createBrowserRouter } from 'react-router-dom';

export const router = createBrowserRouter([
  { path: '/', element: <Root/>, children: [
    { path: 'team', element: <Team/> },
    { path: 'reports', lazy: () => import('./Reports') },
  ]},
]);
'''


def _parse(tmp_path, src: bytes, name: str, *, capture=True) -> FileRecord:
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture)
    return ReactParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, JSX_SRC, "App.tsx", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    # framework is the parser's identity (set unconditionally), not a route-detection
    # by-product — a React file is "react" even with statements/routes disabled.
    assert rec.framework == "react"


def test_jsx_routes_detected_and_nested(tmp_path) -> None:
    rec = _parse(tmp_path, JSX_SRC, "App.tsx")
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {"/", "/users", "/users/:id"}  # nested path joined onto parent
    assert routes["/users/:id"].handler == "UserDetail"
    assert routes["/"].handler == "Home"
    assert {endpoint: route.nodeType for endpoint, route in routes.items()} == {
        "/": "jsx_self_closing_element",
        "/users": "jsx_element",
        "/users/:id": "jsx_self_closing_element",
    }
    assert all(route.nodeType != "synthetic" for route in routes.values())
    assert all(r.framework == "react" and r.routeKind == "page" for r in routes.values())
    assert all(r.parentId == rec.id for r in routes.values())  # parented to file
    assert rec.framework == "react"


def test_config_routes_detected_with_lazy_mount(tmp_path) -> None:
    rec = _parse(tmp_path, CONFIG_SRC, "router.tsx")
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {"/", "/team", "/reports"}
    assert routes["/team"].handler == "Team"
    assert routes["/reports"].routeKind == "mount"  # lazy code-split
    assert routes["/team"].routeKind == "page"
    assert rec.framework == "react"


def test_config_objects_need_a_router_discriminating_key(tmp_path) -> None:
    src = b'''import { createBrowserRouter } from 'react-router-dom';

const breadcrumbs = [
  { path: '/home', label: 'Home' },
  { path: '/about', label: 'About' },
];

export const router = createBrowserRouter([
  { path: '/', element: <Home/> },
  { path: '/crumb', label: 'Breadcrumb' },
  { path: '/nav', name: 'Navigation item', children: [] },
  { path: '/step', title: 'Step', children: [] },
]);
'''
    rec = _parse(tmp_path, src, "router.tsx")
    routes = [s.endpoint for s in rec.statements if s.semanticType == "route"]
    assert routes == ["/"]


@pytest.mark.parametrize("item", [
    "{ path: '/home', label: 'Home' }",
    "{ path: '/projects', title: 'Projects', children: [] }",
    "{ path: '/profile', name: 'Profile', icon: 'user', children: [] }",
    "{ path: '/settings', icon: 'settings', children: [] }",
    "{ path: '/help', href: '/help', children: [] }",
])
def test_navigation_items_with_route_like_keys_are_not_routes(tmp_path, item: str) -> None:
    src = (
        "import { useRoutes } from 'react-router-dom';\n"
        f"const navigation = [{item}];\n"
    ).encode()
    rec = _parse(tmp_path, src, "navigation.tsx")
    assert [s for s in rec.statements if s.semanticType == "route"] == []


def test_config_routes_keep_supported_discriminating_keys(tmp_path) -> None:
    src = b'''import { useRoutes } from 'react-router-dom';

const routes = useRoutes([
  { path: '/Component', Component: Home },
  { path: '/component', component: Home },
  { path: '/lazy', lazy: async () => ({ Component: Home }) },
  { path: '/loader', loader: async () => null },
  { path: '/children', children: [] },
  { path: '/index', index: true },
  { path: '/action', action: async () => null },
  { path: '/error', errorElement: <ErrorPage/> },
  { path: '/handle', handle: {} },
  { path: '/case', caseSensitive: true },
]);
'''
    rec = _parse(tmp_path, src, "routes.tsx")
    endpoints = {s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert endpoints == {
        "/Component", "/component", "/lazy", "/loader", "/children",
        "/index", "/action", "/error", "/handle", "/case",
    }


def test_config_routes_require_react_router_bytes(tmp_path) -> None:
    src = b'''const routes = [
  { path: '/home', element: <Home/> },
];
'''
    rec = _parse(tmp_path, src, "router.tsx")
    assert [s for s in rec.statements if s.semanticType == "route"] == []


INDEX_JSX_SRC = b'''import { Routes, Route } from 'react-router-dom';

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout/>}>
        <Route index element={<Home/>} />
        <Route path="about" element={<About/>} />
      </Route>
    </Routes>
  );
}
'''

INDEX_CONFIG_SRC = b'''import { createHashRouter } from 'react-router-dom';

export const router = createHashRouter([
  { path: '/', element: <Layout/>, children: [
    { index: true, element: <Home/> },
    { path: 'about', element: <About/> },
  ]},
]);
'''


def test_jsx_index_route_captured(tmp_path) -> None:
    rec = _parse(tmp_path, INDEX_JSX_SRC, "App.tsx")
    routes = [(s.endpoint, s.handler) for s in rec.statements if s.semanticType == "route"]
    # index route renders at the parent path ("/") alongside the layout route
    assert ("/", "Home") in routes and ("/", "Layout") in routes and ("/about", "About") in routes


# React Router v7 framework-mode config DSL (route/index/layout/prefix helper calls),
# composed across module-level consts + spreads (as real routes.ts files do).
V7_SRC = b'''import { index, layout, prefix, route } from '@react-router/dev/routes';

const tenderRoutes = prefix('/tender', [
  route('/new', 'routes/tender/new.tsx'),
  route('/:tenderId', 'routes/tender/detail.tsx'),
]);

const procurementRoutes = prefix('/procurements', [
  ...prefix('/:itemId', [
    index('routes/procurement/index.tsx'),
    ...tenderRoutes,
  ]),
]);

export default [
  index('routes/home.tsx'),
  layout('routes/layouts/main.tsx', [
    route('/chat', 'routes/chat.tsx'),
    ...procurementRoutes,
    route('*', 'routes/not-found.tsx'),
  ]),
] satisfies RouteConfig;
'''


def test_v7_config_dsl_routes_detected(tmp_path) -> None:
    rec = _parse(tmp_path, V7_SRC, "routes.ts")
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    # cross-const + spread composition resolves the full prefix chain.
    assert "/procurements/:itemId/tender/new" in routes
    assert "/procurements/:itemId/tender/:tenderId" in routes
    # index() renders at the enclosing prefix path (no own segment).
    assert "/procurements/:itemId" in routes
    assert routes["/procurements/:itemId"].handler == "routes/procurement/index.tsx"
    # layout() is a pathless wrapper (mount) and its children keep the parent path.
    assert "/chat" in routes and routes["/chat"].handler == "routes/chat.tsx"
    main = next(s for s in rec.statements if s.handler == "routes/layouts/main.tsx")
    assert main.routeKind == "mount"
    # catch-all is a real route, not filtered.
    assert "/*" in routes
    assert routes["/procurements/:itemId/tender/new"].framework == "react"
    assert rec.framework == "react"


def test_v7_route_path_from_cross_file_const_object(tmp_path) -> None:
    # A route path built from an imported `{…} as const` object resolves to the real URL
    # (endpoint = fully-resolved, per spec) — not the raw `paths.discover.root` AST text.
    (tmp_path / "paths.ts").write_text(
        "const seg = 'discover';\n"
        "const tabs = { projects: 'projects' } as const;\n"
        "export const paths = { discover: { root: `/${seg}`, tabs } } as const;\n"
    )
    routes = tmp_path / "routes.ts"
    routes.write_bytes(
        b"import { prefix, route } from '@react-router/dev/routes';\n"
        b"import { paths } from './paths';\n"
        b"export default [\n"
        b"  ...prefix(paths.discover.root, [\n"
        b"    route(`/${paths.discover.tabs.projects}`, 'routes/projects.tsx'),\n"
        b"    route('/plain', 'routes/plain.tsx'),\n"
        b"  ]),\n"
        b"];\n"
    )
    parser = ReactParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.ts")))
    ctx = ParseContext(path="routes.ts", abs_path=routes, source=routes.read_bytes(),
                       repo_root=tmp_path, resolution_index=index, capture_statements=True)
    rec = parser.parse_file(ctx)
    endpoints = {s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert "/discover/projects" in endpoints   # member-expr + template both folded
    assert "/discover/plain" in endpoints       # plain literal still joins onto the folded prefix
    assert not any(e and "paths." in e for e in endpoints)  # no raw AST text leaks


def test_v7_unresolved_const_path_falls_back_to_raw(tmp_path) -> None:
    # With no index (or an unresolvable const), the endpoint is the raw text — no worse than
    # before, never a *different* wrong URL (honest-null).
    src = (
        b"import { route } from '@react-router/dev/routes';\n"
        b"import { paths } from './paths';\n"
        b"export default [ route(paths.unknown.thing, 'x.tsx') ];\n"
    )
    rec = _parse(tmp_path, src, "routes.ts")  # _parse passes no resolution_index
    endpoints = {s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert "/paths.unknown.thing" in endpoints


def test_v7_detection_is_inert_on_v6_code(tmp_path) -> None:
    # A v6 file (react-router-dom, JSX/object forms) must not trigger any v7 call
    # matching, and its own detection must be unchanged. Guarded by the import gate.
    for src, name, expected in ((JSX_SRC, "App.tsx", {"/", "/users", "/users/:id"}),
                                (CONFIG_SRC, "router.tsx", {"/", "/team", "/reports"})):
        rec = _parse(tmp_path, src, name)
        eps = {s.endpoint for s in rec.statements if s.semanticType == "route"}
        assert eps == expected, (name, eps)


def test_config_index_route_captured(tmp_path) -> None:
    # createHashRouter is handled like createBrowserRouter (config-object walker)
    rec = _parse(tmp_path, INDEX_CONFIG_SRC, "router.tsx")
    routes = [(s.endpoint, s.handler) for s in rec.statements if s.semanticType == "route"]
    assert ("/", "Home") in routes and ("/", "Layout") in routes and ("/about", "About") in routes


def test_base_extraction_reused(tmp_path) -> None:
    rec = _parse(tmp_path, JSX_SRC, "App.tsx")
    assert "App" in {f.name for f in rec.functions}
    assert rec.language == "typescript"


def test_output_validates(tmp_path) -> None:
    for src, name in ((JSX_SRC, "App.tsx"), (CONFIG_SRC, "router.tsx")):
        rec = _parse(tmp_path, src, name)
        errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                      .iter_errors(json.loads(to_line(rec))))
        assert not errors, errors


# Storybook decorator: a createMemoryRouter config-object router wrapping the component
# under test — a rendering harness, not application routes (R4).
STORY_SRC = b'''import type { Meta } from '@storybook/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { CompanyCard } from '../index';

const meta = {
  component: CompanyCard,
  decorators: [
    (StoryComponent) => {
      const router = createMemoryRouter(
        [{ path: '*', element: <StoryComponent /> }],
        { initialEntries: ['/'] },
      );
      return <RouterProvider router={router} />;
    },
  ],
};
export default meta;
'''


def test_fixture_markers_are_layered() -> None:
    # Global layer: test/spec infixes on any parser.
    for p in ["foo.test.ts", "bar.spec.tsx"]:
        assert BaseParser().is_fixture_file(p), p
    # `.stories.` is a TS-language addition, NOT global — the base parser must not match it.
    assert not BaseParser().is_fixture_file("a/company-card.stories.tsx")
    # TypeScript (and every TS framework parser, which inherits it) adds `.stories.`.
    react = ReactParser()
    for p in ["a/company-card.stories.tsx", "x.stories.ts", "e.cy.ts", "foo.test.ts"]:
        assert react.is_fixture_file(p), p
    for p in ["routes.ts", "App.tsx", "src/story-list.tsx", "a/notification-feed.tsx"]:
        assert not react.is_fixture_file(p), p


def test_story_file_emits_no_routes_but_keeps_structure(tmp_path) -> None:
    # R4: route-only exclusion — a *.stories.tsx file is parsed for structure but its
    # decorator router must NOT produce routes.
    rec = _parse(tmp_path, STORY_SRC, "company-card.stories.tsx")
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    # still parsed for structure — exclusion is route-only. framework is the file's identity
    # (a story is still a React file), so it's stamped; language stays typescript (.tsx).
    assert rec.framework == "react"
    assert rec.language == "typescript"


def test_claims_selects_react() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(ReactParser())
    assert registry.select("App.tsx", b"import { Route } from 'react-router-dom';").name == "typescript-react"
    # A bare `react` import (a plain component file, no router) is now claimed too.
    assert registry.select("Button.tsx", b"import React from 'react';").name == "typescript-react"
    assert registry.select("App.tsx", b"const x = 1;").name == "typescript"  # non-React -> base
    registry.clear()


# ---- uiRole: class / function components -----------------------------------

COMPONENTS_SRC = b'''import React from 'react';

export class Panel extends React.Component {
  render() { return <div>{this.props.title}</div>; }
}

class Badge extends PureComponent {
  render() { return <span/>; }
}

export function Header() {
  return <header><h1>Hi</h1></header>;
}

const Card = ({ items }) => (
  <ul>{items.map((i) => <li key={i}>{i}</li>)}</ul>
);

export function formatDate(d) {
  return d.toISOString();
}

function Compute() {
  return 2 + 2;
}

const useThing = () => {
  return <div/>;
};
'''


def test_component_ui_roles(tmp_path) -> None:
    rec = _parse(tmp_path, COMPONENTS_SRC, "components.tsx")
    # Three orthogonal axes: framework identity, source language, per-node uiRole.
    assert rec.framework == "react" and rec.language == "typescript"
    class_roles = {c.name: c.uiRole for c in rec.classes}
    fn_roles = {f.name: f.uiRole for f in rec.functions}
    # Class components via `extends`.
    assert class_roles["Panel"] == "component"      # React.Component
    assert class_roles["Badge"] == "component"       # bare PureComponent
    # Function components: PascalCase + renders JSX (declaration + arrow, incl. .map JSX).
    assert fn_roles["Header"] == "component"
    assert fn_roles["Card"] == "component"
    # PascalCase utils with no JSX -> not marked; lowercase util -> not marked.
    assert fn_roles["Compute"] is None
    assert fn_roles["formatDate"] is None
    # `useThing` is camelCase (not a component) AND calls no React hook primitive (the file
    # only does a default `import React`, no named hook import) -> not a hook either.
    assert fn_roles["useThing"] is None


def test_component_ui_roles_without_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, COMPONENTS_SRC, "components.tsx", capture=False)
    assert {c.name: c.uiRole for c in rec.classes}["Panel"] == "component"
    assert {f.name: f.uiRole for f in rec.functions}["Header"] == "component"


# ---- uiRole: custom hooks (useX + a React hook primitive) -------------------

HOOKS_SRC = b'''import { useState, useEffect, useContext as useCtx } from 'react';
import { useAuth } from './auth';

export function useCounter(initial) {
  const [n, setN] = useState(initial);
  useEffect(() => { document.title = `${n}`; }, [n]);
  return { n, inc: () => setN(n + 1) };
}

const useTheme = () => {
  const theme = useCtx(ThemeContext);   // aliased react import still resolves
  return theme;
};

export function useCache(key) {          // useX name, but touches no React primitive
  return cacheStore.get(key);
}

export function useCurrentUser() {       // real hook, but only wraps a *custom* hook
  const { user } = useAuth();            // -> honest null (recoverable via calls[])
  return user;
}
'''


def test_hook_ui_roles(tmp_path) -> None:
    rec = _parse(tmp_path, HOOKS_SRC, "hooks.tsx")
    roles = {f.name: f.uiRole for f in rec.functions}
    # useX + a React hook primitive (direct, and via an aliased import) -> hook.
    assert roles["useCounter"] == "hook"
    assert roles["useTheme"] == "hook"
    # useX name but no primitive call -> not a hook (the false positive we avoid).
    assert roles["useCache"] is None
    # A hook that only wraps another custom hook -> honest null (recoverable, not guessed).
    assert roles["useCurrentUser"] is None


def test_hooks_and_components_dont_collide(tmp_path) -> None:
    # The useX vs PascalCase name split keeps the two roles disjoint: a PascalCase component
    # that calls useState is a component, never a hook.
    src = b"import { useState } from 'react';\nexport function Panel() { const [x] = useState(0); return <div>{x}</div>; }\n"
    rec = _parse(tmp_path, src, "Panel.tsx")
    assert {f.name: f.uiRole for f in rec.functions}["Panel"] == "component"


def test_jsx_file_is_javascript_language(tmp_path) -> None:
    # The JS/TS distinction lives on the `language` axis, orthogonal to framework: a .jsx
    # React file is (framework=react, language=javascript); a .tsx is (react, typescript).
    src = b"import React from 'react';\nexport function Widget() { return <div/>; }\n"
    rec = _parse(tmp_path, src, "Widget.jsx")
    assert rec.framework == "react" and rec.language == "javascript"
    assert {f.name: f.uiRole for f in rec.functions}["Widget"] == "component"


def test_navigation_objects_with_path_are_not_routes(tmp_path) -> None:
    src = b'''import { createBrowserRouter } from "react-router-dom";

const navigation = [
  { path: "/home", label: "Home" },
  { path: "/projects", title: "Projects" },
  { path: "/settings", name: "Settings", icon: "settings" },
  { path: "/help", href: "/help" },
];

const router = createBrowserRouter([
  { path: "/", element: <Home /> },
]);
'''

    rec = _parse(tmp_path, src, "App.tsx")

    routes = [
        s.endpoint
        for s in rec.statements
        if s.semanticType == "route"
    ]

    assert routes == ["/"]


def test_valid_react_router_config_is_detected(tmp_path) -> None:
    src = b'''import { useRoutes } from "react-router-dom";

const routes = useRoutes([
  { path: "/component", Component: ComponentPage },
  { path: "/lazy", lazy: loadPage },
  { path: "/loader", loader: loadData },
  { path: "/children", children: [] },
  { path: "/action", action: handleAction },
  { path: "/error", errorElement: <ErrorPage /> },
  { path: "/handle", handle: {} },
  { path: "/case", caseSensitive: true },
]);

'''

    rec = _parse(tmp_path, src, "routes.tsx")

    endpoints = {
        s.endpoint
        for s in rec.statements
        if s.semanticType == "route"
    }

    assert endpoints == {
        "/component",
        "/lazy",
        "/loader",
        "/children",
        "/action",
        "/error",
        "/handle",
        "/case",
    }


def test_config_routes_require_react_router_reference(tmp_path) -> None:
    src = b'''const routes = [
  { path: "/home", element: <Home /> },
];
'''

    rec = _parse(tmp_path, src, "routes.tsx")

    assert [
        s for s in rec.statements
        if s.semanticType == "route"
    ] == []


def test_jsx_routes_preserve_actual_node_type(tmp_path) -> None:
    src = b'''import { Routes, Route } from "react-router-dom";

<Routes>
  <Route path="/" element={<Home />} />
  <Route path="/users">
    <Users />
  </Route>
  <Route path="/users/:id" element={<UserDetail />} />
</Routes>
'''

    rec = _parse(tmp_path, src, "App.tsx")

    routes = {
        s.endpoint: s
        for s in rec.statements
        if s.semanticType == "route"
    }

    assert routes["/"].nodeType == "jsx_self_closing_element"
    assert routes["/users"].nodeType == "jsx_element"
    assert routes["/users/:id"].nodeType == "jsx_self_closing_element"

    assert all(
        route.nodeType != "synthetic"
        for route in routes.values()
    )



def test_nav_item_mixed_into_route_array_is_not_a_route(tmp_path) -> None:
    src = b'''import { createBrowserRouter } from "react-router-dom";
export const router = createBrowserRouter([
  { path: "/", element: <Home />, children: [
    { path: "team", element: <Team /> },
    { path: "crumb", label: "Crumb" },
  ]},
  { path: "/nav", label: "Nav" },
]);
'''
    rec = _parse(tmp_path, src, "router.tsx")
    eps = [s.endpoint for s in rec.statements if s.semanticType == "route"]
    assert eps == ["/", "/team"]


def test_jsx_mixed_forms_report_real_node_type_per_record(tmp_path) -> None:
    src = b'''import { Route } from "react-router-dom";
const app = (
  <Route path="/a" element={<A />}>
    <Route path="b" element={<B />} />
    <Route index element={<C />} />
  </Route>
);
'''
    rec = _parse(tmp_path, src, "App.tsx")
    types = {s.endpoint + s.handler: s.nodeType for s in rec.statements if s.semanticType == "route"}
    assert types == {"/aA": "jsx_element", "/a/bB": "jsx_self_closing_element",
                     "/aC": "jsx_self_closing_element"}
