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
  graph. A missing edge is a gap the agent knows it lacks; a wrong edge is a fabricated path
  it will confidently follow.

(The design-time counterpart — deciding *how* to model something when it's ambiguous — is
§2. That decision is yours to make with a human before you code it; the runtime behavior
above is what you then bake into the parser.)

## 2. When how to model something is ambiguous, ask — do not assume

This is the design-time decision you make *while writing the parser*. Some constructs have
no single obviously-correct representation. When you hit one, **stop and get a human
decision** (present the options and your recommendation) before you code it — do not bake an
assumption into the parser. The modeling is then fixed once, applied uniformly, and the
parser behaves **deterministically** at capture time — never resolving the ambiguity with a
per-file heuristic that guesses differently across the codebase.

| Ambiguity | Why it needs a human decision |
|---|---|
| **Which shape to store it as** — a node type, a flag on a node, a statement kind, or nested detail | The choice determines how every query and the agent reach it, and it is expensive to change later |
| **Reuse an existing kind, or introduce a new one** | New node types / relationship types / statement kinds are a schema-wide commitment with real budgets (see §5) |
| **Which ontology it belongs to** | The same source can describe code, a database schema, or infrastructure — each is a different graph with a different owner |
| **An edge would need information from another file or repo** | Cross-file/repo resolution is heuristic; a wrong join creates false edges |
| **The detection would relabel or overwrite something already captured** | Silently changing another parser's output breaks its consumers |
| **The construct maps to no existing vocabulary** | Inventing meaning is where hallucination starts |

Most syntax has one right answer you discover empirically (see the reference guide). The
point is only that the moment *interpretation* is required, a human makes the call.

## 3. The three ways to extend capture

Decide the category **before** writing code — the mechanism differs, and the wrong choice
causes the failures in §1.

| You are adding… | Category | Mechanism | Owns the file? |
|---|---|---|---|
| A new programming language (its own grammar and extensions) | **Language parser** | The full `LanguageParser` for the language | Yes — the fallback owner for its extensions |
| A framework that defines the file's request/dispatch model — routes, endpoints, message handlers | **Server-framework parser** | Subclass the base language parser; add `claims` + `priority`; reuse the base extraction, then add detection | Yes — exactly one framework parser wins per file |
| A library used *across* files that already belong to other frameworks — ORM, validation, serialization, messaging SDK, HTTP client | **Internal-framework detector** | An *additive* detector — **not** a registered parser | **No** — it composes on top of whatever parser owns the file |

