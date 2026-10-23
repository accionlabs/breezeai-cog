# Ruby Capture Audit — shared Ruby parser, Sinatra, Grape, Rails

Audit of `breezeai-cog` Ruby capture against `docs/architecture.md`, `docs/parser-reference.md`,
`docs/parser-review-guide.md` and `skills/extend-capture/SKILL.md`.
Date of audit: 2026-10-21. Branch `main` @ `b9d9f33` **plus a large uncommitted working tree** (§2).

**This is an audit only. No source, test, schema, config or existing capture artifact was modified.**
The only file created inside the repository is this `review.md` (untracked). Note: the review guide
(Part B) asks that reports live outside the repo; it is placed at the repo root because the task asked for
`review.md`. Move it before committing if the guide is to be followed.

Evidence tags used below: **[R]** real capture / real source in `testing/`, **[S]** synthetic in-memory probe
(parser run on a snippet, nothing written to the repo), **[C]** code reading, **[T]** existing test.
Scratch scripts and fresh captures were written only to
`C:\Users\KEERTH~1\AppData\Local\Temp\claude\C--breezeai-cog\fc61ac7c-2ea2-472a-87f6-802f7574f441\scratchpad\`.

---

## 1. Executive summary

**Scope.** Shared Ruby parser (`parsers/ruby/`), the three registered Ruby framework parsers
(`ruby-sinatra` p10, `ruby-grape` p20, `ruby-rails` p30 — no others exist), and the core changes in the working
tree that Ruby depends on (`core/registry.py`, `core/scanner.py`, `core/executor.py`, `core/pipeline.py`,
`parsers/base.py`, `parsers/detection/*`, `services/diff.py`).

**Result: 29 confirmed gaps — 3 Critical, 3 High, 18 Medium, 5 Low** (severity per review guide Part B).

| Severity | Count | Gaps |
|---|---|---|
| Critical | 3 | G-01 spec/test harness routes emitted as app routes (Sinatra: 62 of 67 routes); G-02 interpolated paths truncated to a prefix and emitted as the endpoint; G-03 Rails routes inside `namespace`/`scope` get unprefixed, wrong endpoints |
| High | 3 | G-04 framework claims too narrow (derived apps, `Sinatra.new`, `::Sinatra::Base` fall to the generic parser, routes lost); G-05 `private def x` / `protected def` / defs inside `begin`/`case`/modifier-`if` not captured; G-06 `require` resolution wrong both ways (false edges, and `lib/` never searched) |
| Medium | 18 | G-07 … G-24 |
| Low | 5 | G-25 … G-29 |

**Audit coverage — partial, not exhaustive.**

| Area | Coverage |
|---|---|
| Grape repo (`testing/grape`, 437 files scanned) | Class/module and function counts, source locations, IDs/parents, schema, route scan: **exhaustive** against an independent tree-sitter walk |
| Sinatra repo (`testing/sinatra`, 201 files scanned) | Same as above: **exhaustive** |
| Rubocop capture (`output/rubocop-*`) | Integrity checks only. **Source repo is not present** (`testing/rubocop` missing) and no skip report exists → cannot compare to source or regenerate |
| Rails | **No Rails fixture repo.** Only 5 unit tests + synthetic probes. Everything Rails is unverified on real code |
| Server mode (`services/diff.py`, `server/`) | Not executed (`fastapi` not installed); static read of the diff only |
| Target Spec (Confluence) | Not accessible; field/enum acceptance by the backend not checked |

**Major unverified areas:** Rails on real code; regenerated Rubocop capture; Grape positional-semantics of
`prefix`/`version` (no framework reference in the contract); backend acceptance of `framework` values
`rails`/`sinatra`/`grape`; parse-timeout behaviour on pathological files.

**What is working (in proportion):** class/module discovery is exact on both repos (195/443 and 50/122);
every function and class `startLine`/`endLine` and every statement id/column matches the source (exhaustive,
~2,000 functions, ~810 classes, ~51,000 statements); no duplicate ids, no unresolved `parentId`, 0 schema
violations in all five artifacts; visibility sections, `private :x`, `private_class_method`, `class << self`,
operator/setter names, top-level privacy and param kinds `*rest/**opts/&blk` are correct; Sinatra
namespace composition and Grape `prefix`+path-version composition are correct for literal values.

---

## 2. Repository and working-tree context

### 2.1 State before the audit (`git status --short`, recorded first)

HEAD `b9d9f33` ("Merge pull request #133 …"). 34 modified/deleted tracked files + 4 untracked entries. After the
audit `git status --short` is **identical** (38 entries) and `output/*` mtimes are unchanged.

| Group | Files (all pre-existing changes, none made by this audit) |
|---|---|
| Docs | `docs/architecture.md`, `docs/parser-reference.md`, `docs/parser-review-guide.md` |
| Core (affects every language) | `core/executor.py`, `core/pipeline.py`, `core/registry.py`, `core/scanner.py`, `parsers/base.py`, `parsers/detection/api_calls.py`, `parsers/detection/db_queries.py`, `services/diff.py` |
| Ruby language parser | `parsers/ruby/{__init__,claims,classes,functions,imports,models,parser}.py`; `statement.py` **deleted**, `statements.py` **untracked/new** (rename to the guide's layout) |
| Framework parsers | `ruby_grape/{parser,routes}.py`, `ruby_rails/parser.py`, `ruby_sinatra/{parser,routes}.py` |
| Tests | `test_detection, test_pipeline, test_registry, test_ruby_{grape,parser,rails,sinatra}_parser, test_scaling, test_scanner, test_server_diff` |
| Untracked | `.vscode/`, `output/`, `testing/` (two independent git repos: `testing/grape` @ `0342ddc`, `testing/sinatra` @ `cb22afd`), `src/.../ruby/statements.py` |

Diff size: 3,212 insertions / 375 deletions over 34 files. Per review guide §1 the core-zone and
language-parser changes get the "highest/high" review level; **no before/after capture diff for any other
language is present in the repo** (G-12, G-14, G-24, G-27 are consequences).

### 2.2 Available sources and artifacts

| Item | Present | Notes |
|---|---|---|
| `testing/grape` (Grape, Ruby) | yes | own git repo; 437 files scanned |
| `testing/sinatra` (Sinatra + rack-protection + sinatra-contrib) | yes | own git repo; 201 files scanned |
| Rails repo | **no** | |
| Rubocop repo | **no** | only its capture exists |
| `output/grape-project-analysis.ndjson.gz` | yes | generated 2026-10-21T05:26Z — **stale** (§3.3) |
| `output/sinatra-project-analysis.ndjson.gz` | yes | generated 2026-10-20T12:02Z — **stale** (§3.3) |
| `output/rubocop-project-analysis.ndjson.gz` + `.ndjson/rubocop-project-analysis.json` | yes | same content (identical metadata line, 1,811 lines each); generated 2026-10-19 — **stale**; the `.ndjson` "file" is actually a directory |
| `testing/grape/.cog/grape-skipped-report.json` | yes | belongs to Grape (§7) |
| `testing/sinatra/.cog/sinatra-skipped-report.json` | yes | belongs to Sinatra (§7) |
| Rubocop skip report | **missing** | limitation recorded; not inferred from another report |
| `.cog/breezeai-cog-skipped-report.json` (repo root) | yes | belongs to *breezeai-cog itself* (2026-10-19); **not used** |

### 2.3 Limitations caused by the pre-existing changes

* Recent behaviour (Grape `prefix`+version, Sinatra namespaces, claims via AST, visibility) exists only in the
  working tree; none of it is committed, so "HEAD behaviour" was reconstructed with `git archive HEAD` into the
  scratchpad solely for comparison.
* The full test suite was run on the working tree (§13). The four failures were re-run on the `git archive HEAD`
  copy and fail identically there, so they are **not** caused by the working-tree changes.
* Fresh captures were produced in-process with `AnalysisService.analyze_repo(out=<scratchpad>)` — **not** with the
  CLI, because the CLI writes `<repo>/.cog/*` and would overwrite the existing skip reports. `.cog` mtimes were
  verified unchanged afterwards.

---

## 3. Capture metrics and artifact integrity

### 3.1 Metrics

"Fresh" = captured by this audit from the current working tree into the scratchpad. "Existing" = file in `output/`.

| Capture | Scanned | Parsed (code + config) | Skipped | Functions | Classes: `totalClasses` / Class records (classes + modules) | Statements | LOC (`totalLinesOfCode`) | Parse failures |
|---|---|---|---|---|---|---|---|---|
| Grape — fresh | 437 | 403 (384 + 19) | 34 | 1,248 | 195 / 638 (195 + 443) | 41,259 | 42,459 (code only; 46,462 incl. config) | 0 |
| Grape — existing | same | 403 | n/a | 1,248 | 195 / 638 | 41,259 | 42,459 | n/a |
| Sinatra — fresh | 201 | 124 (106 + 18) | 77 | 682 | 50 / 172 (50 + 122) | 9,921 | 12,684 (code only; 15,646 incl. config) | 0 |
| Sinatra — existing | n/a | 124 | n/a | **676** | 50 / 172 | **10,139** | 12,684 | n/a |
| Rubocop — existing | n/a | 1,810 (1,791 + 19) | n/a (no report) | 7,937 | **3,676** / 3,676 (965 + 2,711) | 153,238 | **344,259** (= code 336,880 + config 7,379) | n/a |

Reconciliation `scanned = parsed + skipped` holds for both fresh runs (437 = 403 + 34; 201 = 124 + 77).

### 3.2 Independent count validation (tree-sitter walk written for the audit, not the parser's own code)

| Measure | Source (independent) | Capture | Verdict |
|---|---|---|---|
| Grape `class` nodes | 195 | 195 | exact |
| Grape `module` nodes | 443 | 443 | exact |
| Sinatra `class` / `module` nodes | 50 / 122 | 50 / 122 | exact |
| Grape `method`+`singleton_method` nodes | 1,260 | 1,248 | **12 missing** in 11 files (G-05, G-08) |
| Sinatra `method`+`singleton_method` nodes | 688 | 682 | **6 missing** in 4 files (G-05, G-08) |
| Sinatra block-bearing `get/post/…/route` calls (non-ignored source) | 66 | 64 route sites | 1 true negative (`@params.delete('q') { 1.0 }`), **1 real miss** `lib/sinatra/base.rb:2026` (G-04) |
| Grape block-bearing verb calls | 1,093 | 27 | 1,065 are in `spec/` (`Class.new(Grape::API)` fixtures, unclaimed — acceptable); **1 non-spec miss** `benchmark/issue_mounting.rb:23` (G-19) |
| Function/class `startLine`/`endLine` vs source line (starts at `def`/`class|module`, ends at `end`) | 1,930 functions, 810 classes | 0 mismatches | exact |
| Statement `id` line:col vs `startLine` and source text at that column | 51,123 statements | 0 mismatches | exact |
| Source files with tree-sitter `ERROR` nodes | 2 (`grape/lib/grape/dsl/routing.rb:78`, `lib/grape/validations/params_scope.rb:416`; both anonymous `&` block-forwarding) | records produced, function counts matched | grammar limitation, recorded (§10) |

### 3.3 Integrity (all five artifacts)

| Check | Grape fresh | Grape existing | Sinatra fresh | Sinatra existing | Rubocop existing |
|---|---|---|---|---|---|
| Duplicate ids (file+class+function+statement, global) | 0 | 0 | 0 | 0 | 0 |
| Unresolved `parentId` | 0 | 0 | 0 | 0 | 0 |
| `startLine < 1` or `endLine < startLine` | 0 | 0 | 0 | 0 | 0 |
| JSON-Schema (`FileRecord.model_json_schema`) invalid records | 0 | 0 | 0 | 0 | 0 |
| Non-comment statement outside its parent's line span | 0 | 0 | 0 | 0 | 0 |
| `metadata` vs records | see G-27 | | | | |
| Statement pairs identical in span+type+text, same parent | 56 (§G-10/G-07/G-11) | 56 | 23 | 254 | 824 |

Comment statements legitimately sit outside their parent's span (the shared binder attaches a leading comment to
the declaration that follows it — 726 in Grape, e.g. `# frozen_string_literal` bound to `Acme`); verified as
by-design, not counted as a gap.

### 3.4 Freshness — which artifacts do not reflect the current code

| Artifact | Evidence it is stale |
|---|---|
| `output/grape-…ndjson.gz` | **7 of 403 files differ from a fresh capture.** All differences are route `endpoint`/`version`: e.g. `benchmark/simple.rb:10` old `/` → new `/api/v1/` + `version: v1`; `benchmark/grape_on_rack/api/path_versioning.rb:10` old `/` → new `/vendor`…. Confirms the artifact predates the `prefix :api` + path-version change. **It is therefore not evidence for or against the current Grape code.** |
| `output/sinatra-…ndjson.gz` | **27 of 124 files differ.** Old: 676 functions, 63 routes, 16 sinatra files, `<anonymous>` function names (20) and 83 statements parented to `…#<anonymous>@N`, 254 duplicate-span pairs, top-level `Rakefile` functions `public`. New: 682 functions, 67 routes, 17 files, 0 anonymous, 23 duplicate pairs, `Rakefile` functions `private`. |
| `output/rubocop-…` | `totalClasses` = 3,676 (counts modules) and `totalLinesOfCode` = 344,259 (counts config) = the **pre-change metric rules**; 15 `<anonymous>` functions; 165 `api_call` statements that are `x.fetch('Enabled', true)` calls (`cop_config.fetch`, `ENV.fetch`) — the shared-detector change removed that pattern (§G-14). Cannot be regenerated (source absent). |

---

## 4. Confirmed gaps

Format per gap: **Where** (framework / layer / file:line) · **Expected vs actual** · **Evidence & root cause** ·
**Impact** · **Fix** · **Regression test**. A short repro is given per gap; `R`/`S` = real/synthetic.

### Critical

#### G-01 Spec/test-harness routes are captured as application routes (Sinatra)
**Severity** Critical · **Framework** Sinatra (+ shared fixture policy) · **Layer** claims + `BaseParser.is_fixture_file`
**Where** `ruby_sinatra/parser.py:43-47` (`spec_helper` claim path), `parsers/base.py:38,143-159`, `core/default_ignores.txt:80-84`.
**Expected** Route emitters must skip fixture/test harness files (`parser-reference.md` Step 5c; `architecture.md` §6 decision "detection that emits routes skips fixture files"; review guide §5 "a test helper `router.get(...)` in a non-fixture file").
**Actual [R]** In the fresh Sinatra capture **62 of the 67 route statements come from 14 `sinatra-contrib/spec/*_spec.rb` files** (`json_spec.rb` 12, `namespace_spec.rb` 11, `respond_with_spec.rb` 11, `multi_route_spec.rb` 8, …); only 5 come from `examples/`. [S] `spec/app_spec.rb`, `test/app_test.rb`, `spec/support/app.rb`, `app_spec.rb` each yield `GET /x` with `framework=sinatra`.
**Cause [C]** `fixture_markers()` only knows the JS-style infixes `.test.`/`.spec.` (`base.py:38`); Ruby's `_spec.rb`, `_test.rb`, `spec/`, `test/`(only `test/` is pruned by default ignores, `spec/` is not) are unknown. The Sinatra claim additionally *selects* spec files on purpose (`require 'spec_helper'` + any `Sinatra::` constant), and **the test `test_helper_loaded_sinatra_source_selects_sinatra_parser` asserts that `@cookie_app.get('/')` in `spec/cookies_spec.rb` is a route** — the working-tree test suite encodes behaviour opposite to the documented contract.
**Impact** The graph gets ~93% fake Sinatra routes; downstream impact analysis/tracepath treats harness paths as the application's API.
**Fix** Add Ruby fixture markers (`_spec.rb`, `_test.rb`, `spec/`, `test/` segments, `spec_helper`/`support`), and make route emitters skip them; drop (or invert) the helper-loaded claim test. Decision needed in §9 D-12 (capture spec files at all?).
**Regression test** `spec/app_spec.rb` containing `require 'spec_helper'; mock_app { get('/x') { } }` → no `route` statement; `examples/app.rb` with the same body → one route.

#### G-02 String-interpolated paths are truncated to their first literal segment and emitted as the endpoint
**Severity** Critical · **Framework** Sinatra, Grape, Rails, shared `api_call` · **Layer** route/statement value extraction
**Where** `ruby_sinatra/routes.py:66-70`, `ruby_grape/routes.py:38-47` (`_literal_path`, also `_literal_value` 50-59), `ruby_rails/routes.py:29-33`, `ruby/statements.py:46-52` (`_render_url`).
**Expected** Honest-null (`extend-capture` §1): a value not fully present in the syntax stays null / the statement is not emitted. `architecture.md` §10: constants fold only when compile-time.
**Actual [R]** Grape fresh capture: `benchmark/compile_many_routes.rb:12` `get "/test#{index}/"` → `/api/v1/test`; `benchmark/version_throughput/app.rb:36` `get "/resource#{index}"` → `/api/v1/resource`; `:50` `get "/resource#{index}/:id"` → `/api/v1/resource` (the `/:id` segment is lost). [S] Sinatra `get "/users/#{id}/edit"` → `/users/`; Rails `get "/dyn/#{x}", to: …` → `/dyn/`; `HTTParty.get("https://api.example.com/users/#{id}")` → `api_call` `endpoint=https://api.example.com/users/`.
**Cause [C]** every helper reads only the **first `string_content` child** of the `string` node and ignores `interpolation` and later content, so a dynamic string looks like a static prefix.
**Impact** Confident wrong endpoints (they also collide with the genuine `/resource` route).
**Fix** Treat a `string` as static only when all named children are `string_content`/`escape_sequence`; otherwise return `None` (skip route / null endpoint). One shared helper for all four call sites.
**Regression test** For each of Sinatra/Grape/Rails/`api_call`: a double-quoted path with `#{…}` → no route / `endpoint is None`; single-quoted `'/x/#{y}'` (literal `#{`) → emitted verbatim.

#### G-03 Rails routes inside `namespace` / `scope` / `resources` blocks get wrong, unprefixed endpoints
**Severity** Critical · **Framework** Rails · **Layer** `ruby_rails/routes.py`
**Where** `ruby_rails/routes.py:64-98`.
**Expected** Endpoint composed from enclosing `namespace`/`scope` (the review guide §7.1 names "path prefixes" and "mounts" as required route forms), or left null.
**Actual [S]** inside `Rails.application.routes.draw`: `namespace :admin { get "dashboard", to: "admin#dash" }` → endpoint `dashboard` (should be `/admin/dashboard`); nested `namespace :v2 { get "deep" }` → `deep`; `scope "/api" { get "scoped" }` → `scoped`. Also no leading `/` normalisation, so the same route is `/health` or `health` depending on how it is written.
**Cause [C]** the detector only checks "is inside `routes.draw`" and reads `args[0]`; it never walks enclosing `namespace`/`scope`/`resources`/`member` calls (Sinatra and Grape detectors do).
**Impact** A wrong value is presented as the endpoint (violates honest-null).
**Fix** Either compose prefixes (like Sinatra `_namespace_prefix`) or set `endpoint=None` when any enclosing prefix-bearing call exists. Decide `resources`/`root`/`mount` in §9 D-8.
**Regression test** The three snippets above must yield `/admin/dashboard`, `/admin/v2/deep`, `/api/scoped` (or `None`), never the bare segment.

### High

#### G-04 Framework claims are too narrow — whole route files fall through to the generic Ruby parser
**Severity** High · **Framework** Sinatra, Grape (Rails n/a) · **Layer** `claims()` (`ruby/claims.py`, `ruby_sinatra/parser.py:27-47`, `ruby_grape/parser.py:20-35`)
**Expected** Review guide §4 "claim too narrow … files fall back to the base parser and lose their routes — Gap".
**Actual [R]** `lib/sinatra/base.rb` (defines `class Base` and, inside `class << self` → `configure :development do`, line 2026 `get '/__sinatra__/:image.png' do`) has `framework=None`; its route is the one real miss in the Sinatra route scan. [S] each yields `parser=ruby`, no routes: `class Admin < BaseApp; get('/x') { } end` (derived app, no `require 'sinatra'`); `class A < ::Sinatra::Base`; `App = Sinatra.new do get '/x' { } end`; `Class.new(Sinatra::Base) do …`; `require 'sinatra/base'` + route DSL without a direct `Sinatra::Base` superclass; Grape `class A < ::Grape::API`; Grape `class Users < ApplicationAPI` (indirect base).
**Cause [C]** Evidence is limited to an exact `require 'sinatra'|'grape'` and an exact-string superclass in `{Sinatra::Base, Sinatra::Application}` / `{Grape::API, Grape::Endpoint}`. `has_superclass` compares the text including a leading `::`; indirect bases need a cross-file heritage index that Ruby does not build.
**Impact** The commonest modular layouts (base app class + subclasses in separate files; `config.ru` apps) lose all routes.
**Fix** Accept leading `::`, `Sinatra.new`, `Class.new(<base>)`, `require 'sinatra/base'`; for indirect bases use a Ruby `build_index` heritage (`index_common.ClassHeritage`) — needs D-13.
**Regression test** One test per bullet above asserting the file is owned by the framework parser and routes are emitted.

#### G-05 Methods declared as `private def …` / `protected def …` / `public def …` / `private_class_method def self.…` are not captured
**Severity** High · **Framework** shared Ruby · **Layer** `ruby/classes.py:32-57` (`_conditional_definitions`), `iter_definitions` (224-229)
**Where / Expected** Function discovery (review guide §7.1 "functions, methods"; task §C "methods nested inside conditionals").
**Actual [S]** inside a class: `private def inline_priv; end`, `protected def …`, `public def …`, `private_class_method def self.inline_cm; end` → **no Function record**, and no body statements either: the whole method survives only as one class-level `call` statement whose text is `private def x(a) … end`. Likewise missing: `def mod_if; end if RUBY_VERSION` (`if_modifier`), `def` inside `begin … end` and `case/when`. Present: `if/else`-nested defs.
**Cause [C]** `_conditional_definitions` handles `if/unless/elsif/else/then/body_statement` only; a `def` that is the argument of a `call`, or inside `if_modifier`/`begin`/`case`, is never visited. [R] one real instance: `sinatra/lib/sinatra/indifferent_hash.rb:185` `def except(*keys) … end if Gem::Version.new(RUBY_VERSION) >= …`.
**Impact** `private def` is the dominant modern idiom; those methods (and their visibility) vanish.
**Fix** Descend into `call` arguments of visibility/`private_class_method` calls and into `if_modifier`/`unless_modifier`/`begin`/`case`; derive visibility from the wrapping call.
**Regression test** The five snippets above; assert name, owner, `visibility`, `isStatic`.

#### G-06 `require` resolution is wrong in both directions
**Severity** High · **Framework** shared Ruby · **Layer** `ruby/imports.py:20-70`
**Expected** `importFiles` only for certain in-repo targets; honest-null otherwise (architecture §10).
**Actual** *False negatives [R]:* 59 of the 120 `externalImports` in the Sinatra capture name a file that exists as `lib/<req>.rb` or `<gem>/lib/<req>.rb` (e.g. `lib/sinatra/base.rb:22` `require 'sinatra/indifferent_hash'` → `lib/sinatra/indifferent_hash.rb`; `require 'sinatra'` in `examples/*.rb` → `lib/sinatra.rb`; `require 'rack/protection'` → `rack-protection/lib/rack/protection.rb`) yet only 21 `importFiles` exist in the whole capture (19 of them `spec_helper`/`okjson` siblings). Grape: 8 of 78. *Wrong `require_relative` [R]:* `lib/sinatra/base.rb:26` `require_relative 'middleware/logger'` is **not** resolved (`importFiles` of `base.rb` is empty) because a `require_relative` without a leading `./` is looked up from the repo root instead of the file's folder (`imports.py:30-35`). `docs/architecture/cross-file-resolution.md` §10 lists this as a *known limitation* ("Ruby `require_relative "x/y"` (no `./`) resolves from the repo root — it should resolve from the file's own folder"); the only test (`test_ruby_require_relative_does_not_bind_lowercase_basename`) uses an importer at the repo root, so it cannot fail on it. *False positives [R]:* `lib/sinatra/middleware/logger.rb:3` `require 'logger'` (the stdlib) is recorded as an import of **itself** (`lib/sinatra/middleware/logger.rb → lib/sinatra/middleware/logger.rb`) because the fallback looks in the requiring file's own directory. [S] `require 'json'` → sibling `lib/app/json.rb`; `require 'index'` → `lib/app/index.rb` (a Node convention, `imports.py:38`). Ruby never searches the requiring file's directory for a bare `require`. *Garbage [S]:* `require "app/#{x}"` stores the raw template `app/#{x}` in `externalImports`; `autoload` and `load` are ignored.
**Cause [C]** `_resolve_require`/`_resolve_bare_require` tries repo root, then the file's own directory; never `lib/`, gemspec `require_paths`, or monorepo `*/lib`.
**Impact** Missing IMPORTS edges for most intra-repo requires; spurious edges for stdlib-named siblings.
**Fix** Resolve `require_relative` always from the file's folder; resolve bare requires against `lib/` and `*/lib` (and `spec/` for `spec_helper`), never the requiring file's directory; drop `index.rb`; skip non-literal strings.
**Regression test** Tmp repo `lib/app/{main,json,util}.rb`, `lib/foo.rb`: in `lib/app/main.rb`, `require_relative 'util'` → `lib/app/util.rb`; `require 'json'` → external; `require 'foo'` → `lib/foo.rb`; `require "x/#{y}"` → nothing; `lib/x/logger.rb` with `require 'logger'` → no self-import.

### Medium

#### G-07 Grape and Rails routes are emitted twice (plain `call` + `route`) — Sinatra enriches
**Medium** · `ruby_grape/routes.py:221-238`, `ruby_rails/routes.py:78-96` vs `ruby_sinatra/routes.py:181-195`.
**Expected** "Enrich or append … one statement per piece of source, avoids duplicates" (`architecture.md` §7); `docs/architecture/statement-model.md` §4 decision "enrich first, append only when nothing is there" and §9 rule "Enrich an existing statement before appending a new one".
**Actual [R]** 27 of 27 Grape route statements have a second non-semantic `call` statement at the same position (`benchmark/compile_many_routes.rb:12:4` and `:12:4#2`); Sinatra: 0 of 67. [S] Rails likewise (`2:2`, `2:2#2`).
**Fix** Enrich the existing statement like Sinatra. **Test** exactly one statement at the route's line:col, `semanticType=route`.

