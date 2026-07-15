# Parser Reference

The mechanical step-by-step for building a parser in `breezeai-cog` — the package
structure, reusable building blocks, the AST-discovery workflow, the parse/extract split,
the detection idioms, registration, and testing.

Read [`skills/extend-capture/SKILL.md`](../skills/extend-capture/SKILL.md) **first** — it
covers *which* kind of thing to build (language / server framework / internal framework) and
the reliability discipline that governs all of them. This document is the *how*. For the
current list of supported languages and frameworks, run `breezeai-cog capabilities` (the
live source) or see the README support matrix — this guide stays deliberately
inventory-free so it never goes stale when a parser is added.

A parser turns one source file into a `FileRecord` — the capture contract defined in
`src/breezeai_cog/schemas/capture.py` (the source of truth). Parsers self-register and the
pipeline dispatches each file to exactly one parser.

---

## 1. Building blocks to reuse (do not reinvent)

| Need | Use | From |
|---|---|---|
| Parse source → AST (with timeout) | `parse_source(language, source, timeout_micros)` | `parsers/treesitter.py` |
| Node text / line span / first line | `node_text(node, src)`, `line_span(node)`, `first_line(text)` | `parsers/treesitter.py` |
| `id`/`parentId` convention | `file_id`, `class_id`, `function_id`, `statement_id`, `disambiguate` | `emit` |
| Record models | `FileRecord, Class, Function, Statement, Parameter, ConstructorParam, Decorator, Call` | `schemas` |
| API/DB/query call classification | `classify_call(callee, method, arg=None)` → `(semanticType, method, hint)`; `text_has_query(text)` for embedded raw SQL | `parsers.detection` |
| LOC / truncation / snippet / repo-relative path | `count_loc`, `truncate`, `snippet`, `repo_relative` | `utils` |
| Capability-metadata defaults, optional `build_index`, ignore/include loading | subclass `BaseParser` | `parsers/base.py` |
| Registration / single-parser selection | `register`, `PARSERS` list, `select` / `claims` / `priority` | `core/registry.py` |

tree-sitter note: the installed binding requires `Parser(get_language(name))` (handled
inside `parse_source` already) and bounds runtime via `timeout_micros` (cross-platform — no
OS signals). Just call `parse_source`.

---

## 2. The workflow

### Step 0 — Discover the grammar empirically (ALWAYS do this first)
Never guess node types/fields. Dump a representative AST:

```bash
uv run python - <<'PY'
from tree_sitter import Parser
from tree_sitter_language_pack import get_language
src = b"<a representative snippet for the language>"
tree = Parser(get_language("<grammar>")).parse(src)
def t(n): return src[n.start_byte:n.end_byte].decode()
def walk(n, d=0):
    print("  "*d + n.type + (f"  {t(n)[:30]!r}" if not n.named_children else ""))
    for c in n.named_children: walk(c, d+1)
walk(tree.root_node)
# inspect fields of a specific node type:
def fields(n): return [(n.field_name_for_child(i), n.child(i).type) for i in range(n.child_count) if n.child(i).is_named]
PY
```
Grammar names come from `tree_sitter_language_pack`. Use a JSX-aware grammar (`tsx`) for
files with JSX. Note the exact node types and `child_by_field_name` fields you'll rely on
(`name`, `parameters`, `body`, `return_type`, etc.).

**Embedded DSLs (two grammars).** If routes/queries live inside a string of another language
(GraphQL SDL in a template literal, SQL in a string, …), run Step 0 **twice** — once for the
host grammar, once for the embedded one — and confirm the embedded grammar exists in
`tree_sitter_language_pack`. The detector then locates the host node (e.g. `template_string`)
in the primary tree, re-parses its bytes with the embedded grammar, and maps line numbers
back via the host node's `start_point` (see the embedded-DSL idiom below). **Any secondary
`parse_source` must thread `ctx.parse_timeout_micros`** — the same as the primary parse.

### Step 1 — Scaffold the package (language parser)
```
parsers/<lang>/
  __init__.py     # PARSERS = [<Lang>Parser()]   (NOT register() at import)
  parser.py       # <Lang>Parser(BaseParser): parse_file + extract (see Step 2)
  mappings.py     # EMIT_TYPES / CONTROL_FLOW / NESTED_SCOPES sets; STATEMENT_TYPES; FRAMEWORKS
  imports.py      # import extraction + in-repo resolution
  functions.py    # build_function(...) -> (Function, list[Statement])
  classes.py      # build_class(...) -> (Class, list[Function], list[Statement])
  statements.py   # extract_statements(...) (flat, gated, + detection wiring)
  ignore.txt      # per-language ignore defaults (layer 2) — scoped, post-scan (see note)
  include.txt     # per-language force-include overrides — same scoping
```