The distinction that trips people up: a **server framework owns the file** (a file "is a
NestJS controller"), so it is a one-per-file parser. An **internal framework does not own
the file** (a controller *that also publishes to a queue*), so it must be additive — a
one-per-file parser would displace the server framework and lose its routes.

### 3A. Language parser
Implement the full language parser. The step-by-step (package layout, the parse/extract
split, imports/functions/classes/statements, ids, registration, tests) is in
[`docs/parser-reference.md`](../../docs/parser-reference.md). The reliability rules above
apply throughout: resolve imports to real repo-relative paths (or leave them external), and
never invent a call target you cannot resolve.

### 3B. Server-framework parser (one per file)
Subclass the base language parser, sniff the framework cheaply in `claims`, set a
`priority` above the base, reuse `extract` (one parse), then add route/handler detection —
gated behind `--capture-statements`, and skipping fixture files for route emitters. See the
framework-parser section and detection idioms in the reference guide.

### 3C. Internal-framework detector (additive — composes on top)
An internal framework appears *inside* files owned by other frameworks, so it must never be
a one-per-file parser. There are **two hook points** — choose by the shape of the signal:

- **Call-shaped signal** (an ORM query, an HTTP request, a DB access) → extend the shared,
  cross-language classifier in `parsers/detection/`. It is already wired into every
  language's statement capture, so you add a mapping, not a parser. Supporting a new ORM,
  for example, is usually a single dictionary entry that tags the statement as a data-access
  call and records which ORM it is.
- **Structure-shaped signal** (a base class, a decorator, a type annotation, a
  command-object argument) that a flat classifier cannot express → add a `detect_*` pass and
  call it at the end of the base language parser's `extract`, gated by
  `--capture-statements` and preceded by a cheap byte guard so it only runs on relevant
  files. It **enriches statements in place or appends new ones**; it never replaces the
  file's parser. Because it lives in the base `extract`, it runs for every file of the
  language and layers underneath whichever framework won.

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

Two schema boundaries to know before adding a field: some node types are **open** (extra
plain attributes are kept), others are **restricted** (only documented fields survive
ingestion, so a brand-new field is silently dropped unless the backend is updated in step —
a cross-service change to raise, not assume). And reusing an existing statement *kind* (a new
value in an existing category) is cheap, whereas a new node type or relationship type is a
schema-wide commitment — ask.

## 6. Engineering conventions

- **Match the surrounding code** before introducing new patterns; consistency beats
  preference. Keep functions small and single-purpose; name things so intent is clear
  without a comment; comment the *why*, not the *what*.
- **Reuse before you build.** Especially the shared classifiers in `parsers/detection/`
  and the id helpers in `emit` — reuse them, don't fork. Never bake framework-specific
  logic into a base language parser.
- **Fail loudly at boundaries;** never silently swallow exceptions. Avoid magic
  numbers/hard-coded values — put limits in configuration (`config.py`).
- **Design for scale without gold-plating.** Capture streams NDJSON so a large repo never
  sits fully in memory; parsing is per-file and parallelized, so keep parsers free of
  cross-file mutable state (use the repo-wide index hook for shared data, returning a
  picklable value).
- **Test every behavioral change** — happy path, edges, failure modes. For parser work,
  also validate emitted records against the schema and dogfood on a real repository; a unit
  test passing is not enough.

## 7. Setup & workflow

```bash
uv sync --extra all           # install dev + optional dependencies
uv run pytest -q              # run the test suite (must stay green)
uv run ruff check . && uv run ruff format .
uv run mypy                   # type-check
# run on a repository:
uv run breezeai-cog repo-to-json-tree --repo <path> --out <dir> --capture-statements
```

Note: the parallel pool needs a real `__main__` — run via the CLI or a script file, not a
REPL/`python -c`/stdin (or pass `--jobs 1`).

## 8. Directory structure

```
src/breezeai_cog/
├─ cli.py                  # CLI: repo-to-json-tree, capabilities, schema, serve, version
├─ config.py, logging.py   # settings (BREEZEAI_COG_* env); structured logging
├─ schemas/                # Pydantic v2 capture contract (capture.py, enums.py) — source of truth
├─ core/                   # scanner, registry (claims/priority selection), pipeline, executor, default ignores
├─ parsers/                # base + framework parsers + shared detectors (below)
├─ emit/                   # deterministic ids/parentId, ndjson, gzip_stream, sinks, s3
├─ analyzers/              # sql (DDL), es (Elasticsearch) — server-only
├─ services/               # analysis, inprocess, diff, notify
└─ server/                 # FastAPI: app, routes, git

src/breezeai_cog/parsers/
├─ base.py                 # BaseParser, ParseContext
├─ treesitter.py           # grammar loading + bounded parse
├─ statements_common.py    # shared flat-statement emission + detection wiring
├─ callresolve.py          # calls[].path resolution
├─ detection/              # shared cross-language classifiers (api/db/query) — reuse, don't fork
├─ <lang>/                 # one base language parser package per language
└─ <lang>_<framework>/     # framework parsers (subclass the base; one wins per file)
```

## 9. Reliability checklist

- [ ] The grammar was discovered empirically — no node type or field guessed.
- [ ] Every ambiguity of *meaning* was raised for a human decision, not assumed (§2).
- [ ] No field holds a guess; unresolved values are left null (honest-null).
- [ ] No relationship is emitted unless both ends resolve to real graph identities.
- [ ] Semantic statements (routes, data access, events, queries) emit **only** when
      `--capture-statements` is set; route emitters also skip fixture/test files.
- [ ] Ids come only from the `emit` helpers, one shared `seen_ids` set per file, so a
      detector's statements attach to the parent the base parser assigned.
- [ ] Statements are flat and linked to their owner; nesting is expressed by line ranges.
- [ ] The right category was chosen (§3): a server framework is a one-per-file parser; an
      internal framework is an additive detector that does not displace the file's owner.
- [ ] Existing vocabulary reused where it fits; any new node/relationship/statement kind
      was raised for a decision (§5).
- [ ] Verified on unit tests **and** dogfooded on a real repository; the suite stays green
      and every emitted record validates against the schema.

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
