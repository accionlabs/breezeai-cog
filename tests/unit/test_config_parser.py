"""Config-file parser: per-type metadata extraction, selection/matching, hierarchical
capture, projectMetaData.configs aggregation, and schema validity."""

from __future__ import annotations

import gzip
import json

from breezeai_cog.config import Settings
from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.config.parser import ConfigParser


def _meta(path: str, text: str) -> dict:
    ctx = ParseContext(path=path, abs_path=None, source=text.encode(), repo_root=".")
    rec = ConfigParser().parse_file(ctx)
    assert rec.type == "config" and rec.language == "config"
    return rec.metadata


def test_package_json() -> None:
    md = _meta("package.json", json.dumps({
        "name": "app", "version": "1.0.0",
        "scripts": {"build": "x", "test": "y"},
        "dependencies": {"react": "^18", "axios": "^1"},
        "devDependencies": {"jest": "^29"},
    }))
    assert md["packageManager"] == "npm"
    assert md["packageInfo"]["name"] == "app"
    assert set(md["packageInfo"]["dependencies"]) == {"react", "axios"}
    assert md["dependencyCount"] == 2 and md["devDependencyCount"] == 1


def test_pyproject_deps_extracted() -> None:
    # Improvement over the JS analyzer, which only line-counted pyproject.toml.
    md = _meta("pyproject.toml", '[project]\nname = "svc"\nversion = "2.1"\n'
                                 'dependencies = ["fastapi>=0.110", "httpx"]\n')
    assert md["packageManager"] == "pip" and md["projectInfo"]["name"] == "svc"
    assert md["dependencies"] == ["fastapi", "httpx"] and md["dependencyCount"] == 2


def test_pom_xml_structured() -> None:
    md = _meta("pom.xml", """<project xmlns="http://maven.apache.org/POM/4.0.0">
      <groupId>com.acme</groupId><artifactId>svc</artifactId><version>1.0</version>
      <dependencies>
        <dependency><groupId>org.springframework</groupId><artifactId>spring-web</artifactId></dependency>
        <dependency><groupId>junit</groupId><artifactId>junit</artifactId><scope>test</scope></dependency>
      </dependencies></project>""")
    assert md["packageManager"] == "maven" and md["mavenInfo"]["artifactId"] == "svc"
    assert md["mavenInfo"]["dependencyCount"] == 2
    assert md["mavenInfo"]["dependencies"][0]["groupId"] == "org.springframework"


def test_csproj_sdk_style() -> None:
    md = _meta("Svc.csproj", """<Project Sdk="Microsoft.NET.Sdk">
      <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
      <ItemGroup>
        <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
        <PackageReference Include="Serilog" Version="3.1.1" />
        <ProjectReference Include="..\\Lib\\Lib.csproj" />
      </ItemGroup></Project>""")
    assert md["category"] == "dotnet" and md["packageManager"] == "nuget"
    assert md["buildTool"] == "dotnet"
    assert md["dotnetInfo"]["sdk"] == "Microsoft.NET.Sdk"
    assert md["dotnetInfo"]["targetFrameworks"] == ["net8.0"]
    assert md["dependencyCount"] == 2
    assert md["dotnetInfo"]["packageReferences"][0] == {"name": "Newtonsoft.Json", "version": "13.0.3"}
    assert md["dotnetInfo"]["projectReferences"] == ["../Lib/Lib.csproj"]
    assert md["dotnetInfo"]["projectReferenceCount"] == 1


def test_csproj_legacy_namespaced() -> None:
    # Legacy (non-SDK) csproj: default xmlns + version as a child element, multiple TFMs.
    md = _meta("Old.csproj", """<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
      <PropertyGroup><TargetFrameworks>net48;netstandard2.0</TargetFrameworks></PropertyGroup>
      <ItemGroup>
        <PackageReference Include="EntityFramework"><Version>6.4.4</Version></PackageReference>
      </ItemGroup></Project>""")
    assert md["dotnetInfo"]["targetFrameworks"] == ["net48", "netstandard2.0"]
    assert md["dotnetInfo"]["packageReferences"] == [{"name": "EntityFramework", "version": "6.4.4"}]
    assert md["dependencyCount"] == 1 and md["dotnetInfo"]["sdk"] is None


