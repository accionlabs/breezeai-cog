"""AngularJS 1.x detection: ``$routeProvider`` (ngRoute) and ui-router routes, controller /
component / directive registrations, and ``$resource`` calls.

Like Express and Fastify, AngularJS is a substrate rather than a file owner: a hybrid app
(``@angular/upgrade`` next to ``angular.module(…)``) is owned by the Angular parser and still
holds AngularJS routes. So detection runs **additively** for every TS / JS file through the
detector registry (``detector.py``); what one file declares for another (a controller registered
elsewhere, a parent state) reaches it through the ``angularjs.registrations`` and
``angularjs.states`` facts. Role table: ``docs/entry-point-roles/angularjs.md``."""
