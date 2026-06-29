"""Server-side error carrying an HTTP status. The app's exception handler renders it
as ``{"error": "<message>"}`` with that status (the existing contract, §10 — the
``{error}`` shape is the one accepted deviation, kept over RFC 7807)."""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code
