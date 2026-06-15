"""
app/config.py — Application configuration.

Values can be overridden by environment variables where noted.
"""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
WORKFLOW_TEMPLATES_DIR = os.path.join(BASE_DIR, "workflow_templates")


class Config:
    # Flask
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    DEBUG = os.environ.get("DEBUG", "true").lower() == "true"
    HOST = os.environ.get("HOST", "127.0.0.1")
    PORT = int(os.environ.get("PORT", 5050))

    # Database
    DB_PATH = os.environ.get(
        "DB_PATH", os.path.join(DATA_DIR, "optimization.db")
    )

    # Jira
    JIRA_BASE_URL = os.environ.get(
        "JIRA_BASE_URL", "https://nttdata-emea.atlassian.net"
    )
    JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "JUM")
    # Auth: netrc (~/.netrc) — no credentials stored here.
    # The requests library will use netrc automatically when no auth is supplied.

    # Workflow templates directory
    WORKFLOW_TEMPLATES_DIR = WORKFLOW_TEMPLATES_DIR

    # Scheduler defaults
    DEFAULT_DAILY_CAPACITY = 1.0  # full day


# Expose a single instance so imports are simple: from app.config import config
config = Config()
