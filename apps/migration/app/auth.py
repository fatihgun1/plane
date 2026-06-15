"""
app/auth.py — Plane session authentication.

Middleware: every request (except /login, /health and static files) must
carry a valid Plane session cookie, which is validated against the Plane
API (GET /api/users/me/) on each request. Unauthenticated page requests
are redirected to the tool's own login page (/login), which signs the
user in through Plane's /auth/sign-in/ endpoint; API requests get a 401.

Env vars
--------
    PLANE_API_URL          Plane API base URL  (default: http://api:8000)
    PLANE_SESSION_COOKIE   Session cookie name (default: session-id)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from flask import (
    Blueprint,
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

logger = logging.getLogger(__name__)

PLANE_API_URL = os.environ.get("PLANE_API_URL", "http://api:8000").rstrip("/")
SESSION_COOKIE = os.environ.get("PLANE_SESSION_COOKIE", "session-id")

_EXEMPT_ENDPOINTS = {"static", "health", "auth.login", "auth.csrf_relay", "auth.sign_in_relay"}

bp = Blueprint("auth", __name__)


def init_auth(app: Flask) -> None:
    app.register_blueprint(bp)
    app.before_request(_require_plane_user)
    app.after_request(_no_store)
    app.context_processor(lambda: {"current_user": g.get("user")})


def current_user() -> dict:
    """The authenticated Plane user ({id, email, display_name, avatar})."""
    return g.user


@bp.get("/login")
def login():
    """PMO Optimization Tool login page (authenticates via the Plane API)."""
    user = _fetch_user(request.cookies.get(SESSION_COOKIE))
    if user:
        # Only same-site relative paths — prevents open redirects
        next_url = request.args.get("next", "")
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = request.script_root + "/"
        return redirect(next_url)
    return render_template("login.html")


# ---------------------------------------------------------------------------
# Sign-in relay — the browser can't always reach the Plane API directly
# (e.g. dev setup without the reverse proxy), so the login page talks to
# these endpoints and the server forwards to Plane, passing cookies through.
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _relay_cookies(upstream, response: Response) -> Response:
    for set_cookie in upstream.headers.get_all("Set-Cookie") or []:
        response.headers.add("Set-Cookie", set_cookie)
    return response


@bp.get("/auth/csrf")
def csrf_relay():
    """Fetch a CSRF token from the Plane API on the browser's behalf."""
    upstream = urllib.request.urlopen(f"{PLANE_API_URL}/auth/get-csrf-token/", timeout=10)
    resp = Response(upstream.read(), content_type="application/json")
    return _relay_cookies(upstream, resp)


@bp.post("/auth/sign-in")
def sign_in_relay():
    """Forward the login form to Plane's /auth/sign-in/ and report the result.
    Plane answers with a redirect: ?error_code=... on failure, none on success."""
    data = urllib.parse.urlencode(request.form.to_dict()).encode()
    req = urllib.request.Request(
        f"{PLANE_API_URL}/auth/sign-in/",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": request.headers.get("Cookie", ""),
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        upstream = opener.open(req, timeout=10)
    except urllib.error.HTTPError as exc:
        upstream = exc  # 3xx/4xx — headers still carry Location / Set-Cookie

    location = upstream.headers.get("Location", "")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    error_code = query.get("error_code", [None])[0]
    if not location and upstream.code >= 400:
        error_code = str(upstream.code)

    resp = jsonify({"success": error_code is None, "error_code": error_code})
    return _relay_cookies(upstream, resp)


def _no_store(response):
    """Keep authenticated pages out of the browser cache so nothing is
    visible after a Plane logout."""
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


def _require_plane_user():
    if request.endpoint in _EXEMPT_ENDPOINTS:
        return None

    user = _fetch_user(request.cookies.get(SESSION_COOKIE))
    if user:
        g.user = user
        return None

    if request.path.startswith("/api/"):
        return jsonify({"error": "Plane authentication required"}), 401
    return redirect(url_for("auth.login", next=request.script_root + request.path))


def _fetch_user(cookie: str | None) -> dict | None:
    """Validate the session cookie against the Plane API."""
    if not cookie:
        return None
    req = urllib.request.Request(
        f"{PLANE_API_URL}/api/users/me/",
        headers={"Cookie": f"{SESSION_COOKIE}={cookie}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            logger.warning("Plane API auth check failed: HTTP %s", exc.code)
        return None
    except Exception as exc:
        logger.error("Plane API unreachable at %s: %s", PLANE_API_URL, exc)
        return None

    if not data.get("id"):
        return None
    return {
        "id": data["id"],
        "email": data.get("email", ""),
        "display_name": data.get("display_name") or data.get("email", ""),
        "avatar": data.get("avatar_url") or data.get("avatar") or "",
    }