> **ignore.txt / include.txt are language-scoped and applied post-scan (not during the
> walk).** They are compiled per language (keyed by parser name) and matched against a file
> only when that file's *own* classified language owns the rule — so one language's pattern
> can't prune another's tree (e.g. a NuGet `packages/` must not prune a pnpm `packages/`
> workspace). Consequence: a **directory-name** pattern here does **not** prune the walk — it
> filters that language's files after classification. Truly universal directory prunes
> (`node_modules/`, `dist/`, `build/`, `bin/`, `obj/`, `target/`, …) belong in
> `core/default_ignores.txt`, the only set matched at walk time.

### Step 2 — Capability metadata + the parse/extract split (mandatory)
```python
class GoParser(BaseParser):
    name = "go"
    extensions = (".go",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES   # from mappings.py — capability discovery
    frameworks = []                     # frameworks supported by THIS parser

    def parse_file(self, ctx):
        root = parse_source("go", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root, ctx):        # <-- split so framework parsers reuse the tree
        ...build FileRecord from `root`...
```
The split (`parse_file` parses, `extract` consumes a tree) is what lets a framework parser
parse once and reuse the base extraction. Always provide it.

### Step 3 — Extractors (mirror an existing language parser under `parsers/`)
- **imports.py**: resolve relative/internal imports to repo-relative paths (drives
  `IMPORTS`); external/unresolved → `externalImports`. Set `exports`.
- **functions.py**: `Function` per function/method (params with types, `returnType`,
  decorators, `isStatic`, visibility, `calls`). Return `(Function, list[Statement])`.
- **classes.py**: `Class` per class/interface/enum/struct (`extends`/`implements`,
  decorators, `constructorParams`, methods as flat `Function`s). Return
  `(Class, list[Function], list[Statement])`.

### Step 4 — ids (deterministic) + FLAT statements
- Assign ids **only** via `emit` helpers (`function_id(path, name, line, class_name=…)`,
  etc.) + `disambiguate(candidate, seen_ids)`. Determinism is what lets a framework parser
  compute the **same** `parentId` the base parser assigned.
- Statements are **flat on `FileRecord.statements`** (never nested on Function/Class); each
  links to its owner via `parentId`. Use one shared `seen_ids` set per file.

### Step 5 — Wire shared detection into statements
In `statements.py`, find a call inside the statement, extract `(callee, method,
first_string_arg)`, then pass the **string arg** too so raw-SQL builders are caught:
```python
from ..detection import classify_call, text_has_query
classified = classify_call(callee, method, first_string_arg)   # arg enables query_statement
if classified:
    semantic, method_value, hint = classified   # api_call | query_statement | db_method_call
    endpoint = first_string_arg if semantic == "api_call" else None
    Statement(..., semanticType=semantic, method=method_value, endpoint=endpoint, dataAccessHint=hint, ...)
elif text_has_query(statement_text):             # fallback: a raw SQL string literal in the stmt
    Statement(..., semanticType="query_statement", ...)
```
Statement capture is gated by `--capture-statements` — see Step 5b.

### Step 5b — Gate route/db/event/query statements behind `--capture-statements`
Structural statements (control flow, declarations) always emit, but **semantic statements —
routes, api_call/db_method_call/query_statement, events — must only be emitted when
`ctx.capture_statements` is True**. The base extractors already thread
`capture = ctx.capture_statements` into `extract_statements`; **framework parsers must gate
their own detection the same way** (see the framework example below).

### Step 5c — Skip route-only fixture files (story/test harnesses)
Story/test files render components in throwaway routers, so their "routes" are fixtures, not
application routes. **Anything that emits routes** — a framework parser or an additive
`detect_*` pass — must gate detection on `self.is_fixture_file(ctx.path)` **as well as**
`capture_statements`:
```python
if ctx.capture_statements and not self.is_fixture_file(ctx.path):
    routes = detect_xxx_routes(...)
```
This is **route-only** — the file is still parsed for structure (functions/imports); only
route emission is suppressed. Markers are layered via `BaseParser.fixture_markers()`: a
global constant (`.test.`, `.spec.`) extended per language/framework through `super()` (e.g.
TypeScript adds `.stories.`, `.cy.`, `.e2e.`). Add your language's fixture infixes there, not
to a global list. (Full `*.test.*` / `*.spec.*` *exclusion* already lives in
`default_ignores.txt`; this guard is the narrower route-only case for files still captured.)

### Step 6 — `build_index` (only if cross-file resolution is needed)
If imports/symbols need repo-wide info (path aliases, fully-qualified class names, route
mounts):
```python
def build_index(self, repo_root, files, jobs=1):
    return <picklable index>   # threaded into ctx.resolution_index
```
The pipeline runs it once in the main process (before the parse pool), passing the **same
`jobs`** as the parse stage, and hands the (picklable!) result to every worker. Use it in
`extract`/`imports` via `ctx.resolution_index`.

