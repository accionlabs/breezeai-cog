"""ORM / database method-call detection (language-agnostic).

Returns an inferred ``dataAccessHint`` (the DB/ORM) for a normalized call, else
``None``. Detection is heuristic (``dataAccessHint`` is documented as inferred):
distinctive method names map directly; ambiguous ORM verbs (``findOne``/``save``…)
are refined by receiver hints.
"""

from __future__ import annotations

# Distinctive method (lowercased) -> dataAccessHint.
_DISTINCTIVE = {
    "findall": "sequelize", "findbypk": "sequelize", "bulkcreate": "sequelize", "findorcreate": "sequelize",
    "findfirst": "prisma", "findunique": "prisma", "findmany": "prisma", "upsert": "prisma",
    "aggregate": "mongodb", "insertmany": "mongodb", "insertone": "mongodb",
    "updateone": "mongodb", "updatemany": "mongodb", "deleteone": "mongodb", "deletemany": "mongodb",
    "findoneandupdate": "mongodb", "findbyidandupdate": "mongodb",
    "createquerybuilder": "typeorm", "getrepository": "typeorm",
    "executemany": "sql", "filter_by": "sqlalchemy",
}

# Ambiguous ORM verbs -> generic hint, refined by receiver below.
_GENERIC = {
    "findone", "findbyid", "find", "save", "create", "update", "delete", "remove",
    "persist", "merge", "query", "execute",
}

# Receiver substring -> hint (refines _GENERIC).
_RECEIVER_HINTS = (
    ("prisma", "prisma"), ("sequelize", "sequelize"), ("mongoose", "mongodb"),
    ("repository", "typeorm"), ("repo", "typeorm"), ("entitymanager", "typeorm"),
    ("session", "sqlalchemy"), ("queryset", "django"), ("objects", "django"),
)

# Django queryset verbs.
_DJANGO_VERBS = {"filter", "get", "all", "exclude", "annotate", "values"}


def match_db(callee: str, method: str) -> str | None:
    m = method.lower()
    low = callee.lower()
    if m in _DISTINCTIVE:
        return _DISTINCTIVE[m]
    if m in _GENERIC:
        for needle, hint in _RECEIVER_HINTS:
            if needle in low:
                return hint
        return "orm"
    if m in _DJANGO_VERBS and ("objects" in low or "queryset" in low):
        return "django"
    return None
