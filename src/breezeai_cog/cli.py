"""Command-line interface (Typer). A thin client over the services layer (§7).

Commands: ``repo-to-json-tree`` (analyze a local repo), ``capabilities``, ``version``,
``serve`` (the FastAPI service, §10).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from ._version import __version__
from .config import Settings
from .logging import setup_logging
from .services import AnalysisService

app = typer.Typer(
    name="breezeai-cog",
    help="Parse source repositories into the capture NDJSON contract (Part C).",
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
    verbose: bool = typer.Option(False, "--verbose", help="Verbose (DEBUG) logging."),
) -> None:
    """Analyze a repository to a gzipped NDJSON ontology."""
    settings = Settings(
        repo=repo,
        out=out,
        languages=language or None,
        capture_statements=capture_statements,
        jobs=jobs,
        log_level="DEBUG" if verbose else "INFO",
    )
    setup_logging(settings)
    result = AnalysisService(settings).analyze_repo(repo)
    m = result.project_meta
    typer.echo(
        f"{m.totalFiles} files, {m.totalFunctions} functions, {m.totalClasses} classes "
        f"({', '.join(m.analyzedLanguages) or 'none'}) -> {result.out_path}"
    )


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
def version() -> None:
    """Print the tool version."""
    typer.echo(__version__)


def main() -> None:
    app()


if __name__ == "__main__":  # python -m breezeai_cog.cli
    main()