def test_sln_lists_projects_not_folders() -> None:
    sln = (
        'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Svc", "Svc\\Svc.csproj", "{A1}"\n'
        'EndProject\n'
        'Project("{2150E333-8FDC-42A3-9474-1A3956D46DE4}") = "SolutionItems", "SolutionItems", "{B2}"\n'
        'EndProject\n'
    )
    md = _meta("App.sln", sln)
    assert md["category"] == "dotnet" and md["buildTool"] == "dotnet"
    assert md["solutionInfo"]["projectCount"] == 1  # solution folder excluded
    assert md["solutionInfo"]["projects"] == [{"name": "Svc", "path": "Svc/Svc.csproj"}]


def test_docker_compose_and_dockerfile() -> None:
    dc = _meta("docker-compose.yml",
               "services:\n  db:\n    image: postgres:16\n    ports:\n      - '5432:5432'\n  api:\n    build: .\n")
    assert set(dc["dockerCompose"]["services"]) == {"db", "api"}
    assert dc["dockerCompose"]["images"] == ["postgres:16"]

    df = _meta("Dockerfile", "FROM node:20 AS build\nWORKDIR /app\nEXPOSE 3000\nCMD npm start\n")
    assert df["dockerInfo"]["baseImages"] == ["node:20"] and df["dockerInfo"]["stages"] == ["build"]
    assert df["dockerInfo"]["exposedPorts"] == ["3000"]


def test_env_names_only() -> None:
    md = _meta(".env", "# c\nDB_HOST=secret-host\nexport API_KEY=sk-123\n")
    assert md["variables"] == ["DB_HOST", "API_KEY"]  # names only
    assert "secret-host" not in json.dumps(md) and "sk-123" not in json.dumps(md)


def test_requirements_and_tsconfig_and_generic() -> None:
    req = _meta("requirements.txt", "flask==2.0\n# comment\nhttpx>=0.27\n-r base.txt\n")
    assert req["dependencies"] == ["flask", "httpx"]
    ts = _meta("tsconfig.json", json.dumps({"compilerOptions": {"target": "es2020",
               "paths": {"@app/*": ["src/*"]}}, "include": ["src"]}))
    assert ts["buildTool"] == "typescript" and ts["compilerConfig"]["paths"] == ["@app/*"]
    gj = _meta("data.json", json.dumps({"a": 1, "b": 2}))
    assert gj["category"] == "json" and set(gj["topLevelKeys"]) == {"a", "b"}


def test_multi_document_yaml() -> None:
    # Multi-doc YAML (k8s manifests with `---`) must not error into metadata.
    manifest = ("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: svc\n---\n"
                "apiVersion: v1\nkind: Service\nmetadata:\n  name: svc\n")
    md = _meta("deploy.yaml", manifest)
    assert "parseError" not in md
    assert md["documentCount"] == 2
    assert md["resourceKinds"] == ["Deployment", "Service"]
    assert set(md["topLevelKeys"]) == {"apiVersion", "kind", "metadata"}


def test_matches_patterns() -> None:
    p = ConfigParser()
    assert p.matches("package.json") and p.matches("x/Dockerfile") and p.matches("a.yml")
    assert p.matches("Dockerfile.dev") and p.matches(".env.local")  # glob-style
    assert p.matches("src/Svc.csproj") and p.matches("App.sln")  # .NET manifests
    assert not p.matches("main.py") and not p.matches("app.ts")


def test_pipeline_captures_config_and_aggregates(tmp_path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18"}}))
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 8000\n")
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")

    from breezeai_cog.services import AnalysisService
    result = AnalysisService(Settings(jobs=1, out=tmp_path / "out")).analyze_repo(tmp_path)

    lines = gzip.open(result.out_path, "rt").read().splitlines()
    records = [json.loads(x) for x in lines]
    meta, files = records[0], records[1:]
    kinds = {f["path"]: f.get("metadata", {}).get("kind") for f in files if f["type"] == "config"}
    assert kinds.get("package.json") == "package.json" and kinds.get("Dockerfile") == "dockerfile"
    cfg = meta["configs"]
    assert cfg["totalConfigFiles"] == 2 and "npm" in cfg["packageManagers"]
    assert cfg["docker"]["hasDockerfile"] and "8000" in cfg["docker"]["exposedPorts"]
    assert meta["analyzedLanguages"] == ["python"]  # config not a language


def test_config_registered_and_selected() -> None:
    from breezeai_cog.core.registry import discover_builtin
    discover_builtin()
    assert "config" in registry.capabilities()["languages"]
    assert registry.select("package.json", b'{"name":"x"}').name == "config"
