"""
app/connectors/jira_connector.py
=================================
Jira Cloud REST API connector for the NTT-PMO Optimization Tool.

Responsibilities
----------------
- Fetch parent work items from Jira using JQL, mapping fields via jira_field_map.
- Fetch issue links (dependencies) for a given Jira key.
- Discover all Jira field definitions (for field-map setup).
- Push scheduled tasks back to Jira:
    * Create new child sub-tasks (one per phase) when jira_child_key is None.
    * Update planned_start / planned_end on existing child issues.
- Dry-run mode: returns a PushDiff list without making any live API calls.

Auth
----
Uses the workspace-level _KB/patterns/jira_auth.py resolver.
Credential priority: JIRA_TOKEN env var -> ~/.netrc -> RuntimeError.

Config (env vars)
-----------------
    JIRA_BASE_URL       default: https://ndbstr.atlassian.net
    JIRA_EMAIL          default: hasan.guner@nttdata.com
    JIRA_PROJECT_KEY    default: JUM

Usage
-----
    from app.connectors.jira_connector import JiraConnector
    jc = JiraConnector()
    items = jc.fetch_items_by_jql("project=JUM AND issuetype=WRICEF", field_map)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Workspace-level auth resolver
# ---------------------------------------------------------------------------
_WORKSPACE_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..")
)
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

try:
    from _KB.patterns.jira_auth import get_credentials  # noqa: E402
except ImportError:
    # Fallback when the workspace-level resolver is unavailable (e.g. in
    # Docker). Same priority: JIRA_TOKEN env var -> ~/.netrc -> RuntimeError.
    def get_credentials(base_url: str, email: str) -> tuple[str, str]:
        token = os.getenv("JIRA_TOKEN")
        if token:
            return os.getenv("JIRA_EMAIL", email), token
        import netrc

        host = urllib.parse.urlparse(base_url).hostname or base_url
        try:
            auth = netrc.netrc().authenticators(host)
        except (FileNotFoundError, netrc.NetrcParseError):
            auth = None
        if auth:
            login, _, password = auth
            return login or email, password or ""
        raise RuntimeError(
            "No Jira credentials found: set JIRA_TOKEN (and optionally "
            f"JIRA_EMAIL) or add a ~/.netrc entry for {host}"
        )

# ---------------------------------------------------------------------------
# App models
# ---------------------------------------------------------------------------
from app.models import Item, ItemDependency, ScheduledTask  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (overridden via constructor args or env vars)
# ---------------------------------------------------------------------------
_DEFAULT_BASE_URL = os.getenv("JIRA_BASE_URL", "")
_DEFAULT_EMAIL = os.getenv("JIRA_EMAIL", "")
_DEFAULT_PROJECT = os.getenv("JIRA_PROJECT_KEY", "JUM")

_PAGE_SIZE = 100

_PRIORITY_MAP = {
    "highest": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
    "lowest": 5,
}


def _priority_name_to_int(name: str) -> Optional[int]:
    return _PRIORITY_MAP.get(name.lower().strip())


# ---------------------------------------------------------------------------
# Result types for push operations
# ---------------------------------------------------------------------------

@dataclass
class PushAction:
    """Represents one create/update operation (dry-run or live result)."""
    action: str            # 'create_child' | 'update_dates' | 'skip'
    task_id: int           # ScheduledTask.id
    item_jira_key: str
    phase_name: str
    dry_run: bool
    jira_key_before: Optional[str] = None
    jira_key_after: Optional[str] = None
    planned_start: Optional[str] = None
    planned_end: Optional[str] = None
    error: Optional[str] = None
    success: bool = True


@dataclass
class PushResult:
    """Aggregated result from push_scheduled_tasks()."""
    dry_run: bool
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    actions: list[PushAction] = field(default_factory=list)


# ---------------------------------------------------------------------------
# JiraConnector
# ---------------------------------------------------------------------------

class JiraConnector:
    """
    Thin wrapper around the Jira Cloud REST API v3.

    Parameters
    ----------
    base_url : str
        Jira instance base URL, e.g. "https://ndbstr.atlassian.net".
    email : str
        Atlassian account email (Basic Auth username).
    project_key : str
        Default Jira project key.
    timeout : int
        HTTP request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        email: str = _DEFAULT_EMAIL,
        project_key: str = _DEFAULT_PROJECT,
        timeout: int = 30,
        token: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.project_key = project_key
        self.timeout = timeout
        self.token = token  # explicit API token; falls back to get_credentials()
        self._headers: Optional[dict] = None  # built lazily

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get_headers(self) -> dict:
        """Resolve credentials and build Basic Auth + JSON headers (cached)."""
        if self._headers is None:
            if self.token:
                resolved_email, token = self.email, self.token
            else:
                resolved_email, token = get_credentials(self.base_url, self.email)
            encoded = base64.b64encode(
                f"{resolved_email}:{token}".encode()
            ).decode()
            self._headers = {
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        return self._headers

    # ------------------------------------------------------------------
    # Retry helper
    # ------------------------------------------------------------------

    def _request_with_retry(
        self,
        req: urllib.request.Request,
        retries: int = 3,
        backoff: float = 1.0,
    ) -> bytes:
        """Execute *req* with exponential back-off on 429 / 5xx responses."""
        delay = backoff
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or (500 <= exc.code < 600):
                    if attempt < retries:
                        retry_after = exc.headers.get("Retry-After")
                        wait = float(retry_after) if retry_after else delay
                        logger.warning(
                            "Jira HTTP %d on attempt %d/%d — waiting %.1fs",
                            exc.code, attempt + 1, retries + 1, wait,
                        )
                        time.sleep(wait)
                        delay *= 2
                        last_exc = exc
                        # Rebuild request (urlopen consumes it)
                        new_req = urllib.request.Request(
                            req.full_url,
                            data=req.data,
                            headers=dict(req.headers),
                            method=req.get_method(),
                        )
                        req = new_req
                        continue
                raise
        raise last_exc  # type: ignore[misc]

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """HTTP GET — returns parsed JSON body."""
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._get_headers())
        logger.debug("GET %s", url)
        try:
            return json.loads(self._request_with_retry(req).decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"Jira GET {path} failed [{exc.code}]: {body[:400]}"
            ) from exc

    def _post(self, path: str, body: dict) -> dict:
        """HTTP POST — returns parsed JSON body."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=dict(self._get_headers()), method="POST"
        )
        logger.debug("POST %s", url)
        try:
            return json.loads(self._request_with_retry(req).decode())
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"Jira POST {path} failed [{exc.code}]: {body_text[:400]}"
            ) from exc

    def _put(self, path: str, body: dict) -> None:
        """HTTP PUT — Jira update returns 204 No Content."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=dict(self._get_headers()), method="PUT"
        )
        logger.debug("PUT %s", url)
        try:
            self._request_with_retry(req)
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"Jira PUT {path} failed [{exc.code}]: {body_text[:400]}"
            ) from exc

    # ------------------------------------------------------------------
    # Field map helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_reverse_map(field_map: list[dict]) -> dict[str, str]:
        """label (lowercase) -> jira_field_id lookup from DB jira_field_map rows."""
        return {
            row["field_label"].lower(): row["jira_field_id"]
            for row in field_map
        }

    @staticmethod
    def _extract_field(issue: dict, field_id: str) -> Optional[str]:
        """
        Safely extract a scalar string from issue['fields'][field_id].
        Handles:
        - str/int/float  → direct string
        - dict with value/name/key → extracted string
        - list of dicts  → comma-joined names (e.g. components)
        - ADF doc (description) → plain text extracted from content nodes
        """
        if not field_id:
            return None
        raw = issue.get("fields", {}).get(field_id)
        if raw is None:
            return None
        if isinstance(raw, (str, int, float)):
            val = str(raw).strip()
            return val if val else None
        if isinstance(raw, list):
            # e.g. components: [{"name": "Jumbo Serve"}, ...]
            parts = []
            for item in raw:
                if isinstance(item, dict):
                    v = item.get("name") or item.get("value") or item.get("key")
                    if v:
                        parts.append(str(v))
                elif isinstance(item, str):
                    parts.append(item)
            return ", ".join(parts) if parts else None
        if isinstance(raw, dict):
            # Check for Atlassian Document Format (description field in API v3)
            if raw.get("type") == "doc" and "content" in raw:
                return JiraConnector._extract_adf_text(raw)
            # Jira user objects use displayName; also fall back to name/value/key
            return (raw.get("displayName") or raw.get("value") or
                    raw.get("name") or raw.get("key"))
        return None

    @staticmethod
    def _extract_adf_text(node: dict) -> Optional[str]:
        """Recursively extract plain text from an Atlassian Document Format node."""
        parts = []
        if node.get("type") == "text":
            text = node.get("text", "")
            if text:
                parts.append(text)
        for child in node.get("content", []):
            t = JiraConnector._extract_adf_text(child)
            if t:
                parts.append(t)
        result = " ".join(parts).strip()
        return result if result else None

    # ------------------------------------------------------------------
    # Public: discover Jira fields
    # ------------------------------------------------------------------

    def fetch_fields(self) -> list[dict]:
        """
        Return all Jira field definitions.
        Useful during setup to discover custom field IDs.
        """
        return self._get("/rest/api/3/field")

    def fetch_project(self, key: Optional[str] = None) -> dict:
        """Return {key, name} for a Jira project (defaults to project_key)."""
        key = (key or self.project_key or "").strip()
        if not key:
            raise RuntimeError("No Jira project key configured.")
        data = self._get(f"/rest/api/3/project/{key}")
        return {"key": data.get("key", key), "name": data.get("name", key)}

    # ------------------------------------------------------------------
    # Public: fetch items by JQL
    # ------------------------------------------------------------------

    def fetch_items_by_jql(
        self,
        jql: str,
        field_map: Optional[list[dict]] = None,
        max_results: int = 500,
    ) -> list[Item]:
        """
        Execute a JQL query and return matching issues as Item dataclasses.

        Parameters
        ----------
        jql : str
            JQL string, e.g. "project=JUM AND issuetype=WRICEF".
        field_map : list[dict]
            Rows from jira_field_map DB table (field_label, jira_field_id).
        max_results : int
            Cap on total issues returned.
        """
        rmap = self._build_reverse_map(field_map or [])

        # Always request these core fields (standard Jira fields)
        standard_fields = [
            "summary", "issuetype", "status", "priority", "issuelinks",
            "description", "components", "assignee",
        ]
        # Request ALL mapped fields (not just the core ones)
        custom_fields: list[str] = []
        for fid in rmap.values():
            if fid and fid not in standard_fields and fid not in custom_fields:
                custom_fields.append(fid)

        fields_list = standard_fields + custom_fields
        # Keep the full field_map rows for use in _map_issue_to_item
        _full_field_map = field_map or []
        imported_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        items: list[Item] = []
        next_page_token: Optional[str] = None
        fetched = 0

        logger.info("Fetching items via JQL: %s", jql)

        while fetched < max_results:
            batch_size = min(_PAGE_SIZE, max_results - fetched)
            body: dict = {
                "jql": jql,
                "maxResults": batch_size,
                "fields": fields_list,
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token

            data = self._post("/rest/api/3/search/jql", body)
            batch = data.get("issues", [])
            if not batch:
                break

            for issue in batch:
                items.append(self._map_issue_to_item(issue, rmap, imported_at, _full_field_map))

            fetched += len(batch)
            logger.debug("Fetched %d issues so far.", fetched)

            next_page_token = data.get("nextPageToken")
            if data.get("isLast", True) or not next_page_token:
                break

        logger.info("fetch_items_by_jql: %d items returned.", len(items))
        return items

    # ------------------------------------------------------------------
    # Public: fetch issues for Plane migration
    # ------------------------------------------------------------------

    def fetch_issues_for_migration(
        self,
        jql: str,
        field_map: Optional[list[dict]] = None,
        max_results: int = 500,
    ) -> list[dict]:
        """
        Fetch issues with everything the Jira→Plane migration needs:
        parent/sub-task links, assignee email, labels, status category,
        raw ADF description and the mapped custom fields (with their
        configured plane_target). Returns plain dicts, newest schema:

        { key, summary, description_adf, status_name, status_category,
          priority_name, assignee_email, labels[], duedate, issuetype_name,
          is_subtask, parent_key, custom: {label: {value, plane_target}} }
        """
        issues: list[dict] = []
        for page in self.iter_issue_pages(jql, field_map):
            issues.extend(page)
            if len(issues) >= max_results:
                return issues[:max_results]
        return issues

    def iter_issue_pages(self, jql: str, field_map: Optional[list[dict]] = None):
        """Yield migration issues one Jira page (~100) at a time, sequentially
        via nextPageToken. Lets the worker stream all results into SQLite
        without ever holding the whole result set in memory."""
        field_map = field_map or []
        standard_fields = [
            "summary", "issuetype", "status", "priority", "assignee",
            "labels", "duedate", "description", "parent", "issuelinks",
        ]
        custom_ids = []
        for row in field_map:
            fid = row.get("jira_field_id")
            if fid and fid not in standard_fields and fid not in custom_ids:
                custom_ids.append(fid)

        next_page_token: Optional[str] = None
        logger.info("Iterating issues for migration via JQL: %s", jql)
        while True:
            body: dict = {
                "jql": jql,
                "maxResults": _PAGE_SIZE,
                "fields": standard_fields + custom_ids,
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            data = self._post("/rest/api/3/search/jql", body)
            batch = data.get("issues", [])
            if not batch:
                return
            yield [self._map_issue_for_migration(issue, field_map) for issue in batch]
            next_page_token = data.get("nextPageToken")
            if data.get("isLast", True) or not next_page_token:
                return

    def _map_issue_for_migration(self, issue: dict, field_map: list[dict]) -> dict:
        f = issue.get("fields", {})
        status = f.get("status") or {}
        priority = f.get("priority") or {}
        assignee = f.get("assignee") or {}
        issuetype = f.get("issuetype") or {}
        parent = f.get("parent") or {}
        description = f.get("description")

        custom: dict = {}
        for row in field_map:
            fid = row.get("jira_field_id")
            if not fid:
                continue
            value = self._extract_field(issue, fid)
            if value:
                custom[row["field_label"]] = {
                    "value": value,
                    "plane_target": row.get("plane_target") or "description",
                }

        # Issue links (blocks / relates / duplicate ...) — keep the Jira type
        # name and which side this issue is on so they can be mapped to Plane
        # relation types later.
        links: list[dict] = []
        for link in f.get("issuelinks") or []:
            ltype = (link.get("type") or {}).get("name", "")
            if link.get("outwardIssue"):
                links.append({"type": ltype, "side": "outward",
                              "key": link["outwardIssue"].get("key", "")})
            elif link.get("inwardIssue"):
                links.append({"type": ltype, "side": "inward",
                              "key": link["inwardIssue"].get("key", "")})

        return {
            "key": issue.get("key", ""),
            "summary": f.get("summary") or "",
            "description_adf": description if isinstance(description, dict) else None,
            "status_name": status.get("name") or "",
            "status_category": ((status.get("statusCategory") or {}).get("key") or ""),
            "priority_name": priority.get("name") or "",
            "assignee_email": assignee.get("emailAddress") or "",
            "assignee_name": assignee.get("displayName") or "",
            "labels": f.get("labels") or [],
            "duedate": f.get("duedate") or "",
            "issuetype_name": issuetype.get("name") or "",
            "is_subtask": bool(issuetype.get("subtask")),
            "parent_key": parent.get("key") or "",
            "custom": custom,
            "links": links,
        }

    def _map_issue_to_item(
        self,
        issue: dict,
        rmap: dict[str, str],
        imported_at: str,
        full_field_map: Optional[list[dict]] = None,
    ) -> Item:
        """Map a raw Jira issue dict to an Item dataclass."""
        fields = issue.get("fields", {})

        issue_type = (fields.get("issuetype") or {}).get("name")
        status = (fields.get("status") or {}).get("name")
        priority_name = (fields.get("priority") or {}).get("name")
        priority = _priority_name_to_int(priority_name) if priority_name else None

        effort_raw = self._extract_field(issue, rmap.get("story points", ""))
        effort_days: Optional[float] = None
        if effort_raw is not None:
            try:
                effort_days = float(effort_raw)
            except (TypeError, ValueError):
                pass

        epic_key = self._extract_field(issue, rmap.get("epic link", ""))

        # Collect ALL mapped fields into extra_fields JSON
        # Skip only the fields already stored as dedicated top-level columns
        _TOPLEVEL_LABELS = {"module", "stream", "story points", "epic link",
                            "summary", "issue type", "status", "priority"}
        extra: dict = {}
        if full_field_map:
            for row in full_field_map:
                label = row["field_label"]
                fid = row["jira_field_id"]
                if label.lower() in _TOPLEVEL_LABELS:
                    continue
                # Special case: "key" lives at issue top level, not in fields
                if fid == "key":
                    val = issue.get("key")
                else:
                    val = self._extract_field(issue, fid)
                if val is not None:
                    extra[label] = val

        # Capture parent link keys from issuelinks
        # Store as _parent_keys: [{"link_type": "...", "key": "..."}] for scheduler use
        issue_links = fields.get("issuelinks", []) or []
        parent_links: list = []
        for lnk in issue_links:
            lt = (lnk.get("type") or {}).get("name", "")
            inward_issue = lnk.get("inwardIssue")
            outward_issue = lnk.get("outwardIssue")
            # "is parent task of" → inward direction in Jira (parent has outward link TO child)
            # "is child of" / "is parent task of" → depends on Jira configuration
            # Store both directions with their link type names
            if inward_issue:
                parent_links.append({"link_type": lt, "direction": "inward", "key": inward_issue.get("key", "")})
            if outward_issue:
                parent_links.append({"link_type": lt, "direction": "outward", "key": outward_issue.get("key", "")})
        if parent_links:
            extra["_issue_links"] = parent_links

        return Item(
            id=None,
            jira_key=issue.get("key"),
            summary=fields.get("summary"),
            issue_type=issue_type,
            module=self._extract_field(issue, rmap.get("module", "")),
            stream=self._extract_field(issue, rmap.get("stream", "")),
            priority=priority,
            effort_days=effort_days,
            status=status,
            epic_key=epic_key,
            imported_at=imported_at,
            extra_fields=json.dumps(extra, ensure_ascii=False) if extra else None,
        )

    # ------------------------------------------------------------------
    # Public: fetch a single issue (raw dict)
    # ------------------------------------------------------------------

    def fetch_issue(self, jira_key: str) -> dict:
        """Return the raw Jira issue dict for *jira_key*."""
        return self._get(f"/rest/api/3/issue/{jira_key}")

    def fetch_child_issues(self, parent_key: str) -> list[dict]:
        """
        Return all Sub-task issues whose parent is *parent_key*.
        Used for duplicate-summary detection before creating new sub-tasks.
        """
        jql = f'parent = "{parent_key}" AND issuetype = Sub-task ORDER BY created ASC'
        data = self._post(
            "/rest/api/3/search/jql",
            {"jql": jql, "maxResults": 200, "fields": ["summary", "status"]},
        )
        return data.get("issues", [])

    def update_issue(self, jira_key: str, fields: dict) -> None:
        """Update arbitrary fields on a Jira issue via PUT."""
        self._put(f"/rest/api/3/issue/{jira_key}", {"fields": fields})
        logger.info("Updated %s: fields=%s", jira_key, list(fields.keys()))

    def transition_issue(self, jira_key: str, transition_id: str) -> None:
        """Apply a workflow transition to *jira_key*."""
        self._post(
            f"/rest/api/3/issue/{jira_key}/transitions",
            {"transition": {"id": str(transition_id)}},
        )
        logger.info("Transitioned %s → transition_id=%s", jira_key, transition_id)

    # ------------------------------------------------------------------
    # Public: fetch issue dependencies
    # ------------------------------------------------------------------

    def fetch_issue_links(self, jira_key: str) -> list[ItemDependency]:
        """
        Return ItemDependency objects for all 'blocks' / 'is blocked by'
        issue links on *jira_key*.
        """
        issue = self.fetch_issue(jira_key)
        links_raw = issue.get("fields", {}).get("issuelinks", [])
        deps: list[ItemDependency] = []

        for link in links_raw:
            link_type = (link.get("type") or {}).get("name", "").lower()
            if "blocks" not in link_type and "is blocked" not in link_type:
                continue

            outward = link.get("outwardIssue")
            inward = link.get("inwardIssue")

            if outward:
                deps.append(ItemDependency(
                    id=None,
                    blocking_item_jira_key=jira_key,
                    blocked_item_jira_key=outward["key"],
                    dependency_type="blocks",
                ))
            if inward:
                deps.append(ItemDependency(
                    id=None,
                    blocking_item_jira_key=inward["key"],
                    blocked_item_jira_key=jira_key,
                    dependency_type="is_blocked_by",
                ))

        return deps

    # ------------------------------------------------------------------
    # Public: create child sub-task
    # ------------------------------------------------------------------

    def create_child_subtask(
        self,
        parent_key: str,
        summary: str,
        planned_start: Optional[str] = None,
        planned_end: Optional[str] = None,
        field_map: Optional[list[dict]] = None,
    ) -> str:
        """
        Create a Sub-task under *parent_key* and return the new issue key.

        Parameters
        ----------
        parent_key : str
            Jira key of the parent issue, e.g. "JUM-123".
        summary : str
            Summary text for the new sub-task.
        planned_start : str | None
            ISO date string "YYYY-MM-DD" written to the planned-start custom field.
        planned_end : str | None
            ISO date string "YYYY-MM-DD" written to the planned-end custom field.
        field_map : list[dict] | None
            DB field_map rows; required if planned_start/planned_end are provided.
        """
        rmap = self._build_reverse_map(field_map) if field_map else {}

        fields_body: dict = {
            "project": {"key": self.project_key},
            "issuetype": {"name": "Sub-task"},
            "summary": summary,
            "parent": {"key": parent_key},
        }

        if planned_start and rmap.get("planned start"):
            fields_body[rmap["planned start"]] = planned_start
        if planned_end and rmap.get("planned end"):
            fields_body[rmap["planned end"]] = planned_end

        result = self._post("/rest/api/3/issue", {"fields": fields_body})
        new_key: str = result["key"]
        logger.info("Created sub-task %s under %s", new_key, parent_key)
        return new_key

    # ------------------------------------------------------------------
    # Public: update dates on an existing child issue
    # ------------------------------------------------------------------

    def update_child_dates(
        self,
        jira_key: str,
        planned_start: Optional[str],
        planned_end: Optional[str],
        field_map: list[dict],
    ) -> None:
        """
        PUT planned_start / planned_end onto an existing Jira issue.

        Parameters
        ----------
        jira_key : str
            Key of the issue to update.
        planned_start : str | None
            ISO date "YYYY-MM-DD" or None to leave untouched.
        planned_end : str | None
            ISO date "YYYY-MM-DD" or None to leave untouched.
        field_map : list[dict]
            DB field_map rows used to resolve custom field IDs.
        """
        rmap = self._build_reverse_map(field_map)
        update_fields: dict = {}

        if planned_start and rmap.get("planned start"):
            update_fields[rmap["planned start"]] = planned_start
        if planned_end and rmap.get("planned end"):
            update_fields[rmap["planned end"]] = planned_end

        if not update_fields:
            logger.debug("update_child_dates: nothing to update for %s", jira_key)
            return

        self._put(f"/rest/api/3/issue/{jira_key}", {"fields": update_fields})
        logger.info("Updated dates on %s: %s", jira_key, update_fields)

    # ------------------------------------------------------------------
    # Public: push scheduled tasks to Jira
    # ------------------------------------------------------------------

    def push_scheduled_tasks(
        self,
        tasks: list[ScheduledTask],
        item_key_map: dict[int, str],
        field_map: list[dict],
        dry_run: bool = True,
    ) -> PushResult:
        """
        Push a list of ScheduledTask objects to Jira.

        For each task:
        - If task.jira_child_key is None  → create a new Sub-task.
        - If task.jira_child_key is set   → update dates on the existing issue.

        Parameters
        ----------
        tasks : list[ScheduledTask]
            Tasks to push (typically all tasks for a single schedule run).
        item_key_map : dict[int, str]
            Maps item_id → jira_key for parent lookup.
        field_map : list[dict]
            DB field_map rows.
        dry_run : bool
            When True, build PushAction objects but make no API calls.

        Returns
        -------
        PushResult
        """
        result = PushResult(dry_run=dry_run)

        # Pre-fetch existing children per parent for duplicate detection (live only)
        existing_summaries: dict[str, dict[str, str]] = {}  # parent_key -> {summary: child_key}
        if not dry_run:
            parents_needing_create = {
                item_key_map[t.item_id]
                for t in tasks
                if t.item_id in item_key_map
                and not t.jira_child_key
            }
            for parent in parents_needing_create:
                try:
                    children = self.fetch_child_issues(parent)
                    existing_summaries[parent] = {
                        (c.get("fields", {}).get("summary") or "").strip(): c["key"]
                        for c in children
                    }
                except Exception as exc:
                    logger.warning(
                        "Could not fetch existing children for %s: %s", parent, exc
                    )

        for task in tasks:
            parent_key = item_key_map.get(task.item_id, "")
            if not parent_key:
                action = PushAction(
                    action="skip",
                    task_id=task.id,
                    item_jira_key="",
                    phase_name=task.phase_name,
                    dry_run=dry_run,
                    error="parent jira_key not found in item_key_map",
                    success=False,
                )
                result.skipped += 1
                result.actions.append(action)
                continue

            start_str = (
                task.planned_start.strftime("%Y-%m-%d")
                if task.planned_start else None
            )
            end_str = (
                task.planned_end.strftime("%Y-%m-%d")
                if task.planned_end else None
            )

            if task.jira_child_key:
                # Update existing child
                action = PushAction(
                    action="update_dates",
                    task_id=task.id,
                    item_jira_key=parent_key,
                    phase_name=task.phase_name,
                    dry_run=dry_run,
                    jira_key_before=task.jira_child_key,
                    jira_key_after=task.jira_child_key,
                    planned_start=start_str,
                    planned_end=end_str,
                )
                if not dry_run:
                    try:
                        self.update_child_dates(
                            task.jira_child_key, start_str, end_str, field_map
                        )
                        result.updated += 1
                    except Exception as exc:
                        action.success = False
                        action.error = str(exc)
                        result.failed += 1
                        logger.error(
                            "Failed to update %s: %s", task.jira_child_key, exc
                        )
                else:
                    result.updated += 1
            else:
                # Create new child sub-task (with duplicate detection)
                summary = f"[{task.phase_name}] {parent_key}"
                existing_key = existing_summaries.get(parent_key, {}).get(summary.strip())
                if existing_key:
                    # Duplicate found — treat as update
                    logger.info(
                        "Duplicate sub-task found for %s/%s → %s (updating instead)",
                        parent_key, task.phase_name, existing_key,
                    )
                    action = PushAction(
                        action="update_dates",
                        task_id=task.id,
                        item_jira_key=parent_key,
                        phase_name=task.phase_name,
                        dry_run=dry_run,
                        jira_key_before=existing_key,
                        jira_key_after=existing_key,
                        planned_start=start_str,
                        planned_end=end_str,
                    )
                    try:
                        self.update_child_dates(existing_key, start_str, end_str, field_map)
                        result.updated += 1
                    except Exception as exc:
                        action.success = False
                        action.error = str(exc)
                        result.failed += 1
                else:
                    action = PushAction(
                        action="create_child",
                        task_id=task.id,
                        item_jira_key=parent_key,
                        phase_name=task.phase_name,
                        dry_run=dry_run,
                        planned_start=start_str,
                        planned_end=end_str,
                    )
                    if not dry_run:
                        try:
                            new_key = self.create_child_subtask(
                                parent_key, summary, start_str, end_str, field_map
                            )
                            action.jira_key_after = new_key
                            result.created += 1
                        except Exception as exc:
                            action.success = False
                            action.error = str(exc)
                            result.failed += 1
                            logger.error(
                                "Failed to create child for %s phase %s: %s",
                                parent_key, task.phase_name, exc,
                            )
                    else:
                        action.jira_key_after = f"DRY-RUN-{task.id}"
                        result.created += 1

            result.actions.append(action)

        logger.info(
            "push_scheduled_tasks (dry_run=%s): created=%d updated=%d "
            "skipped=%d failed=%d",
            dry_run, result.created, result.updated, result.skipped, result.failed,
        )
        return result
