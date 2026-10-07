import csv
import hashlib
import hmac
import json
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from pipeline import config as C
from pipeline import store, workflow
from pipeline.models import Lead
from pipeline.process import process

TODAY = date(2026, 10, 7)


def hot_quiz(**kw):
    base = dict(name="Ayesha Khan", phone="03001234567", email="ayesha@gmail.com", city="Lahore",
                study_level="pg_taught", subject="Data Science", highest_qualification="bachelors_16",
                cgpa=3.4, passing_year=2025, english_test="ielts", english_score=7.0,
                budget_pkr_lakh=165, funding_source="self_family", preferred_intake="Jan 2027",
                location_pref="outside", has_passport=True, consent=True)
    return Lead(**{**base, **kw})


def edit_csv(path, lead_id, **changes):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fields = list(rows[0].keys())
    for r in rows:
        if r["lead_id"] == lead_id:
            r.update(changes)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def delivery_rows():
    with open(C.DELIVERY_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def quiz_payload(lead):
    return {**json.loads(lead.model_dump_json(include={
        "name", "phone", "email", "city", "study_level", "highest_qualification", "cgpa",
        "english_test", "english_score", "budget_pkr_lakh", "has_passport", "preferred_intake"})),
        "consent": True}


def test_quiz_offers_call_only_to_worthwhile_leads():
    from app import app
    c = TestClient(app)
    r = c.post("/webhook/quiz", json=quiz_payload(hot_quiz())).json()
    lid = store.all_leads()[0].lead_id
    assert r["book_call"] and f"metadata%5Blead_id%5D={lid}" in r["booking_url"]
    r = c.post("/webhook/quiz", json=quiz_payload(hot_quiz(phone="03111111111", budget_pkr_lakh=30))).json()
    assert r["book_call"] is False and r["booking_url"] == ""


def test_booking_webhook_moves_lead_to_call_booked(monkeypatch):
    from app import app
    monkeypatch.setattr(C, "CALCOM_WEBHOOK_SECRET", "s3cret")
    lead = process(hot_quiz(), today=TODAY, notify=False)
    body = json.dumps({"triggerEvent": "BOOKING_CREATED", "payload": {
        "uid": "bk1", "startTime": "2026-10-09T10:00:00Z", "metadata": {"lead_id": lead.lead_id},
        "attendees": [{"name": "Ayesha Khan", "email": "ayesha@gmail.com"}]}}).encode()
    c = TestClient(app)
    assert c.post("/webhook/booking", content=body, headers={"X-Cal-Signature-256": "bad"}).status_code == 401
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    r = c.post("/webhook/booking", content=body, headers={"X-Cal-Signature-256": sig})
    assert r.json() == {"ok": True, "matched": True}
    got = store.get(lead.lead_id)
    assert got.stage == "Call booked" and got.call_at == "2026-10-09T10:00:00Z"
    assert "Qualification call 2026-10-09 10:00" in got.next_action


def test_cooked_ships_once_and_only_with_handoff_consent():
    lead = process(hot_quiz(), today=TODAY, notify=False)
    edit_csv(C.CSV_FALLBACK, lead.lead_id, stage="Cooked", decision_maker="Parent")
    workflow.sync(TODAY)
    blocked = store.get(lead.lead_id)
    assert blocked.stage == "Cooked" and store.deliveries() == []
    assert "handoff_consent" in blocked.next_action

    edit_csv(C.CSV_FALLBACK, lead.lead_id, handoff_consent="Yes", notes="Father paying, funds ready")
    report = workflow.sync(TODAY)
    shipped = store.get(lead.lead_id)
    assert shipped.stage == "Shipped" and any("SHIPPED" in r for r in report)
    assert shipped.handoff_link.startswith("https://wa.me/923001234567?text=")
    [logged] = store.deliveries()
    assert "Decision maker: Parent" in logged["brief"] and "Father paying" in logged["brief"]
    [row] = delivery_rows()
    assert row["lead_id"] == lead.lead_id and "Money: budget 165" in row["brief"]

    assert workflow.sync(TODAY) == []                # nothing changed → nothing re-shipped
    assert len(store.deliveries()) == 1 and len(delivery_rows()) == 1


def test_delivery_log_is_write_once():
    lead = process(hot_quiz(handoff_consent=True), today=TODAY, notify=False)
    edit_csv(C.CSV_FALLBACK, lead.lead_id, stage="Cooked", handoff_consent="Yes")
    workflow.sync(TODAY)
    con = sqlite3.connect(C.DB_PATH)
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        con.execute("UPDATE deliveries SET brief='x'")
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        con.execute("DELETE FROM deliveries")


def test_agency_updates_flow_back_and_paid_sticks():
    lead = process(hot_quiz(), today=TODAY, notify=False)
    edit_csv(C.CSV_FALLBACK, lead.lead_id, stage="Cooked", handoff_consent="Yes")
    workflow.sync(TODAY)
    edit_csv(C.DELIVERY_CSV, lead.lead_id, agency_contacted_on="08/10/2026")
    workflow.sync(TODAY)
    assert store.get(lead.lead_id).stage == "Contacted"

    edit_csv(C.DELIVERY_CSV, lead.lead_id, agency_stage="Visa granted")
    workflow.sync(TODAY)
    assert store.get(lead.lead_id).stage == "Visa granted"
    assert any("Payment due" in t for t in workflow.attention(TODAY))

    edit_csv(C.CSV_FALLBACK, lead.lead_id, stage="Paid")
    workflow.sync(TODAY)
    workflow.sync(TODAY)                             # agency tab still says Visa granted
    paid = store.get(lead.lead_id)
    assert paid.stage == "Paid" and set(paid.stage_history) >= {"Shipped", "Contacted", "Visa granted", "Paid"}


def test_unsynced_sheet_edit_survives_resubmission():
    lead = process(hot_quiz(), today=TODAY, notify=False)
    edit_csv(C.CSV_FALLBACK, lead.lead_id, stage="Not yet", revisit_on="2027-01-10", notes="waiting on IELTS")
    again = process(hot_quiz(subject="AI"), today=TODAY, notify=False)
    assert again.stage == "Not yet" and again.notes == "waiting on IELTS" and again.subject == "AI"
    assert workflow.attention(date(2027, 1, 10)) == [f"Revisit due: Ayesha Khan ({lead.lead_id})"]


def test_stop_message_opts_out():
    from app import _handle_meta
    lead = process(hot_quiz(), today=TODAY, notify=False)
    _handle_meta({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messages": [{"from": "923001234567", "id": "wamid.9", "text": {"body": "STOP"}}]}}]}]})
    got = store.get(lead.lead_id)
    assert got.stage == "Opted out" and got.consent is False and not got.call_eligible


def test_csv_neutralises_formulas():
    lead = process(hot_quiz(name='=HYPERLINK("http://x","hi")'), today=TODAY, notify=False)
    raw = open(C.CSV_FALLBACK, encoding="utf-8").read()
    assert "'=HYPERLINK" in raw and ",+923001234567," in raw   # phone left alone
    workflow.sync(TODAY)                                       # reading back strips the guard
    assert store.get(lead.lead_id).name.startswith("=HYPERLINK")
