"""
app/connectors — Data import connectors.

Available connectors:
    jira_connector   — Jira Cloud REST API (fetch items, push scheduled tasks)
    file_connector   — CSV / XLSX flat-file import
    plane_connector  — Plane external REST API v1 (push work items)

`current_jira()` builds a JiraConnector for the authenticated Plane user:
base URL / project key are workspace-wide settings, while the Jira email +
API token are stored per user (app_settings key: jira.user.<plane_user_id>).

`current_plane()` builds a PlaneConnector with a per-user Plane API token
(app_settings key: plane.user.<plane_user_id>). The token is auto-minted on
first use through the user's Plane session cookie (POST /api/users/api-tokens/).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from app.auth import PLANE_API_URL, SESSION_COOKIE, current_user
from app.db import Database, get_db

logger = logging.getLogger(__name__)


class JiraNotConfigured(Exception):
    """Raised when the Jira connection has not been configured yet."""


def _user_key(user_id: str | None = None) -> str:
    return f"jira.user.{user_id or current_user()['id']}"


def get_user_jira_credentials(db: Database, user_id: str | None = None) -> dict:
    """Return the full per-user Jira config {email, token, base_url, project_key}.
    base_url / project_key fall back to the legacy shared settings so existing
    setups keep working until the user re-saves their connection."""
    raw = db.get_setting(_user_key(user_id))
    creds = json.loads(raw) if raw else {}
    return {
        "email": creds.get("email", ""),
        "token": creds.get("token", ""),
        "base_url": creds.get("base_url") or db.get_setting("jira.base_url", ""),
        "project_key": creds.get("project_key") or db.get_setting("jira.project_key", ""),
    }


def save_user_jira_credentials(db: Database, email: str, token: str,
                               base_url: str = "", project_key: str = "") -> None:
    """Persist a user's full Jira connection in their own per-user record."""
    db.set_setting(_user_key(), json.dumps({
        "email": email, "token": token,
        "base_url": base_url, "project_key": project_key,
    }))


def build_jira(db: Database, user_id: str):
    """Build a JiraConnector for a specific user from their per-user config —
    no Flask request context needed (used by the background migration worker)."""
    from app.connectors.jira_connector import JiraConnector

    creds = get_user_jira_credentials(db, user_id)
    if not creds["base_url"]:
        raise JiraNotConfigured(
            "Jira base URL is not set. Configure it in Settings → Jira Connection."
        )
    if not (creds["email"] and creds["token"]):
        raise JiraNotConfigured(
            "No Jira credentials for your account. Add your Jira email and API "
            "token in Settings → Jira Connection."
        )
    kwargs: dict = {"base_url": creds["base_url"], "email": creds["email"], "token": creds["token"]}
    if creds["project_key"]:
        kwargs["project_key"] = creds["project_key"]
    return JiraConnector(**kwargs)


def current_jira():
    """Build a JiraConnector for the authenticated user (request context)."""
    return build_jira(Database(get_db()), current_user()["id"])


# ---------------------------------------------------------------------------
# Confluence connection (per-user; shares the Atlassian email + token with Jira)
# ---------------------------------------------------------------------------

def _conf_user_key(user_id: str | None = None) -> str:
    return f"confluence.user.{user_id or current_user()['id']}"


def get_user_confluence_config(db: Database, user_id: str | None = None) -> dict:
    """{base_url, space, email, token}. base_url defaults to the Jira site + /wiki;
    email/token come from the shared Atlassian (Jira) credentials."""
    raw = db.get_setting(_conf_user_key(user_id))
    cfg = json.loads(raw) if raw else {}
    jira = get_user_jira_credentials(db, user_id)
    base = cfg.get("base_url") or (
        (jira["base_url"].rstrip("/") + "/wiki") if jira["base_url"] else "")
    return {"base_url": base, "space": cfg.get("space", ""),
            "email": jira["email"], "token": jira["token"]}


def save_user_confluence_config(db: Database, base_url: str, space: str) -> None:
    db.set_setting(_conf_user_key(), json.dumps({"base_url": base_url, "space": space}))


def build_confluence(db: Database, user_id: str):
    """Build a ConfluenceConnector for a user — no Flask context needed (worker)."""
    from app.connectors.confluence_connector import ConfluenceConnector

    cfg = get_user_confluence_config(db, user_id)
    if not (cfg["email"] and cfg["token"]):
        raise JiraNotConfigured(
            "No Atlassian credentials — set your Jira email and API token in "
            "the Jira Connection tab (they're shared with Confluence)."
        )
    if not cfg["base_url"]:
        raise JiraNotConfigured(
            "No Confluence base URL — set it in the Confluence Connection tab."
        )
    return ConfluenceConnector(cfg["base_url"], cfg["email"], cfg["token"])


def current_confluence():
    return build_confluence(Database(get_db()), current_user()["id"])


# ---------------------------------------------------------------------------
# Plane connection (per-user API token, auto-minted from the session)
# ---------------------------------------------------------------------------

