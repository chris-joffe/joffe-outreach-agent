#!/usr/bin/env python3
"""
Joffe School-Safety SDR — outbound outreach agent.
===================================================
Sibling of the Get CPR Done "Vida" agent, tailored for Joffe:

  • Two SDR mailboxes on joffeschoolsafety.com, round-robin:
        Jessica Dean  <jessicad@joffeschoolsafety.com>
        Ryan Andrews  <ryana@joffeschoolsafety.com>
  • Source + status ledger + activity log = the Joffe HubSpot portal (2936356),
    via hubspot_db.py. No Google Sheet.
  • Audience: Relationship Type = School, subscribers first then leads. Anyone at
    MQL or above is excluded (never message SQL or higher).
  • A/B test, assigned 50/50 per contact (mailbox round-robins independently so
    sender reputation doesn't confound the test):
        Variant A — Membership (AAA analogy)   → CTA: book time with Colleen
        Variant B — Assessment (Swiss Cheese)  → CTA: free self-serve assessment
  • 4-touch cadence: opener → book offer (All Clear) → value/proof → breakup.
  • Replies triaged: booking interest → SQL (owner = Colleen, notify Colleen + CC
    Chris); other genuine reply → MQL; opt-out → Unsubscribed; bounce → Bounced.
  • Volume ramp (combined across both mailboxes): 50 → 250 → 500 → 900 → 1000.
  • PUBLIC repo → logs redact recipient emails; no PII persisted to state.json.

Modes:  setup | daily | reply_check | report
"""
import argparse
import hashlib
import json
import logging
import os
import random
import re
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import date, datetime
from html import escape as html_escape
from pathlib import Path
from zoneinfo import ZoneInfo

import hubspot_db as hs

# ─── Identity & contacts ──────────────────────────────────────────────────────
COMPANY_NAME  = "Joffe Emergency Services"
CHRIS_EMAIL   = "chris@joffeemergencyservices.com"
COLLEEN_EMAIL = "colleens@joffeemergencyservices.com"
COLLEEN_OWNER_ID = "199562610"          # HubSpot owner id for SQL assignment
REPORT_TO     = [CHRIS_EMAIL, COLLEEN_EMAIL]

# The two sending identities. Addresses are public; only the app passwords are secret.
PERSONAS = [
    {"key": "jessica", "name": "Jessica Dean",
     "email": "jessicad@joffeschoolsafety.com", "pass_env": "JESSICA_APP_PASSWORD"},
    {"key": "ryan", "name": "Ryan Andrews",
     "email": "ryana@joffeschoolsafety.com", "pass_env": "RYAN_APP_PASSWORD"},
]

COLLEEN_LINK = ("https://www.joffeemergencyservices.com/meetings/colleens/"
                "emergency-management-update?uuid=8a98d109-4d1f-450b-b052-3789356e123f")
ASSESSMENT_LINK = "https://www.joffeemergencyservices.com/school-assessment"
SCHOOLS_SUPPORTED = "2,000 K-12 schools"

# ─── Cadence & volume ─────────────────────────────────────────────────────────
FOLLOWUP_GAP_DAYS   = {1: 3, 2: 4, 3: 7}   # days after touch N before touch N+1 is due
MAX_TOUCHES         = 4
FOLLOWUP_STALE_DAYS = 30                    # don't resurrect a sequence older than this
MIN_DELAY_SEC, MAX_DELAY_SEC = 10, 20       # send spacing (fits Actions job limits)
GENERATION_BATCH_SIZE = 20

# Combined daily cap ramps by whole weeks since launch. Set JOFFE_LAUNCH_DATE
# (YYYY-MM-DD) as a repo variable/secret on go-live; until then we stay at the
# conservative warm-up floor.
RAMP_SCHEDULE = {0: 50, 1: 250, 2: 500, 3: 900}   # week → cap; week ≥ 4 → 1000
RAMP_MAX = 1000

# ─── Models & secrets ─────────────────────────────────────────────────────────
GEN_MODEL          = "claude-haiku-4-5"
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
HUBSPOT_TOKEN      = os.environ.get("HUBSPOT_TOKEN", "")

STATE_FILE = Path(__file__).parent / "state.json"
LOG_FILE   = Path(__file__).parent / "outreach.log"
PACIFIC    = ZoneInfo("America/Los_Angeles")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("joffe-sdr")

ROLE_PREFIXES = {"info", "office", "admin", "contact", "hello", "help", "support",
                 "frontdesk", "reception", "mail", "school", "principal"}
PLACEHOLDER_ADDRESSES = {"email@yourbusiness.com", "test@test.com",
                         "example@example.com", "user@example.com", "name@domain.com"}


# ─── Small utilities ──────────────────────────────────────────────────────────
def pacific_today():
    return datetime.now(PACIFIC).date()


def today_str():
    return pacific_today().isoformat()


def redact_email(addr):
    """j***@domain.com — never print a full recipient address in a public log."""
    if not addr or "@" not in addr:
        return "***"
    local, _, domain = addr.partition("@")
    return f"{local[:1]}***@{domain}"


def is_role_address(email):
    return (email or "").split("@")[0].lower() in ROLE_PREFIXES


def variant_for(email):
    """Deterministic, stable 50/50 split by email so a contact always lands in the
    same arm across runs (and so re-runs don't reshuffle)."""
    h = int(hashlib.md5((email or "").strip().lower().encode()).hexdigest(), 16)
    return "A" if h % 2 == 0 else "B"