**Parallelize a full-parse index.** If `build_index` parses every file, don't loop serially —
split it map/reduce so it scales with `jobs`:
```python
def _index_one(args):              # module-level + picklable (runs in a worker process)
    path, rel = args
    root = parse_source(LANG, Path(path).read_bytes(), 0).root_node
    return <partial fragment>      # pure — no shared state

def build_index(self, repo_root, files, jobs=1):
    index = <empty>
    for frag in parallel_map([(str(f), rel(f)) for f in files], _index_one, jobs):
        <merge frag into index>    # deterministic, order-independent
    return index
```
`parallel_map(items, fn, jobs=1)` (import from `..index_common`) owns the worker pool: it runs
`fn` over `items` across `jobs` processes and **falls back to a plain serial map at `jobs<=1`**
— so calling it with the default is safe (no pool, no `__main__`/spawn constraint; unit tests
build indexes at `jobs=1`). `fn` and every item must be picklable. The merge must be
**order-independent** so the built index is identical regardless of scheduling.

**Reuse `index_common` — don't re-implement these:**
- `record_distinct(map, key, value)` — the honest-null "conflict ⇒ `None`" collapse (mutates
  `map` in place, returns `None`). A key seen again with the **same** value stays resolved;
  only a **differing** value collapses it to `None` — resolving to nothing rather than a guess.
  Use it for any `name → file` index and as the order-independent fragment-merge step.
- `ClassHeritage` + `record_heritage` / `walk_heritage` — cross-file base-class heritage and
  chain walking (base-controller route inheritance, inherited-method call resolution).
- `merge_heritage` / `project_heritage` — partial-class-aware heritage merge (a declaration
  that omits the base clause yields to a concrete base) plus fully-qualified → simple-name
  projection (distinct types sharing a short name collapse to `None`).

### Step 7 — Register
```python
# parsers/<lang>/__init__.py
from .parser import GoParser
PARSERS = [GoParser()]
```
`discover_builtin` imports each `parsers/*` subpackage and registers its `PARSERS`
(idempotent, repopulates after `clear()`). Do **not** rely on `register()` import
side-effects.

### Step 8 — Language label
`FileRecord.language` is the **base language string** (e.g. `"go"`), set in `extract`. The
pipeline reports `analyzedLanguages` from `record.language`, *not* the parser name — so
framework parsers still report the base language.

### Step 9 — Test + dogfood + validate
- Unit tests: instantiate the parser directly (`GoParser().parse_file(ctx)`); assert
  functions/classes/imports/decorators/returnTypes; assert **flat statements** with correct
  `parentId`; assert the record validates against the schema:
  ```python
  from jsonschema import Draft202012Validator
  from breezeai_cog.emit import to_line
  errs = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
              .iter_errors(json.loads(to_line(rec))))
  assert not errs
  ```
- Dogfood on a REAL repo via the CLI:
  `uv run breezeai-cog repo-to-json-tree --repo <real-repo> --out ./out --jobs 4`
  then validate every line against the schema and eyeball counts.
- `uv run pytest -q` (must stay green; the schema-drift gate runs here too).

> Spawn note: the parallel pool can't run from a REPL/`python -c`/stdin (Python spawn needs a
> real `__main__`). Dogfood via the **CLI**, a script **file**, or `jobs=1`.

---

## 3. Framework parser structure

A framework parser subclasses the base language parser, reuses its `extract` on the shared
tree, then adds detection:
```
parsers/<lang>_<framework>/
  __init__.py     # PARSERS = [<Framework>Parser()]
  parser.py       # <Framework>Parser(<Lang>Parser): claims + priority + reuse extract
  routes.py       # framework detection (routes/db/events) -> list[Statement]
```
```python
class NestJSParser(TypeScriptParser):
    name = "typescript-nestjs"
    priority = 10                                        # selected over the base when it claims
    frameworks = ["nestjs"]

    def claims(self, path, source):                      # cheap content sniff of the framework
        return b"@nestjs/" in source

    def parse_file(self, ctx):
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)                 # inherited base extraction, ONE parse
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):  # gated + skip fixtures
            routes = detect_nest_routes(root, ctx.source, ctx.path,
                                        seen_ids={s.id for s in record.statements})
            if routes:
                record.statements.extend(routes)
                record.framework = "nestjs"
        return record
```
Framework detection emits route/db/event `Statement`s whose `parentId` is computed with the
**same** `emit.function_id(...)` the base parser used (so they attach to the right handler) —
this is why deterministic ids matter. **Route/event/query detection MUST be gated behind
`ctx.capture_statements`** — never emit them unconditionally.

### Detection idioms
Live examples of each idiom are in the framework parser packages under `parsers/` — browse
there rather than relying on a list here.

