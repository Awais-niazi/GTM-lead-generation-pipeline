"""Central configuration.

Everything the agency might tune lives here: UK visa figures, cost estimates,
scoring weights and integration settings. Secrets come from environment
variables (see .env.example) — never hard-code them.

UK figures verified October 2026 against GOV.UK-derived sources. Re-check
https://www.gov.uk/student-visa/money each intake cycle.
"""
from __future__ import annotations

import os
from datetime import date

# ---------------------------------------------------------------------------
# Agency / integration settings (from environment)
# ---------------------------------------------------------------------------
BRAND_NAME = os.getenv("BRAND_NAME", "Your Brand")                  # what students see
PARTNER_NAME = os.getenv("PARTNER_NAME", "our partner consultancy")  # the agency leads are shipped to
QUIZ_URL = os.getenv("QUIZ_URL", "https://example.com/uk-eligibility")

# Cal.com event link for the qualification call, and its webhook signing secret
BOOKING_URL = os.getenv("BOOKING_URL", "")
CALCOM_WEBHOOK_SECRET = os.getenv("CALCOM_WEBHOOK_SECRET", "")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Karachi")     # how call times are shown to you

# Google Sheets
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SHEET_TAB = os.getenv("SHEET_TAB", "Leads")                # your working tab
DELIVERY_TAB = os.getenv("DELIVERY_TAB", "Delivery")       # shipped leads, shared with the agency

# Meta (Facebook / Instagram lead ads + WhatsApp Cloud API share one app)
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "change-me")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")           # for X-Hub-Signature-256 checks
META_PAGE_ACCESS_TOKEN = os.getenv("META_PAGE_ACCESS_TOKEN", "")
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v21.0")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")

# Who gets Hot-lead and call-booked alerts (you), and who is told about shipped leads (the agency).
# Comma-separated WhatsApp numbers in +92 format.
def _numbers(var: str) -> list[str]:
    return [n.strip() for n in os.getenv(var, "").split(",") if n.strip()]


ALERT_WHATSAPP = _numbers("ALERT_WHATSAPP")
AGENCY_WHATSAPP = _numbers("AGENCY_WHATSAPP")

# Shared secret the landing page sends so randoms can't spam the quiz webhook
QUIZ_SHARED_KEY = os.getenv("QUIZ_SHARED_KEY", "")

DB_PATH = os.getenv("DB_PATH", "leads.db")
CSV_FALLBACK = os.getenv("CSV_FALLBACK", "leads_export.csv")
DELIVERY_CSV = os.getenv("DELIVERY_CSV", "delivery_export.csv")

# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
# Update weekly-ish. Used only to translate a student's PKR budget into GBP.
GBP_TO_PKR = float(os.getenv("GBP_TO_PKR", "375"))

# UK Student visa (from 8 April 2026)
VISA_FEE_GBP = 558
IHS_PER_YEAR_GBP = 776

# Maintenance funds per month, counted for up to 9 months.
# Rises for applications made on/after 30 Nov 2026 (Statement of Changes HC 584).
MAINTENANCE_CHANGE_DATE = date(2026, 11, 30)
MAINTENANCE_OLD = {"london": 1529, "outside": 1171}
MAINTENANCE_NEW = {"london": 1570, "outside": 1203}


def maintenance_monthly(location: str, apply_on: date | None = None) -> int:
    apply_on = apply_on or date.today()
    table = MAINTENANCE_NEW if apply_on >= MAINTENANCE_CHANGE_DATE else MAINTENANCE_OLD
    return table["london" if location == "london" else "outside"]


# Realistic *lower-end* first-year international tuition (GBP) — the floor a
# budget must clear, not an average. Adjust to your partner universities.
TUITION_FLOOR_GBP = {
    "foundation": 9_000,
    "ug": 14_000,
    "pg_taught": 15_000,
    "mba": 18_000,
    "phd": 17_000,
}
# Visa length in years (course + post-study buffer) used for IHS estimate.
VISA_YEARS = {"foundation": 1, "ug": 3, "pg_taught": 1, "mba": 1, "phd": 3}
FLIGHT_AND_SETUP_GBP = 1_200

