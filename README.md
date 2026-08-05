# Joffe School-Safety SDR — Outreach Agent

Autonomous outbound SDR for Joffe's school-safety line. Sibling of the Get CPR Done
"Vida" agent, rebuilt around the **Joffe HubSpot portal** as the single source of
truth (no Google Sheet).

## What it does

| Mode | When | What happens |
|------|------|--------------|
| `daily` | M–F 9:00 AM PT | Follow-ups due today, then new eligible School contacts, up to the daily cap. Round-robins the two mailboxes, A/B-assigns each contact, sends, logs the email onto the HubSpot timeline, and stamps the tracking properties. Fires the report at the end. |
| `reply_check` | M–F every 2h (9–3 PT) | Reads both inboxes. Booking interest → **SQL** (owner = Colleen, notify Colleen + CC Chris). Other genuine reply → **MQL**. Opt-out → **Unsubscribed**. Bounce → **Bounced**. Self-heals the daily send if its cron dropped. |
| `report` | end of `daily` | Emails Chris + Colleen today's numbers + a lifetime A/B comparison pulled live from HubSpot. |
| `setup` | once / manual | Creates the custom HubSpot properties and verifies token scope + traffic-source attribution. |

## Senders (round-robin, one clean domain)

- **Jessica Dean** — `jessicad@joffeschoolsafety.com`
- **Ryan Andrews** — `ryana@joffeschoolsafety.com`

## Audience

HubSpot contacts where **Relationship Type = School**, **subscribers first then leads**.
Anyone at MQL or above is excluded — so we never message an SQL or higher. "Already
contacted" is simply `joffe_outreach_status` having any value, so each run naturally
returns the next uncontacted slice (no cursor to maintain).

## A/B test

Assigned 50/50 per contact (stable per email); the **mailbox round-robins independently**
so sender reputation doesn't confound the message test.

- **Variant A — Membership (AAA analogy)** → CTA: book time with Colleen
- **Variant B — Assessment (Swiss Cheese)** → CTA: free self-serve assessment (Colleen link as P.S.)

4-touch cadence: opener → *All Clear* book offer → value/proof → gracious breakup.

## HubSpot writes

- **Ledger** (custom props, auto-created by `setup`): `joffe_outreach_status`,
  `joffe_outreach_touches`, `joffe_last_touch_date`, `joffe_outreach_variant`,
  `joffe_outreach_agent`.
- **Activity**: every send is logged as an email engagement on the contact's timeline
  (direct API — no BCC, no browser extension).
- **Attribution**: on a *new* contact created from a reply, Original Source is stamped
  `AI Referrals` with drill-down = the SDR name. Existing contacts' original source is
  never overwritten.

## Volume ramp (combined across both mailboxes)

Set repo **variable** `JOFFE_LAUNCH_DATE` (YYYY-MM-DD) to start the clock:

| Weeks since launch | Daily cap |
|---|---|
| 0 | 50 |
| 1 | 250 |
| 2 | 500 |
| 3 | 900 |
| 4+ | 1000 |

Until `JOFFE_LAUNCH_DATE` is set, the cap stays at the 50/day warm-up floor.

## Secrets (GitHub → Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic key (generation + reply triage; Haiku) |
| `HUBSPOT_TOKEN` | Joffe private-app token — scopes: contacts read/write, deals read, CRM email/engagement write |
| `JESSICA_APP_PASSWORD` | Google Workspace app password for jessicad@joffeschoolsafety.com |
| `RYAN_APP_PASSWORD` | Google Workspace app password for ryana@joffeschoolsafety.com |

Repo **variable**: `JOFFE_LAUNCH_DATE`.

## Local dry-run

```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
export HUBSPOT_TOKEN=...        # or put secrets in a gitignored .env
export ANTHROPIC_API_KEY=...
export JESSICA_APP_PASSWORD=... RYAN_APP_PASSWORD=...

python outreach_agent.py --mode setup                       # create props, verify scopes
python outreach_agent.py --mode daily --dry-run --limit 5   # real contacts + real copy, NO sends
python outreach_agent.py --mode reply_check --dry-run       # triage read-only
python outreach_agent.py --mode report --dry-run
```

Public repo → logs redact recipient addresses (`j***@domain`) and `state.json` holds
no PII (all contact status lives in HubSpot).

## Notes / follow-ups

- **Global unsubscribe**: opt-outs set `joffe_outreach_status = Unsubscribed` (stops this
  agent). Wiring HubSpot's portal-wide email opt-out / subscription suppression is a
  follow-up.
- **Cross-agent cooldown with Vida (GCD)**: not yet wired — dedupe/cooldown so the same
  person isn't hit by both brands at once is a future integration.
- **Deliverability**: confirm SPF/DKIM/DMARC on `joffeschoolsafety.com` before ramping.
