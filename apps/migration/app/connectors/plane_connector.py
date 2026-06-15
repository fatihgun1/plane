"""
app/connectors/plane_connector.py
=================================
Thin client for the Plane external REST API (v1), authenticated with a
personal API key (X-Api-Key header).

Important Plane v1 behaviours this client codes against (verified in
apps/api/plane/api/views):
- There is no PUT upsert. Create-or-update = POST with external_id +
  external_source; on 409 the body contains the existing record's id,
  which is then PATCHed. States and labels follow the same 409+id pattern.
- Responses carry X-RateLimit-Remaining / X-RateLimit-Reset (unix ts);
  default limit is 60 requests/minute per token.
- List endpoints use cursor pagination: ?per_page=100&cursor=... with
  {results, next_cursor, next_page_results} in the body.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

EXTERNAL_SOURCE = "jira"  # never change — Plane-side idempotency depends on it

# Minimum spacing between write calls (60/min limit → ~1.1 s)
_WRITE_INTERVAL = 1.1


class PlaneApiError(Exception):
    """Raised for unexpected (non-2xx, non-409) Plane API responses."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Plane API {status}: {message}")


class PlaneConnector:
    def __init__(self, base_url: str, token: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._last_write = 0.0
        self._rate_remaining: Optional[int] = None
        self._rate_reset: Optional[int] = None

    # ------------------------------------------------------------------
    # HTTP core
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None,
                 params: dict | None = None, retries: int = 3) -> tuple[int, dict]:
        """Perform a request. Returns (status, parsed body). 409 is returned,
        not raised; 429/5xx are retried with backoff."""
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        backoff = 2.0
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "X-Api-Key": self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self._track_rate(resp.headers)
                    return resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                self._track_rate(exc.headers)
                if exc.code == 409:
                    try:
                        return 409, json.loads(payload or b"{}")
                    except ValueError:
                        return 409, {}
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    wait = backoff
                    retry_after = exc.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        wait = max(wait, int(retry_after))
                    logger.warning("Plane API %s on %s — retrying in %.1fs", exc.code, path, wait)
                    time.sleep(wait)
                    backoff *= 2
                    continue
                raise PlaneApiError(exc.code, payload.decode(errors="replace")[:500])
        raise PlaneApiError(0, f"retries exhausted for {path}")

    def _track_rate(self, headers) -> None:
        try:
            if headers.get("X-RateLimit-Remaining") is not None:
                self._rate_remaining = int(headers["X-RateLimit-Remaining"])
            if headers.get("X-RateLimit-Reset") is not None:
                self._rate_reset = int(headers["X-RateLimit-Reset"])
        except (TypeError, ValueError):
            pass

    def throttle(self) -> None:
        """Block until it's safe to issue the next write call."""
        wait = self._last_write + _WRITE_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        if self._rate_remaining is not None and self._rate_remaining <= 2 and self._rate_reset:
            pause = self._rate_reset - time.time()
            if 0 < pause < 120:
                logger.info("Plane rate limit nearly exhausted — pausing %.0fs", pause)
                time.sleep(pause)
        self._last_write = time.time()

    def get_paginated(self, path: str) -> list[dict]:
        results: list[dict] = []
        cursor = None
        while True:
            params = {"per_page": 100}
            if cursor:
                params["cursor"] = cursor
            _, body = self._request("GET", path, params=params)
            page = body.get("results", body if isinstance(body, list) else [])
            results.extend(page)
            if not body.get("next_page_results"):
                return results
            cursor = body.get("next_cursor")
            if not cursor:
                return results

    # ------------------------------------------------------------------
    # API surface (all paths under /api/v1)
    # ------------------------------------------------------------------

    def get_projects(self, slug: str) -> list[dict]:
        return self.get_paginated(f"/api/v1/workspaces/{slug}/projects/")

    def ensure_project(self, slug: str, name: str, identifier: str) -> tuple[str, bool]:
        """Find a project whose identifier matches *identifier* (the Jira key),
        otherwise create one with the same name + identifier. Plane uppercases
        identifiers (max 12 chars) and project-create 409s carry no id, so we
        match against the project list rather than trusting the conflict body.
        Returns (project_id, created)."""
        ident = (identifier or "").strip().upper()[:12]
        for p in self.get_projects(slug):
            if str(p.get("identifier", "")).upper() == ident:
                return str(p["id"]), False
        self.throttle()
        status, data = self._request(
            "POST", f"/api/v1/workspaces/{slug}/projects/",
            # Enable the same features a UI-created project gets (Cycles,
            # Modules, Views, Pages) — the model defaults leave them off.
            {"name": name, "identifier": ident,
             "cycle_view": True, "module_view": True,
             "issue_views_view": True, "page_view": True},
        )
        if status != 409 and data.get("id"):
            return str(data["id"]), True
        # Conflict (name/identifier taken) — re-resolve from the project list
        for p in self.get_projects(slug):
            if (str(p.get("identifier", "")).upper() == ident
                    or p.get("name", "").strip().lower() == name.strip().lower()):
                return str(p["id"]), False
        raise PlaneApiError(status, f"could not create or resolve project "
                                    f"{name!r}/{ident!r}: {data}")

    def get_states(self, slug: str, project_id: str) -> list[dict]:
        return self.get_paginated(f"/api/v1/workspaces/{slug}/projects/{project_id}/states/")

    def ensure_state(self, slug: str, project_id: str, name: str, group: str,
                     external_id: str | None = None) -> str:
        """Create a state (idempotent: 409 returns the existing id)."""
        body = {"name": name, "group": group, "color": "#60646C"}
        if external_id:
            body["external_id"] = external_id
            body["external_source"] = EXTERNAL_SOURCE
        self.throttle()
        status, data = self._request(
            "POST", f"/api/v1/workspaces/{slug}/projects/{project_id}/states/", body
        )
        return str(data["id"])

    def get_members(self, slug: str, project_id: str) -> list[dict]:
        _, body = self._request(
            "GET", f"/api/v1/workspaces/{slug}/projects/{project_id}/members/"
        )
        return body if isinstance(body, list) else body.get("results", [])

    def get_labels(self, slug: str, project_id: str) -> list[dict]:
        return self.get_paginated(f"/api/v1/workspaces/{slug}/projects/{project_id}/labels/")

    def ensure_label(self, slug: str, project_id: str, name: str) -> str:
        self.throttle()
        status, data = self._request(
            "POST", f"/api/v1/workspaces/{slug}/projects/{project_id}/labels/",
            {"name": name, "external_id": name, "external_source": EXTERNAL_SOURCE},
        )
        if "id" in data:
            return str(data["id"])
        # 409 without id (name collision outside external tracking) → look up
        for label in self.get_labels(slug, project_id):
            if label.get("name", "").lower() == name.lower():
                return str(label["id"])
        raise PlaneApiError(status, f"could not create or resolve label {name!r}")

    def create_or_update_work_item(self, slug: str, project_id: str,
                                   payload: dict) -> tuple[str, str]:
        """POST the work item; on 409 PATCH the existing one.
        Returns (plane_issue_id, 'created' | 'updated')."""
        base = f"/api/v1/workspaces/{slug}/projects/{project_id}/work-items/"
        self.throttle()
        status, data = self._request("POST", base, payload)
        if status == 409:
            issue_id = data.get("id")
            if not issue_id:
                raise PlaneApiError(409, f"conflict without id: {data}")
            self.throttle()
            self._request("PATCH", f"{base}{issue_id}/", payload)
            return str(issue_id), "updated"
        return str(data["id"]), "created"

    def ensure_cycle(self, slug: str, project_id: str, name: str) -> str:
        """Create a cycle (Jira sprint → Plane cycle). Idempotent: 409 returns
        the existing id via external_id tracking."""
        self.throttle()
        status, data = self._request(
            "POST", f"/api/v1/workspaces/{slug}/projects/{project_id}/cycles/",
            {"name": name, "external_id": name, "external_source": EXTERNAL_SOURCE},
        )
        if "id" in data:
            return str(data["id"])
        # 409 without id (name clash outside external tracking) → look it up
        for cyc in self.get_paginated(f"/api/v1/workspaces/{slug}/projects/{project_id}/cycles/"):
            if cyc.get("name", "").lower() == name.lower():
                return str(cyc["id"])
        raise PlaneApiError(status, f"could not create or resolve cycle {name!r}")

    def add_cycle_issues(self, slug: str, project_id: str, cycle_id: str,
                         issue_ids: list[str]) -> None:
        """Assign work items to a cycle (sprint planning)."""
        if not issue_ids:
            return
        self.throttle()
        self._request(
            "POST",
            f"/api/v1/workspaces/{slug}/projects/{project_id}/cycles/{cycle_id}/cycle-issues/",
            {"issues": list(issue_ids)},
        )

    def create_relations(self, slug: str, project_id: str, issue_id: str,
                         relation_type: str, related_ids: list[str]) -> None:
        """Create issue-to-issue relations. The endpoint uses ignore_conflicts
        server-side, so this is idempotent across re-runs."""
        if not related_ids:
            return
        self.throttle()
        self._request(
            "POST",
            f"/api/v1/workspaces/{slug}/projects/{project_id}/work-items/{issue_id}/relations/",
            {"relation_type": relation_type, "issues": list(related_ids)},
        )

    def get_work_item_by_external(self, slug: str, project_id: str,
                                  external_id: str) -> Optional[dict]:
        """Look up an existing work item by its Jira key. None if absent."""
        try:
            _, body = self._request(
                "GET", f"/api/v1/workspaces/{slug}/projects/{project_id}/work-items/",
                params={"external_id": external_id, "external_source": EXTERNAL_SOURCE},
            )
            return body if body.get("id") else None
        except PlaneApiError:
            return None
