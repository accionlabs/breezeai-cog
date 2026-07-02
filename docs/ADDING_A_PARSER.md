# Adding a Parser

How to add a parser to `breezeai-cog` — a new programming **language** (Java, Go, C#,
PHP, Ruby, …) or a **framework** on an existing language (Spring, Django, Express,
Angular, …). Covers the language-vs-framework decision, the package structure, the
reusable building blocks, the AST-discovery workflow, the parse/extract split,
registration, and testing.

A parser turns one source file into a `FileRecord` (the Part C contract, see
[`ARCHITECTURE.md`](../../../breeze-cog/docs/ARCHITECTURE.md)). Parsers self-register and
the pipeline dispatches each file to exactly one parser. This is the standard procedure.

---

## 1. First decision: language parser or framework parser?

| If the change is… | Then add a… | Located in | Mechanism |
|---|---|---|---|
| A whole new **language** (its own grammar/extensions) | **language parser** | `parsers/<lang>/` | implement the full `LanguageParser` (subclass `BaseParser`) |
| **Framework-specific** behavior on an existing language (routes/db/special decorators) | **framework parser** | `parsers/<lang>_<framework>/` | **subclass the base language parser**, add only the framework logic |

Rules of thumb:
- *"This is about how `.go`/`.rb`/`.java` files are structured"* → **language parser**.
- *"This is only about Spring / Django / Express / NestJS / FastAPI"* → **framework parser** that reuses the language parser. **Never bake framework logic into the base language parser.**
- *Cross-cutting call recognition* (is this an HTTP call? a DB query?) is **already shared** in `parsers/detection/` — don't reimplement it; feed it.

