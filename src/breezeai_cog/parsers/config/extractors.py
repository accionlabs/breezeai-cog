"""Config-file metadata extractors — one per recognized config type, dispatched by
filename/extension. Uses real parsers (stdlib ``json``/``tomllib``/``configparser``/
``xml.etree`` + PyYAML) rather than the original JS's regex/line scraping, and extracts
things the JS did not (actual ``pyproject.toml`` / ``Pipfile`` dependencies, structured
``pom.xml`` deps, full docker-compose services). Each returns a ``metadata`` dict with a
normalized ``category`` (+ ``packageManager``/``buildTool``/``dependencyCount`` where
relevant) that the pipeline aggregates into ``projectMetaData.configs``.
"""

from __future__ import annotations

import configparser
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

try:  # PyYAML is a core dep; degrade gracefully if somehow absent
    import yaml
except Exception:  # pragma: no cover
    yaml = None

_EXT_CATEGORY = {
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".ini": "ini", ".xml": "xml", ".gradle": "gradle",
    ".csproj": "dotnet", ".sln": "dotnet",
}
_GRADLE_DEP = re.compile(
    r"\b(?:implementation|api|compile|testImplementation|runtimeOnly|annotationProcessor|classpath)\b"
)
_REQ_NAME = re.compile(r"[<>=!~;\s\[]")


def _category(name: str, suffix: str) -> str:
    if name == "Dockerfile" or name.startswith("Dockerfile."):
        return "docker"
    if name == ".env" or name.startswith(".env"):
        return "env"
    if suffix in _EXT_CATEGORY:
        return _EXT_CATEGORY[suffix]
    if name in ("requirements.txt", "Pipfile"):
        return "python"
    return "other"


def extract_config(path: str, text: str) -> dict[str, Any]:
    name, suffix = Path(path).name, Path(path).suffix
    try:
        meta = _dispatch(name, suffix, text)
    except Exception as exc:  # never fail a run on a malformed config
        meta = {"parseError": str(exc)[:200]}
    meta.setdefault("category", _category(name, suffix))
    return meta


def _dispatch(name: str, suffix: str, text: str) -> dict[str, Any]:
    if name == "package.json":
        return _package_json(text)
    if name in ("tsconfig.json", "jsconfig.json"):
        return _tsconfig(name, text)
    if name in ("docker-compose.yml", "docker-compose.yaml"):
        return _docker_compose(text)
    if name == "Dockerfile" or name.startswith("Dockerfile."):
        return _dockerfile(text)
    if name == ".env" or name.startswith(".env"):
        return _env(text)
    if name == "pom.xml":
        return _pom(text)
    if suffix == ".csproj":
        return _csproj(text)
    if suffix == ".sln":
        return _sln(text)
    if name == "requirements.txt":
        return _requirements(text)
    if name == "pyproject.toml":
        return _pyproject(text)
    if name == "Pipfile":
        return _pipfile(text)
    if name in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
        return _gradle(name, text)
    if name == "Makefile":
        return _makefile(text)
    if name in (".gitignore", ".dockerignore"):
        return _ignorefile(name, text)
    if name == "LICENSE":
        return _license(text)
    if name in ("README.md", "README.rst"):
        return {"kind": "readme", "category": "other", "lines": len(text.splitlines())}
    # generic by extension
    if suffix == ".json":
        return _generic_json(text)
    if suffix in (".yml", ".yaml"):
        return _generic_yaml(text)
    if suffix == ".toml":
        return _generic_toml(text)
    if suffix == ".ini":
        return _generic_ini(text)
    if suffix == ".xml":
        return _generic_xml(text)
    if suffix == ".gradle":
        return _gradle(name, text)
    return {}


# ── JSON family ───────────────────────────────────────────────────────────────
def _package_json(text: str) -> dict[str, Any]:
    d = json.loads(text)
    deps = list((d.get("dependencies") or {}).keys())
    dev = list((d.get("devDependencies") or {}).keys())
    return {
        "kind": "package.json", "category": "json", "packageManager": "npm",
        "packageInfo": {
            "name": d.get("name"), "version": d.get("version"),
            "description": d.get("description"), "main": d.get("main"),
            "scripts": list((d.get("scripts") or {}).keys()),
            "dependencies": deps, "devDependencies": dev,
        },
        "dependencyCount": len(deps), "devDependencyCount": len(dev),
    }


def _tsconfig(name: str, text: str) -> dict[str, Any]:
    d = json.loads(text)
    co = d.get("compilerOptions") or {}
    return {
        "kind": name, "category": "json", "buildTool": "typescript",
        "compilerConfig": {
            "target": co.get("target"), "module": co.get("module"),
            "outDir": co.get("outDir"), "rootDir": co.get("rootDir"),
            "strict": co.get("strict"), "paths": list((co.get("paths") or {}).keys()),
            "include": d.get("include"), "exclude": d.get("exclude"),
        },
    }


