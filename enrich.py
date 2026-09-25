"""
Normalize a SAM.gov search result once, for every destination (Teams, Unanet).

enrich(opp) stores the result in opp["_e"]: agency/office names, contacts,
place of performance, codes, dates, attachment links and, optionally, the
notice synopsis (one extra SAM.gov API call).
"""

import html
import logging
import re
from datetime import datetime
from typing import Optional

import requests

log = logging.getLogger(__name__)

NOTICE_DESC_URL = "https://api.sam.gov/prod/opportunities/v1/noticedesc"
# Words that mark a SAM point of contact as an office or mailbox rather than a person.
ORG_WORDS = {"CONTRACTING", "CONTRACTS", "CONTRACT", "OFFICE", "HQ", "TEAM", "DIVISION", "BRANCH",
             "SECTION", "DIRECTORATE", "GROUP", "MAILBOX", "CENTER", "COMMAND", "SQUADRON", "WING",
             "CONS", "OPERATIONAL", "PROCUREMENT", "ACQUISITION", "SUPPORT", "DESK", "INBOX", "DEPT",
             "DEPARTMENT", "AGENCY", "ORGANIZATION", "UNIT", "CELL", "SPECIALIST", "OFFICER", "POC"}
COUNTRIES = {"USA": "United States", "US": "United States", "UNITED STATES": "United States"}


def _name(v) -> str:
    """SAM gives some location parts as {"code": .., "name": ..} and some as plain strings."""
    if isinstance(v, dict):
        return (v.get("name") or v.get("code") or "").strip()
    return (v or "").strip()


def _code(v) -> str:
    if isinstance(v, dict):
        return (v.get("code") or v.get("name") or "").strip()
    return (v or "").strip()


def _date(value: str) -> str:
    """'2026-10-05T12:00:00-05:00' or '10/05/2026' -> '2026-10-05' ('' if unparseable)."""
    value = (value or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    if re.match(r"\d{2}/\d{2}/\d{4}", value):
        m, d, y = value[:10].split("/")
        return f"{y}-{m}-{d}"
    return ""


def pretty_date(value: str) -> str:
    """'2026-10-05T12:00:00-05:00' -> 'Oct 5, 2026 12:00 PM (UTC-05:00)'; date-only -> 'Oct 5, 2026'."""
    value = (value or "").strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        d = _date(value)
        return datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d, %Y") if d else value
    text = dt.strftime("%b %-d, %Y")
    if "T" in value:
        text += dt.strftime(" %-I:%M %p")
        if dt.utcoffset() is not None:
            off = dt.strftime("%z")
            text += f" (UTC{off[:3]}:{off[3:]})"
    return text


def _split_name(full: str) -> tuple[str, str]:
    """'Erika Boles' -> ('Erika', 'Boles'); 'BOLES, ERIKA' -> ('Erika', 'Boles'). ('', '') if not a person's name."""
    full = re.sub(r"\s+", " ", (full or "").strip())
    if "," in full:
        last, first = [p.strip() for p in full.split(",", 1)]
    else:
        parts = full.split(" ")
        if len(parts) < 2:
            return "", ""
        first, last = " ".join(parts[:-1]), parts[-1]
    words = {w.strip(".,()").upper() for w in full.split(" ")}
    if (not first or not last or len(words) > 4 or ORG_WORDS & words
            or any(ch.isdigit() or ch in "/@&" for ch in full)):
        return "", ""
    fix = (lambda s: s.title()) if full.isupper() else (lambda s: s)
    return fix(first), fix(last)


def _phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}" if len(digits) == 10 else (raw or "").strip()


def set_aside(opp: dict) -> str:
    desc = (opp.get("typeOfSetAsideDescription") or "").strip()
    code = (opp.get("typeOfSetAside") or "").strip()
    if not desc or desc.lower() in ("none", "no set aside used") or code.upper() == "NONE":
        return "No set-aside"
    return desc


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", raw or "")
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def fetch_synopsis(notice_id: str, api_key: str) -> Optional[str]:
    try:
        r = requests.get(NOTICE_DESC_URL, params={"noticeid": notice_id, "api_key": api_key}, timeout=30)
        if r.status_code != 200:
            log.warning("Synopsis for %s: HTTP %s", notice_id, r.status_code)
            return None
        text = html_to_text(r.json().get("description") or "")
        return None if not text or text.lower().startswith("description not found") else text
    except Exception as e:
        log.warning("Synopsis for %s failed: %s", notice_id, e)
        return None


