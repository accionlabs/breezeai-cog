# User Guide

`breezeai-cog` reads a codebase and produces a **structured map of it** — every file, class,
function, import, and (optionally) the notable statements inside functions, such as HTTP API calls,
database queries, and web-framework routes. Think of it as turning source code into structured data
that other programs can search, analyze, or load into a graph.

That structured map is called a **code ontology**. `breezeai-cog` is the first stage of a larger
system: it generates the ontology, a separate Breeze backend loads it into a graph database, and
tools query it. This guide only covers the generator — the part you run against your code.

**What it understands today:**
- **Languages:** Python, TypeScript/JavaScript, Java.
- **Frameworks** (route detection on top of those languages): FastAPI (Python), NestJS & Angular
  (TypeScript), Spring Boot (Java).
- **Database/search schemas:** SQL DDL files and Elasticsearch mappings (via the HTTP service).

It does **not** run or execute your code — it only reads and parses the source text, so it's safe to
point at any repository.

**A few terms used throughout:**
- **NDJSON** — "newline-delimited JSON": a text file where **each line is its own JSON object**.
  Easy to stream and process line by line. The output is also **gzip-compressed** (`.ndjson.gz`).
- **Record** — one JSON object (one line) in that output. The first line describes the whole project;
  each remaining line describes one source file.
- **`uv`** — a fast Python package manager used to install and run the tool. (You can use plain
  `pip` instead; both are shown below.)

---

## Install

You don't add anything to the project you want to analyze — the tool reads it from the outside. You
only install the tool itself, once. Pick one of these:

```bash
# 1. Recommended — install an isolated `breezeai-cog` command onto your PATH (needs `uv`).
#    Add "[server]" to also get the HTTP service and S3/SQL support.
uv tool install /path/to/breezeai-cog
uv tool install "/path/to/breezeai-cog[server]"

# 2. Run it without installing (from anywhere) — `uv` fetches deps on the fly.
uv run --project /path/to/breezeai-cog breezeai-cog --help
```

Verify it works:

```bash
breezeai-cog version
breezeai-cog capabilities      # lists the languages/frameworks it currently understands
```

> Don't have `uv`? Install it from <https://docs.astral.sh/uv/>, or use option 3 with `pip`.

---

## Quick start: analyze a project

Point the tool at a folder and tell it where to put the output:

```bash
cd /path/to/your/project
breezeai-cog repo-to-json-tree --repo . --out ./out
```

- `--repo .` — the directory to analyze (`.` means "here").
- `--out ./out` — the **output directory**. The tool creates a file inside it named
  `<repo-name>-project-analysis.ndjson.gz`.

When it finishes, it prints a one-line summary (files / functions / classes found) and the path to
the output file.

---

## CLI commands

`breezeai-cog` has a handful of subcommands. Run any with `--help` for its options.

| Command | What it does |
|---|---|
| `repo-to-json-tree` | Analyze a **local** folder → write the gzipped NDJSON ontology. *(the main one)* |
| `capabilities` | Print the languages / frameworks / statement types it understands (JSON). |
| `schema` | Print the output's JSON Schema (the formal description of the record shapes). |
| `serve` | Start the HTTP service (needs the `[server]` install option). |
| `version` | Print the tool version. |

### `repo-to-json-tree` options

| Option | Default | Description |
|---|---|---|
| `--repo <dir>` | *(required)* | The folder to analyze. |
| `--out <dir>` | the repo's parent folder | Output **directory** (not a filename). The file is named `<repo>-project-analysis.ndjson.gz`. |
| `--language <name>` | all (auto-detected) | Only analyze this language. Repeat the flag for several (e.g. `--language python --language java`). |
| `--capture-statements` | off | Also record statements *inside* functions — needed to detect API calls, DB queries, and routes. Off by default because it produces more data. |
| `--jobs <n>` | number of CPU cores | How many files to parse in parallel. |
| `--verbose` | off | Print detailed (debug) logs. |

Example — analyze only Python and Java, with statement detail, using 8 parallel workers:

```bash
breezeai-cog repo-to-json-tree --repo . --language python --language java \
    --capture-statements --jobs 8 --out ./out
```

> This command only reads **local** folders. To analyze a remote repository by cloning or diffing
> commits, use the HTTP service's `/api/analyze-diff` endpoint (below).

---

## Understanding the output

The output file is one project summary followed by one record per source file.

**Line 1 — the project summary (`projectMetaData`):** the repository name, which languages were
found, and totals — number of files, functions, classes, and lines of code — plus a timestamp and
the tool version.

**Lines 2…N — one record per file (`FileRecord`):**

| Field | Meaning |
|---|---|
| `path`, `language`, `loc` | File path, detected language, line count. |
| `importFiles` | Imports that resolve to **other files in this repo** (the dependency edges). |
| `externalImports` | Imports of third-party/external packages. |
| `functions[]` | Each function/method: name, parameters, return type, decorators, visibility, the calls it makes. |
| `classes[]` | Each class/interface/enum: name, what it extends/implements, its methods. |
| `statements[]` | *(only with `--capture-statements`)* notable in-body statements — including detected API calls, DB queries, and framework routes. |
| `framework` | Set when a framework is detected in the file (e.g. `fastapi`, `nestjs`, `angular`, `spring`). |

