"""Temporary import smoke-test — safe to delete after verification."""
import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from app.config import config
from app.db import init_db
from app.models import Consultant, Item, ScheduledTask, WorkflowTemplate
from app.calendar_engine import add_working_days, working_days_between

print("All imports OK")
print(f"Config loaded: {config.__class__.__name__}, DB_PATH={config.DB_PATH}")
