"""Regression tests: verify PHP ORM hints (eloquent, doctrine) do not leak into other languages."""

from __future__ import annotations

from breezeai_cog.parsers.detection import classify_call, match_db


def test_no_php_hints_in_typescript() -> None:
    # TS TypeORM / generic repository calls
    res1 = match_db("this.userRepository.find", "find", language="typescript")
    assert res1 != "eloquent"
    assert res1 != "doctrine"

    res2 = match_db("entityManager.getRepository", "getRepository", language="typescript")
    assert res2 == "typeorm"

    res3 = match_db("user.save", "save", language="typescript")
    assert res3 != "eloquent"

    res4 = match_db("User.where", "where", language="typescript")
    assert res4 != "eloquent"

    cls = classify_call("this.userRepo.find", "find", language="typescript")
    if cls is not None:
        assert cls[2] != "eloquent"
        assert cls[2] != "doctrine"


def test_no_php_hints_in_python() -> None:
    res1 = match_db("User.objects.filter", "filter", language="python")
    assert res1 == "django"

    res2 = match_db("user.save", "save", language="python")
    assert res2 != "eloquent"

    res3 = match_db("session.query", "query", language="python")
    assert res3 == "sqlalchemy"


def test_no_php_hints_in_java() -> None:
    res1 = match_db("userRepository.findById", "findById", language="java")
    assert res1 != "eloquent"
    assert res1 != "doctrine"

    res2 = match_db("userRepository.save", "save", language="java")
    assert res2 != "eloquent"


def test_no_php_hints_in_csharp() -> None:
    res1 = match_db("repo.GetById", "GetById", language="csharp")
    assert res1 != "eloquent"
    assert res1 != "doctrine"


def test_php_hints_for_php() -> None:
    # Static model call in PHP
    res1 = match_db("User::find", "find", language="php")
    assert res1 == "eloquent"

    res2 = match_db("User::where", "where", language="php")
    assert res2 == "eloquent"

    res3 = match_db("$user->save", "save", language="php")
    assert res3 == "eloquent"

    res4 = match_db("$entityManager->getRepository", "getRepository", language="php")
    assert res4 == "doctrine"

    res5 = match_db("$entityManager.getRepository.find", "find", language="php")
    assert res5 == "doctrine"
