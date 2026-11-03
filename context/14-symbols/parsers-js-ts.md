---
title: "breezeai-cog — Symbol Reference: JS/TS Parsers"
date: 2026-10-30
---

Scope: `parsers/typescript`, `typescript_react`, `typescript_angular`, `typescript_vue`, `typescript_nestjs`, `typescript_nextjs`, `typescript_express`, `typescript_fastify`, `typescript_graphql`, `typescript_graphql_codefirst`, `typescript_loopback`, `html`.

## Common JS/TS patterns

- Base parser `typescript`: `extensions=(".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")`; grammar is `tsx` for `.tsx`/`.jsx`, else `typescript`.
- Framework parsers subclass `TypeScriptParser`, set `priority`, and override `claims(path, source)` with byte markers. Priorities: nestjs and loopback 20, graphql-codefirst 15, the rest 10, base 0.
- Route statements are added only with statement capture and never for fixture files (`is_fixture_file`).
- Express and Fastify have no parser class: they are additive detectors run by `TypeScriptParser.extract` through `run_additive("typescript", ...)` on every TS/JS file, beside AWS events, GraphQL client, Vue routes and per-framework navigation detectors.
- `uiRole` is set on components, hooks, stores and composables by the React, Angular and Vue parsers; `template_extensions` (Vue `.vue`, HTML `.html`/`.htm`) are gated by `--capture-templates`.

## typescript

| Symbol | Notes |
|---|---|
| `TypeScriptParser` `src/breezeai_cog/parsers/typescript/parser.py:130` | `BaseParser`; `name="typescript"`, `FRAMEWORKS` = angular, nestjs, loopback, express, fastify, react, vue plus aws-* and aws-cognito/ssm labels. |
| `TypeScriptParser.fixture_markers` `src/breezeai_cog/parsers/typescript/parser.py:137` | Adds JS/TS fixture markers to the global set; inherited by every framework parser. |
| `TypeScriptParser.build_index` `src/breezeai_cog/parsers/typescript/parser.py:142` | Returns `TsAliasIndex` via `build_ts_index`: tsconfig path aliases plus a string-constant value map. |
| `TypeScriptParser.handler_locator` `src/breezeai_cog/parsers/typescript/parser.py:154` | Returns the handler lookup only for script files (not `.vue`); used when a record names a handler without a line. |
| `TypeScriptParser.extract` `src/breezeai_cog/parsers/typescript/parser.py:164` | Shared extraction for all subclasses; sets wrapped HTTP-client ids for the file and resets them in `finally`; `_extract` ends with `run_additive`. |
| `TsAliasIndex` `src/breezeai_cog/parsers/typescript/imports.py:35` | Repo index; `build_alias_index` at line 130, `build_ts_index` at line 550. |
| `extract_imports` `src/breezeai_cog/parsers/typescript/imports.py:853` | Import extraction; helpers `relative_requires` (line 451), `imported_locals` (line 681), `resolve_const_value` (line 221), `flatten_const_object` (line 251). |
| `build_class` `src/breezeai_cog/parsers/typescript/classes.py:63` | Class records; `build_function` at `typescript/functions.py:278`; `extract_decorators` at `typescript/decorators.py:26`. |
| `extract_statements` `src/breezeai_cog/parsers/typescript/statements.py:318` | Statement capture; `collect_typed_db_receivers` (line 67) gates high-collision ORM verbs by declared type; `collect_http_client_ids` (line 155). |
| `handler_locator` `src/breezeai_cog/parsers/typescript/handlers.py:56` | Leads an entry point to its handler declaration (`handlerLine`, `handlerPath` when in another file). |
| `resolve_export` `src/breezeai_cog/parsers/typescript/declarations.py:238` | Follows re-exports to a declaration; facts `typescript.declarations` and `typescript.vue-components` registered in this module; `module_exports` at line 234. |
| `collect_base_path` `src/breezeai_cog/parsers/typescript/base_paths.py:33` | `basePath` from Next.js config or LoopBack `rest` options; `resolve_base_paths` (line 69), `app_base_path` (line 82). |
| `collect_exports` `src/breezeai_cog/parsers/typescript/reexports.py:56` | Per-module export info for barrel re-exports; `declaring` at line 136. |
| `detect_aws_events` `src/breezeai_cog/parsers/typescript/aws_events.py:359` | Additive detector `aws-events` (order 20): AWS handler and producer statements; file label `aws-lambda` for a handler file. |
| `navigation` `src/breezeai_cog/parsers/typescript/navigation.py:51` | Shared record builder for outbound-navigation detectors; address is a literal absolute path or empty. |

