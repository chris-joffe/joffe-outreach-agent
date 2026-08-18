"""
hubspot_db.py — HubSpot as the Joffe SDR's source, status ledger, and activity log.
=====================================================================================
Unlike Vida (which used a Google Sheet as its DB), the Joffe SDR treats the Joffe
HubSpot portal (2936356) as the single source of truth:

  • SOURCE   — we SEARCH for eligible School contacts (subscribers first, then leads).
  • LEDGER   — we STAMP custom properties on each contact as we act on it, so
               "already contacted" is simply a filter on joffe_outreach_status.
  • ACTIVITY — we LOG the actual email onto the contact's timeline as an email
               engagement (no BCC, no browser extension — done directly via the API).

Auth: a HubSpot private-app token passed in from the agent (env HUBSPOT_TOKEN).
Needs scopes: crm.objects.contacts read+write, crm.objects.deals read (optional
gate), and crm.objects.emails/engagements write (for timeline logging).

Custom properties this module owns (auto-created by ensure_properties):
  joffe_outreach_status   enum   Contacted / Replied / MQL / SQL / Bounced / Unsubscribed
  joffe_outreach_touches  number 1-4 (which touch in the cadence they last got)
  joffe_last_touch_date   string YYYY-MM-DD (compared client-side for follow-up timing)
  joffe_outreach_variant  string A (Membership) / B (Assessment)
  joffe_outreach_agent    string sending SDR display name (Jessica Dean / Ryan Andrews)
"""
import json
import logging
import os
import ssl
import time
from datetime import datetime, timezone
import urllib.request
import urllib.error

from email_format import clean_quote, paras_html

log = logging.getLogger("joffe-sdr")

BASE = "https://api.hubapi.com"

# Contacts we source. We message subscribers before leads (Chris). Anyone at MQL or
# above is deliberately excluded by the lifecycle filter, which satisfies the hard
# rule "never message anyone who's SQL or higher" without a separate exclusion.
SCHOOL_TYPE = "School"          # organization_type_ (Relationship Type*) option value
SOURCE_STAGES = ["subscriber", "lead"]

# email → contact default association typeId (HUBSPOT_DEFINED)
EMAIL_TO_CONTACT_ASSOC = 198

CUSTOM_PROPS = [
    {
        "name": "joffe_outreach_status", "label": "Joffe Outreach Status",
        "type": "enumeration", "fieldType": "select",
        "options": [
            {"label": "Contacted", "value": "Contacted"},
            {"label": "Replied", "value": "Replied"},
            {"label": "MQL", "value": "MQL"},
            {"label": "SQL", "value": "SQL"},
            {"label": "Bounced", "value": "Bounced"},
            {"label": "Unsubscribed", "value": "Unsubscribed"},
            {"label": "Skipped-Customer", "value": "Skipped-Customer"},
        ],
    },
    {"name": "joffe_outreach_touches", "label": "Joffe Outreach Touches",
     "type": "number", "fieldType": "number"},
    {"name": "joffe_last_touch_date", "label": "Joffe Last Touch Date",
     "type": "string", "fieldType": "text"},
    {"name": "joffe_outreach_variant", "label": "Joffe Outreach Variant",
     "type": "string", "fieldType": "text"},
    {"name": "joffe_outreach_agent", "label": "Joffe Outreach Agent",
     "type": "string", "fieldType": "text"},
]

READ_PROPS = [
    "email", "firstname", "lastname", "company", "lifecyclestage",
    "organization_type_", "hubspot_owner_id",
    "joffe_outreach_status", "joffe_outreach_touches", "joffe_last_touch_date",
    "joffe_outreach_variant", "joffe_outreach_agent",
]


def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _request(method, url, token, payload=None, tries=4):
    """One HubSpot REST call with retry on transient (429/5xx/network) errors.
    Returns (status_code, parsed_json). Raises only after exhausting retries on a
    genuine network exception."""
    last = None
    for attempt in range(tries):
        try:
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(url, data=data, headers=_headers(token), method=method)
            with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx()) as r:
                body = r.read().decode()
                return r.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"raw": body}
            # Retry rate-limits and server errors; return anything else to the caller.
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return e.code, parsed
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


# ─── Property + portal setup ─────────────────────────────────────────────────

