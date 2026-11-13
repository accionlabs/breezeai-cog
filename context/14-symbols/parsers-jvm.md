---
title: "breezeai-cog — Symbol Reference: JVM Parsers"
date: 2026-10-30
---

Scope: `parsers/java`, `java_springboot`, `java_jaxrs`, `java_vertx`, `kotlin`, `kotlin_ktor`, `scala`, `scala_play`, `scala_akkahttp`, `scala_http4s`, `groovy`, `groovy_vertx`, plus `vertx_common.py`.

## Common JVM patterns

- `build_index` in Java/Kotlin/Scala/Groovy builds an FQCN-to-file index (`build_fqcn_index`) for cross-file import and call resolution; Java and Groovy indexes also hold a constant map used to fold symbolic addresses.
- Framework parsers subclass the base language parser, set `priority = 10` (base is 0), and override `claims(path, source)` with a byte-marker test; they reuse the inherited `extract` once and add route/event statements only when `ctx.capture_statements` is on.
- Route and event detectors return `Emit` lists applied with `apply(record, emits, source, path)`.
- Additive detectors and registered facts (`register_detector`, `register_fact`) add cross-file knowledge without a peer parser.

## java

| Symbol | Notes |
|---|---|
| `JavaParser` `src/breezeai_cog/parsers/java/parser.py:28` | `BaseParser`; `name="java"`, `extensions=(".java",)`, `FRAMEWORKS` = spring, springboot, jaxrs, quarkus. Imports, classes, methods, statements, comments; ends `extract` with `run_additive("java", ...)`. |
| `JavaParser.build_index` `src/breezeai_cog/parsers/java/parser.py:35` | Returns `JavaIndex` (FQCN map plus `Class.FIELD` constants). |
| `JavaParser.extract` `src/breezeai_cog/parsers/java/parser.py:44` | Single-parse extraction shared by every Java framework subclass. |
| `JavaIndex` `src/breezeai_cog/parsers/java/imports.py:37` | Dataclass index; `build_fqcn_index` at line 93, `extract_imports` at line 140. |
| `build_class` `src/breezeai_cog/parsers/java/classes.py:39` | Class records for class, interface, enum and record declarations. |
| `build_method` `src/breezeai_cog/parsers/java/functions.py:145` | Function records; `extract_annotations` (line 44) produces `Decorator` records that route detectors read; `payload_type` (line 221) unwraps declared types. |
| `collect_constants` `src/breezeai_cog/parsers/java/constants.py:52` | Raw `static final String` token lists keyed by simple and qualified name; folded with `constfold.resolve_all`. |
| `module_of` `src/breezeai_cog/parsers/java/config.py:40` | Maps a path to its module root; `spring_context_paths` (line 125) and `spring_properties` (line 198) read base config; `resolve_placeholders` (line 214). |
| `security_guards` `src/breezeai_cog/parsers/java/guards.py:73` | Guards of a handler, class scope first; per family the method annotations override the class. `Guard` is at line 48. |
| `collect_guard_facts` `src/breezeai_cog/parsers/java/web_guards.py:244` | Servlet filters and MVC interceptors per module; `resolve_guards` (line 288); fact `java.web-guards` registered at line 399. |
| `module_guards` `src/breezeai_cog/parsers/java/web_guards.py:386` | Module root to `ModuleGuards`; read by both Spring and JAX-RS. `spring_global_guards` (line 362), `servlet_match` (line 318), `ant_match` (line 357). |
| `extract_statements` `src/breezeai_cog/parsers/java/statements.py:76` | Statement capture for Java method bodies. |
| Quartz detector (`java/quartz.py`) | Additive detector `quartz` (order 10, guard `quartz`, skips fixtures); rules shared in `quartz_common`. |

## java_springboot

