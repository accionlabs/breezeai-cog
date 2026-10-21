"""Fastify route detection. Like Express, Fastify is a substrate (an app can serve NestJS or
plain routes on it), so detection runs **additively** for every TS / JS file through the
detector registry (``routes.py``); prefixes from ``register`` and ``@fastify/autoload`` reach the
routes through the ``fastify.plugin-prefixes`` fact (``plugins.py``)."""
