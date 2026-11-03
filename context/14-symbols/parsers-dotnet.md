---
title: "breezeai-cog — Symbol Reference: .NET Parsers"
date: 2026-10-30
---

Scope: `parsers/csharp`, `csharp_aspnet`, `csharp_graphql`, `csharp_hotchocolate`, `csharp_wcf`, `csharp_webforms`, `razor`, `vb`, `vb_aspnet`, `dotnet_servicehost`, plus the shared `dotnet_common.py`, `aspnet_common.py`, `aspnet_areas.py`.

## Common .NET patterns

- One parser per file: framework parsers subclass `CSharpParser` (or `VbParser`), set `priority` above the base parser (0) and override `claims(path, source)` with a byte-marker test.
- Framework parsers call the inherited `extract` once (single tree-sitter parse), then add route statements only when `ctx.capture_statements` is on.
- `razor` and the markup half of `csharp-webforms` are template parsers (`template_extensions`); they stay idle without `--capture-templates`.
- Additive detectors (lambda handlers, Lucene, Hangfire, Quartz) run inside `CSharpParser.extract` through `run_additive("csharp", ...)`, so a file keeps its framework identity.
- Priorities: base `csharp` 0, `csharp-aspnet`/`csharp-wcf`/`csharp-webforms` 10, `csharp-graphql` 20, `csharp-hotchocolate` 25.

## csharp

| Symbol | Notes |
|---|---|
| `CSharpParser` `src/breezeai_cog/parsers/csharp/parser.py:58` | `BaseParser`; `name="csharp"`, `extensions=(".cs",)`, `FRAMEWORKS` = aspnet, aspnetcore and the aws-* Lambda labels. Emits imports, classes, methods, statements, comments. |
| `CSharpParser.build_index` `src/breezeai_cog/parsers/csharp/parser.py:65` | Repo pre-pass returning `CSharpIndex` (declared `Namespace.Type` to files); cross-file edges resolve from referenced type names, since `using` names a namespace. |
| `CSharpParser.parse_file` `src/breezeai_cog/parsers/csharp/parser.py:69` | Parses with tree-sitter `csharp`, then delegates to `extract`. |
| `CSharpParser.extract` `src/breezeai_cog/parsers/csharp/parser.py:73` | Shared by every subclass; sets the HttpClient-name context for the file and resets it in `finally`; `_extract` ends with `run_additive`. |
| `iter_type_declarations` `src/breezeai_cog/parsers/csharp/parser.py:47` | Walks block and file-scoped namespaces to reach class/struct/interface/enum/record declarations. |
| `CSharpIndex` `src/breezeai_cog/parsers/csharp/imports.py:56` | Dataclass: type-to-files map (set, so ambiguity is detectable), class heritage, extension methods, plus additive facts. |
| `build_csharp_index` `src/breezeai_cog/parsers/csharp/imports.py:534` | Builds the index in parallel (`jobs`). |
| `extract_imports` `src/breezeai_cog/parsers/csharp/imports.py:693` | Returns internal files, external imports, and name bindings from `using` directives. |
| `bind_static_import_members` `src/breezeai_cog/parsers/csharp/imports.py:650` | Binds members of `using static` types so a bare `M()` resolves; same-file declarations win. |
| `in_scope_namespaces` / `type_files` `src/breezeai_cog/parsers/csharp/imports.py:298` | Namespaces visible in a file and the files declaring a type name under them. |
| `build_class` `src/breezeai_cog/parsers/csharp/classes.py:50` | Builds Class records with their methods and statements. |
| `build_method` `src/breezeai_cog/parsers/csharp/functions.py:204` | Builds Function records (also used by `razor` for `@code` methods); handles local functions. |
| `extract_attributes` `src/breezeai_cog/parsers/csharp/attributes.py:32` | Turns C# attribute lists into `Decorator` records, which route detectors read. |
| `extract_statements` `src/breezeai_cog/parsers/csharp/statements.py:194` | Statement capture for method bodies; `collect_http_client_ids` (line 127) finds names holding an `HttpClient`. |
| `detect_lambda_handlers` `src/breezeai_cog/parsers/csharp/lambda_events.py:131` | Additive detector `lambda-handlers` (order 10): needs `ILambdaContext` plus an AWS event type; event triggers are `eventbus_consumer`, APIGateway/ALB are `route`. |
| `detect_lucene_access` `src/breezeai_cog/parsers/csharp/lucene.py:128` | Additive detector `lucene` (order 20): trusts only receivers declared as Lucene types, never the verb. |
| Hangfire detector (`csharp/hangfire.py`) | Additive detector `hangfire` (order 50, guard `AddOrUpdate`): `timer` bindings; reads literal cron or `Cron.*` helpers. |
| Quartz detector (`csharp/quartz.py`) | Additive detector `quartz` (order 60, guard `Quartz`): `timer` bindings; rules shared with Java in `quartz_common`. |

