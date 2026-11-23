"""Tests for the supplier manual revision reference graph and link verification.

Covers the printed-number reuse trap, the split-chapter semantics, both
authorised old-code deliveries (archive vs. dual-theme index), the forbidden
silent single-target redirect, per-link RPA verification, per-language link
packages, and the immutable-ledger late-scan trail.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from preorderproof.manual import (
    LandingPageRPA,
    ManualGraph,
    ISSUE_CROSS_LANGUAGE,
    ISSUE_DEAD_ANCHOR,
    ISSUE_HTTP_ERROR,
    ISSUE_NEW_PACKAGING_NUMBER,
    ISSUE_REDIRECT_LOOP,
    ISSUE_SILENT_SINGLE,
    ISSUE_COVERAGE_GAP,
    ISSUE_WRONG_THEME_NAIVE,
    audit_body_links,
    body_link_naive_destination,
    graph_from_conn,
    graph_to_events,
    late_pages,
    link_package,
    old_entry_checklist,
    page_digest,
    resolve_number,
    route_journal,
    single_number_patch,
    verify_all,
    verify_route,
)
from preorderproof.store import connect, ingest, rows

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "manual-revision-graph.json"
BATTERY = {"theme-charging", "theme-storage"}


@pytest.fixture(scope="module")
def graph() -> ManualGraph:
    return ManualGraph.load(FIXTURE)


@pytest.fixture()
def rpa(graph) -> LandingPageRPA:
    return LandingPageRPA(graph)


@pytest.fixture()
def report(graph):
    return verify_all(graph)


def issue_codes(report) -> set[str]:
    return {i["code"] for i in report["issues"]}


def issues_for(report, code: str) -> list[dict]:
    return [i for i in report["issues"] if i["code"] == code]


# --------------------------------------------------------------------------
# Baseline behaviour preserved
# --------------------------------------------------------------------------

def test_baseline_resolve_number_still_hits_warranty():
    # The documented trap from test_manual_base.py, unchanged.
    sections = [
        {"language": "en", "number": "7", "title": "Warranty"},
        {"language": "en", "number": "8", "title": "Battery safety"},
    ]
    assert resolve_number(sections, "en", "7")["title"] == "Warranty"


def test_naive_number_7_is_warranty_in_english_and_cleaning_in_chinese(graph):
    en = graph.resolve_section("v2", "en", "7")
    zh = graph.resolve_section("v2", "zh", "7")
    assert en["theme_uid"] == "theme-warranty"
    assert zh["theme_uid"] == "theme-cleaning"


# --------------------------------------------------------------------------
# Split semantics: one old chapter -> two current themes per language
# --------------------------------------------------------------------------

def test_old_battery_chapter_covers_two_leaf_themes(graph):
    for section_uid in ("sec-v1-en-7", "sec-v1-zh-7"):
        assert graph.required_leaf_themes(section_uid) == BATTERY


def test_revision_targets_follow_signed_split_in_both_languages(graph):
    en_targets = {s["uid"] for s in graph.revision_targets("sec-v1-en-7")}
    zh_targets = {s["uid"] for s in graph.revision_targets("sec-v1-zh-7")}
    assert en_targets == {"sec-v2-en-8", "sec-v2-en-10"}
    assert zh_targets == {"sec-v2-zh-9", "sec-v2-zh-12"}
    assert {graph.sections[u]["theme_uid"] for u in en_targets} == BATTERY
    assert {graph.sections[u]["theme_uid"] for u in zh_targets} == BATTERY


def test_single_number_patch_keeps_charging_but_loses_storage(graph):
    patch = single_number_patch(graph, "m1:7", {"en": "8", "zh": "9"})
    for language in ("en", "zh"):
        row = patch[language]
        assert row["covered_theme_uids"] == ["theme-charging"]
        assert row["missing_theme_uids"] == ["theme-storage"]
        assert set(row["required_theme_uids"]) == BATTERY


def test_no_single_number_resolves_both_split_themes(graph):
    # Every individual v2 section carries at most one of the two leaf themes.
    for language in ("en", "zh"):
        for section in graph.sections_for("v2", language):
            assert len(graph.leaf_themes(section["theme_uid"]) & BATTERY) <= 1


# --------------------------------------------------------------------------
# Naive printed QR resolution lands on the wrong chapter
# --------------------------------------------------------------------------

def test_naive_m1_7_never_lands_on_battery_themes(graph, rpa):
    from preorderproof.manual import verify_qr_naive
    rows_ = verify_qr_naive(graph, rpa, "m1:7")
    by_lang = {f["language"]: f for f in rows_}
    assert by_lang["en"]["opened"]["theme_uid"] == "theme-warranty"
    assert by_lang["zh"]["opened"]["theme_uid"] == "theme-cleaning"
    for finding in rows_:
        assert finding["opened"]["theme_uid"] not in BATTERY


def test_wrong_theme_issue_reports_concrete_source_page(report):
    wrong = issues_for(report, ISSUE_WRONG_THEME_NAIVE)
    qr_issues = [i for i in wrong if i["surface"] == "qr:m1:7"]
    assert {(i["language"], i["actual_theme_uid"]) for i in qr_issues} == {
        ("en", "theme-warranty"), ("zh", "theme-cleaning")}
    for issue in qr_issues:
        assert issue["source_page_id"].startswith("pdf-v1-")
        assert set(issue["required_theme_uids"]) == BATTERY


# --------------------------------------------------------------------------
# Both authorised routes make both themes reachable
# --------------------------------------------------------------------------

@pytest.mark.parametrize("language,first_page,entry_numbers", [
    ("en", "pdf-v1-en-p12", ("8", "10")),
    ("zh", "pdf-v1-zh-p12", ("9", "12")),
])
def test_archive_route_first_lands_on_v1_and_reaches_both_new_themes(
        graph, rpa, language, first_page, entry_numbers):
    route = next(r for r in graph.routes
                 if r["strategy"] == "archive" and r["language"] == language
                 and r["status"] == "active")
    result = verify_route(graph, rpa, route)
    assert result["ok"]
    assert result["first_page_id"] == first_page
    assert result["first_theme_uid"] == "theme-battery-safety"
    assert set(result["reachable_theme_uids"]) >= BATTERY
    anchors = [e["opened"]["anchor"].removeprefix("sec-") for e in result["entry_results"]]
    assert sorted(anchors) == sorted(entry_numbers)


@pytest.mark.parametrize("language,index_page", [
    ("en", "web-v2-en-index"), ("zh", "web-v2-zh-index")])
def test_index_route_first_lands_on_dual_theme_index(graph, rpa, language, index_page):
    route = next(r for r in graph.routes
                 if r["strategy"] == "index" and r["language"] == language
                 and r["status"] == "active")
    result = verify_route(graph, rpa, route)
    assert result["ok"]
    assert result["first_page_id"] == index_page
    # Index first landing has no single theme; both are one click away.
    assert result["first_theme_uid"] is None
    assert set(result["reachable_theme_uids"]) == BATTERY


def test_archive_and_index_first_landings_are_distinct_and_measurable(graph, rpa):
    for language in ("en", "zh"):
        archive = next(r for r in graph.routes
                       if r["strategy"] == "archive" and r["language"] == language)
        index = next(r for r in graph.routes
                     if r["strategy"] == "index" and r["language"] == language)
        a = verify_route(graph, rpa, archive)
        b = verify_route(graph, rpa, index)
        assert a["first_page_id"] != b["first_page_id"]
        assert a["first_theme_uid"] == "theme-battery-safety"
        assert b["first_theme_uid"] is None
        # Both still close the same two-theme semantics.
        assert set(a["reachable_theme_uids"]) >= BATTERY
        assert set(b["reachable_theme_uids"]) == BATTERY


def test_old_entry_checklist_distinguishes_archive_index_and_forbidden(graph):
    checklist = old_entry_checklist(graph)
    m1 = [c for c in checklist if c["qr_code"] == "m1:7"]
    kinds = {(c["language"], c["strategy"], c["first_landing_kind"], c["forbidden"]) for c in m1}
    assert ("en", "archive", "archived_v1_section", False) in kinds
    assert ("en", "index", "dual_theme_index", False) in kinds
    assert ("en", "forbidden_silent_single", "silent_single_target", True) in kinds
    for item in m1:
        assert set(item["required_theme_uids"]) == BATTERY


# --------------------------------------------------------------------------
# Forbidden silent single-target redirect
# --------------------------------------------------------------------------

def test_silent_single_target_route_is_rejected(graph, rpa):
    route = next(r for r in graph.routes if r["strategy"] == "forbidden_silent_single")
    result = verify_route(graph, rpa, route)
    assert not result["ok"]
    codes = {i["code"] for i in result["issues"]}
    assert ISSUE_SILENT_SINGLE in codes
    assert ISSUE_COVERAGE_GAP in codes
    gap = next(i for i in result["issues"] if i["code"] == ISSUE_COVERAGE_GAP)
    assert gap["missing_theme_uids"] == ["theme-storage"]


def test_authorized_routes_have_no_silent_single_or_coverage_issues(report):
    for result in report["route_reports"]:
        if result["strategy"] in {"archive", "index"}:
            assert result["ok"]
            assert not {ISSUE_SILENT_SINGLE, ISSUE_COVERAGE_GAP} & {i["code"] for i in result["issues"]}


# --------------------------------------------------------------------------
# In-body Setup links must be corrected through the same mapping
# --------------------------------------------------------------------------

@pytest.mark.parametrize("link_id,wrong_theme", [
    ("bl-v1-en-setup-battery", "theme-warranty"),
    ("bl-v1-zh-setup-battery", "theme-cleaning"),
])
def test_setup_body_link_naively_misdirects_under_new_numbering(graph, link_id, wrong_theme):
    link = next(l for l in graph.body_links if l["link_id"] == link_id)
    naive = body_link_naive_destination(graph, link)
    assert naive["theme_uid"] == wrong_theme
    assert wrong_theme not in BATTERY


def test_setup_body_link_correction_reaches_both_themes(graph, rpa):
    from preorderproof.manual import corrected_body_link
    for link_id in ("bl-v1-en-setup-battery", "bl-v1-zh-setup-battery"):
        link = next(l for l in graph.body_links if l["link_id"] == link_id)
        for strategy in ("archive", "index"):
            corrected = corrected_body_link(graph, rpa, link, strategy)
            assert set(corrected["reachable_theme_uids"]) >= BATTERY
            assert corrected["route_id"] is not None


def test_body_link_destinations_are_listed_per_source_page(report):
    destinations = {d["link_id"]: d for d in report["destinations"]["body_links"]}
    en = destinations["bl-v1-en-setup-battery"]
    assert en["source_page_id"] == "pdf-v1-en-p05"
    assert "archive/v1/en" in en["archive_first_url"]
    assert "battery-index" in en["index_first_url"]
    assert set(en["archive_reachable"]) >= BATTERY
    assert set(en["index_reachable"]) == BATTERY


# --------------------------------------------------------------------------
# RPA defect classes, each pinned to a concrete source page
# --------------------------------------------------------------------------

def test_rpa_reports_redirect_loop_with_source_page(graph, report):
    loops = issues_for(report, ISSUE_REDIRECT_LOOP)
    assert any(i["source_page_id"] == "web-v2-en-p13" for i in loops)
    finding = next(f for f in audit_body_links(graph, LandingPageRPA(graph))
                   if f["link_id"] == "bl-v2-en-loop")
    assert not finding["ok"]
    assert {i["code"] for i in finding["issues"]} == {ISSUE_REDIRECT_LOOP}


def test_rpa_reports_dead_anchor(graph, report):
    dead = issues_for(report, ISSUE_DEAD_ANCHOR)
    target = next(i for i in dead if i["source_page_id"] == "web-v2-en-p11")
    assert target["url"].endswith("#sec-99")


def test_rpa_reports_cross_language_mix(graph, report):
    mixed = issues_for(report, ISSUE_CROSS_LANGUAGE)
    target = next(i for i in mixed if i["source_page_id"] == "web-v2-en-p11")
    assert target["actual_language"] == "zh"
    assert target["expected_language"] == "en"


def test_rpa_reports_http_error_not_just_200_assumption(graph, report):
    errors = issues_for(report, ISSUE_HTTP_ERROR)
    target = next(i for i in errors if i["source_page_id"] == "web-v2-zh-p15")
    assert target["url"].endswith("withdrawn.html")
    opened = LandingPageRPA(graph).open(target["url"], "zh")
    assert opened["issues"][0]["http_status"] == 410


def test_http_200_alone_is_not_acceptance(graph, rpa):
    # A reachable URL can still carry a dead anchor or cross-language landing.
    dead_anchor = rpa.open("https://manual.example/v2/en/charging.html#sec-99", "en")
    cross = rpa.open("https://manual.example/v2/zh/storage.html#sec-12", "en")
    assert dead_anchor["chain"][0]["http_status"] == 200 and not dead_anchor["ok"]
    assert cross["chain"][0]["http_status"] == 200 and not cross["ok"]


# --------------------------------------------------------------------------
# Per-language current-edition link packages
# --------------------------------------------------------------------------

def test_link_packages_use_current_edition_and_real_anchors(graph, rpa):
    for language, numbers in (("en", {"7", "8", "10", "14"}),
                              ("zh", {"7", "9", "12"})):
        package = link_package(graph, language)
        assert package["edition"] == "v2"
        assert {item["number"] for item in package["links"]} == numbers
        for item in package["links"]:
            opened = rpa.open(item["url"], language)
            assert opened["ok"]
            assert opened["theme_uid"] == item["theme_uid"]


def test_new_packaging_must_print_theme_uid_links(graph, report):
    bad = issues_for(report, ISSUE_NEW_PACKAGING_NUMBER)
    assert len(bad) == 1
    assert bad[0]["source_page_id"] == "web-v2-en-p13"
    good = [q for q in graph.qrcodes if q["edition"] == "v2"
            and q["qr_code"] != "m2:bad-number"]
    assert all(q.get("printed_ref", "").startswith("theme:") for q in good)


# --------------------------------------------------------------------------
# Traversable reference graph
# --------------------------------------------------------------------------

def test_reference_graph_is_traversable_across_revisions(graph):
    ref = graph.reference_graph()
    edge_types = {e["type"] for e in ref["edges"]}
    assert {"revision_split", "revision_reuse", "revision_merge",
            "printed_qr", "body_link", "page_anchor", "section_theme"} <= edge_types
    split_edges = [e for e in ref["edges"] if e["type"] == "revision_split"]
    pairs = {(e["from"], e["to"]) for e in split_edges}
    assert ("sec-v1-en-7", "sec-v2-en-8") in pairs
    assert ("sec-v1-en-7", "sec-v2-en-10") in pairs
    assert ("sec-v1-zh-7", "sec-v2-zh-9") in pairs
    assert ("sec-v1-zh-7", "sec-v2-zh-12") in pairs


# --------------------------------------------------------------------------
# Immutable ledger: ingest, rebuild, idempotency, late scans, isolation
# --------------------------------------------------------------------------

@pytest.fixture()
def conn(graph):
    connection = connect(":memory:")
    ingest(connection, "manual", graph_to_events(graph, captured_at="2026-10-05"))
    return connection


def test_manual_events_ingest_and_rebuild_round_trip(graph, conn):
    rebuilt = graph_from_conn(conn)
    assert len(rebuilt["pages"]) == len(graph.pages)
    assert {q["qr_code"] for q in rebuilt["qrcodes"]} == {q["qr_code"] for q in graph.qrcodes}
    assert len(rebuilt["body_links"]) == len(graph.body_links)
    assert len(rebuilt["routes"]) == len(graph.routes)
    # The signed split must survive with both target theme UIDs.
    split = next(r for r in rebuilt["revisions"]
                 if r["revision_id"] == "rev-2" and r["relation"] == "split")
    assert set(split["to_theme_uids"]) == {"theme-charging", "theme-storage"}
    assert split["from_section_uids"] == ["sec-v1-en-7"]
    full = {"product": graph.data["product"], "themes": graph.data["themes"],
            "sections": graph.data["sections"], **rebuilt}
    rebuilt_report = verify_all(ManualGraph(full))
    assert issue_codes(rebuilt_report) == issue_codes(verify_all(graph))


def test_manual_ingest_is_idempotent(graph, conn):
    first = ingest(conn, "manual", graph_to_events(graph))
    assert first["inserted"] == 0 and first["replayed"] == len(graph_to_events(graph))


def test_late_scan_is_kept_and_old_route_trail_is_preserved(graph, conn):
    late_page = {
        "event_id": "manual-page-late-v1-en-p21", "event_at": "2026-11-20",
        "kind": "manual_page", "page_id": "pdf-v1-en-p21", "source": "late_scan",
        "language": "en", "edition": "v1",
        "url": "https://manual.example/archive/v1/en/extra.html",
        "http_status": "200", "digest": "", "redirects_to": ""}
    ingest(conn, "manual", [late_page])
    arrived = late_pages(conn, "2026-10-05")
    assert [p["page_id"] for p in arrived] == ["pdf-v1-en-p21"]

    # The already-delivered m1:7 routes and their first landings remain intact.
    journal = route_journal(conn, "m1:7")
    assert len(journal) == 5
    assert all(r["first_url"] for r in journal)
    archive = next(r for r in journal if r["strategy"] == "archive" and r["language"] == "en")
    assert archive["status"] == "active"
    assert "archive/v1/en" in archive["first_url"]


def test_late_superseding_route_keeps_the_original_visible(graph, conn):
    new_route = {
        "event_id": "manual-route-rt-c-en", "event_at": "2026-11-21",
        "kind": "manual_route", "qr_code": "m1:7", "strategy": "index",
        "language": "en",
        "first_url": "https://manual.example/v2/en/battery-index.html",
        "status": "active", "supersedes": "manual-route-rt-b-en"}
    ingest(conn, "manual", [new_route])
    journal = route_journal(conn, "m1:7")
    assert [r["event_id"] for r in journal].count("manual-route-rt-b-en") == 1
    newest = [r for r in journal if r["event_id"] == "manual-route-rt-c-en"]
    assert newest and newest[0]["supersedes"] == "manual-route-rt-b-en"


def test_manual_events_never_touch_business_ledgers(graph, conn):
    # No offer/payment/delivery projections may appear from manual evidence.
    for table in ("offers", "batches", "orders", "payments", "parcels", "receipts"):
        assert rows(conn, table) == []
    # offer_id is recorded only as packaging/batch association metadata.
    qr_rows = rows(conn, "manual_qrcodes")
    assert {r["offer_id"] for r in qr_rows if r["qr_code"] == "m1:7"} == {"O1"}


def test_validator_rejects_unknown_manual_relation(conn):
    bad = {"event_id": "manual-revision-x", "event_at": "2026-10-05",
           "kind": "manual_revision", "revision_id": "rev-x", "product": "p1",
           "edition": "v2", "language": "en", "relation": "explode",
           "from_theme_uid": "theme-setup", "to_theme_uid": "theme-setup",
           "to_theme_uids": '["theme-setup"]',
           "section_uid": "sec-v1-en-3", "note": "", "signed_by": "supplier-doc-lead",
           "digest": ""}
    with pytest.raises(ValueError, match="relation"):
        ingest(conn, "manual", [bad])


def test_page_digest_detects_tampering(graph):
    page = dict(graph.pages["web-v2-en-p11"])
    original = page_digest(page)
    page["anchors"] = [dict(a, theme_uid="theme-warranty") for a in page["anchors"]]
    assert page_digest(page) != original
