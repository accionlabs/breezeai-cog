"""React framework parser: JSX + config route detection, nesting, base reuse, override."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
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
    assert rec.framework is None


def test_jsx_routes_detected_and_nested(tmp_path) -> None:
    rec = _parse(tmp_path, JSX_SRC, "App.tsx")
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {"/", "/users", "/users/:id"}  # nested path joined onto parent
    assert routes["/users/:id"].handler == "UserDetail"
    assert routes["/"].handler == "Home"
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


def test_claims_selects_react() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(ReactParser())
    assert registry.select("App.tsx", b"import { Route } from 'react-router-dom';").name == "typescript-react"
    assert registry.select("App.tsx", b"const x = 1;").name == "typescript"  # plain TS/React -> base
    registry.clear()