def weeks_since_launch():
    ld = os.environ.get("JOFFE_LAUNCH_DATE", "").strip()
    if not ld:
        return None
    try:
        launch = date.fromisoformat(ld)
    except ValueError:
        return None
    return max(0, (pacific_today() - launch).days // 7)


def daily_cap():
    w = weeks_since_launch()
    if w is None:
        return 0                         # no launch date set → PAUSED (explicit go-live switch)
    return RAMP_SCHEDULE.get(w, RAMP_MAX) if w < 4 else RAMP_MAX


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.warning("state.json unreadable — starting fresh")
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _bump(state, key, n=1, day=None):
    day = day or today_str()
    d = state.setdefault(key, {})
    d[day] = d.get(day, 0) + n


def _bump_variant(state, variant, day=None):
    day = day or today_str()
    d = state.setdefault("daily_variant_sent", {}).setdefault(variant, {})
    d[day] = d.get(day, 0) + 1


# ─── HTTP (Anthropic) ─────────────────────────────────────────────────────────
def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _anthropic(payload, timeout=240):
    headers = {"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
               "anthropic-version": "2023-06-01"}
    body = json.dumps(payload).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        data = json.loads(r.read().decode())
    return next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), "")


# ─── Email generation ─────────────────────────────────────────────────────────
VARIANT_HOOKS = {
    "A": (
        "VARIANT A - MEMBERSHIP (Joffe's entry-tier annual membership, internally 'The Steady "
        "Hand'). Do NOT oversell it and do NOT list its features. Lead with ONE concrete, human "
        "hook and keep it understated. Strongest hook: one number to call, any hour, and a real "
        "Joffe emergency specialist picks up, whether it is an active situation or just a call "
        "they are not sure how to make. The emotional core, in plain words: you stop being "
        "alone with the decision. It is decision support and steady preparedness, not crisis "
        "response and not consulting. You may use ONE true supporting detail (never list them): "
        "a 24/7 hotline answered by a real specialist; the first thing after joining is an hour "
        "with a safety expert, not an invoice; a local trends report built for their campus; "
        "help getting set up for federal priority communications so calls still go through when "
        "networks are jammed. The goal is defensible, clear-headed decisions, not perfection. "
        "Warm, calm, understated. Never scary, never hype."
    ),
    "B": (
        "VARIANT B — ASSESSMENT (the Swiss cheese model). The hook: school incidents "
        "are rarely one big failure — they're small gaps quietly lining up, like the "
        "holes in stacked slices of Swiss cheese, and the hard part is spotting them "
        "before they align. Joffe offers a free, 5-minute self-assessment across the "
        "15 layers schools most often miss. Curious and diagnostic — a free resource, "
        "never a discount or a hard pitch."
    ),
}

FALLBACK = {
    "A": ("One number, any hour",
          "Hi {first},\n\nA Joffe membership is really one thing at its core: a number you "
          "can call any hour, where a real emergency specialist picks up. For an active "
          "situation, or just a call you are not sure how to make. The point is simple. You "
          f"stop being alone with the decision. Joffe supports {SCHOOLS_SUPPORTED} across the country."),
    "B": ("The gaps most schools can't see",
          "Hi {first},\n\nMost school incidents aren't one big failure. They're small gaps "
          "quietly lining up, like holes in stacked slices of Swiss cheese. We built a free "
          "5-minute assessment that walks through the 15 layers schools most often miss. "
          f"Joffe supports {SCHOOLS_SUPPORTED} across the country."),
}


def _fallback(contact):
    v = contact.get("variant", "A")
    subj, body = FALLBACK[v]
    first = contact.get("firstName") or "there"
    school = contact.get("company") or "your school"
    return {"subject": subj.format(school=school),
            "body": body.format(first=first),
            "clean_first_name": "", "clean_last_name": ""}