## typescript_react

| Symbol | Notes |
|---|---|
| `ReactParser` `src/breezeai_cog/parsers/typescript_react/parser.py:24` | Base `TypeScriptParser`; `name="typescript-react"`, priority 10, frameworks `["react"]`. |
| `ReactParser.claims` `src/breezeai_cog/parsers/typescript_react/parser.py:29` | True for `react-router`, `'react'` or `"react"` in the source (exact quoted specifier, so `react-dom` does not match). |
| `ReactParser.parse_file` `src/breezeai_cog/parsers/typescript_react/parser.py:32` | Marks components and hooks, stamps `framework="react"` on every claimed file, then adds routes with statement capture. |
| `mark_react_components` `src/breezeai_cog/parsers/typescript_react/components.py:89` | `uiRole` for class components and PascalCase function components that render JSX; `mark_react_hooks` (line 113) marks `useX` functions calling a hook primitive. |
| `detect_react_routes` `src/breezeai_cog/parsers/typescript_react/routes.py:418` | React Router page routes: JSX, config objects and v7 route modules. |
| React navigation detector (`typescript_react/navigation.py`) | Additive detector `react-navigation` (order 74): `useNavigate()` and router links. |

## typescript_angular

| Symbol | Notes |
|---|---|
| `AngularParser` `src/breezeai_cog/parsers/typescript_angular/parser.py:16` | Base `TypeScriptParser`; `name="typescript-angular"`, priority 10, frameworks `["angular"]`. |
| `AngularParser.claims` `src/breezeai_cog/parsers/typescript_angular/parser.py:21` | True when the source contains `@angular/`. |
| `AngularParser.parse_file` `src/breezeai_cog/parsers/typescript_angular/parser.py:24` | Marks `uiRole`, always stamps `framework="angular"`, then adds routes with statement capture. |
| `mark_angular_ui_roles` `src/breezeai_cog/parsers/typescript_angular/components.py:26` | `uiRole` for `@Component`, `@Directive`, `@Pipe` classes. |
| `detect_angular_routes` `src/breezeai_cog/parsers/typescript_angular/routes.py:324` | Page routes from route configs; `declared_classes` (line 342) and `lazy_mount_guards` (line 349) support lazy mounts. |
| `route_mounts` `src/breezeai_cog/parsers/typescript_angular/mounts.py:243` | Fact lookup for lazy mounts; `mount_served` (line 248); dataclass `RouteMounts` at line 232. |
| `incoming_guards` `src/breezeai_cog/parsers/typescript_angular/mount_guards.py:39` | Guards a lazy mount elsewhere puts in front of this file's routes. |
| Angular navigation detector (`typescript_angular/navigation.py`) | Additive detector `angular-navigation`. |

## typescript_vue

