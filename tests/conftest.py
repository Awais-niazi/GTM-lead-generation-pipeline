import pytest

from pipeline import config as C


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(C, "CSV_FALLBACK", str(tmp_path / "t.csv"))
    monkeypatch.setattr(C, "DELIVERY_CSV", str(tmp_path / "d.csv"))
    monkeypatch.setattr(C, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(C, "META_APP_SECRET", "")
    monkeypatch.setattr(C, "CALCOM_WEBHOOK_SECRET", "")
    monkeypatch.setattr(C, "BOOKING_URL", "https://cal.com/yourbrand/uk-call")
