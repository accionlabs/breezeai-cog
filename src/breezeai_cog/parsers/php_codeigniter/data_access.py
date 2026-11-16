"""CodeIgniter data access (``db_method_call``): the query builder and models. The rules shared by
every PHP data-access detector are in ``parsers/php_data_access_common.py``."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..php.class_ancestors import kinds_by_base
from ..php.receivers import Rules
from ..php_data_access_common import Kinds, detect_data_access

#: CodeIgniter base classes → the kind of class that extends them.
_BASES = {"CodeIgniter\\Model": "ci-model"}
#: Types that are a database handle themselves.
_HANDLES = {
    "CodeIgniter\\Database\\BaseConnection": "ci-db",
    "CodeIgniter\\Database\\ConnectionInterface": "ci-db",
    "CodeIgniter\\Database\\BaseBuilder": "ci-db",
}

_CI_BUILDER = {
    "table", "builder", "query", "simplequery", "get", "getwhere", "insert", "insertbatch",
    "update", "updatebatch", "delete", "replace", "countall", "countallresults", "emptytable",
    "truncate", "select", "where", "wherein", "orwhere", "like", "join", "orderby", "groupby",
    "limit", "set",
}
_CI_MODEL = _CI_BUILDER | {"find", "findall", "first", "save", "findcolumn", "paginate", "chunk"}

_KINDS: Kinds = {
    "ci-db": (_CI_BUILDER, "codeigniter-builder"),
    "ci-model": (_CI_MODEL, "codeigniter-builder"),
}


def _static(fqcn: str, method: str) -> str | None:
    return "ci-db" if fqcn == "Config\\Database" and method == "connect" else None


def _chain_result(kind: str, methods: list[str]) -> str | None:
    """``$builder = $this->db->table('x')`` is a builder. Other chain results are values, not
    handles."""
    return kind if kind == "ci-db" else None


def _detect(dc: DetectContext) -> Detection | None:
    models = kinds_by_base(dc.ctx.resolution_index, _BASES)
    if not models and not any(marker in dc.source for marker in (
            b"CodeIgniter\\", b"db_connect", b"Config\\Database")):
        return None
    ci_file = b"CodeIgniter\\" in dc.source
    rules = Rules(
        types={**models, **_HANDLES},
        static=_static,
        function=lambda name: "ci-db" if name == "db_connect" else None,
        this_property=lambda name: "ci-db" if name == "db" and ci_file else None,
        chain_result=_chain_result,
    )
    return detect_data_access(dc, rules, _KINDS)


register_detector(Detector(
    name="codeigniter-data-access",
    gate=Gate("php", order=42),
    detect=_detect,
))
