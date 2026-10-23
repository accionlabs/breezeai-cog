"""FastAPI app factory. Maps both manual ``ApiError`` and pydantic
``RequestValidationError`` to the existing ``{"error": "<message>"}`` shape — and,
critically, validation failures return **400** (not FastAPI's default 422).

Every error response is also logged (``request.failed``) so the reason survives in the
log file — uvicorn's access line only records the status code."""

from __future__ import annotations

import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .._version import __version__
from ..config import Settings
from ..logging import get_logger
from .deps import ServerDeps, default_deps
from .errors import ApiError
from .git import _scrub
from .routes import router

log = get_logger("breezeai_cog.server")


def _scrubbed_traceback(exc: BaseException | None) -> str | None:
    # Chained causes can echo an authenticated clone URL, so the trace is scrubbed the
    # same way the response message is.
    if exc is None:
        return None
    return _scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def _log_failure(request: Request, status: int, error: str, *, failed_step: str | None = None,
                 cause: BaseException | None = None) -> None:
    fields = {
        "method": request.method, "path": request.url.path, "status": status,
        "failedStep": failed_step, "error": error,
    }
    if status >= 500:
        log.error("request.failed", exception=_scrubbed_traceback(cause), **fields)
    else:
        log.warning("request.failed", **fields)


def create_app(settings: Settings | None = None, deps: ServerDeps | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Breeze Code Ontology Generator", version=__version__)
    app.state.settings = settings
    app.state.deps = deps or default_deps(settings)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        content: dict[str, str] = {"error": str(exc)}
        if exc.failed_step:
            content["failedStep"] = exc.failed_step
        _log_failure(request, exc.status_code, str(exc), failed_step=exc.failed_step,
                     cause=exc.__cause__ or exc)
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        message = errors[0]["msg"] if errors else "Invalid request"
        _log_failure(request, 400, message)
        return JSONResponse(status_code=400, content={"error": message})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # A bug outside the routes' ApiError mapping: uvicorn would print the traceback to
        # stderr only, so it never reached the log file.
        _log_failure(request, 500, _scrub(f"{type(exc).__name__}: {exc}"), cause=exc)
        return JSONResponse(status_code=500, content={"error": "Internal Server Error"})

    app.include_router(router)
    return app


app = create_app()  # module target for `uvicorn breezeai_cog.server.app:app`