- **AST-walk**: the detector walks `root` to find call/decorator patterns. Use when the
  signal isn't already on the extracted record (call-based routing, event bus, JSX) — pass
  `root`, `source`, `path`, and the current `seen_ids`.
- **Off-the-record**: the base parser already captured annotations onto
  `Class.decorators` / `Function.decorators` / `Parameter.decorators`, so the detector reads
  the **`FileRecord`** directly — **no second AST walk**. Prefer this for annotation-driven
  frameworks; it's simpler and can't drift from the base extraction. Route attributes
  (`guards`/`requestDTO`/`responseDTO`/`isRegex`/`authRequired`) are populated here from the
  captured decorators/params/`returnType`.
  ```python
  def parse_file(self, ctx):
      root = parse_source("java", ctx.source, ctx.parse_timeout_micros).root_node
      record = self.extract(root, ctx)
      if ctx.capture_statements:
          routes = detect_spring_routes(record)   # reads record.classes / record.functions
          if routes:
              record.statements.extend(routes)
              record.framework = "spring"
      return record
  ```
- **Hybrid**: off-the-record controllers **plus** an AST walk for call-based routes
  (e.g. minimal-API `app.MapGet(…)`), in one parser.
- **Embedded-DSL secondary parse**: the routes/queries live in a string of a *different*
  language, opaque to the host grammar. AST-walk the host tree to find the string node, slice
  its bytes, re-parse with the embedded grammar (`parse_source("<embedded>", text,
  ctx.parse_timeout_micros)`), walk the sub-tree, and map line numbers back with
  `row_base + sub_node.start_point[0] + 1`, where `row_base` is the host fragment's row. Route
  fields need not be HTTP (e.g. GraphQL emits `framework="graphql"`, `routeKind ∈
  {query,mutation,subscription}`, `method` = the operation kind).

### Additive detectors (internal / cross-cutting frameworks)
Not every concern is a one-per-file parser. A library used *across* files owned by other
frameworks (ORM, validation, messaging SDK) is detected **additively** — see
[`skills/extend-capture/SKILL.md`](../skills/extend-capture/SKILL.md) §3C. Either extend the
shared `parsers/detection/` classifiers (call-shaped signals) or add a `detect_*` pass
invoked from the base parser's `extract` (structure-shaped signals) that enriches/appends
without displacing the file's parser.

### Selection: one parser per file
A file is parsed by **exactly one** parser. `registry.select(path, source)` picks the
highest-`priority` parser whose `claims(path, source)` is True; the base language parser
(`priority = 0`, `claims` → True) is the fallback. So:
- A framework parser **subclasses the base, sets `priority` (> 0) and `claims`**, and does
  full extraction + its detection (single parse, no duplicated code).
- Multiple frameworks for one language **coexist by content** — each `claims` a distinctive
  import/dependency string; plain files fall through to the base. No composition, no
  collisions, single parse each.
- Make `claims` a cheap substring check on `source`.

---

## 4. Non-negotiable conventions (checklist)

- [ ] AST discovered empirically (Step 0), not guessed.
- [ ] `parse_file` / `extract` split provided.
- [ ] Capability metadata set: `name`, `extensions`, `schema_version = SCHEMA_VERSION`,
      `statement_types` (from `mappings.py`), `frameworks`.
- [ ] All ids via `emit.*` + `disambiguate`; one `seen_ids` per file.
- [ ] Statements **flat** on `FileRecord.statements`, linked by `parentId`.
- [ ] Route/event/query/api/db (semantic) statements gated behind `ctx.capture_statements`.
- [ ] Route-emitting parsers also skip fixtures: `and not self.is_fixture_file(ctx.path)`;
      add language fixture infixes to `fixture_markers()`, not a global list.
- [ ] Any **secondary** `parse_source` (embedded-DSL idiom) threads `ctx.parse_timeout_micros`.
- [ ] Imports resolved to repo-relative paths where possible (drives `IMPORTS`).
- [ ] Shared `classify_call(callee, method, arg)` wired for api/db/query (don't reimplement);
      pass the string arg.
- [ ] `FileRecord.language` = base language string.
- [ ] `PARSERS` exported from `__init__.py`.
- [ ] `ignore.txt` / `include.txt` — **language-scoped, post-scan**; universal directory
      prunes go in `core/default_ignores.txt`.
- [ ] `build_index` only if cross-file resolution is needed (return a **picklable** index); if
      it full-parses, structure it as a `parallel_map` worker + deterministic reduce honouring `jobs`.
- [ ] Framework parser subclasses the base, sets `priority` + `claims`, does full extraction
      + detection in a single parse. Annotation-driven frameworks detect **off the record**.
- [ ] Unit tests + schema validation + real-repo dogfood; `uv run pytest` green.
