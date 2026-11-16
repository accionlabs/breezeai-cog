"""Doctrine ORM data access (``db_method_call``): the entity manager and repositories. The rules
shared by every PHP data-access detector are in ``parsers/php_data_access_common.py``."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..php.class_ancestors import kinds_by_base
from ..php.receivers import Rules
from ..php_data_access_common import Kinds, detect_data_access

#: Doctrine base classes → the kind of class that extends them.
_BASES = {
    "Doctrine\\Bundle\\DoctrineBundle\\Repository\\ServiceEntityRepository": "doctrine-repo",
    "Doctrine\\ORM\\EntityRepository": "doctrine-repo",
}
#: Types that are a database handle themselves.
_HANDLES = {
    "Doctrine\\ORM\\EntityManagerInterface": "doctrine-em",
    "Doctrine\\ORM\\EntityManager": "doctrine-em",
    "Doctrine\\Persistence\\ObjectManager": "doctrine-em",
    "Doctrine\\Common\\Persistence\\ObjectManager": "doctrine-em",
    "Doctrine\\Persistence\\ManagerRegistry": "doctrine-em",
}

_DOCTRINE_EM = {
    "persist", "remove", "flush", "find", "createquerybuilder", "createquery", "createnativequery",
    "refresh", "lock", "transactional", "wrapintransaction",
}
_DOCTRINE_REPO = {"find", "findall", "findby", "findoneby", "count", "createquerybuilder", "matching"}

_KINDS: Kinds = {
    "doctrine-em": (_DOCTRINE_EM | _DOCTRINE_REPO, "doctrine"),
    "doctrine-repo": (_DOCTRINE_REPO, "doctrine"),
}


def _this_call(class_kind: str | None, method: str) -> str | None:
    if class_kind != "doctrine-repo":
        return None
    return "doctrine-em" if method == "getentitymanager" else class_kind


def _chain_result(kind: str, methods: list[str]) -> str | None:
    """``$repo = $em->getRepository(X::class)`` is a repository. Other chain results are values,
    not handles."""
    return "doctrine-repo" if kind == "doctrine-em" and methods[-1] == "getrepository" else None


def _detect(dc: DetectContext) -> Detection | None:
    models = kinds_by_base(dc.ctx.resolution_index, _BASES)
    if not models and b"Doctrine\\" not in dc.source:
        return None
    rules = Rules(types={**models, **_HANDLES}, this_call=_this_call, chain_result=_chain_result)
    return detect_data_access(dc, rules, _KINDS)


register_detector(Detector(
    name="doctrine-data-access",
    gate=Gate("php", order=41),
    detect=_detect,
))