def generate_batch(contacts):
    """One Claude call for a batch. Each contact carries variant + touch. Returns a list
    of {subject, body, clean_first_name, clean_last_name} in input order. Body has NO
    CTA/link/signature — those are appended deterministically afterward."""
    if not contacts:
        return []
    system = (
        f"You are a business development associate at {COMPANY_NAME}, a school safety and "
        f"emergency-management partner that supports {SCHOOLS_SUPPORTED} across the country.\n\n"
        "APPROACH (from our SDR playbook, grounded in what actually converts): consultative "
        "and human, never salesy, and never overselling. Under 100 words. ONE clear hook only. "
        "Do not stack benefits or list features. No 'I hope this finds you well'. NEVER use the "
        "word 'pitch', and never defensively deny selling in ANY form ('not a pitch', 'not a "
        "sales tool', 'not selling anything', 'no catch', 'no strings', 'no obligation'); just "
        "be genuinely helpful without disclaiming. No exclamation points. No hard close. Never "
        "mention price or discounts.\n"
        "STYLE (these are the AI tells we must avoid): NEVER use an em dash or en dash (the '—' "
        "or '–' character) anywhere; use a period or a comma instead. Keep sentences plain and "
        "specific, not florid. If a sentence is phrased as a question, it MUST end with a "
        "question mark.\n"
        "TONE: calm, understated, steadying. Absolutely NO fear, doom, or worst-case framing, "
        "and never phrases like 'hope it all holds together', 'what if it fails', or 'before "
        "it's too late'. The reader should feel steadier after reading, never more anxious.\n"
        "PERSONALIZE + VARY: weave the specific school's name into each email naturally, and "
        "make EVERY email in the batch distinct, each with its own subject line and its own "
        "opening sentence. Never send two schools the same template or the same subject.\n"
        "ACCURACY: the '2,000 K-12 schools' figure means schools Joffe SUPPORTS across its "
        "services, NOT members and NOT people who took the assessment (membership is far "
        "smaller). NEVER imply 2,000 members. Never write 'membership with 2,000 schools', "
        "'join 2,000 schools', or 'we do this with 2,000 schools'. Use the number only as a "
        "standalone credibility line such as 'Joffe supports 2,000 K-12 schools across the "
        "country', kept separate from the specific offer.\n\n"
        "Each contact has a VARIANT (A or B) — use its framing:\n"
        f"  {VARIANT_HOOKS['A']}\n"
        f"  {VARIANT_HOOKS['B']}\n\n"
        "FOLLOW-UP CADENCE (SellingSara playbook): outbound is a persistence sequence that "
        "builds familiarity, not one-and-done. Each contact has a TOUCH number (1-4):\n"
        "  1 = first email, the consultative opener in the variant's framing.\n"
        "  2 = short follow-up (2-3 sentences) that gently resurfaces the first note AND offers "
        "Chris Joffe's book 'All Clear' (on school crisis leadership) free to read or listen to. "
        "Warm; assume they simply missed the first note, never a guilt-trip.\n"
        f"  3 = value email. You may state this TRUE proof point: we support {SCHOOLS_SUPPORTED}. "
        "Then describe the KIND of outcome we deliver (we take safety planning, drills, and "
        "readiness off their plate so staff are genuinely ready). Invent NO specific schools, "
        "names, numbers, or testimonials.\n"
        "  4 = brief, gracious breakup ('I'll stop reaching out, the door is open anytime'). "
        "Warm, no pressure.\n"
        "For touches 2-4, keep it SHORTER than touch 1 and briefly reference that you're "
        "following up on your earlier note.\n\n"
        "SHARED / ROLE INBOX (addressType 'shared/role inbox', e.g. info@, office@): don't use "
        "or invent a personal name. Open warmly ('Hi there, whoever is keeping an eye on the "
        "inbox'), and ask who the right person is to talk to about school safety. clean_first_name "
        "must be empty for these.\n\n"
        "NAME SAFETY: the given firstName may be ALL CAPS, miscapitalized, a surname in the "
        "wrong field, swapped with lastName, or junk (single letter, '&', blank). Use the email "
        "local-part as a tiebreaker. Use a name in the greeting ONLY if it's clearly a real "
        "given name, fixing capitalization ('ILISE'->'Ilise'). Otherwise open 'Hi there,'.\n\n"
        "Do NOT include any call-to-action line, link, scheduler, or sign-off/signature — those "
        "are added afterward. End the body on the last real sentence.\n\n"
        "Return ONLY a JSON array (no markdown) with one object per contact IN ORDER, keys: "
        "subject, body, clean_first_name, clean_last_name. subject <= 8 words, conversational. "
        "clean_first_name = the corrected given name you used, or \"\" if you opened 'Hi there'."
    )
    payload_contacts = []
    for i, c in enumerate(contacts):
        payload_contacts.append({
            "index": i, "variant": c.get("variant", "A"), "touch": c.get("touch", 1),
            "addressType": "shared/role inbox" if c.get("is_role") else "individual",
            "firstName": c.get("firstName", ""), "lastName": c.get("lastName", ""),
            "email": c.get("email", ""), "school": c.get("company") or "their school",
        })
    user = ("Write emails for these contacts:\n\n" + json.dumps(payload_contacts, indent=2))
    max_tok = min(16000, 400 * len(contacts) + 500)
    try:
        text = _anthropic({"model": GEN_MODEL, "max_tokens": max_tok,
                           "system": system, "messages": [{"role": "user", "content": user}]})
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        results = json.loads(cleaned)
        if not isinstance(results, list):
            raise ValueError("expected array")
        while len(results) < len(contacts):
            results.append(_fallback(contacts[len(results)]))
        return results[:len(contacts)]
    except Exception as e:
        log.warning(f"  generation failed ({e}) — fallbacks for {len(contacts)} contacts")
        return [_fallback(c) for c in contacts]


def cta_block(variant, touch):
    """Return (plain_suffix, html_suffix) for the call-to-action, by variant & touch."""
    if touch == 2:   # book offer: invite a reply, keep the arm's link as a soft option
        plain = ("\n\nWant a copy of All Clear? Just reply and I'll send it over, to read or "
                 "listen, on us. Happy to mail a physical copy if you send an address.")
        html = ("<br><br>Want a copy of <em>All Clear</em>? Just reply and I'll send it over, "
                "to read or listen, on us. Happy to mail a physical copy if you send an address.")
        return plain, html
    if variant == "B":
        plain = (f"\n\nIf you're curious, the free 5-minute assessment is here: {ASSESSMENT_LINK}\n"
                 f"Or if you'd rather talk it through first, Colleen has a little time here: {COLLEEN_LINK}")
        html = (f'<br><br>If you\'re curious, the free 5-minute assessment is '
                f'<a href="{html_escape(ASSESSMENT_LINK, quote=True)}">here</a>.<br>'
                f'<span style="color:#666">Or if you\'d rather talk it through first, '
                f'<a href="{html_escape(COLLEEN_LINK, quote=True)}">Colleen has a little time here</a>.</span>')
        return plain, html
    plain = f"\n\nIf it would ever help to talk something through, Colleen keeps a little time open here: {COLLEEN_LINK}"
    html = (f'<br><br>If it would ever help to talk something through, '
            f'<a href="{html_escape(COLLEEN_LINK, quote=True)}">Colleen keeps a little time open here</a>.')
    return plain, html


# Belt-and-suspenders: even with the prompt rules, Haiku sometimes emits AI "tells".
# We deterministically strip them post-generation so they can never reach a recipient:
#   • em/en dashes (replaced with commas)
#   • whole sentences that defensively deny selling, or use doom/scare framing
_TELL_RE = re.compile(
    r"not a (sales )?(tool|pitch|sales pitch)|not selling|no catch|no strings|"
    r"no obligation|isn'?t a (sales )?pitch|this is ?n'?t a pitch|"
    r"before it'?s too late|hope it (all )?holds", re.IGNORECASE)


def scrub_body(text):
    text = re.sub(r"\s*—\s*", ", ", text or "")
    text = re.sub(r"\s*–\s*", "-", text)
    out_lines = []
    for line in text.split("\n"):
        if not line.strip():
            out_lines.append("")
            continue
        sents = re.split(r"(?<=[.!?])\s+", line.strip())
        kept = [s for s in sents if not _TELL_RE.search(s)]
        out_lines.append(" ".join(kept))
    out = "\n".join(out_lines)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)   # no space before punctuation
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def assemble(ed, contact, sender_name):
    """Build (subject, plain, html) from the generated body + CTA + signature."""
    variant, touch = contact.get("variant", "A"), contact.get("touch", 1)
    subject = (ed.get("subject") or _fallback(contact)["subject"]).replace("—", ": ").replace("–", "-")
    body = scrub_body(ed.get("body") or _fallback(contact)["body"]).rstrip()
    cta_p, cta_h = cta_block(variant, touch)
    sig_p = f"\n\n{sender_name}\n{COMPANY_NAME}"
    sig_h = f"<br><br>{html_escape(sender_name)}<br>{COMPANY_NAME}"
    plain = body + cta_p + sig_p
    html = html_escape(body).replace("\n", "<br>\n") + cta_h + sig_h
    return subject, plain, html


