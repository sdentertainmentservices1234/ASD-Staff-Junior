#!/usr/bin/env python3
"""
Supreme Court cause-list auto-fetcher — ASD chamber app.

Runs in GitHub Actions (6x/day multi-wave). For each upcoming date it tries
all list suffixes, parses whatever exists with the hardened parser
(causelist_parser.py — tuned on real MAIN + SUPPLEMENTARY samples), matches
against the chamber's live matters, and writes court-updates.json for the
app to read same-origin.

Reliability rules (deliberate):
  R1. NEVER overwrite good data with nothing. If every fetch fails, the
      existing court-updates.json is left untouched and we exit 0 (transient
      network/holiday is normal).
  R2. A fetched-but-zero-items PDF is a PARSE ANOMALY: we keep last-good for
      that date, record the anomaly, and exit 1 so the workflow shows red
      and you actually find out, instead of silently missing a listing.
  R3. Output is schema-versioned so the app can detect format drift.
  R4. Matters come from Firestore via bot login; watchlist.json is the
      fallback, and the two are merged (union) rather than either/or —
      a Firestore outage must not blind the fetcher to watchlist entries.

Env (GitHub secrets):
  FIREBASE_API_KEY, FIREBASE_PROJECT_ID, BOT_EMAIL, BOT_PASSWORD

System dependency: poppler-utils (pdftotext). Workflow step:
  - run: sudo apt-get update && sudo apt-get install -y poppler-utils
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error

from causelist_parser import parse, match_watchlist

# ----------------------------------------------------------------- config

BASE_URL = "https://api.sci.gov.in/jonew/cl/{date}/{suffix}.pdf"
SUFFIXES = ["M_J_1", "M_J_2", "F_J_1", "F_J_2", "M_R_1"]
LOOKAHEAD_DAYS = 3          # main lists publish days ahead
KEEP_DAYS_BACK = 2          # retain recent past days in output (for "yesterday" view)
OUTPUT = "court-updates.json"
WATCHLIST_FILE = "watchlist.json"
SCHEMA_VERSION = 2
TIMEOUT = 30
UA = {"User-Agent": "Mozilla/5.0 (compatible; ASD-chamber-fetcher/2.0)"}

# ------------------------------------------------------------- http utils


def http_get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_post_json(url, payload, timeout=TIMEOUT):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# -------------------------------------------------- matters (Firestore + file)


def firestore_matters():
    """Read live matters from chamber/main/matters via bot login.
    Returns [] on any failure — caller merges with watchlist.json (R4)."""
    try:
        api_key = os.environ["FIREBASE_API_KEY"]
        project = os.environ["FIREBASE_PROJECT_ID"]
        email = os.environ["BOT_EMAIL"]
        password = os.environ["BOT_PASSWORD"]
    except KeyError as e:
        print(f"[matters] secret missing: {e} — Firestore skipped", file=sys.stderr)
        return []
    try:
        auth = http_post_json(
            "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
            f"?key={api_key}",
            {"email": email, "password": password, "returnSecureToken": True})
        token = auth["idToken"]
    except Exception as e:
        print(f"[matters] bot login failed: {e}", file=sys.stderr)
        return []

    matters, page_token = [], None
    base = (f"https://firestore.googleapis.com/v1/projects/{project}"
            f"/databases/(default)/documents/chamber/main/matters?pageSize=300")
    try:
        while True:
            url = base + (f"&pageToken={page_token}" if page_token else "")
            req = urllib.request.Request(
                url, headers={**UA, "Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode())
            for doc in data.get("documents", []):
                f = doc.get("fields", {})
                sv = lambda k: f.get(k, {}).get("stringValue", "")
                entry = {"diaryNo": sv("diaryNo"), "caseNo": sv("caseNo"),
                         "parties": sv("parties"),
                         "id": doc["name"].rsplit("/", 1)[-1]}
                if entry["diaryNo"] or entry["caseNo"] or entry["parties"]:
                    matters.append(entry)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        print(f"[matters] Firestore read failed: {e}", file=sys.stderr)
    print(f"[matters] Firestore: {len(matters)} matters")
    return matters


def file_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        return []
    try:
        with open(WATCHLIST_FILE) as fh:
            wl = json.load(fh)
        print(f"[matters] watchlist.json: {len(wl)} entries")
        return wl if isinstance(wl, list) else []
    except Exception as e:
        print(f"[matters] watchlist.json unreadable: {e}", file=sys.stderr)
        return []


def merged_matters():
    """Union of Firestore + watchlist, de-duplicated on (diaryNo, caseNo)."""
    seen, out = set(), []
    for src in (firestore_matters(), file_watchlist()):
        for m in src:
            key = (m.get("diaryNo", ""), m.get("caseNo", ""),
                   m.get("parties", "")[:40])
            if key in seen:
                continue
            seen.add(key)
            out.append(m)
    return out


# ----------------------------------------------------------- pdf pipeline


def pdf_to_layout_text(pdf_bytes):
    """pdftotext -layout — the geometry the parser is tuned on."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_bytes)
        path = tf.name
    try:
        res = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, timeout=60)
        return res.stdout.decode("utf-8", errors="replace")
    finally:
        os.unlink(path)