#### G-08 Block-scoped method definitions: dropped in classes, mis-owned at file scope, `class << obj` mis-attributed
**Medium** · `ruby/classes.py:60-221`, `ruby/classes.py:224-229`.
**Actual [R]** 18 methods missing across the two repos, all inside blocks: `Data.define(…) do def initialize` (`grape/lib/grape/dsl/rescue_options.rb:20`, 5 more files), `Struct.new(…) do def create` (`middleware/auth/strategy_info.rb:7`), `Class.new { def inspect = … }` (`util/translation.rb:10`), `class_eval do def call/safe?/idempotent?` (`sinatra/base.rb:267`, `sinatra-contrib/lib/sinatra/webdav.rb:44,48`), `::Module.new do def log` (`quiet_logger.rb:48`), `def block.each` (`sinatra/base.rb:297`). [S] Same construct is **dropped inside a class** (`helpers do def h`) but **kept at file scope** with the *file* as owner (`Foo.class_eval do def top_eval` → function of the file); `class << obj` (non-`self`) methods become plain file functions (`on_obj@50`).
**Cause** Only `test_ruby_dynamic_class_eval_method_is_not_assigned_lexical_owner` documents the class_eval case; the rest is accidental. The *inconsistency* is the defect; the *owner policy* is D-4.
**Fix/Test** Apply one policy to both scopes; `class << obj` must not produce file-level functions.