# ─── SMTP send (per persona) ──────────────────────────────────────────────────
def send_email(persona, to, subject, plain, html=None, cc=None, bcc=None):
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    user = persona["email"]
    pw = os.environ.get(persona["pass_env"], "")
    if not pw:
        return {"success": False, "error": f"{persona['pass_env']} not set"}

    msg = MIMEMultipart("alternative") if html else MIMEMultipart()
    msg["From"] = f"{persona['name']} <{user}>"
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    msg.attach(MIMEText(plain, "plain"))
    if html:
        msg.attach(MIMEText(html, "html"))
    # bcc is added to the envelope recipients only — never as a header (that's the point).
    recipients = [to] + ([cc] if cc else []) + ([bcc] if bcc else [])
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=_ssl_ctx()) as s:
            s.login(user, pw)
            s.sendmail(user, recipients, msg.as_string())
        return {"success": True}
    except smtplib.SMTPRecipientsRefused as e:
        return {"success": False, "error": str(e), "hard_bounce": True}
    except Exception as e:
        err = str(e)
        hard = "550" in err or "5.7.1" in err or "does not exist" in err
        return {"success": False, "error": err, "hard_bounce": hard}


def notify_chris(error_msg, context=""):
    try:
        send_email(PERSONAS[0], CHRIS_EMAIL, f"[Joffe SDR ERROR] {today_str()}",
                   f"Hi Chris,\n\nThe Joffe SDR agent hit an error.\n\n"
                   f"Error: {error_msg}\nContext: {context or 'see log'}\n\n—Joffe SDR (automated)")
    except Exception as e:
        log.error(f"could not notify Chris: {e}")


# ─── DAILY SEND ───────────────────────────────────────────────────────────────
def run_daily(dry_run=False, limit=None):
    state = load_state()
    today = today_str()
    if not dry_run and state.get("last_daily_run") != today:
        state["last_daily_run"] = today
        save_state(state)

    # Explicit --limit overrides for controlled tests / small live batches. Otherwise
    # the ramp cap applies, and a cap of 0 (no JOFFE_LAUNCH_DATE) means fully paused.
    if limit is not None:
        cap = limit
    else:
        cap = daily_cap()
        if cap == 0:
            log.info("Paused — set JOFFE_LAUNCH_DATE (repo variable) to go live. No sends this run.")
            return
    already = state.get("daily_sent_count", {}).get(today, 0)
    if already >= cap:
        log.info(f"Daily cap reached ({already}/{cap}). Exiting.")
        return
    remaining = cap - already
    log.info(f"Daily cap {cap} (week {weeks_since_launch()}), {already} already sent, "
             f"{remaining} to go.")

    try:
        hs.ensure_properties(HUBSPOT_TOKEN)

        # 1 — follow-ups first (warmer, time-sensitive), then new contacts to fill the cap
        followups = hs.fetch_followups(HUBSPOT_TOKEN, remaining, pacific_today(),
                                       FOLLOWUP_GAP_DAYS, MAX_TOUCHES, FOLLOWUP_STALE_DAYS)
        new_needed = remaining - len(followups)
        news = hs.fetch_new(HUBSPOT_TOKEN, new_needed) if new_needed > 0 else []
        candidates = followups + news
        if not candidates:
            log.info("Nothing due: no follow-ups and no eligible new contacts.")
            return
        log.info(f"Queued {len(followups)} follow-ups + {len(news)} new = {len(candidates)}")

        # 2 — assign variant (stable per contact; follow-ups keep their stored arm) +
        #     tag role inboxes. Placeholder/garbage addresses are dropped.
        queue = []
        for c in candidates:
            if not c["email"] or c["email"].lower() in PLACEHOLDER_ADDRESSES:
                continue
            c["variant"] = c.get("variant") or variant_for(c["email"])
            c["is_role"] = is_role_address(c["email"])
            queue.append(c)

        # 2b — CUSTOMER GATE (new contacts): never cold-email an account HubSpot shows as
        #      a current/former customer or active opportunity — checked at company +
        #      deal level, since a contact can read 'subscriber' while their school is a
        #      customer. Skipped contacts are stamped so they never resurface.
        gated = 0
        survivors = []
        for c in queue:
            if c.get("touch", 1) == 1:
                skip, reason = hs.customer_gate(HUBSPOT_TOKEN, c)
                if skip:
                    gated += 1
                    log.info(f"  gated {redact_email(c['email'])} — {reason}")
                    if not dry_run:
                        hs.stamp(HUBSPOT_TOKEN, c["id"], status="Skipped-Customer",
                                 agent=f"skipped: {reason}"[:100])
                    continue
            survivors.append(c)
        queue = survivors
        if gated:
            log.info(f"Customer gate: {gated} skipped, {len(queue)} remain.")
        if not queue:
            log.info("Nothing to send after the customer gate.")
            return

        # 3 — generate in batches
        log.info(f"Generating {len(queue)} emails (batches of {GENERATION_BATCH_SIZE})...")
        generated = []
        for i in range(0, len(queue), GENERATION_BATCH_SIZE):
            generated.extend(generate_batch(queue[i:i + GENERATION_BATCH_SIZE]))

        # 4 — send, round-robin the mailbox, log to HubSpot, stamp the ledger
        sent_today = already
        cursor = state.get("persona_cursor", 0)
        for i, (c, ed) in enumerate(zip(queue, generated)):
            persona = PERSONAS[cursor % len(PERSONAS)]
            cursor += 1                       # advance per attempt → true round-robin
            touch, variant, cid = c["touch"], c["variant"], c["id"]
            subject, plain, html = assemble(ed, c, persona["name"])
            clean_first = (ed.get("clean_first_name") or "").strip()
            clean_last = (ed.get("clean_last_name") or "").strip()

            log.info(f"Send {i+1}/{len(queue)} [{persona['key']}] "
                     f"variant {variant} touch {touch} → {redact_email(c['email'])}: {subject}")
            if dry_run:
                if i < 3:   # show a couple of full samples in the dry-run log
                    log.info(f"  --- SAMPLE ---\n{plain}\n  --------------")
                continue

            res = send_email(persona, c["email"], subject, plain, html=html,
                             bcc=os.environ.get("OUTREACH_BCC") or None)
            if res.get("success"):
                sent_today += 1
                ts_ms = int(time.time() * 1000)
                hs.log_email(HUBSPOT_TOKEN, cid, subject, plain,
                             persona["name"], persona["email"], c["email"], ts_ms)
                name_props = {}
                if clean_first and clean_first.lower() != (c["firstName"] or "").lower():
                    name_props["firstname"] = clean_first
                if clean_last and clean_last.lower() != (c["lastName"] or "").lower():
                    name_props["lastname"] = clean_last
                hs.stamp(HUBSPOT_TOKEN, cid, status="Contacted", touches=touch,
                         last_touch=today, variant=variant, agent=persona["name"], **name_props)
                if touch == 1:
                    _bump(state, "daily_new_count")
                _bump(state, "daily_sent_count")
                _bump_variant(state, variant)
            elif res.get("hard_bounce"):
                log.warning(f"  hard bounce {redact_email(c['email'])}")
                hs.stamp(HUBSPOT_TOKEN, cid, status="Bounced", touches=touch, last_touch=today,
                         variant=variant, agent=persona["name"])
                _bump(state, "daily_bounce_count")
            else:
                log.error(f"  send failed: {res.get('error')}")
                notify_chris(f"Send failed to {redact_email(c['email'])}", str(res.get("error")))

            state["persona_cursor"] = cursor
            save_state(state)
            if i < len(queue) - 1:
                time.sleep(random.randint(MIN_DELAY_SEC, MAX_DELAY_SEC))

        log.info(f"Daily complete. {sent_today - already} sent this run ({sent_today}/{cap} today).")
        if not dry_run:
            run_report(dry_run=False, triggered_by_daily=True)
    except Exception as e:
        log.exception(f"Fatal: {e}")
        notify_chris(str(e), f"daily run failed {today}")
        sys.exit(1)


