"""AWS Lambda entry point.

  * HTTP requests arrive through the Function URL and are served by the FastAPI app (via Mangum).
  * EventBridge Scheduler invokes it with {"task": "sync"} every 10 minutes and
    {"task": "rescore"} once a day (see deploy/aws_deploy.py).
"""
from __future__ import annotations

import json

from mangum import Mangum

import run
from app import app
from pipeline import workflow

_http = Mangum(app, lifespan="off")


def handler(event, context):
    task = event.get("task") if isinstance(event, dict) else None
    if task == "sync":
        changes = workflow.sync()
        print(json.dumps({"task": "sync", "changes": changes}))
        return {"ok": True, "changes": len(changes)}
    if task == "rescore":
        run.rescore()
        return {"ok": True}
    return _http(event, context)
