"""Go module metadata extraction for go.mod files."""

from __future__ import annotations

from pathlib import Path


def parse_gomod(path: str | Path, text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("//")]
    module = None
    go_version = None
    require: list[str] = []
    for line in lines:
        if line.startswith("module "):
            module = line.split(None, 1)[1].strip()
        elif line.startswith("go "):
            go_version = line.split(None, 1)[1].strip()
        elif line.startswith("require ("):
            continue
        elif line.startswith("require "):
            require.append(line.split()[1])
        elif line.startswith(")"):
            continue
        elif " " in line and line.split()[0] not in {"replace", "exclude", "toolchain", "retract"}:
            parts = line.split()
            if len(parts) >= 2 and parts[0] not in {"replace", "exclude"}:
                require.append(parts[0])
    return {
        "kind": "gomod",
        "module": module,
        "goVersion": go_version,
        "dependencies": require,
    }