| Symbol | Notes |
|---|---|
| `VueParser` `src/breezeai_cog/parsers/typescript_vue/parser.py:39` | Base `TypeScriptParser`; `name="typescript-vue"`, `extensions` adds `.vue`, `template_extensions=(".vue",)`, priority 10, frameworks `["vue"]`; adds `directive_attribute` and `interpolation` statement types. |
| `VueParser.claims` `src/breezeai_cog/parsers/typescript_vue/parser.py:50` | True for any `.vue` path, or source importing `'vue'`, `"vue"`, `vue-router`, `'pinia'`, `"pinia"`. |
| `VueParser.parse_file` `src/breezeai_cog/parsers/typescript_vue/parser.py:72` | `.vue`: parses the script shadow, sets `language` from the script lang and `uiRole="component"`; a template-only SFC still yields a record. Always stamps `framework="vue"`. |
| `VueParser._template_statements` `src/breezeai_cog/parsers/typescript_vue/parser.py:55` | Parses the original SFC with the `vue` grammar for template bindings; route emission skipped for fixtures. |
| `script_ranges` `src/breezeai_cog/parsers/typescript_vue/sfc.py:42` | `<script>` byte ranges; `script_grammar` (line 47), `script_language` (line 58), `shadow_source` (line 72) blanks everything else, keeping offsets. |
| `collect_vue_template_statements` `src/breezeai_cog/parsers/typescript_vue/template.py:112` | Template statements from the `vue` grammar tree. |
| `detect_vue_routes` `src/breezeai_cog/parsers/typescript_vue/routes.py:331` | vue-router route arrays; run by the additive detector `vue-routes` (order 50) on every TS/JS file, not in `parse_file`. |
| `mark_factory_ui_roles` `src/breezeai_cog/parsers/typescript_vue/components.py:103` | `uiRole` for `defineComponent` and Pinia `defineStore`; `mark_composables` (line 142) marks `useX` functions using reactivity primitives. |
| Vue navigation detector (`typescript_vue/navigation.py`) | Additive detector `vue-navigation`. |

## typescript_nestjs

| Symbol | Notes |
|---|---|
| `NestJSParser` `src/breezeai_cog/parsers/typescript_nestjs/parser.py:29` | Base `TypeScriptParser`; `name="typescript-nestjs"`, priority 20, frameworks `nestjs`, `routing-controllers`, `nestjs-like`. |
| `NestJSParser.claims` `src/breezeai_cog/parsers/typescript_nestjs/parser.py:37` | True for `@nestjs/` or `routing-controllers`, or a `@Controller`/`@JsonController` plus an HTTP-verb decorator (custom decorator modules). |
| `NestJSParser._framework` `src/breezeai_cog/parsers/typescript_nestjs/parser.py:45` | Static label choice: `nestjs`, else `routing-controllers`, else `nestjs-like`. |
| `NestJSParser.parse_file` `src/breezeai_cog/parsers/typescript_nestjs/parser.py:52` | Emits controller routes, then outbound `ClientProxy` sends; sets `framework` only when something is found. |
| `detect_nest_routes` `src/breezeai_cog/parsers/typescript_nestjs/routes.py:446` | Controller and resolver bindings; the global prefix and `RouterModule` path come from the `nest.prefixes` fact (NestJS only). |
| `detect_client_proxy_calls` `src/breezeai_cog/parsers/typescript_nestjs/outbound.py:68` | `eventbus_send`/`eventbus_publish` statements for `ClientProxy.send`/`emit`. |
| `nest_prefixes` `src/breezeai_cog/parsers/typescript_nestjs/prefixes.py:55` | Reads the prefix fact (`NestPrefixes` at line 50). `message_pattern` is at `typescript_nestjs/patterns.py:85`. |

## typescript_nextjs

