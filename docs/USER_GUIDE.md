# User Guide

`breezeai-cog` parses a source repository into the **Part C capture contract** — gzipped
NDJSON describing files, classes, functions, statements, imports, and detected routes/API/DB
calls. It runs as a **CLI** (analyze a local repo) or an **HTTP service** (analyze in-memory
files, git diffs, SQL, or Elasticsearch mappings).

Supported languages: **Python, TypeScript/JavaScript, Java**, with **FastAPI, NestJS, Angular,
and Spring Boot** framework detection.

---

## Install

The tool reads whatever path you point it at, so the target repo needs nothing installed.

```bash
# Recommended: install an isolated `breezeai-cog` command on your PATH
uv tool install /path/to/breezeai-cog            # add "[server]" for the HTTP service + S3/SQL

# Or run in place without installing
uv run --project /path/to/breezeai-cog breezeai-cog --help

# Or into an existing environment
pip install -e "/path/to/breezeai-cog[all]"
```

---

## Quick start

```bash
cd /path/to/your/project
breezeai-cog repo-to-json-tree --repo . --out ./out
# writes ./out/<repo-name>-project-analysis.ndjson.gz
```

Inspect the result:

```bash
f=$(ls ./out/*-project-analysis.ndjson.gz)
zcat "$f" | head -1 | jq        # projectMetaData (first line)
zcat "$f" | sed -n 2p | jq      # first file record
```

---

## CLI commands

| Command | Purpose |
|---|---|
| `repo-to-json-tree` | Analyze a **local** directory → gzipped NDJSON |
| `capabilities` | Print supported languages / frameworks / statement types (JSON) |
| `serve` | Start the FastAPI service (needs the `[server]` extra) |
| `version` | Print the tool version |

### `repo-to-json-tree` options

| Option | Default | Description |
|---|---|---|
| `--repo <dir>` | *(required)* | Directory to analyze |
| `--out <dir>` | the repo's parent | Output **directory**; file is `<repo>-project-analysis.ndjson.gz` |
| `--language <name>` | all (auto-detect) | Restrict to a language; repeatable |
| `--capture-statements` | off | Also emit in-body statements (routes/API/DB detection) |
| `--jobs <n>` | CPU count | Worker processes |
| `--verbose` | off | DEBUG logging |

```bash
breezeai-cog repo-to-json-tree --repo . --language python --language java \
    --capture-statements --jobs 8 --out ./out
```

> The CLI only parses **local** directories. Cloning/diffing a remote git repo is
> server-only (the `/api/analyze-diff` endpoint).

---

## Output format

A gzipped NDJSON stream — one JSON object per line:

- **Line 1** — `projectMetaData`: repo name, analyzed languages, totals (files / functions /
  classes / LOC), config summary, `generatedAt`, `toolVersion`.
- **Lines 2…N** — one `FileRecord` per file: `path`, `language`, `loc`, `importFiles`,
  `externalImports`, `functions[]`, `classes[]`, and (with `--capture-statements`) `statements[]`.

`id` / `parentId` fields link the graph (functions/methods → class, statements → owner). The
full schema is the Pydantic model in `src/breezeai_cog/schemas/` (the source of truth).

---

## Configuration

Every CLI flag has an environment-variable equivalent (prefix `BREEZEAI_COG_`), readable from the
process environment or a `.env` file. Flags win over env vars. Copy
[`.env.example`](../.env.example) to `.env` for the full list of variables with defaults:

```bash
cp .env.example .env
```

A few server/backend vars also accept their conventional unprefixed names (e.g. `BREEZE_API_URL`,
`API_KEY`, `AWS_*`), shown below.

| Setting | Env var | Default |
|---|---|---|
| Languages | `BREEZEAI_COG_LANGUAGE` | all |
| Capture statements | `BREEZEAI_COG_CAPTURE_STATEMENTS` | `false` |
| Worker processes | `BREEZEAI_COG_JOBS` | CPU count |
| Max file size (bytes) | `BREEZEAI_COG_MAX_FILE_SIZE` | `2000000` |
| Parse timeout (s, per file) | `BREEZEAI_COG_PARSE_TIMEOUT` | `10` |
| Log level / format | `BREEZEAI_COG_LOG_LEVEL` · `BREEZEAI_COG_LOG_FORMAT` | `INFO` · `plaintext` |
| Server port | `BREEZEAI_COG_PORT` | `3000` |
| Backend URL | `BREEZE_API_URL` | — |
| Backend API key | `API_KEY` | — |
| AWS S3 | `AWS_ACCESS_KEY` · `AWS_SECRET_KEY` · `AWS_REGION` · `AWS_S3_BUCKET` | region `us-west-2` |

---

## HTTP service

```bash
breezeai-cog serve --port 3000        # requires the [server] extra
```

| Endpoint | Request | Response |
|---|---|---|
| `GET /health` | — | `{ "status": "ok" }` |
| `POST /api/analyze` | `{ files: [{path, content}], projectName? }` | `200 { projectMetaData, files }` (parsed in-process) |
| `POST /api/analyze-diff` | `{ repoUrl, incomingCommitId, gitBranch, projectUuid, codeOntologyId, currentCommitId?, gitToken? }` | `200 { success, s3Key, deletedFiles, message }` — clones/diffs (GitHub/Bitbucket), streams to S3, notifies the backend |
| `POST /api/analyze-sql` | multipart `file` (one `.sql`) + `projectUuid`, `dataLakeId`, `repositoryName?` | `202 { success, s3Key, dialect, tableCount, … }` |
| `POST /api/analyze-es` | multipart `file[]` (ES mapping/settings JSON) + `projectUuid`, `dataLakeId`, `repositoryName?` | `202 { success, s3Key, mode, indexCount, fieldCount, … }` |

Validation errors return `{ "error": "<message>" }` with a `400` (or `422` for unprocessable
input). The streaming endpoints (`-diff`/`-sql`/`-es`) need the AWS and `BREEZE_API_URL` settings
configured.

---

For architecture and contributing, see the [Developer Guide](DEVELOPER_GUIDE.md).