# ─── REPLY TRIAGE ─────────────────────────────────────────────────────────────
def parse_from(sender):
    m = re.search(r"<([^>]+)>", sender)
    email = (m.group(1) if m else sender).strip().lower()
    name = re.sub(r"<[^>]+>", "", sender).strip().strip('"')
    return ("" if "@" in name else name), email


def _extract_phone(text):
    m = re.search(r'(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}', text or "")
    return m.group(0).strip() if m else ""


def classify_reply(sender, subject, body):
    s, sub, b = sender.lower(), subject.lower(), (body or "").lower()
    if any(k in s for k in ["mailer-daemon", "postmaster", "mail delivery"]):
        return "bounce"
    if any(k in sub for k in ["undeliverable", "delivery status notification",
                              "delivery failed", "returned mail", "failed to deliver",
                              "unable to deliver"]):
        return "bounce"
    if any(k in sub for k in ["out of office", "auto-reply", "automatic reply",
                              "on vacation", "ooo:", "annual leave", "on leave"]):
        return "ooo"
    if any(k in b[:300] for k in ["i am currently out", "i'm currently out", "i am away",
                                  "automatic reply", "automated response", "out of the office"]):
        return "ooo"
    unsub = ["unsubscribe", "remove me", "remove us", "opt out", "opt-out", "take me off",
             "take us off", "please remove", "no longer wish", "do not contact",
             "stop emailing", "stop contacting", "please stop"]
    if any(k in sub for k in unsub) or any(k in b[:400] for k in unsub):
        return "unsubscribe"
    return "genuine"


def classify_interest(subject, body):
    """LLM triage of a genuine reply → (auto, opt_out, interested, reason).
    interested = any real curiosity / willingness to talk or learn more (broad, per Chris)."""
    try:
        text = _anthropic({
            "model": GEN_MODEL, "max_tokens": 300,
            "system": ("Classify a reply to a cold school-safety outreach email. Return ONLY "
                       "JSON: {\"auto\":bool,\"opt_out\":bool,\"interested\":bool,\"reason\":\"...\"}. "
                       "auto=true if it's an automated/OOO/no-reply bounce-back. opt_out=true if "
                       "they ask to stop/unsubscribe. interested=true for ANY genuine curiosity, "
                       "question, or willingness to talk/learn more (be generous). reason = a short "
                       "phrase."),
            "messages": [{"role": "user", "content": f"Subject: {subject}\n\n{body[:1500]}"}],
        }, timeout=60).strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:-1])
        d = json.loads(text)
        return bool(d.get("auto")), bool(d.get("opt_out")), bool(d.get("interested")), d.get("reason", "")
    except Exception as e:
        log.warning(f"  interest classify failed ({e}) — treating as interested")
        return False, False, True, "classify error (defaulted to interested)"


def _clean_name(name):
    if not name:
        return ""
    def fix(tok):
        return tok[:1].upper() + tok[1:].lower() if tok and (tok.isupper() or tok.islower()) else tok
    out = []
    for word in name.split():
        parts = re.split(r"([-'])", word)
        out.append("".join(p if p in "-'" else fix(p) for p in parts))
    return " ".join(out).strip()


