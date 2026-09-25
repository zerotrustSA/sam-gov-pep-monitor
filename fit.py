"""
IPSecure fit: keep only cyber / IT / RMF / software-development opportunities.

Runs after a monitor's contract-type filter (CSO, IDIQ, BPA, ...). Signals, strongest first:
  1. Cyber terms in the title (RMF, ATO, zero trust, cybersecurity, ...) -> fit, whatever else it says
     SBIR/STTR announcements                                                -> fit (topic lists need a human look)
  2. Construction / A-E / repair / supply codes or title words             -> not fit
  3. IT NAICS or PSC codes, or IT terms in the title                        -> fit
  4. Otherwise (generic CSO/BAA titles, no codes): read the synopsis        -> fit only with IT/cyber signal

assess() returns (fit, reason); the reason is shown on the Teams run summary so
filtered notices never disappear silently.
"""

import re

# ── Codes ────────────────────────────────────────────────────────────────────
IT_NAICS = {"541511", "541512", "541513", "541519", "518210", "519290", "611420"}
NON_FIT_NAICS_PREFIXES = (
    "23",                                   # construction
    "31", "32", "33",                       # manufacturing (parts, equipment, supplies)
    "42", "44", "45",                       # wholesale / retail
    "48", "49",                             # transportation, warehousing
    "56",                                   # facilities support, janitorial, landscaping, waste
    "81",                                   # repair and maintenance
    "11", "21", "22", "72",                 # agriculture, mining, utilities, food service
    "54131", "54132", "54134", "54135", "54136",  # architecture, landscape arch., drafting, inspection, surveying
)
# PSC: D = IT & telecom services, 7 = IT products. Letter families below are
# construction, A-E, repair, facilities and other non-IT services. Numeric PSCs
# are supply classes (parts, materials) except 70 (legacy IT equipment) and 58 (comms).
NON_FIT_PSC_FAMILIES = set("YZCEFGHMPQSVWX")
NON_FIT_PSC_EXCEPTIONS = ("J070", "N070")      # IT equipment maintenance / installation


def _code_signal(naics_list: list[str], psc: str) -> tuple[int, str]:
    """+1 IT code, -1 non-fit code, 0 neutral/none."""
    psc = (psc or "").upper()
    if any(n in IT_NAICS for n in naics_list):
        return 1, "IT NAICS"
    if psc.startswith("D") or psc.startswith("7"):
        return 1, f"IT PSC {psc}"
    if psc and not psc.startswith(NON_FIT_PSC_EXCEPTIONS):
        if psc[0] in NON_FIT_PSC_FAMILIES or psc[0] in "JN":
            return -1, f"non-IT service PSC {psc}"
        if psc[0].isdigit() and not psc.startswith(("58", "59")):
            return -1, f"supply PSC {psc}"
    if any(n.startswith(NON_FIT_NAICS_PREFIXES) for n in naics_list):
        return -1, f"non-IT NAICS {naics_list[0]}"
    return 0, ""


# ── Keywords ─────────────────────────────────────────────────────────────────
def _rx(words: list[str]) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z])(" + "|".join(words) + r")(?![A-Za-z])", re.IGNORECASE)


CYBER = _rx([
    r"cyber\w*", r"information security", r"infosec", r"RMF", r"risk management framework",
    r"authority to operate", r"authorization to operate", r"zero[- ]trust", r"security operations",
    r"SOC", r"SIEM", r"vulnerabilit\w+", r"penetration test\w*", r"pen[- ]?test\w*", r"red team\w*",
    r"incident response", r"threat hunt\w*", r"ICAM", r"identity,? credential", r"identity and access",
    r"continuous monitoring", r"ConMon", r"STIGs?", r"eMASS", r"800-53", r"800-171", r"FedRAMP",
    r"CMMC", r"OSCAL", r"ISSO", r"ISSM", r"security controls?", r"DevSecOps", r"ICD[- ]503",
])
ATO = re.compile(r"(?<![A-Za-z])ATOs?(?![A-Za-z])")      # case-sensitive: avoid "ato" inside words
IT = _rx([
    r"software", r"information technology", r"cloud", r"data analytics", r"data management",
    r"artificial intelligence", r"machine learning", r"DevOps", r"application development",
    r"app development", r"agile", r"network\w*", r"enterprise services", r"digital", r"C4ISR", r"C5ISR",
    r"C3I", r"command and control", r"automation", r"systems integration", r"database\w*",
    r"help ?desk", r"service desk", r"end[- ]user", r"data cent(er|re)s?", r"SaaS",
    r"enclave", r"web (app\w*|services?|development)", r"IT (services|support|modernization|infrastructure)",
    r"computer\w*", r"server\w*", r"telecommunications", r"data link", r"analytics", r"AI/ML",
    r"e-?learning", r"learning management", r"LMS", r"portal", r"websites?", r"mobile app\w*",
])
SBIR = re.compile(r"(?<![A-Za-z])(SBIR|STTR|small business innovation research|small business technology transfer)(?![A-Za-z])",
                  re.IGNORECASE)