# ---------------------------------------------------------------------------
# Intakes & policy
# ---------------------------------------------------------------------------
# (label, course start)
INTAKES = [
    ("Jan 2027", date(2027, 1, 15)),
    ("May 2027", date(2027, 5, 10)),
    ("Sep 2027", date(2027, 9, 20)),
    ("Jan 2028", date(2028, 1, 15)),
    ("Sep 2028", date(2028, 9, 20)),
]
# Graduate Route: 2 years for applications before 1 Jan 2027; 18 months after
# (bachelor's/master's). PhD stays 3 years.
GRADUATE_ROUTE_MONTHS = {"ug": 18, "pg_taught": 18, "mba": 18, "phd": 36, "foundation": 0}

# English: typical IELTS (Academic / UKVI) overall needed
IELTS_NEEDED = {"foundation": 4.5, "ug": 6.0, "pg_taught": 6.5, "mba": 6.5, "phd": 6.5}

# ---------------------------------------------------------------------------
# Scoring weights (sum to 100)
# ---------------------------------------------------------------------------
WEIGHTS = {
    "budget": 30,       # can they actually fund it?
    "academics": 20,    # is there a UK route for their qualification?
    "english": 15,      # test done / waiver likely?
    "timeline": 15,     # how soon is the intake?
    "intent": 10,       # passport, docs, parents involved, UK-only focus
    "contactability": 10,
}
TIER_THRESHOLDS = {"Hot": 70, "Warm": 50, "Nurture": 30}  # below Nurture → Cold

# Who gets offered a qualification call on the quiz result page. Your time is the cost.
CALL_FILTER = {
    "budget_status": {"sufficient", "tight"},
    "academic_route": {"direct", "check", "foundation", "pre_masters"},
    "max_months_to_intake": 12,
}

# ---------------------------------------------------------------------------
# Lead stages (dropdown in the working tab). The agency moves leads through
# AGENCY_STAGES in the Delivery tab; you mark Paid when the PKR 100k arrives.
# ---------------------------------------------------------------------------
STAGES = [
    "New", "Call booked", "No-show", "Not yet", "Rejected", "Cooked", "Shipped",
    "Contacted", "Counselling", "Applied", "Offer", "CAS", "Visa granted", "Enrolled",
    "Paid", "Lost", "Opted out",
]
AGENCY_STAGES = ["Contacted", "Counselling", "Applied", "Offer", "CAS", "Visa granted", "Enrolled", "Lost"]
DECISION_MAKERS = ["Student", "Parent", "Sibling", "Spouse", "Other"]
SHIP_CONTACT_SLA_DAYS = 2      # agency should have contacted a shipped lead by then
STALE_DAYS = 30                # shipped lead with no agency update for this long → chase

# Pakistani city tiers — used for routing (branch office) and ad targeting
CITY_TIER = {
    1: {"lahore", "karachi", "islamabad", "rawalpindi"},
    2: {"faisalabad", "multan", "peshawar", "gujranwala", "sialkot", "quetta",
        "hyderabad", "sargodha", "bahawalpur", "abbottabad", "gujrat", "sukkur"},
}
CITY_PROVINCE = {
    "lahore": "Punjab", "faisalabad": "Punjab", "multan": "Punjab", "rawalpindi": "Punjab",
    "gujranwala": "Punjab", "sialkot": "Punjab", "sargodha": "Punjab", "bahawalpur": "Punjab",
    "gujrat": "Punjab", "karachi": "Sindh", "hyderabad": "Sindh", "sukkur": "Sindh",
    "islamabad": "ICT", "peshawar": "KP", "abbottabad": "KP", "quetta": "Balochistan",
}

DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "tempmail.com", "10minutemail.com", "guerrillamail.com",
    "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com",
}
