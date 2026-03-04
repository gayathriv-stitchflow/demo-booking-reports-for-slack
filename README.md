# Demo Booking Reports for Slack

Automated weekly and monthly demo booking summaries posted to Slack. Pulls data from HubSpot, detects meeting outcomes via Fireflies.ai, classifies lead source via Instantly, and shows real deal stages from HubSpot CRM.

## What it does

- **Weekly report** — every Monday at 9 AM IST. Covers demos booked the previous Mon–Sun. Shows each contact with outcome, source, deal stage, and active pipeline count.
- **Monthly report** — 1st of every month at 9 AM PST. High-level summary: show rate, channel breakdown, week-by-week counts, and pipeline grouped by deal stage.

Both post to the `#demo-booked` Slack channel.

## How outcomes are detected

| Outcome | Logic |
|---------|-------|
| Cancelled | Meeting title starts with "Canceled:" |
| Upcoming | Meeting start time is in the future |
| Showed | Fireflies.ai link found in meeting body or contact notes |
| No-show | Past meeting, no Fireflies link |

## How source is classified

- **Outbound** — `instantly_campaign_name__all_` field is populated in HubSpot. Campaign name is fetched from the Instantly API (latest campaign by last contact date).
- **Inbound** — no Instantly campaign. Source label derived from `hs_analytics_source` (Organic Search, Direct, etc.).

## Deal stages

Deal stages are pulled from the **actual associated deal objects** (not the contact-level `deal_stage` property, which can lag). For contacts with multiple deals, the most recently modified deal wins.

Active pipeline = deals that have moved past "Demo Completed" but are not yet Closed Won or Closed Lost.

## Setup

### 1. GitHub Actions secrets

Add these three secrets to the repo (`Settings → Secrets and variables → Actions`):

| Secret | What it is |
|--------|------------|
| `HUBSPOT_TOKEN` | HubSpot private app token |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook URL |
| `INSTANTLY_API_KEY` | Instantly API key |

### 2. HubSpot private app scopes required

- CRM → Contacts → Read
- CRM → Meetings → Read (under Engagements)
- CRM → Notes → Read (under Engagements)
- CRM → Associations → Read
- CRM → Deals → Read

### 3. Run locally

```bash
export HUBSPOT_TOKEN=pat-na2-...
export SLACK_WEBHOOK_URL=https://hooks.slack.com/...
export INSTANTLY_API_KEY=...

python report.py weekly --dry-run    # preview weekly, no Slack post
python report.py monthly --dry-run   # preview monthly, no Slack post

python report.py weekly              # post weekly to Slack
python report.py monthly             # post monthly to Slack
```

## Manual trigger

Go to **Actions → Demo Reports → Run workflow** to trigger either report on demand, with an optional dry-run toggle.

## Schedule

| Report | Cron | Time |
|--------|------|------|
| Weekly | `30 3 * * 1` | Monday 9:00 AM IST |
| Monthly | `0 17 1 * *` | 1st of month 9:00 AM PST |
