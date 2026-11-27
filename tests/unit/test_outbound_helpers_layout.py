"""Layout of the helper rule (``parsers/outbound_helpers.py``): language and framework parsers stay
plug and play.

A language package, the shared engine and a language's helper rule never import a framework
package (``parsers/<lang>_<framework>/``): a framework contributes what it recognises by
registering it, so removing its directory removes only that."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from breezeai_cog.parsers.outbound_helpers import request_labels, request_sources

PARSERS = Path(__file__).resolve().parents[2] / "src" / "breezeai_cog" / "parsers"
#: The languages whose helpers the engine follows.
HELPER_LANGUAGES = ("groovy", "java", "python", "typescript")


def _framework_packages() -> set[str]:
    dirs = {p.name for p in PARSERS.iterdir() if p.is_dir() and not p.name.startswith("__")}
    languages = {d for d in dirs if "_" not in d}
    return {d for d in dirs if "_" in d and d.split("_", 1)[0] in languages}


def _checked_modules() -> list[Path]:
    out = [PARSERS / "outbound_helpers.py"]
    for lang in HELPER_LANGUAGES:
        out += sorted((PARSERS / lang).rglob("*.py"))
    return out


@pytest.mark.parametrize("module", _checked_modules(), ids=lambda p: str(p.relative_to(PARSERS)))
def test_no_framework_package_is_imported(module: Path) -> None:
    frameworks = _framework_packages()
    tree = ast.parse(module.read_text())
    package_depth = len(module.relative_to(PARSERS).parts)  # 1: parsers/x.py, 2: parsers/lang/x.py
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == package_depth:
            top = node.module.split(".", 1)[0]  # relative to parsers/
            if top in frameworks:
                imported.append(f"{node.module} (line {node.lineno})")
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names
                         if a.name.startswith("breezeai_cog.parsers.")
                         and a.name.split(".")[2] in frameworks]
    assert not imported, f"imports framework packages: {imported}"


def test_registered_request_recognizers() -> None:
    """Every client a helper's request may be made with, contributed by its own package."""
    assert [s.name for s in request_sources("java")] == ["java-apache-httpclient",
                                                         "java-vertx2-httpclient"]


def test_registered_request_labels() -> None:
    assert [lb.name for lb in request_labels("typescript")] == ["angular", "axios", "fetch"]
