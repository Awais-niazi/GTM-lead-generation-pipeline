from datetime import date

import pytest
from fastapi.testclient import TestClient

from pipeline import config as C
from pipeline import sources
from pipeline.enrich import normalize_phone
from pipeline.models import Lead
from pipeline.process import process

TODAY = date(2026, 10, 7)


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(C, "CSV_FALLBACK", str(tmp_path / "t.csv"))
    monkeypatch.setattr(C, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(C, "META_APP_SECRET", "")


def test_phone_normalization():
    assert normalize_phone("0300-1234567") == "+923001234567"
    assert normalize_phone("92 300 1234567") == "+923001234567"
    assert normalize_phone("3001234567") == "+923001234567"
    assert normalize_phone("+44 7700 900123") == "+447700900123"
    assert normalize_phone("12345") == ""


def test_text_parsers():
    assert sources.norm_budget_lakh("40 lakh") == 40
    assert sources.norm_budget_lakh("35-45 lac") == 35
    assert sources.norm_budget_lakh("1 crore") == 100
    assert sources.norm_budget_lakh("5 million") == 50
    assert sources.norm_intake("September 2027") == "Sep 2027"
    assert sources.norm_level("I want to do MS in data science") == "pg_taught"
    ex = sources.extract_from_text("AoA, MSc in UK for jan 2027, ielts 6.5, budget 70 lakh, from Lahore")
    assert ex["study_level"] == "pg_taught" and ex["english_score"] == 6.5
    assert ex["budget_pkr_lakh"] == 70 and ex["preferred_intake"] == "Jan 2027" and ex["city"] == "lahore"


def hot_quiz():
    return Lead(name="Ayesha Khan", phone="03001234567", email="ayesha@gmail.com", city="Lahore",
                study_level="pg_taught", subject="Data Science", highest_qualification="bachelors_16",
                cgpa=3.4, passing_year=2025, english_test="ielts", english_score=7.0,
                budget_pkr_lakh=165, funding_source="self_family", preferred_intake="Jan 2027",
                location_pref="outside", has_passport=True, consent=True)


def test_hot_lead():
    lead = process(hot_quiz(), today=TODAY, notify=False)
    assert lead.tier == "Hot", lead.score_breakdown
    assert lead.academic_route == "direct" and lead.english_status == "meets"
    assert lead.budget_status == "sufficient"
    assert lead.recommended_intake == "Jan 2027"
    # Jan 2027 visa applications fall after 30 Nov 2026 → new £1,203 rate
    assert lead.est_first_year_cost_gbp == 15000 + 1203 * 9 + 558 + round(776 * 1.5) + 1200


def test_budget_short_caps_tier():
    l = hot_quiz(); l.budget_pkr_lakh = 40
    lead = process(l, today=TODAY, notify=False)
    assert lead.budget_status == "short" and lead.score <= 55 and lead.tier != "Hot"


def test_dependants_flag_and_cap():
    l = hot_quiz(); l.bring_dependants = True
    lead = process(l, today=TODAY, notify=False)
    assert lead.score <= 60 and any("dependants" in f for f in lead.flags)


def test_refusal_never_hot():
    l = hot_quiz(); l.previous_uk_refusal = True
    lead = process(l, today=TODAY, notify=False)
    assert lead.tier == "Warm" and lead.assigned_to == "Senior counselor"


def test_tight_intake_pushed_back():
    l = hot_quiz(); l.has_passport = False; l.english_test = "planned"; l.english_score = None
    lead = process(l, today=TODAY, notify=False)
    assert lead.recommended_intake == "May 2027"
    assert any("too tight" in f for f in lead.flags)


def test_pg_without_degree_redirected():
    l = Lead(phone="03211234567", study_level="pg_taught", highest_qualification="fsc_hssc",
             grade_percent=80, consent=True)
    lead = process(l, today=TODAY, notify=False)
    assert lead.academic_route == "foundation"


def test_whatsapp_then_quiz_merges():
    wa = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "923001234567", "profile": {"name": "Ayesha"}}],
        "messages": [{"from": "923001234567", "id": "wamid.1",
                      "text": {"body": "Hi, want to do masters in UK, ielts 7"}}]}}]}]}
    first = process(sources.from_whatsapp(wa)[0], today=TODAY, notify=False)
    assert first.study_level == "pg_taught" and first.completeness < 60
    second = process(hot_quiz(), today=TODAY, notify=False)
    assert second.lead_id == first.lead_id
    assert second.source == "whatsapp" and second.tier == "Hot"
    assert "masters" in second.message


def test_meta_fields():
    fd = [{"name": "full_name", "values": ["Bilal Ahmed"]},
          {"name": "phone_number", "values": ["+923331234567"]},
          {"name": "city", "values": ["Faisalabad"]},
          {"name": "study_level", "values": ["Bachelor's (BS)"]},
          {"name": "highest_qualification", "values": ["FSc / ICS / HSSC"]},
          {"name": "percentage_or_cgpa", "values": ["78%"]},
          {"name": "ielts_status", "values": ["Not yet, planning"]},
          {"name": "budget", "values": ["50-60 lakh"]},
          {"name": "preferred_intake", "values": ["September 2027"]},
          {"name": "do_you_have_a_passport?", "values": ["Yes"]}]
    lead = process(sources.from_meta_fields(fd, "lg1"), today=TODAY, notify=False)
    assert lead.study_level == "ug" and lead.highest_qualification == "fsc_hssc"
    assert lead.grade_percent == 78 and lead.academic_route == "direct"
    assert lead.budget_pkr_lakh == 50 and lead.city_tier == 2


def test_api_quiz_requires_consent():
    from app import app
    c = TestClient(app)
    assert c.post("/webhook/quiz", json={"phone": "03001234567"}).status_code == 400
    r = c.post("/webhook/quiz", json={**hot_quiz().model_dump(include={
        "name", "phone", "email", "city", "study_level", "highest_qualification", "cgpa",
        "english_test", "english_score", "budget_pkr_lakh", "has_passport"}), "consent": True})
    assert r.status_code == 200 and "score" not in r.json()


def test_meta_verify():
    from app import app
    c = TestClient(app)
    r = c.get("/webhook/meta", params={"hub.mode": "subscribe",
                                       "hub.verify_token": C.META_VERIFY_TOKEN, "hub.challenge": "42"})
    assert r.text == "42"
