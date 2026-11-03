"""English singular / plural of a resource name, for frameworks that derive a URL parameter or a
controller from it (Rails ``resources :photos`` → ``:photo_id``, Laravel ``Route::resource('photos')``
→ ``{photo}``). Both return None when the rules are not sure: a guessed parameter name would be a
wrong address, and an empty one is honest."""

from __future__ import annotations

#: Irregular nouns (singular ↔ plural) that the suffix rules below get wrong.
_IRREGULAR = {"person": "people", "child": "children", "man": "men", "woman": "women",
              "mouse": "mice", "goose": "geese", "foot": "feet", "tooth": "teeth"}
_IRREGULAR_PLURAL = {v: k for k, v in _IRREGULAR.items()}
#: Words that are their own singular and plural (both ActiveSupport and Laravel's inflector).
_UNCOUNTABLE = {"news", "series", "species", "sheep", "fish", "deer", "equipment", "information"}


def singular(name: str) -> str | None:
    """``photos`` → ``photo``, ``categories`` → ``category``, ``people`` → ``person``,
    ``boxes`` → ``box``, ``news`` → ``news``. None when unsure — including ``-ses`` words, where ``statuses`` →
    ``status`` but ``cases`` → ``case``."""
    if name in _UNCOUNTABLE:
        return name
    if name in _IRREGULAR_PLURAL:
        return _IRREGULAR_PLURAL[name]
    if name.endswith("ies") and len(name) > 3:
        return name[:-3] + "y"
    if name.endswith(("sses", "shes", "ches", "xes", "zes")):
        return name[:-2]
    if name.endswith("ses"):
        return None
    if name.endswith("s") and not name.endswith(("ss", "us", "is")):
        return name[:-1]
    return None


def plural(name: str) -> str | None:
    """``profile`` → ``profiles`` (a singular Rails ``resource`` is served by the plural
    controller). None when unsure."""
    if name in _UNCOUNTABLE:
        return name
    if name in _IRREGULAR:
        return _IRREGULAR[name]
    if name.endswith("y") and len(name) > 1 and name[-2] not in "aeiou":
        return name[:-1] + "ies"
    if name.endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s" if name and name[-1].isalpha() else None
