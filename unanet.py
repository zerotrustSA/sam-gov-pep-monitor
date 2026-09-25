"""
Post SAM.gov opportunities into Unanet CRM (Cosential Compass API).

Off unless UNANET_API_KEY, UNANET_USERNAME, UNANET_PASSWORD and UNANET_FIRM_ID are
set — without them the monitor runs exactly as before. UNANET_ENV picks sandbox
(default) or prod; UNANET_STAGE_ID sets the stage for new opportunities.

Matching: each opportunity goes to the company whose ID field (ExternalId) holds the
longest SAM org code matching the notice's fullParentPathCode, e.g.
057.5700.ACC.FA4890 -> Air Combat Command (057.5700.ACC). No match -> skipped;
this module never creates companies.

Idempotent: the SAM notice ID is the opportunity's ExternalId. An existing
opportunity (found by search, plus the newest records to cover search-index lag)
is updated rather than duplicated, and only SAM-owned fields change — stage,
probability, owner, description, address and note edits made by the team are kept.

Contracting officers from the notice become Unanet contacts under the matched
company (matched on email, never duplicated) and are linked to the opportunity.

Field layout (custom-field labels should be renamed to match in Unanet admin):
    Opportunity Description  SAM.gov link + notice synopsis
    Note                     contracting office + points of contact
    Project Address          place of performance
    NAICS (Categorization)   NAICS code(s) — Unanet's own field
    Custom Short Text 1-4    PSC · Set-aside · Notice type · Source monitor
    Custom Short Text 5      SAM.gov link (the "External URL" field built in Unanet's field designer
                             is not exposed by this API; the link also leads the Description)
    Custom Date 1-2          Posted · Archive
    Custom Long Text 1       attachment links
"""

import logging
import os
import re
from collections import Counter
from typing import Optional

import requests

import enrich

log = logging.getLogger(__name__)

BASE_URLS = {
    "sandbox": "https://compass-sandbox.cosential.com",
    "prod":    "https://compass.cosential.com",
}
DEFAULT_STAGE_ID = 60888   # "01-Lead/Prepositioning" (verified in sandbox 2026-09-25)
DEFAULT_ROLE_ID = 0        # Unanet's default opportunity-contact role
SAM_CODE = re.compile(r"^\d{3}(\.|$)")  # company IDs that hold a SAM org code
RECENT_CHECK = 200         # newest records scanned to cover search-index lag
SUCCESS = {"created", "updated", "unchanged", "unmatched"}

# Returned by GET but must not be sent back on PUT.
READ_ONLY = {
    "ROW_VERSION", "version_userName", "version_device", "CreateDateTime",
    "CreatedByUserId", "LastModifiedDateTime", "LastModifiedByUserId",
    "LastDeletedDateTime", "LastDeletedByUserId", "ClientName", "Stage", "StageType",
    "OppTypeDescription", "OwnerName",
}


def enabled() -> bool:
    return all(os.environ.get(k) for k in
               ("UNANET_API_KEY", "UNANET_USERNAME", "UNANET_PASSWORD", "UNANET_FIRM_ID"))


def monitor_name() -> str:
    """'CSO' from GITHUB_REPOSITORY=zerotrustSA/sam-gov-cso-monitor; 'SAM' when run locally."""
    m = re.search(r"sam-gov-(\w+)-monitor", os.environ.get("GITHUB_REPOSITORY", ""))
    return m.group(1).upper() if m else os.environ.get("MONITOR_NAME", "SAM")


def parse_company_codes(external_id: str) -> list[str]:
    """'097.97AK.HC1028|HC1084' -> ['097.97AK.HC1028', '097.97AK.HC1084']"""
    parts = [p.strip() for p in (external_id or "").split("|") if p.strip()]
    if not parts or not SAM_CODE.match(parts[0]):
        return []
    prefix = parts[0].rsplit(".", 1)[0]
    return [parts[0]] + [f"{prefix}.{p}" for p in parts[1:]]