| Symbol | Notes |
|---|---|
| `NextJSParser` `src/breezeai_cog/parsers/typescript_nextjs/parser.py:28` | Base `TypeScriptParser`; `name="typescript-nextjs"`, priority 10, frameworks `["nextjs"]`; route handlers only. |
| `NextJSParser.claims` `src/breezeai_cog/parsers/typescript_nextjs/parser.py:33` | Path-gated: an App Router `route.*` file must export a verb function (`GET`...); a Pages `pages/api` file must have a default export. |
| `NextJSParser.parse_file` `src/breezeai_cog/parsers/typescript_nextjs/parser.py:42` | Emits routes; sets `framework="nextjs"` when found. |
| `detect_nextjs_routes` `src/breezeai_cog/parsers/typescript_nextjs/routes.py:266` | Route handlers; the app's `basePath` from `next.config` is joined in front. Path helpers: `is_app_router_route_file` (line 65), `is_pages_api_file` (line 72), `endpoint_from_path` (line 78), `pages_api_endpoint` (line 90). |
| `detect_nextjs_page` `src/breezeai_cog/parsers/typescript_nextjs/pages.py:123` | Page record for a Next.js page file, via additive detector `nextjs-pages` (order 60), not the parser. |
| `router_root` `src/breezeai_cog/parsers/typescript_nextjs/facts.py:43` | App root under `app`/`pages` (parent when under `src`); `next_apps` (line 56), `next_middlewares` (line 60), `next_base_paths` (line 64) read facts. |

## typescript_express

| Symbol | Notes |
|---|---|
| Express support (`typescript_express/parser.py`) | The module holds only a docstring; no Express parser class exists. Express is an additive detector, labelled in the base parser's `FRAMEWORKS`. |
| `detect_express` `src/breezeai_cog/parsers/typescript_express/routes.py:720` | Express route statements; self-guarded on an `express` import; registered as detector `express` (order 10, skips fixtures). |
| `express_mounts` `src/breezeai_cog/parsers/typescript_express/mounts.py:127` | Reads the repo's Express mounts (`ExpressMounts` at line 88); helpers `router_locals` (line 69), `mw_name` (line 113). |
| `plain_locals` / `package_locals` `src/breezeai_cog/parsers/typescript_express/routes.py:75` | Names bound only to plain values; and local name to (package, export) for imports and requires (second function at line 243). |
| `certain_auth` `src/breezeai_cog/parsers/typescript_express/routes.py:313` | Middleware names that certainly reject anonymous requests; only express-jwt counts, a name like `requireAuth` does not. |

## typescript_fastify

| Symbol | Notes |
|---|---|
| Fastify support (no parser class) | There is no Fastify parser class; Fastify is an additive detector `fastify` (order 15, skips fixtures), framework label `fastify`. |
| `detect_fastify` `src/breezeai_cog/parsers/typescript_fastify/routes.py:133` | Route statements for names bound to a Fastify instance, never by name alone. |
| `analyze` `src/breezeai_cog/parsers/typescript_fastify/plugins.py:751` | Returns `FileFastify` (line 133): instances, scopes, edges, autoloads; `analyzer` at line 756. |
| `plugin_prefixes` `src/breezeai_cog/parsers/typescript_fastify/plugins.py:783` | Reads the plugin-prefixes fact (`PluginPrefixes` at line 768); `join_prefix` (line 151), `guard_names` (line 220). |

## typescript_graphql

| Symbol | Notes |
|---|---|
| `GraphQLParser` `src/breezeai_cog/parsers/typescript_graphql/parser.py:19` | Base `TypeScriptParser`; `name="typescript-graphql"`, priority 10, frameworks `["graphql"]`. |
| `GraphQLParser.claims` `src/breezeai_cog/parsers/typescript_graphql/parser.py:24` | True for a `Resolvers` map with a `Query:`/`Mutation:`/`Subscription:` key, or an SDL `type Query {`-style block (the brace excludes the TS alias). |
| `GraphQLParser.parse_file` `src/breezeai_cog/parsers/typescript_graphql/parser.py:39` | Resolver-map routes first, then embedded SDL classes and statements; sets `framework="graphql"`. |
| `detect_resolver_maps` `src/breezeai_cog/parsers/typescript_graphql/routes.py:105` | Resolver-map operations and federation `__resolveReference` bindings. |
| `detect_embedded_sdl` `src/breezeai_cog/parsers/typescript_graphql/routes.py:210` | Uses the GraphQL language extraction on SDL in template strings; returns `(classes, statements)`. |
| `detect_graphql_client` `src/breezeai_cog/parsers/typescript_graphql/routes.py:424` | `gql` client operations; additive detector `graphql-client` (order 40). |
| `root_field_dto` `src/breezeai_cog/parsers/typescript_graphql/sdl_fields.py:39` | DTOs of `type.field`, `(None, None)` when unknown or ambiguous. |
| GraphQL resolver-modules detector (resolver-modules module) | Additive detector `graphql-resolver-modules` (order 45): a root type given as a namespace import of a module. |