## csharp_aspnet

| Symbol | Notes |
|---|---|
| `AspNetCoreParser` `src/breezeai_cog/parsers/csharp_aspnet/parser.py:25` | Base `CSharpParser`; `name="csharp-aspnet"`, priority 10, frameworks `aspnet`, `aspnetcore`. |
| `AspNetCoreParser.claims` `src/breezeai_cog/parsers/csharp_aspnet/parser.py:30` | True when the source contains `Microsoft.AspNetCore`, `System.Web.Mvc` or `System.Web.Http` (so classic System.Web is covered too). |
| `AspNetCoreParser.parse_file` `src/breezeai_cog/parsers/csharp_aspnet/parser.py:33` | Emits controller routes, minimal-API `Map*` routes, and convention route registrations; resolves a template's default `Controller.Action` to the action's file. |
| `group_extension_prefix` `src/breezeai_cog/parsers/csharp_aspnet/group_extensions.py:100` | Looks up the route prefix of a `MapGroup` extension method from an index fact. |
| `detect_controller_routes` `src/breezeai_cog/parsers/aspnet_common.py:268` | Language-agnostic attribute and convention routes read off the `FileRecord`; area-aware via `aspnet_areas`. |
| `detect_minimal_api_routes` `src/breezeai_cog/parsers/aspnet_common.py:344` | AST walk for `app.MapGet(...)` style endpoints; node type names are passed per language. |
| `detect_route_registrations` `src/breezeai_cog/parsers/aspnet_common.py:749` | Convention route registrations; returns `(emits, wiring_only)` so a wiring-only `RouteConfig` still marks the file ASP.NET. |
| `controller_area` `src/breezeai_cog/parsers/aspnet_areas.py:289` | Resolves `[Area]`/`[RouteArea]`/namespace convention to an `Area`; `convention_endpoint` (line 320) builds the URL. |

## csharp_graphql

| Symbol | Notes |
|---|---|
| `CSharpGraphQLParser` `src/breezeai_cog/parsers/csharp_graphql/parser.py:23` | Base `CSharpParser`; `name="csharp-graphql"`, priority 20, frameworks `["graphql"]`; graphql-dotnet code-first only. |
| `CSharpGraphQLParser.claims` `src/breezeai_cog/parsers/csharp_graphql/parser.py:28` | True when the source contains `ObjectGraphType`. |
| `CSharpGraphQLParser.parse_file` `src/breezeai_cog/parsers/csharp_graphql/parser.py:31` | Skipped for fixture files; emits one route per root field via `detect_graphql_dotnet_routes`, sets `framework="graphql"`. |
| `detect_graphql_dotnet_routes` `src/breezeai_cog/parsers/csharp_graphql/routes.py:205` | Each `Field<T>("name", ...)` of a query, mutation or subscription type. |
| `schema_roots` `src/breezeai_cog/parsers/csharp_graphql/schema_roots.py:39` | Root classes declared in a path mapped to their kind, from an index fact. |

## csharp_hotchocolate