def _check_mailbox(persona, state, dry_run):
    import imaplib
    import email as emaillib
    pw = os.environ.get(persona["pass_env"], "")
    if not pw:
        log.info(f"  [{persona['key']}] no app password — skipping")
        return
    log.info(f"Checking {persona['key']} inbox for replies...")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", ssl_context=_ssl_ctx())
        mail.login(persona["email"], pw)
        mail.select("INBOX")
        _, ids = mail.search(None, "UNSEEN")
        mids = ids[0].split() if ids and ids[0] else []
        log.info(f"  {len(mids)} unread")
        for mid in mids:
            try:
                _, data = mail.fetch(mid, "(RFC822)")
                msg = emaillib.message_from_bytes(data[0][1])
                sender = msg.get("From", "Unknown")
                subject = msg.get("Subject", "")
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="replace")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors="replace")
                _, sender_email = parse_from(sender)
                auto_hdr = (msg.get("Auto-Submitted", "") or "").lower()
                is_auto = ("auto-replied" in auto_hdr or "auto-generated" in auto_hdr
                           or bool(msg.get("X-Autoreply")) or bool(msg.get("X-Autorespond")))
                kind = classify_reply(sender, subject, body)
                cid = hs.find_contact(HUBSPOT_TOKEN, sender_email) if sender_email else ""

                if kind in ("bounce", "ooo"):
                    log.info(f"  {kind} from {redact_email(sender_email)} — archiving")
                    if kind == "bounce" and cid and not dry_run:
                        hs.stamp(HUBSPOT_TOKEN, cid, status="Bounced")
                        _bump(state, "daily_bounce_count")
                    if not dry_run:
                        _archive(mail, mid)
                    continue
                if kind == "unsubscribe" or (kind == "genuine" and _is_optout(subject, body)):
                    log.info(f"  opt-out from {redact_email(sender_email)} — suppressing")
                    if cid and not dry_run:
                        hs.stamp(HUBSPOT_TOKEN, cid, status="Unsubscribed")
                        _bump(state, "daily_unsub_count")
                    if not dry_run:
                        _archive(mail, mid)
                    continue
                if is_auto:
                    if not dry_run:
                        _archive(mail, mid)
                    continue

                # genuine reply → interest triage
                auto, opt_out, interested, reason = classify_interest(subject, body)
                if auto:
                    if not dry_run:
                        _archive(mail, mid)
                    continue
                if opt_out:
                    if cid and not dry_run:
                        hs.stamp(HUBSPOT_TOKEN, cid, status="Unsubscribed")
                        _bump(state, "daily_unsub_count")
                    if not dry_run:
                        _archive(mail, mid)
                    continue

                sname, _ = parse_from(sender)
                first = _clean_name(sname.split()[0]) if sname else ""
                last = _clean_name(sname.split()[-1]) if len(sname.split()) > 1 else ""
                phone = _extract_phone(body)
                _bump(state, "daily_reply_count")

                if interested:
                    log.info(f"  SQL from {redact_email(sender_email)} ({reason}) → HubSpot + Colleen")
                    if not dry_run:
                        new_cid = hs.upsert_lead(HUBSPOT_TOKEN, sender_email,
                                                 "salesqualifiedlead", first, last, phone=phone,
                                                 agent_name=persona["name"],
                                                 owner_id=COLLEEN_OWNER_ID, stamp_source=True)
                        if new_cid:
                            hs.stamp(HUBSPOT_TOKEN, new_cid, status="SQL")
                        _bump(state, "daily_sql_count")
                        _notify_colleen(persona, sender_email, first, last, body, reason,
                                        hs.contact_link(HUBSPOT_TOKEN, new_cid), is_sql=True)
                        _archive(mail, mid)
                else:
                    log.info(f"  genuine reply (not SQL) from {redact_email(sender_email)} → MQL + Colleen")
                    if not dry_run:
                        new_cid = hs.upsert_lead(HUBSPOT_TOKEN, sender_email,
                                                 "marketingqualifiedlead", first, last,
                                                 agent_name=persona["name"])
                        if new_cid:
                            hs.stamp(HUBSPOT_TOKEN, new_cid, status="Replied")
                        _bump(state, "daily_mql_count")
                        _notify_colleen(persona, sender_email, first, last, body, reason,
                                        hs.contact_link(HUBSPOT_TOKEN, new_cid), is_sql=False)
                        _archive(mail, mid)
            except Exception as e:
                log.warning(f"  error on message: {e}")
                continue
        mail.logout()
    except Exception as e:
        log.error(f"  reply check error [{persona['key']}]: {e}")


def _is_optout(subject, body):
    unsub = ["unsubscribe", "remove me", "opt out", "opt-out", "take me off",
             "stop emailing", "stop contacting", "do not contact"]
    return any(k in (subject + " " + body[:400]).lower() for k in unsub)


def _archive(mail, mid):
    mail.store(mid, "+FLAGS", "\\Seen")
    mail.store(mid, "-X-GM-LABELS", "\\Inbox")


def _notify_colleen(persona, email, first, last, body, reason, link, is_sql):
    name = (first + " " + last).strip() or email
    tag = "interested — SQL" if is_sql else "replied — worth a look"
    subject = f"{name} — {tag}"
    hs_line = f"HubSpot: {link}\n\n" if link else ""
    intro = (f"{name} replied to our school-safety outreach and looks like a real lead"
             if is_sql else f"{name} just replied to our outreach — flagging for you")
    body_out = (f"Hi Colleen,\n\n{intro} ({reason}).\n\n{hs_line}"
                f"Here's what they said:\n\n---\n{body[:1200]}\n---\n\n"
                f"{'Assigned to you in HubSpot. ' if is_sql else ''}Thanks!\n{persona['name']}")
    send_email(persona, COLLEEN_EMAIL, subject, body_out, cc=CHRIS_EMAIL)


def run_reply_check(dry_run=False):
    state = load_state()
    # self-heal: if today's daily send never ran (dropped cron), run it first
    if not dry_run and state.get("last_daily_run") != today_str():
        log.info("Daily send hasn't run today — self-healing before reply check.")
        run_daily(dry_run=False)
        state = load_state()
    for persona in PERSONAS:
        _check_mailbox(persona, state, dry_run)
    if not dry_run:
        save_state(state)


