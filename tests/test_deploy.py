"""Run the AWS deploy against moto's simulated AWS: checks every API call is well-formed and
that the setup stays inside the Always Free limits."""
import json

import boto3
import pytest

from deploy import aws_deploy as D


@pytest.fixture
def aws(monkeypatch, tmp_path, tmp_store):
    if tmp_store != "dynamodb":
        pytest.skip("deploy runs once, under the simulated-AWS fixture")
    env = tmp_path / ".env"
    env.write_text('BRAND_NAME="Enrolliq"\nQUIZ_SHARED_KEY=k\nCALCOM_WEBHOOK_SECRET=s\n'
                   "GOOGLE_SHEET_ID=sheet\nDB_PATH=leads.db\n")
    return boto3.Session(region_name="ap-south-1"), env


def test_deploy_creates_free_tier_stack(aws):
    session, env = aws
    url = D.deploy(session, env, code=b"PK\x05\x06" + b"\x00" * 18)     # empty zip stands in for the build
    assert url.startswith("https://")

    lam = session.client("lambda")
    cfg = lam.get_function_configuration(FunctionName=D.FUNCTION)
    v = cfg["Environment"]["Variables"]
    assert v["STORE"] == "dynamodb" and v["BRAND_NAME"] == "Enrolliq" and "DB_PATH" not in v
    assert v["GOOGLE_SERVICE_ACCOUNT_SSM"] == D.SSM_KEY

    ddb = session.client("dynamodb")
    tables = [ddb.describe_table(TableName=f"enrolliq-{t[0]}")["Table"] for t in D.TABLES]
    assert all(t["ProvisionedThroughput"]["ReadCapacityUnits"] > 0 for t in tables)   # provisioned, not on-demand
    assert sum(t["ProvisionedThroughput"]["ReadCapacityUnits"] for t in tables) <= 25
    assert sum(t["ProvisionedThroughput"]["WriteCapacityUnits"] for t in tables) <= 25

    pol = json.loads(json.dumps(session.client("iam").get_role_policy(
        RoleName="enrolliq-api-role", PolicyName="enrolliq-api-role-policy")["PolicyDocument"]))
    deny = [s for s in pol["Statement"] if s["Effect"] == "Deny"][0]
    assert "dynamodb:UpdateItem" in deny["Action"] and deny["Resource"].endswith("enrolliq-deliveries")

    names = {s["Name"] for s in session.client("scheduler").list_schedules()["Schedules"]}
    assert names == {"enrolliq-sync", "enrolliq-rescore"}

    assert D.deploy(session, env, code=b"PK\x05\x06" + b"\x00" * 18) == url   # re-run is idempotent