Worked examples already in the tree:
- Language: `parsers/python/`, `parsers/typescript/`, `parsers/java/`, `parsers/csharp/`, `parsers/vb/`
- Framework (AST-walk detection): `parsers/python_fastapi/` (FastAPI), `parsers/typescript_nestjs/` (NestJS), `parsers/typescript_angular/` (Angular), `parsers/typescript_express/` (Express), `parsers/typescript_loopback/` (Loopback), `parsers/typescript_react/` (React), `parsers/java_vertx/` (Vert.x)
- Framework (**off-the-record** detection — reads annotations the base parser already captured, no AST re-walk): `parsers/java_springboot/` (Spring Boot), `parsers/java_jaxrs/` (JAX-RS)
- Framework (**hybrid** — off-the-record controllers + an AST walk for call-based routes): `parsers/csharp_aspnet/` and `parsers/vb_aspnet/` (ASP.NET — `[ApiController]`/`[HttpGet]` off the record, minimal-API `app.MapGet(…)` via AST walk; the controller detector is language-agnostic and shared between the C# and VB parsers)
- Config: `parsers/config/` (a `priority`-0 parser with a custom `matches()` for glob-ish names like `Dockerfile.*` / `.env.*`)

---

## 2. Building blocks to reuse (do not reinvent)

| Need | Use | From |
|---|---|---|
| Parse source → AST (with timeout) | `parse_source(language, source, timeout_micros)` | `parsers/treesitter.py` |
| Node text / line span / first line | `node_text(node, src)`, `line_span(node)`, `first_line(text)` | `parsers/treesitter.py` |
| `id`/`parentId` convention | `file_id`, `class_id`, `function_id`, `statement_id`, `disambiguate` | `emit` |
| Record models | `FileRecord, Class, Function, Statement, Parameter, ConstructorParam, Decorator, Call` | `schemas` |
| API/DB/query call classification | `classify_call(callee, method, arg=None)` → `(semanticType, method, hint)` (api_call / query_statement / db_method_call); `text_has_query(text)` for embedded raw SQL | `parsers.detection` |
| LOC / truncation / snippet / repo-relative path | `count_loc`, `truncate`, `snippet`, `repo_relative` | `utils` |
| Capability-metadata defaults, optional `build_index`, ignore/include loading | subclass `BaseParser` | `parsers/base.py` |
| Registration / single-parser selection | `register`, `PARSERS` list, `select` / `claims` / `priority` | `core/registry.py` |

tree-sitter note: the installed binding (0.25) requires `Parser(get_language(name))`
(handled inside `parse_source` already) and bounds runtime via `timeout_micros`
(cross-platform — no OS signals). Just call `parse_source`.

---

## 3. The workflow

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
Grammar names come from `tree_sitter_language_pack`. Use a JSX-aware grammar
(`tsx`) for files with JSX. Note the exact node types and `child_by_field_name`
fields you'll rely on (`name`, `parameters`, `body`, `return_type`, etc.).

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
  ignore.txt      # per-language ignore defaults (layer 2)
  include.txt     # per-language force-include overrides
```

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
The split (`parse_file` parses, `extract` consumes a tree) is what lets a framework
parser parse once and reuse the base extraction. Always provide it.

### Step 3 — Extractors (mirror `parsers/python` / `parsers/typescript`)
- **imports.py**: resolve relative/internal imports to repo-relative paths (drives
  `IMPORTS`); external/unresolved → `externalImports`. Set `exports`.
- **functions.py**: `Function` per function/method (params with types, `returnType`,
  decorators, `isStatic`, visibility, `calls`). Return `(Function, list[Statement])`.
- **classes.py**: `Class` per class/interface/enum/struct (`extends`/`implements`,
  decorators, `constructorParams`, methods as flat `Function`s). Return
  `(Class, list[Function], list[Statement])`.

### Step 4 — ids (deterministic) + FLAT statements
- Assign ids **only** via `emit` helpers (`function_id(path, name, line, class_name=…)`,
  etc.) + `disambiguate(candidate, seen_ids)`. Determinism is what lets a framework
  parser compute the **same** `parentId` the base parser assigned.
- Statements are **flat on `FileRecord.statements`** (never nested on Function/Class);
  each links to its owner via `parentId`. Use one shared `seen_ids` set per file.

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
(See `parsers/python/statements.py` / `parsers/typescript/statements.py`.) Statement
capture is gated by `--capture-statements` (spec A4) — see Step 5b.

### Step 5b — Gate route/db/event/query statements behind `--capture-statements`
Structural statements (control flow, declarations) always emit, but **semantic
statements — routes, api_call/db_method_call/query_statement, events — must only be
emitted when `ctx.capture_statements` is True** (spec A4). The base extractors already
thread `capture = ctx.capture_statements` into `extract_statements`; **framework parsers
must gate their own detection the same way** (see the framework example in §4).

### Step 6 — `build_index` (only if cross-file resolution is needed)
If imports/symbols need repo-wide info (tsconfig aliases, Java FQCN, route mounts):
```python
def build_index(self, repo_root, files):
    return <picklable index>   # threaded into ctx.resolution_index
```
The pipeline runs it once in the main process and passes the (picklable!) result to
every worker. Use it in `extract`/`imports` via `ctx.resolution_index`. Example:
`parsers/typescript/parser.py` + `imports.py` (`TsAliasIndex`), or `parsers/java/`
(`FqcnIndex`).

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
`FileRecord.language` is the **base language string** (e.g. `"go"`), set in `extract`.
The pipeline reports `analyzedLanguages` from `record.language`, *not* the parser
name — so framework parsers still report the base language.

### Step 9 — Test + dogfood + validate
- Unit tests: instantiate the parser directly (`GoParser().parse_file(ctx)`); assert
  functions/classes/imports/decorators/returnTypes; assert **flat statements** with
  correct `parentId`; assert the record validates against the schema:
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

> Spawn note: `analyze_repo` with the parallel pool can't run from a REPL/`python -c`/
> stdin (Python spawn needs a real `__main__`). Dogfood via the **CLI**, a script
> **file**, or `iter_file_records` / `jobs=1`.

---

## 4. Framework parser structure

Mirror `parsers/python_fastapi/` or `parsers/typescript_nestjs/`:
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
        if ctx.capture_statements:                       # routes are statements — gated (spec A4)
            routes = detect_nest_routes(root, ctx.source, ctx.path,
                                        seen_ids={s.id for s in record.statements})
            if routes:
                record.statements.extend(routes)
                record.framework = "nestjs"
        return record
```
Framework detection emits route/db/event `Statement`s whose `parentId` is computed
with the **same** `emit.function_id(...)` the base parser used (so they attach to the
right handler) — this is why deterministic ids matter. **Route/event/query detection
MUST be gated behind `ctx.capture_statements`** (spec A4) — never emit them
unconditionally.

### Two detection idioms

- **AST-walk** (FastAPI, NestJS, Express, Angular, React, Vert.x): the detector walks
  `root` to find call/decorator patterns. Use when the signal isn't already on the
  extracted record (call-based routing, event bus, JSX) — pass `root`, `source`, `path`,
  and the current `seen_ids`.
- **Off-the-record** (Spring Boot, JAX-RS): the base Java parser already captured
  annotations onto `Class.decorators` / `Function.decorators` / `Parameter.decorators`,
  so the detector reads the **`FileRecord`** directly — **no second AST walk**. Prefer
  this for annotation-driven frameworks; it's simpler and can't drift from the base
  extraction. Example:
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
  Route attributes (`guards`/`requestDTO`/`responseDTO`/`isRegex`/`authRequired`, spec C5)
  are populated here from the captured decorators/params/`returnType` — see
  `java_springboot/routes.py`, `java_jaxrs/routes.py`, `typescript_nestjs/routes.py`.

### Selection: one parser per file
A file is parsed by **exactly one** parser. `registry.select(path, source)` picks the
highest-`priority` parser whose `claims(path, source)` is True; the base language parser
(`priority = 0`, `claims` → True) is the fallback. So:
- A framework parser **subclasses the base, sets `priority` (> 0) and `claims`**, and does
  full extraction + its detection (single parse, no duplicated code).
- Multiple frameworks for one language **coexist by content** — `@nestjs/` → NestJS,
  `@angular/` → Angular, plain → base TS. No composition, no collisions, single parse each.
- Make `claims` a cheap substring check on `source` (a distinctive import/dependency string).

---

## 5. Non-negotiable conventions (checklist)

- [ ] AST discovered empirically (Step 0), not guessed.
- [ ] `parse_file` / `extract` split provided.
- [ ] Capability metadata set: `name`, `extensions`, `schema_version = SCHEMA_VERSION`,
      `statement_types` (from `mappings.py`), `frameworks`.
- [ ] All ids via `emit.*` + `disambiguate`; one `seen_ids` per file.
- [ ] Statements **flat** on `FileRecord.statements`, linked by `parentId`.
- [ ] Route/event/query/api/db (semantic) statements gated behind `ctx.capture_statements` (spec A4).
- [ ] Imports resolved to repo-relative paths where possible (drives `IMPORTS`).
- [ ] Shared `classify_call(callee, method, arg)` wired for api/db/query (don't reimplement); pass the string arg.
- [ ] `FileRecord.language` = base language string.
- [ ] `PARSERS` exported from `__init__.py`.
- [ ] `ignore.txt` / `include.txt` for a language parser.
- [ ] `build_index` only if cross-file resolution is needed (return a **picklable** index).
- [ ] Framework parser subclasses the base, sets `priority` + `claims` (cheap content
      sniff), does full extraction + detection in a single parse — no duplicated code.
      Annotation-driven frameworks detect **off the record** (read `record.classes` /
      `record.functions`), not by re-walking the AST.
- [ ] Unit tests + schema validation + real-repo dogfood; `uv run pytest` green.
