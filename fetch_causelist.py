#!/usr/bin/env python3
"""
Supreme Court of India - cause-list & supplementary fetcher (multi-wave).

The court publishes in waves:
  * ADVANCE lists (miscellaneous / regular) days ahead - the earliest warning that
    a matter is coming up. Path: /jonew/cl/advance/{date}/{suffix}.pdf
  * MAIN daily lists a few days ahead. Path: /jonew/cl/{date}/{suffix}.pdf
  * SUPPLEMENTARY lists the evening before the hearing (often 8-9 PM, sometimes
    later). Path: /jonew/cl/{date}/{suffix}.pdf  (the *_2 suffixes)

So a complete picture assembles over several days. This script runs SEVERAL TIMES
A DAY (see the workflow) and, on every run, checks a ROLLING WINDOW of upcoming
hearing days. Each matched matter is enriched with court no., item no., coram
(bench), the court's total & fresh counts, and a MAIN vs SUPPLEMENTARY flag.

Drafting aid only - the court's published list is authoritative.
Free: pure fetch + PDF text, no API keys, no paid services.

Improvements over the first version:
  * also fetches the ADVANCE lists (earlier visibility).
  * name matching requires ALL significant name tokens on the line (so "ADITH S.
    DESHMUKH" still matches the watchlist name "Adith Satish Deshmukh"), which is
    far less brittle than a contiguous-substring match.
  * AOR-code matching, but only when the line actually mentions "AOR" (so a stray
    4-digit number can't false-match).
  * every match records what it matched on (matched_on) and a confidence, so the
    app can show strong vs weak hits.
"""

import io
import json
import re
import sys
import datetime
import urllib.request

# Two URL families. suffix -> (human label, kind) where kind is main/supp/advance.
DAILY_BASE   = "https://api.sci.gov.in/jonew/cl/{date}/{suffix}.pdf"
ADVANCE_BASE = "https://api.sci.gov.in/jonew/cl/advance/{date}/{suffix}.pdf"

# Each entry: (suffix, human label, kind, family). "family" groups a list's main/supp/advance
# variants so "main published" can be tracked PER list-type — the Miscellaneous (court),
# Regular/Final (court), Registrar, and Curative/Review lists each publish on their own schedule.
DAILY_LISTS = [
    ("M_J_1",  "Miscellaneous - Main",              "main", "MJ"),
    ("M_J_2",  "Miscellaneous - Supplementary",     "supp", "MJ"),
    ("F_J_1",  "Regular / Final - Main",            "main", "FJ"),
    ("F_J_2",  "Regular / Final - Supplementary",   "supp", "FJ"),
    ("M_R_1",  "Registrar - Main",                  "main", "MR"),
    ("M_R_2",  "Registrar - Supplementary",         "supp", "MR"),
    ("M_CC_1", "Curative & Review (circulation)",   "main", "MCC"),
]
ADVANCE_LISTS = [
    ("M_J", "Miscellaneous - Advance",   "advance", "MJ"),
    ("F_J", "Regular / Final - Advance", "advance", "FJ"),
]

WINDOW_DAYS = 8
# All at the repo root (matching the existing layout): the workflow runs `python
# fetch_causelist.py`, reads watchlist.json, and writes court-updates.json next to index.html
# so the app can fetch it same-origin.
WATCHLIST_FILE = "watchlist.json"
OUTPUT_FILE = "court-updates.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; chamber-causelist-bot/1.0)"}


def load_watchlist():
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            wl = json.load(f)
    except Exception as e:
        print("Could not read watchlist:", e)
        wl = {}
    for k in ("advocate_names", "aor_codes", "parties", "case_numbers", "diary_numbers"):
        wl.setdefault(k, [])
    return wl


def fetch_pdf(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=45) as resp:
            if resp.status == 200:
                return resp.read()
    except Exception:
        pass
    return None


def pdf_to_text(data):
    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
        return text
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        for page in PdfReader(io.BytesIO(data)).pages:
            text += (page.extract_text() or "") + "\n"
    except Exception as e:
        print("  PDF extraction failed:", e)
    return text


