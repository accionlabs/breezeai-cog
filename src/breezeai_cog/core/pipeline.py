"""Analysis pipeline.

``run`` drives the full analysis to a sink using the parallel executor and assembles
``projectMetaData``. ``iter_records`` streams ``(ScanEntry, FileRecord)`` sequentially
in-process for the library's ``iter_file_records``. The ``projectMetaData``-first
temp strategy lives in ``FileSink``.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .._version import __version__
from ..emit import split_oversized_statements
from ..logging import DETAIL_LOGGER, get_logger
from ..parsers.additive import fact_sources
from ..parsers.contract import validate as validate_contract
from ..parsers.markdown.classify import AGENT_DOCUMENT_KINDS
from ..parsers.markdown.parser import MAX_STATEMENT_FILE_BYTES, MAX_TEXT_BYTES
from ..schemas import FileRecord, ProjectMetaData
from . import executor
from .ignore import IgnoreEngine
from .nx import discover_nx_projects
from .ownership import competing_languages, resolve_owners, shared_languages
from .registry import base_parser_for, discover_builtin, registered, template_extensions
from .scanner import ScanEntry, scan
from .skips import SkipReport

log = get_logger("breezeai_cog.pipeline")
# File-only sink for per-file skip rows — captured in the log, never echoed to the console.
detail_log = get_logger(DETAIL_LOGGER)


def _classifier(languages: set[str] | None) -> Callable[[str], str | None]:
    def classify(path: str) -> str | None:
        base = base_parser_for(path)  # the language parser (extension allow-list + label)
        if base is None:
            return None
        if languages and base.name not in languages:
            return None
        return base.name

    return classify


def _template_skip_rule(settings) -> Callable[[str], str | None] | None:
    """The ``skip_rule`` that drops markup/view files, or ``None`` when
    ``--capture-templates`` is on.

    Extension-based, so it costs a suffix lookup and never reads the file. Lowercased, as
    ``BaseParser.matches`` is for template extensions, so ``Site.Master`` and
    ``Default.ASPX`` (endemic in older ASP.NET trees) land in the ``template`` bucket
    without the flag and are parsed with it.
    """
    if settings.capture_templates:
        return None
    exts = template_extensions()  # resolved once, not per file
    if not exts:
        return None

    def skip_rule(path: str) -> str | None:
        return "template" if os.path.splitext(path)[1].lower() in exts else None

    return skip_rule


def _warn_if_only_template_languages(settings, gate_on: bool) -> None:
    """Warn when ``--language`` selects only parsers that own nothing but markup, while the
    template gate is on — otherwise the run silently produces zero files."""
    if not gate_on or not settings.languages:
        return
    selected = set(settings.languages)
    template_only = {
        p.name
        for p in registered()
        # every extension it owns is markup — so with the gate on it can match nothing
        if (tmpl := getattr(p, "template_extensions", ())) and set(p.extensions) <= set(tmpl)
    }
    if selected and selected <= template_only:
        log.warning(
            "scan.templates_gated",
            languages=sorted(selected),
            hint="these languages only own template files, which are skipped by default; "
                 "pass --capture-templates to analyze them",
        )


def _lang_ignored(engine: IgnoreEngine, path: str, language: str) -> bool:
    """Whether ``language``'s layer-2 rules drop ``path`` (and nothing force-includes it)."""
    return engine.is_lang_ignored(path, language) and not (
        engine.is_lang_included(path, language) or engine.is_included_global(path)
    )


