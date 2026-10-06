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
AGENCY_NAME = os.getenv("AGENCY_NAME", "Your Consultancy")
QUIZ_URL = os.getenv("QUIZ_URL", "https://example.com/uk-eligibility")

# Google Sheets
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SHEET_TAB = os.getenv("SHEET_TAB", "Leads")

# Meta (Facebook / Instagram lead ads + WhatsApp Cloud API share one app)
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "change-me")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")           # for X-Hub-Signature-256 checks
META_PAGE_ACCESS_TOKEN = os.getenv("META_PAGE_ACCESS_TOKEN", "")
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v21.0")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")

# Counselor alerts for Hot leads (comma-separated WhatsApp numbers in +92 format)
COUNSELOR_WHATSAPP = [n.strip() for n in os.getenv("COUNSELOR_WHATSAPP", "").split(",") if n.strip()]

# Shared secret the landing page sends so randoms can't spam the quiz webhook
QUIZ_SHARED_KEY = os.getenv("QUIZ_SHARED_KEY", "")

DB_PATH = os.getenv("DB_PATH", "leads.db")
CSV_FALLBACK = os.getenv("CSV_FALLBACK", "leads_export.csv")

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
