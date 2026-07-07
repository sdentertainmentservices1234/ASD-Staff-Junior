"""
Hardened Supreme Court cause-list parser.

Tuned against BOTH supplementary (F_J / M_R) and MAIN (M_J) lists.

Addresses the six main-list divergences vs. the supplementary-only tuning:

  1. Coram appears ONCE per court (only when a HON'BLE line follows the
     COURT NO. header). Continuation pages repeat COURT NO. with no coram.
     -> We carry coram forward per court+time-slot and only overwrite it
        when a fresh HON'BLE block actually appears.

  2. One PDF spans all courts (11..15). Court number comes from the running
     header, never the filename/suffix.

  3. Coram can differ within one court across time-slots (10:30 vs 12:00
     Special Bench re-assembly). We key the coram on (court, time_slot).

  4. Item numbers restart per section; connected items use decimals (3.1).
     The connected row carries "Connected" in the case-no column and the
     real case number wraps to the next line. We capture parent + child.

  5. Case-No column has many types: SLP(C)/SLP(Crl)/Diary/T.P.(C)/T.P.(Crl.)/
     W.P.(C)/W.P.(Crl.)/Crl.A./C.A./MA...in... plus (SCLSC) and roman
     sub-codes on the following line. Number normalisation spans all forms.

  6. Section bands ([BAIL MATTERS] etc.) are the row category. Party-role
     bracket tokens ([R-1],[CAVEAT],[P-1]) must NOT be treated as sections.

Input is the output of:  pdftotext -layout <file>.pdf -
We rely on left-column x-position (preserved by -layout) rather than on
fragile whole-line regexes.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


# ---- known section-band strings (anchor, so party-role brackets don't match)
SECTION_BANDS = {
    "BAIL MATTERS",
    "FRESH (FOR ADMISSION) - CIVIL CASES",
    "FRESH (FOR ADMISSION) - CRIMINAL CASES",
    "FRESHLY / ADJOURNED MATTERS",
    "AFTER NOTICE (FOR ADMISSION) - CIVIL CASES",
    "AFTER NOTICE (FOR ADMISSION) - CRIMINAL CASES",
    "POST-NOTICE-AD INTERIM STAY MATTERS",
    "TRANSFER PETITIONS",
    "LEGAL AID MATTERS",
    "ORDERS (INCOMPLETE MATTERS / IAs / CRLMPs)",
    "PART HEARD MATTERS",
    "MISCELLANEOUS HEARING",
}

# Case-type tokens we accept in the Case No. column (order matters: longest first)
CASE_TYPE_RE = re.compile(
    r"""^(
        SLP\(Crl\)\ No\. | SLP\(C\)\ No\. |
        T\.P\.\(Crl\.\)\ No\. | T\.P\.\(C\)\ No\. |
        W\.P\.\(Crl\.\)\ No\. | W\.P\.\(C\)\ No\. |
        Crl\.A\.\ No\. | C\.A\.\ No\. |
        MA\ [0-9-]+/[0-9]{4}\ in |
        Diary\ No\.
    )""",
    re.VERBOSE,
)

# Standalone centered header only — NOT a court number inside a note sentence
# (e.g. "...AFTER ... COURT NO. 15 IS OVER."). Anchored to whole line.
COURT_HDR_RE = re.compile(r"^\s*COURT NO\.?\s*:?\s*(\d+)\s*$")
# Loose form retained only for detecting in-sentence refs we must ignore.
COURT_INLINE_RE = re.compile(r"COURT NO\.?\s*:?\s*(\d+)")
HONBLE_RE = re.compile(r"HON'BLE\s+(?:MR\.|MS\.|DR\.)?\s*(?:JUSTICE\s+)?(.+?)\s*$")
TIME_RE = re.compile(r"\(TIME\s*:\s*([0-9:]+\s*[AP]M)\)")
ROMAN_SUB_RE = re.compile(r"^(\(SCLSC\)\s*)?[IVXLC]+(-[A-Z])?$")
DIARY_NUM_RE = re.compile(r"Diary No\.\s*([0-9]+-[0-9]{4})")
CASE_NUM_RE = re.compile(r"No\.\s*([0-9]+(?:-[0-9]+)?/[0-9]{4})")

# Left-column geometry (from -layout on the real PDFs).
# Item number begins at col 0; case-no column ~col 5; parties column ~col 28.
ITEM_NUM_COL_MAX = 5          # item number sits in the first ~5 chars
CASE_COL_START = 4            # case-no / "Connected" begins around here
PARTIES_COL_START = 25        # petitioner/respondent begins around here


@dataclass
class Match:
    court: str = ""
    coram: str = ""
    time_slot: str = ""
    section: str = ""
    item: str = ""
    connected_to: Optional[str] = None
    case_type: str = ""
    case_number: str = ""          # normalised e.g. "SLP(C) 22381/2026" or "D-13415/2026"
    diary_number: str = ""         # normalised e.g. "13415-2026" when Diary form
    petitioner: str = ""
    respondent: str = ""
    is_supplementary: bool = False
    raw_case_col: str = ""


def normalise_number(case_type: str, case_col_text: str) -> tuple[str, str]:
    """Return (normalised_case_number, diary_number)."""
    diary = ""
    num = ""
    dm = DIARY_NUM_RE.search(case_col_text)
    if dm:
        diary = dm.group(1)
        num = f"D-{diary}"
        return num, diary
    cm = CASE_NUM_RE.search(case_col_text)
    if cm:
        num = cm.group(1)
    # attach a short type tag for display/matching
    tag = (case_type
           .replace(" No.", "")
           .replace("MA ", "MA")
           .strip())
    return (f"{tag} {num}".strip() if num else case_col_text.strip()), diary


def _col(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse(layout_text: str, is_supplementary: bool = False) -> list[Match]:
    lines = layout_text.split("\n")
    matches: list[Match] = []

    cur_court = ""
    cur_time = ""
    # coram keyed on (court, time_slot); only set when a HON'BLE block appears
    coram_by_slot: dict[tuple[str, str], list[str]] = {}
    cur_section = ""
    pending_honble: list[str] = []
    saw_court_header_recently = False

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\n")
        stripped = line.strip()

        # --- court header (running, may repeat on continuation w/o coram)
        chm = COURT_HDR_RE.search(line)
        if chm and "DAILY CAUSE LIST" not in line:
            cur_court = chm.group(1)
            saw_court_header_recently = True
            pending_honble = []
            i += 1
            continue

        tm = TIME_RE.search(line)
        if tm:
            cur_time = tm.group(1)
            # a new time-slot binds whatever coram we just collected
            if pending_honble:
                coram_by_slot[(cur_court, cur_time)] = pending_honble[:]
                pending_honble = []
            saw_court_header_recently = False
            i += 1
            continue

        # --- coram: only right after a court header (point 1 & 3)
        hm = HONBLE_RE.search(line)
        if hm and saw_court_header_recently:
            name = hm.group(1).strip()
            # guard against stray HON'BLE inside notes
            if name and "APPRECIATED" not in name:
                pending_honble.append(name)
                i += 1
                continue

        # Header-block boilerplate we skip WITHOUT closing the block, so the
        # HON'BLE lines that follow a NOTE / bench line still bind (point 1).
        _boiler = ("PARTIAL COURT" in stripped or "BENCH" in stripped
                   or "MISCELLANEOUS" in stripped or stripped.startswith("NOTE")
                   or stripped.startswith("[SPECIAL BENCH]")
                   or "THIS BENCH WILL" in stripped or not stripped)
        if _boiler:
            i += 1
            continue

        # A genuine content line (section band or item) closes the header block.
        if stripped and not chm and not hm:
            if pending_honble:
                # no TIME line seen (rare) -> bind to blank slot for this court
                coram_by_slot.setdefault((cur_court, cur_time), pending_honble[:])
                pending_honble = []
            saw_court_header_recently = False

        # --- section band (point 6): anchored to known strings
        sb = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if sb and sb.group(1).strip() in SECTION_BANDS:
            cur_section = sb.group(1).strip()
            i += 1
            continue

        # --- item row: starts with an item number in the left column.
        # Real item numbers are N or N.N (one decimal digit for a connected
        # sub-item, e.g. 3.1). We explicitly reject:
        #   - times like "12.00 NOON..." (two decimal digits)
        #   - lines whose text after the number is instruction prose, not a
        #     case entry (contain no case-type token AND look like a NOTE).
        im = re.match(r"^(\d+(?:\.\d)?)(?!\d)\s+(.*)$", line)
        connected_hdr = re.match(r"^(\d+\.\d)(?!\d)\s+Connected\b", line)
        # a boilerplate/note guard: SC notes are ALL-CAPS sentences or start
        # with bracketed directions and carry no case-type token.
        _looks_like_note = False
        if im:
            _tail = im.group(2)
            if (re.match(r"^[A-Z][A-Z ,'\"\]\[().-]{18,}$", _tail)
                    or _tail.lstrip().startswith("]")
                    or "TAKE UP LEFT OVER" in _tail
                    or "NOON IS OVER" in _tail):
                _looks_like_note = True
        if im and not _looks_like_note and _col(line) <= ITEM_NUM_COL_MAX:
            item_no = im.group(1)
            rest = im.group(2)
            connected_to = None
            if "." in item_no:
                connected_to = item_no.split(".")[0]

            # Case-No column: normally rest starts with a case-type token or
            # "Connected". For connected rows the real number wraps to next line.
            # Split the row into fields on runs of 2+ spaces. This is robust
            # to the per-court column-edge drift that fixed slicing missed.
            # Fields after the item number: [case-no] [petitioner] [advocate]
            fields = re.split(r"\s{2,}", rest.strip())
            case_col = ""
            petitioner = ""
            if connected_hdr:
                # fields[0] == "Connected"; petitioner follows; case-no wraps below
                petitioner = fields[1].strip() if len(fields) > 1 else ""
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n:
                    case_col = lines[j].strip()
            else:
                case_col = fields[0].strip() if fields else ""
                petitioner = fields[1].strip() if len(fields) > 1 else ""

            # accumulate wrapped case-no continuation (SLP number line, roman sub)
            look = i + 1
            gathered = [case_col]
            while look < n:
                nxt = lines[look]
                nxt_s = nxt.strip()
                if not nxt_s:
                    break
                # continuation of case-no column: indented into case col,
                # left of parties col, and looks like a number or roman code
                if _col(nxt) >= CASE_COL_START and _col(nxt) < PARTIES_COL_START:
                    if re.match(r"^[0-9]", nxt_s) or ROMAN_SUB_RE.match(nxt_s) \
                       or nxt_s.startswith("(SCLSC)") or "No." in nxt_s:
                        gathered.append(nxt_s)
                        look += 1
                        continue
                break
            case_col_full = " ".join(gathered).strip()

            # Separate the roman sub-code (e.g. "XVI-A", "(SCLSC) II-B") from
            # the case-no text so it can never corrupt number extraction.
            roman_sub = ""
            core_parts = []
            for g in gathered:
                if ROMAN_SUB_RE.match(g) or g.startswith("(SCLSC)"):
                    roman_sub = g
                else:
                    core_parts.append(g)
            case_core = " ".join(core_parts).strip()

            ctm = CASE_TYPE_RE.match(case_core)
            case_type = ctm.group(1).strip() if ctm else ""
            case_number, diary = normalise_number(case_type, case_core)

            # respondent: first non-empty line after a "Versus" within block
            respondent = ""
            k = i + 1
            while k < n:
                if lines[k].strip() == "Versus":
                    # next non-empty line, take the parties-column slice
                    m2 = k + 1
                    while m2 < n and not lines[m2].strip():
                        m2 += 1
                    if m2 < n:
                        rl = lines[m2].strip()
                        # respondent is the first field; advocate (if any) follows
                        respondent = re.split(r"\s{2,}", rl)[0].strip()
                    break
                # stop if we hit the next item
                if re.match(r"^\d+(?:\.\d+)?\s", lines[k]) and _col(lines[k]) <= ITEM_NUM_COL_MAX:
                    break
                k += 1

            matches.append(Match(
                court=cur_court,
                coram="; ".join(coram_by_slot.get((cur_court, cur_time), [])),
                time_slot=cur_time,
                section=cur_section,
                item=item_no,
                connected_to=connected_to,
                case_type=case_type,
                case_number=case_number,
                diary_number=diary,
                petitioner=re.split(r"\s{2,}", petitioner)[0].strip() if petitioner else "",
                respondent=respondent,
                is_supplementary=is_supplementary,
                raw_case_col=case_core,
            ))
        i += 1

    return matches


# ---------- matching against a watchlist (number-exact + distinctive token)

COMMON_TOKENS = {
    # personal-name filler
    "singh", "kumar", "state", "union", "india", "santosh", "ram", "devi",
    "prasad", "yadav", "sharma", "gupta", "reddy", "patel", "khan", "das",
    "mohd", "mohammad", "mohammed", "raj", "lal", "chandra", "nath", "vijay",
    # entity / legal filler common on main lists (heavy on company matters)
    "and", "ors", "anr", "the", "of", "ltd", "pvt", "limited", "private",
    "society", "co", "corporation", "mr", "sri", "smt", "industries",
    "company", "enterprises", "traders", "developers", "builders", "agro",
    "national", "insurance", "bank", "cooperative", "coop",
    "authority", "department", "ministry", "commissioner", "officer",
    "council", "board", "trust", "association", "services", "solutions",
    "new", "shree", "shri", "sree",
    # co-operative-society boilerplate (Maharashtra lists are dense with these)
    "seva", "sahakari", "sanstha", "sansthan", "sahakar", "sahakarita",
    "credit", "pat", "patsanstha", "nagari", "nagri", "gramin", "vividh",
    "karyakari", "maryadit", "mydt", "vikas", "seva-sahakari",
}


def distinctive_tokens(title: str) -> set[str]:
    toks = re.findall(r"[A-Za-z]+", title.lower())
    return {t for t in toks if len(t) >= 4 and t not in COMMON_TOKENS}


def norm_num(s: str) -> str:
    """Reduce a case/diary reference to a canonical 'NUMBER/YEAR' key.
    Handles 'SLP (C) No. 22381 / 2026' -> '22381/2026',
            '37773/2026' or '37773-2026' -> '37773/2026',
            'D-13415/2026' -> '13415/2026'.
    Returns '' if no NUMBER/YEAR pattern is present (so junk never matches)."""
    if not s:
        return ""
    s = s.replace("-", "/")
    m = re.search(r"(\d{1,7})\s*/\s*((?:19|20)\d{2})", s)
    if m:
        return f"{int(m.group(1))}/{m.group(2)}"
    return ""


def match_watchlist(parsed: list[Match], watchlist: list[dict]) -> list[dict]:
    """watchlist entry: {diaryNo?, caseNo?, parties?}.

    Matching rules (deliberately strict, after real-world false positives):
      - NUMBER match: canonical NUMBER/YEAR must be EQUAL (no substrings).
        This is the reliable path and requires the parsed row to actually
        carry a case/diary number — note/boilerplate lines carry none, so
        they can no longer masquerade as a numeric match.
      - TITLE match: requires at least TWO shared distinctive tokens, OR one
        shared token that is long (>=6 chars) and rare. A single common-ish
        token (e.g. 'seva'/'sahakari') can no longer trigger a match.
      - A title match is only reported when the parsed row has real parties
        (petitioner/respondent present) — never on a bare note line.
    """
    hits = []
    for w in watchlist:
        w_diary = norm_num(w.get("diaryNo", "") or "")
        w_case = norm_num(w.get("caseNo", "") or "")
        w_nums = {n for n in (w_diary, w_case) if n}
        w_tokens = distinctive_tokens(w.get("parties", "") or "")
        for m in parsed:
            m_nums = {n for n in (norm_num(m.diary_number),
                                  norm_num(m.case_number)) if n}
            by_number = bool(w_nums & m_nums)

            by_title = False
            has_parties = bool((m.petitioner or "").strip() or
                               (m.respondent or "").strip())
            if w_tokens and has_parties:
                m_tokens = distinctive_tokens(m.petitioner + " " + m.respondent)
                shared = w_tokens & m_tokens
                if len(shared) >= 2 or any(len(t) >= 6 for t in shared):
                    by_title = True

            if by_number or by_title:
                hits.append({
                    "watch": w,
                    "matched_on": ("number" if by_number else "") +
                                  ("+title" if by_title and by_number else
                                   ("title" if by_title else "")),
                    "court": m.court,
                    "coram": m.coram,
                    "time_slot": m.time_slot,
                    "section": m.section,
                    "item": m.item,
                    "connected_to": m.connected_to,
                    "case": m.case_number,
                    "petitioner": m.petitioner,
                    "respondent": m.respondent,
                    "supplementary": m.is_supplementary,
                })
    return hits
