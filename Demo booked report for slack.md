# Demo Report Automation — Internal Technical Brief
*For Jay Srinivasan | March 2026*

---

## What this is, in one sentence

An automated analytics pipeline that pulls demo booking data from HubSpot, cross-references Fireflies call recordings and Instantly campaign data, and posts a structured weekly and monthly report to Slack — showing who showed up, who didn't, where they came from, and what happened to the deal.

---

## The problem it solves

Every demo that gets booked creates a question: what actually happened? Did they show? Was there a Fireflies recording? Is there a deal open? What campaign did they come from?

Answering that manually means jumping between HubSpot (contact + meeting + deal), Fireflies (did the call happen?), and Instantly (which campaign was this from?). For a handful of demos a week it's annoying. At scale — multiple outbound campaigns running, inbound traffic from multiple sources, 5+ reps booking demos — it becomes a real operational blind spot.

The report answers all of those questions automatically, every week and every month, delivered to Slack where the team is already working.

---

## What was built

### The data pipeline

Six HubSpot APIs, one Instantly API, and the Fireflies API — all coordinated into a single coherent picture per demo.

```
HubSpot Meetings API      ─┐
HubSpot Contacts API      ─┤
HubSpot Notes API         ─┤──→ report.py ──→ Slack #demo-booked
HubSpot Deals API         ─┤
HubSpot Associations API  ─┤
HubSpot Scheduler API     ─┤
Instantly Campaigns API   ─┤
Fireflies GraphQL API     ─┘
```

For each reporting period, the pipeline:

1. **Fetches all demo bookings** from HubSpot's meetings API — filtered to `MEETINGS_PUBLIC` (calendar-booked meetings only, ignoring CRM-internal duplicates HubSpot creates for the same meeting)

2. **Loads contact data** for every booker — name, email, company, lead source, Instantly campaign field — batch-fetched 100 at a time via the associations API

3. **Detects whether the meeting happened and enriches with call data** — three-phase lookup:
   - Phase 0 (primary): Query Fireflies API directly for all transcripts in the period. Match by participant email. Captures duration, action item count, and keywords for each matched contact
   - Phase 1 (fallback): For contacts not found in Fireflies, check HubSpot meeting bodies for Fireflies links
   - Phase 2 (fallback): For contacts still unresolved, check associated contact notes
   - Any match → outcome = "showed". The Fireflies API result additionally tells you how long the call ran and what was discussed

4. **Classifies the source** — inbound or outbound, and if outbound, which campaign:
   - Checks which HubSpot booking link was used (requires the scheduler API to map link IDs to "Inbound" vs "Outbound")
   - For outbound contacts, queries Instantly's leads API by email to find the most recent campaign
   - Falls back to HubSpot's `hs_analytics_source` for inbound classification

5. **Looks up deal stage** — not from the contact-level property (which lags), but from the actual deal object:
   - Uses the v4 associations API to find deal IDs per contact
   - Batch-reads deal objects with current stage + last-modified timestamp
   - For contacts with multiple deals, picks the most recently modified one

6. **Deduplicates** — if a contact booked multiple demos in the period, only the latest appears

7. **Formats and posts to Slack** — two report formats (weekly and monthly), both designed to be scanned in 30 seconds

### The two report formats

**Weekly** — granular, per-contact:
- Total demos, show rate, inbound/outbound split
- ⚠️ Alert for contacts who showed but have no deal opened — one line per person with call duration and keywords (from Fireflies)
- Per-contact rows: name, company, booking date, source, deal stage — showed rows include call duration and action item count
- Outbound section grouped by campaign, with bookings and show rate per campaign
- All active campaigns listed (including ones with 0 bookings that period — so nothing is invisible)

**Monthly** — aggregate, trend-focused:
- Overall totals and show rate
- Week-by-week breakdown
- Pipeline visualization grouped by stage (Closed Won → In Pilot → Demo Completed → Closed Lost) with company names
- Inbound vs outbound conversion rates
- Campaign performance across the full month

### Scheduling

Runs automatically on GitHub Actions:
- **Weekly**: Every Monday at 9 AM IST
- **Monthly**: 1st of every month at 9 AM PST

No server, no infrastructure. Manual trigger available via GitHub Actions UI with a dry-run toggle — lets anyone preview the report without posting to Slack.

---

## What it looks like in practice

