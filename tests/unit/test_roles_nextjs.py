"""Next.js role table (docs/entry-point-roles/nextjs.md): one test per row."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}
PKG = '{"dependencies": {"next": "14", "react": "18"}}'


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def _run(analyze_files, files: dict[str, str]):
    return analyze_files({"package.json": PKG, **files})


def test_app_router_verb_exports_are_bindings(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "app/users/[id]/route.ts": "export async function GET(req: Request) {\n  return Response.json({});\n}\n"
            "export const POST = async (req: Request) => {\n  return Response.json({});\n};\n"
        },
    )
    got = {(r.method, r.routeKind, r.endpoint, r.handler, r.handlerLine) for r in _eps(st)}
    assert got == {
        ("GET", "route", "/users/[id]", "GET", 1),
        ("POST", "route", "/users/[id]", "POST", 4),
    }


def test_pages_api_default_export_is_a_binding(analyze_files) -> None:
    st = _run(
        analyze_files,
        {"pages/api/users.ts": "export default function handler(req, res) {\n  res.json([]);\n}\n"},
    )
    [r] = _eps(st)
    assert (r.method, r.routeKind, r.endpoint, r.handler, r.handlerLine) == (
        "ANY",
        "route",
        "/api/users",
        "handler",
        1,
    )


def test_pages_api_anonymous_handler(analyze_files) -> None:
    st = _run(
        analyze_files,
        {"pages/api/ping.ts": "export default (req, res) => {\n  res.send('pong');\n};\n"},
    )
    [r] = _eps(st)
    assert (r.endpoint, r.handler, r.handlerLine) == ("/api/ping", None, 1)


def test_route_group_and_slot_are_wiring(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "app/(admin)/@panel/stats/route.ts": "export async function GET() {\n  return Response.json({});\n}\n"
        },
    )
    [r] = _eps(st)
    assert r.endpoint == "/stats"


def test_base_path_is_wiring(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "next.config.js": "module.exports = { basePath: '/docs' };\n",
            "app/users/route.ts": "export async function GET() {\n  return Response.json({});\n}\n",
        },
    )
    [r] = _eps(st)
    assert r.endpoint == "/docs/users"


@pytest.mark.xfail(strict=True, reason="App Router pages are not recorded")
def test_app_router_page_is_a_binding(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "app/settings/profile/page.tsx": "export default function ProfilePage() {\n  return <h1>Profile</h1>;\n}\n"
        },
    )
    got = {(r.routeKind, r.endpoint, r.handler) for r in _eps(st)}
    assert got == {("page", "/settings/profile", "ProfilePage")}


@pytest.mark.xfail(strict=True, reason="Pages Router pages are not recorded")
def test_pages_router_page_is_a_binding(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "pages/orders/[id].tsx": "export default function OrderPage() {\n  return <h1>Order</h1>;\n}\n"
        },
    )
    got = {(r.routeKind, r.endpoint, r.handler) for r in _eps(st)}
    assert got == {("page", "/orders/[id]", "OrderPage")}


def test_layout_is_wiring(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "app/layout.tsx": "export default function RootLayout({ children }) {\n  return <main>{children}</main>;\n}\n"
        },
    )
    assert _eps(st) == []


def test_middleware_has_no_record(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "middleware.ts": "import { NextResponse } from 'next/server';\n"
            "export function middleware(req) {\n  return NextResponse.next();\n}\n"
            "export const config = { matcher: '/api/:path*' };\n"
        },
    )
    assert _eps(st) == []


def test_middleware_is_a_guard(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "middleware.ts": "import { NextResponse } from 'next/server';\n"
            "export function middleware(req) {\n  return NextResponse.next();\n}\n"
            "export const config = { matcher: '/api/:path*' };\n",
            "pages/api/users.ts": "export default function handler(req, res) {\n  res.json([]);\n}\n",
        },
    )
    [r] = _eps(st)
    assert r.guards == ["middleware"]


def test_fetch_is_outbound(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "app/users/route.ts": "export async function GET() {\n  const r = await fetch('https://example.com/users');\n"
            "  return Response.json(await r.json());\n}\n"
        },
    )
    assert [s.endpoint for s in st if s.semanticType == "api_call"] == ["https://example.com/users"]
