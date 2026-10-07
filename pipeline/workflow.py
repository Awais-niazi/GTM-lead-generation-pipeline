"""Lead workflow: quiz → call → qualify → ship → agency → visa → paid.

You run the middle by hand in the working tab (stage dropdown + call notes).
This module reads those edits back, ships Cooked leads to the agency (write-once
delivery log + Delivery tab row + ready-to-send intro message), pulls the
agency's updates from the Delivery tab, and lists what needs your attention.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from . import config as C
from . import sinks, store
from .enrich import normalize_phone
from .models import Lead, now_iso
from .sources import norm_bool

log = logging.getLogger("uk-leads")

STOP_WORDS = {"stop", "unsubscribe", "stop all"}
_OPEN_STAGES = {"New", "No-show", "Not yet"}            # can still be offered a call
_AGENCY_ACTIVE = {"Shipped", "Contacted", "Counselling", "Applied", "Offer", "CAS"}

LEVEL_NAME = {"foundation": "Foundation", "ug": "Bachelor's", "pg_taught": "Master's", "mba": "MBA", "phd": "PhD"}
QUAL_NAME = {"matric": "Matric", "o_levels": "O Levels", "fsc_hssc": "FSc/HSSC", "a_levels": "A Levels",
             "dae": "DAE", "bachelors_14": "14-yr BA/BSc", "bachelors_16": "16-yr BS",
             "masters": "Master's", "mphil": "MPhil/MS"}
TEST_NAME = {"ielts": "IELTS", "ielts_ukvi": "IELTS UKVI", "pte": "PTE", "toefl": "TOEFL",
             "duolingo": "Duolingo", "oxford_elllt": "Oxford ELLT", "planned": "test planned", "none": "no test"}
FUNDING_NAME = {"self_family": "own/family", "loan": "bank loan", "sponsor": "sponsor",
                "scholarship_only": "scholarship only", "unsure": "not sure"}
FUNDS_NAME = {"yes": "family can show it", "not_sure": "not sure yet", "no": "can't show it"}
ROUTE_NAME = {"direct": "direct entry", "check": "depends on university", "foundation": "foundation first",
              "pre_masters": "pre-master's first", "unknown": "unknown"}


# --------------------------------------------------------------------------- helpers
def set_stage(lead: Lead, stage: str, today: date | None = None) -> None:
    if stage not in C.STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    lead.stage = stage
    lead.stage_history.setdefault(stage, (today or date.today()).isoformat())


def parse_date(s: str) -> date | None:
    """ISO dates/datetimes from the pipeline, or what someone typed into the sheet (dd/mm/yyyy)."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _tz():
    try:
        return ZoneInfo(C.TIMEZONE)
    except Exception:                                # no tz database: Pakistan has no DST
        return timezone(timedelta(hours=5))


def local_iso(utc: str) -> str:
    """Cal.com sends UTC; store call times in local time so the sheet reads naturally."""
    try:
        return datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(_tz()).isoformat(timespec="minutes")
    except ValueError:
        return utc


def fmt_local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_tz()).strftime("%a %d %b, %H:%M")
    except ValueError:
        return iso or "time TBC"


def is_stop(text: str) -> bool:
    return re.sub(r"[^a-z ]", "", (text or "").lower()).strip() in STOP_WORDS


# --------------------------------------------------------------------------- call offer
def call_eligible(lead: Lead) -> bool:
    """Worth 10 minutes of your time? Decides whether the quiz result shows a booking link."""
    f = C.CALL_FILTER
    return bool(
        lead.consent and lead.phone_e164 and lead.stage in _OPEN_STAGES
        and lead.budget_status in f["budget_status"]
        and lead.funds_proof in f["funds_proof"]
        and lead.academic_route in f["academic_route"]
        and lead.months_to_intake is not None and lead.months_to_intake <= f["max_months_to_intake"]
    )


def booking_url(lead: Lead) -> str:
    """Cal.com link prefilled with the student's details; lead_id comes back in the webhook."""
    if not C.BOOKING_URL:
        return ""
    params = {"name": lead.name, "email": lead.email, "attendeePhoneNumber": lead.phone_e164,
              "metadata[lead_id]": lead.lead_id}
    sep = "&" if "?" in C.BOOKING_URL else "?"
    return C.BOOKING_URL + sep + urlencode({k: v for k, v in params.items() if v})


