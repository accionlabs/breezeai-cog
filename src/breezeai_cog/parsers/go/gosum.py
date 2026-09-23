"""Go dependency checksum parsing for go.sum files."""

from __future__ import annotations

from pathlib import Path


def parse_gosum(path: str | Path, text: str) -> dict:
    deps: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("//"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            deps.append(parts[0])
    return {
        "kind": "gosum",
        "dependencyCount": len(set(deps)),
    }