def _generic_json(text: str) -> dict[str, Any]:
    d = json.loads(text)
    return {"kind": "json", "category": "json",
            "topLevelKeys": list(d.keys()) if isinstance(d, dict) else []}


# ── YAML family ─────────────────────────────────────────────────────────────
def _docker_compose(text: str) -> dict[str, Any]:
    d = (yaml.safe_load(text) if yaml else None) or {}
    services = d.get("services") if isinstance(d, dict) else None
    services = services if isinstance(services, dict) else {}
    images, ports, volumes = [], [], []
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        if svc.get("image"):
            images.append(str(svc["image"]))
        for p in svc.get("ports") or []:
            ports.append(str(p.get("published", p) if isinstance(p, dict) else p))
        for v in svc.get("volumes") or []:
            volumes.append(v.get("source", "") if isinstance(v, dict) else str(v))
    networks = d.get("networks") if isinstance(d, dict) else None
    return {
        "kind": "docker-compose", "category": "yaml",
        "dockerCompose": {
            "services": list(services.keys()), "serviceCount": len(services),
            "images": images, "exposedPorts": ports, "volumes": volumes,
            "networks": list(networks.keys()) if isinstance(networks, dict) else [],
        },
    }


def _generic_yaml(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": "yaml", "category": "yaml"}
    if yaml is not None:
        keys: dict[str, None] = {}
        kinds, docs = [], 0
        for doc in yaml.safe_load_all(text):  # handles multi-document YAML (--- separators)
            docs += 1
            if isinstance(doc, dict):
                keys.update(dict.fromkeys(doc.keys()))
                if isinstance(doc.get("kind"), str):
                    kinds.append(doc["kind"])
        out["topLevelKeys"] = list(keys)
        if docs > 1:
            out["documentCount"] = docs
        if kinds:  # e.g. Kubernetes manifests → ["Deployment", "Service", "Ingress"]
            out["resourceKinds"] = kinds
    else:  # pragma: no cover
        out["topLevelKeys"] = [ln.split(":")[0] for ln in text.splitlines()
                               if re.match(r"^\w", ln) and ":" in ln]
    return out


# ── Docker / env ─────────────────────────────────────────────────────────────
def _dockerfile(text: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "baseImages": [], "exposedPorts": [], "volumes": [], "workdir": None,
        "entrypoint": None, "cmd": None, "env": [], "stages": [],
    }
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        up = line.upper()
        if up.startswith("FROM "):
            parts = line[5:].split()
            if parts:
                info["baseImages"].append(parts[0])
            if len(parts) >= 3 and parts[1].upper() == "AS":
                info["stages"].append(parts[2])
        elif up.startswith("EXPOSE "):
            info["exposedPorts"].extend(line[7:].split())
        elif up.startswith("VOLUME "):
            info["volumes"].append(line[7:].strip())
        elif up.startswith("WORKDIR "):
            info["workdir"] = line[8:].strip()
        elif up.startswith("ENTRYPOINT "):
            info["entrypoint"] = line[11:].strip()
        elif up.startswith("CMD "):
            info["cmd"] = line[4:].strip()
        elif up.startswith("ENV "):
            info["env"].append(line[4:].strip().split("=")[0].split()[0])
    return {"kind": "dockerfile", "category": "docker", "dockerInfo": info}


def _env(text: str) -> dict[str, Any]:
    names = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key = line.split("=", 1)[0].strip()
        if key:
            names.append(key)  # names only — values never captured
    return {"kind": "env", "category": "env", "variableCount": len(names), "variables": names}


# ── XML family ───────────────────────────────────────────────────────────────
def _strip(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(parent, tag):
    return next((c for c in parent if _strip(c.tag) == tag), None)


def _pom(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    deps = []
    deps_el = _child(root, "dependencies")
    for d in deps_el if deps_el is not None else []:
        if _strip(d.tag) != "dependency":
            continue
        deps.append({t: (_child(d, t).text if _child(d, t) is not None else None)
                     for t in ("groupId", "artifactId", "version", "scope")})
    return {
        "kind": "pom.xml", "category": "xml", "packageManager": "maven", "buildTool": "maven",
        "mavenInfo": {
            "groupId": getattr(_child(root, "groupId"), "text", None),
            "artifactId": getattr(_child(root, "artifactId"), "text", None),
            "version": getattr(_child(root, "version"), "text", None),
            "packaging": getattr(_child(root, "packaging"), "text", None),
            "dependencies": deps, "dependencyCount": len(deps),
        },
    }


def _generic_xml(text: str) -> dict[str, Any]:
    return {"kind": "xml", "category": "xml", "rootElement": _strip(ET.fromstring(text).tag)}


def _csproj(text: str) -> dict[str, Any]:
    """.NET project file — the NuGet analog of package.json/pom.xml. Extracts the SDK,
    target framework(s), and PackageReference (NuGet) / ProjectReference (inter-project)
    dependencies. Handles both SDK-style (attribute versions) and legacy csproj (default
    xmlns + child <Version>); ``_strip`` drops the namespace so both parse the same."""
    root = ET.fromstring(text)
    packages: list[dict[str, Any]] = []
    projects: list[str] = []
    frameworks: list[str] = []
    for el in root.iter():
        tag = _strip(el.tag)
        if tag == "PackageReference":
            pkg = el.get("Include") or el.get("Update")
            if pkg:
                ver = el.get("Version")
                if ver is None:  # legacy style stores version as a child element
                    child = _child(el, "Version")
                    ver = child.text if child is not None else None
                packages.append({"name": pkg, "version": ver})
        elif tag == "ProjectReference":
            inc = el.get("Include")
            if inc:
                projects.append(inc.replace("\\", "/"))  # normalize Windows separators
        elif tag in ("TargetFramework", "TargetFrameworks") and el.text:
            frameworks.extend(f.strip() for f in el.text.split(";") if f.strip())
    return {
        "kind": "csproj", "category": "dotnet", "packageManager": "nuget", "buildTool": "dotnet",
        "dotnetInfo": {
            "sdk": root.get("Sdk"), "targetFrameworks": frameworks,
            "packageReferences": packages, "projectReferences": projects,
            "projectReferenceCount": len(projects),
        },
        "dependencyCount": len(packages),
    }


_SLN_PROJECT = re.compile(r'^Project\("\{[^}]*\}"\)\s*=\s*"([^"]+)",\s*"([^"]+)"', re.M)


def _sln(text: str) -> dict[str, Any]:
    """Visual Studio solution manifest — a custom (non-XML) text format listing member
    projects, so it needs its own line parser. Solution *folders* also appear as Project
    entries but point at a bare name rather than a project file, so filter to real project
    paths (those carrying a path separator or a *proj extension)."""
    projects = []
    for name, path in _SLN_PROJECT.findall(text):
        norm = path.replace("\\", "/")
        if "/" in norm or norm.endswith((".csproj", ".vbproj", ".fsproj", ".vcxproj")):
            projects.append({"name": name, "path": norm})
    return {
        "kind": "sln", "category": "dotnet", "buildTool": "dotnet",
        "solutionInfo": {"projectCount": len(projects), "projects": projects},
    }


# ── Python / TOML family ─────────────────────────────────────────────────────
def _requirements(text: str) -> dict[str, Any]:
    deps = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        name = _REQ_NAME.split(line, 1)[0].strip()
        if name:
            deps.append(name)
    return {"kind": "requirements.txt", "category": "python", "packageManager": "pip",
            "dependencies": deps, "dependencyCount": len(deps)}


def _pyproject(text: str) -> dict[str, Any]:
    d = tomllib.loads(text)
    project = d.get("project") or {}
    poetry = (d.get("tool") or {}).get("poetry") or {}
    if poetry:
        pm = "poetry"
        names = [k for k in (poetry.get("dependencies") or {}) if k.lower() != "python"]
        info = {"name": poetry.get("name"), "version": poetry.get("version")}
    else:
        pm = "pip"
        names = [_REQ_NAME.split(x, 1)[0].strip() for x in (project.get("dependencies") or [])
                 if isinstance(x, str)]
        info = {"name": project.get("name"), "version": project.get("version")}
    return {"kind": "pyproject.toml", "category": "toml", "packageManager": pm,
            "projectInfo": info, "dependencies": names, "dependencyCount": len(names)}


def _pipfile(text: str) -> dict[str, Any]:
    d = tomllib.loads(text)
    pkgs = list((d.get("packages") or {}).keys())
    dev = list((d.get("dev-packages") or {}).keys())
    return {"kind": "Pipfile", "category": "python", "packageManager": "pipenv",
            "dependencies": pkgs, "devDependencies": dev, "dependencyCount": len(pkgs)}


def _generic_toml(text: str) -> dict[str, Any]:
    return {"kind": "toml", "category": "toml", "topLevelKeys": list(tomllib.loads(text).keys())}


def _generic_ini(text: str) -> dict[str, Any]:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read_string(text)
    return {"kind": "ini", "category": "ini", "sections": cp.sections()}


# ── Gradle / other ───────────────────────────────────────────────────────────
def _gradle(name: str, text: str) -> dict[str, Any]:
    return {"kind": "gradle", "category": "gradle", "packageManager": "gradle",
            "buildTool": "gradle", "dependencyCount": len(_GRADLE_DEP.findall(text)),
            "isKotlinDSL": name.endswith(".kts")}


def _makefile(text: str) -> dict[str, Any]:
    targets = re.findall(r"^([A-Za-z0-9_.\-/]+):(?!=)", text, re.M)
    return {"kind": "makefile", "category": "other", "targets": targets[:100]}


def _ignorefile(name: str, text: str) -> dict[str, Any]:
    patterns = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return {"kind": name.lstrip("."), "category": "other", "patternCount": len(patterns)}


def _license(text: str) -> dict[str, Any]:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return {"kind": "license", "category": "other", "title": first[:120]}