# --------------------------------------------------------------------------- next step
def ship_blockers(lead: Lead) -> list[str]:
    problems = []
    if not lead.consent:
        problems.append("no contact consent")
    if lead.handoff_consent is not True:
        problems.append("set handoff_consent = Yes once they agree to the intro")
    if not lead.phone_e164:
        problems.append("no valid WhatsApp number")
    return problems


def next_step(lead: Lead) -> str:
    s = lead.stage
    if s == "New":
        if lead.call_eligible:
            return "Offered a call · WhatsApp a nudge if no booking within 48h"
        return lead.next_action                      # score-based nurture advice
    if s == "Call booked":
        when = fmt_local(lead.call_at) if lead.call_at else "time TBC"
        skipped = "" if lead.consent else " · they skipped the quiz: send the quiz link first (records consent + answers)"
        return (f"Qualification call {when}: who pays + 28-day funds, passport, any refusals, "
                f"firm intake, decision maker, handoff consent{skipped}")
    if s == "No-show":
        return "Rebook, or qualify over WhatsApp chat"
    if s == "Not yet":
        return f"Revisit on {lead.revisit_on}" if lead.revisit_on else "Set revisit_on"
    if s == "Cooked":
        problems = ship_blockers(lead)
        return "Can't ship yet: " + "; ".join(problems) if problems else "Ships on next sync"
    if s == "Shipped":
        return "Send the intro (handoff_link) · agency should contact within " \
               f"{C.SHIP_CONTACT_SLA_DAYS} days"
    if s in ("Contacted", "Counselling", "Applied", "Offer", "CAS"):
        return "Agency working it · check in with the student monthly"
    if s == "Visa granted":
        return "Confirm with the student · invoice the agency"
    if s == "Enrolled":
        return "Payment due from the agency"
    if s == "Opted out":
        return "Do not contact"
    return "—"                                       # Rejected / Lost / Paid


def refresh(lead: Lead) -> Lead:
    """Run after score(): recompute the call offer and the stage-aware next action."""
    lead.call_eligible = call_eligible(lead)
    lead.next_action = next_step(lead)
    return lead


# --------------------------------------------------------------------------- read back edits
def apply_working_edits(lead: Lead, row: dict, today: date | None = None) -> list[str]:
    """Apply your edits from the working tab. Sheet rows mirror the DB, so a difference is an edit."""
    changes = []
    stage = (row.get("stage") or "").strip()
    if stage and stage != lead.stage:
        if stage in C.STAGES:
            set_stage(lead, stage, today)
            changes.append(f"stage → {stage}")
        else:
            log.warning("lead %s: ignoring unknown stage %r", lead.lead_id, stage)
    for f in ("decision_maker", "reason", "revisit_on", "notes"):
        v = (row.get(f) or "").strip()
        if v != getattr(lead, f):
            setattr(lead, f, v)
            changes.append(f"{f} updated")
    raw = (row.get("handoff_consent") or "").strip()
    hc = norm_bool(raw) if raw else None
    if hc != lead.handoff_consent:
        lead.handoff_consent = hc
        changes.append(f"handoff_consent → {raw or 'blank'}")
    return changes


def apply_agency_edits(lead: Lead, row: dict, today: date | None = None) -> list[str]:
    """Apply the agency's updates from the Delivery tab. Only a *changed* agency stage moves
    the lead, so marking Paid yourself isn't undone by an old 'Enrolled' there."""
    changes = []
    st = (row.get("agency_stage") or "").strip()
    if st != lead.agency_stage:
        lead.agency_stage = st
        if st in C.AGENCY_STAGES:
            set_stage(lead, st, today)
            changes.append(f"agency: {st}")
    for f in ("agency_contacted_on", "agency_notes"):
        v = (row.get(f) or "").strip()
        if v != getattr(lead, f):
            setattr(lead, f, v)
            changes.append(f"{f} updated")
    if lead.agency_contacted_on and lead.stage == "Shipped":
        set_stage(lead, "Contacted", parse_date(lead.agency_contacted_on) or today)
        changes.append("agency: Contacted")
    return changes


