"""Next.js App Router parser: route-handler detection off the record, endpoint derivation
from the file path, base reuse, gating, fixture exclusion, and selection."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_nextjs.parser import NextJSParser
from breezeai_cog.parsers.typescript_nextjs.routes import (
    endpoint_from_path,
    is_app_router_route_file,
    is_pages_api_file,
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
    return NextJSParser().parse_file(ctx)


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


def test_claims_selects_nextjs() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(NextJSParser())
    assert (
        registry.select("app/users/route.ts", b"export async function GET(){}").name
        == "typescript-nextjs"
    )
    # Pages Router API handler (default export) -> nextjs
    assert (
        registry.select("pages/api/users.ts", b"export default function handler(){}").name
        == "typescript-nextjs"
    )
    # a page.tsx under app/ is not a route file -> base parser
    assert (
        registry.select("app/users/page.tsx", b"export default function P(){}").name == "typescript"
    )
    # a route.ts with no exported verb -> base parser
    assert registry.select("app/users/route.ts", b"export const config = {};").name == "typescript"
    # a UI page under pages/ (not pages/api) -> base parser, even with a default export
    assert (
        registry.select("pages/about.tsx", b"export default function About(){}").name
        == "typescript"
    )
    registry.clear()