def enrich(opp: dict, sam_api_key: str = "", synopsis: bool = False) -> dict:
    if "_e" in opp and (opp["_e"].get("synopsis_fetched") or not synopsis):
        return opp["_e"]

    notice_id = opp.get("noticeId") or ""
    names = [n.strip() for n in (opp.get("fullParentPathName") or "").split(".") if n.strip()]
    agency  = names[1] if len(names) > 1 else (names[0] if names else "")
    command = names[2] if len(names) > 2 else ""
    office  = names[-1] if len(names) > 3 else ""

    oa = opp.get("officeAddress") or {}
    office_addr = ", ".join(p for p in [_name(oa.get("city")), " ".join(
        p for p in [_code(oa.get("state")), (oa.get("zipcode") or oa.get("zip") or "").strip()] if p)] if p)

    pop = opp.get("placeOfPerformance") or {}
    country = _code(pop.get("country"))
    place = {
        "address1": (pop.get("streetAddress") or pop.get("street") or "").strip(),
        "city":     _name(pop.get("city")),
        "state":    _code(pop.get("state")),
        "zip":      (pop.get("zip") or pop.get("zipcode") or "").strip(),
        "country":  COUNTRIES.get(country.upper(), country.title()) if country else "",
    }
    if place["city"] or place["state"]:
        place["text"] = ", ".join(p for p in [place["city"], " ".join(p for p in [place["state"], place["zip"]] if p)] if p)
    else:  # SAM sometimes gives only a free-text street line
        place["text"] = " ".join(p for p in [place["address1"], place["zip"] if place["zip"] not in place["address1"] else ""] if p)

    pocs = []
    for p in opp.get("pointOfContact") or []:
        first, last = _split_name(p.get("fullName"))
        pocs.append({
            "name":  (p.get("fullName") or "").strip(),
            "first": first, "last": last,
            "email": (p.get("email") or "").strip().lower(),
            "phone": _phone(p.get("phone")),
            "type":  (p.get("type") or "").strip().lower(),
            "title": (p.get("title") or "").strip(),
        })
    pocs.sort(key=lambda p: p["type"] != "primary")

    naics = opp.get("naicsCodes") or ([opp["naicsCode"]] if opp.get("naicsCode") else [])
    attachments = [u for u in (opp.get("resourceLinks") or []) if u]
    if opp.get("additionalInfoLink"):
        attachments.append(opp["additionalInfoLink"])

    e = {
        "sam_url":     f"https://sam.gov/opp/{notice_id}/view" if notice_id else "https://sam.gov",
        "agency":      agency,
        "command":     command,
        "agency_line": " / ".join(p for p in [agency, command] if p),  # SAM's own capitalization keeps acronyms intact
        "office":      office,
        "office_addr": office_addr,
        "place":       place,
        "pocs":        pocs,
        "naics":       ", ".join(str(n) for n in naics),
        "psc":         (opp.get("classificationCode") or "").strip(),
        "set_aside":   set_aside(opp),
        "notice_type": (opp.get("type") or opp.get("baseType") or "").strip(),
        "posted":      _date(opp.get("postedDate")),
        "due":         _date(opp.get("responseDeadLine")),
        "due_display": pretty_date(opp.get("responseDeadLine")) or "See SAM.gov",
        "archive":     _date(opp.get("archiveDate")),
        "attachments": attachments,
        "synopsis":    fetch_synopsis(notice_id, sam_api_key) if synopsis and notice_id and sam_api_key else None,
        "synopsis_fetched": bool(synopsis and notice_id and sam_api_key),  # don't refetch a missing one
    }
    opp["_e"] = e
    return e