**How things link together:** every function, class, and statement carries an `id`, and a
`parentId` pointing to its container (a method's `parentId` is its class; a statement's `parentId`
is the function it lives in). That parent/child linking is what lets the data form a graph.

To see the precise shape of every field, generate the schema:

```bash
breezeai-cog schema            # prints the JSON Schema to your screen
breezeai-cog schema --out schema.json
```

---

## Configuration

Anything you can pass as a flag can also be set as an **environment variable** (handy for servers
and CI). The rules:

- Most variables use the prefix `BREEZEAI_COG_` (e.g. `--jobs` ↔ `BREEZEAI_COG_JOBS`).
- A few infrastructure variables also accept their conventional names (`BREEZE_API_URL`, `API_KEY`,
  `AWS_*`).
- Variables can live in your shell environment or in a **`.env`** file in the working directory.
- A command-line flag always overrides the matching environment variable.

The repository ships a [`.env.example`](../.env.example) listing every variable with its default —
copy it and edit what you need:

```bash
cp .env.example .env
```

Most-used settings:

| Setting | Env var | Default |
|---|---|---|
| Languages | `BREEZEAI_COG_LANGUAGE` | all |
| Capture statements | `BREEZEAI_COG_CAPTURE_STATEMENTS` | `false` |
| Worker processes | `BREEZEAI_COG_JOBS` | CPU count |
| Max file size (bytes) | `BREEZEAI_COG_MAX_FILE_SIZE` | `2000000` |
| Parse timeout (seconds, per file) | `BREEZEAI_COG_PARSE_TIMEOUT` | `10` |
| Log level / format | `BREEZEAI_COG_LOG_LEVEL` · `BREEZEAI_COG_LOG_FORMAT` | `INFO` · `plaintext` |
| Server port | `BREEZEAI_COG_PORT` | `3000` |
| Backend URL (for upload) | `BREEZE_API_URL` | — |
| Backend API key | `API_KEY` | — |
| AWS S3 (server) | `AWS_ACCESS_KEY` · `AWS_SECRET_KEY` · `AWS_REGION` · `AWS_S3_BUCKET` | region `us-west-2` |

### Choosing which files are analyzed

By default the tool skips the usual noise (VCS folders, dependency and build directories, large
binaries). It also honors **`.gitignore`** and a tool-specific **`.repoignore`**, both using
standard gitignore syntax. To force-include something that would otherwise be ignored, add it to a
**`.repoinclude`** file (same syntax). These files are read per-directory, so rules can be scoped to
subfolders.

---

## HTTP service (advanced)

For automated pipelines, `breezeai-cog` can run as a web service instead of a one-off command.
This is optional — most users only need the CLI above.

```bash
breezeai-cog serve --port 3000        # requires the "[server]" install option
```

| Endpoint | What it's for |
|---|---|
| `GET /health` | Liveness check → `{ "status": "ok" }`. |
| `POST /api/analyze` | Analyze a small set of files sent **in the request body**, returns the ontology as JSON. |
| `POST /api/analyze-diff` | Analyze a **remote** GitHub/Bitbucket repo (or just the files changed between two commits), upload the result to S3, and notify the backend. |
| `POST /api/analyze-sql` | Parse an uploaded SQL `.sql` file's tables/views/indexes. |
| `POST /api/analyze-es` | Parse uploaded Elasticsearch mapping/settings JSON. |

The `-diff`, `-sql`, and `-es` endpoints stream their results to AWS S3 and notify the Breeze
backend, so they require the `AWS_*` and `BREEZE_API_URL` settings. Errors come back as
`{ "error": "<message>" }` with an HTTP `400` (bad request) or `422` (could not process the input).

---

## How it works (in brief)

You don't need this to use the tool, but a mental model helps when reading the output or tuning a
run:

1. **Scan** — walk the repo and pick the files to analyze, skipping ignored paths (see *Choosing
   which files are analyzed* above).
2. **Parse** — each file is handed to exactly one parser, chosen by file type and content (a
   framework-aware parser takes over when it recognizes its framework, e.g. a file importing
   `@nestjs/...`). Parsing uses **tree-sitter**, which builds an accurate syntax tree of the code.
3. **Extract** — the parser pulls out files, classes, functions, imports, and (if requested)
   statements, attaching `id`/`parentId` links and running shared detectors that label API calls,
   DB queries, and routes.
4. **Emit** — records are written out: as the gzipped NDJSON file (CLI), as a JSON response
   (`/api/analyze`), or streamed to S3 (the other endpoints). Files are parsed in parallel across
   worker processes for speed.

That's the whole pipeline: **scan → parse → extract → emit**. For the internals — the parser
selection model, the schema, how to add a new language or framework, and the project layout — see
the **[Developer Guide](DEVELOPER_GUIDE.md)** and **[Adding a Parser](ADDING_A_PARSER.md)**.
