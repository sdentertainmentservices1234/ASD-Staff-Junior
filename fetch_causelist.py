#!/usr/bin/env python3
"""
Supreme Court of India - cause-list & supplementary fetcher (multi-wave).

The court publishes in two waves:
  * MAIN lists (miscellaneous + regular) days ahead - e.g. Monday's main on the
    preceding Thursday evening, Tuesday's on Friday evening.
  * SUPPLEMENTARY lists the evening before the hearing day (Monday's on Saturday
    evening; Tuesday's on Monday by ~8-9 PM, sometimes later).

So a complete picture assembles over several days. This script runs SEVERAL
TIMES A DAY (see the workflow) and, on every run, checks a ROLLING WINDOW of
upcoming hearing days. Each matched matter is enriched with court no., item no.,
coram (bench), the court's total & fresh counts, and a MAIN vs SUPPLEMENTARY
flag.

Drafting aid only - the court's published list is authoritative.
Free: pure fetch + PDF text, no API keys, no paid services.
"""

import io
import json
import re
import sys
import datetime
import urllib.request

BASE = "https://api.sci.gov.in/jonew/cl/{date}/{suffix}.pdf"

# suffix -> (human label, kind) where kind is "main" or "supp"
CAUSE_LISTS = [
    ("M_J_1", "Miscellaneous - Main",           "main"),
    ("M_J_2", "Miscellaneous - Supplementary",  "supp"),
    ("F_J_1", "Regular / Final - Main",          "main"),
    ("F_J_2", "Regular / Final - Supplementary", "supp"),
    ("M_R_1", "Registrar - Main",                "main"),
]

WINDOW_DAYS = 8
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
    for k in ("advocate_names", "parties", "case_numbers", "diary_numbers"):
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


def norm(s):
    s = (s or "").lower().replace(".", " ").replace(",", " ")
    return re.sub(r"\s+", " ", s).strip()


def norm_num(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


CORAM_RE = re.compile(r"hon'?ble.*", re.I)
COURT_RE = re.compile(r"court\s*no\.?\s*([0-9]+)", re.I)
TOTAL_RE = re.compile(r"total\s*(?:matters)?\s*[:\-]?\s*([0-9]+)", re.I)
FRESH_RE = re.compile(r"fresh\s*(?:matters)?\s*[:\-]?\s*([0-9]+)", re.I)


def scan_text(text, wl, list_label, list_kind, for_date):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    name_terms = [norm(x) for x in wl["advocate_names"] if x.strip()]
    party_terms = [norm(x) for x in wl["parties"] if x.strip()]
    num_terms = [norm_num(x) for x in (wl["case_numbers"] + wl["diary_numbers"]) if x.strip()]

    grouped, order = {}, []
    cur_court = cur_coram = cur_total = cur_fresh = cur_item = ""
    cur_key = None

    for line in lines:
        ln = norm(line)
        lnum = norm_num(line)

        cm = COURT_RE.search(line)
        if cm:
            cur_court = cm.group(1)
            cur_coram = ""
            cur_total = cur_fresh = ""
        if not cur_coram:
            cmatch = re.search(r"(hon'?ble.*)", line, re.I)
            if cmatch:
                cur_coram = re.sub(r"\s+", " ", cmatch.group(1)).strip()[:120]
        tm = TOTAL_RE.search(line)
        if tm:
            cur_total = tm.group(1)
        fm = FRESH_RE.search(line)
        if fm:
            cur_fresh = fm.group(1)

        im = re.match(r"^\s*([0-9]{1,4})\b", line)
        if im:
            cur_item = im.group(1)
            cur_key = (list_kind, cur_court, cur_item)

        hits = []
        for t in name_terms:
            if t and t in ln:
                hits.append("advocate")
        for t in party_terms:
            if t and t in ln:
                hits.append("party")
        for t in num_terms:
            if t and len(t) >= 5 and t in lnum:
                hits.append("number")

        if hits:
            key = cur_key or (list_kind, cur_court, cur_item or line[:20])
            if key not in grouped:
                grouped[key] = {
                    "for_date": for_date,
                    "list": list_label,
                    "kind": list_kind,
                    "is_supplementary": list_kind == "supp",
                    "court": cur_court,
                    "item": cur_item,
                    "coram": cur_coram,
                    "court_total": cur_total,
                    "court_fresh": cur_fresh,
                    "matched_on": set(),
                    "text": "",
                }
                order.append(key)
            g = grouped[key]
            g["matched_on"].update(hits)
            if cur_coram and not g["coram"]:
                g["coram"] = cur_coram
            if cur_total and not g["court_total"]:
                g["court_total"] = cur_total
            if cur_fresh and not g["court_fresh"]:
                g["court_fresh"] = cur_fresh
            if re.match(r"^\s*[0-9]{1,4}\b", line) and not re.match(r"^\s*[0-9]{1,4}\b", g["text"]):
                g["text"] = line[:300]
            elif not g["text"]:
                g["text"] = line[:300]

    out = []
    for key in order:
        g = grouped[key]
        g["matched_on"] = sorted(g["matched_on"])
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


def main():
    if len(sys.argv) > 1:
        dates = [sys.argv[1]]
    else:
        dates = upcoming_days(WINDOW_DAYS)

    wl = load_watchlist()
    print("Checking dates:", ", ".join(dates))
    print("Watch-list: {} names, {} parties, {} numbers".format(
        len(wl["advocate_names"]), len(wl["parties"]),
        len(wl["case_numbers"]) + len(wl["diary_numbers"])))

    by_date = {}
    for date_str in dates:
        day = {"matches": [], "lists_found": [], "has_supp": False}
        for suffix, label, kind in CAUSE_LISTS:
            url = BASE.format(date=date_str, suffix=suffix)
            data = fetch_pdf(url)
            if not data:
                continue
            day["lists_found"].append(label)
            if kind == "supp":
                day["has_supp"] = True
            text = pdf_to_text(data)
            if not text.strip():
                continue
            day["matches"].extend(scan_text(text, wl, label, kind, date_str))
        if day["lists_found"] or day["matches"]:
            by_date[date_str] = day
            print("  {}: {} match(es){}".format(
                date_str, len(day["matches"]),
                " [supp published]" if day["has_supp"] else ""))

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
            "match_count": len(by_date[d]["matches"]),
            "matches": by_date[d]["matches"],
        } for d in by_date},
        "match_count": len(all_matches),
        "note": "Drafting aid only. The court's published lists are authoritative. "
                "Main lists publish days ahead; supplementary lists the evening before "
                "(sometimes late) - this file is refreshed several times a day.",
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("Wrote {} - {} match(es) across {} day(s).".format(
        OUTPUT_FILE, len(all_matches), len(by_date)))


if __name__ == "__main__":
    main()
