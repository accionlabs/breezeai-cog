"""What Django's own views and checks do, as tables — read from Django's documentation, never
from the repository.

* :data:`CERTAIN_AUTH` — the checks that reject an anonymous user before the view runs: the only
  ones that set ``authRequired``.
* :data:`NOT_GUARDS` — decorators that change the response or describe the view, not who may call it.
"""

from __future__ import annotations

#: The modules Django's views are imported from.
DJANGO_VIEW_MODULES = ("django.views",)

#: HTTP verbs a class-based view dispatches to a method of the same (lower-case) name.
VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE")

#: Checks that reject an anonymous caller, by dotted origin.
CERTAIN_AUTH = frozenset({
    "django.contrib.auth.decorators.login_required",
    "django.contrib.auth.decorators.permission_required",
    "django.contrib.admin.views.decorators.staff_member_required",
    "django.contrib.auth.mixins.LoginRequiredMixin",
    "django.contrib.auth.mixins.PermissionRequiredMixin",
})
#: The auth mixins that are guards when a class-based view inherits them.
AUTH_MIXINS = frozenset({"LoginRequiredMixin", "PermissionRequiredMixin", "UserPassesTestMixin",
                         "AccessMixin"})
AUTH_MIXIN_MODULE = "django.contrib.auth.mixins"
#: Django 5.1's middleware that requires a login for every view not marked
#: ``@login_not_required``.
LOGIN_REQUIRED_MIDDLEWARE = "django.contrib.auth.middleware.LoginRequiredMiddleware"
LOGIN_NOT_REQUIRED = "django.contrib.auth.decorators.login_not_required"

#: Decorators that restrict the HTTP verbs of a function view: name → verbs (None: read the list
#: argument of ``require_http_methods``).
METHOD_DECORATORS: dict[str, tuple[str, ...] | None] = {
    "require_http_methods": None,
    "require_GET": ("GET",),
    "require_POST": ("POST",),
    "require_safe": ("GET", "HEAD"),
}

#: Decorators that are not guards: response shaping, caching, CSRF exemption, method
#: restriction, documentation, and the decorators Python and DRF use to configure a view.
NOT_GUARDS = frozenset({
    *METHOD_DECORATORS,
    "csrf_exempt", "ensure_csrf_cookie", "requires_csrf_token",
    "cache_page", "cache_control", "never_cache", "vary_on_headers", "vary_on_cookie",
    "gzip_page", "condition", "etag", "last_modified", "conditional_page",
    "xframe_options_exempt", "xframe_options_deny", "xframe_options_sameorigin",
    "sensitive_post_parameters", "sensitive_variables", "login_not_required",
    "method_decorator", "staticmethod", "classmethod", "property", "atomic", "transaction.atomic",
    "api_view", "action", "permission_classes", "authentication_classes", "throttle_classes",
    "renderer_classes", "parser_classes", "schema", "extend_schema", "extend_schema_view",
    "swagger_auto_schema",
})


def short(name: str) -> str:
    """The last part of a dotted name (``generic.ListView`` → ``ListView``)."""
    return name.rsplit(".", 1)[-1]