| Symbol | Notes |
|---|---|
| `SpringBootParser` `src/breezeai_cog/parsers/java_springboot/parser.py:36` | Base `JavaParser`; `name="java-springboot"`, priority 10, frameworks `spring`, `springboot`. |
| `SpringBootParser.claims` `src/breezeai_cog/parsers/java_springboot/parser.py:41` | True when the source contains `org.springframework`. |
| `SpringBootParser.parse_file` `src/breezeai_cog/parsers/java_springboot/parser.py:44` | Emits MVC routes, `RouterFunction` routes, module guards, context-path prefix, listeners/sends and `@Scheduled` timers. WebFlux.fn routes take `spring.webflux.base-path` instead of the servlet context path. |
| `detect_spring_routes` `src/breezeai_cog/parsers/java_springboot/routes.py:121` | One route per mapping annotation (and per path of an array mapping), read off the record. |
| `apply_global_guards` / `apply_context_path` `src/breezeai_cog/parsers/java_springboot/routes.py:91` | Add module filter guards, then prefix the context path (second function at line 102); `None` marks an unknown address. |
| `spring_version` `src/breezeai_cog/parsers/java_springboot/routes.py:28` | Returns 2 for `javax.*` externals, 3 for `jakarta.*`, else None. |
| `detect_spring_functional_routes` `src/breezeai_cog/parsers/java_springboot/functional_routes.py:177` | `RouterFunction` DSL routes (WebMvc.fn and WebFlux.fn); needs the AST. |
| `detect_spring_listeners` `src/breezeai_cog/parsers/java_springboot/messaging.py:141` | Broker listener consumers (Kafka, RabbitMQ, JMS, SQS); `detect_spring_sends` (line 247) finds template sends; `has_messaging` (line 71) is the byte gate. |
| `detect_spring_scheduled` `src/breezeai_cog/parsers/java_springboot/scheduling.py:81` | `@Scheduled` timer records; `has_scheduled` (line 33) is the byte gate. |
| `detect_spring_queries` `src/breezeai_cog/parsers/java_springboot/queries.py:15` | One `query_statement` per Spring Data `@Query` repository method, text from the annotation argument. |
| `context_paths` / `module_properties` `src/breezeai_cog/parsers/java_springboot/config_facts.py:32` | Read facts `spring.context-paths` and `spring.properties` (second function at line 36). |

## java_jaxrs

| Symbol | Notes |
|---|---|
| `JaxRsParser` `src/breezeai_cog/parsers/java_jaxrs/parser.py:20` | Base `JavaParser`; `name="java-jaxrs"`, priority 10, frameworks `["jaxrs"]`. |
| `JaxRsParser.claims` `src/breezeai_cog/parsers/java_jaxrs/parser.py:25` | True for `javax.ws.rs` or `jakarta.ws.rs` in the source. |
| `JaxRsParser.parse_file` `src/breezeai_cog/parsers/java_jaxrs/parser.py:28` | Builds routes with the module `@ApplicationPath`, sub-resource bases, module JAX-RS filters and declared paths. |
| `detect_jaxrs_routes` `src/breezeai_cog/parsers/java_jaxrs/routes.py:55` | Resource-method routes read off the record; an unresolvable prefix leaves the endpoint empty. `jaxrs_version` at line 25. |
| `collect_jaxrs_facts` `src/breezeai_cog/parsers/java_jaxrs/prefixes.py:203` | Collects `@ApplicationPath` and sub-resource locators; `resolve_jaxrs` (line 252); fact `jaxrs.prefixes` registered at line 389. |
| `jaxrs_prefixes` `src/breezeai_cog/parsers/java_jaxrs/prefixes.py:332` | Reads `JaxrsPrefixes` (`app_paths`, `sub_bases`); `JaxrsPrefixes` at line 324. |
| `declared_paths` `src/breezeai_cog/parsers/java_jaxrs/prefixes.py:162` | Per class/method `@Path` values with constants folded; `resolve_path_text` at line 71. |
| `web_xml_bases` / `property_bases` `src/breezeai_cog/parsers/java_jaxrs/config.py:66` | Servlet base paths from `web.xml` and property files (second function at line 97). |

## java_vertx

