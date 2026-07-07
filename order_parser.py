"""
Supreme Court order / judgment parser — ASD chamber app.

Handles both document templates, verified against real samples:

  A. RECORD OF PROCEEDINGS (daily order)
     Keys: ITEM NO.N / COURT NO.N / SECTION, case-type + number line,
     parties (X Petitioner(s) VERSUS Y Respondent(s)), "Date : DD-MM-YYYY",
     CORAM block, disposition inside the O R D E R body.

  B. Reportable judgment
     Keys: "IN THE SUPREME COURT OF INDIA", CIVIL APPEAL NO(S). n/yyyy,
     ...APPELLANT / VERSUS / ...RESPONDENT, author judge ("NAME, J."),
     date at foot ("NEW DELHI; MONTH DD, YYYY"), disposition in the
     final numbered paragraphs.

Disposition classification looks at the TAIL of the document (final
operative paragraphs), because historical narration earlier in a judgment
routinely contains "dismissed"/"allowed" about the courts below.

Status mapping (drives the app's live/closed chip):
  allowed | dismissed | withdrawn | disposed        -> matter DISPOSED
  notice_issued | adjourned | listed_next | unknown -> matter LIVE

Also extracts a best-effort next listing date ("list on DD.MM.YYYY",
"list after N weeks") for next-date-change flags.

Input: pdftotext -layout output.
"""

import re
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CourtDocument:
    doc_kind: str = ""            # "order" | "judgment" | "unknown"
    case_number: str = ""         # e.g. "SLP(C) 22381/2026" / "C.A. 10724/2016"
    item: str = ""
    court: str = ""
    section: str = ""
    petitioner: str = ""
    respondent: str = ""
    date: str = ""                # ISO yyyy-mm-dd when parseable
    coram: str = ""
    author_judge: str = ""
    disposition: str = "unknown"  # allowed|dismissed|withdrawn|disposed|notice_issued|adjourned|listed_next|unknown
    status: str = "live"          # live | disposed
    next_date: str = ""           # ISO, if the order fixes one
    operative_line: str = ""      # the sentence the disposition was read from


# ------------------------------------------------------------ template A

ITEM_RE = re.compile(r"ITEM NO\.?\s*(\d+)")
COURTNO_RE = re.compile(r"COURT NO\.?\s*(\d+)")
SECTION_RE = re.compile(r"SECTION\s+([A-Z0-9-]+)")
ORDER_DATE_RE = re.compile(r"Date\s*:\s*(\d{2})-(\d{2})-(\d{4})")
CASE_LINE_RE = re.compile(
    r"(Petition\(s\) for Special Leave to Appeal\s*\((C|Crl)\)|"
    r"Civil Appeal No\(s\)|Criminal Appeal No\(s\)|"
    r"Writ Petition\s*\((?:C|Crl)\)|Transfer Petition\s*\((?:C|Crl)\))"
    r".{0,60}?No\(s\)\.\s*([0-9-]+/\d{4})", re.DOTALL)

# ------------------------------------------------------------ template B

JUDG_CASE_RE = re.compile(
    r"(CIVIL|CRIMINAL)\s+APPEAL\s+NO\(?S?\)?\.?\s*([0-9-]+\s*/\s*\d{4})")
APPELLANT_RE = re.compile(r"^\s*(.+?)\s*(?:…|\.\.\.)\s*APPELLANT", re.MULTILINE)
RESPONDENT_RE = re.compile(r"^\s*(.+?)\s*(?:…|\.\.\.)\s*RESPONDENT", re.MULTILINE)
AUTHOR_RE = re.compile(r"^\s*([A-Z][A-Z .]+),\s*J\.\s*$", re.MULTILINE)
FOOT_DATE_RE = re.compile(
    r"NEW DELHI[;,]?\s*\n?\s*"
    r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|"
    r"OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2}),?\s+(\d{4})", re.IGNORECASE)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}

HONBLE_RE = re.compile(r"HON'BLE\s+(?:MR\.|MS\.|DR\.)?\s*JUSTICE\s+(.+?)\s*$")

# --------------------------------------------------- disposition (tail scan)

# (pattern, disposition) — order matters; first hit in the tail wins.
DISPOSITION_PATTERNS = [
    (re.compile(r"dismissed as withdrawn", re.I), "withdrawn"),
    (re.compile(r"(appeal|appeals|petition|petitions|slp)s?\b[^.\n]{0,80}?"
                r"\b(is|are|stands?|stand)\b[^.\n]{0,40}?\ballowed\b", re.I), "allowed"),
    (re.compile(r"(special leave petition|appeal|petition|slp)s?\b"
                r"[^.\n]{0,80}?\bdismissed\b", re.I), "dismissed"),
    (re.compile(r"(petition|appeal|matter)s?\b[^.\n]{0,60}?"
                r"\bdisposed of\b", re.I), "disposed"),
    (re.compile(r"\bissue notice\b|\bnotice (?:be )?issued\b", re.I), "notice_issued"),
    (re.compile(r"\badjourned\b", re.I), "adjourned"),
    (re.compile(r"\blist (?:the matter |this matter |it )?(?:on|after)\b", re.I),
     "listed_next"),
]

DISPOSED_SET = {"allowed", "dismissed", "withdrawn", "disposed"}

NEXT_DATE_RE = re.compile(
    r"list[^.\n]{0,40}?\bon\b[^.\n]{0,20}?(\d{1,2})[./-](\d{1,2})[./-](\d{4})", re.I)
NEXT_WEEKS_RE = re.compile(r"list[^.\n]{0,40}?after\s+(\w+)\s+weeks?", re.I)
WORD_NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "eight": 8, "ten": 10, "twelve": 12}

