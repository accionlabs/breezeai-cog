"""Injectable server dependencies — the storage stream factory and the backend notifier.
Real implementations stream to object storage / POST via httpx; tests substitute in-memory
fakes, so the streaming endpoints are testable without AWS or a live backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..config import Settings
from ..infra.interface import InfraStream
from ..integrations.scm.base import AbstractSCMClient, RepoRef


@dataclass
class ServerDeps:
    settings: Settings
    open_storage: Callable[[str], InfraStream]  # key -> open streaming upload
    notify: Callable[[str, dict[str, Any]], Any]  # (backend path, payload) -> response
    # body -> (temp_dir, filter_set | None, deleted_files); None filter = full clone
    acquire_diff: Callable[[Settings, dict[str, Any]], tuple[str, set[str] | None, list[str]]] | None = None
    # (ref, settings, request token) -> provider client; the /api/git/* routes go through this
    # so tests can hand them a scripted client instead of a real provider.
    open_scm: Callable[[RepoRef, Settings, str | None], AbstractSCMClient] | None = None


def default_deps(settings: Settings) -> ServerDeps:
    from ..infra.provider import open_stream
    from ..integrations.scm.factory import SCMClientFactory
    from ..services.notify import post_notification
    from .git import acquire_diff

    return ServerDeps(
        settings=settings,
        open_storage=lambda key: open_stream(key, settings),
        notify=lambda path, payload: post_notification(settings, path, payload),
        acquire_diff=acquire_diff,
        open_scm=lambda ref, s, token: SCMClientFactory.for_repo(ref, s, request_token=token),
    )