# Standard DoD compliance clauses: count in a title, but not on their own in a synopsis.
BOILERPLATE = {"cmmc", "800-171"}
WEAK_IT = re.compile(r"portal|websites?|computer|digital", re.IGNORECASE)  # title-only IT signals
IT_CASE = re.compile(r"(?<![A-Za-z])(IT|AI)(?![A-Za-z])")  # "IT"/"AI" only when capitalized
NON_FIT = _rx([
    r"construct\w*", r"design[- ]build", r"design[- ]bid[- ]build", r"MACC", r"A-?E", r"A&E",
    r"architects?", r"architectural", r"roof\w*", r"paving", r"asphalt", r"HVAC", r"plumbing", r"electrical (work|repair|upgrade)s?",
    r"janitorial", r"custodial", r"refuse", r"trash", r"waste", r"landscap\w*", r"grounds",
    r"mowing", r"trees?", r"snow removal", r"dredg\w*", r"demolition", r"renovat\w*",
    r"repair(s)? (of|and|to)", r"facilit(y|ies) maintenance", r"building maintenance",
    r"vehicle maintenance", r"spare parts", r"repair parts", r"parts", r"valves?", r"pumps?",
    r"engines?", r"bearings?", r"gaskets?", r"hoses?", r"tires?", r"lumber", r"poles", r"gases",
    r"cylinders?", r"fuel", r"ammunition", r"uniforms?", r"furniture", r"food", r"subsistence",
    r"coatings?", r"painting", r"pest control", r"elevators?", r"fenc(e|ing)", r"boilers?",
    r"generators?", r"water treatment", r"sewer", r"laundry", r"dining", r"moving services",
    r"trucks?", r"vessels?", r"concrete", r"steel", r"pipes?", r"environmental remediation",
    r"utilit(y|ies)", r"lodging", r"medical supplies", r"pharmac\w+",
])


def _hits(pattern: re.Pattern, text: str) -> set[str]:
    return {m.group(0).lower() for m in pattern.finditer(text or "")}


def _it_hits(text: str) -> set[str]:
    return _hits(IT, text) | {m.group(0) for m in IT_CASE.finditer(text or "")}


def _cyber_hits(text: str) -> set[str]:
    return _hits(CYBER, text) | {m.group(0) for m in ATO.finditer(text or "")}


def assess(opp: dict, e: dict) -> tuple[bool, str]:
    """(fit, reason) from title + codes. Reason 'needs synopsis' when undecided."""
    title = opp.get("title") or ""
    naics = [n.strip() for n in (e.get("naics") or "").split(",") if n.strip()]
    code, code_reason = _code_signal(naics, e.get("psc"))

    cyber = _cyber_hits(title)
    if cyber:
        return True, "cyber in title: " + ", ".join(sorted(cyber))
    if SBIR.search(title):
        return True, "SBIR/STTR — check the topic list"
    non_fit = _hits(NON_FIT, title)
    if code < 0 or (non_fit and code <= 0):
        return False, code_reason if code < 0 else "title: " + ", ".join(sorted(non_fit))
    if code > 0:
        return True, code_reason
    it = _it_hits(title)
    if it:
        return True, "IT in title: " + ", ".join(sorted(it))
    return False, "needs synopsis"


def assess_synopsis(synopsis: str) -> tuple[bool, str]:
    """For notices the title and codes can't decide (typical of CSOs and BAAs)."""
    if not synopsis:
        return False, "no IT/cyber signal (no synopsis available)"
    cyber, it, non_fit = _cyber_hits(synopsis), _it_hits(synopsis), _hits(NON_FIT, synopsis)
    cyber -= BOILERPLATE  # compliance clauses in most DoD solicitations; they don't make a notice cyber work
    it = {w for w in it if not WEAK_IT.match(w)}  # submission logistics ("upload via the portal"), not the work
    if cyber:
        return True, "cyber in synopsis: " + ", ".join(sorted(cyber)[:4])
    if len(it) >= 2 and len(it) >= len(non_fit):
        return True, "IT in synopsis: " + ", ".join(sorted(it)[:4])
    if non_fit:
        return False, "synopsis: " + ", ".join(sorted(non_fit)[:4])
    return False, "no IT/cyber signal in synopsis"


def category(reason: str) -> str:
    """Group a not-fit reason for the run summary."""
    r = reason.lower()
    if "supply psc" in r or "naics 3" in r or "naics 4" in r or "synopsis: " in r and "parts" in r:
        return "parts / supplies"
    if "no it/cyber" in r:
        return "no IT/cyber signal"
    return "construction / facilities / other services"
