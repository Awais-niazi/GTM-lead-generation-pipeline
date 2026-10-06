"""CLI.

  python run.py import leads.csv     # bulk-process a CSV (fair, expo, old list) — headers = Lead field names
  python run.py rescore              # re-enrich + re-score every stored lead (run daily: intakes move closer)
  python run.py show <lead_id>
"""
from __future__ import annotations

import csv
import sys

from pipeline import sinks, store
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


def rescore() -> None:
    for lead in store.all_leads():
        before = lead.tier
        enrich(lead)
        score(lead)
        store.save(lead, {"rescore": True})
        sinks.write(lead)
        if before != lead.tier:
            print(f"{lead.lead_id} {before} → {lead.tier}")
            if lead.tier == "Hot" and lead.consent:
                sinks.alert_counselors(lead)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "import":
        import_csv(sys.argv[2])
    elif cmd == "rescore":
        rescore()
    elif cmd == "show":
        lead = store.get(sys.argv[2])
        print(sinks.dump_json(lead) if lead else "not found")
    else:
        print(__doc__)
