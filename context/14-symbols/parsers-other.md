---
title: "breezeai-cog — Symbol Reference: Other Parsers"
date: 2026-10-30
---

Scope: every directory under `src/breezeai_cog/parsers/` outside the C#/.NET, VB, Java, Kotlin, Scala, Groovy, TypeScript and HTML shards.

Covered: `apex`, `c`, `cfamily_common`, `config`, `cpp`, `detection`, `go` and the `go_*` detectors, `graphql`, `hcl`, `hcl_terraform`, `jslt`, the `php` family, `prisma`, `python`, `python_fastapi`, the `ruby` family, `structured_json`, `wsdl`, `xsd`. A `terraform/` directory has no tracked source.

Every language parser subclasses `BaseParser` (`parsers/base.py`) and exposes `PARSERS = [...]` in its `__init__.py`. Framework parsers subclass their language parser, set a `priority`, and win a file through `claims(path, source)`. Additive detectors register through `register_detector` / `register_fact` (`parsers/additive.py`) and own no file. Internal helpers are not listed.

## python

| Symbol | Notes |
|---|---|
| `PythonParser` — `src/breezeai_cog/parsers/python/parser.py:26` | Base `BaseParser`; name `python`, extension `.py`. Reports frameworks `fastapi`, `flask`, `django`. `build_index` returns `PythonIndex`; `extract` builds Class/Function/Statement records. |
| `PythonIndex` — `src/breezeai_cog/parsers/python/index.py:20` | Repo index holding registered facts, built once per repo by `build_python_index` (line 44); only files matching a fact gate are parsed. |
| `build_class` — `src/breezeai_cog/parsers/python/classes.py:85` | Builds a Class from a `class_definition`; `iter_definitions` (line 66) walks the body including decorated definitions. |
| `build_function` — `src/breezeai_cog/parsers/python/functions.py:150` | Builds a Function; `extract_decorators`, `extract_params`, `collect_nested_functions` (line 88) support it. |
| `extract_imports` — `src/breezeai_cog/parsers/python/imports.py:163` | Import extraction with in-repo resolution through `_Roots`. |
| `extract_statements` — `src/breezeai_cog/parsers/python/statements.py:121` | Statement walk over control flow, jumps and declarations; semantic call classification goes through `parsers/detection`. |
| `CeleryTasks` — `src/breezeai_cog/parsers/python/celery_tasks.py:58` | Value of fact `celery.tasks`: task name (explicit `name=` or `<module>.<function>`) per task; `TaskDef` (line 47), `task_defs` (line 110), `beat_entries` (line 141). |
| `schedule` — `src/breezeai_cog/parsers/python/celery.py:70` | Additive detector `celery`: `eventbus_consumer` for tasks, `eventbus_send` for `.delay` / `send_task`, beat schedules. |

## python_fastapi

| Symbol | Notes |
|---|---|
| `FastAPIParser` — `src/breezeai_cog/parsers/python_fastapi/parser.py:16` | Base `PythonParser`, name `python-fastapi`, priority 10. `claims` is true when the bytes `fastapi` occur in the source. Under statement capture, `detect_routes` output is applied and `record.framework` becomes `fastapi`. |
| `detect_routes` — `src/breezeai_cog/parsers/python_fastapi/routes.py:218` | Finds `@app.get` / `@router.post` style decorators; emits `route` statements parented to the handler function. |
| `FastapiWiring` — `src/breezeai_cog/parsers/python_fastapi/wiring.py:44` | Value of fact `fastapi.wiring`: cross-file `APIRouter` prefix, `include_router` and mount wiring; accessor `fastapi_wiring` (line 51). |
| `FileFacts` — `src/breezeai_cog/parsers/python_fastapi/file_facts.py:140` | Per-file router, prefix, dependency and middleware facts; built by `file_facts` (line 289), shared with the route detector. |
| `certain_auth` — `src/breezeai_cog/parsers/python_fastapi/auth.py:78` | Names of dependencies that certainly reject anonymous calls (FastAPI security schemes with `auto_error` on); only these assert `authRequired`. |