#### G-09 Nested class/module ids and names are not qualified (contract: `path#Outer.Inner`)
**Medium** · `ruby/classes.py:105-108` (`class_id(path, name)`; `name` = declared text), `emit/ids.py:6`.
**Expected** `architecture.md` §8: "Class `path#Class` (nested: `path#Outer.Inner`)".
**Actual [R]** 420/420 nested Grape classes, 108/108 Sinatra, 2,677/2,677 Rubocop have unqualified-name ids; collisions are patched with `#2` (`grape/lib/grape/json.rb#Json#2`, `…/middleware/stack.rb#Middleware#2`). [S] `module A; module B; class C` → ids `#A`, `#B`, `#C`; yet `class Foo::Qualified` → `#Foo::Qualified` (qualified only when written qualified). Function ids use the simple class name too. `Class.name` feeds EXTENDS-by-name resolution.
**Fix** Qualify with lexical nesting (separator is D-16). **Test** two `Error` classes in different modules in one file get distinct, stable ids.

#### G-10 `enum_member` is a false positive and a duplicate
**Medium** · `ruby/statements.py:68-120`, `ruby/classes.py:128-131`.
**Actual [R]** every `enum_member` also exists as a plain `assignment` at the same span (`sinatra/lib/sinatra/version.rb:4:2` and `4:2#2`; 23 in Grape, 3 in Sinatra, 11 in Rubocop). The heuristic "module containing only constant assignments" labels `VERSION = '4.2.1'` and config constants as enum members; `RescueOptions = Data.define(…) do … end` (`grape/lib/grape/dsl/rescue_options.rb:19:4#2`) is labelled `enum_member`. [S] `module Cfg; DEFAULT_TIMEOUT = 30; RETRIES = 3; end` → 2 `enum_member`; adding `def self.all` flips it off (`singleton_method` disqualifies, `method` does not).
**Fix** Enrich instead of append; tighten (or drop) the heuristic; D-note in §9. **Test** `VERSION` module → one statement, no enum_member.

