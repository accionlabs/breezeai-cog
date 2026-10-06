# Cross-file framework facts

Most framework code can be recognised from the file itself: an import, an annotation, a base
class. Some cannot, because what a file *means* is decided in **another file**:

| What decides it | Where | Example |
|---|---|---|
| A URL prefix | a mount in another file | `app.use('/users', usersRouter())` (Express) |
| A friendly URL | a central route table | `MapPageRoute(...)` in `Global.asax` (Web Forms) |
| Whether a class is framework code at all | a registration in the composition root | `AddQueryType<BookQueries>()` (HotChocolate v11/v12) |

A parser works on one file at a time, in a worker process, so it cannot read those other files.
`breezeai-cog` handles this with two building blocks:

1. **Facts on the base language index** — every case. Collect the fact once over the whole repo,
   resolve it, and put it on the index the parser receives as `ctx.resolution_index`.
2. **`claims_with_index`** — only the rare case where the file carries **no marker of its own**,
   so parser selection must consult the index to hand it to the framework parser at all.

## Flow

```
══════════════ STAGE 1 · BUILD THE INDEX (main process, once per language) ══════════════

  base parser's build_index(repo_root, files, jobs)          ← only BASE parsers are called
        │
        ├─ per file, in parallel ─────────────────────────────────────────────────────────┐
        │    record what the file declares          (types, symbols, …)                   │
        │    cheap byte gate ── no ──► skip         (e.g. b"AddGraphQLServer" in source)   │
        │         │ yes                                                                   │
        │         ▼                                                                       │
        │    record the raw fact + the context needed to resolve it                       │
        │    (the call, the namespaces/imports in scope, the file)                        │
        └─────────────────────────────────────────────────────────────────────────────────┘
        │  merge fragments (deterministic, order-independent)
        ▼
     resolve each raw fact once everything is known
        exactly one target ──► keep it
        ambiguous / none ────► drop it                        ← honest-null, never a guess
        │
        ▼
     index.<fact map>   keyed by what the consumer looks up (usually a repo-relative file)
        │
        │  index is pickled to every worker
        ▼
══════════════ STAGE 2 · PICK A PARSER (worker, per file) ═══════════════════════════════

  select(path, source, index)
    every candidate: claims_with_index(path, source, index)
      default (almost every parser) ──► claims(path, source)   byte check, index unused
      override (rare)               ──► byte check first, then a lookup in the index
    highest priority that says ✓ wins
        │
        ▼
══════════════ STAGE 3 · PARSE (worker, per file) ═══════════════════════════════════════

  parser.parse_file(ctx)
    fact = ctx.resolution_index.<fact map>.get(ctx.path)  ──► prefix / URL / root kind …
```

## Rules

- **Only the base language parser's `build_index` runs.** `_build_indexes` (`core/pipeline.py`)
  calls it for `base_parser_for(path)`. A framework parser that overrides `build_index` is never
  called, so its facts belong on the base index (`CSharpIndex`, the TypeScript index, …).
- **The index is the only state that reaches the workers.** Attributes set on a parser instance
  in the main process do not. Keep the index picklable.
- **Resolve once, look up cheaply.** Do the matching in Stage 1, when every declaration is known;
  Stage 2 and 3 should be dictionary lookups.
- **Key by file, not by name,** when a name can mean several things. A file path names one
  declaration; a simple class name can name many.
- **Ambiguous means absent.** Two candidates and no rule to choose → record nothing.
- **Override `claims_with_index` only for marker-less files.** Keep the byte check first, and fall
  back to it when `index` is None (single-file parsing has no repo pre-pass).
- **Clear raw data** collected for resolution before the index is shipped to the workers.

## Who uses it

| Fact (index field) | Base index | Consumer | Needs `claims_with_index`? |
|---|---|---|---|
| `class_heritage` (inherited `[Route]` / `[Authorize]`) | C#, VB | ASP.NET, VB ASP.NET | no |
| `page_routes` (`MapPageRoute` URLs) | C# | Web Forms | no |
| `data_loaders` (source-generated DataLoader types) | C# | HotChocolate | no |
| `hc_root_files` / `hc_root_types` (registered roots) | C# | HotChocolate | **yes** — the only one |
| `express_mounts` (router mount prefixes) | TypeScript | Express | no |
| `route_mounts`, `const_values` | TypeScript | Angular | no |
| `consts` (repo-wide constants) | Java | Vert.x | no |

## Adding a cross-file fact

1. **Add the field** to the base language's index dataclass, with a comment saying what the key
   and value are and when an entry is absent.
2. **Collect** in the per-file index pass, behind a cheap byte gate. Record the raw fact plus the
   context needed to resolve it.
3. **Merge** the fragment field in the reduce step.
4. **Resolve** after the reduce, when every declaration is known; drop ambiguous results; clear the
   raw list.
5. **Consume** it in the framework parser via `getattr(ctx.resolution_index, "<field>", None)`,
   tolerating `None`.
6. **Only if the target file has no marker:** override `claims_with_index` in the framework
   parser.
7. **Test end to end** through `analyze_repo` (with `jobs` 1 and 2), not only over a hand-built
   index — a hand-built index cannot catch a fact that never reaches the workers.

## Worked example: HotChocolate registration-only roots

A HotChocolate v11/v12 schema root can be a plain class that is a root **only** because the
composition root registers it. The class file has nothing HotChocolate in it, so this is the one
case that needs both building blocks.