**Weekly report → #demo-booked:**

```
📅 *Demo Report — Week of Mar 3 – Mar 9, 2026*
*12 demos booked*   ·   🟢 *3* in active pipeline

✅ *8* showed   ❌ *2* no-show   🚫 *1* cancelled   ⏳ *1* upcoming
Show rate *80%*   ·   🔵 Inbound *6*   🟠 Outbound *6*

⚠️ *Showed — no deal opened yet (2):*
   • *John Smith*  —  TechCorp Inc   43m   ·   pricing, security, timeline
   • *Jane Doe*  —  StartupX   28m

──────────────────────────────────────

🔵 *Inbound (6)*   ·   5/6 showed   ·   2 deals  40% of showed   ·   🟢 1 in pipeline

✅  *Alice Johnson*  —  TechCorp Inc   Mar 5
     Organic Search   ·   Demo Completed   ·   43m   ·   5 action items
✅  *Bob Chen*  —  StartupX   Mar 7
     Direct   ·   In Pilot   ·   31m   ·   3 action items

🟠 *Outbound (6)*   ·   3/6 showed   ·   1 deal  33% of showed   ·   🟢 2 in pipeline

   📣 *Cold Email Q1*   6 booked · 3 showed
   📣 *LinkedIn Outreach*   0 bookings this period

✅  *Carlos Martinez*  —  Enterprise Corp   Mar 4
     Cold Email Q1   ·   Interested In Pilot   ·   52m   ·   7 action items
❌  *Diana Patel*  —  MidMarket Inc   Mar 6
     LinkedIn Outreach   ·   No deal yet
```

**Monthly report → #demo-booked:**

```
📊 *Monthly Demo Report — March 2026*
*47 demos booked*   ·   🟢 *7* in active pipeline

✅ *32* showed   ❌ *10* no-show   🚫 *4* cancelled   |   Show rate *76%*

🔵 *Inbound*   23 booked   ·   18 showed   ·   5 deals  28% → deal   ·   🟢 3 in pipeline
🟠 *Outbound*   24 booked   ·   14 showed   ·   4 deals  29% → deal   ·   🟢 4 in pipeline

   📣 *Cold Email Q1*   12 booked · 8 showed
   📣 *LinkedIn Outreach*   9 booked · 4 showed
   📣 *Intent Marketing*   0 bookings this period

*Week by week:*
  Mar 3    8 booked · ✅ 6  ❌ 1  🚫 1
  Mar 10   12 booked · ✅ 9  ❌ 2  🚫 1
  Mar 17   15 booked · ✅ 11  ❌ 3  🚫 1
  Mar 24   12 booked · ✅ 6  ❌ 4  🚫 2

*Pipeline:*
  🏆 Closed Won (2)      TechCorp Inc, BigCorp Ltd
  🟢 In Pilot (3)        StartupX, MidMarket Inc, Enterprise Corp
  🟡 Demo Completed      18
  🔴 Closed Lost (1)     SmallCorp

⚠️ *Showed — no deal opened (5):*
   • *Prospect A*  —  Company A   38m   ·   integration, pricing, ROI
   • *Prospect B*  —  Company B   22m
   • *Prospect C*  —  Company C   45m   ·   security, compliance, timeline
   • *Prospect D*  —  Company D
   • *Prospect E*  —  Company E   19m
```

---

## What's technically non-trivial

**1. Multi-source data assembly**
Six HubSpot APIs and Instantly — each with its own pagination, auth, and response shape — assembled into one coherent row per contact. All batch-fetched to minimise API roundtrips and stay within rate limits.

**2. Three-phase Fireflies detection with enrichment**
HubSpot doesn't have a "call recording" field. The script runs three phases in order: (0) query Fireflies' own GraphQL API for all transcripts in the date range, matching by participant email — this is the most reliable signal and also returns call duration, action items, and keywords; (1) for contacts not matched in the API, check HubSpot meeting bodies for Fireflies links; (2) for still-unresolved contacts, check associated contact notes. One date range, three API surfaces, one outcome signal — plus enrichment data on every match from Phase 0.

A subtle edge case: demos are filtered by booking date, but Fireflies transcripts exist on meeting date (which can be days later). The script queries Fireflies from the start of the booking period to today — so a meeting booked in week X but held in week X+1 is still matched correctly.

