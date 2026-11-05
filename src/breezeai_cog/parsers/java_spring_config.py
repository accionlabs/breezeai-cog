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
the base files disagree, or the value is a ``${…}`` placeholder. A module without a context path
is absent from the result.

:func:`spring_properties` reads the same base files into a flat ``key → value`` map per module,
so an annotation value written as a property placeholder (``@KafkaListener(topics =
"${app.orders-topic}")``) resolves through :func:`resolve_placeholders`."""

from __future__ import annotations

import re
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


def _properties_all(text: str) -> dict[str, str]:
    """Every ``key = value`` of a ``.properties`` file (``=``, ``:`` or whitespace separated)."""
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
    return found


def _flatten(node: object, prefix: str, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(node, list):
        out[prefix] = ",".join(str(v) for v in node)
    elif node is not None:
        out[prefix] = str(node).strip()


def _yaml_all(text: str) -> dict[str, str]:
    """Every dotted key of the profile-less documents of a YAML file."""
    if yaml is None:
        return {}
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return {}
    out: dict[str, str] = {}
    for doc in docs:
        if isinstance(doc, dict) and not any(_lookup(doc, k) is not None for k in _PROFILE_KEYS):
            _flatten(doc, "", out)
    return out


def _module_properties(module_dir: Path) -> dict[str, str | None]:
    """Flat base-config properties of one module; a key the base files set to different values
    → None (unknown)."""
    out: dict[str, str | None] = {}
    for d in _RESOURCE_DIRS:
        for name in _NAMES:
            f = module_dir / d / name
            if not f.is_file():
                continue
            try:
                text = f.read_text("utf-8", errors="replace")
            except OSError:
                continue
            values = _properties_all(text) if name.endswith(".properties") else _yaml_all(text)
            for key, value in values.items():
                out[key] = value if out.get(key, value) == value else None
    return out


def spring_properties(
    repo_root: Path, java_files: Iterable[str]
) -> dict[str, dict[str, str | None]]:
    """Module root → its base-config properties (``key → value``; None = the base files
    disagree), for every module with Java sources under ``src/main/java`` and a base config."""
    out: dict[str, dict[str, str | None]] = {}
    for module in sorted({m for f in java_files if (m := module_of(f)) is not None}):
        props = _module_properties(repo_root / module if module else repo_root)
        if props:
            out[module] = props
    return out


_PLACEHOLDER = re.compile(r"\$\{([^{}:]+)(?::([^{}]*))?\}")


def resolve_placeholders(value: str, props: dict[str, str | None]) -> str | None:
    """Substitute ``${key}`` / ``${key:default}`` from the module's base config. A key that is
    absent uses its default; absent without a default, or set to different values, → None (the
    address is unknown). A SpEL ``#{…}`` expression never resolves. Nested placeholders are
    followed a few levels."""
    if "#{" in value:
        return None
    for _ in range(4):
        if "${" not in value:
            return value
        missing = False

        def sub(m: re.Match[str]) -> str:
            nonlocal missing
            key, default = m.group(1).strip(), m.group(2)
            if key in props:
                hit = props[key]
                if hit is None:
                    missing = True
                    return ""
                return hit
            if default is not None:
                return default
            missing = True
            return ""

        value = _PLACEHOLDER.sub(sub, value)
        if missing:
            return None
    return None if "${" in value else value
