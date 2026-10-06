"""Outputs: Google Sheets (primary), CSV (fallback), WhatsApp alerts."""
from __future__ import annotations

import csv
import json
import logging
import os

import httpx

from . import config as C
from .models import SHEET_COLUMNS, Lead

log = logging.getLogger("uk-leads")

# Counselors edit these in the sheet; the pipeline never overwrites them.
COUNSELOR_OWNED = {"status", "assigned_to"}


def _row(lead: Lead) -> list[str]:
    d = lead.model_dump()
    out = []
    for col in SHEET_COLUMNS:
        v = d.get(col)
        if isinstance(v, list):
            v = " | ".join(v)
        elif isinstance(v, dict):
            v = ", ".join(f"{k} {n}" for k, n in v.items())
        elif isinstance(v, bool):
            v = "Yes" if v else "No"
        out.append("" if v is None else str(v))
    return out


# --------------------------------------------------------------------------- Google Sheets
_ws = None


def _worksheet():
    global _ws
    if _ws is not None:
        return _ws
    import gspread
    gc = gspread.service_account(filename=C.GOOGLE_SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(C.GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(C.SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(C.SHEET_TAB, rows=1000, cols=len(SHEET_COLUMNS))
    if ws.row_values(1) != SHEET_COLUMNS:
        ws.update([SHEET_COLUMNS], "A1")
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                          "backgroundColor": {"red": .12, "green": .16, "blue": .27}})
    _ws = ws
    return ws


def to_sheet(lead: Lead) -> None:
    ws = _worksheet()
    ids = ws.col_values(1)
    row = _row(lead)
    if lead.lead_id in ids:
        r = ids.index(lead.lead_id) + 1
        current = ws.row_values(r)
        for i, col in enumerate(SHEET_COLUMNS):
            if col in COUNSELOR_OWNED and i < len(current) and current[i]:
                row[i] = current[i]       # keep counselor's edits
        ws.update([row], f"A{r}", value_input_option="USER_ENTERED")
    else:
        ws.append_row(row, value_input_option="USER_ENTERED")


def to_csv(lead: Lead) -> None:
    rows = {}
    if os.path.exists(C.CSV_FALLBACK):
        with open(C.CSV_FALLBACK, newline="", encoding="utf-8") as f:
            for r in csv.reader(f):
                if r and r[0] != "lead_id":
                    rows[r[0]] = r
    rows[lead.lead_id] = _row(lead)
    with open(C.CSV_FALLBACK, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SHEET_COLUMNS)
        w.writerows(sorted(rows.values(), key=lambda r: -int(r[2] or 0)))


def write(lead: Lead) -> str:
    if C.GOOGLE_SHEET_ID and os.path.exists(C.GOOGLE_SERVICE_ACCOUNT_FILE):
        try:
            to_sheet(lead)
            return "sheet"
        except Exception as e:  # never lose a lead because Sheets hiccuped
            log.exception("Sheets write failed, falling back to CSV: %s", e)
    to_csv(lead)
    return "csv"


# --------------------------------------------------------------------------- WhatsApp
def _wa_send(to_e164: str, body: str) -> bool:
    if not (C.WHATSAPP_TOKEN and C.WHATSAPP_PHONE_NUMBER_ID):
        log.info("[whatsapp disabled] → %s: %s", to_e164, body[:120])
        return False
    r = httpx.post(
        f"https://graph.facebook.com/{C.META_GRAPH_VERSION}/{C.WHATSAPP_PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {C.WHATSAPP_TOKEN}"},
        json={"messaging_product": "whatsapp", "to": to_e164.lstrip("+"),
              "type": "text", "text": {"body": body}},
        timeout=15,
    )
    if r.status_code >= 300:
        log.warning("WhatsApp send failed %s: %s", r.status_code, r.text[:300])
    return r.status_code < 300


def alert_counselors(lead: Lead) -> None:
    """Free-form text works only inside a 24h window with each counselor —
    in production, register a 'new_hot_lead' utility template and send that."""
    msg = (f"🔥 HOT UK lead ({lead.score}/100)\n{lead.name or 'Unknown'} · {lead.city_normalized.title()}\n"
           f"{lead.study_level} {lead.subject} · {lead.recommended_intake}\n"
           f"Budget {lead.budget_pkr_lakh} lakh vs need ~{lead.est_first_year_cost_pkr_lakh} lakh "
           f"({lead.budget_status}) · English: {lead.english_status}\n"
           f"{lead.whatsapp_link}\nNext: {lead.next_action}")
    for n in C.COUNSELOR_WHATSAPP:
        _wa_send(n, msg)


def reply_with_quiz(lead: Lead) -> None:
    """Reply to a student who messaged first (inside the 24h service window)."""
    first = (lead.name or "").split(" ")[0]
    link = f"{C.QUIZ_URL}?src=whatsapp&phone={lead.phone_e164.lstrip('+')}"
    _wa_send(lead.phone_e164,
             f"Assalam o Alaikum{(' ' + first) if first else ''}! Thanks for contacting {C.AGENCY_NAME} "
             f"about studying in the UK 🇬🇧\n\nTake our 2-minute UK eligibility check — you'll instantly "
             f"see your estimated cost in PKR and which intake fits you:\n{link}\n\n"
             f"A counselor will also reply here shortly.")


def dump_json(lead: Lead) -> str:
    return json.dumps(lead.model_dump(), indent=2, ensure_ascii=False)
