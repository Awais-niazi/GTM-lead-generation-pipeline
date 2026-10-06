"""Parsers that turn each inbound channel into a Lead.

Channels:
  * web_quiz      — JSON posted by the UK eligibility quiz landing page
  * whatsapp      — WhatsApp Cloud API webhook (student messages you first)
  * meta_lead_ad  — Facebook/Instagram Instant Form, fetched by leadgen_id
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from . import config as C
from .models import Lead, now_iso

# --------------------------------------------------------------------------- free-text helpers
_LEVEL_WORDS = [
    (r"\b(phd|doctorate)\b", "phd"),
    (r"\bmba\b", "mba"),
    (r"\b(masters?|master's|ms|msc|ma|mres|llm|pg|postgrad\w*)\b", "pg_taught"),
    (r"\b(bachelors?|bachelor's|bs|bsc|ba|undergrad\w*|ug|llb)\b", "ug"),
    (r"\bfoundation\b", "foundation"),
]
_MONTHS = {"jan": "Jan", "feb": "Jan", "may": "May", "apr": "May", "jun": "May",
           "sep": "Sep", "sept": "Sep", "oct": "Sep", "aug": "Sep"}


def norm_level(text: str) -> str | None:
    t = (text or "").lower()
    for pat, lvl in _LEVEL_WORDS:
        if re.search(pat, t):
            return lvl
    return None


def norm_qualification(text: str) -> str | None:
    t = (text or "").lower()
    if "mphil" in t:
        return "mphil"
    if re.search(r"master|\bms\b|\bmsc\b|\bma\b|\bmba\b", t):
        return "masters"
    if "16" in t or "4 year" in t or "4-year" in t or re.search(r"\bbs\b|\bbsc\s*\(?hons", t):
        return "bachelors_16"
    if "14" in t or "2 year" in t or "2-year" in t or re.search(r"\bba\b|\bbsc\b|\bbcom\b", t):
        return "bachelors_14"
    if "a level" in t or "a-level" in t:
        return "a_levels"
    if "o level" in t or "o-level" in t:
        return "o_levels"
    if re.search(r"fsc|f\.sc|hssc|intermediate|\bics\b|\bicom\b|\bfa\b|12", t):
        return "fsc_hssc"
    if "dae" in t or "diploma" in t:
        return "dae"
    if "matric" in t or "ssc" in t:
        return "matric"
    return None


def norm_intake(text: str) -> str:
    t = (text or "").lower()
    if "not sure" in t or "undecided" in t:
        return "not sure"
    m = re.search(r"(jan|feb|apr|may|jun|aug|sept?|oct)\w*[\s,'-]*(20)?(2[6-9])", t)
    if m:
        return f"{_MONTHS[m.group(1)[:3]]} 20{m.group(3)}"
    return ""


def norm_budget_lakh(text: str) -> float | None:
    """'40 lakh', '4 million', '35-45 lac', '1 crore', '₨5,000,000' → PKR lakh."""
    t = (text or "").lower().replace(",", "")
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]
    if not nums:
        return None
    n = min(nums) if len(nums) > 1 else nums[0]     # be conservative on ranges
    if "crore" in t or "cr" in t.split():
        return n * 100
    if "million" in t or re.search(r"\d\s*m\b", t):
        return n * 10
    if "lakh" in t or "lac" in t or "lak" in t:
        return n
    if n >= 100_000:
        return n / 100_000
    return n if n < 1000 else None


def norm_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    if s in ("yes", "y", "true", "1", "haan", "ji"):
        return True
    if s in ("no", "n", "false", "0", "nahi"):
        return False
    return None


def extract_from_text(text: str) -> dict[str, Any]:
    """Pull whatever qualification signals a WhatsApp message happens to contain."""
    t = (text or "").lower()
    out: dict[str, Any] = {}
    if lvl := norm_level(t):
        out["study_level"] = lvl
    m = re.search(r"(ielts|pte|toefl|duolingo)\D{0,12}(\d(?:\.\d)?)", t) or \
        re.search(r"(\d(?:\.\d)?)\s*bands?", t)
    if m:
        if m.lastindex == 2:
            out["english_test"] = m.group(1) if m.group(1) != "ielts" else "ielts"
            out["english_score"] = float(m.group(2))
        else:
            out["english_test"], out["english_score"] = "ielts", float(m.group(1))
    elif "ielts" in t:
        out["english_test"] = "planned"
    if b := re.search(r"(\d+(?:\.\d+)?\s*(?:-|to)?\s*\d*\s*(?:lakh|lac|crore|million))", t):
        out["budget_pkr_lakh"] = norm_budget_lakh(b.group(1))
    if i := norm_intake(t):
        out["preferred_intake"] = i
    m = re.search(r"cgpa\D{0,6}([0-4]\.\d{1,2})", t)
    if m:
        out["cgpa"] = float(m.group(1))
    for city in list(C.CITY_PROVINCE):
        if re.search(rf"\b{city}\b", t):
            out["city"] = city
            break
    return out


# --------------------------------------------------------------------------- web quiz
def from_web_quiz(payload: dict[str, Any]) -> Lead:
    data = {k: v for k, v in payload.items() if k in Lead.model_fields and v not in ("", None)}
    lead = Lead(**data, source="web_quiz")
    if lead.consent and not lead.consent_at:
        lead.consent_at = now_iso()
    return lead


# --------------------------------------------------------------------------- whatsapp
def from_whatsapp(webhook: dict[str, Any]) -> list[Lead]:
    leads = []
    for entry in webhook.get("entry", []):
        for change in entry.get("changes", []):
            v = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in v.get("contacts", [])}
            for msg in v.get("messages", []):
                text = (msg.get("text") or {}).get("body", "") or \
                       (msg.get("button") or {}).get("text", "")
                wa = msg.get("from", "")
                lead = Lead(
                    source="whatsapp", source_ref=msg.get("id", ""),
                    name=names.get(wa, ""), phone="+" + wa if wa else "", message=text,
                    # Student started the chat → you may reply. Marketing after 24h
                    # needs their opt-in to template messages (ask in the quiz).
                    consent=True, consent_text="Student initiated WhatsApp conversation",
                    consent_at=now_iso(),
                    **extract_from_text(text),
                )
                leads.append(lead)
    return leads


# --------------------------------------------------------------------------- meta lead ads
# Map your Instant Form question keys → Lead fields. Edit to match your form.
META_FIELD_MAP = {
    "full_name": "name", "phone_number": "phone", "email": "email", "city": "city",
    "study_level": "study_level", "what_do_you_want_to_study?": "study_level",
    "highest_qualification": "highest_qualification",
    "percentage_or_cgpa": "_grade", "passing_year": "passing_year",
    "ielts_status": "_english", "budget": "budget_pkr_lakh",
    "preferred_intake": "preferred_intake", "do_you_have_a_passport?": "has_passport",
    "subject": "subject",
}


def _apply_grade(lead_kwargs: dict, raw: str) -> None:
    nums = re.findall(r"\d+(?:\.\d+)?", raw or "")
    if not nums:
        return
    n = float(nums[0])
    if n <= 4.0:
        lead_kwargs["cgpa"] = n
    elif n <= 100:
        lead_kwargs["grade_percent"] = n


def from_meta_fields(field_data: list[dict[str, Any]], leadgen_id: str = "",
                     ad_name: str = "", campaign: str = "") -> Lead:
    kw: dict[str, Any] = {}
    for f in field_data:
        key = f.get("name", "").strip().lower()
        val = (f.get("values") or [""])[0]
        target = META_FIELD_MAP.get(key)
        if not target or val in ("", None):
            continue
        if target == "study_level":
            kw["study_level"] = norm_level(val)
        elif target == "highest_qualification":
            kw["highest_qualification"] = norm_qualification(val)
        elif target == "_grade":
            _apply_grade(kw, val)
        elif target == "_english":
            ex = extract_from_text(val)
            kw["english_test"] = ex.get("english_test", "none" if "no" in val.lower() else "planned")
            if "english_score" in ex:
                kw["english_score"] = ex["english_score"]
        elif target == "budget_pkr_lakh":
            kw["budget_pkr_lakh"] = norm_budget_lakh(val)
        elif target == "preferred_intake":
            kw["preferred_intake"] = norm_intake(val) or val
        elif target == "has_passport":
            kw["has_passport"] = norm_bool(val)
        elif target == "passing_year":
            m = re.search(r"(19|20)\d{2}", val)
            kw["passing_year"] = int(m.group()) if m else None
        else:
            kw[target] = val
    return Lead(
        **{k: v for k, v in kw.items() if v is not None},
        source="meta_lead_ad", source_ref=leadgen_id,
        utm_source="meta", utm_campaign=campaign or ad_name,
        consent=True, consent_text="Submitted Meta Instant Form (custom disclaimer)",
        consent_at=now_iso(),
    )


def fetch_meta_lead(leadgen_id: str) -> Lead:
    """Webhooks only send a leadgen_id; the answers are fetched from the Graph API."""
    url = f"https://graph.facebook.com/{C.META_GRAPH_VERSION}/{leadgen_id}"
    r = httpx.get(url, params={"access_token": C.META_PAGE_ACCESS_TOKEN,
                               "fields": "field_data,ad_name,campaign_name,created_time"}, timeout=15)
    r.raise_for_status()
    d = r.json()
    return from_meta_fields(d.get("field_data", []), leadgen_id,
                            d.get("ad_name", ""), d.get("campaign_name", ""))


def meta_leadgen_ids(webhook: dict[str, Any]) -> list[str]:
    return [c["value"]["leadgen_id"]
            for e in webhook.get("entry", []) for c in e.get("changes", [])
            if c.get("field") == "leadgen" and "leadgen_id" in c.get("value", {})]