| Symbol | Notes |
|---|---|
| `VertxParser` `src/breezeai_cog/parsers/java_vertx/parser.py:23` | Base `JavaParser`; `name="java-vertx"`, priority 10, frameworks `["vertx"]`. |
| `VertxParser.claims` `src/breezeai_cog/parsers/java_vertx/parser.py:28` | True for `io.vertx` (3.x+) or `org.vertx` (2.x) in the source. |
| `VertxParser.parse_file` `src/breezeai_cog/parsers/java_vertx/parser.py:31` | Folds repo-wide plus file constants, runs `detect_vertx`, sets `framework="vertx"`. |
| `detect_vertx` `src/breezeai_cog/parsers/java_vertx/events.py:485` | Routes, EventBus consumers and sends, timers; a `ServiceBinder` registration yields one consumer per interface method. A handler given as `new Handler(...)` (no anonymous body) resolves to that class's `handle` through `_handler_class` (`java_vertx/events.py:428`). |
| `ServiceInterfaces` `src/breezeai_cog/parsers/java_vertx/service_interfaces.py:38` | Fact `vertx.service-interfaces`; `service_methods` (line 44) lists an interface's methods. |

## vertx_common

| Symbol | Notes |
|---|---|
| `classify_call` `src/breezeai_cog/parsers/vertx_common.py:121` | Maps one call to `(semanticType, method, endpoint, routeKind)` or None; shared by Java and Groovy; route paths need not start with a slash. A router is a name match or a declared `Router` / `RouteMatcher` variable (`routers`). |
| `DEPLOYS` `src/breezeai_cog/parsers/vertx_common.py:54` | Calls classified `verticle_deploy`: `deployVerticle`, `deployWorkerVerticle`, `deployModule`. |
| `vertx_emit` `src/breezeai_cog/parsers/vertx_common.py:388` | Builds the `Emit`, enriching the statement on the call's line or adding a record; `handler_info` (line 350) and `method_lines` (line 380) locate handlers; a `this` handler is the class's own `handle`. |
| `router_vars` `src/breezeai_cog/parsers/vertx_common.py:104` | Names a file declares as a `Router` or `RouteMatcher` (typed, or assigned from `new RouteMatcher(` / `Router.router(`), so a route call on any such variable is a route. |
| `is_route_receiver` / `is_bus_receiver` `src/breezeai_cog/parsers/vertx_common.py:90` | Receiver-name tests (second function at line 113). |
| `render_address` `src/breezeai_cog/parsers/vertx_common.py:192` | Renders a folded address; `join_path` (line 330) joins mount bases. |
| `resolve_mounted_factories` `src/breezeai_cog/parsers/vertx_common.py:252` | Resolves sub-router factory mounts into `MountedFactories` (line 243); `factory_mount` (line 264), `sub_router_bases` (line 284). |

## kotlin

| Symbol | Notes |
|---|---|
| `KotlinParser` `src/breezeai_cog/parsers/kotlin/parser.py:221` | `BaseParser`; `name="kotlin"`, `extensions=(".kt",)` only, `FRAMEWORKS=["ktor"]`; handles objects, companion objects (`_build_companion`), primary constructor params and file-scope declarations. |
| `KotlinParser.build_index` `src/breezeai_cog/parsers/kotlin/parser.py:228` | Returns `KotlinIndex` via `build_fqcn_index` (`kotlin/imports.py:51`). |
| `KotlinParser.parse_file` / `extract` `src/breezeai_cog/parsers/kotlin/parser.py:231` | `extract` (line 235) is reused by `KtorParser`; classes and functions are built by the parser's own `_build_class` and `_build_function`, not a shared module. |
| `extract_statements` `src/breezeai_cog/parsers/kotlin/statements.py:123` | Statement capture for Kotlin bodies; `extract_imports` is at `kotlin/imports.py:78`. |

## kotlin_ktor

| Symbol | Notes |
|---|---|
| `KtorParser` `src/breezeai_cog/parsers/kotlin_ktor/parser.py:22` | Base `KotlinParser`; `name="kotlin-ktor"`, priority 10, frameworks `["ktor"]`. |
| `KtorParser.claims` `src/breezeai_cog/parsers/kotlin_ktor/parser.py:27` | True when the source contains `io.ktor.server`. |
| `KtorParser.parse_file` `src/breezeai_cog/parsers/kotlin_ktor/parser.py:30` | Sets `framework="ktor"` unconditionally on a claimed file; routes only with statement capture. |
| `detect_ktor_routes` `src/breezeai_cog/parsers/kotlin_ktor/routes.py:305` | Every Ktor HTTP and WebSocket route. Helpers: `string_constants` (line 51), `lookup_constant` (line 105). |

