---
title: "breezeai-cog — API Surface"
date: 2026-10-30
---

Scope: CLI commands (all options) and HTTP routes (server mode).

## CLI commands (5 total)

Entry point: `breezeai-cog` → `breezeai_cog.cli:main` (`src/breezeai_cog/cli.py:751`)

### `repo-to-json-tree`

Source: `src/breezeai_cog/cli.py:71`
Analyze a repository to a gzipped NDJSON capture file.

| Option | Default | Description |
|---|---|---|
| `--repo PATH` | required | Repository directory |
| `--out DIR` | `<repo>/.cog/` | Output directory for .ndjson.gz |
| `--language LANG` | all | Restrict to language (repeatable) |
| `--no-capture-statements` | off | Skip in-body statement capture |
| `--capture-templates` | off | Include markup/view template files |
| `--batch` | off | Treat `--repo` as workspace: analyze each immediate subdir |
| `--repo-list FILE` | all subdirs | With `--batch`: file of subdir names to include |
| `--jobs N` | CPU count | Worker processes |
| `--max-concat-depth N` | 100 (min 1) | Max `+` nesting for URL fold (guards recursion) |
| `--upload` | off | Upload result to backend |
| `--baseurl URL` | env `BREEZEAI_COG_BASEURL` or `BREEZE_API_URL` | Backend base URL (with `--upload`) |
| `--uuid UUID` | env | Project UUID (with `--upload`) |
| `--user-api-key KEY` | env `BREEZEAI_COG_USER_API_KEY` or `API_KEY` | Backend API key sent as `api-key` header |
| `--upload-timeout SECS` | 900 | Per-repo upload timeout |
| `--parallel-uploads N` | 1 | Concurrent uploads in batch mode |
| `--upload-max-retries N` | 1 | Retries after failed upload |
| `--force` | off | With `--batch --upload`: discard saved resume state |
| `--verbose` | off | DEBUG-level logging |
| `--capture-statements` | hidden | Deprecated; statements are captured by default, flag forces capture on |

Output: `<out>/<repo-name>-project-analysis.ndjson.gz`; skip report: `<repo>/.cog/<repo-name>-skipped-report.json`; logs: `<repo>/.cog/logs/`.

### `capabilities`

Source: `src/breezeai_cog/cli.py:704`
Print supported languages, frameworks, statement types, extensions as JSON. Derived from the parser registry at runtime — do not run the command to find the list; derive it from `core/registry.capabilities()` or the shard docs.

### `schema`

Source: `src/breezeai_cog/cli.py:732`
Generate the capture JSON Schema from Pydantic models. `--out FILE` writes to file; default prints to stdout.

### `serve`

Source: `src/breezeai_cog/cli.py:714`
Start the FastAPI service with uvicorn. `--port` (default: `$PORT`, else `Settings.port` = 3000, env `BREEZEAI_COG_PORT`); `--host` (default `0.0.0.0`).

### `version`

Source: `src/breezeai_cog/cli.py:746`
Print the tool version string.

---

## HTTP routes (13 total)

All routes are in `src/breezeai_cog/server/routes.py` and `src/breezeai_cog/server/git_routes.py`.
No authentication — service sits behind Kong ingress; called server-to-server.

### Core routes (routes.py)

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns `{"status": "ok"}` |
| `POST` | `/api/analyze` | In-memory file analysis (no disk, no S3) |
| `POST` | `/api/analyze-diff` | Clone/diff + parse + S3 upload + backend notify |
| `POST` | `/api/analyze-sql` | Parse DDL file → S3 → backend notify |
| `POST` | `/api/analyze-nosql` | Parse MongoDB JSON schema → S3 → backend notify |
| `POST` | `/api/analyze-es` | Parse Elasticsearch mapping/settings → S3 → backend notify |

#### `POST /api/analyze`

Source: `src/breezeai_cog/server/routes.py:77`

Body: `{"files": [{"path": str, "content": str}, ...], "projectName"?: str}`
Response: `{"projectMetaData": {...}, "files": [...]}`
Runs `analyze_in_memory` in threadpool. Returns 422 if no supported languages found.

#### `POST /api/analyze-diff`

Source: `src/breezeai_cog/server/routes.py:98`

Body: `{"repoUrl": str, "incomingCommitId": str, "gitBranch": str, "projectUuid": str, "codeOntologyId": str, "currentCommitId"?: str, "gitToken"?: str, "ignorePatterns"?: str|list}`
Response: `{"success": true, "storage_key": str, "deletedFiles": [...], "message": str}`
Stages: `git_acquire` → `ignore_patterns` → `parse_stream` → `upload`, then notify as a background task. A failure in a stage returns `{"error", "failedStep"}`. Without a usable `currentCommitId` (or when the provider client has no incremental support) the repo is fully cloned.
Storage key: `code-ontology/<projectUuid>/<incomingCommitId>.ndjson.gz`

#### `POST /api/analyze-sql`

Source: `src/breezeai_cog/server/routes.py:212`

Body: multipart form — `file` (SQL DDL), `projectUuid`, `dataLakeId` (all required)
Response: 202 with `{success, storage_key, dialect, tableCount, viewCount, procedureCount, indexCount, sequenceCount, message}`
Returns 422 if no DDL objects extracted.

#### `POST /api/analyze-nosql`

Source: `src/breezeai_cog/server/routes.py:275`

Body: multipart form — `file` (repeatable; MongoDB JSON schema files), `projectUuid`, `dataLakeId` (all required)
Response: 202 with `{success, storage_key, collections, recordCount, collectionCount, fieldCount, indexCount, message}`

#### `POST /api/analyze-es`

Source: `src/breezeai_cog/server/routes.py:330`

Body: multipart form — `file` (repeatable; ES mapping/settings JSON), `projectUuid`, `dataLakeId` (all required)
Response: 202 with `{success, storage_key, mode, mapping, setting, mappings, settings, recordCount, indexCount, fieldCount, settingsMatched, message}`

### Git / SCM routes (git_routes.py)

Source: `src/breezeai_cog/server/git_routes.py:41` (`router = APIRouter(prefix="/api/git")`)
All are `POST /api/git/*` with JSON body. Run provider calls in threadpool. Map `SCMError` → 400/502.

| Route | Body | Returns |
|---|---|---|
| `POST /api/git/check-update` | `repoUrl`, `gitBranch`, `gitToken?`, `currentCommitId?` | `{change, latestCommitId, currentCommitId, fileUpdatedCount, fileDeletedCount, latestCommit, changedFiles, deletedFiles}` |
| `POST /api/git/latest-commit` | `repoUrl`, `gitBranch`, `gitToken?` | `{sha, message, author, date}` |
| `POST /api/git/compare` | `repoUrl`, `baseCommitId`, `headCommitId`, `gitToken?` | `GitDiffResult: {baseSha, headSha, totalFiles, files: [{filename, status, additions, deletions, patch?}]}` |
| `POST /api/git/tree` | `repoUrl`, `commitId`, `gitToken?` | `{files, count}` |
| `POST /api/git/pull-request` | `repoUrl`, `pullRequestId`, `gitToken?` | `{number, title, state, baseBranch, headBranch, baseCommitId, headCommitId, url}` |
| `POST /api/git/pr-comment` | `repoUrl`, `pullRequestId`, `body`, `gitToken?` | `{id, url}` — only write route; not retried |
| `POST /api/git/parse-pr-url` | `prUrl` | `{source, repoUrl, prId, prLinks: {repo, diff, comment, decline}}` — no provider call |