def _scan_entries(
    repo_root: Path, settings, report: SkipReport | None = None, jobs: int = 1
) -> list[ScanEntry]:
    """The files to parse, each with its language. A shared file (an extension several
    languages register) gets the language the ownership step decides for it."""
    discover_builtin()
    engine = IgnoreEngine.build(registered())
    languages = set(settings.languages) if settings.languages else None
    # The ownership step reads the files of every language competing for a shared extension,
    # so a --language run scans those too and drops them once shared files are decided.
    scan_languages = languages | competing_languages(languages) if languages else None
    debug_on = settings.log_level == "DEBUG"
    skip_rule = _template_skip_rule(settings)
    _warn_if_only_template_languages(settings, skip_rule is not None)

    def on_skip(path: str, reason: str, *, is_dir: bool = False, size: int | None = None) -> None:
        if report is not None:  # tally + detail for the run summary and the sidecar report
            report.record(path, reason, is_dir=is_dir, size=size)
        if debug_on:  # gated so structlog never renders below DEBUG
            log.debug("scan.file.skipped", path=path, reason=reason, is_dir=is_dir)

    scanned: list[ScanEntry] = []
    for entry in scan(
        repo_root, _classifier(scan_languages),
        engine=engine, max_file_size=settings.max_file_size,
        skip_rule=skip_rule, on_skip=on_skip,
    ):
        # Per-language layer-2 filter, applied post-scan and scoped to the file's
        # own classified language — so e.g. C#'s NuGet ``packages/`` never prunes a
        # TypeScript ``packages/`` workspace. Universal built-ins already pruned the walk.
        # A shared file has no language yet: it is dropped here only when every candidate
        # language drops it, and checked against its decided language below.
        candidates = shared_languages(entry.path) or (entry.language,)
        if all(_lang_ignored(engine, entry.path, lang) for lang in candidates):
            on_skip(entry.path, "ignored")
            continue
        scanned.append(entry)

    decisions = resolve_owners(repo_root, scanned, jobs)
    entries: list[ScanEntry] = []
    for entry in scanned:
        decision = decisions.get(entry.path)
        if decision is not None:
            entry = ScanEntry(path=entry.path, language=decision.language)
            if debug_on:
                log.debug("ownership.decided", path=entry.path, language=decision.language,
                          rule=decision.rule)
            if _lang_ignored(engine, entry.path, entry.language):
                on_skip(entry.path, "ignored")
                continue
        if languages and entry.language not in languages:
            on_skip(entry.path, "unsupported")  # a competing language's file, read as evidence
            continue
        entries.append(entry)
    return entries


def _owners(entries: list[ScanEntry]) -> dict[str, str]:
    """Shared file → its decided language, for the workers' parser selection."""
    return {e.path: e.language for e in entries if shared_languages(e.path)}


def _build_indexes(
    repo_root: Path, entries: list[ScanEntry], jobs: int = 1, *, debug_on: bool = False,
    parse_timeout_micros: int = 0,
) -> dict:
    """Run each parser's optional ``build_index`` once (main process). Maps
    parser-name → index; threaded into ParseContext.resolution_index. ``jobs`` is the same
    worker count as the parse stage — passed to ``build_index`` so its per-file parse can
    parallelize (``jobs<=1`` keeps it serial). A parser that sets
    ``build_index_accepts_timeout`` also gets the run's parse timeout."""
    bases: dict[str, object] = {}
    files: dict[str, list[Path]] = {}
    for entry in entries:
        # index is per base language, keyed by its name; a shared file joins its decided one
        base = base_parser_for(entry.path, entry.language)
        if base is None:
            continue
        bases[base.name] = base
        files.setdefault(base.name, []).append(repo_root / entry.path)
    # A registered fact may read files another parser owns (``FactSpec.sources``): hand them to
    # that language's index too. The owning parser still parses and emits them.
    for language, exts in fact_sources().items():
        if language not in bases:
            continue
        own = set(files[language])
        files[language] += [
            repo_root / e.path for e in entries
            if str(e.path).lower().endswith(exts) and repo_root / e.path not in own
        ]
    indexes: dict[str, object] = {}
    for name, base in bases.items():
        start = time.perf_counter()
        try:
            timeout = (
                {"parse_timeout_micros": parse_timeout_micros}
                if getattr(base, "build_index_accepts_timeout", False) else {}
            )
            index = base.build_index(repo_root, files[name], jobs, **timeout)
        except Exception as exc:
            # Per-file workers already isolate their own failures; this is a last-resort
            # guard so one language's index cannot abort the whole run. Resolution simply
            # falls back to "no index for this language" (honest-null), never a crash.
            log.warning("build_index.failed", parser=name, error=str(exc))
            index = None
        if debug_on:
            log.debug(
                "build_index.done", parser=name, files=len(files[name]),
                ms=round((time.perf_counter() - start) * 1000, 1), built=index is not None,
            )
        if index is not None:
            indexes[name] = index
    return indexes


def iter_records(repo_root: str | Path, settings) -> Iterator[tuple[ScanEntry, FileRecord]]:
    """Sequential, in-process scan + parse (streaming)."""
    repo_root = Path(repo_root)
    entries = _scan_entries(repo_root, settings)
    options = executor._options(settings)
    options["indexes"] = _build_indexes(
        repo_root, entries, debug_on=settings.log_level == "DEBUG",
        parse_timeout_micros=options["parse_timeout_micros"],
    )
    options["owners"] = _owners(entries)
    options["nx_projects"] = discover_nx_projects(repo_root, entries)
    for entry in entries:
        record = executor._parse_entry(entry.path, str(repo_root), options)
        if record is not None:
            yield entry, record


