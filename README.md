# UK Student Lead Pipeline

Finds students who opt in (eligibility quiz, WhatsApp, optionally Meta lead ads),
works out what a UK application actually needs (real first-year cost in PKR vs budget,
academic entry route, English readiness, realistic intake, visa red flags), scores
them 0–100, and walks each one through to a partner consultancy:

```
Post (tracked link) ─► Quiz ─► scored lead ─► booking link (only if worth a call)
                                                  │
                         Cal.com booking ◄────────┘
                                │
                   Qualification call (you) ─► stage = Cooked + handoff consent
                                │
                          python run.py sync ─► write-once delivery log
                                │                + Delivery tab row with a brief
                                │                + intro message ready to send
                          Agency updates the Delivery tab ─► Contacted … Visa granted
                                │
                          You mark Paid
```

A student who WhatsApps you, then fills the quiz, then clicks an ad becomes
**one row** that gets richer each time.

## What gets added to every lead

| Field | How it's derived |
|---|---|
| `phone_e164`, `whatsapp_link` | 03XX / 92… / +92… all normalized; click-to-chat link |
| `est_first_year_cost_pkr_lakh` | tuition floor + 9 months UK maintenance (London vs outside, date-aware) + £558 visa + £776/yr IHS + flights |
| `budget_status`, `budget_gap_pkr_lakh` | student budget vs that cost: sufficient / tight (≥85%) / short |
| `academic_route` | Pakistani qualification → direct / check / foundation / pre-master's (HSSC %, 14 vs 16-year degree, CGPA) |
| `english_status` | meets / below / waiver possible (English-medium, degree level) / needs test |
| `recommended_intake`, `months_to_intake` | earliest intake they can realistically make given passport + English readiness |
| `study_gap_years` | flags gaps of 5+ years that need a written explanation |
| `flags` | dependants on a taught course (not allowed), prior UK refusal, scholarship-only funding, disposable email, no passport… |
| `score`, `tier`, `score_breakdown` | budget 30 · academics 20 · English 15 · timeline 15 · intent 10 · contactability 10 |
| `next_action` | what to do next, based on the stage (and on the score while the lead is New) |
| `call_eligible` | whether the quiz result offers a booking link (see `CALL_FILTER` in `config.py`) |

Tiers: **Hot** ≥70 (call within an hour), **Warm** ≥50, **Nurture** ≥30, **Cold**.
Hard caps: budget short → max 55; dependants on a taught course → max 60; prior refusal → max 65.

All tunables (visa figures, tuition floors, weights, thresholds, PKR rate) are in
`pipeline/config.py`.

## Setup (about an hour)

### 1. Run it
```bash
pip install -r requirements.txt
cp .env.example .env        # fill in, then export (or use your host's env settings)
python -m pytest -q tests    # 20 tests
uvicorn app:app --host 0.0.0.0 --port 8000
```
Host anywhere with HTTPS: Railway, Render, a small VPS. Meta webhooks require HTTPS.
Without Google credentials the pipeline writes `leads_export.csv` instead, so you can try it immediately.

### 2. Google Sheets
1. Google Cloud console → create project → enable **Google Sheets API** and **Google Drive API**.
2. Create a **service account**, download its JSON key as `service_account.json`.
3. Create **two** spreadsheets: your private one (e.g. "Leads — private") and one for the agency
   (e.g. "Deliveries — <agency>"). Share **both** (Editor) with the service account's email.
4. Put their IDs (the long part of each URL between `/d/` and `/edit`) into `GOOGLE_SHEET_ID`
   and `DELIVERY_SHEET_ID`. Share only the Deliveries spreadsheet with the agency.

Two tabs are created automatically, with headers and dropdowns:

- **Leads** (yours): every lead. You edit `stage`, `decision_maker`, `handoff_consent`,
  `reason`, `revisit_on` (dd/mm/yyyy or yyyy-mm-dd) and `notes`. Everything else is
  written by the pipeline.
- **Delivery**, in a **separate spreadsheet** (`DELIVERY_SHEET_ID`) that you share with the
  agency. Sheets can't share a single tab, so sharing the main spreadsheet would show them every
  lead. One row per shipped lead, with a brief. The agency fills in `agency_stage`,
  `agency_contacted_on` and `agency_notes`; the pipeline only reads those.

Values are written as plain text, so nothing a student types can run as a formula.
If you're upgrading an older sheet, start with a fresh tab: the columns have changed.

