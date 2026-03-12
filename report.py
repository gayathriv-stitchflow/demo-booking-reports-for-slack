#!/usr/bin/env python3
"""
HubSpot Demo Report — weekly and monthly Slack summaries.

Usage:
    python report.py weekly           # last Mon–Sun, posts to Slack
    python report.py monthly          # last calendar month, posts to Slack
    python report.py weekly --dry-run # prints to stdout only
"""

import os, sys, re, argparse
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

HUBSPOT_TOKEN    = os.environ["HUBSPOT_TOKEN"]
SLACK_WEBHOOK    = os.environ.get("SLACK_WEBHOOK_URL", "")
INSTANTLY_KEY    = os.environ.get("INSTANTLY_API_KEY", "")
FIREFLIES_KEY    = os.environ.get("FIREFLIES_API_KEY", "")
BASE_URL         = "https://api.hubapi.com"
INSTANTLY_BASE   = "https://api.instantly.ai/api/v2"
FIREFLIES_GQL    = "https://api.fireflies.ai/graphql"
INTERNAL_DOMAIN  = "@stitchflow.io"
HS               = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
INST_H           = {"Authorization": f"Bearer {INSTANTLY_KEY}", "Content-Type": "application/json"}

CONTACT_PROPS = [
    "firstname", "lastname", "email", "company",
    "hs_lead_status",
    "hs_analytics_source",
    "instantly_campaign_name__all_",
]

MEETING_PROPS = [
    "hs_meeting_title", "hs_meeting_start_time",
    "hs_meeting_source", "hs_meeting_body", "hs_createdate",
    "hs_meeting_created_from_link_id",
]

FIREFLIES_RE = re.compile(r'https://app\.fireflies\.ai/view/[^\s"<>]+')

NOW_MS = datetime.now(timezone.utc).timestamp() * 1000


def parse_hs_time(value):
    """
    HubSpot returns timestamps in two formats depending on the property:
      - Epoch milliseconds (string): "1706572800000"
      - ISO 8601 string:             "2026-02-26T18:15:00Z"
    Returns epoch milliseconds as a float, or 0 if unparseable.
    """
    if not value:
        return 0.0
    try:
        return float(value)          # epoch ms string
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() * 1000
    except Exception:
        return 0.0

# ── HubSpot API helpers ────────────────────────────────────────────────────

def hs_get(path, params=None):
    r = requests.get(f"{BASE_URL}{path}", headers=HS, params=params or {})
    r.raise_for_status()
    return r.json()


def hs_post(path, body):
    r = requests.post(f"{BASE_URL}{path}", headers=HS, json=body)
    r.raise_for_status()
    return r.json()


def hs_search_all(obj_type, filters, props):
    """Paginated CRM search — returns all results."""
    results, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": filters}],
            "properties": props,
            "limit": 100,
        }
        if after:
            body["after"] = after
        data = hs_post(f"/crm/v3/objects/{obj_type}/search", body)
        results.extend(data.get("results", []))
        if "next" in data.get("paging", {}):
            after = data["paging"]["next"]["after"]
        else:
            break
    return results


def hs_batch_read(obj_type, ids, props):
    """Batch-read up to N objects, 100 at a time."""
    results = []
    for i in range(0, len(ids), 100):
        data = hs_post(f"/crm/v3/objects/{obj_type}/batch/read", {
            "inputs": [{"id": x} for x in ids[i:i+100]],
            "properties": props,
        })
        results.extend(data.get("results", []))
    return results


def hs_batch_assoc(from_type, from_ids, to_type):
    """
    Batch-fetch associations for multiple objects at once (v4 API).
    Returns dict: {from_id -> [to_id, ...]}
    """
    result = defaultdict(list)
    for i in range(0, len(from_ids), 100):
        batch = from_ids[i:i+100]
        data = hs_post(f"/crm/v4/associations/{from_type}/{to_type}/batch/read", {
            "inputs": [{"id": str(fid)} for fid in batch]
        })
        for item in data.get("results", []):
            fid = str(item.get("from", {}).get("id", ""))
            to_ids = [str(r["toObjectId"]) for r in item.get("to", [])]
            result[fid].extend(to_ids)
    return result