#### G-11 Missing statement kinds the mapping says it covers
**Medium** · `ruby/mappings.py:8-32`.
**Actual [S]** not emitted: `@iv ||= 3`/`x += 1` (`operator_assignment`), `yield x`, `super`, modifier forms (`if_modifier`, `unless_modifier`, `while_modifier`, `rescue_modifier`), ternary `conditional`, `case … in` (`case_match`), `rescue`/`ensure` clauses. Only the inner `assignment` of `a = 1 if x` is kept; the condition itself is not a statement. `mappings.py` claims "Branch / Loop / Exception / Jump" parity.
**Fix** Add the node types to `CONTROL_FLOW`/`JUMP`/`DECLARATIONS`. **Test** one snippet per node type.

#### G-12 Scanner opens every unsupported file (violates the "scan reads paths only" decision)
**Medium** · `core/scanner.py:137-146`.
**Expected** `architecture.md` §4.1 decision: "the scan reads paths only … never opens a file"; §11 "The scan never reads file contents."
**Actual [R]** instrumenting `open()` during `_scan_entries`: Grape 31 files opened (17 `.gemfile`, 7 `.md`, 4 extensionless, 1 `.ru`, 1 `.sh`, 1 `.png`), Sinatra 77 (every `.erb/.haml/.slim/.png/.md`…). The code reads one line of **every** file with no language, then asks `matches_source`, which only acts on suffix-less files — the suffix test happens *after* the read. Applies to every language's repository. The decision text in the architecture doc is unchanged, so doc and code now disagree.
**Fix** Only read when `Path(rel).suffix == ""` (and no parser matched); or update the decision. **Test** scanning `a.png`, `b.md` opens neither; suffix-less shebang file still discovered.

#### G-13 Ruby `build_index` parses without the configured timeout
**Medium** · `ruby/models.py:372-374` (`[(root, f, 0) …]`), docstring at `models.py:296-309`.
**Expected** "Every parse has a time limit" (architecture §4.4); "any secondary `parse_source` must thread `ctx.parse_timeout_micros`" (reference Step 0).
**Actual [C]** The worker docstring says "O2 fix: Uses the caller-supplied `timeout_micros`", but the only caller passes literal `0` (no limit); `build_index` has no timeout parameter. A pathological `.rb` hangs the **single pre-pass** for the whole repo. The worker also swallows every `Exception` silently (`except Exception: return [],[],[]`) — against "fail loudly at boundaries" (`extend-capture` §6).
**Fix** Thread the setting into the index options; log the swallowed failure. **Test** a worker with a tiny timeout returns within bound and logs.

#### G-14 Shared `api_calls.py` change fixes a Ruby false positive but regresses every other language
**Status: Verified fixed** (re-verified in the working tree; original finding text below is kept as history).
**Medium** · `parsers/detection/api_calls.py:79` (diff: `m in _BARE_FUNCTIONS … or low.endswith(".fetch")` → `low in _BARE_FUNCTIONS`).
**Actual [S]** `classify_call(callee, method, arg)`: HEAD → `api_call GET` for `window.fetch`, `globalThis.fetch`, `this.fetch`, `self.fetch`, `ENV.fetch`, `cop_config.fetch`, `params.fetch`; now → `None` for all (only bare `fetch` remains). The Ruby false positives are fixed (the old Rubocop artifact contains such hits among its 165 `api_call` statements), but `window.fetch(url)` in TypeScript/JS is silently lost. No test covers it; no before/after capture diff exists (core-zone requirement, review guide §1.2).
**Fix** Keep `window/globalThis/self/this.fetch` (language-gate the `.fetch` removal to Ruby). **Test** `classify_call("window.fetch","fetch","/x")` → `api_call` for `typescript`, `None` for `ruby`.

**Resolution (verified fixed).**
* **Root cause:** a Ruby-only false-positive fix removed `low.endswith(".fetch")` from `match_api` for every language, so `window.fetch`, `globalThis.fetch`, `this.fetch` and `self.fetch` stopped being `api_call` in TypeScript/JavaScript.
* **Fix** (`parsers/detection/api_calls.py`, `parsers/detection/__init__.py`): `classify_call` passes `language` to `match_api`; the rule is now `low in _BARE_FUNCTIONS or (language != "ruby" and low.endswith(".fetch"))`. Bare `fetch` still matches for all languages.
* **Observed [S]** (`classify_call(callee, "fetch", "/x", language=…)`): `typescript` and `javascript` → `api_call GET` for `fetch`, `window.fetch`, `globalThis.fetch`, `this.fetch`, `self.fetch`; `ruby` → `api_call` only for bare `fetch`, `None` for `window/globalThis/this/self.fetch`, `ENV.fetch` and `cop_config.fetch`.
* **Tests:** `tests/unit/test_detection.py` 33 passed; `test_typescript_parser.py` 56; `test_ruby_parser.py` 39; `test_python_parser.py` 14; combined detection + Ruby + TypeScript + Python run: 142 passed (re-run during this update). Ruff on the five changed files and `git diff --check` on them passed (reported by the implementer, not re-run here).
* **Not verified:** no real-repository capture comparison was performed and no capture artifact was regenerated, so this is verified by unit and parser tests only, not at capture level.
* **Remaining limitation:** for every non-Ruby language the `.fetch` suffix rule is unchanged from HEAD, so receivers such as `ENV.fetch`/`params.fetch` in Python or TypeScript are still tagged `api_call`. That is pre-existing behaviour, not a regression of this change, and is not addressed here.

#### G-15 `cache.get(key)` is tagged `db_method_call` / `redis` on the receiver name alone
**Medium** · `parsers/detection/db_queries.py` (shared, unchanged from HEAD), reached from `ruby/statements.py`.
**Expected** `docs/architecture/statement-model.md` §3 decision: the shared classifiers "must not fire on look-alikes (`Array.find`, `cache.get`, `"Create account"`)"; review guide §5 names `cache.get(key)` as a false-positive example; absent beats wrong (`extend-capture` §1).
**Actual [S]** Ruby `cache.get(key)` → `db_method_call`, `method=get`, `dataAccessHint=redis`. Identical at HEAD, so pre-existing and cross-language; Ruby inherits it. There is no Redis evidence (no `Redis.new`, no `require "redis"`) — the receiver spelling is the only signal.
**Fix** Require positive Redis evidence (typed/assigned receiver or import). **Test** negative: `cache.get(key)` untagged without Redis evidence; `r = Redis.new; r.get(k)` still tagged.

#### G-16 `.ru` (Rack config) files are skipped as unsupported although they hold framework code
**Medium** · `ruby/parser.py:38` (`extensions`).
**Actual [R]** `sinatra/examples/stream.ru` (skip report) contains `class Stream < Sinatra::Base; get '/' do … end; end; run Stream` — one class and one route lost. `grape/benchmark/grape_on_rack/config.ru` (`Acme::API.compile!; run Acme::App.instance`) holds the mount point. Skip report counts: `.ru` ×1 in each repo.
**Fix** Add `.ru` to Ruby extensions. **Test** `config.ru` with a Sinatra class is scanned and yields the route.

#### G-17 Super-linear scaling in Ruby extraction and Grape route detection
**Medium** · `ruby/models.py:407-463` (`extract_param_types`), `ruby/functions.py:172-213` (`_extract_return_type`), `ruby_grape/routes.py:86-177`.
**Expected** Fixed pass budget, no per-item scan (architecture §11; guide §6.1).
**Actual [S]** timings (seconds) for 500 / 1,000 / 2,000 / 4,000 items: class with N methods **0.36 / 1.36 / 12.96 / 64.43**; Grape N routes **1.15 / 4.15 / 20.38 / 101.44**; Sinatra N routes 0.12 / 0.33 / 0.78 / 3.08 (last doubling ×3.96); Sinatra N namespaces 0.17 / 0.49 / 1.06 / 2.91 (≈ linear). cProfile: for methods, 1.77 s of 4.3 s in `extract_param_types` and 1.74 s in `_extract_return_type` (each does `parent.named_children`, `children.index(node)`, and slices all preceding siblings per method → O(n²)); for Grape, 4.5 M `_method()` calls for 1,500 routes from `_class_prefix_for`/`_version_for`, which re-scan the whole class body for every route. `tests/unit/test_scaling.py` passes because its Ruby sample has no leading comments and few routes.
**Fix** Precompute sibling index/leading-comment map and class `prefix`/`version` declarations once per scope. **Test** extend `test_scaling` with a class of N commented methods and N Grape routes.