#: File languages that are documentation, not code (parsers/markdown): no LOC.
DOC_LANGUAGES: frozenset[str] = frozenset({"markdown", "rst"})


class _ConfigSummary:
    """Aggregates config-file ``metadata`` into ``projectMetaData.configs`` — category
    counts, package managers / build tools, docker info, and dependency totals."""

    def __init__(self) -> None:
        self.by_type: dict[str, int] = {}
        self.package_managers: set[str] = set()
        self.build_tools: set[str] = set()
        self.services: set[str] = set()
        self.ports: set[str] = set()
        self.has_dockerfile = self.has_compose = False
        self.dep_total = self.dep_prod = self.dep_dev = 0
        self.captured_documents = 0
        self.agent_documents = 0  # skill / agent / command / agent_instructions / adr
        self.markdown_capped = 0  # docs past the Markdown count or byte cap: File record only

    def add(self, md: dict) -> None:
        self.by_type[md.get("category", "other")] = self.by_type.get(md.get("category", "other"), 0) + 1
        # A ``format`` marks a data document captured in full (its content serialized, e.g. as
        # TOON) rather than reduced to a summary. Format-neutral: any full-capture parser sets
        # it, so this counts real captured content regardless of the source file type.
        if md.get("format"):
            self.captured_documents += 1
        if md.get("category") == "documentation" and md.get("kind") in AGENT_DOCUMENT_KINDS:
            self.agent_documents += 1
        if md.get("category") == "documentation" and md.get("statementSkipped"):
            self.markdown_capped += 1
        if md.get("packageManager"):
            self.package_managers.add(md["packageManager"])
        if md.get("buildTool"):
            self.build_tools.add(md["buildTool"])
        self.dep_total += md.get("dependencyCount", 0)
        if md.get("kind") == "package.json":
            self.dep_prod += md.get("dependencyCount", 0)
            self.dep_dev += md.get("devDependencyCount", 0)
        if md.get("kind") == "dockerfile":
            self.has_dockerfile = True
            self.ports.update((md.get("dockerInfo") or {}).get("exposedPorts", []))
        if (dc := md.get("dockerCompose")):
            self.has_compose = True
            self.services.update(dc.get("services", []))
            self.ports.update(dc.get("exposedPorts", []))

    def result(self, total: int) -> dict:
        return {
            "totalConfigFiles": total,
            "capturedDataDocuments": self.captured_documents,
            "capturedAgentDocuments": self.agent_documents,
            "markdownCapped": self.markdown_capped,
            "byType": self.by_type,
            "packageManagers": sorted(self.package_managers),
            "buildTools": sorted(self.build_tools),
            "docker": {
                "hasDockerfile": self.has_dockerfile, "hasCompose": self.has_compose,
                "services": sorted(self.services), "exposedPorts": sorted(self.ports),
            },
            "dependencies": {"total": self.dep_total, "production": self.dep_prod,
                             "development": self.dep_dev},
        }


def _markdown_skips(
    entries: list[ScanEntry], cap: int, budget: int = 0, repo_root: Path | None = None
) -> dict[str, str] | None:
    """``{path: reason}`` for the Markdown files whose ``doc`` / ``readme`` / ``changelog``
    statement is dropped: past the first ``cap`` documentation files in path order
    (``max_markdown_statements``), or past the ``budget`` bytes those files add up to in path
    order (``max_markdown_bytes``). Decided from the scan, not from the order parallel workers
    finish in, so the same repo always keeps the same docs. None when neither cap is set."""
    if cap <= 0 and budget <= 0:
        return None
    docs = sorted(e.path for e in entries if e.language in DOC_LANGUAGES)
    out: dict[str, str] = {}
    used = 0
    for i, path in enumerate(docs):
        if 0 < cap <= i:
            out[path] = "max_markdown_statements"
            continue
        if budget > 0:
            used += _doc_cost(repo_root, path)
            if used > budget:
                out[path] = "max_markdown_bytes"
    return out


def _doc_cost(repo_root: Path | None, path: str) -> int:
    """The most statement text a documentation file can produce, from its size on disk: the
    file up to the 64 KB statement cap, nothing over the 512 KB File-only limit."""
    if repo_root is None:
        return 0
    try:
        size = (repo_root / path).stat().st_size
    except OSError:
        return 0
    return 0 if size > MAX_STATEMENT_FILE_BYTES else min(size, MAX_TEXT_BYTES)


