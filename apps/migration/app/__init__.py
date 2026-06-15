"""
app/__init__.py — Flask application factory.
"""

from __future__ import annotations

import logging
import os

import yaml
from flask import Flask

from app.models import Workflow, WorkflowPhase


def create_app(config_overrides: dict | None = None) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__, instance_relative_config=False)

    # ------------------------------------------------------------------
    # Default configuration
    # ------------------------------------------------------------------
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        DB_PATH=os.environ.get(
            "DB_PATH", os.path.join(base_dir, "instance", "optimization.db")
        ),
        WORKFLOW_TEMPLATES_DIR=os.path.join(base_dir, "workflow_templates"),
        LOG_LEVEL=os.environ.get("LOG_LEVEL", "INFO"),
    )

    if config_overrides:
        app.config.update(config_overrides)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    logging.basicConfig(
        level=getattr(logging, app.config["LOG_LEVEL"], logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # ------------------------------------------------------------------
    # Load YAML workflow templates into app.config["WORKFLOWS"]
    # ------------------------------------------------------------------
    app.config["WORKFLOWS"] = _load_workflows(app.config["WORKFLOW_TEMPLATES_DIR"])

    # ------------------------------------------------------------------
    # Database — create schema + seed on first run
    # ------------------------------------------------------------------
    with app.app_context():
        from app.db import close_db, init_db, Database, get_db

        app.teardown_appcontext(close_db)
        init_db()

        # Seed workflow_templates DB table from YAML files if empty
        _seed_workflow_templates(app)

    # ------------------------------------------------------------------
    # Plane session authentication — every route except /health requires
    # a valid Plane session cookie (validated against the Plane API).
    # ------------------------------------------------------------------
    from app.auth import init_auth

    init_auth(app)

    # ------------------------------------------------------------------
    # Register blueprints
    # ------------------------------------------------------------------
    from app.routes.dashboard import bp as dashboard_bp
    from app.routes.items import bp as items_bp
    from app.routes.push import bp as push_bp
    from app.routes.schedule import bp as schedule_bp
    from app.routes.consultants import bp as consultants_bp
    from app.routes.holidays import bp as holidays_bp
    from app.routes.settings import bp as settings_bp
    from app.routes.gantt import bp as gantt_bp
    from app.routes.migration import bp as migration_bp
    from app.routes.pages import bp as pages_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(items_bp)
    app.register_blueprint(schedule_bp)
    app.register_blueprint(push_bp)
    app.register_blueprint(consultants_bp)
    app.register_blueprint(holidays_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(gantt_bp)
    app.register_blueprint(migration_bp)
    app.register_blueprint(pages_bp)

    @app.get("/health")
    def health():  # noqa: ANN202
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _seed_workflow_templates(app) -> None:
    """Upsert YAML workflow templates into the workflow_templates DB table."""
    import yaml as _yaml
    from app.db import Database, get_db
    logger = logging.getLogger(__name__)
    templates_dir = app.config.get("WORKFLOW_TEMPLATES_DIR", "")
    if not os.path.isdir(templates_dir):
        return
    try:
        db = Database(get_db())
        for fname in os.listdir(templates_dir):
            if not fname.endswith(".yaml"):
                continue
            fpath = os.path.join(templates_dir, fname)
            with open(fpath, encoding="utf-8") as fh:
                content = fh.read()
            data = _yaml.safe_load(content)
            name = data.get("template_name", fname.replace(".yaml", ""))
            db.upsert_workflow_template(name, content)
            logger.debug("Seeded workflow template: %s", name)
    except Exception as exc:
        logger.warning("Could not seed workflow templates: %s", exc)


def _load_workflows(templates_dir: str) -> dict[str, Workflow]:
    """
    Parse all YAML files in *templates_dir* into Workflow objects.
    Returns a dict keyed by template_name.
    """
    workflows: dict[str, Workflow] = {}
    logger = logging.getLogger(__name__)

    if not os.path.isdir(templates_dir):
        logger.warning("Workflow templates dir not found: %s", templates_dir)
        return workflows

    for fname in os.listdir(templates_dir):
        if not fname.endswith(".yaml"):
            continue
        fpath = os.path.join(templates_dir, fname)
        try:
            with open(fpath, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)

            phases = [
                WorkflowPhase(
                    name=p["name"],
                    order=p["order"],
                    default_effort_days=p.get("default_effort_days", 1),
                    effort_scale=p.get("effort_factor"),
                    required_roles=p.get("required_roles", []),
                    is_blocking=p.get("is_blocking", True),
                )
                for p in data.get("phases", [])
            ]
            phases.sort(key=lambda p: p.order)

            wf = Workflow(
                template_name=data["template_name"],
                applies_to=data.get("applies_to", []),
                phases=phases,
            )
            workflows[wf.template_name] = wf
            logger.info("Loaded workflow: %s", wf.template_name)

        except Exception as exc:
            logger.error("Failed to parse %s: %s", fname, exc)

    return workflows
