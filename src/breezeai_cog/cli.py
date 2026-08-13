"""Command-line interface (Typer). A thin client over the services layer.

Commands: ``repo-to-json-tree`` (analyze a local repo), ``capabilities``, ``version``,
``serve`` (the FastAPI service).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import typer

from ._version import __version__
from .config import Settings
from .core.skips import SkipReport
from .logging import setup_logging
from .schemas import ProjectMetaData
from .services import AnalysisResult, AnalysisService
from .utils.paths import cog_dir

if TYPE_CHECKING:
    from .services import UploadState, UploadTask
    from .services.batch_upload import TrackerSnapshot

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
        help="Output directory for the export only (default: <repo>/.cog). "
             "File: <repo>-project-analysis.ndjson.gz. The skip report and logs always go to <repo>/.cog.",
    ),
    language: Optional[list[str]] = typer.Option(None, "--language", help="Restrict to languages (repeatable)."),
    capture_statements: bool = typer.Option(False, "--capture-statements", help="Capture in-body statements."),
    batch: bool = typer.Option(
        False, "--batch",
        help="Treat --repo as a workspace folder: analyze each immediate subdirectory as its own project "
             "(one .ndjson.gz per subdir). Dot-directories and loose files are skipped.",
    ),
    repo_list: Optional[Path] = typer.Option(
        None, "--repo-list", exists=True, dir_okay=False,
        help="With --batch: a file of immediate-subdirectory names (one per line; '#' comments and "
             "blank lines ignored) to restrict the run to. Default: every subdirectory.",
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
    upload_timeout: Optional[float] = typer.Option(
        None, "--upload-timeout", min=0,
        help="Per-repo upload timeout in seconds — caps both the upload request and the wait "
             "for the backend to finish processing; the repo fails past this "
             "(default 900 = 15 min; env: BREEZEAI_COG_UPLOAD_TIMEOUT).",
    ),
    parallel_uploads: Optional[int] = typer.Option(
        None, "--parallel-uploads", min=1,
        help="Concurrent uploads in --batch mode (default 1; env: BREEZEAI_COG_UPLOAD_PARALLELISM).",
    ),
    upload_max_retries: Optional[int] = typer.Option(
        None, "--upload-max-retries", min=0,
        help="Retries after a failed upload; total attempts = retries + 1 "
             "(default 1; env: BREEZEAI_COG_UPLOAD_MAX_RETRIES).",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="With --batch --upload: ignore any saved resume state and re-upload every "
             "selected project from scratch.",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Verbose (DEBUG) logging."),
) -> None:
    """Analyze a repository to a gzipped NDJSON ontology (optionally uploading it)."""
    if repo_list is not None and not batch:
        typer.secho("error: --repo-list requires --batch", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

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
    if upload_timeout is not None:
        overrides["upload_timeout"] = upload_timeout
    if parallel_uploads is not None:
        overrides["upload_parallelism"] = parallel_uploads
    if upload_max_retries is not None:
        overrides["upload_max_retries"] = upload_max_retries
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
    # Default logs into the repo's .cog/logs (in batch, the workspace's — one log stream per
    # run). An explicit BREEZEAI_COG_LOG_LOCATION resolves to a non-None value and wins.
    if settings.log_location is None:
        settings = settings.model_copy(update={"log_location": cog_dir(repo) / "logs"})
    setup_logging(settings)
    service = AnalysisService(settings)

    # A live bar + final table for humans on an interactive terminal (not under --verbose,
    # where per-file logs already show progress). Piped/CI output keeps the structured
    # `analysis.complete` log line + a plain one-liner. Server/library paths are untouched.
    import sys

    show_bar = not verbose and sys.stderr.isatty()
    render_table = not verbose and sys.stdout.isatty()

    # When a Rich summary table is shown, keep the structured `analysis.complete` /
    # `files.skipped` lines flowing to the log file but drop them from the terminal (the
    # table + skip block present the same info). Piped/CI runs keep the console lines.
    if render_table:
        from .logging import quiet_console

        quiet_console()

    if batch:
        _run_batch(
            service, settings, repo, repo_list,
            force=force,
            show_bar=show_bar, render_table=render_table, live_display=render_table,
        )
    else:
        result = _analyze_and_report(service, settings, repo, show_bar=show_bar, render_table=render_table)
        if settings.upload and result.written:
            from .services import UploadTask

            assert result.out_path is not None  # CLI always owns a FileSink (out_path set)
            tasks = [UploadTask(result.project_meta.repositoryName, result.out_path)]
            failed = _run_upload_phase(tasks, settings, state=None, live_display=render_table)
            if failed:
                raise typer.Exit(1)


def _select_batch_repos(workspace: Path, repo_list: Path | None) -> list[Path]:
    """Immediate subdirectories of ``workspace`` (dot-dirs and loose files skipped), optionally
    restricted to the names listed in ``repo_list``. Errors out if the selection is empty or a
    listed name has no matching subdirectory."""
    subdirs = sorted(p for p in workspace.iterdir() if p.is_dir() and not p.name.startswith("."))
    if repo_list is None:
        if not subdirs:
            typer.secho(f"error: no subdirectories to analyze in {workspace}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        return subdirs

    names = [
        line.strip()
        for line in repo_list.read_text("utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    by_name = {p.name: p for p in subdirs}
    missing = [n for n in names if n not in by_name]
    if missing:
        typer.secho(
            f"error: --repo-list names have no matching subdirectory in {workspace}: "
            f"{', '.join(missing)}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)
    # De-dupe while preserving list order.
    selected = [by_name[n] for n in dict.fromkeys(names)]
    if not selected:
        typer.secho(f"error: --repo-list {repo_list} selected no projects", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    return selected


def _run_batch(
    service: AnalysisService,
    settings: Settings,
    workspace: Path,
    repo_list: Path | None,
    *,
    force: bool,
    show_bar: bool,
    render_table: bool,
    live_display: bool,
) -> None:
    """Two-phase batch: analyze every selected project, then (if configured) upload them all
    with a resumable, optionally-parallel upload phase."""
    from .services import UploadState, UploadTask

    repos = _select_batch_repos(workspace, repo_list)

    # Resume: skip repos already uploaded in a previous (interrupted) run. State lives in the
    # workspace's .cog and is keyed by repository name (== subdirectory name). --force discards
    # any saved state so every selected project is uploaded again.
    state = UploadState.load(workspace) if settings.upload else None
    if state is not None and force:
        state.clear()
        state.completed.clear()
        typer.secho("--force: ignoring saved resume state; re-uploading all selected projects.",
                    fg=typer.colors.YELLOW)
    if state is not None and state.completed:
        already = [r for r in repos if state.is_done(r.name)]
        repos = [r for r in repos if not state.is_done(r.name)]
        if already:
            typer.secho(
                f"Resuming: {len(already)} project(s) already uploaded — "
                f"skipping {', '.join(sorted(r.name for r in already))}.",
                fg=typer.colors.CYAN,
            )
        if not repos:
            typer.secho("All selected projects already uploaded.", fg=typer.colors.GREEN)
            state.clear()
            return

    # ── Phase 1: analyze ──────────────────────────────────────────────────────
    typer.echo(f"Batch: analyzing {len(repos)} project(s) in {workspace} ...")
    tasks: list[UploadTask] = []
    for sub in repos:
        typer.echo(f"\n[{sub.name}]")
        result = _analyze_and_report(service, settings, sub, show_bar=show_bar, render_table=render_table)
        if settings.upload and result.written and result.out_path is not None:
            tasks.append(UploadTask(result.project_meta.repositoryName, result.out_path))

    # ── Phase 2: upload ───────────────────────────────────────────────────────
    if not settings.upload:
        return
    if not tasks:
        typer.secho("No uploadable artifacts produced — nothing to upload.", fg=typer.colors.YELLOW)
        return
    failed = _run_upload_phase(tasks, settings, state=state, live_display=live_display)
    if failed:
        typer.secho(
            f"Upload failed for {len(failed)} project(s): {', '.join(sorted(failed))} "
            f"(rerun the same command to retry only these).",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)
    if state is not None:
        state.clear()
    typer.secho(f"All {len(tasks)} upload(s) complete.", fg=typer.colors.GREEN)


def _display_path(path: Path, repo: Path) -> str:
    """A readable path for the summary: repo-relative (POSIX) when it lives under the repo
    (the common ``.cog/…`` case), otherwise the absolute path."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _print_artifacts(
    repo: Path, settings: Settings, *, export: Path | None, skip_report: Path | None
) -> None:
    """Compact footer listing where this run's artifacts were written. Rows are shown
    repo-relative (the common ``.cog/…`` case); the export uses its absolute path when the
    user chose an explicit ``--out``. Only the artifacts that were actually produced appear."""
    rows: list[tuple[str, str]] = []
    if export is not None:
        rows.append(("Export", str(export.resolve()) if settings.out else _display_path(export, repo)))
    if skip_report is not None:
        rows.append(("Skip report", _display_path(skip_report, repo)))
    if settings.log_to_file and settings.log_location is not None:
        rows.append(("Logs", _display_path(Path(settings.log_location), repo)))
    if not rows:
        return
    width = max(len(label) for label, _ in rows)
    typer.secho("Artifacts", fg=typer.colors.CYAN, bold=True)
    for label, value in rows:
        typer.echo(f"  {label:<{width}}  {value}")