## ruby

| Symbol | Notes |
|---|---|
| `RubyParser` — `src/breezeai_cog/parsers/ruby/parser.py:26` | Base `BaseParser`; name `ruby`, extension `.rb`; `claims_accepts_timeout = True`. No frameworks of its own. |
| `RubyIndex` — `src/breezeai_cog/parsers/ruby/index.py:22` | Repo index of ActiveRecord model constants plus registered facts; `build_ruby_index` (line 49). |
| `models_from_parents` — `src/breezeai_cog/parsers/ruby/models.py:128` | Resolves model classes from declared parent chains (`resolve_models`, line 106); `model_receivers` (line 149) gates data-access hints. |
| `has_require` — `src/breezeai_cog/parsers/ruby/claims.py:18` | AST claim helpers with `has_superclass` (line 32) and `has_call` (line 41); used by every Ruby framework parser. |
| `build_class` — `src/breezeai_cog/parsers/ruby/classes.py:25` | Class/module records; `iter_definitions` (line 96). `build_function` is at `functions.py:116`, `extract_imports` at `imports.py:37`. |
| `owner_id` — `src/breezeai_cog/parsers/ruby/ownership.py:8` | Picks the owning function/class id for a source line. |
| `extract_statements` — `src/breezeai_cog/parsers/ruby/statement.py:68` | Statement extraction; stops descent at class/module/method. |

## ruby_rails

| Symbol | Notes |
|---|---|
| `RailsParser` — `src/breezeai_cog/parsers/ruby_rails/parser.py:14` | Base `RubyParser`, name `ruby-rails`, priority 30, framework `rails`. Claims on `require 'rails'` / `action_controller/railtie`, a `ApplicationController` superclass, or `Rails.application.routes.draw`. |
| `detect_rails_routes` — `src/breezeai_cog/parsers/ruby_rails/routes.py:352` | Conservative route detection in `routes.rb`: verbs, `resources`, `scope`, `namespace`; attaches controller guards. |
| `controller_callbacks` — `src/breezeai_cog/parsers/ruby_rails/callbacks.py:173` | Fact `rails.controller-callbacks`: `before_action` guards per controller; `guards_for` (line 208), `callbacks_of` (line 214). |

## ruby_grape

| Symbol | Notes |
|---|---|
| `GrapeParser` — `src/breezeai_cog/parsers/ruby_grape/parser.py:14` | Base `RubyParser`, name `ruby-grape`, priority 20, framework `grape`. Claims on `require 'grape'` or a `Grape::API` / `Grape::Endpoint` superclass. |
| `detect_grape_routes` — `src/breezeai_cog/parsers/ruby_grape/routes.py:193` | Conservative route detection inside Grape API classes; handles prefix, version and namespace blocks. |

## ruby_sinatra

| Symbol | Notes |
|---|---|
| `SinatraParser` — `src/breezeai_cog/parsers/ruby_sinatra/parser.py:14` | Base `RubyParser`, name `ruby-sinatra`, priority 10, framework `sinatra`. Claims on `require 'sinatra'` or a `Sinatra::Base` / `Sinatra::Application` superclass. |
| `detect_sinatra_routes` — `src/breezeai_cog/parsers/ruby_sinatra/routes.py:165` | Conservative detection of `get '/path'` style route blocks. |

## c

| Symbol | Notes |
|---|---|
| `CParser` — `src/breezeai_cog/parsers/c/parser.py:33` | Base `CFamilyParser`; name `c`; grammar `c`; extensions `.c .h`; `related_languages` is `cpp`; no frameworks. Its `ownership` supplies only `references` (the includes it reads). |
| `CParser.callable_in` — `src/breezeai_cog/parsers/c/parser.py:45` | Returns the C++ index's `c_linkage` map: C can call only C++ functions defined inside `extern "C"`. |
| `build_c_index` — `src/breezeai_cog/parsers/c/index.py:26` | Builds the shared `CFamilyIndex` over C files with the C grammar; `collect_c_facts` (line 16) and `resolve_c_facts` (line 21) run the facts registered for language `c`. |