def _assemble(
    repo_root: Path,
    records: Iterator[tuple[str, FileRecord]],
    sink,
    *,
    statement_text_limit: int = 0,
    max_statement_parts: int = 0,
    max_markdown_statements: int = 0,
    max_markdown_bytes: int = 0,
    markdown_skips: dict[str, str] | None = None,
    candidates: int | None = None,
    skips: dict[str, int] | None = None,
    debug_on: bool = False,
    progress: Callable[[int, int], None] | None = None,
    summary_out: dict | None = None,
) -> ProjectMetaData:
    """Stream (language, record) pairs to the sink and accumulate projectMetaData.

    Always logs an ``analysis.complete`` summary at INFO (candidate files, parsed, failed,
    skipped + cumulative totals) — so it lands in the log file regardless of console mode.
    A CLI showing a Rich table quiets it on the *console* (see ``logging.quiet_console``),
    not in the file. Per-file ``file.skipped`` WARNINGs are emitted by :func:`run` at scan
    time. If ``summary_out`` is given it's filled with the same numbers. ``progress(done,
    total)`` — if given — is called as records arrive. Under ``--verbose`` a per-file
    ``parse.file.done`` is also logged.
    """
    total_files = total_functions = total_classes = total_loc = config_files = 0
    total_statements = 0
    languages: set[str] = set()
    cfg = _ConfigSummary()
    markdown_docs = markdown_chars = doc_statements = doc_bytes = markdown_capped = 0

    if progress is not None and candidates is not None:
        progress(0, candidates)  # establish the total up front

    for language, record in records:
        # Split any statement whose text exceeds the cap into ordered `#partNofN`
        # records (lossless) so the backend never drops an oversized statement whole.
        record.statements = split_oversized_statements(
            record.statements, statement_text_limit, max_statement_parts
        )
        is_doc = record.language in DOC_LANGUAGES
        if is_doc:
            markdown_docs += 1
            md = record.metadata or {}
            # Only agent documents are always kept; a README or changelog counts against the
            # caps like any doc, so a monorepo of package READMEs stays bounded.
            if record.statements and md.get("kind") not in AGENT_DOCUMENT_KINDS:
                doc_statements += 1
                doc_bytes += sum(len(st.text.encode("utf-8")) for st in record.statements)
                reason: str | None
                if markdown_skips is not None:
                    reason = markdown_skips.get(record.path)
                elif 0 < max_markdown_statements < doc_statements:
                    reason = "max_markdown_statements"
                elif 0 < max_markdown_bytes < doc_bytes:
                    reason = "max_markdown_bytes"
                else:
                    reason = None
                if reason is not None:
                    # Non-agent doc past a cap: keep the File (metadata + outline) only.
                    record.statements = []
                    md["statementSkipped"] = reason
                    markdown_capped += 1
            markdown_chars += sum(len(st.text) for st in record.statements)
        sink.write(record)
        total_files += 1
        total_functions += len(record.functions)
        total_classes += len(record.classes)
        total_statements += len(record.statements)
        if not is_doc:  # documentation is not code: no LOC
            total_loc += record.loc
        if record.type == "config":
            config_files += 1
            cfg.add(record.metadata or {})
        else:
            languages.add(language)  # config is not a programming language
        if progress is not None and candidates is not None:
            progress(total_files, candidates)
        if debug_on:  # gated: no structlog cost unless --verbose
            log.debug(
                "parse.file.done", path=record.path, language=language,
                functions=len(record.functions), classes=len(record.classes),
                statements=len(record.statements),
            )
            for v in validate_contract(record):  # entry-point contract check; output unchanged
                log.debug(
                    "contract.violation", path=record.path, statement=v.statement_id,
                    rule=v.rule, severity=v.severity, detail=v.detail,
                )

    meta = ProjectMetaData(
        repositoryPath=str(repo_root.resolve()),
        repositoryName=repo_root.resolve().name,
        analyzedLanguages=sorted(languages),
        totalFiles=total_files,
        totalFunctions=total_functions,
        totalClasses=total_classes,
        totalLinesOfCode=total_loc,
        configs=cfg.result(config_files),
        generatedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        toolVersion=__version__,
    )
    sink.finalize(meta)

    skips = skips or {}
    parsed = total_files
    candidate_n = candidates if candidates is not None else parsed  # source files to parse
    failed = max(0, candidate_n - parsed)                            # candidates that errored
    skipped_total = sum(skips.values())                             # non-candidates dropped in scan
    scanned = candidate_n + skipped_total                           # = parsed + failed + skipped
    if summary_out is not None:
        summary_out.update(
            scanned=scanned, parsed=parsed, failed=failed,
            skipped=skipped_total, skips=dict(skips), statements=total_statements,
        )
    log.info(
        "analysis.complete",
        scanned=scanned,                   # files walked = parsed + failed + skipped
        parsed=parsed,                     # records produced
        failed=failed,                     # candidates that errored out
        skipped=skipped_total or None,     # ignored + unsupported + oversized
        skips=skips or None,
        functions=total_functions, classes=total_classes,
        statements=total_statements, loc=total_loc,
        languages=sorted(languages),
        markdown_docs=markdown_docs or None,
        markdown_chars=markdown_chars or None,
        markdown_capped=markdown_capped or None,
    )
    return meta


