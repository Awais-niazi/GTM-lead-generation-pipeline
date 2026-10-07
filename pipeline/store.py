"""SQLite store: dedupe + merge across channels, an audit trail, and the delivery log."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

from . import config as C
from .models import Lead, now_iso

RAW_FIELDS = [
    "name", "phone", "email", "city", "study_level", "subject", "highest_qualification",
    "grade_percent", "cgpa", "passing_year", "english_test", "english_score", "english_medium",
    "budget_pkr_lakh", "funding_source", "preferred_intake", "location_pref", "has_passport",
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


def get(lead_id: str) -> Lead | None:
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
    with closing(_conn()) as con, con:
        con.execute("INSERT OR REPLACE INTO leads VALUES (?,?,?,?)",
                    (lead.lead_id, lead.model_dump_json(), lead.tier, lead.updated_at))
        con.execute("INSERT INTO events (lead_id, source, at, payload) VALUES (?,?,?,?)",
                    (lead.lead_id, lead.source, now_iso(), json.dumps(payload or {})[:20000]))


def all_leads() -> list[Lead]:
    with closing(_conn()) as con:
        rows = con.execute("SELECT data FROM leads ORDER BY updated_at DESC").fetchall()
    return [Lead.model_validate_json(r[0]) for r in rows]


def log_delivery(lead: Lead, brief: str) -> bool:
    """Record a shipped lead. Returns False if it was already shipped (never overwritten)."""
    snapshot = lead.model_dump_json()
    digest = hashlib.sha256(f"{lead.lead_id}|{lead.shipped_at}|{brief}|{snapshot}".encode()).hexdigest()
    try:
        with closing(_conn()) as con, con:
            con.execute("INSERT INTO deliveries VALUES (?,?,?,?,?)",
                        (lead.lead_id, lead.shipped_at, brief, snapshot, digest))
    except sqlite3.IntegrityError:
        return False
    return True


def deliveries() -> list[dict]:
    with closing(_conn()) as con:
        rows = con.execute("SELECT lead_id, shipped_at, brief, sha256 FROM deliveries "
                           "ORDER BY shipped_at").fetchall()
    return [dict(zip(("lead_id", "shipped_at", "brief", "sha256"), r)) for r in rows]
