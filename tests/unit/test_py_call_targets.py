"""Python ``calls[].path`` names only a file that declares what is called (BREEZEAI-1653,
``parsers/python/call_targets.py``): a package re-export is followed to the declaring file, a
submodule imported from its package is that module, and a member of an imported object keeps the
import's file only when that file declares it."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog import iter_file_records


@pytest.fixture
def calls_of(tmp_path: Path):
    def run(files: dict[str, str]) -> dict[str, list[tuple[str, str | None]]]:
        repo = tmp_path / "repo"
        for rel, src in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        out: dict[str, list[tuple[str, str | None]]] = {}
        for rec in iter_file_records(repo):
            for fn in rec.functions:
                out[fn.name] = [(c.name, c.path) for c in fn.calls]
        return out

    return run


def test_package_reexport_resolves_to_the_declaring_file(calls_of) -> None:
    got = calls_of({
        "pkg/__init__.py": "from .client import run_call\nfrom .tools import *\n",
        "pkg/client.py": "def run_call():\n    return 1\n",
        "pkg/tools.py": "def search_tool():\n    return 2\n",
        "app.py": "from pkg import run_call, search_tool\n\n"
                  "def main():\n    run_call()\n    search_tool()\n",
    })
    assert ("run_call", "pkg/client.py") in got["main"]
    assert ("search_tool", "pkg/tools.py") in got["main"]  # through `import *`


def test_submodule_imported_from_its_package(calls_of) -> None:
    got = calls_of({
        "pkg/__init__.py": "",
        "pkg/a.py": "def save():\n    return 1\n",
        "app.py": "from pkg import a\nfrom . import helpers\n\n"
                  "def main():\n    a.save()\n    a.missing()\n    helpers.go()\n",
        "helpers.py": "def go():\n    return 1\n",
        "__init__.py": "",
    })
    assert ("save", "pkg/a.py") in got["main"]
    assert ("missing", None) in got["main"]
    assert ("go", "helpers.py") in got["main"]


def test_method_of_an_imported_object_has_no_path(calls_of) -> None:
    got = calls_of({
        "utils/__init__.py": "",
        "utils/logger.py": "import logging\nlogger = logging.getLogger('x')\n\n"
                           "def setup():\n    return logger\n",
        "app.py": "from utils.logger import logger, setup\n\n"
                  "def main():\n    logger.info('x')\n    setup()\n",
    })
    assert ("info", None) in got["main"]
    assert ("setup", "utils/logger.py") in got["main"]


def test_class_and_its_method_keep_their_file(calls_of) -> None:
    got = calls_of({
        "svc.py": "class Service:\n    @staticmethod\n    def make():\n        return 1\n",
        "app.py": "from svc import Service\n\n"
                  "def main():\n    Service()\n    Service.make()\n",
    })
    assert ("Service", "svc.py") in got["main"]
    assert ("make", "svc.py") in got["main"]


def test_import_module_and_same_file_unchanged(calls_of) -> None:
    got = calls_of({
        "lib/__init__.py": "",
        "lib/util.py": "def norm(x):\n    return x\n",
        "app.py": "import lib.util\n\ndef helper():\n    return 1\n\n"
                  "def main():\n    lib.util.norm(1)\n    helper()\n",
    })
    assert ("norm", "lib/util.py") in got["main"]
    assert ("helper", "app.py") in got["main"]
