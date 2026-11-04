"""Next.js App Router parser: route-handler detection off the record, endpoint derivation
from the file path, base reuse, gating, fixture exclusion, and selection."""

from __future__ import annotations

import pytest

import json

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.parsers.typescript_nextjs.routes import (
    endpoint_from_path,
    is_app_router_route_file,
    is_pages_api_file,
    is_route_file,
    pages_api_endpoint,
)
from breezeai_cog.schemas import FileRecord

# The two handler forms Next.js accepts: a function declaration and a const arrow.
FUNC_SRC = b"""import { NextResponse } from 'next/server';

export async function GET(req) {
  return NextResponse.json({ ok: true });
}

export async function POST(req) {
  return new Response('created', { status: 201 });
}
"""

ARROW_SRC = b"""export const GET = async (req) => {
  return Response.json([]);
};
export const DELETE = (req) => new Response(null, { status: 204 });
"""

# Pages Router API handlers: named default, and anonymous-arrow default.
PAGES_NAMED_SRC = b"""import type { NextApiRequest, NextApiResponse } from 'next';

export default function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method === 'POST') return res.status(201).json({});
  return res.status(200).json([]);
}
"""

PAGES_ANON_SRC = b"""export default async (req, res) => {
  res.status(200).end();
};
"""


def _parse(tmp_path, src: bytes, rel: str, *, capture=True) -> FileRecord:
    p = tmp_path / "f.ts"  # on-disk name is irrelevant; the route identity is ``rel``
    p.write_bytes(src)
    ctx = ParseContext(
        path=rel, abs_path=p, source=src, repo_root=tmp_path, capture_statements=capture
    )
    return TypeScriptParser().parse_file(ctx)


def test_endpoint_from_path() -> None:
    assert endpoint_from_path("app/route.ts") == "/"
    assert endpoint_from_path("app/users/route.ts") == "/users"
    assert endpoint_from_path("src/app/users/[id]/route.ts") == "/users/[id]"
    assert endpoint_from_path("app/blog/[...slug]/route.ts") == "/blog/[...slug]"
    # route groups (parens) and parallel slots (@) drop out of the URL
    assert endpoint_from_path("app/(admin)/stats/route.ts") == "/stats"
    assert endpoint_from_path("app/@modal/photo/route.ts") == "/photo"
    # nested monorepo app dir
    assert endpoint_from_path("apps/web/app/api/health/route.ts") == "/api/health"


def test_is_app_router_route_file() -> None:
    assert is_app_router_route_file("app/users/route.ts")
    assert is_app_router_route_file("src/app/route.tsx")
    assert not is_app_router_route_file("app/users/page.tsx")  # UI, not a verb handler file
    assert not is_app_router_route_file("lib/route.ts")  # not under app/
    assert not is_app_router_route_file("app/users/service.ts")


def test_is_pages_api_file() -> None:
    assert is_pages_api_file("pages/api/users.ts")
    assert is_pages_api_file("src/pages/api/users/[id].ts")
    assert not is_pages_api_file("pages/about.tsx")  # UI page, not under pages/api
    assert not is_pages_api_file("pages/apiary/x.ts")  # 'api' must be the segment after pages
    assert not is_pages_api_file("app/api/health/route.ts")  # App Router, not Pages Router


def test_pages_api_endpoint() -> None:
    assert pages_api_endpoint("pages/api/users.ts") == "/api/users"
    assert pages_api_endpoint("pages/api/users/[id].ts") == "/api/users/[id]"
    assert pages_api_endpoint("pages/api/users/index.ts") == "/api/users"  # index collapses
    assert pages_api_endpoint("pages/api/index.ts") == "/api"
    assert pages_api_endpoint("src/pages/api/webhooks/[...all].ts") == "/api/webhooks/[...all]"