| Symbol | Notes |
|---|---|
| `CSharpHotChocolateParser` `src/breezeai_cog/parsers/csharp_hotchocolate/parser.py:47` | Base `CSharpParser`; `name="csharp-hotchocolate"`, priority 25, frameworks `["graphql"]`; annotation-based HotChocolate. |
| `CSharpHotChocolateParser.claims` `src/breezeai_cog/parsers/csharp_hotchocolate/parser.py:52` | Delegates to `owned_by_hotchocolate`. |
| `CSharpHotChocolateParser.parse_file` `src/breezeai_cog/parsers/csharp_hotchocolate/parser.py:55` | Emits operations, field resolvers and entity/node resolvers; passes registered root types from the index. |
| `owned_by_hotchocolate` `src/breezeai_cog/parsers/csharp_hotchocolate/mappings.py:138` | Rejects files with `ObjectGraphType` or composition-root calls (`AddGraphQLServer`, `MapGraphQL`, `UseGraphQL`), else needs a schema marker. Gotcha: `Program.cs` stays with `csharp-aspnet`. |
| `RegisteredRoots` `src/breezeai_cog/parsers/csharp_hotchocolate/registered_roots.py:53` | Dataclass `files` (declaring file to root kinds) and `types` (name to kind); `registered_roots` (line 67) reads it. Detector `hotchocolate-registered-roots` emits for attribute-less roots. |
| `detect_hotchocolate_routes` `src/breezeai_cog/parsers/csharp_hotchocolate/routes.py:596` | Main route detector for root-attributed and `[ExtendObjectType]` classes. |
| `root_kind` / `is_operation` `src/breezeai_cog/parsers/csharp_hotchocolate/routes.py:64` | Classify a class as query/mutation/subscription root and a method as an operation. |
| `descriptor_field_statement` `src/breezeai_cog/parsers/csharp_hotchocolate/routes.py:429` | Statement for fields declared in an `IObjectTypeDescriptor` `Configure` method; helpers live in `descriptor.py` (`configure_method` line 66, `field_declarations` line 283). |
| `generated_loaders` `src/breezeai_cog/parsers/csharp_hotchocolate/data_loaders.py:21` | Maps source-generated DataLoaders from an index fact. |
| `camel_case` / `field_name` `src/breezeai_cog/parsers/csharp_hotchocolate/naming.py:32` | GraphQL field naming from a method name and decorators. |

## csharp_wcf

| Symbol | Notes |
|---|---|
| `WcfParser` `src/breezeai_cog/parsers/csharp_wcf/parser.py:24` | Base `CSharpParser`; `name="csharp-wcf"`, priority 10, frameworks `wcf`, `asmx`. |
| `WcfParser.claims` `src/breezeai_cog/parsers/csharp_wcf/parser.py:29` | False for `.aspx.cs`, `.ascx.cs`, `.master.cs` (Web Forms owns them); else needs `System.ServiceModel`, `CoreWCF` or `System.Web.Services`. |
| `WcfParser.parse_file` `src/breezeai_cog/parsers/csharp_wcf/parser.py:34` | Tries `detect_wcf_services`, falls back to `detect_asmx_services`; framework comes from the first route. |
| `detect_wcf_services` `src/breezeai_cog/parsers/csharp_wcf/routes.py:175` | One route per `[OperationContract]` under a `[ServiceContract]`; endpoint is the `.svc` address when the host is known, else empty. |
| `detect_asmx_services` `src/breezeai_cog/parsers/csharp_wcf/routes.py:284` | One `rpc` route per `[WebMethod]`; endpoint `<.asmx path>/<Method>` only when the `.asmx` is found. |
| `svc_host` / `asmx_host` `src/breezeai_cog/parsers/csharp_wcf/routes.py:250` | Resolve the host file serving a class (`asmx_host` at line 245). |
| `SvcHosts` `src/breezeai_cog/parsers/csharp_wcf/svc_hosts.py:39` | Dataclass `by_fqn` (service FQN to `.svc` path); `svc_hosts` (line 50) reads the index fact. |