### 3. Landing page (eligibility quiz)
`landing/uk-eligibility.html`: set `PIPELINE_URL` to `https://your-host/webhook/quiz`,
`QUIZ_KEY` to match `QUIZ_SHARED_KEY`, and `BRAND` to match `BRAND_NAME`. Host it on your website.
Give each post its own link (`?utm_source=tiktok&utm_campaign=cost-pkr-oct12`) so you can see
which posts produce leads that ship. Students who pass `CALL_FILTER` see a "Book a free
10-minute WhatsApp call" button; everyone else sees their plan and goes to nurture.
UTM tags (`?utm_source=fb&utm_campaign=jan27`) are captured automatically.
Students see their result instantly; that instant answer is why they give you a real number.

### 4. Cal.com (qualification call)
The free plan is enough (webhooks included).

**Event type** (e.g. "UK plan check — 10 min", slug `uk-call`):
1. **Location:** *Attendee phone number*. You WhatsApp-call the number they give.
2. **Booking questions:** turn on **Phone number** (`attendeePhoneNumber`) and make it required.
   Optionally add a required **Checkbox** with identifier `consent` and the same wording as the
   quiz checkbox, so people who book from a shared link (skipping the quiz) are covered too.
3. **Limits:** minimum notice ~4 hours, a 10-minute buffer, and a daily cap you can keep up with.
4. **Reminders:** if your plan includes Workflows, add reminders 24 hours and 1 hour before. If not, send a WhatsApp reminder yourself from the `call_at` column.
5. Put the event link in `BOOKING_URL`, e.g. `https://cal.com/yourbrand/uk-call`.

The quiz prefills name, email and phone, and passes `metadata[lead_id]`, so the booking is
tied to the right lead.

**Webhook** (Settings → Developer → Webhooks → New):
- Subscriber URL: `https://your-host/webhook/booking`
- Triggers: *Booking created*, *Booking requested*, *Booking rescheduled*,
  *Booking cancelled*, *Booking rejected*, *Booking no-show updated*
- Secret: a long random string → `CALCOM_WEBHOOK_SECRET` (checked against `X-Cal-Signature-256`)
- Leave **custom payload template empty**; the pipeline expects Cal.com's default payload.

| Cal.com event | Lead |
|---|---|
| Created / requested | → **Call booked**, call time saved in PKT (`TIMEZONE`), WhatsApp alert to you |
| Rescheduled | new time and booking id, alert to you |
| Cancelled / rejected | back to **New** (the booking link is offered again) |
| No-show marked in Cal.com | → **No-show** (unmarking puts it back) |

To test before you have hosting, run the app locally and open a temporary HTTPS tunnel:
```bash
uvicorn app:app --port 8000
cloudflared tunnel --url http://localhost:8000     # prints https://<random>.trycloudflare.com
```
Use that URL as the subscriber URL, press **Ping test** (expect `{"ok":true,"matched":false}`),
then take the quiz and book a slot. The lead's `stage` should change to *Call booked*.

### 5. Daily routine

| Stage | What happens |
|---|---|
| New | Quiz done. Eligible leads were offered a call. |
| Call booked | Set by Cal.com. Do the call: who pays + 28-day funds, passport, refusals, firm intake, decision maker, consent to be introduced. |
| No-show / Not yet / Rejected | You set these. Fill `reason`; `Not yet` takes a `revisit_on` date. |
| Cooked | You set this, plus `handoff_consent = Yes`. The next sync ships it. |
| Shipped | Written to the delivery log and the Delivery tab. Click `handoff_link` to send the student the intro from your phone. |
| Contacted … Visa granted / Enrolled / Lost | From the agency's Delivery tab. |
| Paid | You set this when the money arrives. |
| Opted out | Set automatically when a student replies STOP. |

```bash
python run.py sync         # every 10–15 min (cron): reads both tabs, ships Cooked leads
python run.py todo         # calls to log, revisits due, agency not contacting, money due
python run.py deliveries   # every shipped lead with timestamp + hash: your proof for invoicing
```
The delivery log is a SQLite table that refuses updates and deletes. Keep backups of `leads.db`.

### 6. Meta lead ads (optional; Facebook + Instagram Instant Forms)
1. Create a Meta app (Business type), add the **Webhooks** product.
2. Subscribe the **Page** object to `leadgen`. Callback: `https://your-host/webhook/meta`, verify token = `META_VERIFY_TOKEN`.
3. Generate a long-lived **Page access token** with `leads_retrieval`, `pages_manage_metadata`, `pages_show_list` → `META_PAGE_ACCESS_TOKEN`. Set `META_APP_SECRET` so payload signatures are checked.
4. Build the Instant Form with these question keys (or edit `META_FIELD_MAP` in `pipeline/sources.py`):