def test_routes_require_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, FUNC_SRC, "app/users/route.ts", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_function_handlers_detected(tmp_path) -> None:
    rec = _parse(tmp_path, FUNC_SRC, "app/users/[id]/route.ts")
    routes = {s.method: s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {"GET", "POST"}
    assert all(r.endpoint == "/users/[id]" for r in routes.values())
    assert all(r.framework == "nextjs" and r.routeKind == "route" for r in routes.values())
    assert routes["GET"].handler == "GET"
    # parented to the real extracted handler function (off-the-record id match)
    fn_ids = {f.id for f in rec.functions}
    assert all(r.parentId in fn_ids for r in routes.values())
    assert rec.framework == "nextjs"


def test_arrow_handlers_detected_and_parented(tmp_path) -> None:
    rec = _parse(tmp_path, ARROW_SRC, "app/items/route.ts")
    routes = {s.method: s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {"GET", "DELETE"}
    assert all(r.endpoint == "/items" for r in routes.values())
    fn_ids = {f.id for f in rec.functions}
    assert all(r.parentId in fn_ids for r in routes.values()), (routes, fn_ids)


def test_pages_router_named_handler(tmp_path) -> None:
    rec = _parse(tmp_path, PAGES_NAMED_SRC, "pages/api/users/[id].ts")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    r = routes[0]
    # one endpoint per file; handler declares no verb → ANY
    assert (r.method, r.endpoint, r.framework, r.routeKind) == (
        "ANY",
        "/api/users/[id]",
        "nextjs",
        "route",
    )
    assert r.handler == "handler"
    # parented to the real extracted handler function (off-the-record id match)
    assert r.parentId in {f.id for f in rec.functions}
    assert rec.framework == "nextjs"


def test_pages_router_anonymous_handler_parents_to_file(tmp_path) -> None:
    rec = _parse(tmp_path, PAGES_ANON_SRC, "pages/api/health.ts")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    r = routes[0]
    assert (r.method, r.endpoint) == ("ANY", "/api/health")
    assert r.handler is None  # anonymous default export — honest-null, no handler name
    assert r.parentId == rec.id  # no Function node → parents to the file


def test_pages_router_requires_default_export(tmp_path) -> None:
    # A pages/api file exporting only named symbols (a helper module) is not a handler.
    src = b"export function util() { return 1; }\nexport const config = {};\n"
    rec = _parse(tmp_path, src, "pages/api/_shared.ts")
    assert [s for s in rec.statements if s.semanticType == "route"] == []


def test_pages_router_object_default_is_not_a_route(tmp_path) -> None:
    # `export default { ... }` (config object, not a handler) must emit no route.
    rec = _parse(tmp_path, b"export default { runtime: 'edge' };\n", "pages/api/cfg.ts")
    assert [s for s in rec.statements if s.semanticType == "route"] == []


def test_pages_router_ref_to_non_function_is_not_a_route(tmp_path) -> None:
    # `export default x` where x is an object/const (not a function) must emit no route —
    # the identifier only counts when it resolves to an extracted function.
    rec = _parse(tmp_path, b"const x = { a: 1 };\nexport default x;\n", "pages/api/cfg.ts")
    assert [s for s in rec.statements if s.semanticType == "route"] == []


def test_pages_router_ref_to_function_is_a_route(tmp_path) -> None:
    # `export default handler` where handler IS a defined function → a real route.
    rec = _parse(
        tmp_path, b"function handler(req, res) {}\nexport default handler;\n", "pages/api/ok.ts"
    )
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].handler == "handler"
    assert routes[0].parentId in {f.id for f in rec.functions}


def test_pages_router_requires_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, PAGES_NAMED_SRC, "pages/api/users/[id].ts", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_non_exported_verb_is_not_a_route(tmp_path) -> None:
    # A non-exported `function GET` is not a Next.js handler — must not be emitted.
    src = b"function GET(req) { return null; }\nexport const POST = async () => new Response();\n"
    rec = _parse(tmp_path, src, "app/x/route.ts")
    methods = {s.method for s in rec.statements if s.semanticType == "route"}
    assert methods == {"POST"}


def test_base_extraction_reused(tmp_path) -> None:
    rec = _parse(tmp_path, FUNC_SRC, "app/users/route.ts")
    assert {"GET", "POST"} <= {f.name for f in rec.functions}
    assert rec.language == "typescript"


def test_output_validates(tmp_path) -> None:
    for src, rel in (
        (FUNC_SRC, "app/users/[id]/route.ts"),
        (ARROW_SRC, "app/items/route.ts"),
        (PAGES_NAMED_SRC, "pages/api/users/[id].ts"),
        (PAGES_ANON_SRC, "pages/api/health.ts"),
    ):
        rec = _parse(tmp_path, src, rel)
        errors = list(
            Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
                json.loads(to_line(rec))
            )
        )
        assert not errors, errors