# SC cause-list template columns (595pt-wide page):
#   item-no  x0~43 | case-no  x0 68-160 | PARTIES  x0 185-390 | counsel  x0 426+
# We keep the FULL line for matching (we find our matters BY the counsel name), a
# "left" version with the counsel column dropped (item no + case no + petitioner),
# and a "party" version that is the PARTY COLUMN ONLY (drops both the left case/category
# roman-numeral column and the counsel column) so respondent continuation lines yield
# clean party text with no stray category codes.
ADV_COL_X = 410      # counsel column starts here; drop x0 >= this from titles
PARTY_MIN_X = 170    # party column starts ~185; below this is item-no/case-no/category


def pdf_to_lines(data):
    """Return visual lines as dicts {"full", "left", "party"}. Falls back to plain
    whole-text lines (full == left == party) if word geometry is unavailable."""
    lines = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                # group words into visual lines by their top-y (small tolerance for jitter)
                groups = []
                for w in sorted(page.extract_words(), key=lambda w: w["top"]):
                    if groups and abs(w["top"] - groups[-1][0]) <= 3:
                        groups[-1][1].append(w)
                    else:
                        groups.append((w["top"], [w]))
                for _top, ws in groups:
                    ws = sorted(ws, key=lambda w: w["x0"])
                    full = " ".join(w["text"] for w in ws).strip()
                    left = " ".join(w["text"] for w in ws if w["x0"] < ADV_COL_X).strip()
                    party = " ".join(w["text"] for w in ws
                                     if PARTY_MIN_X <= w["x0"] < ADV_COL_X).strip()
                    if full:
                        lines.append({"full": full, "left": left or full, "party": party})
        if lines:
            return lines
    except Exception:
        pass
    for ln in pdf_to_text(data).splitlines():
        ln = ln.strip()
        if ln:
            lines.append({"full": ln, "left": ln, "party": ln})
    return lines


def norm(s):
    s = (s or "").lower().replace(".", " ").replace(",", " ")
    return re.sub(r"\s+", " ", s).strip()


