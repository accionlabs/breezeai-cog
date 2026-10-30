---
name: extend-capture
description: >
  Guide for developing parsers/detectors for breezeai-cog — adding support for a
  new language, a server framework, or an internal/cross-cutting framework (ORM,
  validation, serialization, messaging SDK). The parsers you write ship in the
  application and run capture later, autonomously, over repositories no one will
  review — so they must be reliable by construction: code them to never emit a
  guessed node or edge, and get a human decision before modeling an ambiguous
  construct. Read before adding or changing any parser or detector.
---

# Extend Capture — breezeai-cog

`breezeai-cog` scans a source repository and parses each file into a structured capture
(gzipped NDJSON — files, classes, functions, imports, and optionally in-body statements:
routes, API calls, DB queries, events). The Breeze backend ingests that capture into a
Neo4j **code graph** with embeddings. The tool only reads and parses source — it never
executes it.

This guide is for whoever **develops the parsers** — a human engineer or any coding agent.
Your job is to write parser/detector code that becomes part of `breezeai-cog`. You do **not**
run capture as the end goal: the shipped application runs capture later, on its own, over
many repositories you will never see. So your deliverable is a parser that is *correct by
construction* — there is no one watching to catch a bad result at capture time.

It is tool-neutral: where it says "ask" or "get a human decision," that means whatever review
channel your setup provides (a maintainer, a review thread, an issue, or an interactive
prompt), not any particular assistant's mechanism.

For the mechanical, step-by-step of building a parser, see
[`docs/parser-reference.md`](../../docs/parser-reference.md). For the current list of
supported languages and frameworks, see the README's support matrix — or run
`breezeai-cog capabilities`, the authoritative live source.

---

## 1. Reliability comes first

The capture your parser produces is not a throwaway artifact — the backend turns it into a
graph that **downstream services and agents traverse and cite as fact** (impact analysis, PR
validation, tracepath, search). Two facts about *when* and *where* your parser runs set the
bar:

- **It runs unattended.** The application invokes your parser at capture time — you are not
  in the loop. A bad result cannot be caught and corrected after the fact; it flows straight
  into the graph.
- **It runs at scale, on unseen code.** Your parser will meet syntax you never tested, in
  repositories you never opened. It must behave conservatively on inputs you did not
  anticipate.

So a wrong node or a false relationship is worse than a missing one — it is high-confidence
wrong, and it silently corrupts everything built on it:

| Outcome | Effect downstream |
|---|---|
| A construct is **not captured** | A known blind spot. Honest — someone can add it later. |
| A construct is captured **correctly** | The win — a verifiable fact the agent can walk and cite. |
| A construct is captured **incorrectly** | The dangerous case. The agent trusts it, PR validation routes on it, impact analysis traverses it — it corrupts them silently. |

Therefore **code the parser to be conservative by construction — it emits only what it can
verify from the syntax in front of it, and never a value it had to guess.** Two concrete
runtime behaviors to build in:

- **Honest-null.** When a field's real value is not present in the code (an address that is
  an injected variable, a type that resolves in another file, a target it cannot see), the
  parser leaves the field null — it never writes the symbol name or a heuristic into a field
  meant to hold a resolved value. A reader must be able to trust that a set field is real.
- **Absent beats wrong for relationships.** The parser does not create an edge (a call, an
  import, a producer→consumer link) unless *both* ends resolve to real identities in the
  graph — an unresolved edge is dropped entirely, never emitted with a null endpoint. A
  missing edge is a gap the agent knows it lacks; a wrong edge is a fabricated path it will
  confidently follow.

(The design-time counterpart — deciding *how* to model something when it's ambiguous — is
§2. That decision is yours to make with a human before you code it; the runtime behavior
above is what you then bake into the parser.)

## 2. When how to model something is ambiguous, ask — do not assume

This is the design-time decision you make *while writing the parser*. Some constructs have
no single obviously-correct representation. When you hit one, **stop and get a human
decision** (present the options and your recommendation) before you code it — do not bake an
assumption into the parser. The modeling is then fixed once, applied uniformly, and the
parser behaves **deterministically** at capture time — never resolving the ambiguity with a
per-file heuristic that guesses differently across the codebase. If no review channel is
reachable at author time, take the **conservative** path — capture nothing / honest-null —
rather than guess: a known gap is recoverable later, a wrong model silently is not.