def pull(lead: Lead, today: date | None = None) -> None:
    """Pick up unsynced sheet edits before the pipeline changes a lead, so they aren't overwritten."""
    try:
        row = sinks.read_working_row(lead.lead_id)
    except Exception:
        log.exception("could not read working tab for %s; continuing", lead.lead_id)
        return
    if row:
        apply_working_edits(lead, row, today)


# --------------------------------------------------------------------------- ship
def brief(lead: Lead) -> str:
    """What the agency counselor reads before the first call."""
    lvl = LEVEL_NAME.get(lead.study_level or "", lead.study_level or "?")
    grade = f"CGPA {lead.cgpa}" if lead.cgpa is not None else \
        f"{lead.grade_percent:g}%" if lead.grade_percent is not None else "grade n/a"
    test = TEST_NAME.get(lead.english_test or "", lead.english_test or "no test")
    eng = f"{test} {lead.english_score:g}" if lead.english_score is not None else test
    num = lambda v: f"{v:g}" if v is not None else "?"
    yn = lambda v: "yes" if v else "no" if v is False else "?"
    lines = [
        f"{lead.name or 'Unknown'} · {lead.city_normalized.title() or 'city n/a'} · {lead.phone_e164}",
        f"Wants: {lvl}{' in ' + lead.subject if lead.subject else ''} · {lead.recommended_intake or 'intake TBC'}"
        + (f" (asked for {lead.preferred_intake})" if lead.preferred_intake
           and lead.preferred_intake != lead.recommended_intake else ""),
        f"Academics: {QUAL_NAME.get(lead.highest_qualification or '', '?')}, {grade}"
        + (f" ({lead.passing_year})" if lead.passing_year else "")
        + f" → {ROUTE_NAME.get(lead.academic_route, lead.academic_route)}",
        f"English: {eng} → {lead.english_status.replace('_', ' ')}",
        f"Fees: budget {num(lead.budget_pkr_lakh)} lakh vs ~{num(lead.est_fees_pkr_lakh)} lakh "
        f"(tuition + visa + IHS + flights) → {lead.budget_status} · funding: "
        f"{FUNDING_NAME.get(lead.funding_source or '', '?')}",
        f"Living funds to show for 28 days: ~{num(lead.est_living_funds_pkr_lakh)} lakh → "
        f"{FUNDS_NAME.get(lead.funds_proof or '', 'not asked')}",
        f"Decision maker: {lead.decision_maker or '?'} · passport: {yn(lead.has_passport)} · "
        f"prior UK refusal: {yn(lead.previous_uk_refusal)} · dependants: {yn(lead.bring_dependants)}",
    ]
    if lead.other_countries:
        lines.append("Also considering: " + ", ".join(lead.other_countries))
    if lead.flags:
        lines.append("Flags: " + " | ".join(lead.flags))
    if lead.notes:
        lines.append("Call notes: " + lead.notes)
    return "\n".join(lines)


def handoff_message(lead: Lead) -> str:
    first = (lead.name or "").split(" ")[0]
    return (f"Assalam o Alaikum{(' ' + first) if first else ''}! As we discussed, I'm introducing you to "
            f"{C.PARTNER_NAME}, our partner consultancy. Their counselor will WhatsApp you within one "
            f"working day to start your UK application. I'll stay in touch too — message me anytime. "
            f"— {C.BRAND_NAME}")


def ship(lead: Lead, today: date | None = None) -> bool:
    """Cooked → Shipped: log it (write-once), add it to the Delivery tab, tell the agency."""
    if lead.stage != "Cooked" or ship_blockers(lead):
        return False
    lead.shipped_at = now_iso()
    text = brief(lead)
    if not store.log_delivery(lead, text):
        log.warning("lead %s was already shipped; not re-sending", lead.lead_id)
        set_stage(lead, "Shipped", today)
        return False
    lead.handoff_link = f"https://wa.me/{lead.phone_e164.lstrip('+')}?text={quote(handoff_message(lead))}"
    set_stage(lead, "Shipped", today)
    sinks.write_delivery(lead, text)
    sinks.notify(C.AGENCY_WHATSAPP,
                 f"New student from {C.BRAND_NAME}: {lead.name} · "
                 f"{LEVEL_NAME.get(lead.study_level or '', '')} {lead.subject} · {lead.recommended_intake}. "
                 f"Full brief in the {C.DELIVERY_TAB} tab.")
    return True