def norm_num(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def name_tokens(name):
    # significant tokens of a name: drop initials/honorifics, keep words >= 3 chars
    drop = {"adv", "advocate", "mr", "mrs", "ms", "dr", "aor", "the"}
    toks = [t for t in norm(name).split() if len(t) >= 3 and t not in drop]
    return toks


# Court header. The real lists write "COURT NO. : 9" (with a colon) and the
# Registrar lists write "Registrar Court No. 2" — the old pattern (no colon)
# matched NEITHER, so the court number was left stale/wrong. Allow an optional
# colon and any prefix. "CHIEF JUSTICE'S COURT" carries no number → Court 1.
COURT_RE = re.compile(r"court\s*no\.?\s*:?\s*([0-9]+)", re.I)
CJ_RE = re.compile(r"chief\s+justice'?s\s+court", re.I)
TOTAL_RE = re.compile(r"total\s*(?:matters)?\s*[:\-]?\s*([0-9]+)", re.I)
FRESH_RE = re.compile(r"fresh\s*(?:matters)?\s*[:\-]?\s*([0-9]+)", re.I)
# A real item line begins with the item number followed by a case-type token, e.g.
# "150 SLP(C) No. 9386/2023 <parties> <advocate>". We anchor on this so the match's
# court/item and DISPLAY TEXT come from the item's own line (case no. + parties),
# not from a wrapped advocate-name continuation line that merely mentions the name.
ITEM_RE = re.compile(
    r"^\s*(\d{1,4})[.\)]?\s+(?:SLP|W\.?\s*P|C\.?\s*A|Crl|Cri|Diary|T\.?\s*P|R\.?\s*P|"
    r"M\.?\s*A|CONMT|SMC|SMW|Cont|Ref|Curative|Review|Comp|Connected)", re.I)
# Connected sub-matter: "127. Connected <petitioner> <counsel>". It reuses the PARENT's
# item number, and its own case number sits on the NEXT line ("1 Diary No. 25800-2025").
CONNECTED_RE = re.compile(r"^\s*(\d{1,4})[.\)]?\s+Connected\b\s*", re.I)
SUBCASE_RE = re.compile(
    r"^\s*\d{1,3}\s+(?:SLP|W\.?\s*P|C\.?\s*A|Crl|Cri|Diary|T\.?\s*P|R\.?\s*P|M\.?\s*A|Cont|Ref|Comp)",
    re.I)
# Lines that END the parties block of an item (applications, notes, the next item, etc.).
# Everything from the item line up to (but not including) one of these — with the party
# column of each line — forms the cause title "PETITIONER Versus RESPONDENT".
TERM_RE = re.compile(
    r"^\s*(?:IA\s*No|I\.A\.|\{|\[|O\.T\.|FOR\b|WITH\b|LIST|TO\s+BE|Mention|"
    r"APPLICATION|ORDER\b|Diary\s*No\.?\s*-)", re.I)
VERSUS_RE = re.compile(r"^\s*versus\s*$", re.I)


def is_bench_note(line):
    """True for bracketed judge-sitting notes like
    '[HON'BLE MR. JUSTICE MANMOHAN WILL SIT IN COURT NO.3 AT 2 P.M. ...]'.
    These mention a court number that must NOT become the running court."""
    l = line.strip()
    return l.startswith("[") or "WILL SIT" in line.upper()


def scan_text(lines, wl, list_label, list_kind, for_date, family=""):
    # lines: list of {"full", "left"} dicts from pdf_to_lines(). We MATCH on "full"
    # (counsel names included) but take the display text from the item's "left"
    # (counsel column dropped). A plain list of strings is also accepted (full==left).
    lines = [({"full": ln, "left": ln, "party": ln} if isinstance(ln, str) else ln) for ln in lines]

    name_token_sets = [set(name_tokens(x)) for x in wl["advocate_names"] if name_tokens(x)]
    party_token_sets = [set(name_tokens(x)) for x in wl["parties"] if name_tokens(x)]
    num_terms = [norm_num(x) for x in (wl["case_numbers"] + wl["diary_numbers"]) if norm_num(x)]
    aor_codes = [norm_num(x) for x in wl["aor_codes"] if norm_num(x)]

    grouped, order = {}, []
    cur_court = cur_coram = cur_total = cur_fresh = cur_item = ""
    cur_key = None
    # Cause title accumulated per item key: item line + petitioner continuation +
    # "Versus" + respondent, taken from the PARTY column so counsel never leaks in.
    item_titles = {}
    accumulating = False
    pending_subcase = False   # just saw a "Connected" line; next line may be its case no.
    conn_seq = 0              # distinct sub-key per Connected block

    for _rec in lines:
        line = _rec["full"]
        left = _rec["left"]
        party = _rec.get("party", left)
        ln = norm(line)
        line_tokens = set(ln.split())
        lnum = norm_num(line)
        # digit tokens on the line, for AOR-code word matching
        digit_tokens = set(re.findall(r"\d+", line))

        # Court header — but never from a bracketed "[... WILL SIT IN COURT NO.N ...]"
        # bench note, which would otherwise hijack the running court number.
        if not is_bench_note(line):
            cm = COURT_RE.search(line)
            if cm:
                cur_court = cm.group(1)
                cur_coram = ""
                cur_total = cur_fresh = ""
            elif CJ_RE.search(line):
                cur_court = "1"          # Chief Justice's Court is Court No. 1
                cur_coram = ""
                cur_total = cur_fresh = ""
        if not cur_coram and not is_bench_note(line):
            cmatch = re.search(r"(hon'?ble.*)", line, re.I)
            if cmatch:
                cur_coram = re.sub(r"\s+", " ", cmatch.group(1)).strip()[:120]
        tm = TOTAL_RE.search(line)
        if tm:
            cur_total = tm.group(1)
        fm = FRESH_RE.search(line)
        if fm:
            cur_fresh = fm.group(1)

        conn = CONNECTED_RE.match(line)
        im = ITEM_RE.match(line)
        if conn:
            # Connected sub-matter. It REUSES the parent's item number, so give it a
            # DISTINCT key — otherwise it would clobber the parent item's already-built
            # title (that exact bug lost the Mannadheswarar case no. and respondent).
            conn_seq += 1
            cur_item = conn.group(1)
            cur_key = (list_kind, cur_court, cur_item + "#c" + str(conn_seq))
            item_titles[cur_key] = CONNECTED_RE.sub(r"\1 ", left.strip())
            accumulating = True
            pending_subcase = True
        elif im and pending_subcase and SUBCASE_RE.match(line):
            # the Connected matter's case number sits on the NEXT line ("1 Diary No. ...");
            # fold it into the sub-item's title instead of starting a bogus new item.
            item_titles[cur_key] = (item_titles[cur_key] + " " +
                                    re.sub(r"^\s*\d{1,3}\s+", "", left.strip())).strip()
            pending_subcase = False
        elif im:
            cur_item = im.group(1)
            cur_key = (list_kind, cur_court, cur_item)
            # Start the cause title from the item line ("left" = item no + case no +
            # petitioner). Following party-column lines (petitioner cont., Versus,
            # respondent) are appended until a terminator line.
            item_titles[cur_key] = left.strip()
            accumulating = True
            pending_subcase = False
        elif cur_key and accumulating:
            pending_subcase = False
            if VERSUS_RE.match(line):
                item_titles[cur_key] += " Versus "
            elif TERM_RE.match(line):
                accumulating = False
            elif party:
                item_titles[cur_key] = (item_titles[cur_key] + " " + party).strip()
                if len(item_titles[cur_key]) > 180:
                    accumulating = False

        hits = []
        # advocate name: ALL significant tokens of a watchlist name present on the line
        for toks in name_token_sets:
            if toks and toks.issubset(line_tokens):
                hits.append("advocate")
                break
        # case / diary numbers: normalized-digit substring, reasonably long
        for t in num_terms:
            if t and len(t) >= 5 and t in lnum:
                hits.append("number")
                break
        # AOR code: exact digit token, but only when the line mentions AOR
        if "aor" in ln:
            for c in aor_codes:
                if c and c in digit_tokens:
                    hits.append("aor")
                    break
        # party: ALL significant tokens present (weaker signal)
        for toks in party_token_sets:
            if toks and toks.issubset(line_tokens):
                hits.append("party")
                break

        if hits:
            # confidence: a case/diary number or AOR code is strong; name alone medium;
            # party alone weak.
            strong = ("number" in hits) or ("aor" in hits)
            confidence = "high" if strong else ("medium" if "advocate" in hits else "low")
            key = cur_key or (list_kind, cur_court, cur_item or line[:20])
            if key not in grouped:
                grouped[key] = {
                    "for_date": for_date,
                    "list": list_label,
                    "kind": list_kind,
                    "family": family,
                    "is_supplementary": list_kind == "supp",
                    "court": cur_court,
                    "item": cur_item,
                    "coram": cur_coram,
                    "court_total": cur_total,
                    "court_fresh": cur_fresh,
                    "matched_on": set(),
                    "confidence": confidence,
                    "text": "",
                }
                order.append(key)
            g = grouped[key]
            g["matched_on"].update(hits)
            # upgrade confidence if a stronger signal appears later for the same item
            if strong:
                g["confidence"] = "high"
            elif "advocate" in hits and g["confidence"] == "low":
                g["confidence"] = "medium"
            if cur_coram and not g["coram"]:
                g["coram"] = cur_coram
            if cur_total and not g["court_total"]:
                g["court_total"] = cur_total
            if cur_fresh and not g["court_fresh"]:
                g["court_fresh"] = cur_fresh
            # Fallback text = the matched line; the accumulated cause title (item no. +
            # case no. + PETITIONER Versus RESPONDENT) is applied after the loop, once it
            # has finished accumulating past the "Versus"/respondent lines.
            if not g["text"]:
                g["text"] = line[:300]

    out = []
    for key in order:
        g = grouped[key]
        g["matched_on"] = sorted(g["matched_on"])
        title = item_titles.get(key)
        if title:
            g["text"] = re.sub(r"\s+", " ", title).strip()[:300]
        out.append(g)
    return out


def upcoming_days(n):
    ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    days = []
    d = ist.date()
    step = 0
    while len(days) < n and step < n * 2 + 4:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
        step += 1
    return days


def scan_family(base, lists, date_str, wl, day):
    for suffix, label, kind, family in lists:
        url = base.format(date=date_str, suffix=suffix)
        data = fetch_pdf(url)
        if not data:
            continue
        day["lists_found"].append(label)
        if kind == "supp":
            day["has_supp"] = True
        if kind == "advance":
            day["has_advance"] = True
        if kind == "main":
            day["has_main"] = True
            day["main_families"].add(family)     # this list-type's main list is now out
        page_lines = pdf_to_lines(data)
        if not page_lines:
            continue
        day["matches"].extend(scan_text(page_lines, wl, label, kind, date_str, family))


def main():
    if len(sys.argv) > 1:
        dates = [sys.argv[1]]
    else:
        dates = upcoming_days(WINDOW_DAYS)

    wl = load_watchlist()
    print("Checking dates:", ", ".join(dates))
    print("Watch-list: {} names, {} AOR codes, {} parties, {} numbers".format(
        len(wl["advocate_names"]), len(wl["aor_codes"]), len(wl["parties"]),
        len(wl["case_numbers"]) + len(wl["diary_numbers"])))

    by_date = {}
    for date_str in dates:
        day = {"matches": [], "lists_found": [], "has_supp": False, "has_advance": False,
               "has_main": False, "main_families": set()}
        # Scan the DAILY (main + supplementary) lists FIRST so that on de-dup the authoritative
        # daily copy of a matter wins over its advance-list copy.
        scan_family(DAILY_BASE, DAILY_LISTS, date_str, wl, day)
        scan_family(ADVANCE_BASE, ADVANCE_LISTS, date_str, wl, day)
        # de-dup matches that appear in both advance and daily lists — keeps first (daily).
        # The key includes the case number (or a text prefix when none): Connected
        # sub-matters share the parent's item number but are DIFFERENT cases, and must
        # not be collapsed into one.
        seen, deduped = set(), []
        for m in day["matches"]:
            nm = re.search(r"\d{1,6}\s*[-/]\s*\d{4}", m.get("text", ""))
            k = (m["court"], m["item"], m["is_supplementary"],
                 nm.group(0).replace(" ", "") if nm else m.get("text", "")[:40])
            if k in seen:
                continue
            seen.add(k)
            deduped.append(m)
        day["matches"] = deduped
        # Once a list-type's MAIN daily list for a date is published, its advance list is
        # superseded: an advance-listed matter may not have materialised. Drop an advance match
        # only when ITS OWN family's main list is out — so, e.g., a Regular-list advance matter
        # isn't dropped just because the Miscellaneous main list happens to be published.
        mf = day["main_families"]
        day["matches"] = [m for m in day["matches"]
                          if not (m.get("kind") == "advance" and m.get("family") in mf)]
        if day["lists_found"] or day["matches"]:
            by_date[date_str] = day
            status = "[main list out — advance dropped]" if day["has_main"] else ("[advance only]" if day["has_advance"] else "")
            print("  {}: {} match(es) {}{}".format(
                date_str, len(day["matches"]), status,
                " [supp]" if day["has_supp"] else ""))

    all_matches = []
    for d in dates:
        if d in by_date:
            all_matches.extend(by_date[d]["matches"])

    result = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "window": dates,
        "by_date": {d: {
            "lists_found": by_date[d]["lists_found"],
            "has_supplementary": by_date[d]["has_supp"],
            "has_advance": by_date[d]["has_advance"],
            "has_main": by_date[d]["has_main"],
            "match_count": len(by_date[d]["matches"]),
            "matches": by_date[d]["matches"],
        } for d in by_date},
        "match_count": len(all_matches),
        "note": "Drafting aid only. The court's published lists are authoritative. "
                "Advance lists publish days ahead; main a few days ahead; supplementary "
                "the evening before (sometimes late) - refreshed several times a day.",
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("Wrote {} - {} match(es) across {} day(s).".format(
        OUTPUT_FILE, len(all_matches), len(by_date)))


if __name__ == "__main__":
    main()