def run(
    repo_root: str | Path, settings, sink,
    *,
    progress: Callable[[int, int], None] | None = None,
    summary_out: dict | None = None,
) -> ProjectMetaData:
    """Full analysis to a sink (parallel) → assembled projectMetaData.

    ``progress(done, total)`` — if given — receives live counts as files complete.
    ``summary_out`` — see :func:`_assemble`.
    """
    repo_root = Path(repo_root)
    debug_on = settings.log_level == "DEBUG"
    report = SkipReport()
    jobs = settings.jobs if settings.jobs and settings.jobs > 0 else (os.cpu_count() or 1)
    entries = _scan_entries(repo_root, settings, report, jobs)
    # One row per skipped file (with its reason) to the file-only detail log — individually
    # recorded on disk, without polluting the console summary.
    for skipped in report.files:
        detail_log.warning(
            "file.skipped",
            path=skipped["path"],
            reason=skipped["reason"],
            **({"size": skipped["size"]} if "size" in skipped else {}),
        )
    indexes = _build_indexes(
        repo_root, entries, jobs, debug_on=debug_on,
        parse_timeout_micros=int(settings.parse_timeout * 1_000_000),
    )
    records = executor.parse_entries(
        entries, repo_root, settings, indexes, _owners(entries),
        discover_nx_projects(repo_root, entries),
    )
    meta = _assemble(
        repo_root, records, sink, statement_text_limit=settings.statement_text_limit,
        max_statement_parts=settings.max_statement_parts,
        max_markdown_statements=settings.max_markdown_statements,
        max_markdown_bytes=settings.max_markdown_bytes,
        markdown_skips=_markdown_skips(
            entries, settings.max_markdown_statements, settings.max_markdown_bytes, repo_root
        ),
        candidates=len(entries), skips=report.counts,
        debug_on=debug_on, progress=progress, summary_out=summary_out,
    )
    if summary_out is not None:  # full detail for the CLI console summary + sidecar report
        summary_out["skip_report"] = report
    return meta


def run_inprocess(repo_root: str | Path, settings, sink) -> ProjectMetaData:
    """Full analysis to a sink, **sequential and in-process** (no spawn pool) — the
    server `/api/analyze` path, where file lists are small and per-request pool
    startup would dominate."""
    repo_root = Path(repo_root)
    debug_on = settings.log_level == "DEBUG"
    report = SkipReport()
    entries = _scan_entries(repo_root, settings, report)
    options = executor._options(settings)
    options["indexes"] = _build_indexes(
        repo_root, entries, debug_on=debug_on, parse_timeout_micros=options["parse_timeout_micros"]
    )
    options["owners"] = _owners(entries)
    options["nx_projects"] = discover_nx_projects(repo_root, entries)

    def gen() -> Iterator[tuple[str, FileRecord]]:
        for entry in entries:
            record = executor._parse_entry(entry.path, str(repo_root), options)
            if record is not None:
                yield record.language, record

    return _assemble(
        repo_root, gen(), sink, statement_text_limit=settings.statement_text_limit,
        max_statement_parts=settings.max_statement_parts,
        max_markdown_statements=settings.max_markdown_statements,
        max_markdown_bytes=settings.max_markdown_bytes,
        markdown_skips=_markdown_skips(
            entries, settings.max_markdown_statements, settings.max_markdown_bytes, repo_root
        ),
        candidates=len(entries), skips=report.counts, debug_on=debug_on,
    )