C is parsed with the tree-sitter C grammar, because C++ reads some valid C (`int new;`, K&R definitions) as errors. Its `ignore.txt` lists vendored directories and is applied with the C++ list: a header shared by both languages is dropped only when both lists drop it.

## cfamily_common

Extraction shared by C and C++ (`parsers/cfamily_common/`). The pipeline reads the C++ ones through `CppParser` and the C ones through `CParser`.

| Symbol | Notes |
|---|---|
| `CFamilyParser` — `src/breezeai_cog/parsers/cfamily_common/parser.py:82` | Base `BaseParser` shared by both languages; sets `build_index_accepts_timeout`; `parse_file` parses with the subclass's `grammar`, `extract` (line 106) builds the `FileRecord`. Subclasses supply `callable_in` (line 98) and `run_detectors` (line 102). |
| `CFamilyIndex` — `src/breezeai_cog/parsers/cfamily_common/index.py:70` | Repo index: `headers`, `includes`, `included_by`, `funcs`, `c_linkage`, `qual`, `fields`, `heritage`, `classdecl`, `facts`. A key two files claim is `None`. |
| `build_cfamily_index` — `src/breezeai_cog/parsers/cfamily_common/index.py:293` | Header map plus a parse pre-pass over definitions, with the language's `grammar`, fact `collect` and `resolve` callables and a parse timeout. |
| `make_cfamily_resolver` — `src/breezeai_cog/parsers/cfamily_common/index.py:482` | Call resolver over the file's own index and the callable maps of related languages; a call resolves only into a header the file can see (`header_visibility`, line 458). |
| `IncludeGraph` — `src/breezeai_cog/parsers/cfamily_common/index.py:407` | Transitive `#include` closure (`closure`, line 427) and includers (line 447) over the headers of both languages. |
| `include_targets` — `src/breezeai_cog/parsers/cfamily_common/imports.py:26` | Quoted `#include` targets as written, read from bytes with no parse; used by the ownership step and the index. System includes are left out. |
| `build_header_index` — `src/breezeai_cog/parsers/cfamily_common/imports.py:39` | Header basename to repo path; `FamilyHeaders` (line 49) reads the own map with the related language's maps; `extract_imports` (line 77) resolves quoted includes. |
| `build_class` — `src/breezeai_cog/parsers/cfamily_common/classes.py:165` | Class/struct/union records, with `alias` set to the typedef tag when it differs; `build_enum` (line 290), `type_alias_statement` (line 146), `typedef_type` (line 69). |
| `build_function` — `src/breezeai_cog/parsers/cfamily_common/functions.py:299` | Free functions; `build_member_function` (line 356), `extract_params` (line 122), `has_declaration_error` (line 44), `is_misread_declarator` (line 84). |
| `extract_statements` — `src/breezeai_cog/parsers/cfamily_common/statements.py:92` | Statement walk; lambdas attribute to the enclosing function. `build_type_map` is at `types.py:129`. |

## cpp

