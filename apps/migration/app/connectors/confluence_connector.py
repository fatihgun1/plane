"""
app/connectors/confluence_connector.py
======================================
Thin Confluence Cloud REST API v2 client (urllib), Basic-auth with an Atlassian
email + API token (the same token works for Jira and Confluence on one site).

Used by the Confluence → Plane Pages migration. Fetches a space's pages with
their hierarchy (parentId) and body as ADF (atlas_doc_format) so the existing
adf_to_html converter can be reused.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100


class ConfluenceConnector:
    def __init__(self, base_url: str, email: str, token: str, timeout: int = 30) -> None:
        # base_url is the wiki root, e.g. https://your-org.atlassian.net/wiki
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.token = token
        self.timeout = timeout
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET an absolute-ish path (under base_url) with retry/backoff."""
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        backoff = 1.0
        for attempt in range(4):
            req = urllib.request.Request(url, headers=self._headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    wait = float(exc.headers.get("Retry-After") or backoff)
                    logger.warning("Confluence %s on %s — retry in %.1fs", exc.code, path, wait)
                    time.sleep(wait)
                    backoff *= 2
                    continue
                body = exc.read().decode(errors="replace")[:400]
                raise RuntimeError(f"Confluence GET {path} failed [{exc.code}]: {body}") from exc

    # ------------------------------------------------------------------
    def list_spaces(self) -> list[dict]:
        """All spaces the account can see: [{id, key, name}]."""
        spaces, cursor = [], None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/api/v2/spaces", params)
            for s in data.get("results", []):
                spaces.append({"id": str(s["id"]), "key": s.get("key", ""), "name": s.get("name", "")})
            cursor = _next_cursor(data)
            if not cursor:
                return spaces

    def get_space_id(self, space_key: str) -> Optional[str]:
        data = self._get("/api/v2/spaces", {"keys": space_key, "limit": 1})
        results = data.get("results", [])
        return str(results[0]["id"]) if results else None

    def iter_pages(self, space_id: str):
        """Yield pages of a space one API page (~100) at a time, sequentially.
        Each item: {id, title, parent_id, adf} (body as Atlassian Document
        Format, so it can be converted with the existing adf_to_html)."""
        cursor = None
        while True:
            params = {"space-id": space_id, "body-format": "atlas_doc_format",
                      "limit": _PAGE_SIZE, "status": "current"}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/api/v2/pages", params)
            results = data.get("results", [])
            if not results:
                return
            batch = []
            for p in results:
                adf_raw = (((p.get("body") or {}).get("atlas_doc_format") or {}).get("value")) or ""
                try:
                    adf = json.loads(adf_raw) if adf_raw else None
                except (ValueError, TypeError):
                    adf = None
                batch.append({
                    "id": str(p["id"]),
                    "title": p.get("title") or "(untitled)",
                    "parent_id": str(p["parentId"]) if p.get("parentId") else "",
                    "adf": adf,
                })
            yield batch
            cursor = _next_cursor(data)
            if not cursor:
                return


def _next_cursor(data: dict) -> Optional[str]:
    """Extract the cursor from a Confluence v2 `_links.next` relative URL."""
    nxt = (data.get("_links") or {}).get("next")
    if not nxt:
        return None
    qs = urllib.parse.urlparse(nxt).query
    return urllib.parse.parse_qs(qs).get("cursor", [None])[0]