#### G-18 Rails: static route values left null; `root`/`resources`/`mount` not represented
**Medium** · `ruby_rails/routes.py:52-98`.
**Actual [S]** `get "about" => "pages#about"` → `GET`, endpoint **None**, handler **None** (hash-rocket form); `get :sym, to: "p#s"` → endpoint None; `get "/c", controller: "cc", action: "aa"` → handler None; `match "/m", to: "x#y", via: [:get, :post]` → method None (honest). No statement at all for `root "home#index"`, `resources :users`, `resource`, `mount Sidekiq::Web => "/sidekiq"`, `devise_for`, concerns. `Rails.application.routes.draw` is the only entry (engines: `MyEngine::Engine.routes.draw` is not claimed → generic parser). Controller `index`/`show` ↔ route `handler: "x#y"` is a string, not a link (D-10).
**Fix** Read static values from `pair` (string key), symbol paths, `controller:`/`action:`; representation of `resources`/`root`/`mount` is D-8. **Test** each form.

#### G-19 Grape route forms not captured or partially captured
**Medium** · `ruby_grape/routes.py:14-16, 81-84, 187-240`.
**Actual** [S] `get '/a', '/b' do` → only `/a` (Sinatra emits both) — silent loss; `route :any, '/any'` and `route [:get,:post], '/multi'` → nothing; `get('/seg') { }` (brace block) → nothing (`_has_block` requires `do_block`); `segment :s do …` not a prefix; `class A < Grape::API::Instance` is claimed but routes are not detected (`Instance` not in `_GRAPE_API_SUPERCLASSES`); `version 'v2','v3', using: :header` keeps only `v2`. [R] `benchmark/issue_mounting.rb:23` `RootAPI = Class.new(Grape::API) do … delete :test do` → file claimed by `ruby-grape` but no route and `framework=None` (anonymous API classes never match `class` nodes). `mount V2::API` / `mount V3::API => '/v3'` produce nothing (D-8).
**Fix/Test** one test per form above.

#### G-20 Chained calls resolve to the current file (false CALLS edge)
**Medium** · `ruby/functions.py:220-257` (`_calls_in_block`).
**Actual [S]** in a class defining `def save`, the call `x.y.save` is stored as `Call(name='save', path='app.rb')` (and `A.new.save`, `::A.save` collapse into the receiver-less key) — the receiver `x.y` is a `call` node, so `parts == ['save']`, `receiver=None`, and the same-file tier "defined here" fires. Duplicate `Call(save, None)` entries also occur for different receivers.
**Fix** Treat any non-simple receiver as unresolved; key on the receiver text. **Test** `x.y.save` and `A.new.save` → `path is None`.

#### G-21 Sinatra route receiver heuristic: false positive on any bare `get`, false negatives on extension idioms
**Medium** · `ruby_sinatra/routes.py:34-51`, `:154-157`.
**Actual [S]** receiver-less `get(...) { }` is accepted anywhere in a claimed file, e.g. `class NotSinatra; def run; get('/not'){ }; end; end` → `GET /not` route. `Ext.registered(app) → app.get('/ext') { }` and `get '/x', &handler` produce **no** route (only the receiver names `klass`, `@app`, `*_app` are accepted; a `&proc` argument is not a block). `app.helpers do def h` also loses `h`.
**Fix** Require Sinatra app scope (inside a Sinatra class/top-level of a classic file) for bare calls; accept the `registered(<param>)` first parameter and `block_argument`. **Test** the three snippets.

#### G-22 Class/module `visibility` is hard-coded `public`
**Medium** · `ruby/classes.py:215`.
**Actual [R]** all 638 Grape and 172 Sinatra class/module records, and all 3,676 Rubocop ones, are `public`; `private_constant :MISSING` (`grape/lib/grape/util/translation.rb:11`) is ignored. Review guide §5 lists "a defaulted value presented as if it was read" as a false-positive source; `parser-reference.md:315-318` still states the same default.
**Fix** Either set `None` (unknown) or implement `private_constant`. **Test** `private_constant :X` marks `X`.

#### G-23 Ruby `spec/` trees are parsed as source (46% of Grape/Rubocop code files)
**Medium** · `core/default_ignores.txt:80-87`, `parsers/ruby/ignore.txt` (empty).
**Actual [R]** only `test/` and JS-style `*.spec.*` are ignored. Grape: 177 of 384 code files are `spec/…`; Rubocop: 833 of 1,791. Sinatra's own `test/` is dropped (ignored directories `['.cog','.git','test']`) while `sinatra-contrib/spec/` and `rack-protection/spec/` are captured — an inconsistent rule. Fixture apps under `spec/fixtures/**` still emit db calls (`Book.all`, `@book.save` in Rubocop `spec/fixtures/.../books_controller.rb`) because the fixture guard is route-only.
**Fix** Add `spec/`, `*_spec.rb`, `*_test.rb` to Ruby ignore defaults (D-12). **Test** scanner skips them.

#### G-24 Type-check regression (`uv run mypy` is the documented workflow)
**Medium** · `parsers/base.py` (widened `claims` signature), `core/registry.py:75-118`.
**Actual** `mypy`: **224 errors now vs 189 at HEAD**; 25 are new `Signature of "claims" incompatible with supertype "BaseParser"` in non-Ruby parsers (e.g. `csharp_aspnet`, `csharp_graphql`), 10 new in `core/registry.py` (the `inspect.signature` dispatch builds untyped arg lists), 2 in `ruby/functions.py`, 2 in `ruby/models.py`, 5 in `ruby_grape/routes.py` (variable `key` reused with two types). `ruff` reports one new `F841` (`ruby/models.py:462`, unused `params_node`). `registry.select` also calls `inspect.signature` for every candidate on every file.
**Fix** Give non-Ruby parsers the new signature or make the base keep the two-arg form; cache signatures. **Test** `mypy`/`ruff` clean in CI.

### Low

| ID | Gap | Evidence | Fix / test |
|---|---|---|---|
| G-25 | `public :name` and `protected :name` are ignored; only `private :name` is handled | [S] `private; def d; end; public :d` → `d` stays `private`; `protected :a` → `a` stays `public` (`functions.py:95-161`) | Handle all three symbol forms; test above |
| G-26 | Documentation drift: `parser-reference.md:315-318` still says Ruby visibility "private/protected … not yet mapped"; `architecture.md` §17 rows "Classify by path only" and "Ownership is decided by a cheap substring check" no longer match code; `docs/architecture/cross-file-resolution.md` still says Ruby imports resolve "only `require_relative`" and every `require` is external (code now resolves bare `require`, wrongly — G-06); `parser-review-guide.md:125` contains a stray `` .x`x` `` | [C] | Edit docs |
| G-27 | Project-metadata totals differ from the emitted records and the rule changed for all languages: `totalClasses` excludes modules (Grape 195 vs 638 `Class` records, Sinatra 50 vs 172); `totalLinesOfCode` excludes config for **every** language while `totalFiles` still counts config (Grape 403 files incl. 19 config; LOC 42,459 vs 46,462). Not documented; the old Rubocop artifact uses the old rule (3,676 / 344,259). Impact is limited: on `/stream-ingest` the backend recomputes file/function/class/LOC counts from the graph (`graph-mapping.md` §7), but `/upsert` and capture consumers use the numbers | [R] `core/pipeline.py:262-276`, `services/diff.py:46-57` | Decide D-11; document or revert; test totals == documented rule on a fixture with modules and a config file |
| G-28 | Absolute constants miss model evidence: `::User.where(a: 1)` is not tagged although `User < ApplicationRecord` is known | [S] `db_queries.py` exact-receiver rule vs `model_receiver_ids` adding `::` only for ambiguous names | Add `::Name` aliases; test |
| G-29 | Hygiene: duplicate `Call(save, None)` entries from different receivers; `ruff F841`; silently swallowed index-worker exceptions (also G-13) | [S]/[C] | Dedup on receiver text; clean lint |

---

## 5. Function visibility and metadata

### 5.1 Cross-framework comparison

All framework parsers inherit `RubyParser.extract` unchanged; visibility/static/params/returnType are therefore
**identical across Sinatra, Grape and Rails** — there are no framework-specific overrides. The table is the
verified behaviour of the shared parser, with the framework-specific route fields below it.

