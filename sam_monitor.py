"""
SAM.gov PEP Opportunity Monitor
Polls SAM.gov for new/updated Procurement for Experimental Purposes notices and posts to Teams.
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config from environment ──────────────────────────────────────────────────
SAM_API_KEY       = os.environ["SAM_API_KEY"]
TEAMS_WEBHOOK_URL = os.environ["TEAMS_WEBHOOK_URL"]
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY")
LOOKBACK_HOURS    = int(os.environ.get("LOOKBACK_HOURS", "26"))
STATE_FILE        = Path(os.environ.get("STATE_FILE", "state.json"))
DRY_RUN           = os.environ.get("DRY_RUN", "false").lower() == "true"  # log only, no individual cards

SAM_OPPORTUNITIES_URL = "https://api.sam.gov/opportunities/v2/search"

# ── PEP relevance terms ──────────────────────────────────────────────────────
# The SAM.gov API does not support phrase matching — keyword searches return
# everything in the date window. We fetch ALL records and filter by title instead.
PEP_TITLE_TERMS = [
    "PROCUREMENT FOR EXPERIMENTAL PURPOSES",
    "PROCUREMENT EXPERIMENTAL PURPOSES",
    "EXPERIMENTAL PROCUREMENT",
    " PEP ",
    "(PEP)",
    "PEP:",
    "PEP-",
    "/PEP",
    "EXPERIMENTAL PURPOSES",
]

# ── Branch and set-aside helpers ─────────────────────────────────────────────
BRANCH_MAP = [
    ("SPACE FORCE", "Space Force"),
    ("AIR FORCE",   "Air Force"),
    ("ARMY",        "Army"),
    ("MARINE",      "Marines"),
    ("NAVY",        "Navy"),
    ("DISA",        "DISA"),
    ("DARPA",       "DARPA"),
    ("DLA",         "DLA"),
    ("NSA",         "NSA"),
    ("NGA",         "NGA"),
    ("SOCOM",       "SOCOM"),
    ("MDA",         "MDA"),
]

SETASIDE_LABELS = {
    "SBA":     "Small Business",
    "SBP":     "SB Partial",
    "8A":      "8(a)",
    "8AN":     "8(a) Sole Source",
    "HZC":     "HUBZone",
    "HZS":     "HUBZone Sole Source",
    "WOSB":    "WOSB",
    "WOSBSS":  "WOSB Sole Source",
    "EDWOSB":  "ED-WOSB",
    "SDVOSBC": "SDVOSB",
    "SDVOSBS": "SDVOSB Sole Source",
    "VSA":     "Veteran-Owned SB",
}


def parse_deadline(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(date_str, "%m/%d/%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def count_closing_soon(opportunities: list, now: datetime, days: int = 7) -> int:
    cutoff = now + timedelta(days=days)
    count = 0
    for opp in opportunities:
        dl = parse_deadline(opp.get("responseDeadLine") or "")
        if dl and now <= dl <= cutoff:
            count += 1
    return count


def branch_breakdown(opportunities: list) -> dict:
    counts: dict[str, int] = {}
    for opp in opportunities:
        combined = f"{opp.get('departmentName', '')} {opp.get('subtierName', '')}".upper()
        label = "Other DoD"
        for key, name in BRANCH_MAP:
            if key in combined:
                label = name
                break
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def setaside_breakdown(opportunities: list) -> dict:
    counts: dict[str, int] = {}
    for opp in opportunities:
        code  = (opp.get("typeOfSetAside") or "").strip().upper()
        label = SETASIDE_LABELS.get(code) or (
            (opp.get("typeOfSetAsideDescription") or "").strip()[:25]
        ) or "Unrestricted"
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


# ── State management ─────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_ids": [], "last_run": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── SAM.gov API ──────────────────────────────────────────────────────────────
# The SAM.gov keyword search is a loose OR-word match — it returns all records
# in the date window regardless of keyword. We fetch everything and filter by title.

# SAM.gov serves only the first page of a result set — offsets past it come back with
# an empty opportunitiesData array even when totalRecords is much higher. We partition
# the query by notice type instead so each slice fits inside a single page.
NOTICE_TYPES = ["o", "p", "k", "r", "s"]
PAGE_LIMIT   = 1000


def _get_with_retry(params: dict) -> Optional[dict]:
    """GET the opportunities endpoint, retrying on rate limit. None if the API 404s."""
    for attempt in range(4):
        resp = requests.get(SAM_OPPORTUNITIES_URL, params=params, timeout=60)
        if resp.status_code == 429:
            wait = 15 * (2 ** attempt)
            log.warning("Rate limited by SAM.gov — waiting %ds (attempt %d/4)", wait, attempt + 1)
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("SAM.gov rate limit not resolved after 4 retries")


def fetch_all_in_window(posted_from: str, posted_to: str) -> list[dict]:
    """Fetch every opportunity in the date window, one request per notice type."""
    results = []
    for ptype in NOTICE_TYPES:
        data = _get_with_retry({
            "api_key":    SAM_API_KEY,
            "keyword":    "solicitation",   # required but ignored — returns all records
            "postedFrom": posted_from,
            "postedTo":   posted_to,
            "limit":      PAGE_LIMIT,
            "offset":     0,
            "ptype":      ptype,
        })
        if data is None:
            continue

        opps  = data.get("opportunitiesData") or []
        total = data.get("totalRecords", 0) or 0
        results.extend(opps)
        if total > len(opps):
            log.warning(
                "ptype=%s returned %d of %d records — page cap hit, some notices skipped",
                ptype, len(opps), total,
            )
        time.sleep(0.25)

    log.info("Fetched %d total records from SAM.gov (%s → %s)", len(results), posted_from, posted_to)
    return results


def collect_all_opportunities(posted_from: str, posted_to: str) -> list[dict]:
    """Fetch all records in window, keep only PEP-titled ones, classify each."""
    raw = fetch_all_in_window(posted_from, posted_to)
    matches = []
    for opp in raw:
        if not is_pep_relevant(opp):
            continue
        opp["_matched_profile"] = classify_opportunity(opp)
        opp["_matched_emoji"]   = emoji_for_profile(opp["_matched_profile"])
        matches.append(opp)
    log.info("Relevance filter: %d PEP matches out of %d records", len(matches), len(raw))
    return matches


def is_pep_relevant(opp: dict) -> bool:
    title    = (opp.get("title") or "").upper()
    combined = f" {title} "
    return any(term in combined for term in PEP_TITLE_TERMS)


def classify_opportunity(opp: dict) -> str:
    dept = (opp.get("departmentName") or "").upper()

    if any(x in dept for x in ["DEPARTMENT OF THE AIR FORCE", "DEPARTMENT OF THE ARMY",
                                "DEPARTMENT OF THE NAVY", "DEFENSE", "DOD"]):
        return "DoD PEP"
    if any(x in dept for x in ["NATIONAL DEFENSE", "DEFENSE ADVANCED", "DARPA"]):
        return "DARPA PEP"
    return "PEP"


def emoji_for_profile(label: str) -> str:
    mapping = {"DoD PEP": "🔵", "DARPA PEP": "🟡", "PEP": "🟢"}
    return mapping.get(label, "⚪")


# ── Optional: OpenAI summary ─────────────────────────────────────────────────
def ai_summarize(opp: dict) -> Optional[str]:
    """Use GPT to generate a 2-sentence relevance summary. Returns None if unavailable."""
    if not OPENAI_API_KEY:
        return None
    try:
        import openai
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        description = (opp.get("description") or opp.get("synopsis") or "")[:2000]
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "You summarize federal contract opportunities in 2 sentences for a BD team. "
                    "Focus on: what is required, who can respond, and deadline. Be concise."
                )},
                {"role": "user", "content": (
                    f"Title: {opp.get('title')}\n"
                    f"Agency: {opp.get('departmentName')} / {opp.get('subtierName')}\n"
                    f"Type: {opp.get('baseType')}\n\n"
                    f"Description:\n{description}"
                )},
            ],
            max_tokens=120,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.warning("OpenAI summarization failed: %s", e)
        return None


# ── Adaptive Card helpers ─────────────────────────────────────────────────────
def _adaptive_card(body: list, actions: list = None) -> dict:
    card = {
        "type":    "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body":    body,
    }
    if actions:
        card["actions"] = actions
    return card


def _text(text: str, **kwargs) -> dict:
    return {"type": "TextBlock", "text": text, "wrap": True, **kwargs}


def _fact(name: str, value: str) -> dict:
    return {"name": name, "value": value or "—"}


# ── Teams notification ───────────────────────────────────────────────────────
def format_teams_message(opp: dict, is_update: bool) -> dict:
    label      = opp.get("_matched_profile", "PEP")
    emoji      = opp.get("_matched_emoji", "⚪")
    action     = "UPDATED" if is_update else "NEW"
    title      = opp.get("title") or "Untitled Opportunity"
    dept       = opp.get("departmentName") or ""
    subtier    = opp.get("subtierName") or ""
    agency_str = f"{dept} / {subtier}".strip(" /")
    notice_id  = opp.get("noticeId") or opp.get("solicitationNumber") or ""
    sol_number = opp.get("solicitationNumber") or ""
    posted     = opp.get("postedDate") or "—"
    response   = opp.get("responseDeadLine") or opp.get("archiveDate") or "See opportunity"
    opp_url    = f"https://sam.gov/opp/{notice_id}/view" if notice_id else "https://sam.gov"
    base_type  = opp.get("baseType") or opp.get("type") or "—"
    color      = "Attention" if action == "NEW" else "Warning"

    ai_summary = ai_summarize(opp)

    facts = [
        _fact("Agency",       agency_str),
        _fact("Type",         base_type),
        _fact("Posted",       posted),
        _fact("Response Due", response),
    ]
    if sol_number:
        facts.append(_fact("Solicitation #", sol_number))

    body = [
        _text(f"{emoji} {action} {label} Opportunity", weight="Bolder", size="Medium", color=color),
        _text(title, weight="Bolder"),
        {"type": "FactSet", "facts": facts},
    ]
    if ai_summary:
        body.append(_text(f"_{ai_summary}_", spacing="Medium", isSubtle=True))

    return _adaptive_card(body, actions=[{"type": "Action.OpenUrl", "title": "View on SAM.gov", "url": opp_url}])


def post_to_teams(card: dict) -> None:
    resp = requests.post(
        TEAMS_WEBHOOK_URL,
        json=card,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()


def post_run_summary(
    now: datetime,
    total: int,
    new_count: int,
    updated_count: int,
    already_seen: int,
    sam_api_ok: bool,
    teams_ok: bool,
    opportunities: list = None,
) -> None:
    opportunities = opportunities or []
    run_time     = now.strftime("%B %d, %Y — %I:%M %p UTC")
    sam_status   = "Authenticated ✅" if sam_api_ok else "Error ❌"
    teams_status = "Connected ✅"     if teams_ok  else "Error ❌"

    closing_soon = count_closing_soon(opportunities, now)
    branches     = branch_breakdown(opportunities)
    setasides    = setaside_breakdown(opportunities)

    def kv(label: str, value: str) -> dict:
        return _text(f"**{label}:** {value}", spacing="Small")

    body = [
        _text("📊 SAM.gov PEP Monitor — Run Complete", weight="Bolder", size="Medium"),
        _text(run_time, isSubtle=True, spacing="None"),
        {"type": "ColumnSet", "separator": True, "columns": [
            {"type": "Column", "width": "stretch", "items": [
                _text("**Results**", weight="Bolder"),
                kv("Total found", str(total)),
                kv("🆕 New",       str(new_count)),
                kv("🔄 Updated",   str(updated_count)),
                kv("✅ Seen",      str(already_seen)),
            ]},
            {"type": "Column", "width": "stretch", "items": [
                _text("**API Status**", weight="Bolder"),
                kv("SAM.gov",        sam_status),
                kv("GitHub Actions", "Scheduled ✅"),
                kv("Teams",          teams_status),
            ]},
        ]},
    ]

    if closing_soon > 0:
        noun = "opportunity" if closing_soon == 1 else "opportunities"
        body.append(_text(
            f"⚠️ **{closing_soon} {noun} closing within 7 days**",
            color="Attention", separator=True, spacing="Medium",
        ))

    if opportunities:
        branch_items = [_text("**By Branch**", weight="Bolder")]
        for b, cnt in list(branches.items())[:6]:
            branch_items.append(kv(b, str(cnt)))

        sa_items = [_text("**By Set-Aside**", weight="Bolder")]
        for sa, cnt in list(setasides.items())[:6]:
            sa_items.append(kv(sa, str(cnt)))

        body.append({"type": "ColumnSet", "separator": True, "columns": [
            {"type": "Column", "width": "stretch", "items": branch_items},
            {"type": "Column", "width": "stretch", "items": sa_items},
        ]})

    post_to_teams(_adaptive_card(body))


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    now = datetime.now(timezone.utc)
    state = load_state()
    seen_ids: set[str] = set(state.get("seen_ids", []))

    # Date window
    lookback = now - timedelta(hours=LOOKBACK_HOURS)
    posted_from = lookback.strftime("%m/%d/%Y")
    posted_to   = now.strftime("%m/%d/%Y")

    log.info("Checking SAM.gov from %s to %s", posted_from, posted_to)

    sam_api_ok = True
    try:
        opportunities = collect_all_opportunities(posted_from, posted_to)
    except Exception as e:
        log.error("Failed to collect opportunities: %s", e)
        opportunities = []
        sam_api_ok = False

    log.info("Total unique opportunities found: %d", len(opportunities))

    new_count     = 0
    updated_count = 0
    teams_ok      = True

    for opp in opportunities:
        notice_id = opp.get("noticeId") or ""
        if not notice_id:
            continue

        update_hash = hashlib.md5(
            f"{opp.get('postedDate')}{opp.get('responseDeadLine')}{opp.get('title')}".encode()
        ).hexdigest()[:8]
        state_key = f"{notice_id}:{update_hash}"

        if state_key in seen_ids:
            continue

        is_update = notice_id in {s.split(":")[0] for s in seen_ids}
        action    = "UPDATE" if is_update else "NEW"

        log.info("[%s] %s — %s", action, opp.get("_matched_profile"), opp.get("title", "")[:80])

        if DRY_RUN:
            log.info("[DRY RUN] Would post: %s", opp.get("title", "")[:100])
            if is_update:
                updated_count += 1
            else:
                new_count += 1
            continue

        try:
            post_to_teams(format_teams_message(opp, is_update=is_update))
            seen_ids.add(state_key)
            if is_update:
                updated_count += 1
            else:
                new_count += 1
            time.sleep(0.5)
        except Exception as e:
            log.error("Failed to post opportunity %s: %s", notice_id, e)
            teams_ok = False

    already_seen = len(opportunities) - new_count - updated_count

    # Prune state to last 5000 entries to prevent unbounded growth
    state["seen_ids"] = list(seen_ids)[-5000:]
    state["last_run"] = now.isoformat()
    save_state(state)

    log.info("Done — %d new, %d updated, %d already seen", new_count, updated_count, already_seen)

    try:
        post_run_summary(now, len(opportunities), new_count, updated_count, already_seen, sam_api_ok, teams_ok, opportunities=opportunities)
    except Exception as e:
        log.error("Failed to post run summary: %s", e)


if __name__ == "__main__":
    main()
