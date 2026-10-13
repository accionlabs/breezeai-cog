"""Server-side error carrying an HTTP status. The app's exception handler renders it
as ``{"error": "<message>"}`` with that status (the existing contract — the
``{error}`` shape is the one accepted deviation, kept over RFC 7807)."""

from __future__ import annotations


class ApiError(Exception):
    """``failed_step`` names the pipeline stage that failed (``git_acquire``,
    ``ignore_patterns``, ``parse_stream``, ``upload``). When set, the app handler adds
    ``failedStep`` to the JSON body so the Breeze backend can persist which step
    broke next to the reason (BREEZEAI-520 / BREEZEAI-681)."""

    def __init__(self, message: str, status_code: int = 500, failed_step: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.failed_step = failed_step
