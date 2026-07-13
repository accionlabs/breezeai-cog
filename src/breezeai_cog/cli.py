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
    jobs: Optional[int] = typer.Option(None, "--jobs", help="Worker processes (default: CPU count)."),
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

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=Console(stderr=True),
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
    if render_table:
        _print_summary_table(m, stats, result.out_path)
    else:
        typer.echo(
            f"{m.totalFiles} files, {m.totalFunctions} functions, {m.totalClasses} classes "
            f"({', '.join(m.analyzedLanguages) or 'none'}) -> {result.out_path}"
        )

    if settings.upload:
        from .errors import UploadError
        from .services import upload_ontology

        assert result.out_path is not None  # CLI always owns a FileSink (out_path set)
        typer.echo(f"Uploading {result.out_path.name} to {settings.baseurl} ...")
        try:
            upload_ontology(settings, result.out_path, repository_name=m.repositoryName)
        except UploadError as exc:
            typer.secho(f"upload failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
        typer.secho("Upload complete.", fg=typer.colors.GREEN)


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