def _analyze_and_report(
    service: AnalysisService,
    settings: Settings,
    repo: Path,
    *,
    show_bar: bool,
    render_table: bool,
) -> AnalysisResult:
    """Analyze one repository and render its summary (no upload — the caller drives that).

    Returns the :class:`AnalysisResult`; ``result.written`` is False when the parser
    produced nothing and no ``.ndjson.gz`` was emitted.
    """
    _warn_if_cog_not_gitignored(repo)
    stats: dict[str, Any] = {}

    def analyze(progress: Callable[[int, int], None] | None) -> AnalysisResult:
        return service.analyze_repo(repo, progress=progress, summary_out=stats)

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
        skip_report_path = _report_skips(report, cog_dir(repo), m.repositoryName)
        _print_artifacts(repo, settings, export=None, skip_report=skip_report_path)
        return result
    if render_table:
        _print_summary_table(m, stats)
    else:
        typer.echo(
            f"{m.totalFiles} files, {m.totalFunctions} functions, {m.totalClasses} classes "
            f"({', '.join(m.analyzedLanguages) or 'none'})"
        )
    skip_report_path = _report_skips(report, cog_dir(repo), m.repositoryName)
    _print_artifacts(repo, settings, export=result.out_path, skip_report=skip_report_path)
    return result


