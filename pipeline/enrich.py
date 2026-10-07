"""Enrichment: turn what a student told us into what a counselor needs to know.

Everything here is derived from the student's own answers plus public UK
visa rules — no third-party data lookups on individuals.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import config as C
from .models import Lead

_CITY_ALIASES = {
    "lhr": "lahore", "khi": "karachi", "isb": "islamabad", "isl": "islamabad",
    "pindi": "rawalpindi", "rwp": "rawalpindi", "fsd": "faisalabad", "lyallpur": "faisalabad",
    "mul": "multan", "pesh": "peshawar", "grw": "gujranwala", "skt": "sialkot",
}

KEY_FIELDS = [
    "name", "phone", "city", "study_level", "highest_qualification", "passing_year",
    "english_test", "budget_pkr_lakh", "funding_source", "preferred_intake", "has_passport",
]


# --------------------------------------------------------------------------- contact
def normalize_phone(raw: str) -> str:
    """Return E.164. Pakistani mobiles → +923XXXXXXXXX; foreign numbers kept if prefixed with +/00."""
    if not raw:
        return ""
    s = raw.strip()
    plus = s.startswith("+") or s.startswith("00")
    d = re.sub(r"\D", "", s)
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("9203") and len(d) == 13:   # "+92 0300 …": country code plus trunk 0
        d = "92" + d[3:]
    if len(d) == 11 and d.startswith("03"):
        return "+92" + d[1:]
    if len(d) == 10 and d.startswith("3"):
        return "+92" + d
    if len(d) == 12 and d.startswith("923"):
        return "+" + d
    if plus and 8 <= len(d) <= 15:
        return "+" + d
    return ""


def is_pk_mobile(e164: str) -> bool:
    return bool(re.fullmatch(r"\+923\d{9}", e164 or ""))


def check_email(email: str) -> bool | None:
    if not email:
        return None
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
        return False
    return email.split("@")[1] not in C.DISPOSABLE_EMAIL_DOMAINS


def normalize_city(raw: str) -> str:
    c = re.sub(r"[^a-z ]", "", (raw or "").lower()).strip()
    return _CITY_ALIASES.get(c, c)


# --------------------------------------------------------------------------- intake
def _intake_by_label(label: str):
    for lab, start in C.INTAKES:
        if lab.lower() == (label or "").strip().lower():
            return lab, start
    return None


def pick_intake(lead: Lead, today: date) -> tuple[str, int | None, list[str]]:
    """Earliest realistic intake given readiness; honours preference if feasible."""
    flags: list[str] = []
    ready = lead.has_passport and lead.english_status in ("meets", "waiver_possible")
    lead_days = 90 if ready else 150    # offer → CAS → 28-day funds → visa
    earliest = today + timedelta(days=lead_days)
    feasible = [(lab, st) for lab, st in C.INTAKES if st >= earliest]
    if not feasible:
        return "", None, flags
    chosen = feasible[0]
    pref = _intake_by_label(lead.preferred_intake)
    if pref:
        if pref[1] >= earliest:
            chosen = pref
        else:
            flags.append(f"Preferred {pref[0]} is too tight — suggest {chosen[0]}")
    months = round((chosen[1] - today).days / 30.4)
    return chosen[0], months, flags


# --------------------------------------------------------------------------- academics
def academic_route(lead: Lead) -> tuple[str, list[str]]:
    lvl, q = lead.study_level, lead.highest_qualification
    pct, cg = lead.grade_percent, lead.cgpa
    flags: list[str] = []
    if not lvl or not q:
        return "unknown", flags

    if lvl == "foundation":
        return "direct", flags

    if lvl == "ug":
        if q in ("matric", "o_levels"):
            return "foundation", ["Needs FSc/A-levels or a foundation year first"]
        if q == "a_levels":
            return "direct", flags
        if q in ("fsc_hssc", "dae"):
            if pct is None:
                return "check", flags
            if pct >= 70:
                return "direct", flags
            if pct >= 55:
                return "check", ["HSSC 55–70%: direct entry at some partners, else foundation"]
            return "foundation", flags
        return "direct", ["Already holds a degree — confirm UG is really the goal"]

    if lvl in ("pg_taught", "mba"):
        if q in ("matric", "o_levels", "fsc_hssc", "a_levels", "dae"):
            return "foundation", ["Wants PG but has no bachelor's — redirect to UG"]
        score4 = cg if cg is not None else (pct / 25 if pct is not None else None)  # rough % → 4.0
        if q == "bachelors_14":
            flags.append("14-year degree: accepted by some UK unis (often 60%+), others need pre-master's")
            if pct is not None and pct < 50:
                return "pre_masters", flags
            return "check", flags
        if score4 is None:
            return "check", flags
        if score4 >= 3.0:
            return "direct", flags
        if score4 >= 2.5:
            return "check", ["CGPA 2.5–3.0: shortlist lower-tariff universities"]
        return "pre_masters", flags

    if lvl == "phd":
        if q in ("masters", "mphil"):
            return "direct", ["PhD: needs proposal + supervisor match"]
        return "check", ["PhD usually needs a master's/MPhil"]

    return "unknown", flags


def study_gap(lead: Lead, intake_year: int) -> int | None:
    if not lead.passing_year:
        return None
    return max(0, intake_year - lead.passing_year - 1)


# --------------------------------------------------------------------------- english
def english_status(lead: Lead) -> str:
    lvl = lead.study_level or "pg_taught"
    need = C.IELTS_NEEDED.get(lvl, 6.0)
    t = lead.english_test
    if t in ("ielts", "ielts_ukvi", "pte", "toefl", "duolingo", "oxford_elllt"):
        if lead.english_score is None:
            return "unknown"
        if lead.english_score >= need:
            return "meets"
        return "below"
    if t == "planned":
        return "needs_test"
    # Degree-level courses: UK universities may self-assess English (e.g. MOI letter /
    # HSSC English marks). Foundation (below degree) needs a UKVI-approved SELT.
    if lead.english_medium and lvl != "foundation":
        return "waiver_possible"
    if t == "none":
        return "needs_test"
    return "unknown"


# --------------------------------------------------------------------------- money
def first_year_cost_gbp(lead: Lead, intake_start: date | None) -> int | None:
    lvl = lead.study_level
    if not lvl:
        return None
    # Visa usually lodged 4–8 weeks before start; 45 days errs toward the newer (higher) rate.
    apply_on = (intake_start - timedelta(days=45)) if intake_start else date.today()
    loc = "london" if lead.location_pref == "london" else "outside"
    maint = C.maintenance_monthly(loc, apply_on) * 9
    ihs = round(C.IHS_PER_YEAR_GBP * (C.VISA_YEARS[lvl] + 0.5))
    return C.TUITION_FLOOR_GBP[lvl] + maint + C.VISA_FEE_GBP + ihs + C.FLIGHT_AND_SETUP_GBP


def budget_status(budget_lakh: float | None, cost_lakh: float | None) -> str:
    if budget_lakh is None or cost_lakh is None:
        return "unknown"
    if budget_lakh >= cost_lakh:
        return "sufficient"
    if budget_lakh >= 0.85 * cost_lakh:
        return "tight"
    return "short"


# --------------------------------------------------------------------------- main
def enrich(lead: Lead, today: date | None = None) -> Lead:
    today = today or date.today()
    flags: list[str] = []

    # Contact
    lead.phone_e164 = normalize_phone(lead.phone)
    if lead.phone_e164:
        lead.whatsapp_link = "https://wa.me/" + lead.phone_e164.lstrip("+")
    elif lead.phone:
        flags.append("Phone number invalid")
    if lead.phone_e164 and not is_pk_mobile(lead.phone_e164):
        flags.append("Non-Pakistani number (overseas family?)")
    lead.email_ok = check_email(lead.email)
    if lead.email_ok is False:
        flags.append("Email invalid or disposable")

    # Location
    lead.city_normalized = normalize_city(lead.city)
    c = lead.city_normalized
    lead.city_tier = 1 if c in C.CITY_TIER[1] else 2 if c in C.CITY_TIER[2] else (3 if c else None)
    lead.province = C.CITY_PROVINCE.get(c, "")

    # English first (intake readiness depends on it)
    lead.english_status = english_status(lead)
    if lead.english_status == "below":
        flags.append(f"English score below typical {C.IELTS_NEEDED.get(lead.study_level or 'pg_taught')}")

    # Intake
    lead.recommended_intake, lead.months_to_intake, f = pick_intake(lead, today)
    flags += f
    intake = _intake_by_label(lead.recommended_intake)
    intake_start = intake[1] if intake else None

    # Academics
    lead.academic_route, f = academic_route(lead)
    flags += f
    lead.study_gap_years = study_gap(lead, intake_start.year if intake_start else today.year + 1)
    if lead.study_gap_years and lead.study_gap_years >= 5:
        flags.append(f"{lead.study_gap_years}-year study gap — needs a documented explanation")

    # Money
    cost = first_year_cost_gbp(lead, intake_start)
    lead.est_first_year_cost_gbp = cost
    if cost:
        lead.est_first_year_cost_pkr_lakh = round(cost * C.GBP_TO_PKR / 100_000, 1)
    lead.budget_status = budget_status(lead.budget_pkr_lakh, lead.est_first_year_cost_pkr_lakh)
    if lead.budget_pkr_lakh is not None and lead.est_first_year_cost_pkr_lakh:
        lead.budget_gap_pkr_lakh = round(lead.budget_pkr_lakh - lead.est_first_year_cost_pkr_lakh, 1)
    if lead.funding_source == "scholarship_only":
        flags.append("Depends on full scholarship — rare for UK taught courses")
    if lead.study_level == "ug":
        flags.append("UG is 3 years — confirm funding beyond year 1")

    # Visa risk signals
    if lead.bring_dependants and lead.study_level != "phd":
        flags.append("Wants dependants — not allowed on taught courses (PhD/research only)")
    if lead.previous_uk_refusal:
        flags.append("Previous UK refusal — senior counselor review")
    if lead.has_passport is False:
        flags.append("No passport yet — start application now (2–6 weeks)")

    # Product context
    if lead.study_level:
        lead.graduate_route_months = C.GRADUATE_ROUTE_MONTHS.get(lead.study_level)

    # Completeness
    answered = sum(1 for k in KEY_FIELDS if getattr(lead, k) not in (None, "", []))
    lead.completeness = round(100 * answered / len(KEY_FIELDS))
    if not lead.consent:
        flags.append("No marketing consent — do not contact until confirmed")

    lead.flags = list(dict.fromkeys(flags))
    return lead
