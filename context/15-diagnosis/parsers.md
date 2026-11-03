---
title: "breezeai-cog — Diagnosis: Parsers"
date: 2026-10-30
---

Scope: Failure modes, assumptions, blast radius, and fixes across tree-sitter parsing, language detection, framework extractors, and language-specific parsers.

## Tree-sitter version incompatibility

- **Symptom:** `AttributeError` on `Parser.timeout_micros` (under tree-sitter 0.26+) or `ValueError: Parsing failed` (a per-file timeout, raised by the binding and caught by the per-file guard).
- **Invariant / Assumption:** Tree-sitter is strictly pinned to `>=0.24,<0.26`. Version 0.26 removed `Parser.timeout_micros`. In version 0.25, `tree_sitter_language_pack.get_parser` rejects raw byte input, so `breezeai_cog.parsers.treesitter` constructs `Parser(get_language(name))` directly.
- **Origin site:** `get_parser` at `src/breezeai_cog/parsers/treesitter.py:49` and the `timeout_micros` assignment in `parse_source` at `src/breezeai_cog/parsers/treesitter.py:71`.
- **Blast radius:** Under 0.26+, every parse fails at the `timeout_micros` assignment; the per-file guard drops each file, so the capture is empty. A genuine timeout (`ValueError`) drops only that file.
- **Fix approach:** Ensure environment uses `tree-sitter>=0.24,<0.26` as specified in `pyproject.toml`. Do not upgrade tree-sitter to 0.26+ without refactoring timeout handling.

## Tree-sitter parser state leakage across files

- **Symptom:** Corrupted line spans, garbled AST statements, or AST nodes containing text offsets from a previous file.
- **Invariant / Assumption:** Parsers are cached per-process (`@lru_cache`). When a parse is halted by timeout or error, it retains internal parser state. Starting every parse with `parser.reset()` is mandatory.
- **Origin site:** `src/breezeai_cog/parsers/treesitter.py:55` in `parse_source(language, source, timeout_micros)`; `parser.reset()` is at line 68.
- **Blast radius:** If `parser.reset()` fails or is bypassed, subsequent files parsed by that worker process inherit corrupted state.
- **Fix approach:** Verify `parser.reset()` (line 68) runs before the `timeout_micros` assignment (line 71) and `parser.parse(source)`.

## Node 24 native tree-sitter binding failure during onboarding

- **Symptom:** Silent failure or process termination with zero captures emitted when running repository onboarding via `/breeze:onboard-repository`.
- **Invariant / Assumption:** Tree-sitter native bindings require Node 22 LTS when driven by node-based onboarding wrappers. Node 24 silently fails on native addon loading.
- **Origin site:** Onboarding toolchain integration; documented in `context/09-integrations-and-async.md`.
- **Blast radius:** Entire repository onboarding script terminates without generating capture envelope.
- **Fix approach:** Switch Node runtime to Node 22 (`nvm use 22` or `fnm use 22`) before launching onboarding scripts.

## Groovy parser emits shallow skeleton captures

- **Symptom:** Groovy source files produce class/method names but omit fine-grained statement graphs or detailed endpoint parameters.
- **Invariant / Assumption:** Groovy is a second-tier parser. The default `tree_sitter_language_pack` Groovy grammar is an unstructural "command soup", so cog loads `dekobon-tree-sitter-groovy` (Accionlabs fork in `_EXTERNAL_GRAMMARS`). It produces structural type captures, but statement-level deep parsing is not implemented.
- **Origin site:** `_load_dekobon_groovy` at `src/breezeai_cog/parsers/treesitter.py:27`, `_EXTERNAL_GRAMMARS` at `src/breezeai_cog/parsers/treesitter.py:43`, and `src/breezeai_cog/parsers/groovy/`.
- **Blast radius:** Groovy/Grails projects have coarser code-ontology graphs than Java or TypeScript projects.
- **Fix approach:** Expected behavior by design. For deeper capture, extend `breezeai_cog/parsers/groovy/` using the instructions in `skills/extend-capture/`.

## Shared header parsed with the wrong language (C vs C++)

- **Symptom:** A `.h` file is captured with the other language's grammar: C++ declarations missing from a header read as C, or C-only constructs (`int new;`, K&R definitions) lost from one read as C++.
- **Invariant / Assumption:** `.h` is registered by both C and C++, so `core/ownership.py` decides it from the repo: C++-only syntax, then the languages of files that include it (a header included from both is C), then its folder and the repo, then the default (C++). A reference that matches no scanned file, or more than one, is not evidence.
- **Origin site:** `resolve_owners` at `src/breezeai_cog/core/ownership.py:129`; the content proof `cpp_only_syntax` at `src/breezeai_cog/parsers/cpp/ownership.py:74`. The `ownership.decided` debug log records the rule that decided each file.
- **Blast radius:** One header's classes, functions and call edges; cross-file calls into it may stay unresolved.
- **Fix approach:** Run with `BREEZEAI_COG_LOG_LEVEL=DEBUG` and read `ownership.decided` for the file.

## Unrecognized framework routes or annotations

- **Symptom:** Controller classes appear in capture, but specific route handlers or HTTP methods are missing from `FileRecord.handlers`.
- **Invariant / Assumption:** Framework additive parsers inspect specific annotations and decorators (e.g. `@GetMapping`, `[HttpGet]`, `@router.get`). Custom wrapper annotations or dynamically registered routes are not matched by standard visitor rules.
- **Origin site:** Framework parser detectors (e.g. `parsers/java_springboot/`, `parsers/csharp_aspnet/`, `parsers/typescript_express/`).
- **Blast radius:** Incomplete route tables and unmapped API surface in code ontology.
- **Fix approach:** Inspect annotation match patterns in the relevant framework parser. Add the custom annotation or decorator name to the framework parser's recognized route decorator set.
