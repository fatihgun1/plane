"""
run.py — Entry point for the Optimization Tool.
Usage: python run.py
"""

import logging
import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    app = create_app()

    with app.app_context():
        logger.info("Initialising database…")
        init_db()
        logger.info("Database ready.")

    host = app.config.get("HOST", "127.0.0.1")
    port = app.config.get("PORT", 5050)
    debug = app.config.get("DEBUG", True)

    logger.info("Starting Optimization Tool on http://%s:%s", host, port)
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
