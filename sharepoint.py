"""
Land SAM.gov opportunities in a SharePoint list through Microsoft Graph: the fallback
(or parallel) destination to Unanet CRM.

Off unless SP_TENANT_ID, SP_CLIENT_ID, SP_CLIENT_SECRET and SP_SITE are set; without
them the monitor runs exactly as before. SP_SITE is "<host>:/sites/<path>", e.g.
ipsecureinc.sharepoint.com:/sites/BDOpportunityIntake. The Entra app needs the Graph
application permission Sites.Selected, granted "write" on that one site only.

Lists (built once by provision_sharepoint.py, from the column specs below):
    SAM Opportunities      one item per notice; Notice ID is indexed and unique
    DoD Customers          IPSecure's customer list with SAM.gov org codes; notices are matched
                           to the longest code, the same rule the Unanet sync uses
    Opportunity Documents  library with one folder per notice holding its SAM.gov attachments
                           (Graph can't add list-item attachments with a client secret)

Ownership: SAM-owned columns are refreshed when the notice changes. Disposition, Owner,
Team notes, Unanet opportunity ID, and Customer/Service after the item is created belong
to the team and are never written. Contract type(s) is multi-select and each monitor only
adds its own type, so a notice several monitors match (e.g. an IDIQ that is also a BPA)
keeps every type whatever order they run in; if two monitors create the same notice at
once, the unique Notice ID rejects the second and it merges into the first instead.
Unlike the Unanet sync, notices outside the customer list still land (Customer left
empty), so the list is the complete triage queue.
"""

import logging
import os
import re
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

import requests

import enrich
import unanet  # shared org-code matching, monitor name and attachment download

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
OPP_LIST, CUSTOMER_LIST, DOC_LIBRARY = "SAM Opportunities", "DoD Customers", "Opportunity Documents"
SUCCESS = {"created", "updated", "unchanged"}
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
CHUNK = 10 * 327680            # upload-session chunks must be multiples of 320 KiB
NOT_AVAILABLE = "Synopsis not available on SAM.gov."


def _text(name, display, **extra):
    return {"name": name, "displayName": display, "text": {}, **extra}


def _multiline(name, display):
    return {"name": name, "displayName": display,
            "text": {"allowMultipleLines": True, "linesForEditing": 6, "textType": "plain"}}


def _date(name, display, with_time=False):
    return {"name": name, "displayName": display,
            "dateTime": {"format": "dateTime" if with_time else "dateOnly", "displayAs": "standard"}}


def _choice(name, display, choices, multi=False, **extra):
    """multi=True: check boxes, i.e. a multi-select (MultiChoice) column."""
    return {"name": name, "displayName": display, **extra,
            "choice": {"choices": choices, "allowTextEntry": True, "displayAs": "checkBoxes" if multi else "dropDownMenu"}}


# A notice can match several monitors (e.g. an IDIQ that is also a BPA). Each monitor only ever
# ADDS its type to this multi-select column, so the result is the same whatever order they run in.
CONTRACT_TYPES = ["CSO", "IDIQ", "BPA", "BAA", "OTA", "PEP", "CRADA"]
MULTI = "Collection(Edm.String)"

# Column specs, in display order. Internal names are what the sync writes.
CUSTOMER_COLUMNS = [
    _text("SamCode", "SAM.gov code(s)", indexed=True),   # "097.97AK.HC1028|HC1084" = extra codes under one parent
    _text("Acronym", "Acronym"),
    _choice("Service", "Service", ["Air Force", "Army", "Navy", "DISA", "Other DoD"]),
    {"name": "Level", "displayName": "Level", "number": {"decimalPlaces": "none"}},
    _text("Parent", "Parent"),
    _choice("Priority", "IPSecure priority", ["HIGH", "MEDIUM", "LOW"]),
]
OPP_COLUMNS = [
    _text("NoticeId", "Notice ID", indexed=True, enforceUniqueValues=True),
    _text("SamLink", "SAM.gov link"),
    _choice("Disposition", "Disposition", ["New", "Reviewing", "Pursue", "No bid", "Entered in Unanet"],
            defaultValue={"value": "New"}),
    {"name": "Owner", "displayName": "Owner",
     "personOrGroup": {"allowMultipleSelection": False, "chooseFromType": "peopleOnly"}},
    _choice("ContractType", "Contract type(s)", CONTRACT_TYPES, multi=True),
    "CUSTOMER_LOOKUP",                                    # filled in by provisioning (needs the list id)
    _text("Service", "Service"),
    _text("Agency", "Agency / command"),
    _text("Office", "Contracting office"),
    _text("SolicitationNumber", "Solicitation #"),
    _text("NoticeType", "Notice type"),
    _date("Posted", "Posted"),
    _date("ResponseDue", "Response due", with_time=True),
    _date("ArchiveDate", "Archive date"),
    _text("NAICS", "NAICS"),
    _text("PSC", "PSC"),
    _text("SetAside", "Set-aside"),
    _text("PlaceOfPerformance", "Place of performance"),
    _multiline("Contacts", "Points of contact"),
    _multiline("Synopsis", "Synopsis (short)"),
    _text("FitReason", "Why it passed the filter"),
    _text("Documents", "Documents"),
    _multiline("AttachmentProblems", "Attachments not retrieved"),
    _date("SamUpdated", "Updated from SAM.gov", with_time=True),
    {"name": "UnanetId", "displayName": "Unanet opportunity ID", "number": {"decimalPlaces": "none"}},
    _multiline("TeamNotes", "Team notes"),
]
DATE_ONLY = {"Posted", "ArchiveDate"}


