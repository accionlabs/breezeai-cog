"""Injectable server dependencies — the S3 stream factory and the backend notifier.
Real implementations stream to S3 / POST via httpx; tests substitute in-memory fakes,
so the streaming endpoints are testable without AWS or a live backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from ..infra.interface import InfraStream
from ..config import Settings


@dataclass
class ServerDeps:
    settings: Settings
    open_stream: Callable[[str], InfraStream]       # key -> open streaming upload
    notify: Callable[[str, dict[str, Any]], Any]  # (backend path, payload) -> response
    # body -> (temp_dir, filter_set | None, deleted_files); None filter = full clone
    acquire_diff: Callable[[Settings, dict[str, Any]], tuple[str, set[str] | None, list[str]]] | None = None


def default_deps(settings: Settings) -> ServerDeps:
    from ...breezeai_cog.infra import provider
    from ..services.notify import post_notification
    from .git import acquire_diff

    return ServerDeps(
        settings=settings,
        open_stream=lambda key: provider.open_stream(key,settings),
        
        notify=lambda path, payload: post_notification(settings, path, payload),
        acquire_diff=acquire_diff,
    )
