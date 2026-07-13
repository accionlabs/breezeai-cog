"""FastAPI app factory. Maps both manual ``ApiError`` and pydantic
``RequestValidationError`` to the existing ``{"error": "<message>"}`` shape — and,
critically, validation failures return **400** (not FastAPI's default 422)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .._version import __version__
from ..config import Settings
from .deps import ServerDeps, default_deps
from .errors import ApiError
from .routes import router


def create_app(settings: Settings | None = None, deps: ServerDeps | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Breeze Code Ontology Generator", version=__version__)
    app.state.settings = settings
    app.state.deps = deps or default_deps(settings)

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        message = errors[0]["msg"] if errors else "Invalid request"
        return JSONResponse(status_code=400, content={"error": message})

    app.include_router(router)
    return app


app = create_app()  # module target for `uvicorn breezeai_cog.server.app:app`