def enabled() -> bool:
    return all(os.environ.get(k) for k in ("SP_TENANT_ID", "SP_CLIENT_ID", "SP_CLIENT_SECRET", "SP_SITE"))


def graph_token() -> str:
    r = requests.post(
        f"https://login.microsoftonline.com/{os.environ['SP_TENANT_ID']}/oauth2/v2.0/token",
        data={"client_id": os.environ["SP_CLIENT_ID"], "client_secret": os.environ["SP_CLIENT_SECRET"],
              "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
        timeout=30,
    )
    token = (r.json() if r.ok else {}).get("access_token")
    if not token:
        raise RuntimeError(f"SharePoint sign-in failed (HTTP {r.status_code}) — check the SP_* secrets")
    return token


class Graph:
    """Thin Graph client: retries throttling, follows paging."""

    def __init__(self, token: str):
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"

    def call(self, method: str, path: str, **kw):
        url = path if path.startswith("https://") else GRAPH + path
        timeout = kw.pop("timeout", 120)
        for attempt in range(5):
            r = self.http.request(method, url, timeout=timeout, **kw)
            if r.status_code in (429, 503, 504) and attempt < 4:
                time.sleep(min(int(r.headers.get("Retry-After", 5 * (attempt + 1))), 60))
                continue
            r.raise_for_status()
            return r.json() if r.content else {}

    def paged(self, path: str, **kw):
        while path:
            page = self.call("GET", path, **kw)
            yield from page.get("value", [])
            path = page.get("@odata.nextLink")


def _utc(value: str) -> Optional[str]:
    """SAM date/time -> UTC for a Graph dateTime column. Date-only -> noon UTC (the same day across the US)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" in value and dt.tzinfo:
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    return f"{enrich._date(value)}T12:00:00Z" if enrich._date(value) else None


def _safe_name(name: str) -> str:
    """SharePoint rejects " * : < > ? / \\ | and names ending in a space or period."""
    return re.sub(r'["*:<>?/\\|#%]', "_", name).strip().rstrip(".") or "attachment"


def _types(value) -> list[str]:
    """A multi-choice value as Graph returns it (a list; tolerate a single string)."""
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [value] if isinstance(value, str) and value else []


def _same(key: str, current, new) -> bool:
    current = "" if current is None else str(current)
    if key in DATE_ONLY:
        return current[:10] == str(new)[:10]
    return current.replace("\r\n", "\n").strip() == str(new).replace("\r\n", "\n").strip()


class SharePoint:
    def __init__(self):
        self.monitor = unanet.monitor_name()
        self.counts: Counter = Counter()
        self.g = Graph(graph_token())
        self.site = self.g.call("GET", f"/sites/{os.environ['SP_SITE']}?$select=id")["id"]
        lists = {l["displayName"]: l["id"] for l in self.g.paged(f"/sites/{self.site}/lists?$select=id,displayName")}
        missing = [n for n in (OPP_LIST, CUSTOMER_LIST, DOC_LIBRARY) if n not in lists]
        if missing:
            raise RuntimeError(f"SharePoint site has no {', '.join(missing)} — run provision_sharepoint.py first")
        self.items = f"/sites/{self.site}/lists/{lists[OPP_LIST]}/items"
        self.customer_items = f"/sites/{self.site}/lists/{lists[CUSTOMER_LIST]}/items"
        self.drive = self.g.call("GET", f"/sites/{self.site}/lists/{lists[DOC_LIBRARY]}/drive?$select=id")["id"]
        self.codes = self._load_customers()
        log.info("SharePoint: %d customer codes loaded", len(self.codes))

    def _load_customers(self) -> dict[str, tuple[int, str, str]]:
        """{SAM org code: (item id, customer name, service)}"""
        codes = {}
        for item in self.g.paged(f"{self.customer_items}?$expand=fields($select=Title,SamCode,Service)&$top=500"):
            f = item["fields"]
            for code in unanet.parse_company_codes(f.get("SamCode")):
                codes[code] = (int(item["id"]), f.get("Title") or "", f.get("Service") or "")
        return codes

    def find_item(self, notice_id: str) -> Optional[dict]:
        nid = notice_id.replace("'", "''")
        hits = list(self.g.paged(f"{self.items}?$filter=fields/NoticeId eq '{nid}'&$expand=fields&$top=5",
                                 headers={"Prefer": "HonorNonIndexedQueriesWarningMayFailRandomly"}))
        return min(hits, key=lambda h: int(h["id"])) if hits else None

    # ── Field mapping ────────────────────────────────────────────────────────
    def _sam_owned(self, opp: dict, e: dict) -> dict:
        contacts = []
        if e["office"] or e["office_addr"]:
            contacts.append("Contracting office: " + " — ".join(p for p in [e["office"], e["office_addr"]] if p))
        for p in e["pocs"]:
            name = f"{p['first']} {p['last']}" if p["first"] else p["name"]
            detail = " · ".join(x for x in [p["email"], p["phone"]] if x)
            contacts.append(f"{(p['type'] or 'POC').title()} POC: {name}" + (f" · {detail}" if detail else ""))
        fields = {
            "Title":              (opp.get("title") or opp["noticeId"])[:255],
            "SamLink":            e["sam_url"][:255],
            "Agency":             e["agency_line"][:255],
            "Office":             " — ".join(p for p in [e["office"], e["office_addr"]] if p)[:255],
            "SolicitationNumber": (opp.get("solicitationNumber") or "")[:255],
            "NoticeType":         e["notice_type"][:255],
            "NAICS":              e["naics"][:255],
            "PSC":                e["psc"][:255],
            "SetAside":           e["set_aside"][:255],
            "PlaceOfPerformance": e["place"]["text"][:255],
            "Contacts":           "\n".join(contacts),
            "Synopsis":           enrich.short_synopsis(e["synopsis"]) or NOT_AVAILABLE,
            "FitReason":          (opp.get("_fit") or "")[:255],
        }
        for key, value in (("Posted", e["posted"]), ("ResponseDue", _utc(opp.get("responseDeadLine"))),
                           ("ArchiveDate", e["archive"])):
            if value:
                fields[key] = value
        return fields

    # ── Main entry point ─────────────────────────────────────────────────────
    def upsert(self, opp: dict, dry_run: bool = False) -> str:
        """Create or update one notice's item. Returns created|updated|unchanged|failed."""
        notice_id = opp.get("noticeId") or ""
        title = (opp.get("title") or "")[:70]
        e = opp.get("_e") or enrich.enrich(opp)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            fields = self._sam_owned(opp, e)
            existing = self.find_item(notice_id)
            if not existing:
                try:
                    self._create(opp, e, fields, notice_id, title, stamp, dry_run)
                    return self._count("created")
                except requests.HTTPError:
                    # Notice ID is unique: an overlapping monitor may have created it a moment ago.
                    existing = self.find_item(notice_id)
                    if not existing:
                        raise
                    log.info("SharePoint: %s was just created by another monitor — merging", notice_id)
            return self._count(self._update(existing, e, fields, notice_id, title, stamp, dry_run))
        except Exception as ex:
            log.error("SharePoint: failed for %s (%s): %s", notice_id, title, ex)
            return self._count("failed")

    def _create(self, opp: dict, e: dict, fields: dict, notice_id: str, title: str, stamp: str, dry_run: bool) -> None:
        match = unanet.best_match(opp.get("fullParentPathCode") or "", self.codes)
        new = {**fields, "NoticeId": notice_id, "SamUpdated": stamp}
        if self.monitor in CONTRACT_TYPES:
            new.update({"ContractType@odata.type": MULTI, "ContractType": [self.monitor]})
        if match:
            new.update({"CustomerLookupId": str(match[0]), "Service": match[2]})
        log.info("SharePoint: %screate %s -> %s", "[DRY RUN] would " if dry_run else "", title,
                 match[1] if match else "no customer match")
        if not dry_run:
            item = self.g.call("POST", self.items, json={"fields": new})
            self.sync_attachments(item["id"], notice_id, e, {})
        if not match:
            self.counts["no customer match"] += 1

    def _update(self, existing: dict, e: dict, fields: dict, notice_id: str, title: str, stamp: str,
                dry_run: bool) -> str:
        current = existing.get("fields") or {}
        if e["synopsis"] is None and current.get("Synopsis") not in (None, "", NOT_AVAILABLE):
            fields.pop("Synopsis")      # synopsis not fetched this run: keep the one already there
        changes = {k: v for k, v in fields.items() if not _same(k, current.get(k), v)}
        have = _types(current.get("ContractType"))
        if self.monitor in CONTRACT_TYPES and self.monitor not in have:   # add, never remove
            merged = set(have) | {self.monitor}
            changes["ContractType"] = [t for t in CONTRACT_TYPES if t in merged] + sorted(merged - set(CONTRACT_TYPES))
        if changes:
            log.info("SharePoint: %supdate item %s (%s): %s", "[DRY RUN] would " if dry_run else "",
                     existing["id"], title, sorted(changes))
        if not dry_run:
            if changes:
                if "ContractType" in changes:
                    changes["ContractType@odata.type"] = MULTI
                self.g.call("PATCH", f"{self.items}/{existing['id']}/fields", json={**changes, "SamUpdated": stamp})
            self.sync_attachments(existing["id"], notice_id, e, current)
        return "updated" if changes else "unchanged"

    # ── Attachments (SAM.gov files -> Opportunity Documents/<notice id>/) ────
    def _folder(self, notice_id: str) -> dict:
        """The notice's folder in the library, created on first use."""
        try:
            return self.g.call("GET", f"/drives/{self.drive}/root:/{notice_id}")
        except requests.HTTPError as ex:
            if ex.response is None or ex.response.status_code != 404:
                raise
        return self.g.call("POST", f"/drives/{self.drive}/root/children",
                           json={"name": notice_id, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})

    def _upload(self, folder_id: str, name: str, data: bytes) -> None:
        target = f"/drives/{self.drive}/items/{folder_id}:/{urllib.parse.quote(name)}:"
        if len(data) <= SIMPLE_UPLOAD_MAX:
            self.g.call("PUT", f"{target}/content", data=data, headers={"Content-Type": "application/octet-stream"})
            return
        session = self.g.call("POST", f"{target}/createUploadSession",
                              json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
        for start in range(0, len(data), CHUNK):
            chunk = data[start:start + CHUNK]
            r = requests.put(session["uploadUrl"], data=chunk, timeout=300, headers={
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(data)}"})
            r.raise_for_status()

    def sync_attachments(self, item_id: str, notice_id: str, e: dict, current: dict) -> None:
        """Upload the notice's files to its folder; a file already there (same name and size) is
        skipped, so amendments only add what's new. Files that can't be fetched are listed on the item."""
        if not e.get("files"):
            return
        problems, changes = [], {}
        try:
            folder = self._folder(notice_id)
            have = {c["name"]: c.get("size") for c in self.g.paged(f"/drives/{self.drive}/items/{folder['id']}/children?$select=name,size")}
            for url in e["files"]:
                name, data, problem = unanet.Unanet._download(url)
                name = _safe_name(name)
                if problem:
                    problems.append(f"{name} ({problem})")
                    continue
                if have.get(name) == len(data):
                    continue
                try:
                    self._upload(folder["id"], name, data)
                    have[name] = len(data)
                    self.counts["documents added"] += 1
                except Exception as ex:
                    problems.append(f"{name} (upload to SharePoint failed: {type(ex).__name__})")
            if not _same("Documents", current.get("Documents"), folder.get("webUrl", "")):
                changes["Documents"] = folder.get("webUrl", "")
        except Exception as ex:
            log.error("SharePoint: documents for %s failed: %s", notice_id, ex)
            problems.append(f"document folder unavailable ({type(ex).__name__})")
        if problems:
            self.counts["documents not retrieved"] += len(problems)
            log.warning("SharePoint: %d attachment(s) not retrieved for %s: %s", len(problems), notice_id, "; ".join(problems))
            noted = (current.get("AttachmentProblems") or "").replace("\r\n", "\n")
            new = [p for p in problems if p not in noted]
            if new:
                changes["AttachmentProblems"] = "\n".join([noted] + new if noted else new)
        if changes:
            try:
                self.g.call("PATCH", f"{self.items}/{item_id}/fields", json=changes)
            except Exception as ex:
                log.error("SharePoint: could not record documents on item %s: %s", item_id, ex)

    def _count(self, outcome: str) -> str:
        self.counts[outcome] += 1
        return outcome

    def summary(self, dry_run: bool = False) -> str:
        c = self.counts
        would = "would be " if dry_run else ""
        parts = [f"{c['created']} {would}created", f"{c['updated']} {would}updated", f"{c['failed']} failed"]
        if c["unchanged"]:
            parts.insert(2, f"{c['unchanged']} already current")
        if c["no customer match"]:
            parts.append(f"{c['no customer match']} outside the customer list")
        if c["documents added"]:
            parts.append(f"{c['documents added']} documents added")
        if c["documents not retrieved"]:
            parts.append(f"{c['documents not retrieved']} documents not retrieved")
        return ", ".join(parts)
