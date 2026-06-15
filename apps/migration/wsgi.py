"""
wsgi.py — Production WSGI entry point.

Mounts the Flask app under URL_PREFIX (default: /migrate) so it can be
served behind the Plane reverse proxy without any path rewriting:
url_for(), request.script_root and static files all resolve to
/migrate/... automatically.

Usage: gunicorn --bind 0.0.0.0:5050 wsgi:application
"""

import os

from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix

from app import create_app

URL_PREFIX = os.environ.get("URL_PREFIX", "/migrate").rstrip("/")

flask_app = create_app()
flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_for=1, x_proto=1, x_host=1)

if URL_PREFIX:

    def _redirect_to_prefix(environ, start_response):
        start_response("302 Found", [("Location", URL_PREFIX + "/")])
        return [b""]

    application = DispatcherMiddleware(_redirect_to_prefix, {URL_PREFIX: flask_app})
else:
    application = flask_app