## csharp_webforms

| Symbol | Notes |
|---|---|
| `WebFormsParser` `src/breezeai_cog/parsers/csharp_webforms/parser.py:43` | Base `CSharpParser`; `name="csharp-webforms"`, priority 10, framework `aspnet-webforms`; also owns `.aspx`, `.ascx`, `.master` (`template_extensions`), adds `element`/`attribute` statement types. |
| `WebFormsParser.claims` `src/breezeai_cog/parsers/csharp_webforms/parser.py:53` | True for markup suffixes, `.aspx.cs`/`.ascx.cs`/`.master.cs`, or source containing `System.Web.UI`. |
| `WebFormsParser._parse_markup` `src/breezeai_cog/parsers/csharp_webforms/parser.py:60` | Markup File with language `html`, `uiRole="template"`; `importFiles` hold code-behind, `Register Src` controls and the master page; parses an island-blanked shadow with the html grammar. |
| `WebFormsParser.parse_file` `src/breezeai_cog/parsers/csharp_webforms/parser.py:100` | Markup goes to `_parse_markup`; code-behind emits page routes, navigation records, and mount/master IMPORTS edges (not statement-gated). |
| `detect_webforms_pages` `src/breezeai_cog/parsers/csharp_webforms/routes.py:52` | File-parented `route` for a `.aspx.cs` page, enriched with `MapPageRoute` URLs; empty for user controls and master pages. |
| `detect_navigation` `src/breezeai_cog/parsers/csharp_webforms/navigation.py:78` | `navigation` records for outgoing page links (code-behind and markup). |
| `detect_generic_handlers` `src/breezeai_cog/parsers/csharp_webforms/handlers.py:38` | One `route` per `.ashx` generic-handler class whose `.ashx` file exists. |
| `page_routes` `src/breezeai_cog/parsers/csharp_webforms/page_routes.py:28` | Index fact `webforms.page-routes`: physical `.aspx` to friendly routed URLs. |
| `shadow_markup` `src/breezeai_cog/parsers/csharp_webforms/markup.py:53` | Blanks server islands, preserving length so offsets stay valid. |
| `resolve_codebehind` / `resolve_control_mounts` `src/breezeai_cog/parsers/csharp_webforms/markup.py:64` | Resolve the code-behind path and `<%@ Register Src %>` mounts (second function at line 80). |
| `collect_markup_statements` `src/breezeai_cog/parsers/csharp_webforms/markup.py:152` | Server-control and `On<Event>` handler statements from markup. |
| `resolve_mounts` `src/breezeai_cog/parsers/csharp_webforms/mounts.py:68` | Sorted, deduped control code-behind paths from markup `Register` and code `LoadControl("...")`; empty without `repo_root`. |
| `resolve_master` / `master_codebehind` `src/breezeai_cog/parsers/csharp_webforms/mounts.py:88` | Master-page link and its code-behind (`master_codebehind` at line 109); `read_sibling_markup` (line 41) reads the sibling markup. |

## razor

| Symbol | Notes |
|---|---|
| `RazorParser` `src/breezeai_cog/parsers/razor/parser.py:39` | `BaseParser`; `name="razor"`, `extensions` and `template_extensions` both `.cshtml`, `.razor`; framework `razor`; language `html`. |
| `RazorParser.parse_file` `src/breezeai_cog/parsers/razor/parser.py:49` | Parses with the `razor` grammar; `uiRole` is `component` for `.razor`, `template` for `.cshtml`; fixtures emit no routes. |
| `RazorParser.handler_locator` `src/breezeai_cog/parsers/razor/parser.py:81` | For `.cshtml` returns a locator for the `@model` class in its `.cshtml.cs` code-behind; `.razor` returns None. |
| `RazorParser._extract_code_methods` `src/breezeai_cog/parsers/razor/parser.py:89` | Static helper; builds `@code` block methods with C# `build_method`, parented to the File. |
| `collect_razor_statements` `src/breezeai_cog/parsers/razor/statements.py:91` | Template statements (page/model directives, expressions, routes) from razor grammar nodes. |
| `page_model_locator` `src/breezeai_cog/parsers/razor/page_models.py:66` | Parses `X.cshtml.cs`; `class_sites` (line 39) maps class names to method sites. |

