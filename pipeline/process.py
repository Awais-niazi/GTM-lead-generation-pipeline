"""One function every channel calls: normalize → dedupe/merge → enrich → score → store → output."""
from __future__ import annotations

import logging
from datetime import date

from . import sinks, store
from .enrich import enrich
from .models import Lead
from .score import score

log = logging.getLogger("uk-leads")


def process(incoming: Lead, payload: dict | None = None, today: date | None = None,
            notify: bool = True) -> Lead:
    enrich(incoming, today)                 # normalizes phone so we can find duplicates
    lid = incoming.compute_id()
    if not lid:
        incoming.tier, incoming.next_action = "Disqualified", "No phone or email"
        return incoming
    existing = store.get(lid)
    was_hot = existing is not None and existing.tier == "Hot"
    lead = store.merge(existing, incoming) if existing else incoming
    lead.lead_id = lid

    enrich(lead, today)
    score(lead)
    store.save(lead, payload)
    where = sinks.write(lead)
    log.info("lead %s %s %s → %s (%s)", lid, lead.tier, lead.score, where,
             "merged" if existing else "new")

    if notify and lead.tier == "Hot" and lead.consent and not was_hot:
        sinks.alert_counselors(lead)
    return lead