def _fmt_mmss(seconds: float) -> str:
    """``MM:SS`` (zero-padded) for a single repo's elapsed time."""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _fmt_hms(seconds: float) -> str:
    """``HH:MM:SS`` (zero-padded) for the cumulative phase time."""
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _upload_active_lines(snap: "TrackerSnapshot", width: int) -> list[Any]:
    """One indented ``  <repo> [MM:SS] . attempt <n>`` line per in-flight upload (truncated to
    ``width`` so it never wraps — a wrapping line would corrupt the Live redraw)."""
    from rich.text import Text

    lines: list[Any] = []
    for a in snap.active:
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(f"  {a.name} ", style="white")
        line.append(f"[{_fmt_mmss(a.elapsed)}]", style="cyan")
        line.append(" . ", style="dim")
        line.append(f"attempt {a.attempt}", style="cyan")
        line.truncate(max(width, 1), overflow="ellipsis")
        lines.append(line)
    return lines


def _run_upload_phase(
    tasks: "list[UploadTask]",
    settings: Settings,
    *,
    state: "UploadState | None",
    live_display: bool,
) -> list[str]:
    """Upload ``tasks`` (single-repo or batch). On an interactive terminal this shows one overall
    progress bar with a line per concurrently-uploading repo::

        Uploading  ━━━━━━━━━╺━━━━━━━━  1/12 [01:12:59]
          repo-1 [05:12] . attempt 1
          repo-2 [08:11] . attempt 2

    Raw backend responses go to the log file only. Returns the list of repo names that failed."""
    import threading

    from .services import UploadTracker, run_batch_uploads

    typer.echo(
        f"\nUploading {len(tasks)} project(s) to {settings.baseurl} "
        f"({settings.upload_parallelism} at a time) ..."
    )
    tracker = UploadTracker(total=len(tasks))
    holder: dict[str, list[str]] = {}

    def _drive() -> None:
        holder["failed"] = run_batch_uploads(tasks, settings, tracker, state=state)

    worker = threading.Thread(target=_drive, name="upload-phase")

    if not live_display:
        # Piped / CI / --verbose: no live bar; the file log carries the raw responses.
        worker.start()
        worker.join()
        failed = holder.get("failed", [])
        snap = tracker.snapshot()
        typer.echo(f"Uploaded {snap.completed}/{snap.total}." + (f" Failed: {', '.join(sorted(failed))}." if failed else ""))
        _print_upload_errors(tracker.errors)
        return failed

    import time as _time

    from rich.console import Console, Group
    from rich.live import Live
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

    from .logging import route_logs_through_console

    console = Console(stderr=True)
    # Overall bar on top; one indented repo line per in-flight upload below (grows/shrinks with
    # concurrency). Each line is truncated to the console width, so the render height is always
    # 1 + len(active) fixed lines — no wrapping to corrupt the Live redraw.
    progress = Progress(
        TextColumn("Uploading"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[{task.fields[total_el]}]"),
        TextColumn("{task.fields[failed_txt]}", style="red"),
        console=console,
    )
    bar = progress.add_task("", total=len(tasks), total_el="00:00:00", failed_txt="")

    def _renderable() -> Any:
        snap = tracker.snapshot()
        # ``\[`` escapes the literal bracket so Rich markup renders "[N failed]" (not a tag).
        failed_txt = f"\\[{len(snap.failed)} failed]" if snap.failed else ""
        progress.update(
            bar,
            completed=snap.completed,
            total_el=_fmt_hms(snap.total_elapsed),
            failed_txt=failed_txt,
        )
        return Group(progress, *_upload_active_lines(snap, console.width))

    # Route logs through the same console so any WARNING/ERROR renders above the display (raw
    # responses are INFO and stay in the log file — the console handler is already quieted).
    with route_logs_through_console(console), Live(
        _renderable(), console=console, refresh_per_second=4, transient=False
    ) as live:
        worker.start()
        while worker.is_alive():
            live.update(_renderable())
            _time.sleep(0.25)
        live.update(_renderable())  # final frame
    worker.join()
    _print_upload_errors(tracker.errors)
    return holder.get("failed", [])


def _print_upload_errors(errors: dict[str, str]) -> None:
    """Surface each failed upload's reason on the console (the full response is in the log file)."""
    if not errors:
        return
    typer.secho("Upload errors:", fg=typer.colors.RED, bold=True, err=True)
    for name, msg in sorted(errors.items()):
        typer.secho(f"  {name}: {msg}", fg=typer.colors.RED, err=True)


def _print_summary_table(meta: ProjectMetaData, stats: dict[str, Any]) -> None:
    """Render the run summary as a readable table (interactive terminal). Artifact paths are
    printed separately by :func:`_print_artifacts`."""
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

    Console().print(table)


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _warn_if_cog_not_gitignored(repo: Path) -> None:
    """Nudge the user to git-ignore ``.cog`` when the repo has a ``.gitignore`` that does not
    already ignore it. ``.cog`` holds generated artifacts (export / skip report / logs) that
    normally shouldn't be committed. We never edit the user's ``.gitignore`` — only warn."""
    gitignore = repo.resolve() / ".gitignore"
    if not gitignore.is_file():
        return
    from .core.ignore import compile_spec

    try:
        spec = compile_spec(gitignore.read_text("utf-8", errors="replace").splitlines())
    except OSError:
        return
    if spec.match_file(".cog/"):  # already ignored (matches .cog, .cog/, /.cog, …)
        return
    typer.secho(
        f"warning: '.cog/' is not ignored by {gitignore}. breezeai-cog writes its artifacts "
        "(export, skip report, logs) there — add '.cog/' to your .gitignore to keep them out of git.",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _report_skips(report: SkipReport | None, out_dir: Path, repo_name: str) -> Path | None:
    """Print a grouped skip summary and write the full ``<repo>-skipped-report.json`` sidecar
    into ``out_dir`` (the repo's ``.cog`` dir). Returns the sidecar path when one was written
    (so the caller can list it in the artifacts footer), else ``None``.

    Covers the files/folders the scanner dropped and why (unsupported extension, ignore
    rule, or oversized). The console view is truncated; the sidecar holds the full list.
    """
    if report is None:
        return None
    if report.is_empty:
        typer.echo("No files or folders skipped.")
        return None

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

    sidecar = out_dir / f"{repo_name}-skipped-report.json"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(report.to_dict(), indent=2))
    except OSError as exc:
        typer.secho(f"  (could not write skip report: {exc})", fg=typer.colors.YELLOW, err=True)
        return None
    return sidecar


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
