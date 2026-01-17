import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("SECRET_KEY", "test")

from app import create_app  # noqa: E402


def test_index_route():
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200


def test_assignments_route():
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    response = client.get("/assignments")
    assert response.status_code == 200
