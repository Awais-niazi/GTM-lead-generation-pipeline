"""Lead store: dedupe + merge across channels, an audit trail, and the write-once delivery log.

Two backends behind the same functions:
  * SQLite   (default; local runs and tests)        — DB_PATH
  * DynamoDB (STORE=dynamodb; AWS Lambda)           — tables <DYNAMO_PREFIX>-leads / -events /
                                                       -deliveries, created by deploy/aws_deploy.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing

from . import config as C
from .models import Lead, now_iso

RAW_FIELDS = [
    "name", "phone", "email", "city", "study_level", "subject", "highest_qualification",
    "grade_percent", "cgpa", "passing_year", "english_test", "english_score", "english_medium",
    "budget_pkr_lakh", "funds_proof", "funding_source", "preferred_intake", "location_pref", "has_passport",
    "previous_uk_refusal", "bring_dependants", "utm_source", "utm_campaign",
]


def _conn():
    con = sqlite3.connect(C.DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS leads (
        lead_id TEXT PRIMARY KEY, data TEXT NOT NULL, tier TEXT, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, source TEXT, at TEXT, payload TEXT)""")
    # Proof of origination: one row per shipped lead, never updated or deleted.
    con.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        lead_id TEXT PRIMARY KEY, shipped_at TEXT NOT NULL, brief TEXT NOT NULL,
        snapshot TEXT NOT NULL, sha256 TEXT NOT NULL)""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS deliveries_no_update BEFORE UPDATE ON deliveries
        BEGIN SELECT RAISE(ABORT, 'delivery log is write-once'); END""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS deliveries_no_delete BEFORE DELETE ON deliveries
        BEGIN SELECT RAISE(ABORT, 'delivery log is write-once'); END""")
    return con


def _dynamo() -> bool:
    return C.STORE == "dynamodb"


_tables: dict[str, object] = {}


def _table(name: str):
    key = f"{C.DYNAMO_PREFIX}-{name}"
    if key not in _tables:
        import boto3
        _tables[key] = boto3.resource("dynamodb", region_name=C.AWS_REGION or None).Table(key)
    return _tables[key]


def _scan(name: str) -> list[dict]:
    table, items, kw = _table(name), [], {}
    while True:
        page = table.scan(**kw)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get(lead_id: str) -> Lead | None:
    if _dynamo():
        item = _table("leads").get_item(Key={"lead_id": lead_id}).get("Item")
        return Lead.model_validate_json(item["data"]) if item else None
    with closing(_conn()) as con:
        row = con.execute("SELECT data FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
    return Lead.model_validate_json(row[0]) if row else None


def merge(old: Lead, new: Lead) -> Lead:
    """Newer non-empty answers win; provenance, consent and history are preserved."""
    m = old.model_copy(deep=True)
    for f in RAW_FIELDS:
        v = getattr(new, f)
        if v not in (None, "", []) and not (f == "location_pref" and v == "any"):
            setattr(m, f, v)
    if new.source == "web_quiz" and old.source != "web_quiz":
        m.source_ref = m.source_ref or new.source_ref
    if new.consent and not old.consent:
        m.consent, m.consent_text, m.consent_at = True, new.consent_text, new.consent_at
    m.other_countries = sorted(set(old.other_countries) | set(new.other_countries))
    if new.message and new.message not in old.message:
        m.message = (old.message + "\n" + new.message).strip()
    m.updated_at = now_iso()
    return m


def save(lead: Lead, payload: dict | None = None) -> None:
    if _dynamo():
        _table("leads").put_item(Item={"lead_id": lead.lead_id, "data": lead.model_dump_json(),
                                       "tier": lead.tier, "updated_at": lead.updated_at})
        # Payloads are trimmed harder here: every KB written costs a unit of the free write capacity.
        _table("events").put_item(Item={"lead_id": lead.lead_id, "at": f"{now_iso()}#{uuid.uuid4().hex[:8]}",
                                        "source": lead.source, "payload": json.dumps(payload or {})[:4000]})
        return
    with closing(_conn()) as con, con:
        con.execute("INSERT OR REPLACE INTO leads VALUES (?,?,?,?)",
                    (lead.lead_id, lead.model_dump_json(), lead.tier, lead.updated_at))
        con.execute("INSERT INTO events (lead_id, source, at, payload) VALUES (?,?,?,?)",
                    (lead.lead_id, lead.source, now_iso(), json.dumps(payload or {})[:20000]))


def all_leads() -> list[Lead]:
    if _dynamo():
        items = sorted(_scan("leads"), key=lambda i: i.get("updated_at", ""), reverse=True)
        return [Lead.model_validate_json(i["data"]) for i in items]
    with closing(_conn()) as con:
        rows = con.execute("SELECT data FROM leads ORDER BY updated_at DESC").fetchall()
    return [Lead.model_validate_json(r[0]) for r in rows]


def log_delivery(lead: Lead, brief: str) -> bool:
    """Record a shipped lead. Returns False if it was already shipped (never overwritten)."""
    snapshot = lead.model_dump_json()
    digest = hashlib.sha256(f"{lead.lead_id}|{lead.shipped_at}|{brief}|{snapshot}".encode()).hexdigest()
    if _dynamo():
        # Write-once: the conditional put refuses an existing id, and the Lambda's IAM role is
        # denied UpdateItem/DeleteItem on this table (see deploy/aws_deploy.py).
        from botocore.exceptions import ClientError
        try:
            _table("deliveries").put_item(
                Item={"lead_id": lead.lead_id, "shipped_at": lead.shipped_at, "brief": brief,
                      "snapshot": snapshot, "sha256": digest},
                ConditionExpression="attribute_not_exists(lead_id)")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True
    try:
        with closing(_conn()) as con, con:
            con.execute("INSERT INTO deliveries VALUES (?,?,?,?,?)",
                        (lead.lead_id, lead.shipped_at, brief, snapshot, digest))
    except sqlite3.IntegrityError:
        return False
    return True


def deliveries() -> list[dict]:
    if _dynamo():
        return [{k: i[k] for k in ("lead_id", "shipped_at", "brief", "sha256")}
                for i in sorted(_scan("deliveries"), key=lambda i: i["shipped_at"])]
    with closing(_conn()) as con:
        rows = con.execute("SELECT lead_id, shipped_at, brief, sha256 FROM deliveries "
                           "ORDER BY shipped_at").fetchall()
    return [dict(zip(("lead_id", "shipped_at", "brief", "sha256"), r)) for r in rows]
