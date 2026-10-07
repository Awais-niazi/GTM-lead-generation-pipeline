"""Lead scoring: 0–100, explainable, tuned for 'will this student actually go to the UK?'"""
from __future__ import annotations

from . import config as C
from .models import Lead


def _budget(lead: Lead) -> int:
    w = C.WEIGHTS["budget"]
    s = lead.budget_status
    if s == "sufficient":
        pts = w
    elif s == "tight":
        pts = round(w * 0.65)
    elif s == "short":
        ratio = (lead.budget_pkr_lakh or 0) / (lead.est_fees_pkr_lakh or 1)
        pts = round(w * 0.3) if ratio >= 0.6 else 0
    else:
        pts = round(w * 0.25)
    if lead.funds_proof == "no":                 # can't show 28-day living funds → no visa
        pts = min(pts, 5)
    elif lead.funds_proof == "not_sure":
        pts = max(0, pts - 5)
    if lead.funding_source == "scholarship_only":
        pts = min(pts, 5)
    elif lead.funding_source == "loan":
        pts = max(0, pts - 3)
    return pts


def _academics(lead: Lead) -> int:
    w = C.WEIGHTS["academics"]
    pts = {"direct": w, "check": round(w * .7), "pre_masters": w // 2,
           "foundation": w // 2}.get(lead.academic_route, round(w * .3))
    gap = lead.study_gap_years or 0
    if gap >= 8:
        pts -= 6
    elif gap >= 5:
        pts -= 3
    return max(0, pts)


def _english(lead: Lead) -> int:
    w = C.WEIGHTS["english"]
    return {"meets": w, "waiver_possible": round(w * .75), "needs_test": round(w * .45),
            "below": round(w * .4)}.get(lead.english_status, round(w * .25))


def _timeline(lead: Lead) -> int:
    w = C.WEIGHTS["timeline"]
    m = lead.months_to_intake
    if m is None:
        return 2
    if m <= 5:
        return w
    if m <= 9:
        return round(w * .75)
    if m <= 13:
        return round(w * .45)
    return round(w * .25)


def _intent(lead: Lead) -> int:
    pts = 0
    if lead.has_passport:
        pts += 4
    others = [c for c in lead.other_countries if c.lower() not in ("uk", "united kingdom")]
    if not others:
        pts += 3                       # UK is the only destination considered
    if lead.preferred_intake and lead.preferred_intake.lower() != "not sure":
        pts += 2
    if lead.subject:
        pts += 1
    return min(C.WEIGHTS["intent"], pts)


def _contact(lead: Lead) -> int:
    pts = 0
    if lead.phone_e164:
        pts += 6
    if lead.email_ok:
        pts += 2
    if lead.completeness >= 80:
        pts += 2
    return min(C.WEIGHTS["contactability"], pts)


def _next_action(lead: Lead) -> str:
    if lead.tier == "Disqualified":
        return "No valid contact details — discard"
    if not lead.consent:
        return "Hold: get consent before any outreach"
    if lead.tier == "Hot":
        return "Call within 1 hour · book counselling · request transcripts + passport copy"
    if lead.tier == "Warm":
        gaps = []
        if lead.english_status in ("needs_test", "below"):
            gaps.append("IELTS plan")
        if lead.budget_status in ("tight", "short"):
            gaps.append("budget options outside London")
        if lead.funds_proof == "not_sure":
            gaps.append("28-day living funds")
        extra = f" · discuss {', '.join(gaps)}" if gaps else ""
        return "WhatsApp today, call within 24h" + extra
    if lead.tier == "Nurture":
        if lead.funds_proof == "no":
            return "Nurture: explain the 28-day living-funds rule; revisit when the family can show it"
        if lead.budget_status == "short":
            return "Nurture: low-cost universities, instalment plans, foundation routes"
        if lead.english_status in ("needs_test", "below"):
            return "Nurture: IELTS prep offer, re-score after test"
        return "Nurture: fortnightly WhatsApp updates + webinar invite"
    return "Monthly newsletter only"


def score(lead: Lead) -> Lead:
    if not lead.phone_e164 and not lead.email_ok:
        lead.score, lead.tier, lead.score_breakdown = 0, "Disqualified", {}
        lead.next_action = _next_action(lead)
        return lead

    bd = {
        "budget": _budget(lead),
        "academics": _academics(lead),
        "english": _english(lead),
        "timeline": _timeline(lead),
        "intent": _intent(lead),
        "contactability": _contact(lead),
    }
    total = sum(bd.values())
    # Hard caps: some issues block a 'Hot' label regardless of the rest.
    if lead.bring_dependants and lead.study_level != "phd":
        total = min(total, 60)
    if lead.budget_status == "short" or lead.funds_proof == "no":
        total = min(total, 55)
    if lead.previous_uk_refusal:          # needs senior review before anyone promises anything
        total = min(total, 65)

    lead.score_breakdown = bd
    lead.score = total
    t = C.TIER_THRESHOLDS
    lead.tier = "Hot" if total >= t["Hot"] else "Warm" if total >= t["Warm"] \
        else "Nurture" if total >= t["Nurture"] else "Cold"
    lead.next_action = _next_action(lead)
    if lead.previous_uk_refusal:
        lead.assigned_to = "Senior counselor"
    else:
        lead.assigned_to = f"{lead.province} team" if lead.province else "Online team"
    return lead