## scala

| Symbol | Notes |
|---|---|
| `ScalaParser` `src/breezeai_cog/parsers/scala/parser.py:42` | `BaseParser`; `name="scala"`, `extensions=(".scala", ".sc", ".mill")`, `FRAMEWORKS=["akka","spark"]`. Handles `package object`; `_extract` calls `run_additive` for Akka/Pekko and Spark. |
| `ScalaParser.build_index` `src/breezeai_cog/parsers/scala/parser.py:52` | Returns `ScalaIndex` (`scala/imports.py:39`) via `build_fqcn_index` (line 171). |
| `ScalaParser.extract` `src/breezeai_cog/parsers/scala/parser.py:60` | Collects HTTP-client names scoped to the file, then `_extract`; shared by every Scala framework parser. |
| `visible_names` `src/breezeai_cog/parsers/scala/imports.py:82` | `VisibleNames` (line 62); `extract_imports` at line 196. |
| `build_class` `src/breezeai_cog/parsers/scala/classes.py:78` | Class, object, trait, enum records; `build_function` at `scala/functions.py:232`. |
| `extract_statements` `src/breezeai_cog/parsers/scala/statements.py:416` | Statement capture; `collect_http_clients` (line 364), `scoped_http_clients` (line 381), `find_enclosing_parent_id` (line 457). |
| `detect_scala_events` `src/breezeai_cog/parsers/scala/events.py:55` | Additive detector `akka-events` (order 10): labels files doing Akka/Pekko actor messaging; actors are not entry points. |
| `detect_spark_calls` `src/breezeai_cog/parsers/scala/spark.py:112` | Additive detector `spark` (order 20): enriches or appends Spark `.read`/`.write` statements. |

## scala_play

| Symbol | Notes |
|---|---|
| `ScalaPlayParser` `src/breezeai_cog/parsers/scala_play/parser.py:35` | Base `ScalaParser`; `name="scala-play"`, priority 10, frameworks `["play"]`. |
| `ScalaPlayParser.claims` `src/breezeai_cog/parsers/scala_play/parser.py:40` | True when the source contains `play.api`. |
| `ScalaPlayParser.parse_file` `src/breezeai_cog/parsers/scala_play/parser.py:43` | Emits SIRD routes; actions only label the file `play` (no record of their own). |
| `PlayRoutesIndex` `src/breezeai_cog/parsers/scala_play/parser.py:58` | Frozen dataclass holding `facts` from the routes pre-pass. |
| `PlayRoutesParser` `src/breezeai_cog/parsers/scala_play/parser.py:64` | Plain `BaseParser`, `name="scala-play-routes"`, priority 0; owns files matched by `matches` (`is_routes_file`); emits a `config` File of `language="scala"` with route statements. |
| `PlayRoutesParser.build_index` `src/breezeai_cog/parsers/scala_play/parser.py:71` | Resolves `->` router includes and route handlers into facts. |
| `extract_routes` `src/breezeai_cog/parsers/scala_play/routes.py:51` | Bindings of one routes file; `prefix=None` means unknown, and a router served at several prefixes yields empty addresses. `route_lines` at line 43. |
| `detect_play_sird_routes` `src/breezeai_cog/parsers/scala_play/actions.py:284` | SIRD `case GET(p"/path")` routes, prefixed from the including routes file or the router's own `val prefix`. |
| `detect_play_actions` `src/breezeai_cog/parsers/scala_play/actions.py:64` | Returns controller functions that are Play actions; emits no statement. |
| `collect_includes` / `resolve_includes` `src/breezeai_cog/parsers/scala_play/includes.py:130` | Router include collection and resolution (second function at line 146); `router_prefix` (line 107), `router_unknown` (line 118), `served_routers` (line 96). |
| `route_handler` `src/breezeai_cog/parsers/scala_play/handlers.py:106` | Locates a binding's controller method; `collect_route_handlers` (line 80), `split_handler` (line 46). |