| Symbol | Notes |
|---|---|
| `CppParser` — `src/breezeai_cog/parsers/cpp/parser.py:26` | Base `CFamilyParser`; name `cpp`; grammar `cpp`; extensions `.cpp .cc .cxx .c++ .hpp .h .hh .hxx .ipp .inl` (not `.c`); `related_languages` is `c`. Its `ownership` proves C++ by `cpp_only_syntax`, includes `c`, and defaults `.h`. No frameworks. |
| `CppParser.callable_in` — `src/breezeai_cog/parsers/cpp/parser.py:45` | Returns the C index's `funcs` map: C++ can call every C function. |
| `build_cpp_index` — `src/breezeai_cog/parsers/cpp/index.py:26` | Builds the shared `CFamilyIndex` over C++ files with the C++ grammar; `collect_cpp_facts` (line 16), `resolve_cpp_facts` (line 21). |
| `cpp_only_syntax` — `src/breezeai_cog/parsers/cpp/ownership.py:74` | True when a header uses syntax C lacks (`namespace`, `class`, `template`, `std::`, a linkage specification). Comments and strings are ignored; inside `#if` groups testing `__cplusplus` only a C++ declaration counts. |

## go

| Symbol | Notes |
|---|---|
| `GoParser` — `src/breezeai_cog/parsers/go/parser.py:54` | Base `BaseParser`; name `go`; claims `.go`, `go.mod`, `go.sum`; `parse_file` routes the module files to `parse_gomod` / `parse_gosum`. No frameworks of its own. |
| `GoIndex` — `src/breezeai_cog/parsers/go/imports.py:19` | Repo index; `build_fqcn_index` (line 61), `import_aliases` (line 148), `extract_imports` (line 164), `default_package_name` (line 116). |
| `Declarations` — `src/breezeai_cog/parsers/go/declarations.py:42` | Value of fact `go.declarations`: where functions, methods, package names and struct field types are declared; accessor `declarations` (line 176). |
| `build_class` — `src/breezeai_cog/parsers/go/classes.py:74` | Struct/interface records. `build_function` and `build_anonymous_function` are at `functions.py:173` and `functions.py:214`; `scan_file` at line 119. |
| `parse_gomod` — `src/breezeai_cog/parsers/go/gomod.py:8` | go.mod metadata; `parse_gosum` is at `gosum.py:8`. |
| `RouterSpec` — `src/breezeai_cog/parsers/go/routing.py:54` | Describes a router framework (import path, constructors, verb and group methods); `analyze` (line 717) and `detect_routes` (line 876) do the shared analysis; `register_router_fact` (line 788). |
| `Value` — `src/breezeai_cog/parsers/go/values.py:49` | Typed-value analysis for non-router libraries; `ValueSpec` (line 29), `TypedCall` (line 56), `typed_calls` (line 205). |
| `extract_statements` — `src/breezeai_cog/parsers/go/statements.py:82` | Statement walk; closures attribute to the enclosing function. |

## go detectors

All are additive detectors gated on `go` with a byte guard and `skip_fixtures`; they own no file.