def best_match(path_code: str, codes: dict[str, tuple[int, str]]) -> Optional[tuple[int, str]]:
    """Longest company code that equals path_code or is a dot-boundary prefix of it."""
    matches = [c for c in codes if path_code == c or path_code.startswith(c + ".")]
    return codes[max(matches, key=len)] if matches else None


class Unanet:
    def __init__(self):
        self.env = os.environ.get("UNANET_ENV", "sandbox")
        self.base = BASE_URLS[self.env]
        self.stage_id = int(os.environ.get("UNANET_STAGE_ID", DEFAULT_STAGE_ID))
        self.monitor = monitor_name()
        self.counts: Counter = Counter()
        self._contacts: dict[str, int] = {}  # email -> ContactId, this run
        auth = requests.post(
            f"{self.base}/v2/api/token/auth",
            json={
                "UserName": os.environ["UNANET_USERNAME"],
                "Password": os.environ["UNANET_PASSWORD"],
                "FirmId":   os.environ["UNANET_FIRM_ID"],
            },
            timeout=30,
        )
        token = ((auth.json() if auth.ok else {}).get("Response") or {}).get("Token")
        if not token:
            raise RuntimeError(f"Unanet login failed (HTTP {auth.status_code}) — check the UNANET_* secrets")
        self.headers = {
            "Authorization":     f"Bearer {token}",
            "x-compass-api-key": os.environ["UNANET_API_KEY"],
            "Content-Type":      "application/json",
        }
        self.codes = self._load_company_codes()
        log.info("Unanet (%s): %d company codes loaded", self.env, len(self.codes))

    # ── API helpers ──────────────────────────────────────────────────────────
    def _get(self, path: str, **params):
        r = requests.get(f"{self.base}{path}", headers=self.headers, params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, items: list) -> list:
        r = requests.post(f"{self.base}{path}", headers=self.headers, json=items, timeout=30)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, record: dict, changes: dict) -> None:
        body = {k: v for k, v in record.items() if k not in READ_ONLY}
        body.update(changes)
        r = requests.put(f"{self.base}{path}", headers=self.headers, json=body, timeout=30)
        r.raise_for_status()

    def _load_company_codes(self) -> dict[str, tuple[int, str]]:
        codes, start = {}, 0
        while True:
            batch = self._get("/api/companies", **{"from": start, "size": 500})
            for c in batch:
                for code in parse_company_codes(c.get("ExternalId")):
                    codes[code] = (c["CompanyId"], c.get("Name") or "")
            start += len(batch)
            if len(batch) < 500:
                return codes

    def _find(self, entity: str, query: str, key: str, is_match) -> dict[int, dict]:
        """Records matching is_match: search first, then the newest records (search lags behind creates)."""
        hits = {h[key]: h for h in self._get(f"/api/{entity}/search", q=query) if is_match(h)}
        if not hits:
            recent = self._get(f"/api/{entity}", **{"from": 0, "size": RECENT_CHECK})
            hits = {h[key]: h for h in recent if is_match(h)}
        return hits

    def find_opportunity(self, notice_id: str) -> Optional[dict]:
        """Existing opportunity with this ExternalId, or None. Unanet does not enforce
        unique ExternalIds, so this check is what prevents duplicates."""
        hits = self._find("opportunities", f'ExternalId:"{notice_id}"', "OpportunityId",
                          lambda h: h.get("ExternalId") == notice_id)
        if len(hits) > 1:
            log.warning("Unanet has %d opportunities with ExternalId %s (%s); using the oldest",
                        len(hits), notice_id, sorted(hits))
        return hits[min(hits)] if hits else None

    # ── Field mapping ────────────────────────────────────────────────────────
    def _sam_owned(self, e: dict, opp: dict) -> dict:
        """Fields that always mirror SAM.gov — refreshed when the notice changes."""
        fields = {
            "SolicitationNumber":    (opp.get("solicitationNumber") or "")[:100],
            "OpportunityShortText1": e["psc"][:100],
            "OpportunityShortText2": e["set_aside"][:100],
            "OpportunityShortText3": e["notice_type"][:100],
            "OpportunityLongText1":  "\n".join(e["attachments"]),
            "OpportunityShortText5": e["sam_url"],
        }
        for field, value in (("ProposalDueDate", e["due"]), ("OpportunityDate1", e["posted"]),
                             ("OpportunityDate2", e["archive"])):
            if value:
                fields[field] = value
        return fields

    def _note(self, e: dict) -> str:
        """Contracting office and points of contact (the SAM.gov link leads the Description)."""
        lines = []
        if e["office"] or e["office_addr"]:
            lines.append("Contracting office: " + " — ".join(p for p in [e["office"], e["office_addr"]] if p))
        for p in e["pocs"]:
            detail = " · ".join(x for x in [p["email"], p["phone"]] if x)
            name = f"{p['first']} {p['last']}" if p["first"] else p["name"]  # proper case when SAM sends ALL CAPS
            lines.append(f"{(p['type'] or 'POC').title()} POC: {name}" + (f" · {detail}" if detail else ""))
        return "\n".join(lines)

    def _new_payload(self, opp: dict, e: dict, client_id: int) -> dict:
        synopsis = e["synopsis"] or "Synopsis not available — see the SAM.gov link above."
        payload = {
            "OpportunityName":        (opp.get("title") or opp["noticeId"])[:255],
            "ClientId":               client_id,
            "StageId":                self.stage_id,
            "ActiveInd":              1,
            "ExternalId":             opp["noticeId"],
            "OpportunityDescription": f"SAM.gov: {e['sam_url']}\n\n{synopsis}"[:30000],
            "Note":                   self._note(e),
            "OpportunityShortText4":  f"SAM.gov {self.monitor} monitor",
            **self._sam_owned(e, opp),
        }
        place = e["place"]
        for field, value in (("Address1", place["address1"]), ("City", place["city"]),
                             ("State", place["state"]), ("PostalCode", place["zip"]),
                             ("Country", place["country"])):
            if value:
                payload[field] = value
        return payload

    # ── NAICS (Unanet's Categorization field) ────────────────────────────────
    def set_naics(self, opp_id: int, e: dict) -> None:
        """Attach the notice's NAICS codes. Re-posting a code is a no-op; codes the team
        added by hand are kept; codes Unanet doesn't recognize are dropped with a warning."""
        codes = [c.strip() for c in e["naics"].split(",") if c.strip()]
        if not codes:
            return
        try:
            saved = {n.get("Code") for n in self._post(f"/api/opportunities/{opp_id}/naics",
                                                       [{"Code": c} for c in codes])}
            missing = [c for c in codes if c not in saved]
            if missing:
                log.warning("Unanet: NAICS %s not recognized for opportunity %s", ", ".join(missing), opp_id)
        except Exception as ex:
            log.error("Unanet: NAICS for opportunity %s failed: %s", opp_id, ex)

    # ── Contacts ─────────────────────────────────────────────────────────────
    def _contact_id(self, poc: dict, company_id: int) -> Optional[int]:
        email = poc["email"]
        if email in self._contacts:
            return self._contacts[email]
        hits = self._find("contacts", f'Email:"{email}"', "ContactId",
                          lambda h: email in {(h.get("Email") or "").lower(),
                                              (h.get("BusinessEmailAddress") or "").lower()})
        if hits:
            cid = min(hits)
        else:
            cid = self._post("/api/contacts", [{
                "FirstName": poc["first"], "LastName": poc["last"], "CompanyId": company_id,
                "Email": email, **({"Title": poc["title"]} if poc["title"] else {}),
            }])[0]["ContactId"]
            if poc["phone"]:
                self._post(f"/api/contacts/{cid}/addresses",
                           [{"AddressType": "Office", "DefaultInd": True, "Phone": poc["phone"]}])
            log.info("Unanet: created contact %s %s (%s)", poc["first"], poc["last"], cid)
            self.counts["contacts created"] += 1
        self._contacts[email] = cid
        return cid

    def link_contacts(self, opp_id: int, company_id: int, e: dict) -> None:
        """Link each named, emailed POC to the opportunity (skips ones already linked)."""
        try:
            linked = {(a.get("Contact") or {}).get("ContactId")
                      for a in self._get(f"/api/opportunities/{opp_id}/contacts")}
            for poc in e["pocs"]:
                if not (poc["email"] and poc["first"] and poc["last"]):
                    continue  # group mailboxes / unnamed POCs stay in the Note only
                cid = self._contact_id(poc, company_id)
                if cid in linked:
                    continue
                self._post(f"/api/opportunities/{opp_id}/contacts", [{
                    "Contact": {"ContactId": cid},
                    "ContactRole": {"ContactRoleID": DEFAULT_ROLE_ID},
                    "PrimaryContact": poc["type"] == "primary",
                    "Notes": f"SAM.gov {poc['type'] or ''} point of contact".replace("  ", " "),
                }])
                linked.add(cid)
        except Exception as ex:
            log.error("Unanet: contacts for opportunity %s failed: %s", opp_id, ex)
            self.counts["contact errors"] += 1

    # ── Main entry point ─────────────────────────────────────────────────────
    def upsert(self, opp: dict, dry_run: bool = False) -> str:
        """Create or update one opportunity. Returns created|updated|unchanged|unmatched|failed."""
        notice_id = opp.get("noticeId") or ""
        title = (opp.get("title") or "")[:70]
        e = opp.get("_e") or enrich.enrich(opp)
        would = "[DRY RUN] would " if dry_run else ""
        try:
            match = best_match(opp.get("fullParentPathCode") or "", self.codes)
            if not match:
                log.info("Unanet: no matching company for %s (%s) — skipped", title, opp.get("fullParentPathCode"))
                return self._count("unmatched")
            client_id, client_name = match

            existing = self.find_opportunity(notice_id)
            if existing:
                opp_id = existing["OpportunityId"]
                record = self._get(f"/api/opportunities/{opp_id}")
                sam = self._sam_owned(e, opp)
                current = {k: str(record.get(k) or "") for k in sam}
                for k in ("ProposalDueDate", "OpportunityDate1", "OpportunityDate2"):
                    if k in current:
                        current[k] = current[k][:10]  # "2026-10-15T00:00:00"
                changes = {k: v for k, v in sam.items() if current[k] != v}
                if changes:
                    log.info("Unanet: %supdate %s (%s): %s", would, opp_id, title, sorted(changes))
                if not dry_run:
                    if changes:
                        self._put(f"/api/opportunities/{opp_id}", record, changes)
                    self.set_naics(opp_id, e)
                    self.link_contacts(opp_id, record.get("ClientId") or client_id, e)
                return self._count("updated" if changes else "unchanged")

            log.info("Unanet: %screate %s -> %s (%s)", would, title, client_name, client_id)
            if not dry_run:
                opp_id = self._post("/api/opportunities", [self._new_payload(opp, e, client_id)])[0]["OpportunityId"]
                log.info("Unanet: created OpportunityId %s", opp_id)
                self.set_naics(opp_id, e)
                self.link_contacts(opp_id, client_id, e)
            return self._count("created")
        except Exception as ex:
            log.error("Unanet: failed for %s (%s): %s", notice_id, title, ex)
            return self._count("failed")

    def _count(self, outcome: str) -> str:
        self.counts[outcome] += 1
        return outcome

    def summary(self, dry_run: bool = False) -> str:
        c = self.counts
        would = "would be " if dry_run else ""
        parts = [f"{c['created']} {would}created", f"{c['updated']} {would}updated",
                 f"{c['unmatched']} no matching company", f"{c['failed']} failed"]
        if c["unchanged"]:
            parts.insert(2, f"{c['unchanged']} already current")
        if c["contacts created"]:
            parts.append(f"{c['contacts created']} new contacts")
        if c["contact errors"]:
            parts.append(f"{c['contact errors']} contact errors")
        return ", ".join(parts) + ("" if self.env == "prod" else f" ({self.env})")