**3. Deal stage from actual deal objects, not contact properties**
HubSpot's contact-level deal stage field lags. The script uses the v4 associations API to find real deal IDs per contact, batch-reads the deal objects directly, and for contacts with multiple deals picks the most recently modified one. The pipeline stage labels are also fetched live from the pipelines API rather than hardcoded.

**4. Source classification via live Instantly lookup**
Determining which campaign an outbound contact came from isn't as simple as reading a field. The script queries Instantly's leads API by email to find the contact's most recent campaign, sorted by `timestamp_last_contact`. This gives the currently active campaign, not whatever was last written to a custom HubSpot field.

**5. HubSpot meeting deduplication**
HubSpot creates two meeting records per calendar booking: a `MEETINGS_PUBLIC` record and a `CRM_UI` record. The script filters to `MEETINGS_PUBLIC` only to avoid counting every demo twice.

**6. "No deal opened" alert**
The ⚠️ flag for contacts who showed but have no deal is a join across three data sources: the meeting (did they attend, per Fireflies), the contact (are they associated to a deal?), and the deal (what stage is it?). It's the most actionable signal in the report — contacts that slipped through the follow-up net.

---

## Current setbacks

**1. Fireflies detection can still miss meetings**
The direct Fireflies API query catches significantly more meetings than the old HubSpot scrape (which depended on Fireflies writing a link back into HubSpot). But if the Fireflies bot wasn't invited to the meeting, or was blocked by the prospect, no transcript exists at all — and the outcome still shows as "no-show" even if the call happened. There's no fallback beyond the Fireflies signal.

**2. Instantly campaign attribution is best-effort**
The script looks up each outbound contact in Instantly to find their most recent campaign. If a contact was enrolled in multiple campaigns or was manually added to HubSpot without going through Instantly, attribution can be wrong or missing.

**3. Deal stage freshness depends on HubSpot hygiene**
The report shows deal stage, but only if deals are being created and updated in HubSpot. If reps don't update deal stages (or don't create deals at all), "no deal yet" accumulates and loses meaning.

**4. No historical view**
The weekly and monthly reports are snapshots. There's no longitudinal tracking — no way to see show rate trend over 6 months, or campaign conversion rate over time. The data to build this exists (each run is self-contained) but it's not being stored anywhere.

**5. Slack-only output**
The report is formatted specifically for Slack plaintext. There's no shareable link, no spreadsheet export, no PDF. Fine for the team; limiting if Jay wants to show it to a board or investor.

---

## Where we can go from here

**1. Store run outputs**
Append each week/month's summary data to a CSV or database. Enables trend analysis, show rate over time, campaign-by-campaign conversion history. The pipeline already computes everything needed — it just doesn't persist it.

**2. Alerts for stale deals**
The report already identifies contacts with no deal opened. A follow-up alert (same day, or next morning) for reps to action those contacts directly would close the loop.

**3. Connect to the MCP pipeline**
The outreach project in progress — pulling leads associated with deals and drafting personalized notes — would benefit directly from this report's data. Contacts who showed but haven't converted are the highest-priority targets for personalized follow-up.

**4. Rep-level breakdowns**
Currently the report is company-wide. If HubSpot deals/contacts are assigned to reps, a per-rep view (who owns the most shows, whose deals are progressing) would add significant operational value.

**5. Shareable report format**
Generate a Notion page, Google Doc, or simple HTML alongside the Slack message — something that can be linked in a board update or shared with an investor without forwarding a Slack screenshot.

---

## Files and what they do

| File | Purpose |
|------|---------|
| `report.py` | Everything — data pipeline (HubSpot + Fireflies + Instantly API calls, data assembly, deduplication), weekly + monthly Slack formatters, and CLI entry point |
| `.github/workflows/reports.yml` | GitHub Actions: weekly cron (Monday 9 AM IST) + monthly cron (1st of month 9 AM PST), with manual trigger + dry-run toggle |

**Environment variables required:**

| Variable | What it is |
|----------|-----------|
| `HUBSPOT_TOKEN` | HubSpot private app token (read access: contacts, meetings, deals, notes, associations) |
| `INSTANTLY_API_KEY` | Instantly API key (read access: campaigns, leads) |
| `FIREFLIES_API_KEY` | Fireflies API key — used to fetch transcripts directly for show detection + call enrichment |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook for #demo-booked |

---

*Built by Gayathri, 2025–2026*
