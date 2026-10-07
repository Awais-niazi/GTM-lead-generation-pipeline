"""Webhook server. Run:  uvicorn app:app --host 0.0.0.0 --port 8000

Endpoints
  POST /webhook/quiz        landing-page submissions
  GET  /webhook/meta        Meta verification handshake (lead ads + WhatsApp)
  POST /webhook/meta        Meta events: 'leadgen' (lead ads) and WhatsApp messages
  POST /webhook/booking     Cal.com booking created / rescheduled / cancelled
  GET  /leads/summary       quick counts by tier/source
  GET  /health
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from pipeline import config as C
from pipeline import sinks, sources, store, workflow
from pipeline.process import process

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger().setLevel(logging.INFO)   # Lambda pre-installs a handler, so basicConfig is a no-op there
log = logging.getLogger("uk-leads")

app = FastAPI(title=f"{C.BRAND_NAME} UK lead pipeline")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["POST"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"ok": True}


# --------------------------------------------------------------------------- landing page quiz
@app.post("/webhook/quiz")
async def quiz(request: Request, x_quiz_key: str | None = Header(default=None)):
    if C.QUIZ_SHARED_KEY and x_quiz_key != C.QUIZ_SHARED_KEY:
        raise HTTPException(401, "bad key")
    payload = await request.json()
    if payload.get("website"):                 # honeypot field — bots fill it
        return {"ok": True}
    if not payload.get("consent"):
        raise HTTPException(400, "consent required")
    lead = await run_in_threadpool(process, sources.from_web_quiz(payload), payload)
    # Return only what the student should see (no internal score).
    url = workflow.booking_url(lead) if lead.call_eligible else ""
    return {"ok": True, "recommended_intake": lead.recommended_intake,
            "est_cost_pkr_lakh": lead.est_first_year_cost_pkr_lakh,
            "budget_status": lead.budget_status, "english_status": lead.english_status,
            "academic_route": lead.academic_route, "book_call": bool(url), "booking_url": url}


# --------------------------------------------------------------------------- Meta (lead ads + WhatsApp)
@app.get("/webhook/meta", response_class=PlainTextResponse)
def meta_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == C.META_VERIFY_TOKEN:
        return q.get("hub.challenge", "")
    raise HTTPException(403, "verification failed")


def _valid_signature(body: bytes, header: str | None, secret: str, prefix: str = "sha256=") -> bool:
    if not secret:
        return True                            # dev mode
    if not header or not header.startswith(prefix):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len(prefix):])


def _handle_meta(payload: dict) -> None:
    obj = payload.get("object")
    if obj == "page":
        for lid in sources.meta_leadgen_ids(payload):
            try:
                process(sources.fetch_meta_lead(lid), {"leadgen_id": lid})
            except Exception:
                log.exception("failed to fetch Meta lead %s", lid)
    elif obj == "whatsapp_business_account":
        for lead in sources.from_whatsapp(payload):
            if workflow.is_stop(lead.message):
                workflow.opt_out(lead.phone)
                continue
            is_new = store.get(_id_for(lead)) is None
            processed = process(lead, payload)
            if is_new and processed.completeness < 60:
                sinks.reply_with_quiz(processed)


def _id_for(lead):
    from pipeline.enrich import normalize_phone
    lead.phone_e164 = normalize_phone(lead.phone)
    return lead.compute_id()


@app.post("/webhook/meta")
async def meta_events(request: Request, bg: BackgroundTasks,
                      x_hub_signature_256: str | None = Header(default=None)):
    body = await request.body()
    if not _valid_signature(body, x_hub_signature_256, C.META_APP_SECRET):
        raise HTTPException(401, "bad signature")
    bg.add_task(_handle_meta, await request.json())   # ack fast; Meta retries slow endpoints
    return {"ok": True}


# --------------------------------------------------------------------------- Cal.com bookings
@app.post("/webhook/booking")
async def booking(request: Request, x_cal_signature_256: str | None = Header(default=None)):
    body = await request.body()
    if not _valid_signature(body, x_cal_signature_256, C.CALCOM_WEBHOOK_SECRET, prefix=""):
        raise HTTPException(401, "bad signature")
    lead = await run_in_threadpool(workflow.handle_booking, await request.json())
    return {"ok": True, "matched": bool(lead)}


# --------------------------------------------------------------------------- reporting
@app.get("/leads/summary")
def summary():
    leads = store.all_leads()
    by = lambda attr: {k: sum(1 for l in leads if getattr(l, attr) == k)
                       for k in sorted({getattr(l, attr) for l in leads})}
    return {"total": len(leads), "by_tier": by("tier"), "by_source": by("source"), "by_stage": by("stage")}
