# UK Student Lead Pipeline

Captures students who opt in through three channels, enriches each lead with
what a UK counselor actually needs (real first-year cost in PKR vs budget,
academic entry route, English readiness, realistic intake, visa red flags),
scores it 0–100, and writes it to Google Sheets. Hot leads trigger a WhatsApp
alert to counselors.

```
Eligibility quiz ─┐
WhatsApp chats ───┼─► normalize ─► dedupe/merge ─► enrich ─► score ─► Google Sheet
Meta lead ads ────┘                (by phone)                        └─► WhatsApp alert (Hot)
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
| `next_action`, `assigned_to` | what the counselor should do, routed by province (refusals → senior counselor) |

Tiers: **Hot** ≥70 (call within an hour), **Warm** ≥50, **Nurture** ≥30, **Cold**.
Hard caps: budget short → max 55; dependants on a taught course → max 60; prior refusal → max 65.

All tunables (visa figures, tuition floors, weights, thresholds, PKR rate) are in
`pipeline/config.py`.

## Setup (about an hour)

### 1. Run it
```bash
pip install -r requirements.txt
cp .env.example .env        # fill in, then export (or use your host's env settings)
python -m pytest -q tests    # 12 tests
uvicorn app:app --host 0.0.0.0 --port 8000
```
Host anywhere with HTTPS: Railway, Render, a small VPS. Meta webhooks require HTTPS.
Without Google credentials the pipeline writes `leads_export.csv` instead, so you can try it immediately.

### 2. Google Sheets
1. Google Cloud console → create project → enable **Google Sheets API** and **Google Drive API**.
2. Create a **service account**, download its JSON key as `service_account.json`.
3. Create a sheet, share it (Editor) with the service account's email.
4. Put the sheet ID from its URL into `GOOGLE_SHEET_ID`.

The header row is created automatically. Counselors own the `status` and `assigned_to`
columns; the pipeline never overwrites them once filled. Add a filter view on
`tier = Hot` for the morning call list.

### 3. Landing page (eligibility quiz)
`landing/uk-eligibility.html`: set `PIPELINE_URL` to `https://your-host/webhook/quiz`,
`QUIZ_KEY` to match `QUIZ_SHARED_KEY`, and your agency name. Host it on your website.
UTM tags (`?utm_source=fb&utm_campaign=jan27`) are captured automatically.
Students see their result instantly; that instant answer is why they give you a real number.

### 4. Meta lead ads (Facebook + Instagram Instant Forms)
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

### 5. WhatsApp (Cloud API)
1. In the same Meta app add **WhatsApp**, register your business number.
2. Subscribe the WhatsApp Business Account webhook to `messages` (same callback URL).
3. Set `WHATSAPP_TOKEN` (system user permanent token) and `WHATSAPP_PHONE_NUMBER_ID`.

When a new student messages you, the pipeline pulls whatever it can from the text
("MS in UK sept 2027, ielts 6.5, budget 1.2 crore, Lahore" fills five fields), and if
the lead is under 60% complete it replies with the quiz link prefilled with their number.

Counselor alerts: free-form messages only deliver inside a 24-hour window. For reliable
alerts, register a utility template (e.g. `new_hot_lead`) and switch `alert_counselors`
in `pipeline/sinks.py` to send it.

### 6. Daily rescore
Intakes get closer every day, so scores change. Run once a day (cron / host scheduler):
```bash
python run.py rescore      # re-enrich + re-score; alerts on leads that just turned Hot
```
Old lists from expos or past inquiries: `python run.py import file.csv` (column names =
Lead fields; see `samples/sample_leads.csv`).

## Compliance

Every lead here has opted in: quiz consent checkbox, Meta form disclaimer, or the student
messaging you first. Keep it that way. Don't load scraped Facebook-group numbers, purchased
lists or "student databases"; they burn your WhatsApp number's quality rating, breach Meta
terms, and conflict with Pakistan's PECA and personal-data rules. Leads without consent are
held with "Hold: get consent" and never alerted. Honour STOP replies by setting status to
"Opted out".

## Keep current
UK figures (checked Oct 2026): visa £558, IHS £776/yr, maintenance £1,171/£1,529 per month
rising to £1,203/£1,570 for applications from 30 Nov 2026, Graduate Route 18 months for
applications from 1 Jan 2027. Check https://www.gov.uk/student-visa/money each cycle and
update `config.py`. Tuition floors are deliberately low-end; set them to your partner
universities' real fees.
