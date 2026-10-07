"""CLI.

  python run.py sync                 # read sheet edits, ship Cooked leads, pull agency updates (every 10–15 min)
  python run.py todo                 # what needs you: calls to log, revisits, agency follow-ups, money due
  python run.py rescore              # sync, then re-enrich + re-score every lead (daily: intakes move closer)
  python run.py deliveries           # the write-once log of every lead you shipped (for invoicing)
  python run.py import leads.csv     # bulk-process a CSV — headers = Lead field names
  python run.py show <lead_id>
"""
from __future__ import annotations

import csv
import sys

from pipeline import sinks, store, workflow
from pipeline.enrich import enrich
from pipeline.models import Lead
from pipeline.process import process
from pipeline.score import score
from pipeline.sources import norm_bool

BOOL_FIELDS = {"consent", "has_passport", "previous_uk_refusal", "bring_dependants", "english_medium"}
NUM_FIELDS = {"grade_percent", "cgpa", "english_score", "budget_pkr_lakh"}


def import_csv(path: str) -> None:
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            kw = {}
            for k, v in row.items():
                k = (k or "").strip()
                if k not in Lead.model_fields or v in ("", None):
                    continue
                if k in BOOL_FIELDS:
                    v = norm_bool(v)
                elif k in NUM_FIELDS:
                    v = float(v)
                elif k == "passing_year":
                    v = int(float(v))
                elif k == "other_countries":
                    v = [c.strip() for c in v.split(",")]
                kw[k] = v
            lead = process(Lead(**kw, source="import"), row, notify=False)
            print(f"{lead.lead_id or '-':12} {lead.tier:12} {lead.score:3}  {lead.name}")
            n += 1
    print(f"\n{n} rows processed")


def sync() -> None:
    for line in workflow.sync():
        print(line)


def todo() -> None:
    items = workflow.attention()
    print("\n".join(items) if items else "Nothing needs you right now.")


def rescore() -> None:
    sync()                                   # never overwrite unsynced sheet edits
    for lead in store.all_leads():
        before = lead.tier
        enrich(lead)
        score(lead)
        workflow.refresh(lead)
        store.save(lead, {"rescore": True})
        sinks.write(lead)
        if before != lead.tier:
            print(f"{lead.lead_id} {before} → {lead.tier}")
            if lead.tier == "Hot" and lead.consent and lead.stage == "New":
                sinks.alert_hot(lead)
    todo()


def show_deliveries() -> None:
    rows = store.deliveries()
    for d in rows:
        print(f"{d['shipped_at']}  {d['lead_id']}  sha256:{d['sha256'][:12]}\n  " +
              d["brief"].replace("\n", "\n  ") + "\n")
    print(f"{len(rows)} leads shipped")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "import":
        import_csv(sys.argv[2])
    elif cmd == "sync":
        sync()
    elif cmd == "todo":
        todo()
    elif cmd == "rescore":
        rescore()
    elif cmd == "deliveries":
        show_deliveries()
    elif cmd == "show":
        lead = store.get(sys.argv[2])
        print(sinks.dump_json(lead) if lead else "not found")
    else:
        print(__doc__)
