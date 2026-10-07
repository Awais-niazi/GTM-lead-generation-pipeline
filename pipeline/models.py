"""Lead data model: what the student told us (raw) + what we derived (enriched)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

StudyLevel = Literal["foundation", "ug", "pg_taught", "mba", "phd"]
Qualification = Literal[
    "matric", "o_levels", "fsc_hssc", "a_levels", "dae",
    "bachelors_14", "bachelors_16", "masters", "mphil",
]
EnglishTest = Literal["none", "planned", "ielts", "ielts_ukvi", "pte", "toefl", "duolingo", "oxford_elllt"]
Funding = Literal["self_family", "loan", "sponsor", "scholarship_only", "unsure"]
Location = Literal["london", "outside", "any"]
FundsProof = Literal["yes", "not_sure", "no"]
Source = Literal["web_quiz", "whatsapp", "meta_lead_ad", "import", "booking"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Lead(BaseModel):
    # ---- identity & provenance ------------------------------------------
    lead_id: str = ""
    source: Source = "web_quiz"
    source_ref: str = ""                 # meta leadgen_id, whatsapp msg id, etc.
    utm_source: str = ""
    utm_campaign: str = ""
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

    # ---- consent (required to contact) -----------------------------------
    consent: bool = False
    consent_text: str = ""
    consent_at: str = ""

    # ---- what the student told us ----------------------------------------
    name: str = ""
    phone: str = ""
    email: str = ""
    city: str = ""
    study_level: Optional[StudyLevel] = None
    subject: str = ""
    highest_qualification: Optional[Qualification] = None
    grade_percent: Optional[float] = None    # Matric/FSc/14-yr degree %
    cgpa: Optional[float] = None             # out of 4.0
    passing_year: Optional[int] = None
    english_test: Optional[EnglishTest] = None
    english_score: Optional[float] = None    # IELTS-equivalent overall
    english_medium: Optional[bool] = None    # studied in English medium (waiver signal)
    budget_pkr_lakh: Optional[float] = None  # budget for fees: tuition + visa + IHS + flights, PKR lakh
    funds_proof: Optional[FundsProof] = None # can the family show the living funds for 28 days?
    funding_source: Optional[Funding] = None
    preferred_intake: str = ""               # "Jan 2027", "Sep 2027", "not sure"
    location_pref: Location = "any"
    has_passport: Optional[bool] = None
    previous_uk_refusal: Optional[bool] = None
    bring_dependants: Optional[bool] = None
    other_countries: list[str] = Field(default_factory=list)
    message: str = ""                        # free text (WhatsApp / notes)

    # ---- enrichment (derived; never asked) -------------------------------
    phone_e164: str = ""
    whatsapp_link: str = ""
    email_ok: Optional[bool] = None
    city_normalized: str = ""
    city_tier: Optional[int] = None
    province: str = ""
    study_gap_years: Optional[int] = None
    est_fees_gbp: Optional[int] = None               # paid to get there: tuition + visa + IHS + flights
    est_fees_pkr_lakh: Optional[float] = None
    est_living_funds_pkr_lakh: Optional[float] = None  # shown in the bank for 28 days (UKVI maintenance)
    budget_gap_pkr_lakh: Optional[float] = None
    budget_status: str = ""                  # sufficient / tight / short / unknown
    academic_route: str = ""                 # direct / check / foundation / pre_masters / redirect_ug / unknown
    english_status: str = ""                 # meets / below / waiver_possible / needs_test / unknown
    recommended_intake: str = ""
    months_to_intake: Optional[int] = None
    graduate_route_months: Optional[int] = None
    flags: list[str] = Field(default_factory=list)
    completeness: int = 0                    # % of key fields answered

    # ---- scoring & routing ----------------------------------------------
    score: int = 0
    score_breakdown: dict[str, int] = Field(default_factory=dict)
    tier: str = ""                           # Hot / Warm / Nurture / Cold / Disqualified
    next_action: str = ""
    assigned_to: str = ""

    # ---- workflow: quiz → call → qualify → ship → agency → visa ------------
    stage: str = "New"                       # see config.STAGES
    stage_history: dict[str, str] = Field(default_factory=dict)   # stage → date first reached
    call_eligible: bool = False              # quiz result page offers a booking link
    call_at: str = ""                        # booked call start (from Cal.com)
    booking_ref: str = ""
    # you fill these in the working tab during/after the call
    decision_maker: str = ""                 # Student / Parent / ...
    handoff_consent: Optional[bool] = None   # agreed to be introduced to the partner consultancy
    reason: str = ""                         # why Rejected / Not yet / Lost
    revisit_on: str = ""                     # for Not yet
    notes: str = ""
    # shipping + agency feedback
    shipped_at: str = ""
    handoff_link: str = ""                   # wa.me link with the intro message, ready for you to send
    agency_stage: str = ""                   # last value seen in the Delivery tab
    agency_contacted_on: str = ""
    agency_notes: str = ""

    def compute_id(self) -> str:
        """Stable id from phone (preferred) or email so re-submissions merge."""
        key = self.phone_e164 or self.email.strip().lower() or self.source_ref
        return hashlib.sha1(key.encode()).hexdigest()[:12] if key else ""


# Working tab (yours), in order. Columns you edit are in WORKING_EDITABLE;
# the pipeline reads them back on sync and before it touches a lead.
SHEET_COLUMNS = [
    "lead_id", "stage", "tier", "score", "next_action", "call_at",
    "name", "phone_e164", "whatsapp_link",
    "decision_maker", "handoff_consent", "reason", "revisit_on", "notes", "handoff_link",
    "email", "city_normalized", "province",
    "study_level", "subject", "highest_qualification", "grade_percent", "cgpa", "passing_year",
    "study_gap_years", "academic_route",
    "english_test", "english_score", "english_status",
    "budget_pkr_lakh", "est_fees_pkr_lakh", "budget_gap_pkr_lakh", "budget_status",
    "est_living_funds_pkr_lakh", "funds_proof",
    "funding_source", "preferred_intake", "recommended_intake", "months_to_intake",
    "location_pref", "has_passport", "previous_uk_refusal", "bring_dependants",
    "graduate_route_months", "flags", "score_breakdown", "completeness", "call_eligible",
    "source", "utm_source", "utm_campaign", "consent", "consent_at",
    "shipped_at", "agency_stage", "stage_history",
    "created_at", "updated_at", "message",
]
WORKING_EDITABLE = ["stage", "decision_maker", "handoff_consent", "reason", "revisit_on", "notes"]

# Delivery tab (shared with the agency). Written once per lead when shipped;
# the agency owns AGENCY_EDITABLE and the pipeline only reads those back.
DELIVERY_COLUMNS = [
    "lead_id", "shipped_at", "agency_stage", "agency_contacted_on", "agency_notes",
    "name", "phone_e164", "whatsapp_link", "email", "city_normalized",
    "study_level", "subject", "recommended_intake", "brief",
]
AGENCY_EDITABLE = ["agency_stage", "agency_contacted_on", "agency_notes"]