| Construct | Captured | Status | Evidence |
|---|---|---|---|
| Ordinary instance method, class scope | `type=method`, `visibility=public`, `isStatic=None` | Verified correct within scope | [S][R][T] |
| `def self.x`, `def Foo.x` | `isStatic=True` | Verified correct | [S] |
| `class << self` methods, with `private` inside | `isStatic=True`, visibility per section | Verified correct | [S][T] |
| `class << obj` (non-self) | becomes a file-level function | **G-08** | [S] |
| Section markers `private`/`protected`/`public` | per method | Verified correct | [S][T] |
| `private :a, :b` / `private "a"` | per name | Verified correct | [S][T] |
| `public :a`, `protected :a` | ignored | **G-25** | [S] |
| `private def x`, `protected def x`, `public def x` | not captured | **G-05** | [S] |
| `private_class_method :x, :y` | private + static | Verified correct | [S][T] |
| `private_class_method def self.x` | not captured | **G-05** | [S] |
| `module_function` / `extend self` | `public`, `isStatic=None` | Unresolved design decision (D-3) | [S] |
| `private_constant` | ignored | G-22 | [S] |
| Top-level `def` | `private` (Ruby's real default), `public` after bare `public` | Verified correct; `type="method"` for file-level defs is D-15 | [S][T] |
| Operator / setter / predicate / bang names (`==`, `[]=`, `name=`, `<=>`, `foo?`, `bang!`) | preserved | Verified correct | [S][T] |
| Endless `def x(a) = …` | captured | Verified correct | [S] |
| Params: required, optional (default text), `*rest`, `**opts`, `&blk`, `*`,`**`,`&` | prefix-marked, defaults kept | Verified correct | [S] |
| Required keyword `k:` | name `k`, no default — **indistinguishable from a positional `k`** | Schema limitation → D-2 | [S] |
| `...`, destructuring `(a, b)`, `**nil` | dropped silently | Schema limitation → D-2 | [S] |
| `returnType` | from Sorbet `sig { returns(T) }` / YARD `@return [T]` | Verified correct (tests); absent otherwise (honest) | [T] |
| `Class.type` | `class`/`module` | Verified correct | [R] |
| `Class.visibility` | always `public` | **G-22** | [R] |
| `constructorParams` | from `initialize` | Verified correct | [S] |
| `extends` | declared superclass text (`::` prefix stripped) | Verified correct; mixins → D-1 | [S] |
| Function `startLine/endLine`, ids, `parentId` | exact | Verified correct | [R] exhaustive |

### 5.2 Framework-specific route metadata

| Field | Sinatra | Grape | Rails |
|---|---|---|---|
| `method` | verb; `route :get, :post, …` expands to one statement per verb×path | verb; `route` not supported (G-19) | verb; `match` → `None` (honest) |
| `endpoint` | literal only; namespace prefixes composed | literal + prefix + path-version | literal; **no prefix composition (G-03)** |
| `version` | — | `version` literal (first only) | — |
| `handler`/`handlerLine` | none | none | `to: "c#a"` string only |
| `isRegex` | always `false`; regex routes **not emitted** | always `false` | always `false` |
| statement ownership (`parentId`) | most specific enclosing function/class (`owner_id`) | same | same |
| Duplicate plain statement | no (enriched) | **yes (G-07)** | **yes (G-07)** |

---

## 6. Framework-by-framework review

### 6.1 Shared Ruby parser (`parsers/ruby/`)

* **Registration/layout.** `PARSERS=[RubyParser()]`; `parse_file`/`extract` split present; `statements.py` now
  matches the guide's layout; `mappings.py` present; `ignore.txt`/`include.txt` empty. `schema_version` set. **OK.**
* **Parse budget.** One parse per file (the executor parses Ruby once, passes `parsed_root` to `select` and
  `ParseContext`) — verified in `core/executor.py`; `claims()` of the three frameworks reuse the tree. But
  `build_index` is a *second* full parse of every `.rb` containing `class` (documented as acceptable), without a
  timeout (G-13). Claims run up to three `_nodes` walks each behind weak byte guards (Rails guard is
  `b"rails"|b"Rails"|b"ApplicationController"`, which many files contain) — linear, but 4–7 tree walks per such file.
* **Discovery.** Classes/modules/functions exact except G-05, G-08; locations exact.
* **Statements.** Flat, correct parents, gated by `capture_statements`. Over-capture: **~90% of Grape
  call/assignment statements (35,771 of 39,532) and ~75% of Sinatra (6,856 of 9,103) are sub-expressions nested in
  another statement with the same parent** (e.g. `foo(bar(baz(1)))` → three statements). Not an id/parent defect;
  it contradicts "one line-level unit" only if D-5 is decided that way → **Unresolved design decision.**
* **Comments.** `comment` statements captured (1,123 Grape / 421 Sinatra); binding rule behaves as designed.
* **Semantic classification.** `api_call`, `db_method_call`, `query_statement` are positive-evidence gated for
  ActiveRecord (model index + typed receivers) — checked with look-alikes: `Tempfile.create`, `Settings.all`,
  `Foo.where` (unknown model), `"select an option"`, `puts "delete everything from here"`, `ENV.fetch`,
  `params.fetch` → correctly *not* tagged [S][T]. Gaps: G-02 (`api_call` endpoint), G-15, G-28.
* **Calls.** Bare identifiers deliberately skipped (documented in `functions.py:216-219`); `self.x` and bare
  `foo(args)` resolve to the file; G-20.
* **Imports.** G-06. `exports` collects `attr_*`/`public` names (kept as designed).
* **Unverified:** `Gemfile`/`.gemspec`/`Rakefile` parse as code and emit `gem 'x'` call statements (noise,
  not wrong); `build_index` behaviour with `--jobs>1` on Windows (pool spawn) not exercised.

### 6.2 Sinatra (`ruby-sinatra`, priority 10)

* **Claim [C][T][S].** `b"sinatra"` byte guard + AST evidence (`require 'sinatra'`, or superclass
  `Sinatra::Base|Application`, or `spec_helper` + `Sinatra::` namespace). Comment/string mentions are *not* claimed
  ([S] "comment mention", "string mention" cases → `ruby`). Narrow (G-04), over-broad for specs (G-01).
* **Routes verified correct:** all nine verbs incl. `link`/`unlink`, `route :a, :b, '/p'`, multi-path expansion,
  `''` and `/` paths, brace and `do` blocks, nested `namespace` composition including pathless `get do` inside a
  prefixed namespace, enrich-not-duplicate, parent = enclosing method/class, fixture gate present (but G-01).
  In the real capture, 64 of 66 block-bearing verb sites are captured; the other two are 1 true negative + G-04.
* **Gaps:** G-01, G-02, G-04, G-21; regex/`%r{}` routes, `send(verb, …)`, constants as paths, `get '/x', &h`.
* **Intentional / honest-null (verified):** dynamic/constant paths `get PATH`, `'/a' + SUFFIX`, adjacent literals,
  regex namespaces → not emitted ([S][T]).
* **Unresolved:** regex endpoint encoding (D-6), constant folding (D-7), root-path encoding (D-9).

### 6.3 Grape (`ruby-grape`, priority 20)

* **Claim.** `b"grape"` guard + `require 'grape'` or superclass `Grape::API|Endpoint`. Narrow: G-04, G-19.
* **Verified correct [S][T][R]:** all seven verbs; symbol and string paths; `namespace`/`resource(s)`/`route_param`
  (`:id`, symbol → `:id` segment); class-level `prefix :api`; `version 'v1', using: :path` (class level and
  `version … do` block); `prefix`+`version` order `/api/v1/…`; header/param versions → `version` metadata only,
  path unchanged; prefix/version do not leak between API classes (T); dynamic prefix/version → no route
  (honest-null, T). The fresh capture shows the new `prefix`+version behaviour on real code
  (`benchmark/simple.rb` → `/api/v1/`, `version=v1`).
* **Stale artifact (explicit):** `output/grape-project-analysis.ndjson.gz` predates this behaviour (7 files differ,
  §3.4). It neither proves nor disproves the new code; the fresh capture does.
* **Gaps:** G-02 (3 real routes), G-07 (27/27), G-17, G-19.
* **Captured routes vs effective mounted endpoints:** `mount` is not captured and no prefix from a mount point is
  applied to a mounted API's routes → captured paths are *declaration-local*, not effective. Intentional
  limitation until D-8 (no cross-file mount resolution exists).
* **Unverified:** Grape semantics for `prefix`/`version` declared *after* an endpoint (code uses source order,
  `D` class probe: `get '/before'` → `/before`, `get '/after'` after `prefix :late` → `/late/after`);
  `version` arrays; `mount … => '/v3'`.

### 6.4 Rails (`ruby-rails`, priority 30)

* **Claim.** byte guard (`rails`/`Rails`/`ApplicationController`) + AST: `require 'rails'|'action_controller/railtie'`,
  superclass exactly `ApplicationController`, or `Rails.application.routes.draw`. Comment mentions not claimed ([S]).
* **What it emits.** Only route statements inside `Rails.application.routes.draw`; **no controller/action,
  inheritance or mixin capture beyond the shared parser.** A Rails controller file is owned by `ruby-rails` but
  `record.framework` stays `None` (framework is set only when routes are found — consistent with the reference's
  NestJS example).
* **Verified [S][T]:** `get/post/delete "/p", to: "c#a"` → method, endpoint, `handler`, `handlerLine`;
  `match` → method `None`; dynamic/`to:` lambda → handler `None`; gated by `capture_statements`; routes attach to an
  enclosing class when present; non-application `draw` receivers ignored.
* **Gaps:** G-02, G-03, G-07, G-18; engines not claimed. `ApplicationController` claim yields no detection yet
  takes ownership from Grape/Sinatra (D-13).
* **Everything Rails is unverified on a real repository** (no fixture; 5 unit tests).

### 6.5 Other registered Ruby frameworks

None. `registry.registered()` contains exactly `ruby`, `ruby-grape`, `ruby-rails`, `ruby-sinatra`; no `ruby_*`
package beyond those three exists. Nothing further was invented.

### 6.6 Parser selection and claims overlap (task dimension A)

* Registration correct; derived-framework relationships: none are *derived* from each other, but priorities are
  explicit (Rails 30 > Grape 20 > Sinatra 10).
* [S] mixed-file overlap: a file with `class A < Grape::API; get '/x' do end; end` plus
  `class B < ApplicationController; end` → `ruby-rails` wins, **Grape routes dropped**; `require 'grape'` +
  `require 'sinatra'` → Grape wins. Explicit priority satisfies the guide's "decide the priority explicitly", but the
  loss is silent → **Unresolved design decision D-13** (frequency in real repos unverified).
* Ordinary Ruby files are not claimed by comment/string mentions ([S]); `Gemfile` (`gem 'sinatra'`) is not claimed.
* Valid framework constructs falling to the generic parser: **yes** (G-04, G-19).

---

## 7. File discovery and skipped files

**Supported Ruby extensions/names** (`ruby/parser.py:38`): `.rb`, `.rake`, `.gemspec`, `Gemfile`, `Rakefile`, plus
suffix-less files with a Ruby shebang (new `matches_source`, tests pass). **Not supported:** `.ru`, `.gemfile`,
`Guardfile`, `Dangerfile`, `Capfile`, `.rbi`, `.jbuilder`, `.erb/.haml/.slim` (no Ruby view parser; no
`template_extensions` declared, so they are reported as `unsupported`, not `template`).
**Ignored directories** observed: Grape `['.cog', '.git']`; Sinatra `['.cog', '.git', 'test']` (`test/` comes from
`default_ignores.txt:80`; `spec/` does not — G-23).

**Skip-report provenance.** `testing/grape/.cog/grape-skipped-report.json` (34 skipped, mtime 2026-10-21 10:56) and
`testing/sinatra/.cog/sinatra-skipped-report.json` (77 skipped, 2026-10-20 17:32) match the corresponding artifact
timestamps and repository paths, and a fresh scan reproduces the same counts and reasons (34 = 31 unsupported + 3
template; 77 unsupported). **No Rubocop skip report exists; none was inferred.** The repo-root `.cog` report is for
`breezeai-cog` itself and was not used.

| Repo | Extension / name | Count | Category | Verdict |
|---|---|---|---|---|
| Grape | `.gemfile` | 17 | Ruby DSL (`gemfiles/*.gemfile`, `gem 'rails', '~> 8.0.0'`) | **Unsupported ⚠** (dependency declarations only; low value) |
| Grape | `.md` ×7, `.png` ×1, `.sh` ×1, `<none>` `.rspec/.simplecov/.yardopts` ×3 | 12 | docs / asset / script / dotfiles | Ignored by design |
| Grape | `Dangerfile` | 1 | Ruby tooling (`danger.import_dangerfile`) | Unsupported, no framework content — low |
| Grape | `.ru` (`benchmark/grape_on_rack/config.ru`) | 1 | Rack bootstrap, holds the mount (`run Acme::App.instance`) | **Unsupported ⚠ → G-16** |
| Grape | `.html` ×3 (`benchmark/…/public/*.html`) | 3 | template | Ignored by design (`--capture-templates`) |
| Sinatra | `.erb` 17, `.erubis` 10, `.haml` 10, `.hamlit` 10, `.slim` 10, `.sass` 1, `.yajl` 1, `.txt` 1 | 60 | view/spec fixtures under `sinatra-contrib/spec/` | Intentional: no template parser (unverified for real Rails views) |
| Sinatra | `.md` ×8, `.png` ×2, `<none>` ×6 (`.yardopts`, `VERSION`, `License`, `CODEOWNERS`, `.rspec`) | 16 | docs / asset / metadata | Ignored by design |
| Sinatra | `.ru` (`examples/stream.ru`) | 1 | **real Sinatra class + route** | **Unsupported ⚠ → G-16** |

Source omissions that matter: `examples/stream.ru` (route + class), `config.ru` (mount), and every block-scoped method
(G-08) are code, not documentation. Everything else skipped is docs/assets/tooling/templates.

---

## 8. Schema and serialization review

* **Ids.** Unique per file and globally (all 5 artifacts). Format follows `emit.ids`, **except nested classes (G-09)**.
  `disambiguate` suffixes `#2`… are deterministic; route statements that share a call position with the plain call
  get `#2` (G-07).
* **Parents.** Every `parentId` resolves; no statement/function lies outside its parent's span except comments
  (by design). Route statements are parented by `owner_id` = smallest enclosing function else class else file
  (verified e.g. Sinatra route in `helper_route@17`, Rails route in an enclosing class).
* **Statement ownership.** Correct scope boundaries (statements do not leak across nested `class`/`def`); the two
  defects are duplicates (G-07, G-10) and sub-expression granularity (D-5).
* **Schema validity.** 0 invalid records in all five artifacts against `FileRecord.model_json_schema`. Schema
  limits that affect Ruby: `Parameter` has no *kind*; `Class` has no mixin field (open node — extra attributes would
  be preserved; restricted `Function`/`Statement` would silently drop new fields, `extend-capture` §5); `Function.type`
  is an open string; `ClassType` accepts `module`.
* **Metric consistency.** `totalFunctions` = Σ functions (exact in all artifacts). `totalClasses` ≠ Σ Class records
  for Ruby by design (G-27). `totalLinesOfCode` ≠ Σ file `loc` by design (G-27). `totalFiles` = number of file records.
* **In-memory vs serialized.** Fresh capture JSON re-validated; in-memory probe records and serialized records
  agree on ids/parents (the probe harness uses the same `parse_file`; a byte-level comparison of the in-memory
  record to the NDJSON line was **not** done — see §10).
* **Observation (not scored):** statement `text` keeps the trailing `\r` of CRLF checkouts (`"# frozen_string_literal: true\r"`),
  because both repos are CRLF in this working copy; cause is the environment, not the parser; other parsers were not checked.

---

## 9. Unresolved design decisions (needed before implementation)

| # | Decision | Options | Blocks |
|---|---|---|---|
| D-1 | **Mixins** (`include`/`extend`/`prepend`, `include A, B`): currently nothing is captured except a raw `call` statement. Contract does not map them to `implements`. | (a) `implements` list; (b) open-node attribute on `Class` (e.g. `mixins`); (c) typed statement; (d) leave as statements | mixins fix |
| D-2 | **Parameter kind** (required keyword, `...`, destructuring, `**nil`, anonymous) | add `kind` field (restricted-field backend change) vs encode in `name`/`default` | params fix |
| D-3 | **`module_function` / `extend self`** (one def = private instance + public singleton) | emit two functions, or mark `isStatic` + visibility, or leave | visibility |
| D-4 | **Owner of methods defined in blocks** (`helpers do`, `included do`, `Struct.new do`, `class_eval`, `Module.new`) | lexical class (guess) / skip everywhere (consistent honest-null) / synthetic anonymous owner | G-08 |
| D-5 | **Statement granularity** — keep nested sub-expression `call`s as statements? | statement-level only vs current | statement volume, G-07/G-10 |
| D-6 | **Regex routes** (`get %r{…}`, `/…/`; Grape `requirements:`) — encode pattern text with `isRegex=true`? | emit with pattern / skip | G-04/G-21 |
| D-7 | **Constant folding for Ruby route paths** (`PATH = '/x'; get PATH`) | extend `constfold` to frozen literal constants / leave null | Sinatra/Grape |
| D-8 | **Rails `resources`/`root`/`mount`, Grape/Sinatra `mount`**: expand into synthetic routes, one summary statement, or skip; cross-file effective endpoints | | G-18, G-19 |
| D-9 | **Root/empty path encoding** (`''` vs `/` vs prefix-only; Sinatra `get '/'` in `namespace '/v1'` → `/v1/`, Grape in `namespace :users` → `/users`) | one canonical rule across frameworks | routes |
| D-10 | **Route → handler relationship** (Rails `users#index`, Grape endpoint ↔ class) as a link/edge vs string | | G-18 |
| D-11 | **Meaning of `projectMetaData.totalClasses` / `totalLinesOfCode`** | count emitted records vs rule-based | G-27 |
| D-12 | **Fixture/test policy for Ruby** (`spec/`, `_spec.rb`, `test/`, `spec_helper` claim) | skip entirely / parse structure but no routes / current | G-01, G-23 |
| D-13 | **Claim policy**: priority Rails>Grape>Sinatra, `ApplicationController` claim with no detector, indirect bases needing a heritage index | exclusive claims vs explicit priority; build Ruby index | G-04, §6.6 |
| D-14 | **Grape positional semantics** of `prefix`/`version` and multi-version arrays | follow Grape docs vs conservative | G-19 |
| D-15 | **`Function.type` for file-level `def`** (`method` today) | `function` | cosmetic |
| D-16 | **Nested class id separator** for G-09 (`Outer.Inner` per doc vs Ruby `Outer::Inner`) | | G-09 |

---

## 10. Unverified areas and limitations

| Area | Why unverified |
|---|---|
| Rails on real code (controllers, engines, `config/routes.rb` with `resources`, concerns, devise) | no Rails fixture; 5 unit tests + synthetic probes only |
| Regenerated Rubocop capture | source repo absent; existing artifact stale |
| Rubocop skipped files | no skip report |
| Backend/Target-Spec acceptance (`framework` ∈ `rails`/`sinatra`/`grape`, `routeKind`, `version`, class `type=module`) | Confluence spec not accessible; the repo docs themselves state the enum additions are still pending |
| Server mode (`services/diff.py`, `/api/analyze-diff`) | `fastapi` not installed; only the class-count/LOC diff read statically |
| Pool execution (`--jobs>1`, spawn on Windows) | fresh captures ran with `jobs=1` |
| Parse timeouts / pathological files | no such file in the corpus; G-13 is code-read |
| `scripts/bench_parse_scaling.py` | not run; scaling measured with my own in-memory timings (G-17) |
| In-memory record vs serialized NDJSON byte comparison | not done; schema validity and id/parent integrity done on serialized output |
| CLI `breezeai-cog capabilities` | used the library `capabilities()` / registry instead |
| Grammar limits | 2 Grape files contain tree-sitter `ERROR` nodes at anonymous `&` forwarding (`routing.rb:78`, `params_scope.rb:416`); records were still produced and counts matched, effect on statements in those spans not checked |
| `.erb/.haml/.slim`, Rails views, `.jbuilder` | no parser; value for Ruby web frameworks unassessed |
| Language-neutral effects of `db_queries.py` (`sqlalchemy/django` hints restricted to Python) | other languages' output not diffed |
| Grape/Sinatra *real-world* frequency of G-04/G-18/G-21 forms | only two library repos available (they are frameworks, not applications) |
| CRLF `\r` in statement text | environment-dependent; other parsers not compared |

---

## 11. Prioritized remediation plan

### Priority 1 — confirmed correctness defects (wrong data in the graph)

| Task | Gaps | Files | Expected behaviour | Regression test |
|---|---|---|---|---|
| P1-1 Ruby fixture policy + drop spec-helper claim as route source | G-01, G-23 | `core/default_ignores.txt` or `ruby/ignore.txt`, `ruby/parser.py` (`fixture_markers`), `ruby_sinatra/parser.py`, `tests/unit/test_ruby_sinatra_parser.py` (flip test at L230) | no routes from `*_spec.rb`/`spec/`/`test/`; examples still emit | `spec/app_spec.rb` → 0 routes; `app.rb` → 1 |
| P1-2 Static-string helper (reject interpolation) shared by all route/URL extractors | G-02 | `ruby_sinatra/routes.py`, `ruby_grape/routes.py`, `ruby_rails/routes.py`, `ruby/statements.py` | dynamic string ⇒ no route / `endpoint=None` | table-driven test per framework |
| P1-3 Rails prefix composition or null | G-03 | `ruby_rails/routes.py` | `/admin/dashboard` or `None`, never `dashboard` | three snippets in G-03 |
| P1-4 Claims widening (leading `::`, `Sinatra.new`, `Class.new(base)`, `require 'sinatra/base'`) | G-04 | `ruby/claims.py`, `ruby_sinatra/parser.py`, `ruby_grape/parser.py`, `routes.py` | listed forms are claimed and routed | one test per form |
| P1-5 `private def` & friends, modifier/`begin`/`case` defs | G-05 | `ruby/classes.py` | functions captured with correct visibility/owner | five snippets |
| P1-6 `require` resolution (`lib/` paths, no sibling dir, no `index.rb`, no dynamic strings) | G-06 | `ruby/imports.py` | see G-06 | tmp-repo test |

### Priority 2 — other confirmed capture gaps

| Task | Gaps | Files | Expected / test |
|---|---|---|---|
| P2-1 Enrich route statements in Grape/Rails | G-07 | `ruby_grape/routes.py`, `ruby_rails/routes.py` | one statement per route position |
| P2-2 Consistent block-scoped-def policy; `class << obj` | G-08 | `ruby/classes.py` | per D-4 |
| P2-3 Qualified nested class ids | G-09 | `ruby/classes.py` | distinct stable ids |
| P2-4 Fix `enum_member` heuristic and duplicate | G-10 | `ruby/statements.py`, `ruby/classes.py` | single statement |
| P2-5 Missing statement kinds | G-11 | `ruby/mappings.py` | one test per node type |
| P2-6 Scanner reads only suffix-less files | G-12 | `core/scanner.py` | no `open()` for `.png` |
| P2-7 Index timeout + logging | G-13 | `ruby/models.py`, `core/pipeline.py` | time-bounded pre-pass |
| P2-8 Restore `window/this/self.fetch` for non-Ruby | G-14 | `parsers/detection/api_calls.py`, `tests/unit/test_detection.py` | TS `window.fetch` → `api_call` |
| P2-9 Require positive Redis evidence for `cache.get`-style hints | G-15 | `parsers/detection/db_queries.py`, `tests/unit/test_detection.py` | untagged without Redis evidence |
| P2-10 `.ru` support | G-16 | `ruby/parser.py` | `stream.ru` route captured |
| P2-11 Linear-time param/return/prefix/version lookups | G-17 | `ruby/models.py`, `ruby/functions.py`, `ruby_grape/routes.py`, `tests/unit/test_scaling.py` | ≤2.2× for 2× size |
| P2-12 Rails static values / engines | G-18 | `ruby_rails/routes.py`, `ruby_rails/parser.py` | forms in G-18 |
| P2-13 Grape multi-path, `route`, brace blocks, `Instance`, `Class.new(Grape::API)`, multi-version | G-19 | `ruby_grape/routes.py` | one test per form |
| P2-14 Calls: no same-file resolution for chained receivers | G-20 | `ruby/functions.py` | `path is None` |
| P2-15 Sinatra receiver scope | G-21 | `ruby_sinatra/routes.py` | see G-21 |
| P2-16 Class visibility (null or `private_constant`) | G-22 | `ruby/classes.py`, `docs/parser-reference.md` | no hard-coded `public` |
| P2-17 Type-check cleanup | G-24 | `parsers/base.py`, 25 non-Ruby parsers, `core/registry.py`, Ruby files | `mypy` ≤ HEAD baseline, `ruff` clean |

### Priority 3 — contract / schema decisions

D-1 … D-16 in §9. Recommended order: D-12 (fixtures) → D-13 (claims) → D-8 (mounts/resources) → D-4 (block owners)
→ D-1 (mixins) → D-2 (param kind) → D-11 (metrics) → the rest.

### Priority 4 — optional coverage improvements

G-25 (`public/protected :name`), G-26 (docs), G-27 (metadata totals — document or revert, D-11), G-28 (`::Const` receivers), G-29 (hygiene);
`.gemfile`/`Dangerfile` support; Rails/Sinatra view templates; regenerate `output/*` artifacts after fixes and add a
Rails fixture repo to `testing/`.

---

## 12. Final conclusion

**Confirmed gaps remain: 29 (Critical 3, High 3, Medium 18, Low 5), listed in §4.** The most damaging, in order:
spec-harness routes presented as the application's routes (G-01), interpolated paths emitted as truncated static
endpoints (G-02), unprefixed Rails endpoints (G-03), framework claims that drop whole route files (G-04), missing
`private def` methods (G-05) and incorrect `require` resolution (G-06).

The statement "No gaps found within the defined and verified scope." **does not apply** to this audit and is not
made. Passing tests are not evidence of completeness here: the working-tree tests encode G-01 as expected behaviour,
and the scaling test does not catch G-17.

What *is* verified (and stated with its scope): exact class/module discovery, exact source locations/ids/parents and
zero schema violations on Grape and Sinatra; correct visibility sections, `private :x`, `private_class_method`,
`class << self`, special method names and `*/**/&` params; correct Sinatra namespace composition and Grape
`prefix`+path-version composition for literal values; honest-null for dynamic Grape/Sinatra paths and Grape
prefix/version. Rails is unverified on real code.

---

## 13. Verification log

All commands ran from `C:\breezeai-cog` unless noted. Nothing below modifies the repo; Python used `.venv\Scripts\python.exe` (3.13.3).

| # | Command / action | Result |
|---|---|---|
| 1 | `git status --short` (before) | 34 modified/deleted + 4 untracked entries (§2.1) |
| 2 | `git diff --stat`, `git diff` on `docs/ core/ parsers/base.py services/ parsers/detection/` | 3,212 ins / 375 del; core changes read |
| 3 | Read `docs/architecture.md`, `parser-review-guide.md`, `parser-reference.md`, `skills/extend-capture/SKILL.md` completely; read `docs/architecture/statement-model.md` completely; read the Ruby rows / §7 / §10 of `docs/architecture/cross-file-resolution.md` and `graph-mapping.md` (the remainder of those two pages was not read) | done |
| 4 | Read all Ruby sources (`parsers/ruby*/**`) | done |
| 5 | `pytest -p no:cacheprovider -q` on `test_ruby_{parser,sinatra_parser,grape_parser,rails_parser}`, `test_registry`, `test_scanner`, `test_pipeline`, `test_scaling`, `test_detection` | **157 passed** |
| 6 | `pytest -p no:cacheprovider -q tests --continue-on-collection-errors` | **1511 passed, 2 skipped, 4 failed, 8 collection errors.** Failures: `test_cli.py::test_cog_pre_exists_as_file_gives_clean_error`, `::test_cog_read_only_gives_clean_error`, `test_webforms_parser.py::test_mappageroute_subdir_app_root`, `::test_page_route_subdir_app_end_to_end`. Errors: `fastapi` not installed (`test_server_*`, `test_analyzer_sql`, `integrations/test_scm_repository`) |
| 7 | `git archive HEAD` into scratchpad + `pytest tests/unit/test_cli.py tests/unit/test_webforms_parser.py` with `PYTHONPATH=<head>/src` | the same 4 fail at HEAD (81 passed) → pre-existing, unrelated |
| 8 | Fresh capture: `AnalysisService(Settings(repo, out=<scratchpad>, jobs=1)).analyze_repo(...)` for `testing/grape`, `testing/sinatra` | grape: 437/403/34, 0 failed; sinatra: 201/124/77, 0 failed; `.cog` mtimes verified unchanged |
| 9 | Integrity script on 5 artifacts (unique ids, parents, line ranges, `FileRecord.model_json_schema` validation, metadata vs sums) | §3.3 — 0 violations |
| 10 | Old-vs-fresh record diff (Grape, Sinatra) | Grape 7 files differ; Sinatra 27 files differ |
| 11 | Independent tree-sitter AST count of `class`/`module`/`method`/`singleton_method` vs capture | §3.2 |
| 12 | Independent route scan (block-bearing verb calls vs captured route lines) Sinatra/Grape | §3.2 |
| 13 | Exhaustive source-location check (functions, classes, statement ids) | 0 mismatches |
| 14 | ~15 synthetic probe scripts (visibility, names, params, odd positions, statements, semantic detection, Sinatra/Grape/Rails routes, claims, imports, calls, enums) | basis of every **[S]** tag |
| 15 | `open()` spy around `pipeline._scan_entries` | Grape 31, Sinatra 77 files opened during scan (G-12) |
| 16 | In-memory timings at 500/1000/2000/4000 items + `cProfile` | G-17 |
| 17 | `classify_call` on current tree vs `git archive HEAD` tree | G-14 |
| 18 | `ruff check --no-cache` on Ruby/core/detection dirs and Ruby tests | 1 error (`F841` `ruby/models.py:462`) |
| 19 | `mypy --cache-dir=nul` (current vs HEAD copy) | 224 vs 189 errors; 25 new `claims` override errors (G-24) |
| 20 | `git status --short` (after) and `ls -la output` | identical to before; no repo file changed except new `review.md` |

**Not run:** the CLI (`repo-to-json-tree`, would overwrite `.cog` reports), `scripts/bench_parse_scaling.py`, server
tests, `--jobs>1` captures, any Rails or Rubocop capture, backend/Target-Spec validation, byte-level comparison of
in-memory records with serialized lines.