## typescript_graphql_codefirst

| Symbol | Notes |
|---|---|
| `GraphQLCodeFirstParser` `src/breezeai_cog/parsers/typescript_graphql_codefirst/parser.py:27` | Base `TypeScriptParser`; `name="typescript-graphql-codefirst"`, priority 15, frameworks `["graphql"]`. |
| `GraphQLCodeFirstParser.claims` `src/breezeai_cog/parsers/typescript_graphql_codefirst/parser.py:32` | Needs an import from `type-graphql` or a project-relative `decorators` module AND `@Query(`/`@Mutation(`/`@Subscription(` usage; excludes `@nestjs/` files. |
| `GraphQLCodeFirstParser.parse_file` `src/breezeai_cog/parsers/typescript_graphql_codefirst/parser.py:35` | Emits operations; sets `framework="graphql"` when found. |
| `detect_codefirst_graphql_routes` `src/breezeai_cog/parsers/typescript_graphql_codefirst/routes.py:187` | `@Query`/`@Mutation`/`@Subscription` methods on a resolver class. |

## typescript_loopback

| Symbol | Notes |
|---|---|
| `LoopBackParser` `src/breezeai_cog/parsers/typescript_loopback/parser.py:17` | Base `TypeScriptParser`; `name="typescript-loopback"`, priority 20, frameworks `["loopback"]`. |
| `LoopBackParser.claims` `src/breezeai_cog/parsers/typescript_loopback/parser.py:25` | True when the source contains `@loopback/`. Express routes in the same file still come from the additive detector. |
| `LoopBackParser.parse_file` `src/breezeai_cog/parsers/typescript_loopback/parser.py:28` | Emits controller routes; sets `framework="loopback"` when found. |
| `detect_loopback_routes` `src/breezeai_cog/parsers/typescript_loopback/routes.py:158` | Controller routes with the app's `rest.basePath` prepended (unknown when unresolvable). |
| `loopback_base_paths` `src/breezeai_cog/parsers/typescript_loopback/facts.py:19` | Reads the `loopback.base-paths` fact. |

## html

| Symbol | Notes |
|---|---|
| `HtmlParser` `src/breezeai_cog/parsers/html/parser.py:53` | `BaseParser`; `name="html"`, `extensions` and `template_extensions` both `.html`, `.htm`. Framework-neutral: a file is a template (`uiRole="template"`) only when detected. |
| `HtmlParser.build_index` `src/breezeai_cog/parsers/html/parser.py:61` | Returns `HtmlTemplateIndex` via `build_html_template_index`. |
| `HtmlParser.parse_file` `src/breezeai_cog/parsers/html/parser.py:68` | Framework from the index (also links `importFiles` to the owning component), else the fingerprint; unknown gives a plain html File. Statements parsed only for frameworks with a grammar (Angular today). |
| `HtmlTemplateIndex` `src/breezeai_cog/parsers/html/index.py:69` | Template path to `ComponentRef` (line 57); a template claimed by two components is dropped as ambiguous. `build_html_template_index` at line 139. |
| `detect_rendering_framework` `src/breezeai_cog/parsers/html/fingerprint.py:76` | Uniquely-identifying markers only; two matching frameworks give None; ignores `<pre>`/`<code>` content. |
| `detect_behaviors` `src/breezeai_cog/parsers/html/fingerprint.py:85` | Sorted htmx, Alpine, Stimulus markers, independent of the rendering framework. |
| `collect_template_statements` `src/breezeai_cog/parsers/html/statements.py:154` | Template statements from the Angular grammar tree; routes skipped for fixtures. |
