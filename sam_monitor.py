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

import enrich
import fit
import unanet

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
        combined = (opp.get("fullParentPathName") or "").upper()  # search results have no departmentName
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
        label = SETASIDE_LABELS.get(code) or enrich.set_aside(opp)[:25]
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
    """GET the opportunities endpoint, retrying on rate limits and timeouts. None if the API 404s."""
    for attempt in range(4):
        try:
            resp = requests.get(SAM_OPPORTUNITIES_URL, params=params, timeout=90)
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == 3:
                raise
            log.warning("SAM.gov request failed (%s) — retrying in %ds (attempt %d/4)", type(e).__name__, 10 * (attempt + 1), attempt + 1)
            time.sleep(10 * (attempt + 1))
            continue
        if resp.status_code == 429:
            wait = 15 * (2 ** attempt)
            log.warning("Rate limited by SAM.gov — waiting %ds (attempt %d/4)", wait, attempt + 1)
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("SAM.gov request not resolved after 4 attempts")


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
    e          = opp.get("_e") or enrich.enrich(opp)
    label      = opp.get("_matched_profile", "PEP")
    emoji      = opp.get("_matched_emoji", "⚪")
    action     = "UPDATED" if is_update else "NEW"
    title      = opp.get("title") or "Untitled Opportunity"
    color      = "Attention" if action == "NEW" else "Warning"

    ai_summary = ai_summarize(opp)

    rows = [
        ("Agency",               e["agency_line"] or "—"),
        ("Office",               e["office"]),
        ("Type",                 e["notice_type"] or "—"),
        ("Set-aside",            e["set_aside"]),
        ("Posted",               enrich.pretty_date(e["posted"]) or "—"),
        ("Response Due",         e["due_display"]),
        ("Solicitation #",       opp.get("solicitationNumber") or ""),
        ("NAICS / PSC",          " / ".join(x for x in [e["naics"], e["psc"]] if x)),
        ("Place of performance", e["place"]["text"]),
    ]
    if e["pocs"]:
        p = e["pocs"][0]
        rows.append(("Contracting POC", " · ".join(x for x in [p["name"], p["email"], p["phone"]] if x)))
    facts = [_fact(name, value) for name, value in rows if value]  # hide empty rows

    body = [
        _text(f"{emoji} {action} {label} Opportunity", weight="Bolder", size="Medium", color=color),
        _text(title, weight="Bolder"),
        {"type": "FactSet", "facts": facts},
    ]
    if e["attachments"]:
        n = len(e["attachments"])
        body.append(_text(f"📎 {n} attachment{'s' if n != 1 else ''} on SAM.gov", isSubtle=True, spacing="Small"))
    if ai_summary:
        body.append(_text(f"_{ai_summary}_", spacing="Medium", isSubtle=True))

    return _adaptive_card(body, actions=[{"type": "Action.OpenUrl", "title": "View on SAM.gov", "url": e["sam_url"]}])


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
    unanet_status: str = None,
    filtered: list = None,
    filtered_before: int = 0,
) -> None:
    """opportunities = the IPSecure-fit ones; filtered = (title, reason) judged not fit this run."""
    filtered = filtered or []
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
                kv("🎯 IPSecure fit", str(total - len(filtered) - filtered_before)),
                kv("🆕 New",       str(new_count)),
                kv("🔄 Updated",   str(updated_count)),
                kv("✅ Seen",      str(already_seen)),
            ]},
            {"type": "Column", "width": "stretch", "items": [
                _text("**API Status**", weight="Bolder"),
                kv("SAM.gov",        sam_status),
                kv("GitHub Actions", "Scheduled ✅"),
                kv("Teams",          teams_status),
            ] + ([kv("Unanet", unanet_status)] if unanet_status else [])},
        ]},
    ]

    if filtered or filtered_before:
        by_cat: dict[str, int] = {}
        for _, reason in filtered:
            by_cat[fit.category(reason)] = by_cat.get(fit.category(reason), 0) + 1
        parts = [f"{c} {n}" for c, n in sorted(by_cat.items(), key=lambda x: -x[1])]
        if filtered_before:
            parts.append(f"{filtered_before} judged earlier")
        body.append(_text(f"🚫 **Not IPSecure fit — {len(filtered) + filtered_before} not posted:** " + " · ".join(parts),
                          separator=True, spacing="Medium", wrap=True))
        for title, reason in filtered[:5]:
            body.append(_text(f"• {title[:70]} — _{reason[:50]}_", isSubtle=True, spacing="None", wrap=True))
        if len(filtered) > 5:
            body.append(_text(f"…and {len(filtered) - 5} more (see the GitHub Actions log)", isSubtle=True, spacing="None"))

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
# Flow: SAM.gov query -> relevance filter -> enrich once -> deliver to each
# destination independently. Each destination keeps its own "delivered" list in
# state.json, so a Teams outage never blocks Unanet (or vice versa); anything
# that fails is kept as "pending" and retried on later runs.
PENDING_LIMIT   = 100
DELIVERED_LIMIT = 5000


def notice_key(opp: dict) -> str:
    """noticeId plus a hash of the fields whose change counts as an update."""
    update_hash = hashlib.md5(
        f"{opp.get('postedDate')}{opp.get('responseDeadLine')}{opp.get('title')}".encode()
    ).hexdigest()[:8]
    return f"{opp.get('noticeId')}:{update_hash}"


