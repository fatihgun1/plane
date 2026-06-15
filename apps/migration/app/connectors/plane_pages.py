"""
app/connectors/plane_pages.py
=============================
Plane Pages client over the session-authenticated **app** API (Pages are not
exposed in the external v1 API). Proven against the live API:

- Page create needs the Django CSRF token + cookie AND a matching Referer header.
- `parent` is NOT accepted on create (returns 404) — create first, then PATCH
  the page to set its parent.
- Sending only `description_html` is enough; Plane (live/editor) converts it to
  the collaborative binary on first open.

The background worker has no Flask request context, so it builds this with the
session cookie persisted on the job.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_WRITE_INTERVAL = 0.4  # gentle spacing between writes


class PlanePagesError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Plane Pages API {status}: {message}")


class PlanePagesConnector:
    def __init__(self, base_url: str, session_cookie: str, cookie_name: str = "session-id",
                 timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_cookie = session_cookie
        self.cookie_name = cookie_name
        self.timeout = timeout
        self._csrf: str | None = None
        self._last_write = 0.0

    # ------------------------------------------------------------------
    def _csrf_token(self) -> str:
        if self._csrf is None:
            req = urllib.request.Request(
                f"{self.base_url}/auth/get-csrf-token/",
                headers={"Cookie": f"{self.cookie_name}={self.session_cookie}",
                         "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._csrf = (json.loads(resp.read() or b"{}")).get("csrf_token", "")
        return self._csrf

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json",
                   "Cookie": f"{self.cookie_name}={self.session_cookie}"}
        if method in ("POST", "PATCH", "PUT", "DELETE"):
            csrf = self._csrf_token()
            headers["Cookie"] = f"{self.cookie_name}={self.session_cookie}; csrftoken={csrf}"
            headers["X-CSRFToken"] = csrf
            headers["Content-Type"] = "application/json"
            headers["Referer"] = f"{self.base_url}/"  # required by Django CSRF
        backoff = 1.0
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                payload = exc.read().decode(errors="replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise PlanePagesError(exc.code, payload[:400])
        raise PlanePagesError(0, f"retries exhausted for {path}")

    def _throttle(self) -> None:
        wait = self._last_write + _WRITE_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_write = time.time()

    # ------------------------------------------------------------------
    def _pages_path(self, slug: str, project_id: str) -> str:
        return f"/api/workspaces/{slug}/projects/{project_id}/pages/"

    def create_page(self, slug: str, project_id: str, name: str, html: str) -> str:
        self._throttle()
        _, data = self._request("POST", self._pages_path(slug, project_id),
                                {"name": name or "(untitled)",
                                 "description_html": html or "<p></p>"})
        return str(data["id"])

    def set_parent(self, slug: str, project_id: str, page_id: str, parent_id: str) -> None:
        self._throttle()
        self._request("PATCH", f"{self._pages_path(slug, project_id)}{page_id}/",
                      {"parent": parent_id})

    def update_description(self, slug: str, project_id: str, page_id: str, html: str) -> None:
        self._throttle()
        self._request("PATCH", f"{self._pages_path(slug, project_id)}{page_id}/",
                      {"description_html": html or "<p></p>"})
