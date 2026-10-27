"""NestJS framework parser: route detection, parentId linkage, base reuse, override."""

from __future__ import annotations

import json

import pytest

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
    # Routes are statements — only emitted with statement capture (spec A4).
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
    # @ResolveField is captured as a field_resolver route, addressed <ParentType>.<field> from
    # @Resolver(() => Conversation), and with no method — a field resolver has no verb.
    assert ("RESOLVE_FIELD", "Conversation.title") in ops
    assert ops[("RESOLVE_FIELD", "Conversation.title")].routeKind == "field_resolver"
    assert ops[("RESOLVE_FIELD", "Conversation.title")].framework == "graphql"
    # @ResolveField(() => String) — the declared type, a scalar
    assert ops[("RESOLVE_FIELD", "Conversation.title")].responseDTO == "String"
    # the @Query PARAM decorator on SearchResolver.search must NOT produce a route
    assert "search" not in {s.endpoint for s in ops.values()}
    # ops parent to their handler methods
    fn_ids = {f.id for f in rec.functions}
    assert all(s.parentId in fn_ids for s in ops.values())


_RESOLVE_FIELD_SRC = b'''import { Resolver, ResolveField, Args, Query } from '@nestjs/graphql';

@Resolver(() => Brand)
export class BrandsQueryResolver {
  @Query(() => [Brand])
  async brands(): Promise<Brand[]> { return []; }

  @ResolveField(() => BrandResult)
  async byObjectIds(
    @Args({ name: 'objectIds', type: () => [String] }) objectIds: string[],
    @Args({ nullable: true }) { skip, take }: GetBrandsArgs,
  ): Promise<BrandResult> { return null; }

  @ResolveField(() => Number)
  async count(@Args() { region }: RegionArgs): Promise<number> { return 0; }
}
'''


