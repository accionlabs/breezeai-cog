"""Spring Boot application config read at index time: the servlet context path per module.

``server.servlet.context-path`` (Spring Boot 2+; ``server.context-path`` in 1.x) is a prefix
for every HTTP route of the application. It lives in config, not in code, so the per-file
parse cannot see it; the Java index reads it once per module and the Spring parser joins it
onto each route.

A module is the directory above ``src/main/java``. Only its **base** config counts:
``src/main/resources/application.{properties,yml,yaml}`` and the same under
``src/main/resources/config/`` (Spring loads both). Test resources and profile files
(``application-<profile>.*``) are ignored, and so are YAML documents activated by a profile.

Result per module: the path, or ``None`` when a context path is set but cannot be resolved —
the base files disagree, or the value is a ``${…}`` placeholder (cog does not resolve property
placeholders). A module without a context path is absent from the result."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:  # PyYAML is a core dep; degrade gracefully if somehow absent
    import yaml  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    yaml = None

_MAIN_JAVA = "src/main/java/"
_RESOURCE_DIRS = ("src/main/resources", "src/main/resources/config")
_NAMES = ("application.properties", "application.yml", "application.yaml")
_KEYS = ("server.servlet.context-path", "server.context-path")  # Boot 2+ first, then 1.x
_PROFILE_KEYS = ("spring.profiles", "spring.config.activate.on-profile")


def module_of(rel_path: str) -> str | None:
    """The module root of a Java source file (``core/src/main/java/a/B.java`` → ``core``; a
    root-level module → ``""``), or None when the file is not under ``src/main/java``."""
    p = rel_path.replace("\\", "/")
    i = p.find(_MAIN_JAVA)
    if i < 0 or (i > 0 and p[i - 1] != "/"):
        return None
    return p[: i].rstrip("/")


def _properties_value(text: str) -> str | None:
    """The context path set in a ``.properties`` file (the first key of :data:`_KEYS` that is
    present), else None."""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        for sep in ("=", ":"):
            if sep in line:
                key, value = line.split(sep, 1)
                break
        else:
            parts = line.split(None, 1)
            key, value = parts[0], parts[1] if len(parts) > 1 else ""
        found[key.strip()] = value.strip()
    return next((found[k] for k in _KEYS if k in found), None)


def _lookup(doc: dict[str, Any], dotted: str) -> object | None:
    """``server.servlet.context-path`` in a YAML document — nested maps or a flat dotted key."""
    if dotted in doc:
        return doc[dotted]  # type: ignore[no-any-return]
    node: object = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _yaml_values(text: str) -> list[str]:
    """Context paths set by the profile-less documents of a YAML file."""
    if yaml is None:
        return []
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return []
    out: list[str] = []
    for doc in docs:
        if not isinstance(doc, dict) or any(_lookup(doc, k) is not None for k in _PROFILE_KEYS):
            continue
        value = next((v for k in _KEYS if (v := _lookup(doc, k)) is not None), None)
        if value is not None:
            out.append(str(value).strip())
    return out


def _module_context_path(module_dir: Path) -> tuple[bool, str | None]:
    """(set?, value) for one module: whether any base config sets a context path, and its
    value — None when the files disagree or a value is a placeholder."""
    values: list[str] = []
    for d in _RESOURCE_DIRS:
        for name in _NAMES:
            f = module_dir / d / name
            if not f.is_file():
                continue
            try:
                text = f.read_text("utf-8", errors="replace")
            except OSError:
                continue
            if name.endswith(".properties"):
                v = _properties_value(text)
                values.extend([v] if v is not None else [])
            else:
                values.extend(_yaml_values(text))
    if not values:
        return False, None
    distinct = set(values)
    if len(distinct) > 1 or any("${" in v for v in distinct):
        return True, None
    return True, values[0]


def spring_context_paths(repo_root: Path, java_files: Iterable[str]) -> dict[str, str | None]:
    """Module root → its servlet context path (None: set but unresolvable), for every module
    that has Java sources under ``src/main/java`` and a context path in its base config."""
    out: dict[str, str | None] = {}
    for module in sorted({m for f in java_files if (m := module_of(f)) is not None}):
        is_set, value = _module_context_path(repo_root / module if module else repo_root)
        if is_set:
            out[module] = value
    return out