class PlaneNotConfigured(Exception):
    """Raised when a Plane API token can't be obtained for the current user."""


def _plane_user_key() -> str:
    return f"plane.user.{current_user()['id']}"


def get_user_plane_token(db: Database) -> dict:
    """Return {token, token_id, workspace_slug} for the current user ({} if unset)."""
    raw = db.get_setting(_plane_user_key())
    return json.loads(raw) if raw else {}


def clear_user_plane_token(db: Database) -> None:
    db.set_setting(_plane_user_key(), "")


def _session_request(method: str, path: str, session_cookie: str,
                     body: dict | None = None, extra_headers: dict | None = None):
    """Call the session-authenticated Plane app API with the user's cookie."""
    headers = {
        "Cookie": f"{SESSION_COOKIE}={session_cookie}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        f"{PLANE_API_URL}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read() or b"{}"), resp.headers


def mint_plane_token(session_cookie: str, label: str) -> dict:
    """Create a personal Plane API token via the user's session.
    Returns {"token": <raw token>, "token_id": <uuid>}."""
    # Django CSRF: fetch a token (sets the csrftoken cookie) and echo it back
    csrf_body, csrf_headers = _session_request("GET", "/auth/get-csrf-token/", session_cookie)
    csrf_token = csrf_body.get("csrf_token", "")
    data, _ = _session_request(
        "POST", "/api/users/api-tokens/", session_cookie,
        body={"label": label, "description": "Auto-minted by the Plane Migration tool"},
        extra_headers={
            "Cookie": f"{SESSION_COOKIE}={session_cookie}; csrftoken={csrf_token}",
            "X-CSRFToken": csrf_token,
        },
    )
    if not data.get("token"):
        raise PlaneNotConfigured(f"Token mint failed: unexpected response {data}")
    return {"token": data["token"], "token_id": data.get("id", "")}


def fetch_workspace_slug(session_cookie: str) -> str:
    """The v1 API can't list workspaces — use the session-authenticated app API."""
    workspaces, _ = _session_request("GET", "/api/users/me/workspaces/", session_cookie)
    if not workspaces:
        raise PlaneNotConfigured("No Plane workspace found for your account.")
    return workspaces[0]["slug"]


def get_workspace_member_emails(session_cookie: str, slug: str) -> set:
    """Lowercased emails of everyone already in the workspace (so we don't
    re-invite them — Plane's invite endpoint rejects the whole batch if any
    email is already a member)."""
    data, _ = _session_request("GET", f"/api/workspaces/{slug}/members/", session_cookie)
    rows = data if isinstance(data, list) else data.get("results", [])
    emails = set()
    for row in rows:
        member = row.get("member") or {}
        if member.get("email"):
            emails.add(member["email"].lower())
    return emails


def invite_workspace_members(session_cookie: str, slug: str, emails: list,
                             role: int = 15) -> dict:
    """Invite users to the workspace via Plane's own invitation flow (the same
    endpoint the Plane UI uses). role 15 = Member. Returns the API response."""
    csrf_body, _ = _session_request("GET", "/auth/get-csrf-token/", session_cookie)
    csrf = csrf_body.get("csrf_token", "")
    data, _ = _session_request(
        "POST", f"/api/workspaces/{slug}/invitations/", session_cookie,
        body={"emails": [{"email": e, "role": role} for e in emails]},
        extra_headers={
            "Cookie": f"{SESSION_COOKIE}={session_cookie}; csrftoken={csrf}",
            "X-CSRFToken": csrf,
        },
    )
    return data


def ensure_plane_access(db: Database, session_cookie: str) -> dict:
    """Return stored {token, token_id, workspace_slug}, minting/discovering
    anything missing through the session cookie (request context only)."""
    stored = get_user_plane_token(db)
    if stored.get("token") and stored.get("workspace_slug"):
        return stored
    try:
        if not stored.get("token"):
            user = current_user()
            stored.update(mint_plane_token(session_cookie, f"plane-migration {user['email']}"))
        if not stored.get("workspace_slug"):
            stored["workspace_slug"] = fetch_workspace_slug(session_cookie)
    except urllib.error.HTTPError as exc:
        raise PlaneNotConfigured(
            f"Plane API rejected the request (HTTP {exc.code}) while preparing API access."
        ) from exc
    db.set_setting(_plane_user_key(), json.dumps(stored))
    logger.info("Minted Plane API token for user %s", current_user()["id"])
    return stored


def current_plane():
    """Build a PlaneConnector for the current user. Raises PlaneNotConfigured
    if no token is stored (mint happens via ensure_plane_access in routes)."""
    from app.connectors.plane_connector import PlaneConnector

    stored = get_user_plane_token(Database(get_db()))
    if not stored.get("token"):
        raise PlaneNotConfigured(
            "No Plane API token yet — open the Migration page to set it up."
        )
    return PlaneConnector(PLANE_API_URL, stored["token"])
