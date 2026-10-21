"""NestJS role table (docs/entry-point-roles/nestjs.md): one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point


EP = {"route", "eventbus_consumer", "timer"}
PKG = '{"dependencies": {"@nestjs/core": "10", "@nestjs/common": "10"}}'
COMMON = (
    "import { Controller, Get, Post, UseGuards, UseInterceptors, Version } from '@nestjs/common';\n"
)


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _ctrl(analyze_files, body: str, *, decorators: str = "", imports: str = "", **extra: str):
    src = (
        COMMON
        + imports
        + f"{decorators}@Controller('users')\nexport class UsersController {{\n"
        + body
        + "}\n"
    )
    return analyze_files({"package.json": PKG, "src/users.controller.ts": src, **extra})


def test_verb_decorator_is_a_binding(analyze_files) -> None:
    st = _ctrl(analyze_files, "  @Get(':id')\n  findOne() { return 1; }\n")
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.handler, r.handlerLine) == (
        "route",
        "GET",
        "route",
        "/users/:id",
        "findOne",
        5,
    )


def test_controller_is_wiring(analyze_files) -> None:
    st = _ctrl(analyze_files, "  @Post()\n  create() { return 1; }\n")
    [r] = _eps(st)  # only the method; the class adds its prefix
    assert (r.method, r.endpoint) == ("POST", "/users")


def test_global_prefix_is_wiring(analyze_files) -> None:
    st = _ctrl(
        analyze_files,
        "  @Get()\n  list() { return []; }\n",
        **{
            "src/main.ts": "import { NestFactory } from '@nestjs/core';\n"
            "async function bootstrap() {\n  const app = await NestFactory.create(AppModule);\n"
            "  app.setGlobalPrefix('api');\n  await app.listen(3000);\n}\nbootstrap();\n"
        },
    )
    assert [r.endpoint for r in _eps(st)] == ["/api/users"]


def test_router_module_is_wiring(analyze_files) -> None:
    st = _ctrl(
        analyze_files,
        "  @Get()\n  list() { return []; }\n",
        **{
            "src/admin.module.ts": "import { Module } from '@nestjs/common';\n"
            "import { RouterModule } from '@nestjs/core';\n"
            "import { UsersController } from './users.controller';\n"
            "@Module({ controllers: [UsersController] })\nexport class AdminModule {}\n"
            "@Module({ imports: [AdminModule, RouterModule.register([{ path: 'admin', module: AdminModule }])] })\n"
            "export class AppModule {}\n"
        },
    )
    assert [r.endpoint for r in _eps(st)] == ["/admin/users"]


def test_version_is_wiring(analyze_files) -> None:
    st = _ctrl(analyze_files, "  @Post()\n  @Version('2')\n  create() { return 1; }\n")
    [r] = _eps(st)
    assert r.version == "2"


def test_use_guards_are_guards(analyze_files) -> None:
    st = _ctrl(
        analyze_files,
        "  @Get()\n  @UseGuards(RolesGuard)\n  list() { return []; }\n",
        decorators="@UseGuards(AuthGuard)\n",
    )
    [r] = _eps(st)
    assert r.guards == ["AuthGuard", "RolesGuard"]


def test_interceptors_are_guards(analyze_files) -> None:
    st = _ctrl(
        analyze_files, "  @Get()\n  @UseInterceptors(LoggingInterceptor)\n  list() { return []; }\n"
    )
    [r] = _eps(st)
    assert r.guards == ["LoggingInterceptor"]


MICRO = "import { EventPattern, MessagePattern } from '@nestjs/microservices';\n"


def test_event_pattern_is_a_consumer(analyze_files) -> None:
    st = _ctrl(
        analyze_files, "  @EventPattern('user.deleted')\n  onDeleted(data: any) {}\n", imports=MICRO
    )
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.handler) == (
        "eventbus_consumer",
        "EVENT",
        "message",
        "user.deleted",
        "onDeleted",
    )


def test_message_pattern_is_a_consumer(analyze_files) -> None:
    st = _ctrl(
        analyze_files,
        "  @MessagePattern('sum')\n  sum(data: number[]) { return 1; }\n",
        imports=MICRO,
    )
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.handler) == (
        "eventbus_consumer",
        "MESSAGE",
        "message",
        "sum",
        "sum",
    )


def test_cron_and_interval_are_timers(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": PKG,
            "src/jobs.service.ts": "import { Injectable } from '@nestjs/common';\n"
            "import { Cron, Interval } from '@nestjs/schedule';\n"
            "@Injectable()\nexport class JobsService {\n"
            "  @Cron('0 2 * * *')\n  nightly() {}\n"
            "  @Interval(5000)\n  poll() {}\n}\n",
        }
    )
    got = {(r.semanticType, r.endpoint, r.handler) for r in _eps(st)}
    assert got == {("timer", "0 2 * * *", "nightly"), ("timer", "5000", "poll")}


def test_interval_from_a_numeric_constant(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": PKG,
            "src/timing.ts": "export const EVERY = 5000;\n",
            "src/jobs.service.ts": "import { Injectable } from '@nestjs/common';\n"
            "import { Interval } from '@nestjs/schedule';\n"
            "import { EVERY } from './timing';\n"
            "@Injectable()\nexport class JobsService {\n"
            "  @Interval(EVERY)\n  poll() {}\n}\n",
        }
    )
    assert {(r.endpoint, r.handler) for r in _eps(st)} == {("5000", "poll")}


def test_named_interval_timeout_and_library_cron(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": PKG,
            "src/jobs.service.ts": "import { Injectable } from '@nestjs/common';\n"
            "import { Cron, CronExpression, Interval, Timeout } from '@nestjs/schedule';\n"
            "@Injectable()\nexport class JobsService {\n"
            "  @Interval('sync', 60000)\n  sync() {}\n"
            "  @Timeout(3000)\n  warmup() {}\n"
            "  @Cron(CronExpression.EVERY_HOUR, { name: 'h' })\n  hourly() {}\n"
            "  @Cron(CronExpression.NOT_A_MEMBER)\n  unknown() {}\n}\n",
        }
    )
    got = {(r.method, r.endpoint, r.handler) for r in _eps(st)}
    # A library enum member is its fixed value; an unknown member stays empty, never its name.
    assert got == {("PERIODIC", "60000", "sync"), ("TIMER", "3000", "warmup"),
                   ("PERIODIC", "0 0-23/1 * * *", "hourly"), ("PERIODIC", None, "unknown")}


GQL = "import { Resolver, Query, Mutation, Subscription, ResolveField, ResolveReference, Parent } from '@nestjs/graphql';\n"


def _resolvers(analyze_files, src: str):
    return analyze_files({"package.json": PKG, "src/books.resolver.ts": GQL + src})


def test_resolver_class_is_wiring_and_operations_are_bindings(analyze_files) -> None:
    st = _resolvers(
        analyze_files,
        "@Resolver()\nexport class BooksResolver {\n"
        "  @Query(() => [Book])\n  books() { return []; }\n"
        "  @Mutation(() => Book)\n  addBook() { return null; }\n"
        "  @Subscription(() => Book)\n  bookAdded() { return null; }\n}\n",
    )
    got = {(r.framework, r.method, r.routeKind, r.endpoint, r.handler) for r in _eps(st)}
    assert got == {
        ("graphql", "QUERY", "query", "books", "books"),
        ("graphql", "MUTATION", "mutation", "addBook", "addBook"),
        ("graphql", "SUBSCRIPTION", "subscription", "bookAdded", "bookAdded"),
    }


def test_data_type_field_is_none(analyze_files) -> None:
    st = _resolvers(
        analyze_files,
        "@Resolver(() => Book)\nexport class BookResolver {\n"
        "  @ResolveField(() => Author)\n  author(@Parent() book: Book) { return null; }\n}\n",
    )
    assert _eps(st) == []
    [f] = [s for s in st if s.routeKind == "field_resolver"]
    assert (f.method, f.endpoint) == ("RESOLVE_FIELD", "Book.author")


def test_data_type_field_beside_a_mutation_is_none(analyze_files) -> None:
    st = _resolvers(
        analyze_files,
        "@Resolver(() => Book)\nexport class BookResolver {\n"
        "  @Mutation(() => Book)\n  addBook() { return null; }\n"
        "  @ResolveField(() => Author)\n  author(@Parent() book: Book) { return null; }\n}\n",
    )
    assert [r.endpoint for r in _eps(st)] == ["addBook"]


def test_stub_parent_field_is_an_operation(analyze_files) -> None:
    st = _resolvers(
        analyze_files,
        "@Resolver(() => NotificationsMutation)\nexport class NotificationsResolver {\n"
        "  @Mutation(() => NotificationsMutation)\n  notifications() { return {}; }\n"
        "  @ResolveField(() => Boolean)\n  create() { return true; }\n}\n",
    )
    got = {(r.method, r.routeKind, r.endpoint) for r in _eps(st) if r.handler == "create"}
    assert got == {("MUTATION", "mutation", "NotificationsMutation.create")}


def test_stub_parent_field_in_another_class_is_an_operation(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": PKG,
            "src/root.resolver.ts": GQL + "@Resolver()\nexport class RootResolver {\n"
            "  @Mutation(() => AlertsMutation)\n  alerts() { return {}; }\n}\n",
            "src/alerts.resolver.ts": GQL
            + "@Resolver(() => AlertsMutation)\nexport class AlertsResolver {\n"
            "  @ResolveField(() => Boolean)\n  dismiss() { return true; }\n}\n",
        }
    )
    got = {(r.method, r.endpoint) for r in _eps(st) if r.handler == "dismiss"}
    assert got == {("MUTATION", "AlertsMutation.dismiss")}


def test_resolve_reference_is_a_binding(analyze_files) -> None:
    st = _resolvers(
        analyze_files,
        "@Resolver(() => Book)\nexport class BookResolver {\n"
        "  @ResolveReference()\n  resolveReference(ref: { id: string }) { return null; }\n}\n",
    )
    got = {(r.method, r.routeKind, r.endpoint, r.handler) for r in _eps(st)}
    assert got == {("RESOLVE_REFERENCE", "query", "Book", "resolveReference")}


def test_bootstrap_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": PKG,
            "src/main.ts": "import { NestFactory } from '@nestjs/core';\n"
            "async function bootstrap() {\n  const app = await NestFactory.create(AppModule);\n"
            "  await app.listen(3000);\n}\nbootstrap();\n",
        }
    )
    assert _eps(st) == []


def test_http_client_call_is_outbound(analyze_files) -> None:
    st = _ctrl(
        analyze_files,
        "  constructor(private http: HttpService) {}\n"
        "  @Get()\n  list() {\n    return this.http.get('https://example.com/remote');\n  }\n",
    )
    assert [s.endpoint for s in st if s.semanticType == "api_call"] == [
        "https://example.com/remote"
    ]


def test_client_proxy_is_outbound(analyze_files) -> None:
    st = _ctrl(
        analyze_files,
        "  constructor(private client: ClientProxy) {}\n"
        "  @Post()\n  create() {\n    this.client.emit('user.created', {});\n"
        "    return this.client.send('audit', {});\n  }\n",
        imports="import { ClientProxy } from '@nestjs/microservices';\n",
    )
    got = {
        (s.semanticType, s.endpoint)
        for s in st
        if s.semanticType and s.semanticType.startswith("eventbus_")
    }
    assert got == {("eventbus_publish", "user.created"), ("eventbus_send", "audit")}


def test_array_route_path_is_one_binding_per_path(analyze_files) -> None:
    # Nest serves the handler at each path of the array: one record per path, same handler.
    st = _ctrl(analyze_files, "  @Get([':id', ':slug'])\n  find() {}\n")
    got = sorted((r.method, r.endpoint, r.handler) for r in _eps(st))
    assert got == [("GET", "/users/:id", "find"), ("GET", "/users/:slug", "find")]
    # An element that is not a string literal is unknown: no source text in the address.
    st = _ctrl(analyze_files, "  @Get(['a', PATHS.B])\n  two() {}\n")
    got = sorted((r.endpoint or "") for r in _eps(st) if r.handler == "two")
    assert got == ["", "/users/a"]


def test_sse_decorator_is_a_get_binding(analyze_files) -> None:
    st = _ctrl(
        analyze_files,
        "  @Sse('stream')\n  stream() { return null; }\n",
        imports="import { Sse } from '@nestjs/common';\n",
        **{
            "src/main.ts": "import { NestFactory } from '@nestjs/core';\n"
            "async function bootstrap() {\n  const app = await NestFactory.create(AppModule);\n"
            "  app.setGlobalPrefix('api');\n}\nbootstrap();\n"
        },
    )
    [r] = _eps(st)
    assert (r.method, r.routeKind, r.endpoint, r.handler, r.handlerLine) == (
        "GET", "route", "/api/users/stream", "stream", 6)


GATEWAY = "import { SubscribeMessage, WebSocketGateway } from '@nestjs/websockets';\n"


def _gateway(analyze_files, args: str, body: str, *, decorators: str = ""):
    src = (COMMON + GATEWAY + f"{decorators}@WebSocketGateway({args})\n"
           "export class ChatGateway {\n" + body + "}\n")
    return analyze_files({"package.json": PKG, "src/chat.gateway.ts": src})


def test_gateway_message_handler_is_a_ws_binding(analyze_files) -> None:
    st = _gateway(analyze_files, "", "  @SubscribeMessage('msg')\n  onMsg() { return 1; }\n")
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.handler, r.handlerLine,
            r.framework) == ("route", "MESSAGE", "ws", "msg", "onMsg", 6, "nestjs")


def test_gateway_namespace_is_part_of_the_address(analyze_files) -> None:
    # A namespace is written as a client writes it, io('http://host/chat'); `#` keeps the
    # address from reading as a URL path. The port and the engine path are not part of it.
    st = _gateway(
        analyze_files, "8080, { namespace: 'chat', path: '/ws', cors: { origin: '*' } }",
        "  @SubscribeMessage('msg')\n  onMsg() { return 1; }\n",
    )
    assert [r.endpoint for r in _eps(st)] == ["/chat#msg"]


def test_gateway_default_namespace_adds_nothing(analyze_files) -> None:
    st = _gateway(analyze_files, "{ namespace: '/' }",
                  "  @SubscribeMessage('msg')\n  onMsg() { return 1; }\n")
    assert [r.endpoint for r in _eps(st)] == ["msg"]


def test_gateway_dynamic_namespace_is_unknown(analyze_files) -> None:
    st = _gateway(analyze_files, "{ namespace: /^\\/room-\\d+$/ }",
                  "  @SubscribeMessage('msg')\n  onMsg() { return 1; }\n")
    [r] = _eps(st)
    assert (r.endpoint, r.handler) == (None, "onMsg")


def test_gateway_guards(analyze_files) -> None:
    st = _gateway(analyze_files, "", "  @SubscribeMessage('msg')\n  onMsg() { return 1; }\n",
                  decorators="@UseGuards(WsAuthGuard)\n")
    [r] = _eps(st)
    assert (r.guards, r.authRequired) == (["WsAuthGuard"], True)


def test_subscribe_message_outside_a_gateway_is_none(analyze_files) -> None:
    st = _ctrl(analyze_files, "  @SubscribeMessage('msg')\n  onMsg() { return 1; }\n",
               imports=GATEWAY)
    assert _eps(st) == []