# ── Meeting link map ───────────────────────────────────────────────────────

def fetch_meeting_link_map():
    """
    Returns {link_id -> 'Inbound'|'Outbound'} by reading all HubSpot booking pages.
    Links whose name contains 'outbound' → Outbound; everything else → Inbound.
    """
    link_map = {}
    try:
        data = hs_get("/scheduler/v3/meetings/meeting-links")
        for link in data.get("results", []):
            lid  = str(link.get("id", ""))
            name = (link.get("name") or link.get("slug") or "").lower()
            link_map[lid] = "Outbound" if "outbound" in name else "Inbound"
    except Exception:
        pass
    return link_map


# ── Instantly API helpers ──────────────────────────────────────────────────

_campaign_name_cache = {}

def instantly_campaign_name(campaign_id):
    """Look up a campaign name by ID, with local cache to avoid repeat calls."""
    if campaign_id not in _campaign_name_cache:
        try:
            r = requests.get(f"{INSTANTLY_BASE}/campaigns/{campaign_id}", headers=INST_H)
            r.raise_for_status()
            _campaign_name_cache[campaign_id] = r.json().get("name") or campaign_id
        except Exception:
            _campaign_name_cache[campaign_id] = None
    return _campaign_name_cache[campaign_id]


def fetch_active_instantly_campaigns():
    """
    Returns list of names of currently Active campaigns (status=1) in Instantly.
    """
    if not INSTANTLY_KEY:
        return []
    names = []
    try:
        starting_after = None
        while True:
            params = {"limit": 100}
            if starting_after:
                params["starting_after"] = starting_after
            r = requests.get(f"{INSTANTLY_BASE}/campaigns", headers=INST_H, params=params)
            r.raise_for_status()
            data = r.json()
            for c in data.get("items", []):
                if c.get("status") == 1:
                    names.append(c["name"])
            if not data.get("next_starting_after"):
                break
            starting_after = data["next_starting_after"]
    except Exception:
        pass
    return names


def preload_instantly_campaigns(contact_email_map):
    """
    For outbound contacts, fetch their most recently active Instantly campaign.

    contact_email_map: {contact_id -> email}
    Returns:           {contact_id -> campaign_name | None}
    """
    if not INSTANTLY_KEY:
        return {}

    result = {}
    for cid, email in contact_email_map.items():
        if not email:
            result[cid] = None
            continue
        try:
            r = requests.post(
                f"{INSTANTLY_BASE}/leads/list",
                headers=INST_H,
                json={"search": email, "limit": 20},
            )
            r.raise_for_status()
            leads = r.json().get("items", [])
            if not leads:
                result[cid] = None
                continue
            # Most recent by timestamp_last_contact
            leads.sort(key=lambda l: l.get("timestamp_last_contact") or "", reverse=True)
            campaign_id = leads[0].get("campaign")
            result[cid] = instantly_campaign_name(campaign_id) if campaign_id else None
        except Exception:
            result[cid] = None
    return result


# ── Fireflies detection ────────────────────────────────────────────────────

