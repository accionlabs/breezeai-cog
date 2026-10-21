"""Python import roots: an absolute import resolves where Python could find it from the file —
the repository root, then (dotted) a non-package folder above it or (single-part) the script's
own folder, plus a declared project's ``src/`` — and only when exactly one place has it."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.python.parser import PythonParser


def _imports(tmp_path: Path, files: dict[str, str], path: str) -> tuple[list[str], list[str]]:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    source = (tmp_path / path).read_bytes()
    rec = PythonParser().parse_file(ParseContext(
        path=path, abs_path=tmp_path / path, source=source, repo_root=tmp_path))
    return rec.importFiles, rec.externalImports


def test_dotted_import_resolves_under_the_app_folder_not_a_sibling_app(tmp_path) -> None:
    files = {
        "etl/dags/eu/plan.py": "from src.constants import REGION\nimport airflow.models\n",
        "etl/src/__init__.py": "", "etl/src/constants.py": "REGION = 'eu'\n",
        "harvester/src/__init__.py": "", "harvester/src/constants.py": "REGION = 'x'\n",
    }
    internal, external = _imports(tmp_path, files, "etl/dags/eu/plan.py")
    assert internal == ["etl/src/constants.py"]
    assert external == ["airflow.models"]  # an installed package stays external


def test_root_still_wins(tmp_path) -> None:
    files = {"app/main.py": "from src.constants import X\n",
             "src/__init__.py": "", "src/constants.py": "X = 1\n",
             "app/src/__init__.py": "", "app/src/constants.py": "X = 2\n"}
    internal, _ = _imports(tmp_path, files, "app/main.py")
    assert internal == ["src/constants.py"]


def test_two_folders_providing_the_module_is_ambiguous(tmp_path) -> None:
    files = {"a/b/job.py": "from lib.tools import run\n",
             "a/lib/__init__.py": "", "a/lib/tools.py": "def run(): ...\n",
             "a/b/lib/__init__.py": "", "a/b/lib/tools.py": "def run(): ...\n"}
    internal, external = _imports(tmp_path, files, "a/b/job.py")
    assert internal == [] and external == ["lib.tools"]


def test_single_part_import_resolves_only_from_a_script_folder(tmp_path) -> None:
    files = {"project/main.py": "from worker import create_task\nimport utils\n",
             "project/worker.py": "def create_task(): ...\n",
             "utils.py": "",  # repo root: tried first
             "pkg/__init__.py": "", "pkg/inner.py": "import worker\nimport helpers\n",
             "helpers.py": ""}
    internal, _ = _imports(tmp_path, files, "project/main.py")
    assert internal == ["project/worker.py", "utils.py"]
    # inside a package, a single name is not looked for in the folders above or beside it
    internal, external = _imports(tmp_path, files, "pkg/inner.py")
    assert internal == ["helpers.py"] and external == ["worker"]


def test_single_part_name_is_not_looked_for_above_the_file(tmp_path) -> None:
    # `import transformers` means the installed package unless the script's own folder has it
    files = {"svc/app/run.py": "import transformers\n", "svc/transformers.py": ""}
    internal, external = _imports(tmp_path, files, "svc/app/run.py")
    assert internal == [] and external == ["transformers"]


def test_declared_project_src_layout(tmp_path) -> None:
    files = {"libs/core/pyproject.toml": "[project]\nname='core'\n",
             "libs/core/src/core/__init__.py": "", "libs/core/src/core/models.py": "",
             "libs/core/tests/test_models.py": "from core.models import Order\nimport core\n"}
    internal, _ = _imports(tmp_path, files, "libs/core/tests/test_models.py")
    assert internal == ["libs/core/src/core/models.py", "libs/core/src/core/__init__.py"]
