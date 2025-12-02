import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_PATH = REPO_ROOT / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))


from utils.onedrive_client import OneDriveClient  # noqa: E402


class DummyItem:
    def __init__(self, name: str, modified: datetime, is_file: bool = True):
        self.name = name
        self.modified = modified
        self.created = modified
        self.is_file = is_file


class DummyFolder:
    def __init__(self, items):
        self._items = items

    def get_items(self):
        return self._items


def call_list_all_documents(folder, extensions, month=None):
    client = OneDriveClient.__new__(OneDriveClient)
    return client.list_all_documents(folder, extensions, month)


def test_list_all_documents_filters_by_extension_and_month():
    items = [
        DummyItem("invoice001.pdf", datetime(2025, 12, 2, tzinfo=timezone.utc)),
        DummyItem("receipt002.png", datetime(2025, 12, 1, tzinfo=timezone.utc)),
        DummyItem("note.txt", datetime(2025, 12, 1, tzinfo=timezone.utc)),
        DummyItem("legacy.jpg", datetime(2025, 11, 29, tzinfo=timezone.utc)),
    ]
    folder = DummyFolder(items)

    docs = call_list_all_documents(
        folder,
        extensions=['.pdf', '.png', '.jpg'],
        month='2025-12',
    )

    names = [doc.name for doc in docs]
    assert "invoice001.pdf" in names
    assert "receipt002.png" in names
    assert "note.txt" not in names
    assert "legacy.jpg" not in names  # filtered out by month
