# Developer Guide

How `breezeai-cog` is built, how to work on it, and how to extend it.

---

## Setup

The project is [uv](https://docs.astral.sh/uv/)-managed.

```bash
uv sync --extra all        # runtime + server/upload extras + dev tools
uv run pytest -q           # run the test suite
uv run ruff check .        # lint
uv run mypy                # type-check
```

Dev tools live in the PEP 735 `dev` group (installed by `uv sync`); the HTTP service / S3 / SQL
deps live in the `server` and `all` optional-dependency extras.

---

## Project layout

```
src/breezeai_cog/
  schemas/        # Pydantic v2 capture contract — the SOURCE OF TRUTH (JSON Schema generated on demand)
  config.py       # Settings: CLI flags ↔ env vars (BREEZEAI_COG_*)
  logging.py      # structlog setup
  core/           # registry · scanner · ignore · pipeline · executor (multiprocess)
  parsers/        # tree-sitter parsers
    base.py       #   BaseParser, ParseContext, the LanguageParser protocol
    treesitter.py #   grammar loading + bounded parse
    detection/    #   shared, language-agnostic API/DB/route classification
    <lang>/       #   base language parser (python, typescript, java, csharp, vb, kotlin, groovy, cpp, …)
    <lang>_<fw>/  #   framework parser (e.g. python_fastapi, typescript_nestjs, java_springboot, csharp_wcf) — run `breezeai-cog capabilities` for the live list
  emit/           # id convention · ndjson · gzip · sinks (file/memory)
  infra/          # object-storage providers behind the InfraStream interface
    interface.py  #   InfraStream — the provider-agnostic contract
    provider.py   #   open_stream(key, settings) — the entry point callers use
    factory.py    #   ProviderFactory maps ProviderType -> implementation
    aws/s3.py     #   AWSStreamUpload — streaming gzip upload to S3
  integrations/   # external services (server-only)
    scm/          #   git-hosting providers behind the AbstractSCMClient interface
      base.py     #     AbstractSCMClient · RepoRef · ChangeSet · SUPPORTED_PROVIDERS
      factory.py  #     SCMClientFactory.for_repo(ref, settings, token) — the entry point
      http.py     #     SCMHttpClient — shared httpx wrapper: retry, page cap, error mapping
      retry.py    #     request_with_retry (429/502/503/504 + transport errors, jittered backoff)
      repository.py #   parse_repo_url → RepoRef (public clouds + configured self-hosted hosts)
      github.py · bitbucket.py · gitlab.py · azure_devops.py   # one class per provider
  analyzers/      # non-AST: sql (DDL via sqlglot), es (Elasticsearch mappings)
  services/       # analysis · inprocess · diff · notify
  server/         # FastAPI app · routes · git_routes (/api/git/* for the backend) · deps · git orchestration (clone / REST diff) · errors
  cli.py          # Typer CLI
```

---

## How it works

```
scan → select parser → parse → emit FileRecord → sink (file / memory / S3)
                                                 → projectMetaData
```

1. **Scan** (`core/scanner.py`) walks the repo, applying hierarchical ignore/include rules
   (`.gitignore`/`.repoignore` + per-language defaults) and size limits.
2. **Select** (`core/registry.py`) — each file is parsed by **exactly one** parser:
   `select(path, source)` returns the highest-`priority` parser whose `claims(path, source)` is
   `True`, falling back to the base language parser. Framework parsers (priority 10) sniff their
   signature (e.g. `b"@nestjs/" in source`); the base parser (priority 0) is the fallback. No file
   is ever parsed twice — multiple frameworks for one language coexist by per-file content.
3. **Parse** (`parsers/<lang>/`) turns the tree-sitter AST into a `FileRecord`. The id convention
   (`emit/ids.py`) assigns deterministic `id`/`parentId`, so framework parsers attach routes to the
   right handler.
4. **Emit** through a sink: the gzipped-NDJSON **file** sink (CLI), the **in-memory** sink
   (`/api/analyze`), or the streaming **S3** upload (`/api/analyze-diff|-sql|-es`).

The pipeline runs parsers across processes (`core/executor.py`, spawn-safe) for the CLI, and
sequentially in-process for the server's small payloads.

---

## Adding a parser

A new **language** or **framework** parser is a self-contained subpackage. Start with the
**[Extending Capture skill](../skills/extend-capture/SKILL.md)** (which kind to build + the
reliability discipline); the full mechanical recipe — structure, the tools to reuse, the
`claims`/`priority` selection model, and testing standards — is in the
**[Parser Reference](parser-reference.md)**.

In short:

- **Language** (new grammar) → `parsers/<lang>/` implementing the full parser; `priority = 0`.
- **Framework** (on an existing language) → `parsers/<lang>_<framework>/` that subclasses the base
  parser, sets `priority` + `claims`, and adds only its detection (single parse, no duplication).
- Register by exporting `PARSERS = [...]` from the subpackage's `__init__.py` (auto-discovered).
- Cross-language API/DB call recognition is shared in `parsers/detection/` — feed it, don't fork it.

---

## Object storage providers

The server streams NDJSON.gz to object storage, then notifies the backend with only the
**storage key** — the backend downloads and ingests that object later. Callers never touch a
cloud SDK; they use the provider-agnostic `InfraStream`:

```python
stream = open_stream(key, settings)   # infra/provider.py
for record in records:
    stream.write_line(json.dumps(record) + "\n")
storage_key = stream.close()          # the key is proof the object is complete
```

`open_stream` resolves `settings.infra_provider` through `ProviderConfig`, and `ProviderFactory`
returns the implementation for that `ProviderType`.

**Two rules any implementation must honour** (see the contract on `InfraStream`):

1. **Retries belong to the provider SDK.** The upload streams from an OS pipe, which cannot be
   rewound — retrying a whole upload would re-send only the unconsumed tail and store a truncated
   object. Configure the SDK to retry at a granularity where it buffers the bytes itself (botocore
   replays an individual multipart part), and never wrap the upload in a retry loop.
2. **`close()` must raise if the object is incomplete, and must not return a key.** Callers treat a
   returned key as proof the object is readable, so swallowing an upload error would point the
   backend at corrupt data. Failing loudly means the route returns 500 *before* the notification
   fires.

### Adding a provider

1. Add the member to `ProviderType` (`infra/provider_type.py`).
2. Widen the `Literal` on `infra_provider` in `config.py` (e.g. `Literal["aws", "azure"]`).
3. Create `infra/<provider>/` implementing `InfraStream` (`write_line` + `close`). Keep the SDK,
   client construction, auth, and retry configuration inside that package.
4. Add the `case` to `ProviderFactory.create_stream()` — the `case _` guard exists so a forgotten
   case raises instead of silently returning `None`.
5. Reuse the generic `storage_*` settings for retries and timeouts rather than adding
   provider-prefixed twins.

---

## Git providers (`/api/analyze-diff`)

`server/git.py` is orchestration only: it decides between a shallow **clone** and an
**incremental REST diff**, materialises the temp tree, and maps errors. Everything
provider-specific lives in `integrations/scm/` behind one contract:

```python
ref = parse_repo_url(body["repoUrl"], settings.scm_instances)        # -> RepoRef | None
client = SCMClientFactory.for_repo(ref, settings, request_token=body.get("gitToken"))
with client:
    client.branch_head(ref, branch)          # CommitInfo(sha, message, author, date) at the tip
    client.tree(ref, commit)                 # every blob path at a commit
    client.pull_request(ref, number)         # PullRequestInfo: normalised state, full base/head SHAs
    client.post_pr_comment(ref, number, body) # PrComment(id, url) — the only write; not retried
    client.compare(ref, base, head)          # ChangeSet(changed, deleted)
    client.file_content(ref, path, commit)   # UTF-8 text; SCMAPIError for binary/unreadable
    client.clone_url(ref)                    # https URL with the credential embedded
```

Rules every provider honours:

1. **Go through `SCMHttpClient`.** It owns the `httpx.Client`, the retry policy, the pagination
   ceiling (`scm_max_pages`) and error conversion. Reads use `get*` (retried); the single write uses
   `post_json`, which is **never retried** because a repeated POST can create a duplicate. Never raise with a response body in the
   message — bodies can echo the credential; the wrapper logs them at DEBUG only.
2. **Return repo-relative paths without a leading slash** (Azure DevOps sends `/src/a.cs`).
3. **A rename marks the old path deleted** and the new path changed.
4. **Decode file content strictly.** `get_text` raises on non-UTF-8 so a binary is skipped by the
   orchestration rather than written as replacement characters and handed to a parser.
5. **Build URLs from `RepoRef.host`**, never from a hard-coded public host, so self-hosted
   instances work. The REST base comes from the factory (`scm_instance_base_url_mapping`, else
   `<provider>_api_base_url`).
6. **`supports_incremental`** is the gate `acquire_diff` reads. Set it `False` only for a provider
   whose tree / compare / content calls are not implemented.

Credential order in `resolve_scm_token`: request `gitToken` → `scm_token_<provider>` → anonymous
(allowed; public repos work and the client logs a warning).

### Adding a git provider

1. Create `integrations/scm/<provider>.py` with a class deriving `AbstractSCMClient`; set
   `provider` and `supports_incremental`; take `(token, settings, api_base_url=None, *,
   transport=None, sleep=time.sleep)` so tests can inject `httpx.MockTransport`.
2. Register its dotted path in `_PROVIDER_CLASSES` (`factory.py`) and add the slug to
   `SUPPORTED_PROVIDERS` (`base.py`) — the module asserts the two agree.
3. Add `<provider>_api_base_url` and `scm_token_<provider>` to `Settings`, `.env.example` and the
   user guide table.
4. Teach `repository.py` its URL grammar: a public-host pattern in `_public`, and the
   self-hosted path grammar in `_instance`.
5. Add `tests/unit/integrations/test_scm_<provider>.py` using the `router` fixture; pin the
   request shapes (path, query, headers), the change mapping incl. renames, binary rejection,
   and the clone URL for public and self-hosted hosts.

---

## The capture contract

`src/breezeai_cog/schemas/` (Pydantic v2) **is the source of truth**. The language-agnostic JSON
Schema is *generated on demand* from the models — `breezeai-cog schema` (or `export_json_schema()`) —
for cross-language consumers; it is not committed. `SCHEMA_VERSION` gates parser registration.
Changing the contract means editing the models; consumers regenerate the schema when they need it.

### Statement `nodeType` (route detectors)

A statement's `nodeType` is the **raw tree-sitter `node.type`** of the node it was extracted from in
the **file's primary (host-language) parse tree**. A detection with **no backing node in that host
tree** — a route decomposed from an annotation / attribute / decorator / config / filename — must use
the single sentinel **`nodeType="synthetic"`**. Never invent a label (e.g. `graphql_field`,
`page_directive`).

Watch the host-tree qualifier for **embedded DSLs**: SQL in a string, or GraphQL SDL inside a
`` gql`…` `` template, is text — there is no host-AST node for the SQL column or the SDL field. Follow
the established precedent (a SQL string `const q = "SELECT …"` → `lexical_declaration`, the *host* node
that wraps it) — surface the wrapping host node, or `synthetic` when there's no distinct one per item;
**never** the embedded grammar's own node type (e.g. `field_definition`). The page / mount / rpc /
`query`/`mutation`/`subscription` (GraphQL) distinction is carried by `routeKind` + `framework`, **not** by `nodeType`, so nothing is lost.

### IaC parsers (`semanticType` + `platform`)

Config parsers for Infrastructure-as-Code tools use a dedicated **`iac_*` `semanticType` family**
(e.g. `iac_resource`, `iac_provider`, `iac_variable`) instead of the route/db/event family — see
`schemas/enums.py` for the full list. They also set a **`platform`** field (`"aws"`, `"azure"`,
`"google"`, `etc.) on each `Statement` (inferred from the resource-type prefix or
provider name) and on `FileRecord` (dominant platform across statements). Both fields are absent
when no cloud provider can be inferred, and omit from serialized output via `exclude_none=True`.

---

## Testing & quality

- `uv run pytest -q` — unit tests under `tests/unit/` (one per module/parser). Parser tests parse a
  synthetic sample, assert extraction, and validate the record against the JSON Schema.
- Server tests use FastAPI's `TestClient` with **injected fake deps** (`ServerDeps`), so the
  streaming endpoints are covered without AWS or a live backend.
- Keep `ruff` and `mypy` clean. Match surrounding style; tests are expected with new parsers/endpoints.

---

For installation and usage, see the [User Guide](USER_GUIDE.md).
