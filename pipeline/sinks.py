"""Outputs: Google Sheets (primary), CSV (fallback), WhatsApp alerts.

Two tabs:
  * working tab  (C.SHEET_TAB)    — every lead; yours. You edit WORKING_EDITABLE columns.
  * delivery tab (C.DELIVERY_TAB, in C.DELIVERY_SHEET_ID) — shipped leads only; a separate
                                    spreadsheet shared with the agency, who edit
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
    return bool(C.GOOGLE_SHEET_ID) and bool(C.GOOGLE_SERVICE_ACCOUNT_SSM
                                           or os.path.exists(C.GOOGLE_SERVICE_ACCOUNT_FILE))


# --------------------------------------------------------------------------- Google Sheets
_ws: dict[tuple[str, str], object] = {}
_gc = None


def _client():
    """gspread client from the key file (local) or the SSM SecureString (Lambda)."""
    global _gc
    if _gc is None:
        import gspread
        if C.GOOGLE_SERVICE_ACCOUNT_SSM:
            import boto3
            p = boto3.client("ssm", region_name=C.AWS_REGION or None).get_parameter(
                Name=C.GOOGLE_SERVICE_ACCOUNT_SSM, WithDecryption=True)
            _gc = gspread.service_account_from_dict(json.loads(p["Parameter"]["Value"]))
        else:
            _gc = gspread.service_account(filename=C.GOOGLE_SERVICE_ACCOUNT_FILE)
    return _gc


def _worksheet(sheet_id: str, tab: str, columns: list[str], dropdowns: dict[str, list[str]]):
    if (sheet_id, tab) in _ws:
        return _ws[(sheet_id, tab)]
    import gspread
    sh = _client().open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        sheets = sh.worksheets()
        if len(sheets) == 1 and not any(c for r in sheets[0].get_all_values() for c in r):  # new: reuse "Sheet1"
            ws = sheets[0]
            ws.update_title(tab)
            ws.resize(rows=1000, cols=len(columns))
        else:
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
    _ws[(sheet_id, tab)] = ws
    return ws


def _sheet_records(sheet_id: str, tab: str, columns: list[str], dropdowns: dict) -> list[dict]:
    values = _worksheet(sheet_id, tab, columns, dropdowns).get_all_values()
    if not values:
        return []
    header = values[0]
    return [dict(zip(header, r)) for r in values[1:] if r and r[0]]


def _sheet_upsert(sheet_id: str, tab: str, columns: list[str], dropdowns: dict, row: list,
                  replace: bool) -> None:
    ws = _worksheet(sheet_id, tab, columns, dropdowns)
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
            _sheet_upsert(C.GOOGLE_SHEET_ID, C.SHEET_TAB, SHEET_COLUMNS, WORKING_DROPDOWNS,
                          _row(d, SHEET_COLUMNS), replace=True)
            return "sheet"
        except Exception as e:  # never lose a lead because Sheets hiccuped
            log.exception("Sheets write failed, falling back to CSV: %s", e)
    _csv_upsert(C.CSV_FALLBACK, SHEET_COLUMNS, dict(zip(SHEET_COLUMNS, _row(d, SHEET_COLUMNS))),
                replace=True, sort_key=_score_desc)
    return "csv"


def write_all(leads: list[Lead]) -> str:
    """Rewrite every lead's row in one request (daily rescore). Rows keep their positions;
    rows for ids not in `leads` are left as they are; new leads are appended."""
    rows = {l.lead_id: _row(l.model_dump(), SHEET_COLUMNS) for l in leads if l.lead_id}
    if _use_sheets():
        try:
            ws = _worksheet(C.GOOGLE_SHEET_ID, C.SHEET_TAB, SHEET_COLUMNS, WORKING_DROPDOWNS)
            current = ws.get_all_values()[1:]
            grid = [SHEET_COLUMNS] + [rows.pop(r[0], r) if r else r for r in current] + list(rows.values())
            width = len(SHEET_COLUMNS)
            grid = [(list(r) + [""] * width)[:width] for r in grid]
            if len(grid) > ws.row_count:
                ws.resize(rows=len(grid) + 200)
            ws.update(grid, "A1", value_input_option="RAW")
            return "sheet"
        except Exception as e:
            log.exception("Sheets batch write failed, falling back to CSV: %s", e)
    existing = {r["lead_id"]: r for r in _csv_records(C.CSV_FALLBACK)}
    existing.update({i: dict(zip(SHEET_COLUMNS, r)) for i, r in rows.items()})
    with open(C.CSV_FALLBACK, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows({c: _csv_safe(r.get(c, "")) for c in SHEET_COLUMNS}
                    for r in sorted(existing.values(), key=_score_desc))
    return "csv"


def read_working() -> list[dict]:
    """Rows of the working tab as {column: text}. Raises if Sheets is configured but unreachable."""
    if _use_sheets():
        return _sheet_records(C.GOOGLE_SHEET_ID, C.SHEET_TAB, SHEET_COLUMNS, WORKING_DROPDOWNS)
    return _csv_records(C.CSV_FALLBACK)


def read_working_row(lead_id: str) -> dict | None:
    return next((r for r in read_working() if r.get("lead_id") == lead_id), None)


# --------------------------------------------------------------------------- delivery tab
def write_delivery(lead: Lead, brief: str) -> str:
    """Append the shipped lead. An existing row is never rewritten (the agency owns its edits)."""
    d = {**lead.model_dump(), "brief": brief}
    if _use_sheets():
        try:
            _sheet_upsert(C.DELIVERY_SHEET_ID, C.DELIVERY_TAB, DELIVERY_COLUMNS, DELIVERY_DROPDOWNS,
                          _row(d, DELIVERY_COLUMNS), replace=False)
            return "sheet"
        except Exception as e:
            log.exception("Delivery sheet write failed, falling back to CSV: %s", e)
    _csv_upsert(C.DELIVERY_CSV, DELIVERY_COLUMNS, dict(zip(DELIVERY_COLUMNS, _row(d, DELIVERY_COLUMNS))),
                replace=False)
    return "csv"


def read_delivery() -> list[dict]:
    if _use_sheets():
        return _sheet_records(C.DELIVERY_SHEET_ID, C.DELIVERY_TAB, DELIVERY_COLUMNS, DELIVERY_DROPDOWNS)
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
           f"Fees budget {lead.budget_pkr_lakh} lakh vs ~{lead.est_fees_pkr_lakh} lakh ({lead.budget_status}) · "
           f"living funds: {lead.funds_proof or '?'} · English: {lead.english_status}\n"
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
