"""Outputs: Google Sheets (primary), CSV (fallback), WhatsApp alerts.

Two tabs:
  * working tab  (C.SHEET_TAB)    — every lead; yours. You edit WORKING_EDITABLE columns.
  * delivery tab (C.DELIVERY_TAB) — shipped leads only; shared with the agency, who edit
                                    AGENCY_EDITABLE columns. Rows are written once.
Both are read back by pipeline.workflow.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re

import httpx

from . import config as C
from .models import DELIVERY_COLUMNS, SHEET_COLUMNS, Lead

log = logging.getLogger("uk-leads")

# Dropdowns added to each tab so edits stay machine-readable.
WORKING_DROPDOWNS = {"stage": C.STAGES, "decision_maker": C.DECISION_MAKERS, "handoff_consent": ["Yes", "No"]}
DELIVERY_DROPDOWNS = {"agency_stage": C.AGENCY_STAGES}


def _cell(v):
    """Typed value for Sheets (written RAW, so text is never parsed as a formula or number)."""
    if isinstance(v, list):
        return " | ".join(v)
    if isinstance(v, dict):
        return ", ".join(f"{k} {n}" for k, n in v.items())
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return "" if v is None else v


def _row(values: dict, columns: list[str]) -> list:
    return [_cell(values.get(c)) for c in columns]


def _csv_safe(v) -> str:
    """Stop spreadsheet apps executing student-typed text like '=HYPERLINK(...)' from the CSV."""
    s = str(v)
    if s[:1] in ("=", "@") or (s[:1] in ("+", "-") and not re.fullmatch(r"[+-][\d.\s]+", s)):
        return "'" + s
    return s


def _use_sheets() -> bool:
    return bool(C.GOOGLE_SHEET_ID) and os.path.exists(C.GOOGLE_SERVICE_ACCOUNT_FILE)


# --------------------------------------------------------------------------- Google Sheets
_ws: dict[str, object] = {}


def _worksheet(tab: str, columns: list[str], dropdowns: dict[str, list[str]]):
    if tab in _ws:
        return _ws[tab]
    import gspread
    gc = gspread.service_account(filename=C.GOOGLE_SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(C.GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(tab, rows=1000, cols=len(columns))
    if ws.row_values(1) != columns:
        ws.update([columns], "A1")
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                          "backgroundColor": {"red": .12, "green": .16, "blue": .27}})
        sh.batch_update({"requests": [{"setDataValidation": {
            "range": {"sheetId": ws.id, "startRowIndex": 1,
                      "startColumnIndex": columns.index(col), "endColumnIndex": columns.index(col) + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": o} for o in opts]},
                     "strict": True, "showCustomUi": True},
        }} for col, opts in dropdowns.items()]})
    _ws[tab] = ws
    return ws


def _sheet_records(tab: str, columns: list[str], dropdowns: dict) -> list[dict]:
    values = _worksheet(tab, columns, dropdowns).get_all_values()
    if not values:
        return []
    header = values[0]
    return [dict(zip(header, r)) for r in values[1:] if r and r[0]]


def _sheet_upsert(tab: str, columns: list[str], dropdowns: dict, row: list, replace: bool) -> None:
    ws = _worksheet(tab, columns, dropdowns)
    ids = ws.col_values(1)
    if row[0] in ids:
        if replace:
            ws.update([row], f"A{ids.index(row[0]) + 1}", value_input_option="RAW")
    else:
        ws.append_row(row, value_input_option="RAW")


# --------------------------------------------------------------------------- CSV fallback
def _csv_records(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (v[1:] if v.startswith("'") else v) for k, v in r.items() if k}
                for r in csv.DictReader(f) if r.get("lead_id")]


def _csv_upsert(path: str, columns: list[str], row: dict, replace: bool, sort_key=None) -> None:
    rows = {r["lead_id"]: r for r in _csv_records(path)}
    if row["lead_id"] in rows and not replace:
        return
    rows[row["lead_id"]] = row
    out = sorted(rows.values(), key=sort_key) if sort_key else list(rows.values())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows({c: _csv_safe(r.get(c, "")) for c in columns} for r in out)


def _score_desc(r: dict) -> float:
    try:
        return -float(r.get("score") or 0)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- working tab
def write(lead: Lead) -> str:
    d = lead.model_dump()
    if _use_sheets():
        try:
            _sheet_upsert(C.SHEET_TAB, SHEET_COLUMNS, WORKING_DROPDOWNS, _row(d, SHEET_COLUMNS), replace=True)
            return "sheet"
        except Exception as e:  # never lose a lead because Sheets hiccuped
            log.exception("Sheets write failed, falling back to CSV: %s", e)
    _csv_upsert(C.CSV_FALLBACK, SHEET_COLUMNS, dict(zip(SHEET_COLUMNS, _row(d, SHEET_COLUMNS))),
                replace=True, sort_key=_score_desc)
    return "csv"


def read_working() -> list[dict]:
    """Rows of the working tab as {column: text}. Raises if Sheets is configured but unreachable."""
    if _use_sheets():
        return _sheet_records(C.SHEET_TAB, SHEET_COLUMNS, WORKING_DROPDOWNS)
    return _csv_records(C.CSV_FALLBACK)


def read_working_row(lead_id: str) -> dict | None:
    return next((r for r in read_working() if r.get("lead_id") == lead_id), None)


# --------------------------------------------------------------------------- delivery tab
def write_delivery(lead: Lead, brief: str) -> str:
    """Append the shipped lead. An existing row is never rewritten (the agency owns its edits)."""
    d = {**lead.model_dump(), "brief": brief}
    if _use_sheets():
        try:
            _sheet_upsert(C.DELIVERY_TAB, DELIVERY_COLUMNS, DELIVERY_DROPDOWNS,
                          _row(d, DELIVERY_COLUMNS), replace=False)
            return "sheet"
        except Exception as e:
            log.exception("Delivery sheet write failed, falling back to CSV: %s", e)
    _csv_upsert(C.DELIVERY_CSV, DELIVERY_COLUMNS, dict(zip(DELIVERY_COLUMNS, _row(d, DELIVERY_COLUMNS))),
                replace=False)
    return "csv"


def read_delivery() -> list[dict]:
    if _use_sheets():
        return _sheet_records(C.DELIVERY_TAB, DELIVERY_COLUMNS, DELIVERY_DROPDOWNS)
    return _csv_records(C.DELIVERY_CSV)


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


def notify(numbers: list[str], body: str) -> None:
    """Free-form text works only inside a 24h window with each recipient —
    in production, register utility templates and send those instead."""
    for n in numbers:
        _wa_send(n, body)


def alert_hot(lead: Lead) -> None:
    notify(C.ALERT_WHATSAPP,
           f"🔥 HOT UK lead ({lead.score}/100)\n{lead.name or 'Unknown'} · {lead.city_normalized.title()}\n"
           f"{lead.study_level} {lead.subject} · {lead.recommended_intake}\n"
           f"Budget {lead.budget_pkr_lakh} lakh vs need ~{lead.est_first_year_cost_pkr_lakh} lakh "
           f"({lead.budget_status}) · English: {lead.english_status}\n"
           f"{lead.whatsapp_link}\nNext: {lead.next_action}")


def reply_with_quiz(lead: Lead) -> None:
    """Reply to a student who messaged first (inside the 24h service window)."""
    first = (lead.name or "").split(" ")[0]
    link = f"{C.QUIZ_URL}?src=whatsapp&phone={lead.phone_e164.lstrip('+')}"
    _wa_send(lead.phone_e164,
             f"Assalam o Alaikum{(' ' + first) if first else ''}! Thanks for contacting {C.BRAND_NAME} "
             f"about studying in the UK 🇬🇧\n\nTake our 2-minute UK eligibility check — you'll instantly "
             f"see your estimated cost in PKR and which intake fits you:\n{link}\n\n"
             f"We'll also reply here shortly.")


def dump_json(lead: Lead) -> str:
    return json.dumps(lead.model_dump(), indent=2, ensure_ascii=False)
