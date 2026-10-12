"""LoopBack 4 role table (docs/entry-point-roles/loopback.md): one test per row."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}
PKG = '{"dependencies": {"@loopback/core": "5", "@loopback/rest": "13"}}'
IMPORTS = (
    "import { get, post, del, operation, api } from '@loopback/rest';\n"
    "import { authenticate } from '@loopback/authentication';\n"
)


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def _ctrl(analyze_files, body: str, *, decorators: str = "", **extra: str):
    src = IMPORTS + f"{decorators}export class ProductController {{\n" + body + "}\n"
    return analyze_files(
        {"package.json": PKG, "src/controllers/product.controller.ts": src, **extra}
    )


def test_verb_decorator_is_a_binding(analyze_files) -> None:
    st = _ctrl(analyze_files, "  @get('/products/{id}')\n  findById(id: string) { return null; }\n")
    [r] = _eps(st)
    assert (r.method, r.routeKind, r.endpoint, r.handler, r.handlerLine) == (
        "GET",
        "route",
        "/products/{id}",
        "findById",
        5,
    )


def test_operation_decorator_is_a_binding(analyze_files) -> None:
    st = _ctrl(
        analyze_files,
        "  @operation('patch', '/products/{id}')\n  update(id: string) { return null; }\n",
    )
    [r] = _eps(st)
    assert (r.method, r.endpoint, r.handler) == ("PATCH", "/products/{id}", "update")


def test_api_base_path_is_wiring(analyze_files) -> None:
    st = _ctrl(
        analyze_files,
        "  @del('/{id}')\n  remove(id: string) {}\n",
        decorators="@api({ basePath: '/products' })\n",
    )
    [r] = _eps(st)
    assert (r.method, r.endpoint) == ("DELETE", "/products/{id}")


@pytest.mark.xfail(strict=True, reason="the application-level rest basePath is not applied")
def test_application_base_path_is_wiring(analyze_files) -> None:
    st = _ctrl(
        analyze_files,
        "  @get('/products')\n  find() { return []; }\n",
        **{
            "src/application.ts": "import { RestApplication } from '@loopback/rest';\n"
            "export class ShopApplication extends RestApplication {\n"
            "  constructor() {\n    super({ rest: { basePath: '/api' } });\n  }\n}\n"
        },
    )
    [r] = _eps(st)
    assert r.endpoint == "/api/products"


def test_unresolved_path_part_leaves_endpoint_empty(analyze_files) -> None:
    st = _ctrl(analyze_files, "  @get(appConfig.apiPath + '/products')\n  find() { return []; }\n")
    [r] = _eps(st)
    assert r.endpoint in (None, "")


def test_authenticate_is_a_guard(analyze_files) -> None:
    st = _ctrl(
        analyze_files, "  @authenticate('jwt')\n  @post('/products')\n  create() { return null; }\n"
    )
    [r] = _eps(st)
    assert r.guards == ["jwt"]


def test_authenticate_skip_adds_nothing(analyze_files) -> None:
    st = _ctrl(
        analyze_files, "  @authenticate.skip()\n  @get('/ping')\n  ping() { return 'pong'; }\n"
    )
    [r] = _eps(st)
    assert r.guards is None


def test_start_up_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": PKG,
            "src/index.ts": "import { ShopApplication } from './application';\n"
            "export async function main() {\n  const app = new ShopApplication();\n"
            "  await app.start();\n}\n",
        }
    )
    assert _eps(st) == []