def test_resolve_field_captured(tmp_path) -> None:
    # Gap 1: @ResolveField methods produce route statements with routeKind="field_resolver".
    p = tmp_path / "brands.resolver.ts"
    p.write_text(_RESOLVE_FIELD_SRC.decode())
    ctx = ParseContext(path="brands.resolver.ts", abs_path=p, source=_RESOLVE_FIELD_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    fields = {s.endpoint: s for s in rec.statements
              if s.semanticType == "route" and s.routeKind == "field_resolver"}
    assert set(fields) == {"Brand.byObjectIds", "Brand.count"}
    assert all(s.method == "RESOLVE_FIELD" for s in fields.values())  # the spec's verb
    # return type from @ResolveField(() => BrandResult) → responseDTO
    assert fields["Brand.byObjectIds"].responseDTO == "BrandResult"
    # @ResolveField(() => Number) → Number is a scalar → responseDTO None
    assert fields["Brand.count"].responseDTO == "Number"  # () => Number: the declared type
    # framework is graphql
    assert all(s.framework == "graphql" for s in fields.values())


def test_resolve_field_args_dto(tmp_path) -> None:
    # Gap 2: @Args()-decorated params with class types are captured as requestDTO.
    p = tmp_path / "brands.resolver.ts"
    p.write_text(_RESOLVE_FIELD_SRC.decode())
    ctx = ParseContext(path="brands.resolver.ts", abs_path=p, source=_RESOLVE_FIELD_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    by_endpoint = {s.endpoint: s for s in rec.statements
                   if s.semanticType == "route" and s.routeKind == "field_resolver"}
    # GetBrandsArgs is the first class-typed @Args param (objectIds is scalar string[])
    assert by_endpoint["Brand.byObjectIds"].requestDTO == "GetBrandsArgs"
    # RegionArgs is the @Args() type for count
    assert by_endpoint["Brand.count"].requestDTO == "RegionArgs"


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
    # responseDTO falls back to the handler return type (Promise<T>/T); a built-in type is kept.
    p = tmp_path / "ret.controller.ts"
    p.write_text(_RETTYPE_SRC.decode())
    ctx = ParseContext(path="ret.controller.ts", abs_path=p, source=_RETTYPE_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    dto = {s.handler: s.responseDTO for s in rec.statements if s.semanticType == "route"}
    assert dto["list"] == "OrderDto"          # Promise<OrderDto[]> → OrderDto
    assert dto["one"] == "UserDto"            # UserDto
    assert dto["count"] == "number"           # Promise<number> → the built-in type


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


# Namespace/grouping resolver: @Resolver(() => NotificationsMutation) with a singleton-return
# @Mutation and @ResolveField methods that represent the real operations.
_GROUPING_RESOLVER_SRC = b'''import { Resolver, Mutation, ResolveField, Args } from '@nestjs/graphql';

@ObjectType()
class NotificationsMutation {}

@Resolver(() => NotificationsMutation)
export class NotificationsMutationResolver {
  @Mutation(() => NotificationsMutation)
  notifications(): NotificationsMutation { return new NotificationsMutation(); }

  @ResolveField(() => NotificationDto)
  async create(@Args('input') input: CreateNotificationInput): Promise<NotificationDto> {
    return null;
  }

  @ResolveField(() => NotificationDto)
  async markAsRead(@Args('id') id: string): Promise<NotificationDto> {
    return null;
  }
}

@Resolver(() => Brand)
export class BrandEntityResolver {
  @Query(() => [Brand])
  async brands(): Promise<Brand[]> { return []; }

  @ResolveField(() => [NotificationDto])
  async all(): Promise<NotificationDto[]> { return []; }
}
'''


def test_grouping_resolver_resolve_fields_inherit_parent_op(tmp_path) -> None:
    # A stub parent's @ResolveField is an operation of the stub's kind: endpoint
    # "<StubType>.<field>", method = MUTATION/QUERY, routeKind = mutation/query.
    p = tmp_path / "notifications-mutation.resolver.ts"
    p.write_text(_GROUPING_RESOLVER_SRC.decode())
    ctx = ParseContext(path="notifications-mutation.resolver.ts", abs_path=p,
                       source=_GROUPING_RESOLVER_SRC, repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    by_ep = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    # Stub-parent fields: endpoint = "<StubType>.<field>", method = MUTATION
    assert "NotificationsMutation.create" in by_ep
    assert "NotificationsMutation.markAsRead" in by_ep
    assert by_ep["NotificationsMutation.create"].method == "MUTATION"
    assert by_ep["NotificationsMutation.create"].routeKind == "mutation"
    assert by_ep["NotificationsMutation.create"].framework == "graphql"
    assert by_ep["NotificationsMutation.create"].responseDTO == "NotificationDto"
    assert by_ep["NotificationsMutation.create"].requestDTO == "CreateNotificationInput"
    assert by_ep["NotificationsMutation.markAsRead"].method == "MUTATION"
    # Entity resolver (@Query returning an array): @ResolveField stays a field_resolver,
    # addressed <ParentType>.<field> with no method.
    assert "Brand.all" in by_ep
    assert by_ep["Brand.all"].routeKind == "field_resolver"
    assert by_ep["Brand.all"].method == "RESOLVE_FIELD"


def test_gql_op_response_dto_from_decorator_arg(tmp_path) -> None:
    # Gap 2: @Query/@Mutation with arrow-function arg expose responseDTO from the arg type,
    # e.g. @Query(() => UserDto) → responseDTO="UserDto".
    src = b'''import { Resolver, Query, Mutation } from '@nestjs/graphql';

@Resolver(() => User)
export class UserResolver {
  @Query(() => UserDto)
  async getUser(): Promise<UserDto> { return null; }

  @Mutation(() => [UserDto])
  async createUser(): Promise<UserDto[]> { return []; }

  @Query(() => String)
  async ping(): Promise<string> { return ''; }
}
'''
    p = tmp_path / "user.resolver.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="user.resolver.ts", abs_path=p, source=src,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    by_ep = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    # @Query(() => UserDto) → responseDTO from decorator arg, not return type
    assert by_ep["getUser"].responseDTO == "UserDto"
    # @Mutation(() => [UserDto]) — array form → UserDto (scalar stripped by _dto_from_type)
    assert by_ep["createUser"].responseDTO == "UserDto"
    # @Query(() => String) — the declared scalar
    assert by_ep["ping"].responseDTO == "String"


def _resolver_routes(tmp_path, src: bytes) -> dict[str, tuple[str | None, str | None]]:
    p = tmp_path / "x.resolver.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="x.resolver.ts", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    return {s.endpoint: (s.method, s.routeKind) for s in rec.statements if s.semanticType == "route"}


def test_crud_resolver_field_stays_a_field_resolver(tmp_path) -> None:
    # @Mutation(() => Book) returns a real Book: Book is an entity, `author` is its data field.
    routes = _resolver_routes(tmp_path, b'''import { Resolver, Mutation, Query, ResolveField, Args } from '@nestjs/graphql';
@Resolver(() => Book)
export class BookResolver {
  @Query(() => Book)
  book(@Args('id') id: string) { return this.books.find(id); }

  @Mutation(() => Book)
  addBook(@Args('input') input: BookInput) { return this.books.create(input); }

  @ResolveField(() => Author)
  author(@Parent() book: Book) { return this.authors.find(book.authorId); }
}
''')
    assert routes["Book.author"] == ("RESOLVE_FIELD", "field_resolver")
    assert routes["addBook"] == ("MUTATION", "mutation")
    assert "addBook.author" not in routes and "book.author" not in routes


def test_stub_parent_accepts_empty_object_forms(tmp_path) -> None:
    for ret in (b"return {};", b"return {} as OrdersQuery;", b"return new OrdersQuery();"):
        routes = _resolver_routes(tmp_path, b'''import { Resolver, Query, ResolveField } from '@nestjs/graphql';
@Resolver(() => OrdersQuery)
export class OrdersQueryResolver {
  @Query(() => OrdersQuery)
  orders() { ''' + ret + b''' }

  @ResolveField(() => [Order])
  list() { return []; }
}
''')
        assert routes["OrdersQuery.list"] == ("QUERY", "query"), ret


# ── Global prefix (setGlobalPrefix) and RouterModule paths ─────────────────────────────────────
_NEST_PKG = '{"dependencies": {"@nestjs/core": "10", "@nestjs/common": "10"}}'


def _ctrl_src(name: str, base: str, body: str) -> str:
    return (
        "import { Controller, Get, Post } from '@nestjs/common';\n"
        "import { MessagePattern } from '@nestjs/microservices';\n"
        f"@Controller('{base}')\nexport class {name} {{\n{body}}}\n"
    )


def _main(prefix_call: str) -> str:
    return (
        "import { NestFactory } from '@nestjs/core';\nimport { AppModule } from './app.module';\n"
        "async function bootstrap() {\n  const app = await NestFactory.create(AppModule);\n"
        f"  {prefix_call}\n  await app.listen(3000);\n}}\nbootstrap();\n"
    )


def _http(stmts) -> dict[tuple[str | None, str], str | None]:
    """(method, handler) → endpoint for NestJS HTTP routes."""
    return {(s.method, s.handler): s.endpoint for s in stmts
            if s.semanticType == "route" and s.framework == "nestjs"}


def test_global_prefix_and_exclude(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEST_PKG,
        "src/main.ts": _main(
            "app.setGlobalPrefix('api', { exclude: ['health', "
            "{ path: 'users/:id', method: RequestMethod.POST }] });"
        ),
        "src/users.controller.ts": _ctrl_src("UsersController", "users", (
            "  @Get(':id')\n  one() { return 1; }\n"
            "  @Post(':id')\n  touch() { return 1; }\n"
            "  @MessagePattern('users.created')\n  created() { return 1; }\n"
        )),
        "src/health.controller.ts": _ctrl_src("HealthController", "health", "  @Get()\n  ok() {}\n"),
    })
    assert _http(st) == {
        ("GET", "one"): "/api/users/:id",
        ("POST", "touch"): "/users/:id",  # excluded for POST only
        ("GET", "ok"): "/health",          # excluded
    }
    [msg] = [s for s in st if s.semanticType == "eventbus_consumer"]
    assert msg.endpoint == "users.created"  # message patterns take no HTTP prefix


def test_global_prefix_from_a_constant(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEST_PKG,
        "src/config.ts": "export const API_PREFIX = 'v1';\n",
        "src/main.ts": "import { API_PREFIX } from './config';\n" + _main("app.setGlobalPrefix(API_PREFIX);"),
        "src/users.controller.ts": _ctrl_src("UsersController", "users", "  @Get()\n  list() {}\n"),
    })
    assert _http(st) == {("GET", "list"): "/v1/users"}


def test_unresolvable_global_prefix_leaves_endpoint_empty(analyze_files) -> None:
    # The prefix exists but comes from runtime config: the address is unknown (decision Q8).
    st = analyze_files({
        "package.json": _NEST_PKG,
        "src/main.ts": _main("app.setGlobalPrefix(config.values.globalPrefix);"),
        "src/users.controller.ts": _ctrl_src("UsersController", "users", "  @Get()\n  list() {}\n"),
    })
    [r] = [s for s in st if s.semanticType == "route"]
    assert (r.method, r.endpoint, r.handler) == ("GET", None, "list")


def test_global_prefix_per_app_in_a_monorepo(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEST_PKG,
        "apps/admin/src/main.ts": _main("app.setGlobalPrefix('admin-api');"),
        "apps/admin/src/a.controller.ts": _ctrl_src("AController", "a", "  @Get()\n  a() {}\n"),
        "apps/shop/src/main.ts": _main("// no prefix"),
        "apps/shop/src/b.controller.ts": _ctrl_src("BController", "b", "  @Get()\n  b() {}\n"),
        "libs/shared/c.controller.ts": _ctrl_src("CController", "c", "  @Get()\n  c() {}\n"),
    })
    assert _http(st) == {
        ("GET", "a"): "/admin-api/a",
        ("GET", "b"): "/b",
        ("GET", "c"): None,  # belongs to no app: the admin prefix may or may not apply
    }


def test_no_bootstrap_leaves_routes_unchanged(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEST_PKG,
        "src/users.controller.ts": _ctrl_src("UsersController", "users", "  @Get()\n  list() {}\n"),
    })
    assert _http(st) == {("GET", "list"): "/users"}