def fetch_fireflies_transcripts(start_ms, end_ms):
    """
    Query Fireflies API for all transcripts in the reporting date range.
    Returns a list of raw transcript dicts from the GraphQL response.
    """
    if not FIREFLIES_KEY:
        return []
    from_date = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    to_date   = datetime.fromtimestamp(end_ms   / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    query = """
    {
      transcripts(fromDate: "%s", toDate: "%s", limit: 50) {
        id
        title
        date
        duration
        participants
        summary { overview action_items keywords }
      }
    }
    """ % (from_date, to_date)
    try:
        r = requests.post(
            FIREFLIES_GQL,
            headers={"Authorization": f"Bearer {FIREFLIES_KEY}", "Content-Type": "application/json"},
            json={"query": query},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("data", {}).get("transcripts") or []
    except Exception as e:
        print(f"  Fireflies API error: {e}")
        return []


def preload_fireflies(contact_ids, contact_email_map, start_ms, end_ms):
    """
    For a list of contact IDs, determine which ones have a Fireflies recording
    and enrich with duration, action items, and keywords.

    Strategy:
      Phase 0 — Fireflies API (direct): match transcripts by participant email.
      Phase 1 — HubSpot meeting bodies (fallback for unresolved contacts).
      Phase 2 — HubSpot contact notes (fallback for still-unresolved contacts).

    contact_email_map: {contact_id -> email}
    Returns: {contact_id -> {found, url, duration, action_items_count, keywords, overview}}
    """
    _blank = lambda: {"found": False, "url": None, "duration": None,
                      "action_items_count": 0, "keywords": [], "overview": None}
    ff_map    = {cid: _blank() for cid in contact_ids}
    remaining = list(contact_ids)

    # ── Phase 0: Fireflies API ───────────────────────────────────────────
    print(f"  Querying Fireflies API ...")
    transcripts = fetch_fireflies_transcripts(start_ms, end_ms)
    if transcripts:
        email_to_cid = {v.lower(): k for k, v in contact_email_map.items() if v}
        for t in transcripts:
            external_emails = [
                p.lower()
                for p in (t.get("participants") or [])
                if p and INTERNAL_DOMAIN not in p.lower()
            ]
            for email in external_emails:
                cid = email_to_cid.get(email)
                if cid and cid in remaining:
                    summary      = t.get("summary") or {}
                    ai_raw       = summary.get("action_items") or ""
                    ai_count     = len([l for l in ai_raw.splitlines() if l.strip()]) if ai_raw else 0
                    kw_raw       = summary.get("keywords") or []
                    keywords     = kw_raw if isinstance(kw_raw, list) else [k.strip() for k in kw_raw.split(",") if k.strip()]
                    ff_map[cid]  = {
                        "found":              True,
                        "url":                None,
                        "duration":           t.get("duration"),
                        "action_items_count": ai_count,
                        "keywords":           keywords[:5],
                        "overview":           summary.get("overview"),
                    }
                    remaining.remove(cid)
                    break  # one transcript per contact is enough

    # ── Phase 1: HubSpot meeting bodies (fallback) ───────────────────────
    if remaining:
        contact_to_meeting_ids = hs_batch_assoc("contacts", remaining, "meetings")
        all_meeting_ids = list({mid for mids in contact_to_meeting_ids.values() for mid in mids})
        if all_meeting_ids:
            meetings    = hs_batch_read("meetings", all_meeting_ids, ["hs_meeting_body"])
            body_by_mid = {m["id"]: m.get("properties", {}).get("hs_meeting_body") or "" for m in meetings}
            for cid in remaining[:]:
                for mid in contact_to_meeting_ids.get(cid, []):
                    body = body_by_mid.get(mid, "")
                    if "fireflies.ai" in body:
                        match = FIREFLIES_RE.search(body)
                        ff_map[cid] = {**_blank(), "found": True, "url": match.group(0) if match else None}
                        remaining.remove(cid)
                        break

    # ── Phase 2: contact notes (fallback for still-unresolved contacts) ──
    if remaining:
        contact_to_note_ids = hs_batch_assoc("contacts", remaining, "notes")
        all_note_ids = list({nid for nids in contact_to_note_ids.values() for nid in nids})
        if all_note_ids:
            notes       = hs_batch_read("notes", all_note_ids, ["hs_note_body"])
            body_by_nid = {n["id"]: n.get("properties", {}).get("hs_note_body") or "" for n in notes}
            for cid in remaining:
                for nid in contact_to_note_ids.get(cid, []):
                    body = body_by_nid.get(nid, "")
                    if "fireflies.ai" in body:
                        match = FIREFLIES_RE.search(body)
                        ff_map[cid] = {**_blank(), "found": True, "url": match.group(0) if match else None}
                        break

    return ff_map

# ── Deal lookup ────────────────────────────────────────────────────────────

def fetch_pipeline_stages():
    """Returns {stage_id: stage_label} across all deal pipelines."""
    stage_map = {}
    try:
        data = hs_get("/crm/v3/pipelines/deals")
        for pipeline in data.get("results", []):
            for stage in pipeline.get("stages", []):
                stage_map[stage["id"]] = stage["label"]
    except Exception:
        pass
    return stage_map


def fetch_contact_deal_stages(contact_ids, stage_map):
    """
    For each contact, find their associated deals and return the stage label
    of the most recently modified deal.

    Returns: {contact_id -> (stage_label: str, has_deal: bool)}
    """
    result = {cid: ("", False) for cid in contact_ids}

    contact_to_deal_ids = hs_batch_assoc("contacts", list(contact_ids), "deals")
    all_deal_ids = list({did for dids in contact_to_deal_ids.values() for did in dids})
    if not all_deal_ids:
        return result

    deals = hs_batch_read("deals", all_deal_ids, ["dealstage", "hs_lastmodifieddate"])
    deal_map = {d["id"]: d.get("properties", {}) for d in deals}

    for cid in contact_ids:
        deal_ids = contact_to_deal_ids.get(cid, [])
        if not deal_ids:
            continue
        # Pick the most recently modified deal
        def deal_sort_key(did):
            return deal_map.get(did, {}).get("hs_lastmodifieddate") or ""
        best_id = max(deal_ids, key=deal_sort_key)
        stage_id = deal_map.get(best_id, {}).get("dealstage") or ""
        stage_label = stage_map.get(stage_id, stage_id)
        result[cid] = (stage_label, True)

    return result


# ── Outcome & source logic ─────────────────────────────────────────────────

def get_outcome(meeting_props, contact_id, ff_map):
    title    = meeting_props.get("hs_meeting_title") or ""
    start_ts = parse_hs_time(meeting_props.get("hs_meeting_start_time"))

    if title.lower().startswith("canceled"):
        return "cancelled", None
    if start_ts > NOW_MS:
        return "upcoming", None

    ff_data = ff_map.get(contact_id, {})
    if ff_data.get("found"):
        return "showed", ff_data.get("url")

    if 0 < start_ts < NOW_MS:
        return "noshow", None

    return "unknown", None


SRC_LABELS = {
    "ORGANIC_SEARCH": "Organic Search",
    "DIRECT_TRAFFIC":  "Direct",
    "REFERRALS":        "Referral",
    "OTHER_CAMPAIGNS":  "Campaign",
    "PAID_SEARCH":      "Paid Search",
    "SOCIAL_MEDIA":     "Social",
    "AI_REFERRALS":     "AI Referral",
    "OFFLINE":          "Offline",
}

def classify_source(props, link_channel=None, instantly_campaign=None):
    # 1. Calendar link is the most direct signal
    if link_channel == "Outbound":
        name = (instantly_campaign
                or (props.get("instantly_campaign_name__all_") or "").split(";")[-1].strip()
                or "Outbound")
        return "Outbound", name
    if link_channel == "Inbound":
        src = props.get("hs_analytics_source") or ""
        return "Inbound", SRC_LABELS.get(src, src or "Unknown")
    # 2. Fall back to Instantly / HubSpot property (link ID unknown)
    campaign = (props.get("instantly_campaign_name__all_") or "").strip()
    if campaign:
        name = instantly_campaign or campaign.split(";")[-1].strip()
        return "Outbound", name
    src = props.get("hs_analytics_source") or ""
    return "Inbound", SRC_LABELS.get(src, src or "Unknown")

# ── Data fetching ──────────────────────────────────────────────────────────

def fmt_ts(ms):
    if not ms:
        return "?"
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%b %-d")


def fetch_demos(start_ms, end_ms):
    """
    Pull all calendar-booked (MEETINGS_PUBLIC) demos in the given epoch-ms range.
    Returns a list of row dicts ready for reporting.
    """
    link_map = fetch_meeting_link_map()

    print(f"  Fetching meetings booked {fmt_ts(start_ms)} → {fmt_ts(end_ms)} ...")
    meetings = hs_search_all("meetings", [
        {"propertyName": "hs_meeting_source",  "operator": "EQ",  "value": "MEETINGS_PUBLIC"},
        {"propertyName": "hs_createdate",       "operator": "GTE", "value": str(start_ms)},
        {"propertyName": "hs_createdate",       "operator": "LTE", "value": str(end_ms)},
    ], MEETING_PROPS)
    print(f"  {len(meetings)} meetings found")

    # Pre-compute link channel per meeting
    link_channel_by_mtg = {}
    for m in meetings:
        lid = str(m.get("properties", {}).get("hs_meeting_created_from_link_id") or "")
        link_channel_by_mtg[m["id"]] = link_map.get(lid)  # None if link ID unknown

    if not meetings:
        return []

    # ── Contacts ─────────────────────────────────────────────────────────
    meeting_ids = [m["id"] for m in meetings]
    mtg_to_contact = hs_batch_assoc("meetings", meeting_ids, "contacts")

    contact_ids = list({cids[0] for cids in mtg_to_contact.values() if cids})
    contacts_raw = hs_batch_read("contacts", contact_ids, CONTACT_PROPS)
    contact_map  = {c["id"]: c.get("properties", {}) for c in contacts_raw}

    # ── Fireflies ─────────────────────────────────────────────────────────
    contact_email_map = {cid: (contact_map.get(cid) or {}).get("email", "") for cid in contact_ids}
    print(f"  Checking Fireflies for {len(contact_ids)} contacts ...")
    ff_map = preload_fireflies(contact_ids, contact_email_map, start_ms, max(end_ms, NOW_MS))

    # ── Deals (from actual deal objects, not contact property) ────────────
    print(f"  Fetching deals for {len(contact_ids)} contacts ...")
    stage_map   = fetch_pipeline_stages()
    deal_stages = fetch_contact_deal_stages(contact_ids, stage_map)

    # ── Instantly campaigns (outbound contacts only) ───────────────────────
    # Outbound = booked via outbound calendar link OR has instantly_campaign_name__all_ set
    outbound_contact_ids = set()
    for m in meetings:
        cid = (mtg_to_contact.get(m["id"]) or [None])[0]
        if not cid:
            continue
        lc = link_channel_by_mtg.get(m["id"])
        if lc == "Outbound" or (contact_map.get(cid) or {}).get("instantly_campaign_name__all_"):
            outbound_contact_ids.add(cid)
    outbound_email_map = {
        cid: contact_map[cid].get("email", "")
        for cid in outbound_contact_ids
        if contact_map.get(cid)
    }
    instantly_map = {}
    if outbound_email_map:
        print(f"  Fetching Instantly campaigns for {len(outbound_email_map)} outbound contacts ...")
        instantly_map = preload_instantly_campaigns(outbound_email_map)

    print(f"  Fetching active Instantly campaigns ...")
    active_campaigns = fetch_active_instantly_campaigns()

    # ── Build rows ────────────────────────────────────────────────────────
    rows = []
    for m in meetings:
        mp  = m.get("properties", {})
        cid = (mtg_to_contact.get(m["id"]) or [None])[0]
        props = contact_map.get(cid, {}) if cid else {}

        outcome, ff_url     = get_outcome(mp, cid, ff_map)
        link_channel        = link_channel_by_mtg.get(m["id"])
        channel, src_detail = classify_source(props, link_channel, instantly_map.get(cid))
        deal_stage, has_deal = deal_stages.get(cid, ("", False)) if cid else ("", False)
        ff_data              = ff_map.get(cid, {}) if cid else {}

        name = f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip()

        rows.append({
            "id":                   m["id"],
            "contact_id":           cid or "",
            "name":                 name or "Unknown",
            "company":              props.get("company") or "—",
            "meeting_date":         fmt_ts(parse_hs_time(mp.get("hs_meeting_start_time"))),
            "booked_ts":            parse_hs_time(mp.get("hs_createdate")),
            "channel":              channel,
            "source_detail":        src_detail,
            "outcome":              outcome,
            "ff_url":               ff_url,
            "ff_duration":          ff_data.get("duration"),
            "ff_action_items_count": ff_data.get("action_items_count", 0),
            "ff_keywords":          ff_data.get("keywords", []),
            "ff_overview":          ff_data.get("overview"),
            "lead_status":          props.get("hs_lead_status") or "—",
            "deal_stage":           deal_stage,
            "has_deal":             has_deal,
        })

    # Deduplicate — one row per contact, keep the most recent booking
    seen: dict = {}
    for r in rows:
        key = r["contact_id"] or r["name"]   # contact_id preferred; fall back to name
        if key not in seen or r["booked_ts"] > seen[key]["booked_ts"]:
            seen[key] = r
    rows = list(seen.values())

    return sorted(rows, key=lambda r: r["booked_ts"]), active_campaigns

# ── Slack message formatting ───────────────────────────────────────────────

E_OUT = {"showed": "✅", "noshow": "❌", "cancelled": "🚫", "upcoming": "⏳", "unknown": "❓"}
E_SRC = {"Inbound": "🔵", "Outbound": "🟠"}


def show_rate(rows):
    s = sum(1 for r in rows if r["outcome"] == "showed")
    n = sum(1 for r in rows if r["outcome"] == "noshow")
    return f"{int(s / max(s + n, 1) * 100)}%" if (s + n) > 0 else "—"


def row_block(r):
    """Two-line entry. Channel emoji omitted — caller renders the section header."""
    stage = r["deal_stage"] if r["deal_stage"] else "No deal yet"
    src   = r["source_detail"]
    if len(src) > 26:
        src = src[:25] + "…"
    line1 = f"{E_OUT[r['outcome']]}  *{r['name']}*  —  {r['company']}   _{r['meeting_date']}_"
    line2 = f"     {src}   ·   {stage}"
    if r["outcome"] == "showed" and r.get("ff_duration"):
        extras = [f"{round(r['ff_duration'])}m"]
        if r.get("ff_action_items_count"):
            extras.append(f"{r['ff_action_items_count']} action items")
        line2 += "   ·   " + "   ·   ".join(extras)
    return f"{line1}\n{line2}"


def campaign_breakdown(outbound_rows, active_campaigns=None):
    """
    Per-campaign summary lines.
    Shows campaigns that produced demos first, then active campaigns with 0 demos.
    active_campaigns: list of campaign names currently Active in Instantly.
    """
    buckets = defaultdict(list)
    for r in outbound_rows:
        buckets[r["source_detail"]].append(r)

    lines = []
    # Campaigns that produced demos this period
    for name, cr in sorted(buckets.items(), key=lambda x: -len(x[1])):
        cs = sum(1 for r in cr if r["outcome"] == "showed")
        lines.append(f"   📣 *{name}*   {len(cr)} booked  ·  {cs} showed")

    # Active campaigns with 0 demos this period
    if active_campaigns:
        for name in active_campaigns:
            if name not in buckets:
                lines.append(f"   📣 *{name}*   0 bookings this period")

    return lines


def channel_section(rows, emoji, label, campaign_lines=None):
    """Render a labelled channel block — header with stats + rows."""
    if not rows:
        return []
    total    = len(rows)
    showed   = sum(1 for r in rows if r["outcome"] == "showed")
    deals    = sum(1 for r in rows if r["deal_stage"])
    pipeline = active_pipeline(rows)
    deal_pct = f"{int(deals / showed * 100)}%" if showed else "—"
    header = (
        f"{emoji} *{label} ({total})*"
        f"   ·   {showed}/{total} showed"
        f"   ·   {deals} deals  _{deal_pct} of showed_"
        f"   ·   🟢 {pipeline} in pipeline"
    )
    lines = [header, ""]
    if campaign_lines:
        lines += campaign_lines + [""]
    lines.extend(row_block(r) for r in rows)
    return lines


ENTRY_STAGE = "Demo Completed"

def active_pipeline(rows):
    """Deals that have moved past Demo Completed and aren't Closed Lost/Won."""
    TERMINAL = {"Closed Lost", "Closed Won", ENTRY_STAGE, ""}
    return sum(1 for r in rows if r["deal_stage"] not in TERMINAL)


def slack_weekly(rows, label, active_campaigns=None):
    showed    = sum(1 for r in rows if r["outcome"] == "showed")
    noshow    = sum(1 for r in rows if r["outcome"] == "noshow")
    cancelled = sum(1 for r in rows if r["outcome"] == "cancelled")
    upcoming  = sum(1 for r in rows if r["outcome"] == "upcoming")
    inbound   = sum(1 for r in rows if r["channel"] == "Inbound")
    outbound  = sum(1 for r in rows if r["channel"] == "Outbound")
    pipeline  = active_pipeline(rows)
    # Showed but no deal opened yet — needs follow-up
    needs_fu  = [r for r in rows if r["outcome"] == "showed" and not r["has_deal"]]

    lines = [
        f"📅 *Demo Report — {label}*",
        f"*{len(rows)} demos booked*   ·   🟢 *{pipeline}* in active pipeline",
        "",
        f"✅ *{showed}* showed   ❌ *{noshow}* no-show   🚫 *{cancelled}* cancelled   ⏳ *{upcoming}* upcoming",
        f"Show rate *{show_rate(rows)}*   ·   🔵 Inbound *{inbound}*   🟠 Outbound *{outbound}*",
    ]

    if needs_fu:
        lines += ["", f"⚠️ *Showed — no deal opened yet ({len(needs_fu)}):*"]
        for r in needs_fu:
            detail = f"  —  {r['company']}"
            if r.get("ff_duration"):
                detail += f"   {r['ff_duration']}m"
            if r.get("ff_keywords"):
                detail += f"   ·   {', '.join(r['ff_keywords'][:3])}"
            lines.append(f"   • *{r['name']}*{detail}")

    inbound_rows  = [r for r in rows if r["channel"] == "Inbound"]
    outbound_rows = [r for r in rows if r["channel"] == "Outbound"]

    lines += ["", "─" * 38, ""]
    lines += channel_section(inbound_rows,  "🔵", "Inbound")
    if inbound_rows and outbound_rows:
        lines += [""]
    lines += channel_section(outbound_rows, "🟠", "Outbound",
                             campaign_lines=campaign_breakdown(outbound_rows, active_campaigns))
    return "\n".join(lines)


def slack_monthly(rows, label, active_campaigns=None):
    showed    = sum(1 for r in rows if r["outcome"] == "showed")
    noshow    = sum(1 for r in rows if r["outcome"] == "noshow")
    cancelled = sum(1 for r in rows if r["outcome"] == "cancelled")
    pipeline  = active_pipeline(rows)

    # Channel comparison (single line each)
    def channel_summary(ch):
        cr = [r for r in rows if r["channel"] == ch]
        if not cr:
            return None
        cs  = sum(1 for r in cr if r["outcome"] == "showed")
        cd  = sum(1 for r in cr if r["deal_stage"])
        cp  = active_pipeline(cr)
        pct = f"{int(cd / cs * 100)}%" if cs else "—"
        emoji = "🔵" if ch == "Inbound" else "🟠"
        return (
            f"{emoji} *{ch}*   {len(cr)} booked   ·   {cs} showed"
            f"   ·   {cd} deals  _{pct} → deal_"
            f"   ·   🟢 {cp} in pipeline"
        )

    # Week-by-week
    week_buckets = defaultdict(list)
    for r in rows:
        dt  = datetime.fromtimestamp(r["booked_ts"] / 1000, tz=timezone.utc)
        mon = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        week_buckets[mon].append(r)

    # Pipeline breakdown — group company names by stage
    STAGE_ORDER = [
        "Closed Won", "Procurement", "In Pilot", "Pilot Scheduled",
        "Interested In Pilot", "Interested - Reach Out Later",
        "Demo Completed", "Closed Lost",
    ]
    stage_buckets = defaultdict(list)
    for r in rows:
        key = r["deal_stage"] if r["deal_stage"] else "No deal yet"
        stage_buckets[key].append(r["company"] if r["company"] != "—" else r["name"])

    def stage_sort_key(s):
        if s in STAGE_ORDER: return (0, STAGE_ORDER.index(s))
        if s == "No deal yet": return (2, 0)
        return (1, s)

    def stage_line(stage, companies):
        # For Demo Completed show count only (usually many); show names for everything else
        if stage == ENTRY_STAGE and len(companies) > 4:
            return f"  🟡 {stage}   *{len(companies)}*"
        names = ",  ".join(companies)
        if stage == "Closed Won":      icon = "🏆"
        elif stage in ("In Pilot", "Pilot Scheduled", "Interested In Pilot",
                       "Interested - Reach Out Later", "Procurement"): icon = "🟢"
        elif stage == "Closed Lost":   icon = "🔴"
        elif stage == ENTRY_STAGE:     icon = "🟡"
        else:                          icon = "⚪"
        return f"  {icon} *{stage}*  ({len(companies)})   {names}"

    lines = [
        f"📊 *Monthly Demo Report — {label}*",
        f"*{len(rows)} demos booked*   ·   🟢 *{pipeline}* in active pipeline",
        "",
        f"✅ *{showed}* showed   ❌ *{noshow}* no-show   🚫 *{cancelled}* cancelled   |   Show rate *{show_rate(rows)}*",
        "",
    ]

    for ch in ("Inbound", "Outbound"):
        s = channel_summary(ch)
        if s:
            lines.append(s)
            if ch == "Outbound":
                cr = [r for r in rows if r["channel"] == "Outbound"]
                lines.extend(campaign_breakdown(cr, active_campaigns))

    lines += ["", "─" * 38, "", "*Week by week:*"]
    for mon_dt in sorted(week_buckets):
        wr   = week_buckets[mon_dt]
        ws   = sum(1 for r in wr if r["outcome"] == "showed")
        wn   = sum(1 for r in wr if r["outcome"] == "noshow")
        wc   = sum(1 for r in wr if r["outcome"] == "cancelled")
        lines.append(f"  *{mon_dt.strftime('%b %-d')}*   {len(wr)} booked · ✅ {ws}  ❌ {wn}  🚫 {wc}")

    lines += ["", "─" * 38, "", "*Pipeline:*"]
    for stage in sorted(stage_buckets, key=stage_sort_key):
        lines.append(stage_line(stage, stage_buckets[stage]))

    needs_fu = [r for r in rows if r["outcome"] == "showed" and not r["has_deal"]]
    if needs_fu:
        names = ",  ".join(r["company"] if r["company"] != "—" else r["name"] for r in needs_fu)
        lines += ["", f"⚠️ *Showed — no deal opened ({len(needs_fu)}):*   {names}"]

    return "\n".join(lines)

# ── Post to Slack ──────────────────────────────────────────────────────────

def post_slack(text, dry_run=False):
    print("\n" + "=" * 60)
    print(text)
    print("=" * 60 + "\n")

    if dry_run:
        print("[DRY RUN — not posting to Slack]")
        return

    if not SLACK_WEBHOOK:
        print("ERROR: SLACK_WEBHOOK_URL not set")
        sys.exit(1)

    r = requests.post(SLACK_WEBHOOK, json={"text": text})
    if r.status_code != 200:
        print(f"Slack error {r.status_code}: {r.text}")
        sys.exit(1)
    print("Posted to Slack.")

# ── Entry points ───────────────────────────────────────────────────────────

def run_weekly(dry_run=False):
    now      = datetime.now(timezone.utc)
    # Go back to last Monday (runs on Monday, so weekday()=0, days_back=7)
    days_back = now.weekday() + 7
    last_mon  = (now - timedelta(days=days_back)).replace(hour=0,  minute=0,  second=0,  microsecond=0)
    last_sun  = (last_mon + timedelta(days=6)   ).replace(hour=23, minute=59, second=59, microsecond=999999)
    label     = f"Week of {last_mon.strftime('%b %-d')} – {last_sun.strftime('%b %-d, %Y')}"

    print(f"\nWeekly report: {label}")
    rows, active_campaigns = fetch_demos(int(last_mon.timestamp() * 1000), int(last_sun.timestamp() * 1000))
    post_slack(slack_weekly(rows, label, active_campaigns), dry_run)


def run_monthly(dry_run=False):
    now        = datetime.now(timezone.utc)
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev  = first_this - timedelta(seconds=1)
    first_prev = last_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    label      = first_prev.strftime("%B %Y")

    print(f"\nMonthly report: {label}")
    rows, active_campaigns = fetch_demos(int(first_prev.timestamp() * 1000), int(last_prev.timestamp() * 1000))
    post_slack(slack_monthly(rows, label, active_campaigns), dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["weekly", "monthly"])
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout, skip Slack post")
    args = parser.parse_args()

    if args.mode == "weekly":
        run_weekly(args.dry_run)
    else:
        run_monthly(args.dry_run)