CASE_TAG = {
    "Petition(s) for Special Leave to Appeal (C)": "SLP(C)",
    "Petition(s) for Special Leave to Appeal (Crl)": "SLP(Crl)",
    "Civil Appeal No(s)": "C.A.",
    "Criminal Appeal No(s)": "Crl.A.",
}


def _tail(text, frac=0.35, min_chars=1800):
    n = len(text)
    start = max(0, min(int(n * (1 - frac)), n - min_chars))
    return text[start:]


def classify_disposition(text):
    # normalise whitespace: operative sentences wrap across lines in layout
    # text ("...is, accordingly,\ndismissed") and must still match.
    tail = " ".join(_tail(text).split())
    # the e-sign stamp is embedded mid-page in SC PDFs and pollutes sentences
    tail = re.sub(r"Signature Not Verified|Digitally signed by\s+\S+(\s+\S+)?|"
                  r"Date:\s*\d{4}\.\d{2}\.\d{2}\s*\S*|Reason:\s*\S*", "", tail)
    tail = " ".join(tail.split())
    for pat, label in DISPOSITION_PATTERNS:
        m = pat.search(tail)
        if m:
            s = tail.rfind(".", 0, m.start())
            e = tail.find(".", m.end())
            line = tail[s + 1: e + 1 if e != -1 else None]
            return label, line.strip()[:220]
    return "unknown", ""


def extract_next_date(text, base_date: Optional[dt.date] = None):
    tail = _tail(text)
    m = NEXT_DATE_RE.search(tail)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            return ""
    m = NEXT_WEEKS_RE.search(tail)
    if m and base_date:
        w = WORD_NUMS.get(m.group(1).lower())
        if w is None and m.group(1).isdigit():
            w = int(m.group(1))
        if w:
            return (base_date + dt.timedelta(weeks=w)).isoformat()
    return ""


def parse_court_document(layout_text: str) -> CourtDocument:
    doc = CourtDocument()
    text = layout_text

    is_order = "RECORD OF PROCEEDINGS" in text[:2500]
    is_judg = (not is_order) and ("APPELLANT" in text[:3000]
                                  or "REPORTABLE" in text[:600])
    doc.doc_kind = "order" if is_order else ("judgment" if is_judg else "unknown")

    if is_order:
        m = ITEM_RE.search(text[:400]);    doc.item = m.group(1) if m else ""
        m = COURTNO_RE.search(text[:400]); doc.court = m.group(1) if m else ""
        m = SECTION_RE.search(text[:400]); doc.section = m.group(1) if m else ""
        m = CASE_LINE_RE.search(text[:2500])
        if m:
            head = m.group(1)
            tag = CASE_TAG.get(head)
            if tag is None:
                tag = head.replace("No(s)", "").strip()
                if "Special Leave" in head:
                    tag = f"SLP({m.group(2)})"
            num = m.group(3) if m.lastindex >= 3 else m.group(m.lastindex)
            doc.case_number = f"{tag} {num}".strip()
        m = ORDER_DATE_RE.search(text)
        if m:
            doc.date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        # parties: line ending Petitioner(s), then after VERSUS line ending Respondent(s)
        pm = re.search(r"^\s*(.+?)\s{2,}Petitioner\(s\)", text, re.MULTILINE)
        rm = re.search(r"^\s*(.+?)\s{2,}Respondent\(s\)", text, re.MULTILINE)
        doc.petitioner = pm.group(1).strip() if pm else ""
        doc.respondent = rm.group(1).strip() if rm else ""
        # coram
        cor = []
        cm = re.search(r"CORAM\s*:(.*?)(?:For Petitioner|For Respondent|UPON)",
                       text, re.DOTALL)
        if cm:
            for line in cm.group(1).split("\n"):
                h = HONBLE_RE.search(line)
                if h:
                    cor.append(h.group(1).strip())
        doc.coram = "; ".join(cor)

    elif is_judg:
        m = JUDG_CASE_RE.search(text[:1500])
        if m:
            tag = "C.A." if m.group(1).upper() == "CIVIL" else "Crl.A."
            doc.case_number = f"{tag} {m.group(2).replace(' ', '')}"
        pm = APPELLANT_RE.search(text[:3000])
        rm = RESPONDENT_RE.search(text[:3000])
        doc.petitioner = pm.group(1).strip() if pm else ""
        # respondent names often span multiple lines between VERSUS and the
        # ...RESPONDENT marker — collect them all, not just the marker line.
        if rm:
            head = text[:3000]
            vpos = head.upper().rfind("VERSUS", 0, rm.start())
            if vpos != -1:
                block = head[vpos + len("VERSUS"): rm.end()]
                block = re.sub(r"(?:…|\.\.\.)\s*RESPONDENT.*", "", block,
                               flags=re.DOTALL)
                parts = [ln.strip().rstrip(",") for ln in block.split("\n")
                         if ln.strip()]
                doc.respondent = " ".join(parts)
            else:
                doc.respondent = rm.group(1).strip()
        am = AUTHOR_RE.search(text[:4000])
        doc.author_judge = am.group(1).strip() if am else ""
        fm = FOOT_DATE_RE.search(_tail(text, frac=0.15, min_chars=800))
        if fm:
            doc.date = dt.date(int(fm.group(3)), MONTHS[fm.group(1).upper()],
                               int(fm.group(2))).isoformat()

    doc.disposition, doc.operative_line = classify_disposition(text)
    doc.status = "disposed" if doc.disposition in DISPOSED_SET else "live"
    base = None
    if doc.date:
        try:
            base = dt.date.fromisoformat(doc.date)
        except ValueError:
            pass
    doc.next_date = extract_next_date(text, base)
    return doc