def test_router_module_children_compose(analyze_files) -> None:
    st = analyze_files({
        "package.json": _NEST_PKG,
        "src/main.ts": _main("app.setGlobalPrefix('api');"),
        "src/app.module.ts": (
            "import { Module } from '@nestjs/common';\nimport { RouterModule } from '@nestjs/core';\n"
            "@Module({ controllers: [ReportsController] })\nexport class ReportsModule {}\n"
            "@Module({ controllers: [] })\nexport class AdminModule {}\n"
            "@Module({ imports: [RouterModule.register([\n"
            "  { path: 'admin', module: AdminModule, children: [{ path: 'reports', module: ReportsModule }] },\n"
            "])] })\nexport class AppModule {}\n"
        ),
        "src/reports.controller.ts": _ctrl_src("ReportsController", "daily", "  @Get()\n  daily() {}\n"),
    })
    assert _http(st) == {("GET", "daily"): "/api/admin/reports/daily"}


def test_interceptors_are_guards_but_not_auth(tmp_path) -> None:
    src = b'''import { Controller, Get, UseGuards, UseInterceptors } from '@nestjs/common';
@Controller('items')
@UseInterceptors(LoggingInterceptor)
export class ItemsController {
  @Get()
  @UseInterceptors(CacheInterceptor)
  list() { return []; }

  @Get(':id')
  @UseGuards(AuthGuard)
  @UseInterceptors(TimingInterceptor)
  one() { return null; }
}
'''
    p = tmp_path / "items.controller.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="items.controller.ts", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    got = {s.endpoint: (s.guards, s.authRequired)
           for s in rec.statements if s.semanticType == "route"}
    assert got == {
        "/items": (["LoggingInterceptor", "CacheInterceptor"], False),  # interceptors: not auth
        "/items/:id": (["LoggingInterceptor", "AuthGuard", "TimingInterceptor"], True),
    }