def ensure_properties(token, group="contactinformation"):
    """Create any of our custom tracking properties that don't exist yet, and sync enum
    options if we've added new ones since (e.g. Skipped-Customer). Idempotent. Returns
    the list of created names."""
    created = []
    for p in CUSTOM_PROPS:
        status, resp = _request("GET", f"{BASE}/crm/v3/properties/contacts/{p['name']}", token)
        if status == 200:
            # property exists — add any enum options that are missing
            if "options" in p:
                have = {o.get("value") for o in resp.get("options", [])}
                want = {o["value"] for o in p["options"]}
                if not want <= have:
                    _request("PATCH", f"{BASE}/crm/v3/properties/contacts/{p['name']}", token,
                             {"options": p["options"]})
            continue
        body = {"name": p["name"], "label": p["label"], "type": p["type"],
                "fieldType": p["fieldType"], "groupName": group}
        if "options" in p:
            body["options"] = p["options"]
        st, resp = _request("POST", f"{BASE}/crm/v3/properties/contacts", token, body)
        if st in (200, 201):
            created.append(p["name"])
        elif st == 409:
            pass  # created concurrently / already there
        else:
            raise RuntimeError(f"Failed to create property {p['name']}: HTTP {st} {resp}")
    return created


# Company lifecycle stages that mean "don't cold-email anyone here" (SQL or higher).
SKIP_COMPANY_STAGES = {"salesqualifiedlead", "opportunity", "customer", "evangelist"}

# Standard HubSpot lifecycle ordering — used so a reply can only ADVANCE a contact's
# stage, never drag a more-advanced record (opportunity/customer) backward.
_STAGE_RANK = {
    "": 0, "other": 0, "subscriber": 1, "lead": 2, "marketingqualifiedlead": 3,
    "salesqualifiedlead": 4, "opportunity": 5, "customer": 6, "evangelist": 7,
}


def _current_lifecycle(token, cid):
    _, d = _request("GET", f"{BASE}/crm/v3/objects/contacts/{cid}?properties=lifecyclestage", token)
    return (d.get("properties", {}).get("lifecyclestage") or "").strip().lower()


def _deal_flags(token, deal_id):
    """(is_won, is_open) for a deal, using HubSpot's pipeline-agnostic closed flags."""
    _, d = _request("GET", f"{BASE}/crm/v3/objects/deals/{deal_id}"
                    "?properties=hs_is_closed_won,hs_is_closed", token)
    p = d.get("properties", {})
    won = str(p.get("hs_is_closed_won")).lower() == "true"
    closed = str(p.get("hs_is_closed")).lower() == "true"
    return won, (not closed)


def _assoc_ids(token, from_obj, obj_id, to_obj):
    _, d = _request("GET", f"{BASE}/crm/v3/objects/{from_obj}/{obj_id}/associations/{to_obj}", token)
    return [r["id"] for r in d.get("results", [])]


def customer_gate(token, contact):
    """Return (skip, reason). Skip a contact if HubSpot shows the account is a current
    or former customer / active opportunity — checked at the COMPANY level (lifecycle +
    the company's deals) and the contact's own deals. Catches the case where an
    individual contact still reads 'subscriber' but their school is a customer.
    NOTE: can only catch what HubSpot records — a former customer with no company
    lifecycle, no deal, is invisible here (needs a suppression list)."""
    cid = contact["id"]
    for company_id in _assoc_ids(token, "contacts", cid, "companies"):
        _, co = _request("GET", f"{BASE}/crm/v3/objects/companies/{company_id}"
                         "?properties=name,lifecyclestage", token)
        cp = co.get("properties", {})
        name = cp.get("name") or company_id
        if (cp.get("lifecyclestage") or "").lower() in SKIP_COMPANY_STAGES:
            return True, f"company '{name}' is {cp.get('lifecyclestage')}"
        for did in _assoc_ids(token, "companies", company_id, "deals"):
            won, is_open = _deal_flags(token, did)
            if won:
                return True, f"company '{name}' has a closed-won deal"
            if is_open:
                return True, f"company '{name}' has an open deal"
    for did in _assoc_ids(token, "contacts", cid, "deals"):
        won, is_open = _deal_flags(token, did)
        if won:
            return True, "contact has a closed-won deal"
        if is_open:
            return True, "contact has an open deal"
    return False, ""


_PORTAL = None


def portal_id(token):
    global _PORTAL
    if _PORTAL is not None:
        return _PORTAL
    try:
        _, data = _request("GET", f"{BASE}/account-info/v3/details", token)
        _PORTAL = str(data.get("portalId", "") or "")
    except Exception:
        _PORTAL = ""
    return _PORTAL


