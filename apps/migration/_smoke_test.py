import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app

app = create_app()
print("OK — blueprints:", list(app.blueprints.keys()))
print("OK — workflows:", list(app.config["WORKFLOWS"].keys()))
