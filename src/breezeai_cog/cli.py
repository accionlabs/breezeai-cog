"""Command-line interface (Typer). A thin client over the services layer.

Commands: ``repo-to-json-tree`` (analyze a local repo), ``capabilities``, ``version``,
``serve`` (the FastAPI service).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ._version import __version__
from .config import Settings
from .core.skips import SkipReport
from .logging import setup_logging
from .schemas import ProjectMetaData
from .services import AnalysisResult, AnalysisService

app = typer.Typer(
    name="breezeai-cog",
    help="Parse source repositories into the capture NDJSON contract.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("repo-to-json-tree")
def repo_to_json_tree(
    repo: Path = typer.Option(..., "--repo", exists=True, file_okay=False, help="Repository directory."),
    out: Optional[Path] = typer.Option(
        None, "--out", file_okay=False,
        help="Output directory (default: the repo's parent). File: <repo>-project-analysis.ndjson.gz.",
    ),
    language: Optional[list[str]] = typer.Option(None, "--language", help="Restrict to languages (repeatable)."),
    capture_statements: bool = typer.Option(False, "--capture-statements", help="Capture in-body statements."),
    batch: bool = typer.Option(
        False, "--batch",
        help="Treat --repo as a workspace folder: analyze each immediate subdirectory as its own project "
             "(one .ndjson.gz per subdir). Dot-directories and loose files are skipped.",
    ),
    jobs: Optional[int] = typer.Option(None, "--jobs", help="Worker processes (default: CPU count)."),
    max_concat_depth: Optional[int] = typer.Option(
        None, "--max-concat-depth", min=1,
        help="Max `+` nesting folded into an endpoint before bailing to null (default 100; "
             "env: BREEZEAI_COG_MAX_CONCAT_DEPTH). Guards generated HTML/JS string builders.",
    ),
    upload: bool = typer.Option(
        False, "--upload", help="Upload the result to the Breeze backend (needs --baseurl, --uuid, --user-api-key)."
    ),
    baseurl: Optional[str] = typer.Option(
        None, "--baseurl", help="Breeze backend base URL (with --upload; env: BREEZE_API_URL)."
    ),
    uuid: Optional[str] = typer.Option(
        None, "--uuid", help="Project UUID to upload into (with --upload)."
    ),
    user_api_key: Optional[str] = typer.Option(
        None, "--user-api-key", help="Backend API key, sent as `api-key` (with --upload; env: API_KEY).",
    ),
    llm_platform: Optional[str] = typer.Option(
        None, "--llm-platform",
        help="Embedding platform sent as `?llmPlatform=` on upload (default AWSBEDROCK, matches the "
             "web UI; env: BREEZEAI_COG_LLM_PLATFORM). Must be configured on the deployment or the "
             "backend leaves the ontology stuck in 'creating_embeddings'.",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Verbose (DEBUG) logging."),
) -> None:
    """Analyze a repository to a gzipped NDJSON ontology (optionally uploading it)."""
    # Only forward upload flags that were actually supplied so env / .env can fill the
    # rest (init kwargs outrank env in pydantic-settings — passing None would clobber it).
    overrides: dict[str, Any] = {}
    if upload:
        overrides["upload"] = True
    if baseurl is not None:
        overrides["baseurl"] = baseurl
    if uuid is not None:
        overrides["uuid"] = uuid
    if user_api_key is not None:
        overrides["user_api_key"] = user_api_key
    if llm_platform is not None:
        overrides["llm_platform"] = llm_platform
    if max_concat_depth is not None:
        overrides["max_concat_depth"] = max_concat_depth

    from pydantic import ValidationError

    try:
        settings = Settings(
            repo=repo,
            out=out,
            languages=language or None,
            capture_statements=capture_statements,
            jobs=jobs,
            log_level="DEBUG" if verbose else "INFO",
            **overrides,
        )
    except ValidationError as exc:
        for err in exc.errors():
            typer.secho(f"error: {err['msg']}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    setup_logging(settings)
    service = AnalysisService(settings)

    # A live bar + final table for humans on an interactive terminal (not under --verbose,
    # where per-file logs already show progress). Piped/CI output keeps the structured
    # `analysis.complete` log line + a plain one-liner. Server/library paths are untouched.
    import sys

    show_bar = not verbose and sys.stderr.isatty()
    render_table = not verbose and sys.stdout.isatty()

    if batch:
        # Immediate subdirectories only; skip dot-directories. Loose files are ignored
        # because we iterate directories, never the workspace itself.
        repos = sorted(p for p in repo.iterdir() if p.is_dir() and not p.name.startswith("."))
        if not repos:
            typer.secho(f"error: no subdirectories to analyze in {repo}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        typer.echo(f"Batch: analyzing {len(repos)} project(s) in {repo} ...")
        failed: list[str] = []
        for sub in repos:
            typer.echo(f"\n[{sub.name}]")
            if not _analyze_and_report(service, settings, sub, show_bar=show_bar, render_table=render_table):
                failed.append(sub.name)
        if failed:
            typer.secho(
                f"Upload failed for {len(failed)} project(s): {', '.join(failed)}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)
    else:
        if not _analyze_and_report(service, settings, repo, show_bar=show_bar, render_table=render_table):
            raise typer.Exit(1)


def _analyze_and_report(
    service: AnalysisService,
    settings: Settings,
    repo: Path,
    *,
    show_bar: bool,
    render_table: bool,
) -> bool:
    """Analyze one repository, render its summary, and (if configured) upload it.

    Returns True on success; False if upload was attempted and failed (the
    analysis itself always completes first, leaving the .ndjson.gz on disk).
    """
    stats: dict[str, Any] = {}

    def analyze(progress: Callable[[int, int], None] | None) -> AnalysisResult:
        return service.analyze_repo(
            repo, progress=progress, summary_out=stats, log_summary=not render_table,
        )

    if show_bar:
        from rich.console import Console
        from rich.progress import (
            BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn,
        )

        from .logging import route_logs_through_console

        console = Console(stderr=True)  # shared by the bar AND the log routing below
        # Route logs through the same console so warnings render above the pinned bar (own
        # lines, coloured) instead of shredding it. Entered BEFORE analyze() so the worker
        # QueueListener funnels through it too.
        with route_logs_through_console(console), Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,            # clear the bar when done; the summary remains
            refresh_per_second=10,     # throttled redraw — cheap regardless of file count
        ) as prog:
            task = prog.add_task("Analyzing", total=None)

            def _on_progress(done: int, total: int) -> None:
                prog.update(task, completed=done, total=total)
                # Tear the bar down as soon as parsing finishes — before the pipeline
                # logs its summary line — so that log starts on a clean line.
                if total and done >= total:
                    prog.stop()

            result = analyze(_on_progress)
    else:
        result = analyze(None)

    m = result.project_meta
    report = stats.get("skip_report")
    if not result.written:
        name = result.out_path.name if result.out_path else "output"
        typer.secho(
            f"No parseable source files — skipped {name} (no ndjson written).",
            fg=typer.colors.YELLOW,
        )
        _report_skips(report, result.out_path, m.repositoryName)
        return True
    if render_table:
        _print_summary_table(m, stats, result.out_path)
    else:
        typer.echo(
            f"{m.totalFiles} files, {m.totalFunctions} functions, {m.totalClasses} classes "
            f"({', '.join(m.analyzedLanguages) or 'none'}) -> {result.out_path}"
        )
    _report_skips(report, result.out_path, m.repositoryName)

    if settings.upload:
        from .errors import UploadError
        from .services import extract_ontology_id, poll_ontology_status, upload_ontology

        assert result.out_path is not None  # CLI always owns a FileSink (out_path set)
        typer.echo(f"Uploading {result.out_path.name} to {settings.baseurl} ...")
        try:
            upload_resp = upload_ontology(settings, result.out_path, repository_name=m.repositoryName)
        except UploadError as exc:
            typer.secho(f"upload failed: {exc}", fg=typer.colors.RED, err=True)
            return False
        typer.secho("Upload complete.", fg=typer.colors.GREEN)
        if upload_resp:
            typer.echo(f"  Upload response: {json.dumps(upload_resp)}")

        ontology_id = extract_ontology_id(upload_resp)
        if ontology_id:
            typer.echo(f"Waiting for backend to process ontology {ontology_id} ...")
            try:
                final_status = poll_ontology_status(
                    settings,
                    ontology_id,
                    on_response=lambda p: typer.echo(f"  Poll response: {json.dumps(p)}"),
                    on_waiting=lambda s: typer.echo(
                        f"  status: {s or 'pending'} — rechecking in 60s ..."
                    ),
                )
            except UploadError as exc:
                typer.secho(f"status poll failed: {exc}", fg=typer.colors.YELLOW, err=True)
            else:
                color = typer.colors.GREEN if final_status == "active" else typer.colors.YELLOW
                typer.secho(f"Processing status: {final_status}.", fg=color)
        else:
            typer.secho(
                "  (upload response missing '_id'; skipping status check)",
                fg=typer.colors.YELLOW,
            )

    return True


def _print_summary_table(meta: ProjectMetaData, stats: dict[str, Any], out_path: Path | None) -> None:
    """Render the run summary as a readable table (interactive terminal)."""
    from rich import box
    from rich.console import Console
    from rich.table import Table

    skips = stats.get("skips") or {}
    skipped = stats.get("skipped", 0)
    skip_detail = ", ".join(f"{k} {v:,}" for k, v in sorted(skips.items()))

    table = Table(title="Analysis summary", title_style="bold cyan", title_justify="left",
                  show_header=False, box=box.ROUNDED)
    table.add_column(style="cyan", justify="right", no_wrap=True)
    table.add_column(style="white")

    # scanned = parsed + failed + skipped (these reconcile)
    table.add_row("Files scanned", f"{stats.get('scanned', meta.totalFiles):,}")
    table.add_row("  parsed", f"{stats.get('parsed', meta.totalFiles):,}")
    if stats.get("failed"):
        table.add_row("  failed", f"[red]{stats['failed']:,}[/red]")
    if skipped:
        table.add_row("  skipped", f"{skipped:,}" + (f"  ([dim]{skip_detail}[/dim])" if skip_detail else ""))
    table.add_section()
    table.add_row("Functions", f"{meta.totalFunctions:,}")
    table.add_row("Classes", f"{meta.totalClasses:,}")
    table.add_row("Statements", f"{stats.get('statements', 0):,}")
    table.add_row("Lines of code", f"{meta.totalLinesOfCode:,}")
    table.add_section()
    table.add_row("Languages", ", ".join(meta.analyzedLanguages) or "none")
    table.add_row("Output", str(out_path))

    Console().print(table)


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _report_skips(report: SkipReport | None, out_path: Path | None, repo_name: str) -> None:
    """Print a grouped skip summary and write the full ``<repo>-skipped-report.json`` sidecar.

    Covers the files/folders the scanner dropped and why (unsupported extension, ignore
    rule, or oversized). The console view is truncated; the sidecar holds the full list.
    """
    if report is None:
        return
    if report.is_empty:
        typer.echo("No files or folders skipped.")
        return

    typer.secho(
        f"Skipped {report.total_files:,} file(s), {len(report.dirs):,} folder(s):",
        fg=typer.colors.CYAN,
    )
    for reason in ("unsupported", "ignored", "oversized"):
        count = report.counts.get(reason, 0)
        if not count:
            continue
        extra = ""
        if reason == "unsupported":
            top = ", ".join(f"{ext} {cnt}" for ext, cnt in report.top_extensions(6))
            more = " ..." if len(report.extensions) > 6 else ""
            extra = f" — {top}{more}" if top else ""
        elif reason == "oversized":
            big = [f for f in report.files if f["reason"] == "oversized"][:3]
            samples = ", ".join(f"{f['path']} ({_human_size(f.get('size', 0))})" for f in big)
            extra = f" — {samples}" if samples else ""
        typer.echo(f"  {reason:<12}({count:,}){extra}")

    if report.dirs:
        shown = ", ".join(sorted(report.dirs)[:6])
        more = " ..." if len(report.dirs) > 6 else ""
        typer.echo(f"  {'folders':<12}({len(report.dirs):,}) — {shown}{more}")

    if out_path is not None:
        sidecar = out_path.with_name(f"{repo_name}-skipped-report.json")
        try:
            sidecar.write_text(json.dumps(report.to_dict(), indent=2))
            typer.secho(f"  Wrote {sidecar.name}", fg=typer.colors.GREEN)
        except OSError as exc:
            typer.secho(f"  (could not write skip report: {exc})", fg=typer.colors.YELLOW, err=True)


@app.command()
def capabilities() -> None:
    """Print supported languages / frameworks / statement types as JSON."""
    from .core.registry import capabilities as _caps
    from .core.registry import discover_builtin

    discover_builtin()
    typer.echo(json.dumps(_caps(), indent=2))


@app.command()
def serve(
    port: Optional[int] = typer.Option(None, "--port", help="Port (default: 3000 / $PORT / settings)."),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host."),
) -> None:
    """Start the FastAPI service (/health, /api/analyze[-diff|-sql|-es])."""
    import os

    import uvicorn

    from .server.app import create_app

    settings = Settings(port=port) if port is not None else Settings()
    setup_logging(settings)
    bind_port = port or int(os.environ.get("PORT", settings.port))
    uvicorn.run(create_app(settings), host=host, port=bind_port)


@app.command()
def schema(
    out: Optional[Path] = typer.Option(None, "--out", help="Write to this file instead of stdout."),
) -> None:
    """Generate the capture JSON Schema from the Pydantic models (the source of truth)."""
    from .schemas import export_json_schema, write_json_schema

    if out is not None:
        write_json_schema(str(out))
        typer.echo(f"wrote {out}")
    else:
        typer.echo(json.dumps(export_json_schema(), indent=2))


@app.command()
def version() -> None:
    """Print the tool version."""
    typer.echo(__version__)


def main() -> None:
    app()


if __name__ == "__main__":  # python -m breezeai_cog.cli
    main()