## vb

| Symbol | Notes |
|---|---|
| `VbParser` `src/breezeai_cog/parsers/vb/parser.py:53` | `BaseParser`; `name="vb"`, `extensions=(".vb",)`, `FRAMEWORKS` = aspnet, aspnetcore. |
| `VbParser.build_index` `src/breezeai_cog/parsers/vb/parser.py:60` | Returns `VbIndex`: class name to heritage only (VB imports name namespaces, so no file map). |
| `VbParser.parse_file` / `extract` `src/breezeai_cog/parsers/vb/parser.py:66` | Parse with grammar `vb`; `extract` (line 70) builds classes, methods, statements and comments. |
| `build_vb_index` `src/breezeai_cog/parsers/vb/imports.py:76` | Index builder; `VbIndex` is at line 35, `extract_imports` at line 22. |
| `build_class` / `build_method` `src/breezeai_cog/parsers/vb/classes.py:84` | VB class and method builders (`build_method` in `vb/functions.py:172`). |
| `extract_statements` `src/breezeai_cog/parsers/vb/statements.py:113` | VB statement capture. |

## vb_aspnet

| Symbol | Notes |
|---|---|
| `VbAspNetParser` `src/breezeai_cog/parsers/vb_aspnet/parser.py:22` | Base `VbParser`; `name="vb-aspnet"`, priority 10, frameworks `aspnet`, `aspnetcore`. |
| `VbAspNetParser.claims` `src/breezeai_cog/parsers/vb_aspnet/parser.py:27` | Same three byte markers as `csharp-aspnet`. |
| `VbAspNetParser.parse_file` `src/breezeai_cog/parsers/vb_aspnet/parser.py:30` | Reuses `aspnet_common` detectors with VB node names (`invocation`, `member_access`); no `Controller.Action` handler relinking. |

## dotnet_servicehost

| Symbol | Notes |
|---|---|
| `ServiceHostParser` `src/breezeai_cog/parsers/dotnet_servicehost/parser.py:27` | Plain `BaseParser` (no tree-sitter); `name="dotnet-servicehost"`, owns `.svc` and `.asmx` (keys of `DIRECTIVES`), frameworks `wcf`, `asmx`; empty `statement_types`. |
| `ServiceHostParser.parse_file` `src/breezeai_cog/parsers/dotnet_servicehost/parser.py:34` | File record with per-type language; with statement capture adds the directive statement, framework label and `importFiles` for the `CodeBehind`. |
| `DIRECTIVES` `src/breezeai_cog/parsers/dotnet_servicehost/routes.py:50` | Per-type data: directive keyword, class attribute (`Service=`/`Class=`), framework label. `spec_for` (line 76) picks the spec by path. |
| `detect_service_host` `src/breezeai_cog/parsers/dotnet_servicehost/routes.py:111` | Returns `(statement, framework, imports)`; the statement is plain wiring (OData hosts keep a `mount` record); unresolved `CodeBehind` yields no import edge. |

## Shared helpers (dotnet_common)

| Symbol | Notes |
|---|---|
| `code_behind_host` `src/breezeai_cog/parsers/dotnet_common.py:178` | Finds the `.asmx`/`.svc`/`.ashx` host whose directive names the class declared in a code-behind. |
| `ci_resolve` / `web_app_root` / `to_repo_path` `src/breezeai_cog/parsers/dotnet_common.py:129` | Case-insensitive path resolution, web.config-based app root (line 152), and `~/` to repo path mapping (line 164). |
| `simple_attr_name` / `response_dto` / `innermost_type` `src/breezeai_cog/parsers/dotnet_common.py:24` | Attribute-name trimming and return-type unwrapping (lines 60, 81). |