def test_route_file_gate() -> None:
    # The detector's gate: the path, then the handler's export shape.
    assert is_route_file("app/users/route.ts", b"export async function GET(){}")
    assert is_route_file("pages/api/users.ts", b"export default function handler(){}")
    assert not is_route_file("app/users/page.tsx", b"export default function P(){}")
    assert not is_route_file("app/users/route.ts", b"export const config = {};")
    assert not is_route_file("pages/about.tsx", b"export default function About(){}")


def test_routes_are_found_whatever_parser_owns_the_file(analyze_files) -> None:
    # Additive: another parser owning a route file must not lose its routes.
    st = analyze_files({
        "package.json": '{"dependencies": {"next": "14"}}',
        "app/api/graphql/route.ts": (  # Apollo on Next.js: the GraphQL parser owns it
            "import { ApolloServer } from '@apollo/server';\n"
            "import { startServerAndCreateNextHandler } from '@as-integrations/next';\n"
            "import { gql } from 'graphql-tag';\n"
            "const typeDefs = gql`type Query { hello: String }`;\n"
            "const handler = startServerAndCreateNextHandler(new ApolloServer({ typeDefs }));\n"
            "export async function GET(req) { return handler(req); }\n"
            "export async function POST(req) { return handler(req); }\n"
        ),
        "pages/api/graphql.ts": (  # apollo-server-micro: the GraphQL parser owns it
            "import { ApolloServer, gql } from 'apollo-server-micro';\n"
            "const typeDefs = gql`type Query { hello: String }`;\n"
            "const server = new ApolloServer({ typeDefs });\n"
            "export default function handler(req, res) { return server.createHandler()(req, res); }\n"
        ),
        "app/api/orders/route.ts": (  # a @nestjs client: the NestJS parser owns it
            "import { ClientProxyFactory } from '@nestjs/microservices';\n"
            "export async function POST() { return Response.json({}); }\n"
        ),
    })
    got = {(s.path, s.method, s.endpoint) for s in st
           if s.semanticType == "route" and s.framework == "nextjs"}
    assert got == {
        ("app/api/graphql/route.ts", "GET", "/api/graphql"),
        ("app/api/graphql/route.ts", "POST", "/api/graphql"),
        ("pages/api/graphql.ts", "ANY", "/api/graphql"),
        ("app/api/orders/route.ts", "POST", "/api/orders"),
    }


# ── next.config basePath (app-level wiring) ───────────────────────────────────────────────────
_NEXT_PKG = '{"dependencies": {"next": "14"}}'
_ROUTE = "export async function GET() {\n  return Response.json({});\n}\n"
_PAGES_API = "export default function handler(req, res) {\n  res.json([]);\n}\n"


def _next_endpoints(analyze_files, files: dict[str, str]) -> dict[str, str | None]:
    return {
        s.path: s.endpoint
        for s in analyze_files({"package.json": _NEXT_PKG, **files})
        if s.semanticType == "route" and s.framework == "nextjs"
    }


def test_base_path_prefixes_app_and_pages_api_routes(analyze_files) -> None:
    got = _next_endpoints(analyze_files, {
        "next.config.mjs": "const nextConfig = { basePath: '/docs', reactStrictMode: true };\n"
                           "export default nextConfig;\n",
        "app/users/[id]/route.ts": _ROUTE,
        "pages/api/health.ts": _PAGES_API,
    })
    assert got == {"app/users/[id]/route.ts": "/docs/users/[id]", "pages/api/health.ts": "/docs/api/health"}


def test_base_path_per_app_in_a_monorepo(analyze_files) -> None:
    got = _next_endpoints(analyze_files, {
        "apps/web/next.config.js": "module.exports = { basePath: '/web' };\n",
        "apps/web/app/ping/route.ts": _ROUTE,
        "apps/admin/next.config.js": "module.exports = { reactStrictMode: true };\n",
        "apps/admin/app/ping/route.ts": _ROUTE,
    })
    assert got == {"apps/web/app/ping/route.ts": "/web/ping", "apps/admin/app/ping/route.ts": "/ping"}


