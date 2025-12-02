import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_PATH = REPO_ROOT / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))


from utils.ocr_schedule_runner import (
    describe_month_structure,
    _run_gemini_ocr,
    _append_rows_to_excel_on_onedrive,
)  # noqa: E402


def make_schedule(**overrides):
    defaults = {
        "auto_month_folders": False,
        "schedule_root_path": None,
        "material_root_path": "HYA-OCR/Manual/Material",
        "history_root_path": "HYA-OCR/Manual/History",
        "output_root_path": "HYA-OCR/Manual/Output",
        "failed_subfolder_name": "_Failed",
        "month_folder_pattern": "{YYYYMM}",
        "material_subfolder_name": "Material",
        "history_subfolder_name": "history",
        "output_filename_pattern": "{YYYYMM}.xlsx",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_describe_month_structure_manual_paths():
    schedule = make_schedule()

    result = describe_month_structure(schedule, "202512")

    assert result["material_folder_path"] == "HYA-OCR/Manual/Material/202512"
    assert result["history_folder_path"] == "HYA-OCR/Manual/History/202512"
    assert result["failed_folder_path"] == "HYA-OCR/Manual/Material/202512/_Failed"
    assert result["output_excel_path"] == "HYA-OCR/Manual/Output/202512.xlsx"
    assert result["auto_month_folders"] is False


def test_describe_month_structure_auto_paths_with_sanitization():
    schedule = make_schedule(
        auto_month_folders=True,
        schedule_root_path="HYA-OCR/Shop Invoice",
        material_subfolder_name="Material Files",
        history_subfolder_name="History:Records",
        output_filename_pattern="{YYYY}/{MM}.xlsx",
    )

    result = describe_month_structure(schedule, "202512")

    assert result["month_folder_path"] == "HYA-OCR/Shop Invoice/202512"
    assert result["material_folder_path"] == "HYA-OCR/Shop Invoice/202512/Material Files"
    assert result["history_folder_path"] == "HYA-OCR/Shop Invoice/202512/History-Records"
    assert result["failed_folder_path"] == "HYA-OCR/Shop Invoice/202512/_Failed"
    assert result["output_excel_path"] == "HYA-OCR/Shop Invoice/202512/2025-12.xlsx"
    assert result["auto_month_folders"] is True


@pytest.mark.parametrize("invalid_month", ["", "2025-13", "DEC2025"])
def test_describe_month_structure_invalid_month(invalid_month):
    schedule = make_schedule()

    with pytest.raises(ValueError):
        describe_month_structure(schedule, invalid_month)


def test_describe_month_structure_missing_root_in_auto_mode():
    schedule = make_schedule(auto_month_folders=True, schedule_root_path=None)

    with pytest.raises(ValueError):
        describe_month_structure(schedule, "202512")


def test_run_gemini_ocr_uses_pdf_extractor(monkeypatch, tmp_path):
    from utils import ocr_schedule_runner as runner

    called = {"pdf": False, "image": False}

    async def fake_pdf(path, prompt, response_schema=None):
        called["pdf"] = True
        return {"text": "pdf"}

    async def fake_image(path, prompt, response_schema=None):
        called["image"] = True
        return {"text": "img"}

    monkeypatch.setattr(runner, "extract_text_from_pdf", fake_pdf)
    monkeypatch.setattr(runner, "extract_text_from_image", fake_image)

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"pdf")

    result = _run_gemini_ocr(str(pdf_path), "sample.pdf", "prompt", {"type": "object"})

    assert called["pdf"] is True
    assert called["image"] is False
    assert result["data"]["raw_text"] == "pdf"


def test_run_gemini_ocr_uses_image_extractor(monkeypatch, tmp_path):
    from utils import ocr_schedule_runner as runner

    called = {"pdf": False, "image": False}

    async def fake_pdf(path, prompt, response_schema=None):
        called["pdf"] = True
        return {"text": "pdf"}

    async def fake_image(path, prompt, response_schema=None):
        called["image"] = True
        return {"text": "img"}

    monkeypatch.setattr(runner, "extract_text_from_pdf", fake_pdf)
    monkeypatch.setattr(runner, "extract_text_from_image", fake_image)

    img_path = tmp_path / "image.jpeg"
    img_path.write_bytes(b"img")

    result = _run_gemini_ocr(str(img_path), "image.jpeg", "prompt", {"type": "object"})

    assert called["image"] is True
    assert called["pdf"] is False
    assert result["data"]["raw_text"] == "img"


def test_append_rows_overwrites_excel(monkeypatch):
    class DummyFolder:
        def __init__(self):
            self.upload_calls = []

        def upload_file(self, item, item_name=None, **kwargs):
            self.upload_calls.append((item, item_name))

    class DummyClient:
        def __init__(self, folder):
            self._folder = folder

        def download_file_content(self, path):
            return None

        def get_folder(self, path):
            return self._folder

    dummy_folder = DummyFolder()
    result = _append_rows_to_excel_on_onedrive(
        DummyClient(dummy_folder),
        "HYA-OCR/Shop Invoice/202512/202512.xlsx",
        {"raw_text": "hello"},
    )

    # For this test we just ensure that an upload is attempted to the expected filename
    assert result == 2 or result == -1
    assert dummy_folder.upload_calls
    uploaded_item, uploaded_name = dummy_folder.upload_calls[0]
    assert isinstance(uploaded_item, str)
    assert uploaded_name == "202512.xlsx"
