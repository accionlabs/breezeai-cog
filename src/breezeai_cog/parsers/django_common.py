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