def delivery_state(state: dict) -> tuple[dict, dict]:
    """delivered/pending per destination. Older state files only had seen_ids (= Teams)."""
    delivered = state.get("delivered") or {"teams": list(state.get("seen_ids", [])), "unanet": []}
    pending = state.get("pending") or {"teams": [], "unanet": []}
    for d in (delivered, pending):
        d.setdefault("teams", []); d.setdefault("unanet", [])
    return delivered, pending


def main() -> None:
    now = datetime.now(timezone.utc)
    state = load_state()
    delivered, pending = delivery_state(state)
    done = {dest: set(keys) for dest, keys in delivered.items()}
    teams_notices = {k.split(":")[0] for k in done["teams"]}
    filtered_keys = list(state.get("filtered", []))  # judged not IPSecure fit (re-judged if SAM updates them)
    not_fit = set(filtered_keys)

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

    # Destinations. Unanet is optional — off unless its secrets are set.
    destinations = ["teams"]
    crm = None
    if unanet.enabled():
        destinations.append("unanet")
        try:
            crm = unanet.Unanet()
        except Exception as e:
            log.error("Unanet unavailable this run: %s", e)

    # This run's work: fresh notices, then anything still pending from earlier runs.
    work, queued = [], set()
    for opp in opportunities:
        if opp.get("noticeId"):
            work.append(opp); queued.add(notice_key(opp))
    for dest in destinations:
        for opp in pending[dest]:
            if notice_key(opp) not in queued:
                work.append(opp); queued.add(notice_key(opp))

    new_count = updated_count = already_seen = 0
    teams_ok = True
    still_pending = {"teams": [], "unanet": []}
    fit_opps, filtered_now, filtered_before = [], [], 0

    for opp in work:
        key = notice_key(opp)
        if key in not_fit:
            filtered_before += 1
            continue
        todo = [d for d in destinations if key not in done[d]]
        if not todo:
            already_seen += 1
            fit_opps.append(opp)
            continue

        # IPSecure fit: cyber / IT / RMF / software only. Title and codes first; the
        # synopsis (one SAM call) only when they can't decide, e.g. generic CSO titles.
        e = enrich.enrich(opp)
        is_fit, reason = fit.assess(opp, e)
        if reason == "needs synopsis":
            e = enrich.enrich(opp, SAM_API_KEY, synopsis=True)
            is_fit, reason = fit.assess_synopsis(e["synopsis"])
        if not is_fit:
            log.info("[NOT FIT] %s — %s", opp.get("title", "")[:80], reason)
            filtered_now.append((opp.get("title") or "", reason))
            not_fit.add(key)
            filtered_keys.append(key)
            continue
        fit_opps.append(opp)

        is_update = opp["noticeId"] in teams_notices
        log.info("[%s] %s — %s (to: %s; fit: %s)", "UPDATE" if is_update else "NEW",
                 opp.get("_matched_profile"), opp.get("title", "")[:80], ", ".join(todo), reason)
        enrich.enrich(opp, SAM_API_KEY, synopsis="unanet" in todo and crm is not None)

        for dest in todo:
            if DRY_RUN:
                if dest == "teams":
                    log.info("[DRY RUN] Would post to Teams: %s", opp.get("title", "")[:100])
                elif crm:
                    crm.upsert(opp, dry_run=True)
                continue

            if dest == "teams":
                try:
                    post_to_teams(format_teams_message(opp, is_update=is_update))
                    ok = True
                    time.sleep(0.5)
                except Exception as e:
                    log.error("Teams post failed for %s: %s", opp["noticeId"], e)
                    ok = teams_ok = False
            else:
                ok = bool(crm) and crm.upsert(opp) in unanet.SUCCESS

            if ok:
                done[dest].add(key)
                delivered[dest].append(key)
            else:
                still_pending[dest].append({k: v for k, v in opp.items() if k != "_e"})

        if "teams" in todo and (DRY_RUN or key in done["teams"]):
            if is_update:
                updated_count += 1
            else:
                new_count += 1

    if not DRY_RUN:
        state["delivered"] = {d: keys[-DELIVERED_LIMIT:] for d, keys in delivered.items()}
        state["pending"]   = {d: items[-PENDING_LIMIT:] for d, items in still_pending.items()}
        state["seen_ids"]  = state["delivered"]["teams"]  # kept so an older version could still read this file
        state["filtered"]  = filtered_keys[-DELIVERED_LIMIT:]
    state["last_run"] = now.isoformat()
    save_state(state)

    log.info("Done — %d new, %d updated, %d already seen, %d not IPSecure fit",
             new_count, updated_count, already_seen, len(filtered_now) + filtered_before)
    teams_pending = len(still_pending["teams"])
    if teams_pending:
        log.warning("Teams: %d notices pending retry", teams_pending)

    unanet_status = None
    if "unanet" in destinations:
        unanet_status = crm.summary(dry_run=DRY_RUN) if crm else "Error ❌"
        if still_pending["unanet"]:
            unanet_status += f" · {len(still_pending['unanet'])} pending retry"
        log.info("Unanet: %s", unanet_status)

    try:
        post_run_summary(now, len(opportunities), new_count, updated_count, already_seen, sam_api_ok, teams_ok,
                         opportunities=fit_opps, unanet_status=unanet_status,
                         filtered=filtered_now, filtered_before=filtered_before)
    except Exception as e:
        log.error("Failed to post run summary: %s", e)


if __name__ == "__main__":
    main()
