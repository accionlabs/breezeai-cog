"""What Django's own views and checks do, as tables — read from Django's documentation, never
from the repository.

* :data:`DJANGO_VIEWS` — the HTTP methods each generic class-based view handles, so a view that
  inherits them (``class OrderList(ListView)``) still gets one record per verb.
* :data:`CERTAIN_AUTH` — the checks that reject an anonymous user before the view runs: the only
  ones that set ``authRequired``.
* :data:`NOT_GUARDS` — decorators that change the response or describe the view, not who may call it.
"""

from __future__ import annotations

#: Django's generic views (``django.views.generic`` and ``django.views``) → the verbs they handle,
#: each by the method of that name. ``RedirectView`` answers every verb (``ANY``).
DJANGO_VIEWS: dict[str, tuple[str, ...]] = {
    "TemplateView": ("GET",),
    "ListView": ("GET",),
    "DetailView": ("GET",),
    "ArchiveIndexView": ("GET",),
    "YearArchiveView": ("GET",),
    "MonthArchiveView": ("GET",),
    "WeekArchiveView": ("GET",),
    "DayArchiveView": ("GET",),
    "TodayArchiveView": ("GET",),
    "DateDetailView": ("GET",),
    "FormView": ("GET", "POST", "PUT"),
    "CreateView": ("GET", "POST", "PUT"),
    "UpdateView": ("GET", "POST", "PUT"),
    "DeleteView": ("GET", "POST", "DELETE"),
    "RedirectView": ("ANY",),
}
#: Views Django itself provides that a urlconf names directly (``path("m/<path:path>", serve)``,
#: ``path("reset/", PasswordResetView.as_view())``): served by Django's code, so their records
#: lead to nothing in the repo. Names in ``django.views`` and ``django.contrib.*.views``.
DJANGO_PROVIDED_VIEWS = frozenset({
    "serve", "page_not_found", "server_error", "bad_request", "permission_denied",
    "set_language", "JavaScriptCatalog", "JSONCatalog", "sitemap", "shortcut", "flatpage",
    "LoginView", "LogoutView", "PasswordChangeView", "PasswordChangeDoneView", "PasswordResetView",
    "PasswordResetDoneView", "PasswordResetConfirmView", "PasswordResetCompleteView",
})
#: Django bases that handle no verb themselves: a view built on them handles what it defines.
DJANGO_BASES = frozenset({"View", "ContextMixin", "TemplateResponseMixin", "SingleObjectMixin",
                          "MultipleObjectMixin", "FormMixin", "ModelFormMixin", "ProcessFormView"})
#: Every view name Django serves itself, as a record's ``handler`` (not located: nothing in the repo).
DJANGO_SERVED = DJANGO_PROVIDED_VIEWS | DJANGO_VIEWS.keys() | DJANGO_BASES
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
