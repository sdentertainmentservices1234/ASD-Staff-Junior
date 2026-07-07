"""
Golden-file regression tests — ASD cause-list & orders pipeline.

Run by the GitHub Actions workflow BEFORE every fetch. If any parser change
breaks main-list, order, or judgment parsing, the run fails and nothing
ships. Fixtures are real SC documents committed under fixtures/.

Requires poppler-utils (pdftotext) — installed by the workflow.
"""

import subprocess
import pathlib
import pytest

from causelist_parser import parse, match_watchlist
from order_parser import parse_court_document

FIX = pathlib.Path(__file__).parent / "fixtures"


def layout(pdf_name):
    res = subprocess.run(
        ["pdftotext", "-layout", str(FIX / pdf_name), "-"],
        capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr.decode()[:300]
    return res.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------- main list (M_J_1)

@pytest.fixture(scope="module")
def main_list():
    return parse(layout("M_J_1_2026-07-08.pdf"))


def test_main_list_item_count(main_list):
    assert len(main_list) == 77


def test_all_courts_have_coram(main_list):
    by_court = {}
    for m in main_list:
        by_court.setdefault(m.court, m.coram)
    assert set(by_court) == {"11", "12", "13", "14", "15"}
    for court, coram in by_court.items():
        assert coram, f"court {court} lost its coram"
    assert by_court["15"] == "R. MAHADEVAN; MANMOHAN"


def test_connected_item_linked(main_list):
    conn = [m for m in main_list if m.connected_to]
    assert any(m.item == "11.1" and m.connected_to == "11"
               and m.case_number == "T.P.(C) 175/2026" for m in conn)


def test_case_number_not_truncated(main_list):
    tp = next(m for m in main_list if m.item == "11" and m.court == "11")
    assert tp.case_number == "T.P.(C) 3594/2025"          # full year
    ioc = next(m for m in main_list if m.court == "15" and m.item == "5")
    assert ioc.case_number == "SLP(C) 22536/2026"
    assert ioc.petitioner == "INDIAN OIL CORPORATION"      # no column bleed


def test_sections_anchored_no_bracket_pollution(main_list):
    secs = {m.section for m in main_list}
    assert "BAIL MATTERS" in secs
    assert not any(s.startswith("R-") or s == "CAVEAT" for s in secs)


def test_common_token_false_positive_guard(main_list):
    # "industries" alone must NOT match (the Ballarpur lesson)
    hits = match_watchlist(main_list, [
        {"parties": "Some Other Industries Limited"}])
    assert hits == []


def test_true_matches_survive(main_list):
    wl = [{"diaryNo": "38158-2026", "parties": "Ekansh Dhingra"},
          {"caseNo": "SLP(C) 22536/2026",
           "parties": "Indian Oil Corporation vs Ravidas"}]
    hits = match_watchlist(main_list, wl)
    assert len(hits) == 2
    assert {h["court"] for h in hits} == {"11", "15"}


# ------------------------------------------------------ order & judgment

def test_order_kavale():
    doc = parse_court_document(layout("Order_37773_03-Jul-2026.pdf"))
    assert doc.doc_kind == "order"
    assert doc.case_number == "SLP(C) 22381/2026"
    assert (doc.court, doc.item) == ("3", "31")
    assert doc.date == "2026-07-03"
    assert doc.coram == "K.V. VISWANATHAN; SHREE CHANDRASHEKHAR"
    assert doc.disposition == "dismissed"
    assert doc.status == "disposed"
    assert "Signature Not Verified" not in doc.operative_line


def test_judgment_padmanabhan():
    doc = parse_court_document(layout("Judgement_6150_04-Jun-2026.pdf"))
    assert doc.doc_kind == "judgment"
    assert doc.case_number == "C.A. 10724/2016"
    assert doc.petitioner == "T.K.A. PADMANABHAN"
    assert "ABHIYAN COOPERATIVE" in doc.respondent
    assert doc.author_judge == "VIKRAM NATH"
    assert doc.date == "2026-06-04"
    assert doc.disposition == "allowed"
    assert doc.status == "disposed"