def test_unresolvable_base_path_leaves_endpoint_empty(analyze_files) -> None:
    # Set from the environment / a conditional: the address is unknown (contract §8, decision Q8).
    for cfg in ("module.exports = { basePath: process.env.BASE_PATH };\n",
                "module.exports = { basePath: isProd ? '/docs' : '' };\n"):
        got = _next_endpoints(analyze_files, {"next.config.js": cfg, "app/users/route.ts": _ROUTE})
        assert got == {"app/users/route.ts": None}, cfg


def test_base_path_from_a_constant(analyze_files) -> None:
    got = _next_endpoints(analyze_files, {
        "config.ts": "export const BASE = '/shop';\n",
        "next.config.ts": "import { BASE } from './config';\nexport default { basePath: BASE };\n",
        "app/cart/route.ts": _ROUTE,
    })
    assert got == {"app/cart/route.ts": "/shop/cart"}


def _next_guards(analyze_files, files: dict[str, str]) -> dict[str, list[str] | None]:
    return {
        s.endpoint: s.guards
        for s in analyze_files({"package.json": _NEXT_PKG, **files})
        if s.semanticType == "route" and s.framework == "nextjs"
    }


_MW = "import { NextResponse } from 'next/server';\nexport function middleware(req) {\n  return NextResponse.next();\n}\n"
_GET = "export async function GET() {\n  return Response.json([]);\n}\n"


def test_middleware_guards_matched_routes(analyze_files) -> None:
    guards = _next_guards(analyze_files, {
        "next.config.js": "module.exports = {};\n",
        "src/middleware.ts": _MW + "export const config = { matcher: ['/api/:path*', '/admin/:id'] };\n",
        "src/app/api/users/[id]/route.ts": _GET,     # under /api/:path* → covered
        "src/app/admin/[slug]/route.ts": _GET,       # :id covers one dynamic segment
        "src/app/admin/[slug]/x/route.ts": _GET,     # one segment too many → not covered
        "src/app/public/route.ts": _GET,             # not matched
    })
    assert guards == {
        "/api/users/[id]": ["middleware"],
        "/admin/[slug]": ["middleware"],
        "/admin/[slug]/x": None,
        "/public": None,
    }


def test_middleware_without_matcher_guards_every_route(analyze_files) -> None:
    guards = _next_guards(analyze_files, {
        "middleware.ts": _MW,
        "app/a/route.ts": _GET,
        "pages/api/b.ts": "export default function handler(req, res) { res.json([]); }\n",
    })
    assert guards == {"/a": ["middleware"], "/api/b": ["middleware"]}


def test_middleware_uncertain_coverage_adds_nothing(analyze_files) -> None:
    guards = _next_guards(analyze_files, {
        # a regex-like matcher, a literal segment over a dynamic one, a has-condition source
        "middleware.ts": _MW + "export const config = { matcher: ['/((?!_next).*)', '/users/me',"
        " { source: '/orders/:p*', has: [{ type: 'header', key: 'x' }] }] };\n",
        "app/users/[id]/route.ts": _GET,
        "app/orders/route.ts": _GET,
        # another app (its own next.config) is not covered by the root middleware
        "apps/web/next.config.js": "module.exports = {};\n",
        "apps/web/app/x/route.ts": _GET,
    })
    assert guards == {"/users/[id]": None, "/orders": None, "/x": None}


# ── Pages (App Router page.tsx, Pages Router pages/**) ────────────────────────────────────────
_NEXT_PKG = '{"dependencies": {"next": "14", "react": "18"}}'


def _pages(analyze_files, files: dict[str, str], pkg: str = _NEXT_PKG):
    st = analyze_files({"package.json": pkg, **files})
    return {s.endpoint: (s.handler, s.handlerLine, s.path)
            for s in st if s.semanticType == "route" and s.routeKind == "page"}