## scala_akkahttp

| Symbol | Notes |
|---|---|
| `ScalaAkkaHttpParser` `src/breezeai_cog/parsers/scala_akkahttp/parser.py:13` | Base `ScalaParser`; `name="scala-akka-http"`, priority 10, frameworks `["akka-http"]`. |
| `ScalaAkkaHttpParser.claims` `src/breezeai_cog/parsers/scala_akkahttp/parser.py:18` | True for `akka.http.scaladsl` in the source. |
| `ScalaAkkaHttpParser.parse_file` `src/breezeai_cog/parsers/scala_akkahttp/parser.py:21` | Skips fixture files; sets `framework="akka-http"` only when routes are found. |
| `detect_akkahttp_routes` `src/breezeai_cog/parsers/scala_akkahttp/routes.py:159` | AST walk of the akka-http directive DSL. |

## scala_http4s

| Symbol | Notes |
|---|---|
| `ScalaHttp4sParser` `src/breezeai_cog/parsers/scala_http4s/parser.py:13` | Base `ScalaParser`; `name="scala-http4s"`, priority 10, frameworks `["http4s"]`. |
| `ScalaHttp4sParser.claims` `src/breezeai_cog/parsers/scala_http4s/parser.py:18` | True when the source contains `org.http4s`. |
| `ScalaHttp4sParser.parse_file` `src/breezeai_cog/parsers/scala_http4s/parser.py:21` | Skips fixtures; sets `framework="http4s"` when routes are found. |
| `detect_http4s_routes` `src/breezeai_cog/parsers/scala_http4s/routes.py:89` | Finds `HttpRoutes.of` and `AuthedRoutes.of` blocks and emits a route per case clause. |

## groovy

| Symbol | Notes |
|---|---|
| `GroovyParser` `src/breezeai_cog/parsers/groovy/parser.py:42` | `BaseParser`; `name="groovy"`, `extensions=(".groovy",)`, empty `FRAMEWORKS`. Second-tier, not skeleton-only: classes, methods, top-level script methods and statements, and calls are captured; the grammar degrades some bodies to missing nodes. |
| `GroovyParser.build_index` `src/breezeai_cog/parsers/groovy/parser.py:49` | Returns `GroovyIndex` (`groovy/imports.py:37`) via `build_fqcn_index` (line 103): FQCN map plus constants. |
| `GroovyParser.extract` `src/breezeai_cog/parsers/groovy/parser.py:58` | Shared by `GroovyVertxParser`; no additive detectors run here. |
| `build_class` `src/breezeai_cog/parsers/groovy/classes.py:63` | Class, interface, enum and trait records. |
| `build_method` `src/breezeai_cog/parsers/groovy/functions.py:193` | Method records; `build_function` (line 237) for top-level script methods; `has_declaration_error` (line 26) skips malformed declarations. |
| `collect_constants` `src/breezeai_cog/parsers/groovy/constants.py:50` | Constant token lists for address folding; `fold_arg` at line 19. |
| `extract_statements` `src/breezeai_cog/parsers/groovy/statements.py:83` | Statement capture for Groovy bodies. |

## groovy_vertx

| Symbol | Notes |
|---|---|
| `GroovyVertxParser` `src/breezeai_cog/parsers/groovy_vertx/parser.py:27` | Base `GroovyParser`; `name="groovy-vertx"`, priority 10, frameworks `["vertx"]`. |
| `GroovyVertxParser.claims` `src/breezeai_cog/parsers/groovy_vertx/parser.py:32` | True for `io.vertx` or `org.vertx` in the source. |
| `GroovyVertxParser.parse_file` `src/breezeai_cog/parsers/groovy_vertx/parser.py:35` | Runs `detect_vertx_groovy`; handles Vert.x 2.x `RouteMatcher` routes that the Java parser never sees. |
| `detect_vertx_groovy` `src/breezeai_cog/parsers/groovy_vertx/events.py:374` | Routes, consumers, sends, timers from the Groovy AST; fact `vertx.mounted-factories` registered at line 368. |
