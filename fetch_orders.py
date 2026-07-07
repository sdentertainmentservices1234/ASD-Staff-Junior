#!/usr/bin/env python3
"""
Orders / judgments / case-status fetcher — ASD chamber app.

Design: the cause-list pipeline FEEDS this one. A matter that appeared in
court-updates.json on date D is polled for an order/judgment dated D (and
the next few days, since uploads lag). This keeps requests proportional to
your actual listings instead of hammering sci.gov.in for every matter daily.

Endpoint (as specified):
  https://www.sci.gov.in/view-pdf/?diary_no={NUM}&diary_year={YYYY}&type={o|j}&order_date={DATE}

HONEST CAVEAT: I cannot reach sci.gov.in from the build environment, so the
exact parameter spelling/date format is taken from your spec and best
knowledge. The script logs every attempted URL; if the first live run 404s
on documents you know exist, the fix is the two FORMAT constants below —
one look at the Actions log tells you which.

Output: orders-updates.json
  { schema, generated_at,
    matters: { "<diaryNo|caseNo>": {
        status: live|disposed, last_action, last_action_date, next_date,
        orders: [ {date, type, disposition, operative_line, court, item,
                   coram, author_judge, pdf_url, fetched_at} ] } } }

Reliability mirrors fetch_causelist.py: merge (never clobber), de-dup on
(matter, date, type), zero-parse PDFs recorded as anomalies with exit 1.
"""

import datetime as dt
import json
import os
import sys
import urllib.error

# reuse the http + pdf + matters plumbing from the cause-list fetcher
from fetch_causelist import (http_get, pdf_to_layout_text, merged_matters)
from order_parser import parse_court_document

ORDER_URL = ("https://www.sci.gov.in/view-pdf/?diary_no={num}"
             "&diary_year={year}&type={typ}&order_date={date}")
ORDER_DATE_FMT = "%d-%m-%Y"       # flip to "%Y-%m-%d" if live run 404s
POLL_DAYS_AFTER_LISTING = 4       # order upload can lag the hearing
COURT_UPDATES = "court-updates.json"
OUTPUT = "orders-updates.json"
SCHEMA_VERSION = 1


def matter_key(m):
    return (m.get("diaryNo") or m.get("caseNo") or m.get("parties", ""))[:80]


def split_diary(diary_no):
    """'37773-2026' -> ('37773', '2026'); tolerate '37773/2026'."""
    s = diary_no.replace("/", "-").strip()
    if "-" in s:
        num, year = s.rsplit("-", 1)
        return num.strip(), year.strip()
    return s, ""


def listing_dates_by_matter():
    """From court-updates.json: {matter_key: set(iso dates listed)}."""
    if not os.path.exists(COURT_UPDATES):
        return {}
    try:
        with open(COURT_UPDATES) as fh:
            cu = json.load(fh)
    except Exception:
        return {}
    out = {}
    for date_str, day in cu.get("days", {}).items():
        for m in day.get("matches", []):
            w = m.get("watch", {})
            k = matter_key(w)
            if k:
                out.setdefault(k, set()).add(date_str)
    return out


def candidate_dates(listed_dates, today=None):
    """Listing date .. +POLL_DAYS_AFTER_LISTING, capped at today."""
    today = today or dt.date.today()
    cands = set()
    for d in listed_dates:
        try:
            base = dt.date.fromisoformat(d)
        except ValueError:
            continue
        for i in range(POLL_DAYS_AFTER_LISTING + 1):
            c = base + dt.timedelta(days=i)
            if c <= today:
                cands.add(c)
    return sorted(cands)


def main():
    matters = merged_matters()
    if not matters:
        print("[fatal] no matters — aborting untouched.", file=sys.stderr)
        sys.exit(1)
    listings = listing_dates_by_matter()
    if not listings:
        print("[done] no recent listings in court-updates.json — nothing to poll.")
        sys.exit(0)

    existing = {"schema": SCHEMA_VERSION, "matters": {}}
    if os.path.exists(OUTPUT):
        try:
            with open(OUTPUT) as fh:
                existing = json.load(fh)
        except Exception:
            pass
    record = existing.get("matters", {})
    anomalies = []
    new_docs = 0

    by_key = {matter_key(m): m for m in matters}
    for key, dates in listings.items():
        m = by_key.get(key)
        if not m or not m.get("diaryNo"):
            continue           # orders endpoint is diary-number based
        num, year = split_diary(m["diaryNo"])
        if not num or not year:
            continue
        entry = record.setdefault(key, {"status": "live", "orders": []})
        have = {(o["date"], o["type"]) for o in entry["orders"]}

        for cand in candidate_dates(dates):
            date_param = cand.strftime(ORDER_DATE_FMT)
            for typ in ("o", "j"):
                if (cand.isoformat(), typ) in have:
                    continue
                url = ORDER_URL.format(num=num, year=year, typ=typ,
                                       date=date_param)
                try:
                    pdf = http_get(url)
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        print(f"[fetch] {url} HTTP {e.code}", file=sys.stderr)
                    continue
                except Exception as e:
                    print(f"[fetch] {url} failed: {e}", file=sys.stderr)
                    continue
                if not pdf or not pdf[:5].startswith(b"%PDF"):
                    continue   # HTML error page, not a document

                text = pdf_to_layout_text(pdf)
                doc = parse_court_document(text)
                if doc.doc_kind == "unknown" and len(pdf) > 8000:
                    anomalies.append({"matter": key, "url": url,
                                      "bytes": len(pdf)})
                    continue

                entry["orders"].append({
                    "date": doc.date or cand.isoformat(),
                    "type": "judgment" if doc.doc_kind == "judgment" else "order",
                    "disposition": doc.disposition,
                    "operative_line": doc.operative_line,
                    "court": doc.court, "item": doc.item,
                    "coram": doc.coram, "author_judge": doc.author_judge,
                    "case_number": doc.case_number,
                    "pdf_url": url,
                    "fetched_at": dt.datetime.utcnow().isoformat() + "Z",
                })
                have.add((doc.date or cand.isoformat(), typ))
                new_docs += 1
                print(f"[order] {key}: {doc.doc_kind} {doc.date} "
                      f"-> {doc.disposition}")

        # derive current status from the LATEST document
        if entry["orders"]:
            entry["orders"].sort(key=lambda o: o["date"])
            latest = entry["orders"][-1]
            entry["status"] = ("disposed" if latest["disposition"] in
                               {"allowed", "dismissed", "withdrawn", "disposed"}
                               else "live")
            entry["last_action"] = latest["disposition"]
            entry["last_action_date"] = latest["date"]
            nd = ""
            # next_date from the latest order that fixed one
            for o in reversed(entry["orders"]):
                if o.get("next_date"):
                    nd = o["next_date"]
                    break
            if nd:
                entry["next_date"] = nd

    out = {"schema": SCHEMA_VERSION,
           "generated_at": dt.datetime.utcnow().isoformat() + "Z",
           "matters": record,
           "anomalies": anomalies}
    with open(OUTPUT, "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"[done] wrote {OUTPUT}: {new_docs} new document(s), "
          f"{len(record)} matter(s) tracked")
    if anomalies:
        print(f"[warn] {len(anomalies)} parse anomalies — failing visibly.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