def test_app_and_pages_router_pages(analyze_files) -> None:
    got = _pages(analyze_files, {
        "src/app/(shop)/orders/[id]/page.tsx": (
            "import React from 'react';\nexport default function OrderPage() {\n  return <div/>;\n}\n"
        ),
        "src/app/page.tsx": "export default () => <main/>;\n",
        "src/app/dashboard/layout.tsx": "export default function L({ children }) { return children; }\n",
        "src/app/_lib/page.tsx": "export default function Hidden() { return null; }\n",
        "src/app/@modal/(.)photo/page.tsx": "export default function Modal() { return null; }\n",
        "pages/about.tsx": (
            "import { memo } from 'react';\nfunction About() { return <p/>; }\nexport default memo(About);\n"
        ),
        "pages/blog/index.tsx": "export default class Blog { render() { return null; } }\n",
        "pages/_app.tsx": "export default function App({ Component }) { return <Component/>; }\n",
        "pages/404.tsx": "export default function NotFound() { return null; }\n",
        "pages/api/users.ts": "export default function handler(req, res) { res.json([]); }\n",
    })
    assert got == {
        "/orders/[id]": ("OrderPage", 2, "src/app/(shop)/orders/[id]/page.tsx"),
        "/": (None, 1, "src/app/page.tsx"),
        "/about": ("About", 2, "pages/about.tsx"),
        "/blog": ("Blog", 1, "pages/blog/index.tsx"),
    }


def test_pages_need_a_next_app(analyze_files) -> None:
    # Gatsby also routes src/pages/*.tsx — without a `next` dependency (or next.config) no page.
    got = _pages(analyze_files, {
        "src/pages/about.tsx": "export default function About() { return null; }\n",
    }, pkg='{"dependencies": {"gatsby": "5", "react": "18"}}')
    assert got == {}


def test_page_gets_base_path_and_middleware_guard(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEXT_PKG,
        "next.config.js": "module.exports = { basePath: '/shop' };\n",
        "middleware.ts": (
            "export function middleware(req) { return; }\n"
            "export const config = { matcher: ['/account/:path*'] };\n"
        ),
        "app/account/page.tsx": "export default function Account() { return null; }\n",
        "app/help/page.tsx": "export default function Help() { return null; }\n",
    })
    got = {s.endpoint: s.guards for s in st if s.routeKind == "page"}
    assert got == {"/shop/account": ["middleware"], "/shop/help": None}


def test_parallel_route_slots_are_not_pages(analyze_files) -> None:
    # A slot (@x) is drawn inside its parent's layout; only the parent's page is a URL.
    got = _pages(analyze_files, {
        "app/dashboard/page.tsx": "export default function Dashboard() { return null; }\n",
        "app/dashboard/@team/page.tsx": "export default function TeamSlot() { return null; }\n",
        "app/dashboard/@analytics/page.tsx": "export default function AnalyticsSlot() { return null; }\n",
        "app/dashboard/@team/members/page.tsx": "export default function Members() { return null; }\n",
    })
    assert got == {"/dashboard": ("Dashboard", 1, "app/dashboard/page.tsx")}


def test_records_report_the_node_they_stand_for(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEXT_PKG,
        "app/dashboard/page.tsx": "export default function Dashboard() {\n  return null;\n}\n",
        "app/api/orders/route.ts": "export async function GET() {\n  return Response.json([]);\n}\n",
        "app/api/items/route.ts": "export const POST = async (req) => {\n  return Response.json({});\n};\n",
        "pages/api/hello.ts": "export default function handler(req, res) {\n  res.json([]);\n}\n",
        "pages/api/ref.ts": (
            "async function handler(req, res) {\n  res.json([]);\n}\n\nexport default handler;\n"
        ),
        "pages/api/anon.ts": "export default (req, res) => {\n  res.end();\n};\n",
    })
    got = {s.endpoint: (s.nodeType, s.text, s.startLine) for s in st if s.semanticType == "route"}
    assert got == {
        "/dashboard": ("export_statement", "export default function Dashboard() {", 1),
        "/api/orders": ("export_statement", "export async function GET() {", 1),
        "/api/items": ("export_statement", "export const POST = async (req) => {", 1),
        "/api/hello": ("export_statement", "export default function handler(req, res) {", 1),
        # a referenced handler is recorded at its declaration
        "/api/ref": ("function_declaration", "async function handler(req, res) {", 1),
        "/api/anon": ("export_statement", "export default (req, res) => {", 1),
    }