# ─── DASHBOARD / REPORT ───────────────────────────────────────────────────────
def _count(status=None, variant=None):
    filters = [{"propertyName": "organization_type_", "operator": "EQ", "value": hs.SCHOOL_TYPE}]
    if status:
        filters.append({"propertyName": "joffe_outreach_status", "operator": "EQ", "value": status})
    if variant:
        filters.append({"propertyName": "joffe_outreach_variant", "operator": "EQ", "value": variant})
    st, data = hs._request("POST", f"{hs.BASE}/crm/v3/objects/contacts/search", HUBSPOT_TOKEN,
                           {"filterGroups": [{"filters": filters}], "limit": 1})
    return data.get("total", 0) if st == 200 else 0


def _sum_today(state, key):
    return state.get(key, {}).get(today_str(), 0)


def _sum_recent(counts_by_date, days):
    from datetime import timedelta
    t = pacific_today()
    return sum(counts_by_date.get((t - timedelta(days=i)).isoformat(), 0) for i in range(days))


def _sql_emails(limit=50):
    """Emails of contacts currently at SQL status — the QA list Colleen should have."""
    st, d = hs._request("POST", f"{hs.BASE}/crm/v3/objects/contacts/search", HUBSPOT_TOKEN, {
        "filterGroups": [{"filters": [
            {"propertyName": "organization_type_", "operator": "EQ", "value": hs.SCHOOL_TYPE},
            {"propertyName": "joffe_outreach_status", "operator": "EQ", "value": "SQL"}]}],
        "properties": ["email"], "limit": limit})
    return [r.get("properties", {}).get("email", "") for r in (d.get("results", []) if st == 200 else [])]


def run_report(dry_run=False, triggered_by_daily=False):
    """Email Chris + Colleen an HTML dashboard: volume, replies, SQLs, deliverability,
    and the A/B (Membership vs Assessment) comparison. Today + 7-day from state counters,
    Lifetime live from HubSpot."""
    state = load_state()
    today = today_str()
    try:
        dsc = state.get("daily_sent_count", {})
        sent_total = sum(dsc.values())
        # Lifetime, live from HubSpot (status = the contact's latest state)
        c_contacted = _count("Contacted"); c_replied = _count("Replied"); c_sql = _count("SQL")
        c_bounced = _count("Bounced"); c_unsub = _count("Unsubscribed"); c_skip = _count("Skipped-Customer")
        reached = c_contacted + c_replied + c_sql + c_bounced + c_unsub
        replies = c_replied + c_sql          # MQL-path stamps 'Replied', SQL-path stamps 'SQL'
        mql = c_replied
        ab = {v: {"reached": _count(variant=v), "sql": _count("SQL", v),
                  "mql": _count("Replied", v),
                  "replied": _count("Replied", v) + _count("SQL", v)} for v in ("A", "B")}
        sql_list = _sql_emails()

        def td(k): return _sum_today(state, k)
        def wk(k): return _sum_recent(state.get(k, {}), 7)
        def pct(n, d): return f"{100.0 * n / d:.1f}%" if d else "—"

        rows = [
            ("Emails sent (all touches)", td("daily_sent_count"), wk("daily_sent_count"), sent_total, False),
            ("New schools reached",       td("daily_new_count"),   wk("daily_new_count"),   reached, False),
            ("Replies",                   td("daily_reply_count"), wk("daily_reply_count"), replies, False),
            ("SQLs &rarr; Colleen",       td("daily_sql_count"),   wk("daily_sql_count"),   c_sql, True),
            ("MQLs",                      td("daily_mql_count"),   wk("daily_mql_count"),   mql, False),
            ("Hard bounces",              td("daily_bounce_count"),wk("daily_bounce_count"),c_bounced, False),
            ("Unsubscribes",              td("daily_unsub_count"), wk("daily_unsub_count"), c_unsub, False),
        ]
        subject = (f"[Joffe SDR] {today} — {td('daily_sql_count')} SQLs today, "
                   f"{td('daily_sent_count')} sent (cap {daily_cap()})")

        # Plain-text fallback
        def pr(l, a, b, c): return f"  {l:<26}{a:>8,}{b:>10,}{c:>12,}\n"
        body = (
            f"Joffe School-Safety SDR dashboard\nAs of {today}\n{'=' * 58}\n\n"
            f"  {'':<26}{'TODAY':>8}{'7 DAYS':>10}{'LIFETIME':>12}\n  {'-' * 56}\n"
            + "".join(pr(l.replace('&rarr;', '->'), a, b, c) for l, a, b, c, _ in rows)
            + f"\n  Lifetime reply rate: {pct(replies, reached)}   SQL rate: {pct(c_sql, reached)}\n"
            + f"  Customers shielded (QB + gate): {c_skip:,}\n\n"
            f"A/B (lifetime)\n"
            f"  A Membership:  reached {ab['A']['reached']:,}, replies {ab['A']['replied']}, "
            f"MQL {ab['A']['mql']}, SQL {ab['A']['sql']}, engaged {pct(ab['A']['replied'], ab['A']['reached'])}\n"
            f"  B Assessment:  reached {ab['B']['reached']:,}, replies {ab['B']['replied']}, "
            f"MQL {ab['B']['mql']}, SQL {ab['B']['sql']}, engaged {pct(ab['B']['replied'], ab['B']['reached'])}\n\n"
            f"Sales-Qualified Leads ({len(sql_list)}):\n"
            + (("\n".join(f"  - {e}" for e in sql_list)) if sql_list else "  (none yet)")
            + f"\n\nSenders: Jessica Dean + Ryan Andrews (round-robin). Week {weeks_since_launch()}.\n"
            f"-Joffe SDR (automated)"
        )

        def hr(l, a, b, c, hi):
            bg = " background:#f2f8f2;" if hi else ""
            return (f"<tr style='border-top:1px solid #e5e5e5;{bg}'>"
                    f"<td style='padding:6px 14px'>{l}</td>"
                    f"<td align='right' style='padding:6px 14px'>{a:,}</td>"
                    f"<td align='right' style='padding:6px 14px'>{b:,}</td>"
                    f"<td align='right' style='padding:6px 14px;font-weight:600'>{c:,}</td></tr>")

        def abrow(name, v):
            return (f"<tr style='border-top:1px solid #e5e5e5'><td style='padding:6px 14px'>{name}</td>"
                    f"<td align='right' style='padding:6px 14px'>{v['reached']:,}</td>"
                    f"<td align='right' style='padding:6px 14px'>{v['replied']}</td>"
                    f"<td align='right' style='padding:6px 14px'>{v['mql']}</td>"
                    f"<td align='right' style='padding:6px 14px'>{v['sql']}</td>"
                    f"<td align='right' style='padding:6px 14px'>{pct(v['replied'], v['reached'])}</td></tr>")

        html = (
            "<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222'>"
            "<h2 style='margin:0 0 2px'>Joffe School-Safety SDR</h2>"
            f"<div style='color:#888;margin-bottom:14px'>Jessica Dean &amp; Ryan Andrews &middot; "
            f"as of {today} &middot; daily cap {daily_cap()} (week {weeks_since_launch()})</div>"
            "<table style='border-collapse:collapse;font-size:14px;min-width:440px'>"
            "<thead><tr style='background:#eee'><th align='left' style='padding:8px 14px'>&nbsp;</th>"
            "<th align='right' style='padding:8px 14px'>Today</th><th align='right' style='padding:8px 14px'>7 days</th>"
            "<th align='right' style='padding:8px 14px'>Lifetime</th></tr></thead><tbody>"
            + "".join(hr(l, a, b, c, hi) for l, a, b, c, hi in rows)
            + "</tbody></table>"
            f"<p style='margin:14px 0 4px'><b>Lifetime reply rate:</b> {pct(replies, reached)} &nbsp;&nbsp;"
            f"<b>SQL rate:</b> {pct(c_sql, reached)} &nbsp;&nbsp;"
            f"<b>Customers shielded (QB + gate):</b> {c_skip:,}</p>"
            "<h3 style='margin:18px 0 4px'>A/B test (lifetime)</h3>"
            "<table style='border-collapse:collapse;font-size:14px;min-width:440px'>"
            "<thead><tr style='background:#eee'><th align='left' style='padding:8px 14px'>Arm</th>"
            "<th align='right' style='padding:8px 14px'>Reached</th><th align='right' style='padding:8px 14px'>Replies</th>"
            "<th align='right' style='padding:8px 14px'>MQL</th><th align='right' style='padding:8px 14px'>SQL</th>"
            "<th align='right' style='padding:8px 14px'>Engaged</th></tr></thead><tbody>"
            + abrow("A &middot; Membership", ab["A"]) + abrow("B &middot; Assessment", ab["B"])
            + "</tbody></table>"
            f"<p style='margin:14px 0 4px'><b>Sales-Qualified Leads ({len(sql_list)})</b> (with Colleen):</p>"
            + ("<ul style='margin:4px 0 0;padding-left:22px'>" + "".join(f"<li>{e}</li>" for e in sql_list)
               + "</ul>" if sql_list else "<p style='color:#888'>(none yet)</p>")
            + "<p style='color:#999;font-size:12px;max-width:560px'>Today &amp; 7-day build over time; "
            "Lifetime is live from HubSpot. Follow-ups (SellingSara 4-touch: day 3 / 7 / 14) send before "
            "new contacts each day.</p></div>"
        )

        if dry_run:
            log.info("[DRY RUN] dashboard (plain):\n" + body)
            return
        res = send_email(PERSONAS[0], REPORT_TO[0], subject, body, html=html, cc=REPORT_TO[1])
        state["last_report_run"] = today
        save_state(state)
        log.info("Report sent to Chris + Colleen." if res.get("success")
                 else f"Report send failed: {res.get('error')}")
    except Exception as e:
        log.exception(f"report failed: {e}")


