import pytest

from pipeline import config as C
from pipeline import store


@pytest.fixture(autouse=True, params=["sqlite", "dynamodb"])
def tmp_store(request, tmp_path, monkeypatch):
    """Every test runs against both backends: SQLite (local) and DynamoDB (AWS, simulated by moto)."""
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(C, "CSV_FALLBACK", str(tmp_path / "t.csv"))
    monkeypatch.setattr(C, "DELIVERY_CSV", str(tmp_path / "d.csv"))
    monkeypatch.setattr(C, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(C, "META_APP_SECRET", "")
    monkeypatch.setattr(C, "CALCOM_WEBHOOK_SECRET", "")
    monkeypatch.setattr(C, "BOOKING_URL", "https://cal.com/yourbrand/uk-call")
    monkeypatch.setattr(C, "STORE", request.param)
    store._tables.clear()
    if request.param == "sqlite":
        yield request.param
        return
    import boto3
    from moto import mock_aws

    from deploy.aws_deploy import ensure_tables
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                 "AWS_DEFAULT_REGION": "ap-south-1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(C, "AWS_REGION", "ap-south-1")
    # Load AWS-managed IAM policies so the deploy test can attach AWSLambdaBasicExecutionRole.
    with mock_aws(config={"iam": {"load_aws_managed_policies": True}}):
        ensure_tables(boto3.client("dynamodb", region_name="ap-south-1"))
        yield request.param
    store._tables.clear()
