"""Deploy the pipeline to AWS using only Always Free services. Safe to re-run: it creates what's
missing and updates what exists.

    .venv/bin/python deploy/aws_deploy.py            # build + deploy everything
    .venv/bin/python deploy/aws_deploy.py --code     # just ship new code (faster)

Creates, in your AWS CLI default region (`aws configure get region`; override with --region):
  * DynamoDB tables  enrolliq-leads / -events / -deliveries  — PROVISIONED capacity, 25 RCU / 25 WCU
                                                               in total (the Always Free allowance)
  * SSM SecureString /enrolliq/google-service-account        — the Google key, encrypted
  * IAM roles        for the function and for the scheduler
  * Lambda           enrolliq-api + a public Function URL     — code uploaded directly (no S3)
  * Schedules        sync every 10 min, rescore daily 06:00 PKT
  * Log group        14-day retention

Settings come from .env (the same file the local app uses). Never deploys: EC2, RDS, NAT gateways,
load balancers, API Gateway or S3 — the things that cost money.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
NAME = "enrolliq"
FUNCTION = f"{NAME}-api"
SSM_KEY = f"/{NAME}/google-service-account"
RUNTIME = "python3.12"

# (name, key schema, read units, write units). Totals must stay ≤ 25 / 25 to remain free.
TABLES = [
    ("leads", [("lead_id", "HASH")], 18, 14),
    ("events", [("lead_id", "HASH"), ("at", "RANGE")], 2, 8),
    ("deliveries", [("lead_id", "HASH")], 3, 2),
]
assert sum(t[2] for t in TABLES) <= 25 and sum(t[3] for t in TABLES) <= 25

# .env keys copied to the function. Local-only ones (file paths, DB_PATH) are left out.
ENV_KEYS = [
    "BRAND_NAME", "PARTNER_NAME", "QUIZ_URL", "QUIZ_SHARED_KEY", "BOOKING_URL", "CALCOM_WEBHOOK_SECRET",
    "TIMEZONE", "GOOGLE_SHEET_ID", "SHEET_TAB", "DELIVERY_SHEET_ID", "DELIVERY_TAB",
    "META_VERIFY_TOKEN", "META_APP_SECRET", "META_PAGE_ACCESS_TOKEN", "META_GRAPH_VERSION",
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "ALERT_WHATSAPP", "AGENCY_WHATSAPP", "GBP_TO_PKR",
]


def say(msg: str) -> None:
    print(f"• {msg}", flush=True)


# --------------------------------------------------------------------------- DynamoDB
def ensure_tables(ddb, prefix: str = NAME) -> None:
    existing = set(ddb.list_tables()["TableNames"])
    for name, keys, rcu, wcu in TABLES:
        full = f"{prefix}-{name}"
        if full in existing:
            continue
        ddb.create_table(
            TableName=full,
            KeySchema=[{"AttributeName": a, "KeyType": t} for a, t in keys],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a, _ in keys],
            BillingMode="PROVISIONED",          # on-demand is NOT covered by Always Free
            ProvisionedThroughput={"ReadCapacityUnits": rcu, "WriteCapacityUnits": wcu},
        )
        say(f"created table {full} ({rcu} RCU / {wcu} WCU)")
    for name, *_ in TABLES:
        ddb.get_waiter("table_exists").wait(TableName=f"{prefix}-{name}")


# --------------------------------------------------------------------------- IAM
def _role(iam, name: str, service: str, policy: dict, managed: list[str] = ()) -> str:
    trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        say(f"created role {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=name, PolicyName=f"{name}-policy", PolicyDocument=json.dumps(policy))
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    return arn


def function_policy(region: str, account: str) -> dict:
    table = lambda n: f"arn:aws:dynamodb:{region}:{account}:table/{NAME}-{n}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow",
         "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan", "dynamodb:Query"],
         "Resource": [table("leads"), table("events"), table("deliveries")]},
        # The delivery log is your proof of origination: the function can add to it, never change it.
        {"Effect": "Deny", "Action": ["dynamodb:UpdateItem", "dynamodb:DeleteItem",
                                      "dynamodb:BatchWriteItem", "dynamodb:DeleteTable"],
         "Resource": table("deliveries")},
        {"Effect": "Allow", "Action": "ssm:GetParameter",
         "Resource": f"arn:aws:ssm:{region}:{account}:parameter{SSM_KEY}"},
        {"Effect": "Allow", "Action": "kms:Decrypt", "Resource": "*",
         "Condition": {"StringEquals": {"kms:ViaService": f"ssm.{region}.amazonaws.com"}}},
    ]}


# --------------------------------------------------------------------------- code package
def build_zip() -> bytes:
    build = ROOT / "deploy" / ".build"
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True)
    say("installing Lambda dependencies (Linux x86_64 wheels)…")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "-q", "--target", str(build),
        "--platform", "manylinux2014_x86_64", "--implementation", "cp",
        "--python-version", RUNTIME.removeprefix("python"), "--only-binary=:all:",
        "-r", str(ROOT / "requirements-lambda.txt")])
    for f in ("app.py", "run.py", "lambda_function.py"):
        shutil.copy(ROOT / f, build / f)
    shutil.copytree(ROOT / "pipeline", build / "pipeline", ignore=shutil.ignore_patterns("__pycache__"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(build.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, p.relative_to(build))
    data = buf.getvalue()
    say(f"package is {len(data) / 1e6:.1f} MB")
    if len(data) > 50_000_000:
        sys.exit("package over 50 MB — Lambda would need S3, which isn't free")
    return data


# --------------------------------------------------------------------------- Lambda
def ensure_function(lam, role_arn: str, code: bytes, env: dict) -> str:
    config = dict(FunctionName=FUNCTION, Runtime=RUNTIME, Role=role_arn, Handler="lambda_function.handler",
                  MemorySize=512, Timeout=300, Environment={"Variables": env})
    arch = ["x86_64"]                            # set with the code: update_function_configuration rejects it
    try:
        lam.get_function(FunctionName=FUNCTION)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        for attempt in range(10):                # a brand-new role takes a few seconds to be usable
            try:
                arn = lam.create_function(Code={"ZipFile": code}, Architectures=arch, **config)["FunctionArn"]
                break
            except ClientError as e2:
                if "cannot be assumed" not in str(e2) or attempt == 9:
                    raise
                time.sleep(5)
        say(f"created function {FUNCTION}")
        lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION)
        return arn
    lam.update_function_code(FunctionName=FUNCTION, ZipFile=code, Architectures=arch)
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
    arn = lam.update_function_configuration(**config)["FunctionArn"]
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
    say(f"updated function {FUNCTION}")
    return arn


def ensure_url(lam) -> str:
    try:
        url = lam.get_function_url_config(FunctionName=FUNCTION)["FunctionUrl"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        url = lam.create_function_url_config(FunctionName=FUNCTION, AuthType="NONE")["FunctionUrl"]
        say("created public Function URL")
    # Public URLs need both permissions; the app itself checks Cal.com / Meta signatures and the quiz key.
    for sid, extra in (("public-url", {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"}),
                       ("public-url-invoke", {"Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True})):
        try:
            lam.add_permission(FunctionName=FUNCTION, StatementId=sid, Principal="*", **extra)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceConflictException":
                raise
    return url


# --------------------------------------------------------------------------- schedules + logs
def ensure_schedules(sched, fn_arn: str, role_arn: str) -> None:
    for name, expr, task in ((f"{NAME}-sync", "rate(10 minutes)", "sync"),
                             (f"{NAME}-rescore", "cron(0 6 * * ? *)", "rescore")):
        spec = dict(Name=name, ScheduleExpression=expr, ScheduleExpressionTimezone="Asia/Karachi",
                    FlexibleTimeWindow={"Mode": "OFF"}, State="ENABLED",
                    Target={"Arn": fn_arn, "RoleArn": role_arn, "Input": json.dumps({"task": task}),
                            "RetryPolicy": {"MaximumRetryAttempts": 0}})
        for attempt in range(12):                # a brand-new role takes a few seconds to be assumable
            try:
                try:
                    sched.create_schedule(**spec)
                    say(f"created schedule {name} ({expr})")
                except ClientError as e:
                    if e.response["Error"]["Code"] != "ConflictException":
                        raise
                    sched.update_schedule(**spec)
                break
            except ClientError as e:
                if "assume the role" not in str(e) or attempt == 11:
                    raise
                time.sleep(5)


def ensure_logs(logs) -> None:
    group = f"/aws/lambda/{FUNCTION}"
    try:
        logs.create_log_group(logGroupName=group)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    logs.put_retention_policy(logGroupName=group, retentionInDays=14)   # keeps storage inside the free 5 GB


# --------------------------------------------------------------------------- landing page
def write_site(url: str, env: dict) -> Path:
    """The quiz page with the live API filled in, ready to drag onto Netlify."""
    page = (ROOT / "landing" / "uk-eligibility.html").read_text(encoding="utf-8")
    for old, new in (('const PIPELINE_URL = "";', f'const PIPELINE_URL = "{url}webhook/quiz";'),
                     ('const QUIZ_KEY = "";', f'const QUIZ_KEY = "{env.get("QUIZ_SHARED_KEY", "")}";')):
        assert old in page, old
        page = page.replace(old, new)
    site = ROOT / "deploy" / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(page, encoding="utf-8")
    return site


# --------------------------------------------------------------------------- main
def deploy(session, env_file: Path, code_only: bool = False, code: bytes | None = None) -> str:
    from dotenv import dotenv_values

    region = session.region_name
    account = session.client("sts").get_caller_identity()["Account"]
    dot = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    env = {k: dot[k] for k in ENV_KEYS if dot.get(k)}
    env.update(STORE="dynamodb", DYNAMO_PREFIX=NAME, GOOGLE_SERVICE_ACCOUNT_SSM=SSM_KEY,
               CSV_FALLBACK="/tmp/leads_export.csv", DELIVERY_CSV="/tmp/delivery_export.csv")
    say(f"account {account}, region {region}")

    iam = session.client("iam")
    fn_role = _role(iam, f"{NAME}-api-role", "lambda.amazonaws.com", function_policy(region, account),
                    ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"])
    if not code_only:
        ensure_tables(session.client("dynamodb"))
        key_file = ROOT / dot.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        if key_file.exists():
            session.client("ssm").put_parameter(Name=SSM_KEY, Value=key_file.read_text(), Type="SecureString",
                                                Overwrite=True, Tier="Standard")
            say(f"stored Google key in SSM {SSM_KEY}")
        ensure_logs(session.client("logs"))

    lam = session.client("lambda")
    fn_arn = ensure_function(lam, fn_role, code if code is not None else build_zip(), env)
    url = ensure_url(lam)
    if not code_only:
        sched_role = _role(iam, f"{NAME}-scheduler-role", "scheduler.amazonaws.com", {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": fn_arn}]})
        ensure_schedules(session.client("scheduler"), fn_arn, sched_role)
    return url


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default=None, help="default: your AWS CLI region")
    ap.add_argument("--profile", default=None, help="AWS CLI profile (default: your default credentials)")
    ap.add_argument("--code", action="store_true", help="only rebuild and upload the code")
    args = ap.parse_args()
    env_file = ROOT / ".env"
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if not session.region_name:
        sys.exit("no AWS region set: run `.venv/bin/aws configure set region <region>` or pass --region")
    url = deploy(session, env_file, code_only=args.code)

    from dotenv import dotenv_values
    site = write_site(url, dotenv_values(env_file))
    print(f"""
Done.
  API:              {url}
  Health check:     {url}health
  Cal.com webhook:  {url}webhook/booking
  Quiz page ready:  {site / 'index.html'}  → drag the '{site.name}' folder onto app.netlify.com/drop
""")


if __name__ == "__main__":
    main()