# --------------------------------------------------------------------------- sync + attention
def sync(today: date | None = None) -> list[str]:
    """Read both tabs, apply edits, ship Cooked leads. Run every 10–15 minutes."""
    working = {r["lead_id"]: r for r in sinks.read_working()}
    agency = {r["lead_id"]: r for r in sinks.read_delivery()}
    report = []
    for lead in store.all_leads():
        changes = []
        if row := working.get(lead.lead_id):
            changes += apply_working_edits(lead, row, today)
        if row := agency.get(lead.lead_id):
            changes += apply_agency_edits(lead, row, today)
        if lead.stage == "Cooked" and ship(lead, today):
            changes.append("SHIPPED to agency")
        if changes:
            refresh(lead)
            lead.updated_at = now_iso()
            store.save(lead, {"sync": changes})
            sinks.write(lead)
            report.append(f"{lead.lead_id} {lead.name}: " + "; ".join(changes))
    return report


def attention(today: date | None = None) -> list[str]:
    """Your to-do list: calls to log, revisits due, agency follow-ups, money owed."""
    today = today or date.today()
    out = []
    for lead in store.all_leads():
        who = f"{lead.name or lead.lead_id} ({lead.lead_id})"
        s = lead.stage
        if s == "Call booked" and (d := parse_date(lead.call_at)) and d < today:
            out.append(f"Log call outcome: {who}")
        elif s == "Not yet" and (d := parse_date(lead.revisit_on)) and d <= today:
            out.append(f"Revisit due: {who}")
        elif s == "Cooked" and (p := ship_blockers(lead)):
            out.append(f"Blocked from shipping: {who}: " + "; ".join(p))
        elif s in ("Visa granted", "Enrolled"):
            out.append(f"Payment due ({s}): {who}")
        elif s in _AGENCY_ACTIVE:
            shipped = parse_date(lead.shipped_at)
            if s == "Shipped" and shipped and (today - shipped).days > C.SHIP_CONTACT_SLA_DAYS:
                out.append(f"Agency hasn't contacted {who} after {(today - shipped).days} days")
            last = max((parse_date(v) for v in lead.stage_history.values() if parse_date(v)), default=None)
            if last and (today - last) > timedelta(days=C.STALE_DAYS):
                out.append(f"No progress for {(today - last).days} days ({s}): {who}")
    return out


# --------------------------------------------------------------------------- inbound events
def opt_out(phone: str, today: date | None = None) -> Lead | None:
    lead = store.get(Lead(phone_e164=normalize_phone(phone)).compute_id())
    if not lead:
        return None
    pull(lead, today)
    lead.consent = False
    set_stage(lead, "Opted out", today)
    refresh(lead)
    lead.updated_at = now_iso()
    store.save(lead, {"opt_out": phone})
    sinks.write(lead)
    return lead


def _answer(p: dict, *keys: str) -> str:
    """A booking-form answer. Cal.com wraps each one as {"label", "value", "isHidden"}; a location
    value nests once more as {"value": "phone", "optionValue": "+92..."}; name can be
    {firstName, lastName}. Older payloads send bare values."""
    responses = p.get("responses") or {}
    for k in keys:
        v = responses.get(k)
        for _ in range(2):                           # unwrap {label, value} then {value, optionValue}
            if isinstance(v, dict) and ("value" in v or "optionValue" in v):
                v = v.get("optionValue") or v.get("value")
        if isinstance(v, dict):
            v = " ".join(str(x) for x in v.values() if x)
        if v not in (None, "", [], {}):
            return str(v)
    return ""


def _real_email(e: str) -> str:
    """Cal.com invents <phone>@sms.cal.com when the email field is hidden; that isn't a contact."""
    e = (e or "").strip().lower()
    return "" if e.endswith("@sms.cal.com") else e


def _booking_phone(p: dict) -> str:
    # Never the top-level "location": with a "Phone call" location that's *your* number.
    return normalize_phone(_answer(p, "attendeePhoneNumber", "phone", "location"))