def test_client_proxy_send_and_emit_are_outbound(analyze_files) -> None:
    st = analyze_files({
        "package.json": '{"dependencies": {"@nestjs/core": "10", "@nestjs/microservices": "10"}}',
        "src/constants.ts": "export const AUDIT = 'audit.write';\n",
        "src/orders.service.ts": (
            "import { Inject, Injectable } from '@nestjs/common';\n"
            "import { ClientProxy, ClientProxyFactory } from '@nestjs/microservices';\n"
            "import { AUDIT } from './constants';\n"
            "@Injectable()\nexport class OrdersService {\n"
            "  private events = ClientProxyFactory.create({});\n"
            "  constructor(@Inject('MATH') private readonly math: ClientProxy, private mailer: Mailer) {}\n"
            "  run(topic: string) {\n"
            "    this.math.send({ cmd: 'sum' }, [1, 2]);\n"
            "    this.math.send(AUDIT, {});\n"
            "    this.events.emit('order.created', {});\n"
            "    this.events.emit(topic, {});\n"
            "    return this.mailer.send('x', {});\n"
            "  }\n}\n"
        ),
        "src/math.controller.ts": (
            "import { Controller } from '@nestjs/common';\n"
            "import { MessagePattern } from '@nestjs/microservices';\n"
            "@Controller()\nexport class MathController {\n"
            "  @MessagePattern({ cmd: 'sum' })\n  sum(data: number[]) { return 3; }\n}\n"
        ),
    })
    out = sorted((s.semanticType, s.endpoint or "")
                 for s in st if s.semanticType in ("eventbus_send", "eventbus_publish"))
    assert out == [
        ("eventbus_publish", ""),               # topic is a parameter → empty, never the symbol
        ("eventbus_publish", "order.created"),
        ("eventbus_send", "audit.write"),       # constant resolved
        ("eventbus_send", '{"cmd":"sum"}'),     # canonical JSON, like the consumer (Q12)
    ]                                           # this.mailer.send: not a ClientProxy → nothing
    consumer = [s.endpoint for s in st if s.semanticType == "eventbus_consumer"]
    assert consumer == ['{"cmd":"sum"}']