| Ambiguity | Why it needs a human decision |
|---|---|
| **Which shape to store it as** — a node type, a flag on a node, a statement kind, or nested detail | The choice determines how every query and the agent reach it, and it is expensive to change later |
| **Reuse an existing kind, or introduce a new one** | New node types / relationship types / statement kinds are a schema-wide commitment with real budgets (see §5) |
| **Which ontology it belongs to** | The same source can describe code, a database schema, or infrastructure — each is a different graph with a different owner |
| **An edge would need information from another file or repo** | Cross-file/repo resolution is heuristic; a wrong join creates false edges |
| **The detection would relabel or overwrite something already captured** | Silently changing another parser's output breaks its consumers |
| **The construct maps to no existing vocabulary** | Inventing meaning is where hallucination starts |

Most syntax has one right answer you discover empirically (see the reference guide). The
point is only that the moment *interpretation* is required, a human makes the call. The
dividing line for storage shape (§5): **reusing** an existing node / edge / statement kind is
a call you make yourself; **introducing a new** one is a proposal you raise, and use only once it
is approved (§5).

## 3. The three ways to extend capture

Decide the category **before** writing code — the mechanism differs, and the wrong choice
causes the failures in §1.

| You are adding… | Category | Mechanism | Owns the file? |
|---|---|---|---|
| A new programming language (its own grammar and extensions) | **Language parser** | The full `LanguageParser` for the language | Yes — the fallback owner for its extensions |
| A framework that defines the file's request/dispatch model — routes, endpoints, message handlers — **and** has a file-level signature no other framework shares (NestJS, Spring, Vert.x) | **Server-framework parser** | Subclass the base language parser; add `claims` + `priority`; reuse the base extraction, then add detection | Yes — exactly one framework parser wins per file |
| A library used *across* files that already belong to other frameworks — ORM, validation, serialization, messaging SDK, HTTP client — **or** a route, job or consumer style that can appear in files other frameworks own (Express, Fastify, Celery, Quartz) | **Internal-framework detector** | An *additive* detector — **not** a registered parser | **No** — it composes on top of whatever parser owns the file |