| Key | Type | Choices |
|---|---|---|
| `full_name`, `phone_number`, `email`, `city` | prefill | |
| `study_level` | multiple choice | Foundation · Bachelor's · Master's · MBA · PhD |
| `highest_qualification` | multiple choice | Matric/O Level · FSc/ICS/HSSC · A Levels · BA/BSc (14 yrs) · BS (16 yrs) · Master's/MPhil |
| `percentage_or_cgpa` | short answer | |
| `ielts_status` | short answer | "IELTS 6.5", "not yet" |
| `budget` | multiple choice | 30–50 lakh · 50–80 lakh · 80 lakh–1.2 crore · 1.2 crore+ |
| `preferred_intake` | multiple choice | Jan 2027 · May 2027 · Sep 2027 · Not sure |
| `do_you_have_a_passport?` | multiple choice | Yes · No |

Use **Higher intent** form type, and add a custom consent disclaimer that names WhatsApp contact.
Include the budget question; it is the single best filter for serious students.

### 7. WhatsApp (Cloud API)
1. In the same Meta app add **WhatsApp**, register your business number.
2. Subscribe the WhatsApp Business Account webhook to `messages` (same callback URL).
3. Set `WHATSAPP_TOKEN` (system user permanent token) and `WHATSAPP_PHONE_NUMBER_ID`.

When a new student messages you, the pipeline pulls whatever it can from the text
("MS in UK sept 2027, ielts 6.5, budget 1.2 crore, Lahore" fills five fields), and if
the lead is under 60% complete it replies with the quiz link prefilled with their number.

Alerts to you and the agency are free-form messages, which only deliver inside a 24-hour
window. For reliable alerts, register utility templates and switch `notify` in
`pipeline/sinks.py` to send them. Replying STOP opts a student out.

### 8. Daily rescore
Intakes get closer every day, so scores change. Run once a day (cron / host scheduler):
```bash
python run.py rescore      # sync, re-enrich + re-score, alert on new Hot leads, print the to-do list
```
Old lists from expos or past inquiries: `python run.py import file.csv` (column names =
Lead fields; see `samples/sample_leads.csv`).

## Deploy to AWS for $0

Runs entirely on AWS **Always Free** services: Lambda (+ a public Function URL), DynamoDB
(provisioned, 25 RCU / 25 WCU in total), EventBridge Scheduler, SSM Parameter Store and CloudWatch
Logs with 14-day retention. No EC2, RDS, NAT gateway, load balancer, API Gateway or S3.

```bash
.venv/bin/aws configure                         # once: access key of an IAM user + your region
.venv/bin/python deploy/aws_deploy.py           # creates/updates everything; prints the API URL
.venv/bin/python deploy/aws_deploy.py --code    # later: ship code changes only
```
The script reads settings from `.env`, stores `service_account.json` encrypted in SSM, and
writes `deploy/site/index.html` (the quiz with the live API URL filled in) for Netlify Drop.
Point the Cal.com webhook at `<API URL>webhook/booking`.

Some new AWS accounts (the "Free plan" ones inside an AWS-managed organization) may only use the
region they were created in; an `explicit deny in a service control policy` error means use that
region. This deployment runs in `ap-southeast-2` (Sydney) for that reason.

Schedules: `sync` every 10 minutes, `rescore` daily at 06:00 PKT. Logs:
`.venv/bin/aws logs tail /aws/lambda/enrolliq-api --follow`.

Keep a **zero-spend budget** (Billing → Budgets → "Zero spend budget") so AWS emails you if
anything ever costs a cent. After the 6-month free plan ends, upgrade to the paid plan: these
services stay free within their monthly limits.

## Compliance

Every lead here has opted in: quiz consent checkbox (which also covers sharing details with
the partner consultancy), Meta form disclaimer, or the student messaging you first. Nothing
ships without `handoff_consent = Yes` from the call. Keep it that way. Don't load scraped Facebook-group numbers, purchased
lists or "student databases"; they burn your WhatsApp number's quality rating, breach Meta
terms, and conflict with Pakistan's PECA and personal-data rules. Leads without consent are
held with "Hold: get consent" and never alerted. STOP replies set the stage to "Opted out"
automatically.

## Keep current
UK figures (checked Oct 2026): visa £558, IHS £776/yr, maintenance £1,171/£1,529 per month
rising to £1,203/£1,570 for applications from 30 Nov 2026, Graduate Route 18 months for
applications from 1 Jan 2027. Check https://www.gov.uk/student-visa/money each cycle and
update `config.py`. Tuition floors are deliberately low-end; set them to your partner
universities' real fees.