def test_object_patterns_are_canonical_json_on_both_sides(tmp_path) -> None:
    # NestJS sorts the keys of an object pattern and serialises it as JSON, so spacing, quotes
    # and key order do not matter: both sides are written {"a":1,"cmd":"sum"}.
    src = b"""import { Controller } from '@nestjs/common';
import { MessagePattern, EventPattern, ClientProxy } from '@nestjs/microservices';
const RUNTIME = process.env.X;
@Controller()
export class MathController {
  constructor(private readonly client: ClientProxy) {}
  @MessagePattern({ cmd: 'sum', a: 1 })
  sum() { return 0; }
  @MessagePattern({ role: 'user', nested: { b: [1, true, null] } })
  nested() { return 0; }
  @MessagePattern({ cmd: RUNTIME })
  unknown() { return 0; }
  @EventPattern('order.created')
  created() { return 0; }
  run() {
    this.client.send({a:1,"cmd":"sum"}, []);
    this.client.send({ ...base, cmd: 'x' }, []);
  }
}
"""
    p = tmp_path / "math.controller.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="math.controller.ts", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    consumers = [s.endpoint for s in rec.statements if s.semanticType == "eventbus_consumer"]
    assert consumers == [
        '{"a":1,"cmd":"sum"}',
        '{"nested":{"b":[1,true,null]},"role":"user"}',
        None,                      # a runtime value → unknown
        "order.created",           # a string pattern is the string
    ]
    sends = [s.endpoint for s in rec.statements if s.semanticType == "eventbus_send"]
    assert sends == ['{"a":1,"cmd":"sum"}', None]  # matches its consumer; a spread → unknown


def test_object_pattern_with_a_numeric_constant_is_unknown(analyze_files) -> None:
    # The constant index holds 2 and '2' alike as "2"; inside an object JSON writes them
    # differently ({"v":2} / {"v":"2"}), so such a part is unknown — never a guessed type.
    # At the top level both serialise to "2", so the pattern is known.
    st = analyze_files({
        "package.json": '{"dependencies": {"@nestjs/core": "10", "@nestjs/microservices": "10"}}',
        "src/math.controller.ts": (
            "import { Controller } from '@nestjs/common';\n"
            "import { MessagePattern } from '@nestjs/microservices';\n"
            "const VERSION = 2;\nconst NAME = 'sum';\n"
            "@Controller()\nexport class MathController {\n"
            "  @MessagePattern({ cmd: NAME, v: VERSION })\n  versioned() { return 0; }\n"
            "  @MessagePattern({ cmd: NAME })\n  named() { return 0; }\n"
            "  @MessagePattern(VERSION)\n  top() { return 0; }\n}\n"
        ),
    })
    got = {s.handler: s.endpoint for s in st if s.semanticType == "eventbus_consumer"}
    assert got == {"versioned": None, "named": '{"cmd":"sum"}', "top": "2"}


@pytest.mark.parametrize(("ret", "dto"), [
    ("Promise<string>", "string"),  # a built-in type is the declared type
    ("Observable<number[] | null>", "number"),
    ("orders.Order", "Order"),  # never the namespace
    ("{ url: string }", None),  # an object type: not its field's type
    ("Record<string, OrderDto>", "OrderDto"),
    ("Promise<void>", None),
])
def test_response_dto_keeps_built_in_types(analyze_files, ret: str, dto: str | None) -> None:
    st = analyze_files({"src/a.controller.ts": (
        "import { Controller, Get } from '@nestjs/common';\n"
        f"@Controller('a')\nexport class A {{\n  @Get()\n  get(): {ret} {{ return null; }}\n}}\n"
    )})
    [r] = [s for s in st if s.semanticType == "route"]
    assert r.responseDTO == dto