The distinction that trips people up: a **server framework owns the file** (a file "is a
NestJS controller"), so it is a one-per-file parser. An **internal framework does not own
the file** (a controller *that also publishes to a queue*), so it must be additive — a
one-per-file parser would displace the server framework and lose its routes.
The test: **can this framework's entry points appear in a file another framework owns?** If
so, it is additive even though it defines routes — an Express `app.get(...)` inside a NestJS
`main.ts` is captured by the `express` detector while NestJS keeps the file.

### 3A. Language parser
Implement the full language parser. The step-by-step (package layout, the parse/extract
split, imports/functions/classes/statements, ids, registration, tests) is in
[`docs/parser-reference.md`](../../docs/parser-reference.md). The reliability rules above
apply throughout: resolve imports to real repo-relative paths (or leave them external), and
never invent a call target you cannot resolve.

A new language also needs, from its first commit:
- the `run_additive(<language>, …)` call at the end of its `extract`, and the language added to
  `HOOKED_LANGUAGES` in `parsers/additive.py` — so framework detectors can be added later
  without touching the language parser again;
- if it has a `build_index`, the `collect_additive` / `resolve_additive` calls and the language
  added to `INDEX_HOOKED_LANGUAGES`, so detectors can register facts from other files (§3C).

Keep the `schema_version` the parser inherits from `BaseParser`: `core/registry.py` refuses to
register a parser whose `schema_version` is not the current `SCHEMA_VERSION`.

If the file type you are adding is **markup/view** (a template: `.hbs`, `.jsp`, `.svelte`,
`.twig`, …), also declare `template_extensions` on the parser — the subset of `extensions`
that is markup rather than code. Those files are skipped at scan time unless the caller
passes `--capture-templates`, which is what keeps markup from flooding the graph. Declare
only the markup half: a parser owning both (WebForms: `.cs` + `.aspx`; Vue: `.ts` + `.vue`)
must keep capturing its code half. Skip this and your new template type silently
re-pollutes every graph. Add the new type to [`docs/template-capture.md`](../../docs/template-capture.md)
("What counts as a template", "Which stacks need the flag").

### 3B. Server-framework parser (one per file)
Subclass the base language parser, sniff the framework cheaply in `claims`, set a
`priority` above the base, reuse `extract` (one parse), then add route/handler detection —
gated on `ctx.capture_statements` (on by default, off with `--no-capture-statements`), and skipping fixture files for route emitters. See the
framework-parser section and detection idioms in the reference guide. Entry points (routes,
consumers, scheduled jobs, pages) follow the
[entry-point contract](../../docs/architecture/entry-points.md): give each framework construct
a role, record one entry point per binding, and keep wiring (mounts, prefixes, registrations)
as plain statements. Lead each entry point to its handler with `parsers/handler_sites.py`: a
detector that knows the class calls `locate_method` (or `locate_export` for a TS module) and
writes the result with `handler_location`; one that knows only a name written in the file names
the handler and lets the parser's `handler_locator` (TypeScript) find it — never guess a file
(see
[leading an entry point to its handler](../../docs/architecture/cross-file-resolution.md#leading-an-entry-point-to-its-handler)).
Write the role table (in
[`docs/entry-point-roles/`](../../docs/entry-point-roles/README.md)) and its
`tests/unit/test_roles_<framework>.py` before the detection code.

Guards need the same care as addresses. A middleware, filter or decorator that runs before a
handler goes into `guards` by name; `authRequired: true` is set **only** when it is the
framework's own check and it certainly rejects anonymous calls. An optional or skipped check, an
"allow anonymous" or open-access marker, the app's own `requireAuth` helper, or an option that
can't be read makes no claim — leave `authRequired` unset. (Examples: the Express and Ktor role
tables.)

### 3C. Internal-framework detector (additive — composes on top)
An internal framework appears *inside* files owned by other frameworks, so it must never be
a one-per-file parser. There are **two hook points** — choose by the shape of the signal:

- **Call-shaped signal** (an ORM query, an HTTP request, a DB access) → extend the shared,
  cross-language classifier in `parsers/detection/`. It is already wired into every
  language's statement capture, so you add a mapping, not a parser. Supporting a new ORM,
  for example, is usually a single dictionary entry that tags the statement as a data-access
  call and records which ORM it is.
- **Structure-shaped signal** (a base class, a decorator, a type annotation, a
  command-object argument) that a flat classifier cannot express → write a `detect_*` pass
  and **register it** with `register_detector(Detector(...))` at the bottom of its own module
  (e.g. `parsers/typescript_express/routes.py`; the registry is `parsers/additive.py`). Do **not** edit the language parser: its `extract` already ends
  with `run_additive(<language>, …)`, which runs every registered detector for that language,
  so it fires for every file of the language and layers underneath whichever framework won.
  Its `detect` **returns** what it found (`Detection` of `Emit`s: node + fields + whether it
  may enrich the statement already there); the registry writes it (`parsers/emits.py` `apply`:
  enrich in place or append, ids, parents). `detect(dc)` gets a `DetectContext`: `dc.root`,
  `dc.source`, `dc.path`, `dc.record` (the statements so far), `dc.is_fixture`, and per-language
  extras (`dc.bindings`: TypeScript import bindings; `dc.types`: Scala type map). It never
  replaces the file's parser. Its `Gate`
  decides when it runs: only with `ctx.capture_statements`, not on fixture files when
  `skip_fixtures=True` (set it for route / entry-point emitters), only when the cheap byte
  `guard` is present (otherwise guard inside the detector); a returned framework label fills
  `record.framework` only while it is unset. Pick a unique `order` — it fixes the run order, which decides statement-id disambiguation — declare
  the `frameworks` labels it can emit, and add it to the inventory in
  `tests/unit/test_additive.py`. A language without the hook (see `HOOKED_LANGUAGES`) needs a
  one-time `run_additive` call in its base `extract` first, and the language added to
  `HOOKED_LANGUAGES` (the hooked-language test checks the call) — that one is a
  language-parser change.
- **A fact from another file** (a class is an API only because the composition root registers
  it; a URL prefix set by a mount elsewhere) → register a **fact** beside the
  detector: `register_fact(FactSpec(name="framework.concern", language="csharp", gate=…, collect=…, resolve=…))`.
  The language's `build_index` builds it once before parsing, and the detector reads it with
  `index_fact`. Only languages whose `build_index` calls `collect_additive` /
  `resolve_additive` can hold facts (`INDEX_HOOKED_LANGUAGES`); registering one for another
  language raises. Do not add a framework field to the
  language index or a new selection hook — the file keeps its owner and the detector adds to it.
  Bind names with the language's rules and leave ambiguous ones unbound. Recipe and worked
  example: the parser reference ("Additive detectors", facts from other files) and
  [`docs/architecture/cross-file-resolution.md`](../../docs/architecture/cross-file-resolution.md#a-framework-fact-from-another-file-a-registered-fact).

Some "internal frameworks" only describe data models. Often the base parser already
captures the class, its base class, and its fields — so the only thing to add is a **role
marker** (a plain descriptive attribute on the class), not a new statement kind. When unsure
whether a construct is a statement, a role marker, or already covered — ask (§2).

## 4. Discover the grammar empirically — never guess it

Before writing any detector, dump a real AST and read the actual node types and field
names. This is separate from §2 (which is about *meaning*); this is about *syntax* — but the
principle is the same: no assumptions. The exact procedure is in the reference guide.

## 5. Choosing how to store what you capture

Every value lands in the graph as one of a few shapes. Pick the shape from how the value
will be *used*, and stay inside the working budgets — crossing one is a signal to reconsider
the shape, and (per §2) a new shape is a decision to raise, not to make silently.

| Store it as… | When the value is… |
|---|---|
| A plain attribute (scalar) | Filtered, sorted, or branched on; low-cardinality; verifiable |
| A relationship (edge) or a node type | Traversed, or expresses a connection between two things |
| Nested descriptive detail | Read together with its parent, never filtered on its own; useful to embed |

Working budgets (treat a breach as "reconsider the shape," not an error): roughly a dozen
node types; ~fifteen relationship types; ~a dozen plain attributes per node; node
connection-counts under ~a hundred; descriptive text as one readable block, not many
bookkeeping keys.

### What the backend keeps, and what agents can find

A value counts only if it survives ingest and agents can reach it. Three rules (full table:
[`docs/architecture/graph-mapping.md` §11](../../docs/architecture/graph-mapping.md#11-checklist-adding-a-field-or-value)):

| You are adding… | What happens |
|---|---|
| A new field on `File` or `Class` | Kept — these nodes are **open**. Prefer `metadata` for anything nested. |
| A new field on `Function` or `Statement` | **Silently dropped** — these nodes are **allow-listed**. Once approved, it needs a backend allow-list change in the same release; unit tests and schema validation pass either way, so they won't tell you. |
| A new `semanticType` value | Stored and filterable at once, with no backend change. Once approved, add it to the [Target Spec](https://accionlabs.atlassian.net/wiki/spaces/~5cfa0cffd898610dbf3bacf1/pages/2483453956/Code+Ontology+Parser+Target+Spec+Neo4j+Graph+Model). |
| A new node type or relationship type | A schema-wide change for every query and agent — propose it only when no field, value or statement kind can carry the meaning. |

Only `File`, `Class` and `Function` are embedded; **statements are never embedded**. So decide
by how agents will look for the value:

```
by meaning ("where is checkout handled?") → on File / Class / Function (a field or metadata)
by exact field (routes, endpoints)        → a Statement (filterable, returned as context)
both                                      → a summary on the parent's metadata + statements
```

For example, moving enum members from `Class.metadata` to `enum_member` statements kept them
filterable but made the enum class rank lower for "Status enum active inactive" — the member
names were no longer in its embedded text.

**Existing fields and values first; new ones by proposal.** Use the fields and values already
in the Target Spec wherever they fit. A value belongs to its context — a `method` belongs to a
`routeKind` (`rpc` → `RPC`) — so reusing one under a new framework, in the same context, is not
a new value.

The spec is not a wall, though: don't force a construct into a value that misdescribes it, or
drop information agents need, just to stay inside it. When no existing field or value fits in
practice, **propose** a new one, with:

- the construct, as a small source example, and the record you would emit;
- why the existing fields and values don't fit;
- where it lives (field, `metadata`, statement kind, node or edge) and what the backend needs —
  a new `Function` / `Statement` field needs an allow-list change (table above).

Emit it only **after it is approved**. Until then, capture what the existing values can express
(or leave it out) and keep the proposal open — never ship the new value ahead of the decision.

### Statement rules

The full list is in [`docs/architecture/statement-model.md` §9](../../docs/architecture/statement-model.md#9-rules-for-parser-authors).
The ones most often broken:

- **`nodeType` is the raw tree-sitter `node.type`** of the node the statement came from, in the
  file's own (host-language) tree. A detection with no node of its own there — a route built
  from an annotation, a config entry, a file name — uses `synthetic`. Never invent a label
  (`graphql_field`, `page_directive`). For an embedded DSL (SQL in a string, SDL in a
  `` gql`…` `` template) use the host node that wraps it, never the embedded grammar's type.
  The kind of thing is carried by `semanticType` / `routeKind`, not by `nodeType`.
- **No `semanticType` on a control-flow statement** (`if`, `for`, `try`, …).
- **DTOs as written.** `requestDTO` / `responseDTO` are the declared type as written in the
  source language — `List<OrderDto>`, not `OrderDto` — with only the wrappers that are not
  payload removed (async results such as `Promise<T>` / `Task<T>`, HTTP results such as
  `ResponseEntity<T>`). The exact list is in statement-model §1.
- **Don't truncate text in the parser;** splitting happens at output.

## 6. Engineering conventions

- **Match the surrounding code** before introducing new patterns; consistency beats
  preference. Keep functions small and single-purpose; name things so intent is clear
  without a comment; comment the *why*, not the *what*.
- **Reuse before you build.** Especially the shared classifiers in `parsers/detection/`
  and the id helpers in `emit` — reuse them, don't fork. Never bake framework-specific
  logic into a base language parser.
- **Fail loudly at boundaries;** never silently swallow exceptions. Avoid magic
  numbers/hard-coded values — put limits in configuration (`config.py`). A parser already runs
  under the existing ones: `parse_timeout` (per-file tree-sitter timeout; pass
  `ctx.parse_timeout_micros` when you re-parse an embedded fragment), `max_file_size` (larger
  files are never scanned) and `--max-concat-depth` (how deep a `+` chain is folded into an
  endpoint before it is left null). Reuse them before adding a new limit.
- **Design for scale without gold-plating.** Capture streams NDJSON so a large repo never
  sits fully in memory; parsing is per-file and parallelized, so keep parsers free of
  cross-file mutable state (use the repo-wide index hook for shared data, returning a
  picklable value). Only the **language** parser's `build_index` runs — a framework's facts
  reach it through a registered fact (`register_fact`), never through the framework parser's
  own `build_index` or instance state. When that hook (`build_index`) parses every file, structure it as a
  picklable per-file worker plus a deterministic (order-independent) reduce and run it
  through the shared `parallel_map(files, fn, jobs)` — so it honours `--jobs` (serial at
  `jobs<=1`) like the parse stage, instead of looping single-threaded.
- **Test every behavioral change** — happy path, edges, failure modes. For parser work,
  also validate emitted records against the schema and dogfood on a real repository; a unit
  test passing is not enough. Anything that crosses files is also tested end to end through
  `analyze_repo` with `jobs` 1 and 2 — a hand-built index cannot catch a fact that never
  reaches the workers.

## 7. Setup & workflow

```bash
uv sync --extra all           # install dev + optional dependencies
uv run pytest -q              # run the test suite (must stay green)
uv run ruff check . && uv run ruff format .
uv run mypy                   # type-check
# run on a repository:
uv run breezeai-cog repo-to-json-tree --repo <path> --out <dir>
```

Note: the parallel pool needs a real `__main__` — run via the CLI or a script file, not a
REPL/`python -c`/stdin (or pass `--jobs 1`).

### Before you open a PR

1. **Update the docs that list what cog supports:**

   | You added… | Update |
   |---|---|
   | A language, framework or detector | The README's "Supported languages & frameworks" table (it is a snapshot of `breezeai-cog capabilities` and is not generated) |
   | Entry points | The framework's role table in `docs/entry-point-roles/` and a link to it in that folder's `README.md` |
   | A template type | `docs/template-capture.md` (§3A) |
   | A field or value | The Target Spec, and the backend allow-list when §5 says so |

2. **Review your own change** with [`docs/parser-review-guide.md`](../../docs/parser-review-guide.md)
   Part A (the `gap-review` skill runs it), and fix what it finds before asking for review.
3. **Commit and PR conventions.** Commits use `type(scope): what is now true`, scoped by
   framework or area — e.g. `fix(express): .all() before other verbs in a route() chain is their
   middleware`, `docs(statement-model): …`, `feat(go): …`. CI rejects a PR whose title has no
   Jira id: include `BREEZEAI-<number>` in the title.

## 8. Directory structure

```
src/breezeai_cog/
├─ cli.py                  # CLI: repo-to-json-tree, capabilities, schema, serve, version
├─ config.py, logging.py   # settings (BREEZEAI_COG_* env); structured logging
├─ schemas/                # Pydantic v2 capture contract (capture.py, enums.py) — source of truth
├─ core/                   # scanner, registry (claims/priority selection), pipeline, executor, default ignores
├─ parsers/                # base + framework parsers + shared detectors (below)
├─ emit/                   # deterministic ids/parentId, ndjson, gzip_stream, sinks, split
├─ analyzers/              # sql (DDL), es (Elasticsearch), nosql — server-only
├─ services/               # analysis, inprocess, upload, batch_upload, diff, notify
├─ server/                 # FastAPI: app, routes, git
├─ infra/                  # object storage providers (infra/aws/s3.py)
├─ integrations/scm/       # git providers (GitHub, GitLab, Bitbucket, Azure DevOps)
├─ utils/                  # loc, paths, source cache, text helpers
└─ errors.py               # error types

src/breezeai_cog/parsers/
├─ base.py                 # BaseParser, ParseContext
├─ treesitter.py           # grammar loading + bounded parse
├─ statements_common.py    # shared flat-statement emission + detection wiring
├─ callresolve.py          # calls[].path resolution
├─ index_common.py         # shared build_index helpers: ambiguity collapse, heritage, parallel_map
├─ additive.py             # detector + fact registry (register_detector, register_fact, run_additive)
├─ emits.py                # Emit + apply: the one place detector output is written
├─ contract.py             # entry-point contract check (validate)
├─ detection/              # shared cross-language classifiers (api/db/query) — reuse, don't fork
├─ <lang>/                 # one base language parser package per language
└─ <lang>_<framework>/     # framework parsers (subclass the base; one wins per file) and
                           # detector-only packages (register detectors / facts)
```

## 9. Reliability checklist

- [ ] The grammar was discovered empirically — no node type or field guessed.
- [ ] Every ambiguity of *meaning* was raised for a human decision, not assumed (§2).
- [ ] No field holds a guess; unresolved values are left null (honest-null).
- [ ] No relationship is emitted unless both ends resolve to real graph identities.
- [ ] Semantic statements (routes, data access, events, queries) emit **only** when
      `ctx.capture_statements` is True (i.e. not `--no-capture-statements`); route emitters also skip fixture/test files.
- [ ] Ids come only from the `emit` helpers, one shared `seen_ids` set per file, so a
      detector's statements attach to the parent the base parser assigned.
- [ ] Statements are flat and linked to their owner; nesting is expressed by line ranges.
- [ ] Every `nodeType` is a real tree-sitter `node.type` from the host tree, or `synthetic`; no
      control-flow statement has a `semanticType`; DTOs are the declared type as written (§5).
- [ ] Existing fields and values used where they fit; a new field or value is emitted only
      after its proposal was approved, and a new `Function` / `Statement` field ships with the
      backend allow-list change; content agents must find by meaning is on File / Class /
      Function, not only on a Statement (§5).
- [ ] `authRequired` is set only by a framework check that certainly rejects anonymous calls
      (§3B).
- [ ] The right category was chosen (§3): a framework is a one-per-file parser only when no
      other framework's file can hold its entry points; otherwise it is an additive detector
      that does not displace the file's owner.
- [ ] Any new node, relationship or statement kind was proposed and approved before it was
      emitted (§2, §5).
- [ ] Entry points follow the entry-point contract: a role table with one test per row, one
      record per binding, `endpoint` resolved or left unset (never `""` or a code placeholder
      like `${…}`), wiring never recorded as a route.
- [ ] Verified on unit tests **and** dogfooded on a real repository; the suite stays green
      and every emitted record validates against the schema and the entry-point contract
      (`parsers/contract.py`; role tests through `analyze_files` fail on a contract error).
- [ ] Support docs updated, the change self-reviewed with the parser review guide, and the PR
      title carries a `BREEZEAI-<number>` id (§7).

## 10. Explaining a problem or fix, and writing docs

When you describe a problem or a fix — in a doc, a comment, a review, or a reply — keep it
**short and informative**, and show rather than tell:

- Lead with the point in a sentence or two: what's wrong (or what changed) and why.
- Include a concrete **example** — a minimal snippet or a before → after — not an abstract
  description.
- Use a **table** to compare options, and a small **diagram** when a flow reads more clearly
  drawn than written. Cut background the reader doesn't need.

Keep documentation **readable for a person with no internal context** — no tracking
identifiers (issue numbers, internal task codes) in prose. Name things by what they are, and
prefer examples and tables. Write for the next engineer, who reads this to decide how to add
support for something new — not for the person who already knows the history.