def contact_link(token, cid):
    pid = portal_id(token)
    return f"https://app.hubspot.com/contacts/{pid}/record/0-1/{cid}" if pid and cid else ""


# ─── Source: search for eligible contacts ────────────────────────────────────

def _parse_contact(result):
    p = result.get("properties", {})
    return {
        "id": result.get("id"),
        "email": (p.get("email") or "").strip(),
        "firstName": (p.get("firstname") or "").strip(),
        "lastName": (p.get("lastname") or "").strip(),
        "company": (p.get("company") or "").strip(),
        "lifecycle": (p.get("lifecyclestage") or "").strip(),
        "owner_id": (p.get("hubspot_owner_id") or "").strip(),
        "status": (p.get("joffe_outreach_status") or "").strip(),
        "touches": p.get("joffe_outreach_touches") or "",
        "last_touch": (p.get("joffe_last_touch_date") or "").strip(),
        "variant": (p.get("joffe_outreach_variant") or "").strip(),
        "agent": (p.get("joffe_outreach_agent") or "").strip(),
    }


def _search(token, filters, needed, sort_prop="createdate"):
    """Page through a HubSpot contact search until we collect `needed` results or run
    out. Returns a list of parsed contact dicts."""
    out = []
    after = None
    while len(out) < needed:
        payload = {
            "filterGroups": [{"filters": filters}],
            "properties": READ_PROPS,
            "sorts": [{"propertyName": sort_prop, "direction": "ASCENDING"}],
            "limit": min(100, needed - len(out)),
        }
        if after:
            payload["after"] = after
        status, data = _request("POST", f"{BASE}/crm/v3/objects/contacts/search", token, payload)
        if status != 200:
            raise RuntimeError(f"contact search failed: HTTP {status} {data}")
        for r in data.get("results", []):
            c = _parse_contact(r)
            if c["email"]:
                out.append(c)
        after = (data.get("paging", {}).get("next", {}) or {}).get("after")
        if not after:
            break
    return out[:needed]


def fetch_new(token, needed):
    """Eligible NEVER-CONTACTED School contacts: subscribers first, then leads.
    'Never contacted' = our joffe_outreach_status has no value yet. Because we stamp
    that property the moment we email someone, each run naturally returns the next
    slice — no cursor to maintain."""
    collected = []
    for stage in SOURCE_STAGES:
        if len(collected) >= needed:
            break
        filters = [
            {"propertyName": "organization_type_", "operator": "EQ", "value": SCHOOL_TYPE},
            {"propertyName": "lifecyclestage", "operator": "EQ", "value": stage},
            {"propertyName": "joffe_outreach_status", "operator": "NOT_HAS_PROPERTY"},
            {"propertyName": "email", "operator": "HAS_PROPERTY"},
        ]
        batch = _search(token, filters, needed - len(collected))
        for c in batch:
            c["touch"] = 1
        collected.extend(batch)
    return collected


def fetch_followups(token, needed, today, gap_days, max_touches, stale_days):
    """Contacts mid-sequence (status Contacted, touches 1..max-1) whose next touch is
    due. Date-due is judged client-side to avoid HubSpot date-filter epoch quirks."""
    from datetime import date
    filters = [
        {"propertyName": "organization_type_", "operator": "EQ", "value": SCHOOL_TYPE},
        {"propertyName": "joffe_outreach_status", "operator": "EQ", "value": "Contacted"},
        {"propertyName": "joffe_outreach_touches", "operator": "LT", "value": max_touches},
    ]
    # Pull a generous window; due-filtering below trims it.
    candidates = _search(token, filters, max(needed * 4, 200), sort_prop="joffe_last_touch_date")
    due = []
    for c in candidates:
        if len(due) >= needed:
            break
        try:
            t = int(float(c["touches"]))
        except (TypeError, ValueError):
            continue
        if t < 1 or t >= max_touches:
            continue
        try:
            sent = date.fromisoformat((c["last_touch"] or "")[:10])
        except ValueError:
            continue
        age = (today - sent).days
        if gap_days.get(t, 999) <= age <= stale_days:
            c["touch"] = t + 1
            due.append(c)
    return due


# ─── Ledger writes ───────────────────────────────────────────────────────────

