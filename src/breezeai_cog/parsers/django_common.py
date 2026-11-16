"""The view names Django serves itself, as data, shared by the Django parser and the entry-point
contract (:mod:`.contract`). It lives beside the parsers, not in the Django package, so core
never imports a framework package (architecture §15).

* ``DJANGO_VIEWS`` — the HTTP methods each generic class-based view handles, so a view that
  inherits them (``class OrderList(ListView)``) still gets one record per verb.
* ``DJANGO_PROVIDED_VIEWS`` — views a urlconf names directly that Django's own code serves.
* ``DJANGO_BASES`` — Django bases that handle no verb themselves.
* ``DJANGO_SERVED`` — every name above, as a record's ``handler`` that nothing in the repo defines.
"""

from __future__ import annotations

#: Django's class-based views (``django.views``, ``django.views.generic``, ``django.views.i18n`` and
#: ``django.contrib.auth.views``) → the verbs they handle, each by the method of that name.
#: ``RedirectView`` answers every verb (``ANY``). The auth views are built on ``FormView`` /
#: ``TemplateView`` and handle what those do.
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
    "JavaScriptCatalog": ("GET",),
    "JSONCatalog": ("GET",),
    "LoginView": ("GET", "POST", "PUT"),
    "PasswordChangeView": ("GET", "POST", "PUT"),
    "PasswordChangeDoneView": ("GET",),
    "PasswordResetView": ("GET", "POST", "PUT"),
    "PasswordResetDoneView": ("GET",),
    "PasswordResetConfirmView": ("GET", "POST", "PUT"),
    "PasswordResetCompleteView": ("GET",),
}
#: Views Django itself provides that a urlconf names directly (``path("m/<path:path>", serve)``):
#: served by Django's code, so their records lead to nothing in the repo; those not in
#: :data:`DJANGO_VIEWS` have no verb recorded — function views answer any, and ``LogoutView``
#: handles ``POST`` only from Django 5 (``GET`` too before), so which verbs it has depends on the
#: version in use. Names in ``django.views`` and ``django.contrib.*.views``.
DJANGO_PROVIDED_VIEWS = frozenset({
    "serve", "page_not_found", "server_error", "bad_request", "permission_denied",
    "set_language", "sitemap", "shortcut", "flatpage", "LogoutView",
})
#: Django bases that handle no verb themselves: a view built on them handles what it defines.
DJANGO_BASES = frozenset({"View", "ContextMixin", "TemplateResponseMixin", "SingleObjectMixin",
                          "MultipleObjectMixin", "FormMixin", "ModelFormMixin", "ProcessFormView"})
#: Every view name Django serves itself, as a record's ``handler`` (not located: nothing in the repo).
DJANGO_SERVED = DJANGO_PROVIDED_VIEWS | DJANGO_VIEWS.keys() | DJANGO_BASES
