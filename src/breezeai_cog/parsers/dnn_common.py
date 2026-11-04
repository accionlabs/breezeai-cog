"""Shared DotNetNuke manifest parsing and path resolution."""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypedDict

from .dotnet_common import ci_resolve, to_repo_path


class DnnManifestControl(TypedDict):
    module: str
    controlKey: str
    source: str


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _attribute(attrs: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    return next((value for key, value in attrs.items() if _local(key) == wanted), None)


def _child_text(element: ET.Element, name: str) -> str | None:
    return next(
        ((child.text or "").strip() for child in element if _local(child.tag) == name),
        None,
    )


def _descendant_text(element: ET.Element, name: str) -> str | None:
    return next(
        (
            (child.text or "").strip()
            for child in element.iter()
            if _local(child.tag) == name and child.text and child.text.strip()
        ),
        None,
    )


def _control(element: ET.Element, module: str) -> DnnManifestControl | None:
    key = _child_text(element, "controlkey")
    source = _child_text(element, "controlsrc")
    if key is None:
        key = _attribute(element.attrib, "controlkey")
    if source is None:
        source = _attribute(element.attrib, "controlsrc")
    if key is None or not source:
        return None
    return {"module": module, "controlKey": key, "source": source}


def parse_dnn_manifest(source: str | bytes) -> tuple[str | None, list[DnnManifestControl]]:
    """Return the first module name and all controls from Module components.

    The DNN schema permits several packages/modules in one manifest. Controls carry their
    owning folder so downstream path resolution never assigns one package's controls to
    another. A small root-level fallback is retained for older/minimal manifests used by
    module tooling and tests.
    """
    root = ET.fromstring(source)
    module_components = [
        element
        for element in root.iter()
        if _local(element.tag) == "component"
        and (_attribute(element.attrib, "type") or "").lower() == "module"
    ]
    controls: list[DnnManifestControl] = []
    modules: list[str] = []
    for component in module_components:
        module = _descendant_text(component, "foldername") or _descendant_text(
            component, "modulename"
        )
        if not module:
            continue
        if module not in modules:
            modules.append(module)
        for element in component.iter():
            if _local(element.tag) != "modulecontrol":
                continue
            control = _control(element, module)
            if control is not None:
                controls.append(control)

    if not module_components:
        module = _descendant_text(root, "foldername") or _descendant_text(root, "modulename") or ""
        if module:
            modules.append(module)
        for element in root.iter():
            if _local(element.tag) == "modulecontrol":
                control = _control(element, module)
                if control is not None:
                    controls.append(control)

    return (modules[0] if modules else None), controls


def resolve_dnn_control_source(repo_root: Path, manifest_path: str, source: str) -> str | None:
    """Resolve a manifest ``controlSrc`` to the real repo-relative file path."""
    relative = to_repo_path(source, posixpath.dirname(manifest_path), "")
    if relative is not None:
        actual = ci_resolve(repo_root, relative)
        if actual is not None:
            return actual
    normalized = source.replace("\\", "/").lstrip("/")
    if normalized.lower().startswith("desktopmodules/"):
        return ci_resolve(repo_root, normalized)
    return None
