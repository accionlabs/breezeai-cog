"""Backbone.js capture for TypeScript/JavaScript - additive detectors, no parser of its own.

``backbone`` finds model/collection API calls and views (and registers the cross-file base
class fact); ``routes`` finds router route tables, ``router.route(...)`` and the history root.
"""

from . import backbone, routes  # noqa: F401  (each registers its detector)

PARSERS: list = []
