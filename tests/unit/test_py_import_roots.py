"""A package folder the code shows is on ``sys.path`` resolves absolute imports
(``parsers/python/imports.py``, :func:`import_roots`): Airflow's ``plugins/``, a pytest ``tests/``."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog import iter_file_records


def _records(tmp_path: Path, files: dict[str, str]):
    repo = tmp_path / "repo"
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return {r.path: r for r in iter_file_records(repo)}


PLUGINS = {
    "app/pyproject.toml": "[project]\nname='app'\n",
    "app/plugins/__init__.py": "",
    "app/plugins/strategies/__init__.py": "",
    "app/plugins/strategies/base.py": "class CrawlStrategy:\n    def crawl(self, s):\n        return s\n",
    "app/plugins/strategies/site/__init__.py": "",
    "app/plugins/strategies/site/crawl.py": (
        "from strategies.base import CrawlStrategy\nfrom utils import clean\nimport logging\n\n"
        "class SiteCrawl(CrawlStrategy):\n    def run(self, s):\n        return clean(s)\n"),
    "app/plugins/utils/__init__.py": "def clean(x):\n    return x\n",
    "app/plugins/logging.py": "def info(x):\n    return x\n",
}


def test_plugins_folder_is_a_root(tmp_path: Path) -> None:
    recs = _records(tmp_path, PLUGINS)
    crawl = recs["app/plugins/strategies/site/crawl.py"]
    assert "app/plugins/strategies/base.py" in crawl.importFiles
    assert "app/plugins/utils/__init__.py" in crawl.importFiles  # a single name too
    (run,) = [f for f in crawl.functions if f.name == "run"]
    assert ("clean", "app/plugins/utils/__init__.py") in [(c.name, c.path) for c in run.calls]


def test_standard_library_name_is_never_a_local_file(tmp_path: Path) -> None:
    crawl = _records(tmp_path, PLUGINS)["app/plugins/strategies/site/crawl.py"]
    assert "app/plugins/logging.py" not in crawl.importFiles
    assert "logging" in crawl.externalImports


def test_package_folder_without_evidence_is_no_root(tmp_path: Path) -> None:
    # nothing under lib/ imports `pkg` without `lib.`: `utils` stays external
    recs = _records(tmp_path, {
        "lib/__init__.py": "",
        "lib/pkg/__init__.py": "",
        "lib/pkg/a.py": "from lib.utils import x\nfrom utils import y\n",
        "lib/utils.py": "x = 1\ny = 2\n",
    })
    a = recs["lib/pkg/a.py"]
    assert "lib/utils.py" in a.importFiles        # the repository root resolves `lib.utils`
    assert "utils" in a.externalImports               # no evidence that lib/ is on sys.path


def test_module_under_two_roots_stays_external(tmp_path: Path) -> None:
    files = {
        "p/__init__.py": "",
        "p/q/__init__.py": "",
        "p/q/strategies/__init__.py": "",
        "p/q/strategies/a.py": "from strategies.b import f\nfrom helpers import g\n",
        "p/q/strategies/b.py": "def f():\n    return 1\n",
        "p/q/helpers.py": "def g():\n    return 1\n",
        "p/qq/__init__.py": "",
        "p/qq/q/__init__.py": "",
        "p/helpers.py": "def g():\n    return 2\n",
        "p/q2/__init__.py": "",
        "p/q2/x.py": "from q2 import y\n",
    }
    a = _records(tmp_path, files)["p/q/strategies/a.py"]
    assert "p/q/strategies/b.py" in a.importFiles
    # `helpers` is in p/q/ and in p/, both roots by evidence (p/q2 imports q2): unsure
    assert "helpers" in a.externalImports