def test_a_handler_declaration_that_is_an_api_call_keeps_it(analyze_files) -> None:
    # `sdk-calls` classified the one-line declaration as the outbound call; the route is a
    # record beside it, at the same node, not an overwrite of it.
    st = analyze_files({
        "package.json": _NEXT_PKG,
        "pages/api/proxy.ts": (
            "const handler = async (req, res) =>\n"
            "  res.json(await (await fetch('https://api.example.com/b')).json());\n"
            "export default handler;\n"
        ),
    })
    got = sorted((s.semanticType, s.nodeType, s.startLine, s.endpoint)
                 for s in st if s.semanticType in ("route", "api_call"))
    assert got == [
        ("api_call", "lexical_declaration", 1, "https://api.example.com/b"),
        ("route", "lexical_declaration", 1, "/api/proxy"),
    ]


_NEXT16_PKG = '{"dependencies": {"next": "^16.0.0", "react": "19"}}'
_PROXY = (
    "import { NextResponse } from 'next/server';\n"
    "export function proxy(req) { return NextResponse.next(); }\n"
    "export const config = { matcher: ['/dashboard/:path*'] };\n"
)


def test_next16_proxy_is_a_guard_named_proxy(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEXT16_PKG,
        "proxy.ts": _PROXY,
        "app/page.tsx": "export default function Home() { return null; }\n",
        "app/dashboard/page.tsx": "export default function Dashboard() { return null; }\n",
    })
    got = {s.endpoint: (s.guards, s.authRequired) for s in st if s.routeKind == "page"}
    # a proxy may only rewrite or log: it guards, but asserts no authentication
    assert got == {"/dashboard": (["proxy"], None), "/": (None, None)}


def test_proxy_helper_below_the_app_root_is_not_a_guard(analyze_files) -> None:
    # `app/api/proxy.ts` is a fetch helper, not the app's interceptor: only the file at an app's
    # root (or its `src/`) is one.
    st = analyze_files({
        "package.json": _NEXT16_PKG,
        "app/api/proxy.ts": "export async function proxy(req) { return fetch(req.url); }\n",
        "app/api/orders/route.ts": "export async function GET() { return Response.json([]); }\n",
    })
    assert [s.guards for s in st if s.semanticType == "route"] == [None]


def test_proxy_with_an_unknown_next_version_is_not_a_guard(analyze_files) -> None:
    # A workspace link pins no version: before Next.js 16 `proxy.ts` is an ordinary module.
    st = analyze_files({
        "package.json": '{"dependencies": {"next": "workspace:*"}}',
        "proxy.ts": _PROXY,
        "app/dashboard/page.tsx": "export default function Dashboard() { return null; }\n",
    })
    assert [s.guards for s in st if s.routeKind == "page"] == [None]


def test_middleware_in_src_of_a_monorepo_app_is_a_guard(analyze_files) -> None:
    st = analyze_files({
        "package.json": '{"private": true, "workspaces": ["apps/*"]}',
        "apps/web/package.json": '{"dependencies": {"next": "14"}}',
        "apps/web/src/middleware.ts": "export function middleware(req) { return; }\n",
        "apps/web/src/app/dashboard/page.tsx": "export default function D() { return null; }\n",
    })
    assert [s.guards for s in st if s.routeKind == "page"] == [["middleware"]]


def test_proxy_file_without_its_export_is_not_a_guard(analyze_files) -> None:
    # `proxy.ts` must export `proxy` (or a default); a `middleware` export there is not one.
    st = analyze_files({
        "package.json": _NEXT16_PKG,
        "proxy.ts": "export function middleware(req) { return; }\n",
        "app/dashboard/page.tsx": "export default function Dashboard() { return null; }\n",
    })
    assert [s.guards for s in st if s.routeKind == "page"] == [None]