# ─── SETUP (one-time / idempotent) ────────────────────────────────────────────
def run_test_send():
    """Send ONE real outreach-style email from Jessica to TEST_EMAIL_TO so a human can
    reply and exercise the reply pipeline. Touches no HubSpot data."""
    to = os.environ.get("TEST_EMAIL_TO", "").strip()
    if not to:
        log.error("TEST_EMAIL_TO not set."); return
    persona = PERSONAS[0]   # Jessica — reply lands in her inbox
    first = to.split("@")[0].split(".")[0].split("+")[0].title() or "there"
    c = {"variant": "A", "touch": 1, "firstName": first, "company": "your school", "is_role": False}
    subject, plain, html = assemble(_fallback(c), c, persona["name"])
    res = send_email(persona, to, subject, plain, html=html)
    log.info(f"Test email to {redact_email(to)} from {persona['email']}: "
             f"{'SENT' if res.get('success') else res.get('error')} | subject: {subject}")


def run_setup():
    log.info("Ensuring HubSpot custom properties exist...")
    created = hs.ensure_properties(HUBSPOT_TOKEN)
    log.info(f"  created: {created or 'none (all present)'}")
    sp = hs._resolve_source_prop(HUBSPOT_TOKEN)
    log.info(f"  traffic-source attribution → {sp}")
    log.info(f"  portal id: {hs.portal_id(HUBSPOT_TOKEN)}")


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Joffe School-Safety SDR agent")
    ap.add_argument("--mode", required=True,
                    choices=["setup", "daily", "reply_check", "report", "test"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force-weekday", action="store_true")   # accepted for parity; unused
    args = ap.parse_args()

    if not HUBSPOT_TOKEN:
        log.error("HUBSPOT_TOKEN not set."); sys.exit(1)
    if args.mode in ("daily", "reply_check") and not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set."); sys.exit(1)

    if args.mode == "test":
        run_test_send()
    elif args.mode == "setup":
        run_setup()
    elif args.mode == "daily":
        run_daily(dry_run=args.dry_run, limit=args.limit)
    elif args.mode == "reply_check":
        run_reply_check(dry_run=args.dry_run)
    elif args.mode == "report":
        run_report(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