def _find_booking_lead(p: dict) -> Lead | None:
    lid = (p.get("metadata") or {}).get("lead_id")
    if lid and (lead := store.get(str(lid))):
        return lead
    refs = {r for r in (p.get("uid"), p.get("rescheduleUid"), p.get("bookingUid")) if r}
    leads = store.all_leads()
    if refs and (lead := next((l for l in leads if l.booking_ref in refs), None)):
        return lead
    if (phone := _booking_phone(p)) and (lead := store.get(Lead(phone_e164=phone).compute_id())):
        return lead
    emails = {_real_email(a.get("email")) for a in p.get("attendees") or []}
    emails |= {_real_email(_answer(p, "email"))}
    emails.discard("")
    return next((l for l in leads if l.email.strip().lower() in emails), None) if emails else None


BOOKED = {"BOOKING_CREATED", "BOOKING_REQUESTED", "BOOKING_RESCHEDULED"}
UNBOOKED = {"BOOKING_CANCELLED", "BOOKING_REJECTED"}


def handle_booking(event: dict, today: date | None = None) -> Lead | None:
    """Cal.com webhook. Created / requested / rescheduled → Call booked; cancelled / rejected →
    back to New; no-show marked in Cal.com → No-show. Everything else (PING, ...) is ignored."""
    trigger = event.get("triggerEvent", "")
    p = event.get("payload") or {}
    if trigger not in BOOKED | UNBOOKED | {"BOOKING_NO_SHOW_UPDATED"}:
        return None
    lead = _find_booking_lead(p)
    if lead is None:
        if trigger not in ("BOOKING_CREATED", "BOOKING_REQUESTED"):
            log.warning("Cal.com %s for an unknown booking %s", trigger, p.get("uid") or p.get("bookingUid"))
            return None
        from .process import process                # someone booked without taking the quiz
        attendee = (p.get("attendees") or [{}])[0]
        lead = process(Lead(source="booking", source_ref=p.get("uid", ""),
                            name=_answer(p, "name") or attendee.get("name", ""),
                            email=_real_email(_answer(p, "email") or attendee.get("email", "")),
                            phone=_booking_phone(p)),
                       event, today, notify=False)
        if not lead.lead_id:
            return None
    else:
        pull(lead, today)

    if not lead.consent and norm_bool(_answer(p, "consent")):
        # Cal.com booking question (checkbox, identifier "consent") — same wording as the quiz
        lead.consent, lead.consent_at = True, now_iso()
        lead.consent_text = "Ticked consent checkbox on the Cal.com booking form"

    duplicate = False
    if trigger in BOOKED:
        call_at = local_iso(p.get("startTime", ""))
        duplicate = lead.stage == "Call booked" and lead.booking_ref == p.get("uid") and lead.call_at == call_at
        lead.call_at, lead.booking_ref = call_at, p.get("uid", "") or lead.booking_ref
        if lead.stage in _OPEN_STAGES | {"Call booked"}:
            set_stage(lead, "Call booked", today)
    elif trigger in UNBOOKED:
        if lead.stage != "Call booked":
            return lead
        lead.stage, lead.call_at = "New", ""
    else:                                            # BOOKING_NO_SHOW_UPDATED
        marked = [a.get("noShow") for a in p.get("attendees") or []]
        if any(marked) and lead.stage == "Call booked":
            set_stage(lead, "No-show", today)
        elif marked and not any(marked) and lead.stage == "No-show":
            lead.stage = "Call booked"               # unmarked by mistake
        else:
            return lead
    if duplicate:                                    # Cal.com retried a delivery
        return lead
    refresh(lead)
    lead.updated_at = now_iso()
    store.save(lead, event)
    sinks.write(lead)
    if trigger in BOOKED:
        verb = "rescheduled" if trigger == "BOOKING_RESCHEDULED" else "booked"
        sinks.notify(C.ALERT_WHATSAPP,
                     f"📅 Call {verb}: {lead.name or 'Unknown'} · {fmt_local(lead.call_at)}\n"
                     f"{lead.tier} {lead.score}/100 · {lead.study_level} {lead.subject} · "
                     f"{lead.recommended_intake}\n{lead.whatsapp_link}")
    return lead