def test_middleware_and_proxy_in_one_app_cover_nothing(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEXT16_PKG,
        "proxy.ts": _PROXY,
        "middleware.ts": "export function middleware(req) { return; }\n",
        "app/dashboard/page.tsx": "export default function Dashboard() { return null; }\n",
    })
    assert [s.guards for s in st if s.routeKind == "page"] == [None]


def test_pages_api_endpoint_with_page_extensions() -> None:
    exts = ("page.tsx", "api.ts")
    assert pages_api_endpoint("pages/api/hello.api.ts", exts) == "/api/hello"
    assert pages_api_endpoint("pages/api/util.ts", exts) is None  # not an api.ts file
    assert pages_api_endpoint("pages/api/hello.api.ts", None) is None  # unknown extensions


def test_page_extensions_decide_which_files_are_pages(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEXT_PKG,
        "next.config.js": "module.exports = { pageExtensions: ['page.tsx', 'api.ts'] };\n",
        "pages/index.page.tsx": "export default function Home() { return null; }\n",
        "pages/orders/OrderRow.tsx": "export default function OrderRow() { return null; }\n",
        "pages/orders/[id].page.tsx": "export default function Order() { return null; }\n",
        "pages/_app.page.tsx": "export default function App({ Component }) { return null; }\n",
        "pages/api/hello.api.ts": "export default function handler(req, res) { res.json([]); }\n",
        "pages/api/util.ts": "export default function helper(req, res) { res.json([]); }\n",
    })
    got = {(s.routeKind, s.endpoint, s.handler) for s in st if s.semanticType == "route"}
    assert got == {("page", "/", "Home"), ("page", "/orders/[id]", "Order"),
                   ("route", "/api/hello", "handler")}


def test_unknown_page_extensions_give_no_page_or_route_records(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEXT_PKG,
        "next.config.js": (
            "const exts = ['page.tsx'];\nmodule.exports = { pageExtensions: [...exts, 'mdx'] };\n"
        ),
        "pages/index.page.tsx": "export default function Home() { return null; }\n",
        "pages/api/hello.ts": "export default function handler(req, res) { res.json([]); }\n",
        "app/about/page.tsx": "export default function About() { return null; }\n",
    })
    got = {(s.routeKind, s.endpoint) for s in st if s.semanticType == "route"}
    assert got == set()  # which App Router files are pages is unknown too


def test_dotted_route_and_page_helpers_are_not_routes(analyze_files) -> None:
    # Without pageExtensions, only `route.<ext>` / `page.<ext>` count: a `route.helpers.ts`
    # beside a route is a module of it, not a second route.
    st = analyze_files({
        "package.json": _NEXT_PKG,
        "app/api/x/route.ts": "export async function GET() { return Response.json(1); }\n",
        "app/api/x/route.helpers.ts": "export async function GET() { return 1; }\n",
        "app/about/page.tsx": "export default function About() { return null; }\n",
        "app/about/page.helpers.tsx": "export default function Helper() { return null; }\n",
    })
    got = {(s.path, s.endpoint) for s in st if s.semanticType == "route"}
    assert got == {("app/api/x/route.ts", "/api/x"), ("app/about/page.tsx", "/about")}


def test_page_extensions_apply_per_app(analyze_files) -> None:
    # A nested app's config without pageExtensions keeps the default for its own pages.
    st = analyze_files({
        "package.json": _NEXT_PKG,
        "next.config.js": "module.exports = { pageExtensions: ['page.tsx'] };\n",
        "pages/index.page.tsx": "export default function Home() { return null; }\n",
        "apps/web/next.config.js": "module.exports = {};\n",
        "apps/web/package.json": _NEXT_PKG,
        "apps/web/pages/about.tsx": "export default function About() { return null; }\n",
    })
    got = {s.endpoint for s in st if s.routeKind == "page"}
    assert got == {"/", "/about"}


def test_referenced_pages_api_handler_is_one_statement(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEXT_PKG,
        "pages/api/a.ts": "const handler = async (req, res) => {\n  res.end();\n};\nexport default handler;\n",
    })
    on_line_1 = [s for s in st if s.path == "pages/api/a.ts" and s.startLine == 1]
    assert [(s.semanticType, s.nodeType) for s in on_line_1] == [("route", "lexical_declaration")]