| Symbol | Notes |
|---|---|
| `SPEC` — `src/breezeai_cog/parsers/go_nethttp/routes.py:30` | net/http `ServeMux` and default mux, order 10, guard `"net/http"`; a Go 1.22 pattern verb goes to `method`. |
| `SPEC` — `src/breezeai_cog/parsers/go_gin/routes.py:18` | Gin routes, order 20, guard `gin-gonic/gin`; `Any` is `ANY`, `Handle` takes the verb, static helpers have no handler. |
| `SPEC` — `src/breezeai_cog/parsers/go_echo/routes.py:18` | Echo routes, order 30, guard `labstack/echo`; `CONNECT`/`TRACE` are not recorded. |
| `SPEC` — `src/breezeai_cog/parsers/go_chi/routes.py:20` | chi routes, order 40, guard `go-chi/chi`; `Route`, `Mount`, `Group`, `With`, `Method`; `Handle` records an empty method. |
| `SPEC` — `src/breezeai_cog/parsers/go_fiber/routes.py:19` | Fiber routes, order 50, guard `gofiber/fiber`; `All`, `Add`, `Static`, `Mount`. |
| `SPEC` — `src/breezeai_cog/parsers/go_gorilla/routes.py:55` | gorilla/mux routes, order 60, guard `gorilla/mux`; one route per `.Methods(...)` verb, none means empty method. |
| `detect_grpc` — `src/breezeai_cog/parsers/go_grpc/routes.py:110` | Order 70, guard `Register`; one `route` (kind rpc) per RPC where a `RegisterXServer` call binds an implementation. |
| `services` — `src/breezeai_cog/parsers/go_grpc/services.py:139` | Fact `grpc.services`: service/method descriptors read from generated `*_grpc.pb.go`; `_collect` (line 104), `_resolve` (line 131). |
| `detect_nats` — `src/breezeai_cog/parsers/go_nats/events.py:53` | Order 80, guard `nats-io/nats.go`; Subscribe becomes `eventbus_consumer`, Publish `eventbus_publish`, Request `eventbus_send`. |
| `detect_kafka` — `src/breezeai_cog/parsers/go_kafka/events.py:88` | Order 90; segmentio, sarama, confluent: readers/consumers become `eventbus_consumer`, writers/producers `eventbus_send`; non-literal topic leaves `endpoint` empty. |
| `detect_scheduling` — `src/breezeai_cog/parsers/go_scheduling/timers.py:64` | Order 100; robfig/cron and `time` tickers/`AfterFunc` become `timer` with interval in milliseconds; `time.After` is ignored. |
| `detect_access` — `src/breezeai_cog/parsers/go_database/access.py:130` | Order 110, no guard; `db_method_call` only when the receiver is bound to a database library type (sql, sqlx, gorm, pgx, mongodb, redis). |
| `detect_lambda` — `src/breezeai_cog/parsers/go_lambda/handlers.py:82` | Order 120, guard `aws-lambda-go/lambda`; `lambda.Start` binds the handler; event type picks `eventbus_consumer` or an API Gateway `route`. |

## php

| Symbol | Notes |
|---|---|
| `PhpParser` — `src/breezeai_cog/parsers/php/parser.py:42` | Base `BaseParser`; name `php`; extensions `.php`, `.phtml`; `matches` rejects `.blade.php`. Frameworks list `laravel`, `codeigniter`, `slim`, `symfony`, `wordpress` in mappings. `handler_locator` (line 61) resolves route handler names to declarations. |
| `PhpIndex` — `src/breezeai_cog/parsers/php/index.py:28` | Repo FQCN index; `build_fqcn_index` (line 102), `namespace_of` (line 58). |
| `build_class` — `src/breezeai_cog/parsers/php/classes.py:21` | Class/interface/trait/enum records. `build_function` is at `functions.py:144`, `extract_attributes` at line 24. |
| `use_aliases` — `src/breezeai_cog/parsers/php/imports.py:131` | `use` alias map; `resolve_class` (line 142), `extract_imports` (line 157). |
| `Rules` — `src/breezeai_cog/parsers/php/receivers.py:45` | Evidence-based receiver typing for call chains; `outermost_calls` (line 223), `method_name` (line 65), `chain` (line 81). |
| `detect_php_data_access` — `src/breezeai_cog/parsers/php/data_access.py:222` | Additive; `db_method_call` with hints `eloquent`, `doctrine`, `codeigniter-builder`, only on proven receivers. Fact `php.model-classes` supplies Eloquent models. |
| `detect_php_http_calls` — `src/breezeai_cog/parsers/php/http_calls.py:94` | Additive; `api_call` only where the client is proven (Guzzle etc.), never by variable name. |
| `detect_wordpress_hooks` — `src/breezeai_cog/parsers/php/wordpress.py:11` | Additive; WordPress hook registrations as event consumers. |
| `handler_locator` — `src/breezeai_cog/parsers/php/handlers.py:68` | Looks up handlers named as `Controller@method`, invokables or function names. |

## php frameworks