def stamp(token, cid, **props):
    """PATCH the contact with any of our custom tracking properties. Keys accepted:
    status, touches, last_touch, variant, agent (plus raw lifecyclestage/hs_lead_status
    if passed as those exact names)."""
    keymap = {
        "status": "joffe_outreach_status", "touches": "joffe_outreach_touches",
        "last_touch": "joffe_last_touch_date", "variant": "joffe_outreach_variant",
        "agent": "joffe_outreach_agent",
    }
    body = {}
    for k, v in props.items():
        if v is None:
            continue
        body[keymap.get(k, k)] = str(v)
    if not body:
        return
    status, resp = _request("PATCH", f"{BASE}/crm/v3/objects/contacts/{cid}", token,
                            {"properties": body})
    if status not in (200, 201):
        raise RuntimeError(f"stamp {cid} failed: HTTP {status} {resp}")


def log_email(token, cid, subject, body_text, from_name, from_email, to_email, ts_ms):
    """Log a sent email onto the contact's timeline as an email engagement — the direct-
    API equivalent of the HubSpot Chrome extension / BCC logging. Best-effort: a failure
    here never blocks the send (returns False)."""
    payload = {
        "properties": {
            "hs_timestamp": str(ts_ms),
            "hs_email_direction": "EMAIL",
            "hs_email_status": "SENT",
            "hs_email_subject": subject,
            "hs_email_text": body_text,
            "hs_email_headers": json.dumps({
                "from": {"email": from_email, "firstName": from_name},
                "to": [{"email": to_email}],
            }),
        },
        "associations": [{
            "to": {"id": str(cid)},
            "types": [{"associationCategory": "HUBSPOT_DEFINED",
                       "associationTypeId": EMAIL_TO_CONTACT_ASSOC}],
        }],
    }
    try:
        status, resp = _request("POST", f"{BASE}/crm/v3/objects/emails", token, payload)
        return status in (200, 201)
    except Exception:
        return False


# ─── Reply-side writes: promote to MQL / SQL, stamp attribution ───────────────

_SOURCE_PROP = None  # cache: (prop_name, option_value) or (None, None)


def _resolve_source_prop(token):
    """Find a WRITABLE traffic-source contact property with an 'AI Referrals' option.
    Prefers Original Source (hs_analytics_source) — durable, first-known-source, and
    manually writable — over Latest Traffic Source (recalculated each web session).
    Returns (name, value) or (None, None) if none is writable."""
    global _SOURCE_PROP
    if _SOURCE_PROP is not None:
        return _SOURCE_PROP
    _SOURCE_PROP = (None, None)
    try:
        _, data = _request("GET", f"{BASE}/crm/v3/properties/contacts", token)
        props = {p["name"]: p for p in data.get("results", [])}
        for name in ("hs_analytics_source", "hs_latest_source"):
            p = props.get(name)
            if not p:
                continue
            if (p.get("modificationMetadata") or {}).get("readOnlyValue"):
                continue  # can't write it
            for opt in p.get("options", []):
                if opt.get("label", "").strip().lower() in ("ai referrals", "ai referral"):
                    _SOURCE_PROP = (name, opt["value"])
                    return _SOURCE_PROP
    except Exception:
        pass
    return _SOURCE_PROP


def find_contact(token, email):
    """Return the contact id for an email, or '' if not in HubSpot."""
    status, data = _request("POST", f"{BASE}/crm/v3/objects/contacts/search", token, {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "lifecyclestage"], "limit": 1,
    })
    if status == 200 and data.get("results"):
        return str(data["results"][0]["id"])
    return ""