def test_page_extensions_apply_to_the_app_router(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEXT_PKG,
        "next.config.js": "module.exports = { pageExtensions: ['page.tsx', 'api.ts'] };\n",
        "app/dashboard/page.tsx": "export default function Dashboard() { return null; }\n",
        "app/orders/page.page.tsx": "export default function Orders() { return null; }\n",
        "app/api/a/route.ts": "export async function GET() { return Response.json([]); }\n",
        "app/api/b/route.api.ts": "export async function GET() { return Response.json([]); }\n",
    })
    got = {(s.routeKind, s.endpoint) for s in st if s.semanticType == "route"}
    assert got == {("page", "/orders"), ("route", "/api/b")}


def test_proxy_file_before_next16_is_not_a_guard(analyze_files) -> None:
    st = analyze_files({
        "package.json": '{"dependencies": {"next": "^15.2.0", "react": "18"}}',
        "proxy.ts": _PROXY,
        "app/dashboard/page.tsx": "export default function Dashboard() { return null; }\n",
    })
    assert [s.guards for s in st if s.routeKind == "page"] == [None]


_PAGES_EXT_FILES = {
    "pages/index.page.tsx": "export default function Home() { return null; }\n",
    "pages/orders/OrderRow.tsx": "export default function OrderRow() { return null; }\n",
}


@pytest.mark.parametrize("config", [
    "const pageExtensions = ['page.tsx'];\nmodule.exports = { pageExtensions };\n",
    "const exts = ['page.tsx'];\nmodule.exports = { pageExtensions: exts };\n",
    "export const pageExtensions = ['page.tsx'];\nexport default { pageExtensions };\n",
])
def test_page_extensions_from_a_same_file_constant(analyze_files, config: str) -> None:
    st = analyze_files({"package.json": _NEXT_PKG, "next.config.mjs": config, **_PAGES_EXT_FILES})
    assert {(s.endpoint, s.handler) for s in st if s.routeKind == "page"} == {("/", "Home")}


@pytest.mark.parametrize("config", [
    "const cfg = {};\ncfg.pageExtensions = ['page.tsx'];\nmodule.exports = cfg;\n",
    "const cfg = {};\ncfg['pageExtensions'] = ['page.tsx'];\nmodule.exports = cfg;\n",
    "let pageExtensions = ['page.tsx'];\nmodule.exports = { pageExtensions };\n",
])
def test_page_extensions_set_at_run_time_are_unknown(analyze_files, config: str) -> None:
    # Which files are pages is unknown: no page records rather than components as pages.
    st = analyze_files({"package.json": _NEXT_PKG, "next.config.js": config, **_PAGES_EXT_FILES})
    assert [s for s in st if s.routeKind == "page"] == []


def _frameworks(tmp_path, files: dict[str, str]) -> dict[str, str | None]:
    from breezeai_cog import iter_file_records

    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return {r.path: r.framework for r in iter_file_records(tmp_path) if r.path in files}


def test_route_file_framework_is_nextjs_whatever_else_it_does(tmp_path) -> None:
    got = _frameworks(tmp_path, {
        "package.json": '{"dependencies": {"next": "15", "@aws-sdk/client-s3": "3"}}',
        # an S3 call: its detector runs first, but the file is a Next.js route
        "app/api/upload/route.ts": (
            "import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';\n"
            "const s3 = new S3Client({});\n"
            "export async function POST() {\n"
            "  await s3.send(new PutObjectCommand({ Bucket: 'b', Key: 'k' }));\n"
            "  return Response.json(1);\n}\n"
        ),
        # an OG-image route importing React: owned by the React parser, still a Next.js route
        "app/og/route.tsx": (
            "import React from 'react';\nimport { ImageResponse } from 'next/og';\n"
            "export async function GET() { return new ImageResponse(<div>hi</div>); }\n"
        ),
        # a page importing React stays a React file (as before)
        "app/about/page.tsx": "import React from 'react';\nexport default function About() { return <p/>; }\n",
    })
    assert got["app/api/upload/route.ts"] == "nextjs"
    assert got["app/og/route.tsx"] == "nextjs"
    assert got["app/about/page.tsx"] == "react"