| Symbol | Notes |
|---|---|
| `LaravelParser` — `src/breezeai_cog/parsers/php_laravel/parser.py:470` | Base `PhpParser`, name `php-laravel`, priority 10. Claims on `Illuminate\Support\Facades\Route/Event/Bus`, `Illuminate\Routing\` with `Route::`, or any `use`/`namespace illuminate\`. |
| `detect_laravel_routes` — `src/breezeai_cog/parsers/php_laravel/parser.py:338` | `Route::` verbs, groups, `resource`/`apiResource`; skipped for fixture files. |
| `detect_laravel_events` — `src/breezeai_cog/parsers/php_laravel/events.py:102` | Additive for every PHP file: event and command bus consumers and senders. |
| `CodeIgniterParser` — `src/breezeai_cog/parsers/php_codeigniter/parser.py:342` | Base `PhpParser`, name `php-codeigniter`, priority 20. Claims on a CodeIgniter declaration, or `$routes->` route calls; a filename is not evidence. |
| `detect_codeigniter_routes` — `src/breezeai_cog/parsers/php_codeigniter/parser.py:247` | `$routes->` verbs, groups, resources; events via `_detect_codeigniter_events` (line 219). |
| `SlimParser` — `src/breezeai_cog/parsers/php_slim/parser.py:189` | Base `PhpParser`, name `php-slim`, priority 30. Claims on `slim\`, `RouteCollectorProxy`, `RouteCollectorInterface`. |
| `detect_slim_routes` — `src/breezeai_cog/parsers/php_slim/parser.py:109` | Slim route calls and groups; runs only if `_may_contain_slim_routes` passes. |
| `SymfonyParser` — `src/breezeai_cog/parsers/php_symfony/parser.py:17` | Base `PhpParser`, name `php-symfony`, priority 40. Refuses files that mention `illuminate\`; claims on Symfony routing/framework namespaces, `#[Route`, `RoutingConfigurator`. |
| `detect_symfony_routes` — `src/breezeai_cog/parsers/php_symfony/routes.py:202` | Attribute routes; `detect_symfony_route_builders` (line 334) for configurator routes; `strip_route_decorators` (line 379). |
| `detect_symfony_events` — `src/breezeai_cog/parsers/php_symfony/events.py:36` | Additive for every PHP file: EventDispatcher and Messenger. |
| `TwigParser` — `src/breezeai_cog/parsers/php_twig/parser.py:189` | Base `BaseParser`; name `twig`; extension `.twig`; framework `twig`. Emits template directive and `output_directive` statements. |
| `TwigTemplateIndex` — `src/breezeai_cog/parsers/php_twig/parser.py:30` | Resolves a template reference to a unique repo path; ambiguous suffix matches stay unresolved. |

## apex

