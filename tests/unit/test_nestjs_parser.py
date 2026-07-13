"""NestJS framework parser: route detection, parentId linkage, base reuse, override."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_nestjs.parser import NestJSParser
from breezeai_cog.schemas import FileRecord

SRC = b'''import { Controller, Get, Post, Param } from '@nestjs/common';

@Controller('orders')
export class OrderController {
  @Get(':id')
  getOne(@Param('id') id: string) { return id; }

  @Post()
  create() {}

  helper() { return 1; }
}
'''


def _parse(tmp_path, *, capture=True) -> FileRecord:
    p = tmp_path / "order.controller.ts"
    p.write_text(SRC.decode())
    ctx = ParseContext(path="order.controller.ts", abs_path=p, source=SRC, repo_root=tmp_path,
                       capture_statements=capture)
    return NestJSParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    # Routes are statements — only emitted with --capture-statements (spec A4).
    rec = _parse(tmp_path, capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_routes_detected_and_linked(tmp_path) -> None:
    rec = _parse(tmp_path)
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {"/orders/:id", "/orders"}
    assert routes["/orders/:id"].method == "GET" and routes["/orders/:id"].handler == "getOne"
    assert routes["/orders/:id"].framework == "nestjs"
    assert routes["/orders"].method == "POST" and routes["/orders"].handler == "create"
    fn_ids = {f.id for f in rec.functions}
    assert all(r.parentId in fn_ids for r in routes.values())  # parented to handler methods
    assert rec.framework == "nestjs"


def test_base_extraction_reused(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert {f.name for f in rec.functions} == {"getOne", "create", "helper"}
    assert rec.language == "typescript"


def test_param_decorators_captured(tmp_path) -> None:
    # spec C4.1 — TS parameter decorators (@Param/@Body/…) now captured.
    rec = _parse(tmp_path)
    getone = next(f for f in rec.functions if f.name == "getOne")
    assert [d.name for d in getone.params[0].decorators] == ["Param"]


_ATTRS_SRC = b'''import { Controller, Post, Body, UseGuards } from '@nestjs/common';
import { ApiResponse } from '@nestjs/swagger';

@Controller('orders')
@UseGuards(JwtAuthGuard)
export class OrderController {
  @Post()
  @UseGuards(RolesGuard)
  @ApiResponse({ status: 201, type: OrderDto })
  create(@Body() dto: CreateOrderDto) { return dto; }

  @Get()
  list() { return []; }
}
'''


def test_route_attributes(tmp_path) -> None:
    # spec C5 — guards (controller+method merged), authRequired, requestDTO, responseDTO.
    p = tmp_path / "attrs.controller.ts"
    p.write_text(_ATTRS_SRC.decode())
    ctx = ParseContext(path="attrs.controller.ts", abs_path=p, source=_ATTRS_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    routes = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    create = routes["create"]
    assert create.guards == ["JwtAuthGuard", "RolesGuard"]  # controller + method merged
    assert create.authRequired is True
    assert create.requestDTO == "CreateOrderDto"
    assert create.responseDTO == "OrderDto"
    assert create.isRegex is False
    # controller guard still applies to routes with no method-level guard
    assert routes["list"].guards == ["JwtAuthGuard"] and routes["list"].authRequired is True
    assert routes["list"].requestDTO is None and routes["list"].responseDTO is None


# @nestjs/graphql code-first resolver — grounded on the real decorator shapes:
# thunk-only (name = method), explicit { name }, SDL-string first arg, @ResolveField
# (field resolver, not an op), and a @Query PARAM decorator that must not be mistaken.
_GQL_SRC = b'''import { Resolver, Query, Mutation, Subscription, ResolveField, Args } from '@nestjs/graphql';

@Resolver(() => Conversation)
export class ConversationResolver {
  @Query(() => [Conversation])
  async getConversations(): Promise<Conversation[]> { return []; }

  @Mutation(() => CapAI, { name: 'capAi' })
  public capAi(): CapAI { return null; }

  @Query('productCollections(params: Params!): ProductCollectionsResponse!')
  public productCollections() { return null; }

  @Subscription(() => ChatEvent, { name: 'chatEvents' })
  chatEvents() { return null; }

  @ResolveField(() => String)
  async title(): Promise<string> { return ''; }
}

@Resolver()
export class SearchResolver {
  // @Query here is the @nestjs/common PARAM decorator, NOT a GraphQL op
  search(@Query() q: string) { return q; }
}
'''


def test_graphql_code_first_operations(tmp_path) -> None:
    p = tmp_path / "conversation.resolver.ts"
    p.write_text(_GQL_SRC.decode())
    ctx = ParseContext(path="conversation.resolver.ts", abs_path=p, source=_GQL_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    ops = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    # name = method (thunk), explicit { name }, SDL-string leading id, subscription name
    assert ("QUERY", "getConversations") in ops
    assert ("MUTATION", "capAi") in ops
    assert ("QUERY", "productCollections") in ops
    assert ("SUBSCRIPTION", "chatEvents") in ops
    for s in ops.values():
        assert s.framework == "graphql"
    assert ops[("QUERY", "getConversations")].routeKind == "query"
    assert ops[("SUBSCRIPTION", "chatEvents")].routeKind == "subscription"
    # @ResolveField is a field resolver, not a client-callable op → not a route
    assert "title" not in {s.endpoint for s in ops.values()}
    # the @Query PARAM decorator on SearchResolver.search must NOT produce a route
    assert "search" not in {s.endpoint for s in ops.values()}
    # ops parent to their handler methods
    fn_ids = {f.id for f in rec.functions}
    assert all(s.parentId in fn_ids for s in ops.values())


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_claims_selects_nestjs() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(NestJSParser())
    assert registry.select("x.ts", b"import { Controller } from '@nestjs/common';").name == "typescript-nestjs"
    assert registry.select("x.ts", b"const x = 1;").name == "typescript"  # plain TS -> base
    registry.clear()


_OBJ_SRC = b'''import { Controller, Get, Version } from '@nestjs/common';
import type { Request } from 'express';

@Controller({ path: 'orders', host: ':tenant.api.example.com' })
export class OrdersController {
  @Get(':id')
  findById() {}

  @Version('2')
  @Get()
  findV2() {}
}
'''


def test_object_form_controller_prefix_and_version(tmp_path) -> None:
    # #3: @Controller({ path, host }) object form -> base prefix from `path` (not the
    # whole object literal); @Version('2') captured on the route.
    p = tmp_path / "obj.controller.ts"
    p.write_text(_OBJ_SRC.decode())
    ctx = ParseContext(path="obj.controller.ts", abs_path=p, source=_OBJ_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    routes = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    assert routes["findById"].endpoint == "/orders/:id"   # not /{ path: ... }/:id
    assert routes["findV2"].endpoint == "/orders"
    assert routes["findV2"].version == "2"
    assert routes["findById"].version is None


_COMMENT_SRC = b'''import { Controller, Get } from '@nestjs/common';

@Controller('pipeline')
export class PipelineController {
  @Get('has-comment')
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  getA(): never { throw new Error('x'); }

  @Get('no-comment')
  getB() { return 1; }
}
'''


def test_route_survives_comment_between_decorator_and_handler(tmp_path) -> None:
    # Regression: a comment line between @Get(...) and the method must NOT drop the route.
    p = tmp_path / "pipeline.controller.ts"
    p.write_text(_COMMENT_SRC.decode())
    ctx = ParseContext(path="pipeline.controller.ts", abs_path=p, source=_COMMENT_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    endpoints = {s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert endpoints == {"/pipeline/has-comment", "/pipeline/no-comment"}


_MSG_SRC = b'''import { Controller } from '@nestjs/common';
import { EventPattern, MessagePattern } from '@nestjs/microservices';

@Controller()
export class HubspotController {
  @EventPattern('hubspot.contact')
  onContact() {}

  @MessagePattern({ cmd: 'sum' })
  sum() {}
}
'''


def test_event_and_message_patterns_detected(tmp_path) -> None:
    # spec B1.4 — @EventPattern/@MessagePattern → eventbus_consumer with the topic as endpoint.
    p = tmp_path / "hubspot.controller.ts"
    p.write_text(_MSG_SRC.decode())
    ctx = ParseContext(path="hubspot.controller.ts", abs_path=p, source=_MSG_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    consumers = {s.handler: s for s in rec.statements if s.semanticType == "eventbus_consumer"}
    assert set(consumers) == {"onContact", "sum"}
    assert consumers["onContact"].endpoint == "hubspot.contact"
    assert consumers["onContact"].method == "EVENT"
    assert consumers["onContact"].routeKind == "message" and consumers["onContact"].framework == "nestjs"
    assert consumers["sum"].method == "MESSAGE"


_RETTYPE_SRC = b'''import { Controller, Get } from '@nestjs/common';

@Controller('orders')
export class OrderController {
  @Get()
  list(): Promise<OrderDto[]> { return null as any; }

  @Get('one')
  one(): UserDto { return null as any; }

  @Get('count')
  count(): Promise<number> { return null as any; }
}
'''


def test_response_dto_from_return_type(tmp_path) -> None:
    # responseDTO falls back to the handler return type (Promise<T>/T), skipping primitives.
    p = tmp_path / "ret.controller.ts"
    p.write_text(_RETTYPE_SRC.decode())
    ctx = ParseContext(path="ret.controller.ts", abs_path=p, source=_RETTYPE_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    dto = {s.handler: s.responseDTO for s in rec.statements if s.semanticType == "route"}
    assert dto["list"] == "OrderDto"          # Promise<OrderDto[]> → OrderDto
    assert dto["one"] == "UserDto"            # UserDto
    assert dto["count"] is None               # Promise<number> → primitive → None


# routing-controllers: same @Controller/@Get grammar, different import package. Mirrors the
# breezeai-backend controllers (which the old Node parser detected but cog previously missed).
_RC_SRC = b'''import { Controller, Get, Post, Delete, Param, Body, Authorized } from 'routing-controllers';

@Controller('/tags')
export class TagController {
  @Get('/:id')
  getTagById(@Param('id') id: string): Tag { return null as any; }

  @Post('/')
  @Authorized(['ADMIN'])
  createTag(@Body() body: CreateTagValidation): Tag { return null as any; }

  @Delete('/:id')
  deleteTag(@Param('id') id: string) {}
}
'''


def test_routing_controllers_claimed_and_composed(tmp_path) -> None:
    # The parser must claim routing-controllers files and compose base + method path.
    from breezeai_cog.core import registry
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.clear()
    registry.register(TypeScriptParser())
    registry.register(NestJSParser())
    assert registry.select("tag.controller.ts", _RC_SRC).name == "typescript-nestjs"
    registry.clear()

    p = tmp_path / "tag.controller.ts"
    p.write_text(_RC_SRC.decode())
    ctx = ParseContext(path="tag.controller.ts", abs_path=p, source=_RC_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    routes = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {"getTagById", "createTag", "deleteTag"}
    assert routes["getTagById"].method == "GET" and routes["getTagById"].endpoint == "/tags/:id"
    assert routes["createTag"].method == "POST" and routes["createTag"].endpoint == "/tags"
    assert routes["deleteTag"].method == "DELETE" and routes["deleteTag"].endpoint == "/tags/:id"
    # framework label reflects routing-controllers, not nestjs
    assert routes["getTagById"].framework == "routing-controllers"
    assert rec.framework == "routing-controllers"
    # @Authorized → authRequired, @Body → requestDTO
    assert routes["createTag"].authRequired is True
    assert routes["createTag"].guards == ["Authorized"]
    assert routes["createTag"].requestDTO == "CreateTagValidation"
    # routes parented to their handler methods
    fn_ids = {f.id for f in rec.functions}
    assert all(r.parentId in fn_ids for r in routes.values())


_JSONCTRL_SRC = b'''import { JsonController, Get } from 'routing-controllers';

@JsonController('/users')
export class UserController {
  @Get('/me')
  me() { return {}; }
}
'''


def test_json_controller_base(tmp_path) -> None:
    # routing-controllers @JsonController is a controller too.
    p = tmp_path / "user.controller.ts"
    p.write_text(_JSONCTRL_SRC.decode())
    ctx = ParseContext(path="user.controller.ts", abs_path=p, source=_JSONCTRL_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].endpoint == "/users/me" and routes[0].method == "GET"