```
 REPO
 ┌─────────────────────────────────────────────┐   ┌───────────────────────────────────────────┐
 │ Api/Program.cs                              │   │ Api/BookQueries.cs                        │
 │   using Catalog;                            │   │   namespace Catalog {                     │
 │   AddGraphQLServer()                        │   │     public class BookQueries {            │
 │     .AddQueryType<BookQueries>();           │   │       public Book GetBookById(int id) …   │
 └─────────────────────────────────────────────┘   │   } }        ← nothing HotChocolate here  │
                                                   └───────────────────────────────────────────┘

══════════════════════ STAGE 1 · BUILD THE INDEX (main process, once) ═══════════════════════

  For every .cs file (in parallel):
  ┌──────────────────────────────────────────────────────────────────────────────────────┐
  │ record its declared types        Catalog.BookQueries → Api/BookQueries.cs            │
  │                                                                                      │
  │ contains "AddGraphQLServer"?  ── no ──► skip                                         │
  │        │ yes                                                                         │
  │        ▼                                                                             │
  │ record each registration          (Api/Program.cs, query, "BookQueries",             │
  │ + the namespaces in scope there    scopes = {Catalog, …})                            │
  └──────────────────────────────────────────────────────────────────────────────────────┘
                                          │  combine all files
                                          ▼
  For every registration, once every type is known:
  ┌──────────────────────────────────────────────────────────────────────────────────────┐
  │ "BookQueries" under each namespace in scope ──► which declared types match?          │
  │                                                                                      │
  │    exactly one ─────────────────────► bind it (all files, if it's a partial class)   │
  │    several ──► one in the same project? ── yes ─► bind it                            │
  │                                        └─ no ──► bind nothing (ambiguous)            │
  │    none ────────────────────────────► bind nothing                                   │
  │                                                                                      │
  │ same class registered as two different kinds? ──► drop it                            │
  └──────────────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
              index.hc_root_files = { "Api/BookQueries.cs": { "BookQueries": "query" } }
              index.hc_root_types = { "BookQueries": "query" }   ← only if every class
                                          │                         of that name is registered
                                          │  index is sent to every worker
                                          ▼
═════════════════════ STAGE 2 · PICK A PARSER (worker, per file) ════════════════════════════

  select("Api/BookQueries.cs", source, index)
  ask every .cs parser: claims_with_index(path, source, index)

  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────────────────────────┐
  │ csharp (0)       │  │ csharp-aspnet(10)│  │ csharp-hotchocolate (25)                     │
  │ always ✓         │  │ no marker ✗      │  │  1. HotChocolate marker in file?   ✗         │
  └──────────────────┘  └──────────────────┘  │  2. setup file / graphql-dotnet?   ✗         │
                                              │  3. path in index.hc_root_files?   ✓         │
                                              └──────────────────────────────────────────────┘
                                          │
                                          ▼  highest priority that said ✓
                                 csharp-hotchocolate
                                          │
                                          ▼
═════════════════════ STAGE 3 · PARSE (worker, per file) ════════════════════════════════════

  roots for this file = hc_root_types  +  hc_root_files["Api/BookQueries.cs"]
                      = { "BookQueries": "query" }
                                          │
                                          ▼
  class BookQueries is a query root ──► each public method is an operation
                                          │
                                          ▼
                     route  QUERY  bookById   (handler GetBookById)
                     framework: graphql
```

### Same-named classes: why the key is a file

```
  Shop/Program.cs   using Shop.GraphQL;   AddQueryType<BookQueries>()  ─► Shop/BookQueries.cs   ✓
  Admin/Program.cs  using Admin.GraphQL;  AddQueryType<BookQueries>()  ─► Admin/BookQueries.cs  ✓
  Legacy/BookQueries.cs   (namespace Legacy, registered by no one)     ─► not in the map        ✗

  Stage 2 for Legacy/BookQueries.cs:  rule 3 ✗  ─►  plain csharp, no routes
```

Keyed by the simple name `BookQueries`, all three classes would have been roots. The name-only
lookup `hc_root_types` (for a target declared in another file, such as
`[ExtendObjectType(typeof(BookQueries))]`) is set only when **every** class of that name is a
registered root of one kind; here it stays unset, because the Legacy class is not registered.

### Code map

| Stage | Where |
|---|---|
| Collect registrations per file | `_index_root_registrations` in `parsers/csharp/imports.py` |
| Bind registrations to declaring files | `_registered_type_files`, `_resolve_root_registrations` (same file) |
| Index fields | `CSharpIndex.hc_root_files`, `hc_root_types` |
| Selection hook | `BaseParser.claims_with_index` (`parsers/base.py`), `select` (`core/registry.py`), `_parse_entry` (`core/executor.py`) |
| Claim + parse | `CSharpHotChocolateParser.claims_with_index`, `_root_types` (`parsers/csharp_hotchocolate/parser.py`) |
| Tests | registration section of `tests/unit/test_hotchocolate_parser.py`, including the end-to-end `test_registration_only_root_through_the_pipeline` |

### Limits

- A root declared **inside** the composition root itself is not captured: that file stays with
  `csharp-aspnet`.
- Only the generic form names a class. `AddQueryType(d => d.Name("Query"))` names a schema type,
  which `[ExtendObjectType("Query")]` targets already resolve.
- Registrations made outside a file containing `AddGraphQLServer` (for example inside a helper
  extension method in another file) are not read.