def is_supplementary(text):
    return "SUPPLEMENTARY" in text[:3000].upper()


def dates_to_check(today=None):
    today = today or dt.date.today()
    return [today + dt.timedelta(days=d) for d in range(0, LOOKAHEAD_DAYS + 1)]


# ------------------------------------------------------------------ main


def main():
    matters = merged_matters()
    if not matters:
        print("[fatal] no matters from Firestore OR watchlist.json — "
              "matching would be meaningless; aborting without touching output.",
              file=sys.stderr)
        sys.exit(1)

    # last-good output (R1/R2)
    existing = {"schema": SCHEMA_VERSION, "days": {}}
    if os.path.exists(OUTPUT):
        try:
            with open(OUTPUT) as fh:
                existing = json.load(fh)
        except Exception:
            pass
    days = dict(existing.get("days", {}))

    anomalies = []
    fetched_anything = False

    for date in dates_to_check():
        date_str = date.isoformat()
        day_lists, day_matches = [], []
        for suffix in SUFFIXES:
            url = BASE_URL.format(date=date_str, suffix=suffix)
            try:
                pdf = http_get(url)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue          # list not published — normal
                print(f"[fetch] {url} HTTP {e.code}", file=sys.stderr)
                continue
            except Exception as e:
                print(f"[fetch] {url} failed: {e}", file=sys.stderr)
                continue

            fetched_anything = True
            text = pdf_to_layout_text(pdf)
            supp = is_supplementary(text)
            parsed = parse(text, is_supplementary=supp)
            print(f"[parse] {date_str}/{suffix}: {len(parsed)} items "
                  f"({'supp' if supp else 'main'})")

            if pdf and len(pdf) > 10_000 and len(parsed) == 0:
                # non-trivial PDF, zero items -> parser blind spot (R2)
                anomalies.append({"date": date_str, "suffix": suffix,
                                  "bytes": len(pdf), "items": 0})
                continue

            hits = match_watchlist(parsed, matters)
            for h in hits:
                h["list_suffix"] = suffix
                h["date"] = date_str
            day_lists.append({"suffix": suffix, "supplementary": supp,
                              "items_parsed": len(parsed),
                              "fetched_at": dt.datetime.utcnow().isoformat() + "Z"})
            day_matches.extend(hits)

        if day_lists:
            # replace this day's data only when we actually parsed lists (R1)
            days[date_str] = {"lists": day_lists, "matches": day_matches}

    # prune old days
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS_BACK)).isoformat()
    days = {d: v for d, v in days.items() if d >= cutoff}

    if not fetched_anything:
        print("[done] nothing fetched (holiday / not yet published) — "
              "output untouched.")
        sys.exit(0)

    out = {
        "schema": SCHEMA_VERSION,
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "matters_count": len(matters),
        "days": days,
        "anomalies": anomalies,
    }
    with open(OUTPUT, "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    total = sum(len(v["matches"]) for v in days.values())
    print(f"[done] wrote {OUTPUT}: {len(days)} day(s), {total} match(es)")

    if anomalies:
        print(f"[warn] {len(anomalies)} parse anomalies — failing the run "
              "so it is visible.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