def log_reply_note(token, cid, reply_text, why="", agent_name=""):
    """Put the prospect's actual reply on the record as a note.

    Without it the contact carries a task saying "follow up" and nothing else — the reply
    lives only in the SDR mailbox, which sales can't see (Manae hit exactly this on
    2026-08-17). Notes are used rather than logged emails on purpose: a note does not move
    notes_last_contacted or hs_last_sales_activity_timestamp, so it can't make an untouched
    lead look answered in the speed-to-lead timer.
    """
    if not cid:
        return ""
    body = clean_quote(reply_text or "")
    html = (f"<b>Reply received by {agent_name or 'the SDR'}</b> (logged automatically)"
            + (f"<br><br><i>{why}</i>" if why else "")
            + "<br><br>" + (paras_html(body) if body else "<i>(no readable message body)</i>"))
    payload = {"properties": {
        "hs_note_body": html[:65000],
        "hs_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
        "associations": [{"to": {"id": cid}, "types": [
            {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]}
    try:
        st, data = _request("POST", f"{BASE}/crm/v3/objects/notes", token, payload)
        if st in (200, 201):
            return str(data.get("id", ""))
        log.warning(f"    note create failed ({st}): {str(data)[:140]}")
    except Exception as e:
        log.warning(f"    note create failed: {e}")
    return ""


def create_followup_task(token, cid, owner_id, name, why, due_minutes, agent_name=""):
    """Put a handed-over SQL in the owner's HubSpot task queue, due in `due_minutes`.

    Before this, a handoff existed only as an email in one inbox: nothing appeared in a
    queue and nothing measured whether it was worked (Chris, 2026-08-13). Best-effort —
    a failure here never blocks the lead write or the notification.
    """
    if not (cid and owner_id):
        return ""
    due_ms = int((time.time() + due_minutes * 60) * 1000)
    body = {"properties": {
        "hs_task_subject": f"Follow up: {name or 'new lead'} (school-safety enquiry)",
        "hs_task_body": (why or "Replied to outreach.") + (f" — handed over by {agent_name}"
                                                           if agent_name else ""),
        "hs_task_status": "NOT_STARTED",
        "hs_task_priority": "HIGH",
        "hs_task_type": "EMAIL",
        "hs_timestamp": due_ms,
        "hubspot_owner_id": str(owner_id),
    }, "associations": [{
        "to": {"id": cid},
        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}],
    }]}
    try:
        st, data = _request("POST", f"{BASE}/crm/v3/objects/tasks", token, body)
        if st in (200, 201):
            return str(data.get("id", ""))
        log.warning(f"    task create failed ({st}): {str(data)[:160]}")
    except Exception as e:
        log.warning(f"    task create failed: {e}")
    return ""


def followup_state(token, cid):
    """(last_touch_epoch_ms, lead_status, lifecycle) for a contact, or (None,"","") if the
    read fails — the caller then leaves the lead alone rather than crying wolf."""
    if not cid:
        return None, "", ""
    props = "notes_last_contacted,hs_last_sales_activity_timestamp,hs_lead_status,lifecyclestage"
    try:
        _, d = _request("GET", f"{BASE}/crm/v3/objects/contacts/{cid}?properties={props}", token)
    except Exception as e:
        log.warning(f"  stall check: could not read contact {cid}: {e}")
        return None, "", ""
    pr = d.get("properties", {}) or {}

    def _ms(v):
        if not v:
            return 0
        try:
            return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            try:
                return int(v)
            except Exception:
                return 0
    return (max(_ms(pr.get("notes_last_contacted")),
                _ms(pr.get("hs_last_sales_activity_timestamp"))),
            (pr.get("hs_lead_status") or ""), (pr.get("lifecyclestage") or ""))


def upsert_lead(token, email, stage, first="", last="", company="", phone="",
                agent_name="", owner_id=None, stamp_source=False):
    """Create or update a HubSpot contact at `stage` (marketingqualifiedlead or
    salesqualifiedlead). Optionally assign an owner and stamp Original Source =
    AI Referrals + drill-down = the SDR name (only on create/convert, never
    clobbering an existing lead's genuine original source unless stamp_source=True).
    Returns the contact id or ''."""
    props = {"email": email, "lifecyclestage": stage}
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company
    if phone:
        props["phone"] = phone
    if owner_id:
        props["hubspot_owner_id"] = str(owner_id)

    existing = find_contact(token, email)

    if stamp_source and (not existing):  # only stamp source on brand-new contacts
        sp_name, sp_val = _resolve_source_prop(token)
        if sp_name:
            props[sp_name] = sp_val
            drill = f"{sp_name}_data_1"
            props[drill] = f"Joffe SDR – {agent_name}" if agent_name else "Joffe SDR"

    if existing:
        # Never downgrade: if the contact is already at or beyond the target stage
        # (e.g. opportunity/customer), leave lifecycle AND owner untouched and only
        # update supporting fields (name/phone). Advancing writes go through normally.
        cur = _current_lifecycle(token, existing)
        if _STAGE_RANK.get(stage, 0) <= _STAGE_RANK.get(cur, 0):
            props.pop("lifecyclestage", None)
            props.pop("hubspot_owner_id", None)
        status, resp = _request("PATCH", f"{BASE}/crm/v3/objects/contacts/{existing}", token,
                                {"properties": props})
        return existing if status in (200, 201) else ""
    status, resp = _request("POST", f"{BASE}/crm/v3/objects/contacts", token, {"properties": props})
    return str(resp.get("id", "")) if status in (200, 201) else ""