| Symbol | Notes |
|---|---|
| `ApexParser` — `src/breezeai_cog/parsers/apex/parser.py:32` | Base `BaseParser`; name `apex`; extensions `.cls`, `.trigger`. `claims` rejects VB6 class modules (`VERSION` plus `Attribute VB_Name`) and LaTeX (leading `\` or `%`). Frameworks `apex-rest`, `apex-aura`, `apex-soap`, `apex-trigger`, `apex-platform-event`, `apex-schedule`. |
| `ApexIndex` — `src/breezeai_cog/parsers/apex/index.py:24` | Lower-cased type name to declaring file (Apex has no imports); duplicates map to `None`; `build_class_index` (line 55). |
| `build_class` — `src/breezeai_cog/parsers/apex/classes.py:49` | Class/interface/enum records. `build_method` (`functions.py:108`) and `build_trigger` (`functions.py:151`). |
| `classify_node` — `src/breezeai_cog/parsers/apex/detect.py:265` | Classifies SOQL/SOSL, DML, callouts, `System.schedule`, `EventBus.publish`; names compared lower-case; `in_test_code` (line 94). |
| `detect_apex_routes` — `src/breezeai_cog/parsers/apex/routes.py:78` | Entry points: `@RestResource` HTTP methods (under `/services/apexrest`), `@AuraEnabled`, `webservice` methods, triggers. `@IsTest` classes emit nothing. |
| `enrich_jobs` — `src/breezeai_cog/parsers/apex/jobs.py:60` | Adds framework labels and the `execute` handler of scheduled job classes. |
| `extract_statements` — `src/breezeai_cog/parsers/apex/statements.py:45` | Statement walk over control flow, jumps and declarations. |
| `declared_name` — `src/breezeai_cog/parsers/apex/modifiers.py:19` | With `modifier_keywords`, `visibility_of` (default private), `sharing_of`, `extract_annotations`, `has_annotation`. |

## graphql

| Symbol | Notes |
|---|---|
| `GraphQLParser` — `src/breezeai_cog/parsers/graphql/parser.py:33` | Base `BaseParser`; name `graphql`; extensions `.graphql .gql .graphqls`; framework `graphql`. Standalone SDL/operation files; embedded `gql` in TS is handled elsewhere. Keeps real grammar `nodeType`s. |
| `extract_graphql` — `src/breezeai_cog/parsers/graphql/sdl.py:297` | Shared SDL extractor (also used by the embedded TS path); `root_field_dtos` (line 263), `request_dto` (line 228), `parse_key_selection` (line 135). |
| `GraphQLIndex` — `src/breezeai_cog/parsers/graphql/index.py:19` | Registered facts of GraphQL files; `build_graphql_index` (line 44). |
| `is_client_copy` — `src/breezeai_cog/parsers/graphql/client_configs.py:24` | Fact `graphql.client-configs`: a `.graphqlrc.json` marks a schema copy as a client project, so its Query fields are not this repo's routes. |

## prisma

| Symbol | Notes |
|---|---|
| `PrismaParser` — `src/breezeai_cog/parsers/prisma/parser.py:27` | Base `BaseParser`; name `prisma`; extension `.prisma`; framework `prisma`. Node types `model_block`, `enum_block`, `key_value_block`. Composite `type` blocks and empty blocks parse to ERROR and are not captured. |
| `collect_prisma_statements` — `src/breezeai_cog/parsers/prisma/schema.py:47` | One statement per top-level block; `model` becomes a `data_model` entity; enum and config blocks are plain statements. |

## hcl

| Symbol | Notes |
|---|---|
| `HclParser` — `src/breezeai_cog/parsers/hcl/parser.py:263` | Base `BaseParser`; name `hcl`; extensions `.tf .tfvars .hcl`. One statement per top-level block with verbatim text; `module` blocks also yield Class records; module and provider sources go to `externalImports`. |
| `TerraformIndex` — `src/breezeai_cog/parsers/hcl/parser.py:34` | Directory to `.tf` files map used to resolve local module sources. |
| `_SEMANTIC_TYPE` — `src/breezeai_cog/parsers/hcl/parser.py:47` | `resource` is `iac_resource`, `data` is `iac_data`, `module` is `iac_module`; variable/output/locals/provider/terraform have none. Variable values emit `iac_variable_value`. |
| `_terraform_address` — `src/breezeai_cog/parsers/hcl/parser.py:173` | Terraform resource address used as the statement `endpoint`; platform comes from `aws_`, `azurerm_`, `google_` prefixes. |

## hcl_terraform

| Symbol | Notes |
|---|---|
| `TerraformParser` — `src/breezeai_cog/parsers/hcl_terraform/parser.py:23` | Base `HclParser`, name `hcl-terraform`, priority 10, framework `terraform`. Claims `.tf` and `.tfvars` always, and `.hcl` only when a Terraform block marker matches. |

## jslt

| Symbol | Notes |
|---|---|
| `JsltParser` — `src/breezeai_cog/parsers/jslt/parser.py:29` | Base `BaseParser`; name `jslt`; extension `.jslt`. No tree-sitter grammar: a hand-rolled line scan. `def` functions are captured always; statements and comments are not captured as AST comments. Node type `synthetic`. |
| `collect_jslt_declarations` — `src/breezeai_cog/parsers/jslt/statements.py:203` | Emits `import`, `def`, `let` declarations and the module output expression. |

## xsd and wsdl

| Symbol | Notes |
|---|---|
| `XsdParser` — `src/breezeai_cog/parsers/xsd/parser.py:30` | Base `BaseParser`; name `xsd`; extension `.xsd`; framework `xsd`. Uses the `xml` grammar, so every node type is `element`. |
| `collect_schema_statements` — `src/breezeai_cog/parsers/xsd/schema.py:64` | One statement per global declaration; `schema_locations` (line 51) gives include/import targets. Also reused for schemas embedded in WSDL. |
| `WsdlParser` — `src/breezeai_cog/parsers/wsdl/parser.py:37` | Base `BaseParser`; name `wsdl`; extension `.wsdl`; framework `wsdl`. WSDL 1.1 only. `data_model` per message, plain statement per portType operation, `import` per `wsdl:import`. Records no entry points. |
| `collect_definitions_statements` — `src/breezeai_cog/parsers/wsdl/operations.py:158` | Walks `definitions`; `definitions_locations` (line 147). |

## structured_json

| Symbol | Notes |
|---|---|
| `JsonParser` — `src/breezeai_cog/parsers/structured_json/parser.py:37` | Base `BaseParser`; name `json`; extensions `.json`, `serverless.template`; `serialization = "toon"`. Sole owner of `.json`. Named configs in `RICH_JSON_NAMES` and empty or scalar JSON become `type="config"`; all other JSON becomes a `structured_data` statement. |
| `encode` — `src/breezeai_cog/parsers/structured_json/toon.py:50` | Delegates to the `toon-format` library; returns `Encoded` (line 45). |
| `redact_secrets` — `src/breezeai_cog/parsers/structured_json/redaction.py:59` | Value-level redaction of unambiguous secret formats, in addition to key-name redaction. |

## config

| Symbol | Notes |
|---|---|
| `ConfigParser` — `src/breezeai_cog/parsers/config/parser.py:23` | Base `BaseParser`; name `config`. Claims `.yaml .yml .toml .ini .xml .gradle .sbt`, project files (`.csproj .vbproj .fsproj .vcxproj .sln`) and named files (Dockerfile, Makefile, requirements.txt, Pipfile, `.env`, README, LICENSE, ignore files). `matches` also takes `Dockerfile.*`, `.env.*` and .NET config. Not `.json`. |
| `extract_config` — `src/breezeai_cog/parsers/config/extractors.py:61` | Returns the structural `metadata` dict per file type; `is_dotnet_config` (line 438). Output is `FileRecord(type="config")`. |

## detection

| Symbol | Notes |
|---|---|
| `classify_call` — `src/breezeai_cog/parsers/detection/__init__.py:13` | Classifies a normalized call: API first, then raw query, then DB method; returns `(semanticType, method, dataAccessHint)` or `None`. |
| `match_api` — `src/breezeai_cog/parsers/detection/api_calls.py:73` | HTTP verb for an HTTP-client call, else `None`. |
| `match_db` — `src/breezeai_cog/parsers/detection/db_queries.py:308` | Inferred `dataAccessHint` (ORM/DB) with a language-aware .NET gate. |
| `is_query` — `src/breezeai_cog/parsers/detection/queries.py:44` | Raw SQL/JPQL detection; `text_has_query` (line 65). `TRUNCATE` counts only as `TRUNCATE TABLE` or `TRUNCATE name` ending at `;`, `,`, `)`, `CASCADE`, `RESTART`, `CONTINUE` or end of text, so prose such as "Truncate nothing" is not SQL. |
| `detect_sdk_calls` — `src/breezeai_cog/parsers/detection/sdk_calls.py:690` | Additive TypeScript detector `sdk-calls`, order 30: outbound vendor-SDK integration calls through typed clients. |
