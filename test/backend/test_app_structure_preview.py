import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_PATH = REPO_ROOT / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))


from app import app  # noqa: E402
from db.database import get_db  # noqa: E402


class FakeQuery:
    def __init__(self, schedule):
        self._schedule = schedule

    def filter(self, *_, **__):
        return self

    def first(self):
        return self._schedule


class FakeSession:
    def __init__(self, schedule):
        self._schedule = schedule

    def query(self, _model):
        return FakeQuery(self._schedule)


def override_db_with(schedule):
    def _override():
        yield FakeSession(schedule)

    return _override


@pytest.fixture(autouse=True)
def reset_overrides():
    original = dict(app.dependency_overrides)
    yield
    app.dependency_overrides = original


def make_schedule(**overrides):
    defaults = {
        "schedule_id": 1,
        "name": "Demo Schedule",
        "auto_month_folders": True,
        "schedule_root_path": "HYA-OCR/Schedule",
        "material_subfolder_name": "Material",
        "history_subfolder_name": "history",
        "output_filename_pattern": "{YYYYMM}.xlsx",
        "failed_subfolder_name": "_Failed",
        "month_folder_pattern": "{YYYYMM}",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_structure_preview_endpoint_returns_paths_for_auto_schedule():
    schedule = make_schedule()
    app.dependency_overrides[get_db] = override_db_with(schedule)

    client = TestClient(app)
    response = client.get("/ocr-schedules/1/structure-preview?month=2025-12")

    assert response.status_code == 200
    data = response.json()
    assert data["schedule_id"] == 1
    assert data["month_folder_path"].endswith("/202512")
    assert data["material_folder_path"].endswith("/Material")


def test_structure_preview_endpoint_returns_404_when_schedule_missing():
    app.dependency_overrides[get_db] = override_db_with(None)

    client = TestClient(app)
    response = client.get("/ocr-schedules/999/structure-preview")

    assert response.status_code == 404


def test_structure_preview_endpoint_validates_month_format():
    schedule = make_schedule()
    app.dependency_overrides[get_db] = override_db_with(schedule)

    client = TestClient(app)
    response = client.get("/ocr-schedules/1/structure-preview?month=December-2025")

    assert response.status_code == 400
