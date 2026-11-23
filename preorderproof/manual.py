"""Supplier manual revision reference graph and clickable-link verification.

The printed-packaging QR codes were minted against manual edition v1. The
supplier later split one chapter into two and reused the old printed numbers
for unrelated chapters, with different numbering in the English and Chinese
editions. A lookup keyed only on ``(language, printed number)`` therefore
resolves the old code onto the wrong chapter (see :func:`resolve_number`).

This module models the manual as a traversable reference graph of stable
theme UIDs, edition/language sections, printed pages and anchors, in-body
hyperlinks, printed QR codes, signed split/merge revisions and the two
authorised old-code deliveries (archive first vs. dual-theme index first).
A small RPA opens every link through the captured page registry and verifies
the *theme UID set* reached, not merely HTTP 200.

The module is deliberately standalone: the SQLite ledger integration
(:func:`graph_to_events`, :func:`graph_from_conn`) imports the store lazily,
so the graph and verifier work from the JSON fixture on their own.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag

MAX_HOPS = 10

# Verification issue codes. Every issue carries the concrete source page the
# link was captured on, so failures are traceable to a scanned PDF page.
ISSUE_REDIRECT_LOOP = "redirect_loop"
ISSUE_HTTP_ERROR = "http_error"
ISSUE_DEAD_ANCHOR = "dead_anchor"
ISSUE_CROSS_LANGUAGE = "cross_language_mix"
ISSUE_WRONG_THEME_NAIVE = "wrong_theme_naive_number"
ISSUE_COVERAGE_GAP = "coverage_gap"
ISSUE_SILENT_SINGLE = "forbidden_silent_single_target_redirect"
ISSUE_NEW_PACKAGING_NUMBER = "new_packaging_bare_number"
ISSUE_DIGEST_MISMATCH = "page_digest_mismatch"
ISSUE_ANCHOR_THEME_MISMATCH = "anchor_theme_mismatch"
ISSUE_UNRESOLVED = "unresolved_reference"


def resolve_number(sections, language, number):
    """Baseline lookup: printed number inside one language's section set.

    Retained unchanged as the documented trap. On the English v2 section set
    the old printed number ``7`` returns the Warranty section, not the
    battery-safety content the packaging QR was minted for.
    """
    return next((row for row in sections
                 if row["language"] == language and row["number"] == number), None)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def split_url(url: str) -> tuple[str, str]:
    base, anchor = urldefrag(url)
    return base, (anchor or "")


def page_digest(page: dict) -> str:
    """Content digest of a captured page over its ledger-persisted fields.

    Covers page identity, language/edition, the OCR'd anchor-to-theme mapping
    and the redirect target. Outbound hyperlinks are separate auditable edge
    rows (``manual_body_links``), so they are intentionally not part of the
    page's own hash.
    """
    payload = {
        "page_id": page["page_id"],
        "url": page["url"],
        "language": page["language"],
        "edition": page["edition"],
        "anchors": sorted((a["anchor"], a["theme_uid"]) for a in page.get("anchors", [])),
        "redirects_to": page.get("redirects_to", ""),
    }
    return digest(payload)


class ManualGraph:
    """Traversable reference graph built from the supplier revision fixture."""

    def __init__(self, data: dict):
        self.data = data
        self.product = data["product"]
        self.current_edition = data["product"]["current_edition"]
        self.themes = {t["uid"]: t for t in data["themes"]}
        self.sections = {s["uid"]: s for s in data["sections"]}
        self.revisions = list(data["revisions"])
        self.qrcodes = list(data["qrcodes"])
        self.body_links = list(data["body_links"])
        self.routes = list(data["routes"])
        self.pages = {p["page_id"]: p for p in data["pages"]}
        self._page_by_url = {split_url(p["url"])[0]: p for p in self.pages.values()}
        self._validate()

    @classmethod
    def load(cls, source) -> "ManualGraph":
        if isinstance(source, (str, Path)):
            source = json.loads(Path(source).read_text(encoding="utf-8"))
        return cls(source)

    def _validate(self) -> None:
        for section in self.sections.values():
            if section["theme_uid"] not in self.themes:
                raise ValueError(f"section {section['uid']} references unknown theme")
        for page in self.pages.values():
            for anchor in page.get("anchors", []):
                if anchor["theme_uid"] not in self.themes:
                    raise ValueError(f"page {page['page_id']} anchor references unknown theme")
            for link in page.get("links", []):
                self._require_known_url(link["url"], page["page_id"])
        for qr in self.qrcodes:
            if qr["section_uid"] not in self.sections:
                raise ValueError(f"QR {qr['qr_code']} references unknown section")
            if qr["source_page_id"] not in self.pages:
                raise ValueError(f"QR {qr['qr_code']} references unknown source page")
        for link in self.body_links:
            if link["source_page_id"] not in self.pages:
                raise ValueError(f"body link {link['link_id']} references unknown source page")
            self._require_known_url(link["url"], link["link_id"])
        for route in self.routes:
            self._require_known_url(route["first_url"], route["route_id"])
            for entry in route.get("entries", []):
                self._require_known_url(entry["url"], route["route_id"])
                if entry["theme_uid"] not in self.themes:
                    raise ValueError(f"route {route['route_id']} references unknown theme")

    def _require_known_url(self, url: str, where: str) -> None:
        # The base page must have been captured. A missing anchor is a defect
        # class the RPA is built to report (ISSUE_DEAD_ANCHOR), so it is not a
        # construction error.
        base, _anchor = split_url(url)
        if self._page_by_url.get(base) is None:
            raise ValueError(f"{where} points at uncaptured page {url}")

    # -- traversal primitives -------------------------------------------------

    def section(self, uid: str) -> dict | None:
        return self.sections.get(uid)

    def sections_for(self, edition: str, language: str) -> list[dict]:
        return sorted((s for s in self.sections.values()
                       if s["edition"] == edition and s["language"] == language),
                      key=lambda s: int(s["number"]))

    def resolve_section(self, edition: str, language: str, number: str) -> dict | None:
        return resolve_number(self.sections_for(edition, language), language, number)

    def page_for_url(self, url: str) -> dict | None:
        return self._page_by_url.get(split_url(url)[0])

    def anchor_theme(self, url: str) -> str | None:
        base, anchor = split_url(url)
        page = self._page_by_url.get(base)
        if page is None or not anchor:
            return None
        found = next((a for a in page.get("anchors", []) if a["anchor"] == anchor), None)
        return found["theme_uid"] if found else None

    def section_url(self, section_uid: str, page_id: str | None = None) -> str:
        section = self.sections[section_uid]
        page_id = page_id or self._page_id_for_section(section_uid)
        page = self.pages[page_id]
        anchor = next(a["anchor"] for a in page["anchors"]
                      if a["theme_uid"] == section["theme_uid"]
                      and _anchor_number(a["anchor"]) == section["number"])
        return f"{split_url(page['url'])[0]}#{anchor}"

    def _page_id_for_section(self, section_uid: str) -> str:
        section = self.sections[section_uid]
        page = next(p for p in self.pages.values()
                    if p["edition"] == section["edition"]
                    and p["language"] == section["language"]
                    and any(a["theme_uid"] == section["theme_uid"]
                            and _anchor_number(a["anchor"]) == section["number"]
                            for a in p.get("anchors", [])))
        return page["page_id"]

    def leaf_themes(self, theme_uid: str) -> set[str]:
        """Stable leaf themes underneath a (possibly composite) theme UID."""
        children = {t["uid"] for t in self.themes.values() if t.get("parent") == theme_uid}
        if not children:
            return {theme_uid}
        leaves: set[str] = set()
        for child in children:
            leaves |= self.leaf_themes(child)
        return leaves

    def required_leaf_themes(self, section_uid: str) -> set[str]:
        return self.leaf_themes(self.sections[section_uid]["theme_uid"])

    # A split/merge preserves the chapter's content; a reuse/rename does not
    # (reuse is precisely the printed-number hijack), so only content-bearing
    # relations are followed when resolving where an old chapter's content went.
    CONTENT_RELATIONS = {"split", "merge"}

    def revision_rows(self, section_uid: str) -> list[dict]:
        rows = [r for r in self.revisions
                if r["relation"] in self.CONTENT_RELATIONS
                and (section_uid == r.get("from_section_uid")
                     or section_uid in r.get("from_section_uids", []))]
        return sorted(rows, key=lambda r: r["revision_id"])

    def revision_targets(self, section_uid: str) -> list[dict]:
        """Follow signed split / merge / reuse revisions to current sections.

        Returns one row per reachable current section. A split yields several
        targets, which is why a single replacement number can never close the
        semantics of an old composite chapter.
        """
        seen: set[str] = set()
        targets: list[dict] = []
        queue = deque([section_uid])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            rows = self.revision_rows(current)
            if not rows:
                targets.append(self.sections[current])
                continue
            for row in rows:
                for to_uid in row.get("to_section_uids", []):
                    if to_uid not in self.sections:
                        continue
                    if self.revision_rows(to_uid):
                        queue.append(to_uid)
                    else:
                        targets.append(self.sections[to_uid])
        dedup = {s["uid"]: s for s in targets}
        return [dedup[uid] for uid in sorted(dedup)]

    def reference_graph(self) -> dict:
        """The full traversable reference graph as nodes and typed edges."""
        nodes = (
            [{"id": t["uid"], "kind": "theme"} for t in self.themes.values()]
            + [{"id": s["uid"], "kind": "section", "edition": s["edition"],
                "language": s["language"], "number": s["number"]} for s in self.sections.values()]
            + [{"id": p["page_id"], "kind": "page", "url": p["url"]} for p in self.pages.values()]
            + [{"id": q["qr_code"], "kind": "qrcode"} for q in self.qrcodes]
        )
        edges = []
        for section in self.sections.values():
            edges.append({"from": section["uid"], "to": section["theme_uid"], "type": "section_theme"})
        for theme in self.themes.values():
            if theme.get("parent"):
                edges.append({"from": theme["parent"], "to": theme["uid"], "type": "theme_parent"})
        for revision in self.revisions:
            sources = revision.get("from_section_uids") or [revision["from_section_uid"]]
            for source in sources:
                for target in revision["to_section_uids"]:
                    edges.append({"from": source, "to": target, "type": f"revision_{revision['relation']}"})
        for qr in self.qrcodes:
            edges.append({"from": qr["qr_code"], "to": qr["section_uid"], "type": "printed_qr"})
        for link in self.body_links:
            edges.append({"from": link["source_page_id"], "to": link["url"],
                          "type": "body_link", "source_anchor": link["source_anchor"]})
        for page in self.pages.values():
            for anchor in page.get("anchors", []):
                edges.append({"from": page["page_id"], "to": anchor["theme_uid"], "type": "page_anchor"})
        return {"nodes": nodes, "edges": edges}

    def routes_for(self, qr_code: str, language: str | None = None) -> list[dict]:
        return [r for r in self.routes if r["qr_code"] == qr_code
                and (language is None or r["language"] == language)]

    def active_routes(self, qr_code: str, language: str) -> list[dict]:
        return [r for r in self.routes_for(qr_code, language) if r["status"] == "active"]


def _anchor_number(anchor: str) -> str:
    return anchor.rsplit("-", 1)[-1]


class LandingPageRPA:
    """Opens captured pages the way a user's browser would.

    The supplier PDF/web capture is treated as the live site: each page knows
    its HTTP status, anchors, outbound links and optional redirect. The RPA
    follows redirects, reports loops, and resolves the final anchor to its
    signed theme UID. No real network access is required.
    """

    def __init__(self, graph: ManualGraph):
        self.graph = graph

    def open(self, url: str, expected_language: str | None = None) -> dict:
        chain: list[dict] = []
        visited: set[str] = set()
        current = url
        issue = None
        for _ in range(MAX_HOPS + 1):
            base, anchor = split_url(current)
            page = self.graph.page_for_url(current)
            if page is None:
                issue = {"code": ISSUE_UNRESOLVED, "url": current,
                         "detail": "no captured page for URL", "http_status": None}
                return {"ok": False, "url": url, "chain": chain, "final_url": current,
                        "page_id": None, "anchor": anchor, "theme_uid": None, "issues": [issue]}
            hop = {"url": current, "page_id": page["page_id"], "http_status": int(page["http_status"])}
            chain.append(hop)
            if str(page["http_status"]) != "200":
                issue = {"code": ISSUE_HTTP_ERROR, "url": current, "http_status": int(page["http_status"]),
                         "detail": f"landing returned HTTP {page['http_status']}"}
                return self._fail(url, chain, current, page, anchor, [issue], expected_language)
            if current in visited:
                issue = {"code": ISSUE_REDIRECT_LOOP, "url": url,
                         "detail": "redirect chain revisited " + current}
                return self._fail(url, chain, current, page, anchor, [issue], expected_language)
            visited.add(current)
            if page.get("redirects_to"):
                current = page["redirects_to"]
                continue
            issues: list[dict] = []
            if expected_language and page["language"] != expected_language:
                issues.append({"code": ISSUE_CROSS_LANGUAGE, "url": current,
                               "expected_language": expected_language,
                               "actual_language": page["language"],
                               "detail": f"link from {expected_language} content lands on {page['language']} page"})
            if anchor:
                matched = next((a for a in page.get("anchors", []) if a["anchor"] == anchor), None)
                if matched is None:
                    issues.append({"code": ISSUE_DEAD_ANCHOR, "url": current, "anchor": anchor,
                                   "detail": "final page has no such anchor"})
                    return self._fail(url, chain, current, page, anchor, issues, expected_language)
                theme_uid = matched["theme_uid"]
            else:
                theme_uid = None
            stored = page.get("digest") or page_digest(page)
            actual = page_digest(page)
            if stored and stored != actual:
                issues.append({"code": ISSUE_DIGEST_MISMATCH, "page_id": page["page_id"],
                               "expected": stored, "actual": actual})
            return {"ok": not issues, "url": url, "chain": chain, "final_url": current,
                    "page_id": page["page_id"], "anchor": anchor, "theme_uid": theme_uid,
                    "digest": actual, "issues": issues}
        issue = {"code": ISSUE_REDIRECT_LOOP, "url": url,
                 "detail": f"more than {MAX_HOPS} redirects"}
        return {"ok": False, "url": url, "chain": chain, "final_url": current,
                "page_id": None, "anchor": split_url(current)[1], "theme_uid": None, "issues": [issue]}

    def _fail(self, url, chain, final_url, page, anchor, issues, expected_language) -> dict:
        return {"ok": False, "url": url, "chain": chain, "final_url": final_url,
                "page_id": page["page_id"] if page else None, "anchor": anchor,
                "theme_uid": None, "issues": issues}

    def reachable_themes(self, first_url: str, expected_language: str,
                         allowed_urls: set[str] | None = None) -> dict:
        """BFS over clickable links from a first landing.

        Returns the theme UID set a user can actually click to, the ordered
        hops, and every issue encountered. This is the verification that HTTP
        200 alone cannot provide. When ``allowed_urls`` is given, only those
        declared links are traversed (used for route verification, so defects
        on unrelated in-body links do not pollute a route's own verdict).
        """
        opened = self.open(first_url, expected_language)
        themes: set[str] = set()
        if opened["theme_uid"]:
            themes.add(opened["theme_uid"])
        issues = list(opened["issues"])
        seen_urls = {first_url}
        queue = deque([opened["final_url"]]) if opened["ok"] else deque()
        while queue:
            current = queue.popleft()
            page = self.graph.page_for_url(current)
            if page is None:
                continue
            for link in page.get("links", []):
                if link["url"] in seen_urls:
                    continue
                if allowed_urls is not None and link["url"] not in allowed_urls:
                    continue
                seen_urls.add(link["url"])
                result = self.open(link["url"], expected_language)
                issues.extend(result["issues"])
                if result["theme_uid"]:
                    themes.add(result["theme_uid"])
                if result["ok"]:
                    queue.append(result["final_url"])
        return {"first_url": first_url, "first_page_id": opened["page_id"],
                "first_theme_uid": opened["theme_uid"], "theme_uids": sorted(themes),
                "ok": opened["ok"] and not issues, "open": opened, "issues": issues}


def _issue(code: str, *, surface: str, source_page_id: str, language: str,
           url: str, detail: str, **extra) -> dict:
    return {"code": code, "surface": surface, "source_page_id": source_page_id,
            "language": language, "url": url, "detail": detail, **extra}


def _enrich(issues: list[dict], *, surface: str, source_page_id: str,
            language: str) -> list[dict]:
    """Give RPA-level issues the uniform envelope without overwriting context."""
    out = []
    for issue in issues:
        out.append({"code": issue["code"],
                    "surface": issue.get("surface", surface),
                    "source_page_id": issue.get("source_page_id") or source_page_id,
                    "language": issue.get("language") or language,
                    "url": issue.get("url", ""),
                    "detail": issue.get("detail", ""),
                    **{k: v for k, v in issue.items()
                       if k not in {"code", "surface", "source_page_id", "language", "url", "detail"}}})
    return out


def naive_number_destination(graph: ManualGraph, edition: str, language: str,
                             number: str) -> dict | None:
    """Where a bare printed number resolves on a given edition (the trap)."""
    section = graph.resolve_section(edition, language, number)
    if section is None:
        return None
    url = graph.section_url(section["uid"])
    return {"edition": edition, "language": language, "number": number,
            "section_uid": section["uid"], "theme_uid": section["theme_uid"], "url": url}


def single_number_patch(graph: ManualGraph, qr_code: str, new_number: dict[str, str]) -> dict:
    """Simulate the 'regular patch': one replacement number per language.

    Even with perfect OCR and language detection, a single number can only
    reach one of the two split themes, so the other half of the old chapter
    is silently lost.
    """
    result = {}
    for qr in (q for q in graph.qrcodes if q["qr_code"] == qr_code):
        language = qr["language"]
        original = graph.sections[qr["section_uid"]]
        required = graph.required_leaf_themes(qr["section_uid"])
        target_number = new_number[language]
        target = graph.resolve_section(graph.current_edition, language, target_number)
        covered = graph.leaf_themes(target["theme_uid"]) if target else set()
        result[language] = {
            "printed_number": original["number"], "replacement_number": target_number,
            "target_section_uid": target["uid"] if target else None,
            "target_theme_uid": target["theme_uid"] if target else None,
            "required_theme_uids": sorted(required),
            "covered_theme_uids": sorted(covered),
            "missing_theme_uids": sorted(required - covered),
        }
    return result


def verify_qr_naive(graph: ManualGraph, rpa: LandingPageRPA, qr_code: str,
                    language: str | None = None) -> list[dict]:
    """Report where each printed QR lands when resolved by number alone."""
    findings = []
    for qr in (q for q in graph.qrcodes if q["qr_code"] == qr_code
               and (language is None or q["language"] == language)):
        original = graph.sections[qr["section_uid"]]
        naive = naive_number_destination(graph, graph.current_edition,
                                         qr["language"], original["number"])
        opened = rpa.open(naive["url"], qr["language"])
        findings.append({
            "qr_code": qr_code, "language": qr["language"],
            "source_page_id": qr["source_page_id"], "printed_page": qr["printed_page"],
            "naive": naive, "opened": opened,
            "required_theme_uids": sorted(graph.required_leaf_themes(qr["section_uid"])),
        })
    return findings


def verify_route(graph: ManualGraph, rpa: LandingPageRPA, route: dict) -> dict:
    """Fully verify one authorised old-code route through real clicks."""
    qr = next(q for q in graph.qrcodes
              if q["qr_code"] == route["qr_code"] and q["language"] == route["language"])
    required = graph.required_leaf_themes(qr["section_uid"])
    allowed = {route["first_url"], *(e["url"] for e in route.get("entries", []))}
    reached = rpa.reachable_themes(route["first_url"], route["language"], allowed_urls=allowed)
    surface = f"route:{route['route_id']}"
    issues = _enrich(reached["issues"], surface=surface,
                     source_page_id=qr["source_page_id"], language=route["language"])
    entry_results = []
    for entry in route.get("entries", []):
        opened = rpa.open(entry["url"], route["language"])
        entry_results.append({"theme_uid": entry["theme_uid"], "url": entry["url"], "opened": opened})
        if opened["ok"] and opened["theme_uid"] != entry["theme_uid"]:
            issues.append(_issue(ISSUE_ANCHOR_THEME_MISMATCH,
                                 surface=surface,
                                 source_page_id=reached["first_page_id"] or qr["source_page_id"],
                                 language=route["language"], url=entry["url"],
                                 detail="entry anchor resolves to a different theme UID",
                                 expected_theme_uid=entry["theme_uid"],
                                 actual_theme_uid=opened["theme_uid"]))
        issues.extend(_enrich(opened["issues"], surface=surface,
                              source_page_id=qr["source_page_id"], language=route["language"]))
    reached_set = set(reached["theme_uids"])
    missing = required - reached_set
    if missing:
        issues.append(_issue(ISSUE_COVERAGE_GAP, surface=surface,
                             source_page_id=qr["source_page_id"], language=route["language"],
                             url=route["first_url"],
                             detail="reachable theme set does not cover the full old chapter",
                             required_theme_uids=sorted(required),
                             reachable_theme_uids=sorted(reached_set),
                             missing_theme_uids=sorted(missing)))
    if route["strategy"] == "forbidden_silent_single":
        issues.append(_issue(ISSUE_SILENT_SINGLE, surface=surface,
                             source_page_id=qr["source_page_id"], language=route["language"],
                             url=route["first_url"],
                             detail="old split chapter must not be silently redirected to one replacement number",
                             missing_theme_uids=sorted(missing)))
    return {"route_id": route["route_id"], "qr_code": route["qr_code"],
            "strategy": route["strategy"], "language": route["language"],
            "status": route["status"], "first_url": route["first_url"],
            "first_page_id": reached["first_page_id"],
            "first_theme_uid": reached["first_theme_uid"],
            "required_theme_uids": sorted(required),
            "reachable_theme_uids": sorted(reached_set),
            "entry_results": entry_results, "issues": issues, "ok": not issues}


def corrected_body_link(graph: ManualGraph, rpa: LandingPageRPA, link: dict,
                        strategy: str) -> dict:
    """Resolve an in-body link through the same signed revision mapping as a QR.

    A v1 Setup link that pointed at printed #7 must not be left pointing at
    the new #7 (Warranty/Cleaning). Its correction follows the active archive
    or index route carrying the same old chapter theme in the link language;
    the raw captured href is opened and reported separately.
    """
    raw = rpa.open(link["url"], link["language"])
    targets = graph.revision_targets(link["section_uid"])
    target_section = graph.sections[link["section_uid"]]
    # The route that protects this link is the active route on a printed QR
    # whose old section carries the same theme as the link's target section.
    route = next((r for r in graph.routes
                  if r["language"] == link["language"] and r["strategy"] == strategy
                  and r["status"] == "active" and any(
                      graph.sections[q["section_uid"]]["theme_uid"] == target_section["theme_uid"]
                      for q in graph.qrcodes
                      if q["qr_code"] == r["qr_code"] and q["language"] == link["language"])),
                 None)
    corrected = None
    issues = _enrich(raw["issues"], surface=f"body_link:{link['link_id']}",
                     source_page_id=link["source_page_id"], language=link["language"])
    if route is not None:
        corrected = verify_route(graph, rpa, route)
        issues.extend(corrected["issues"])
    return {"link_id": link["link_id"], "language": link["language"],
            "source_page_id": link["source_page_id"], "source_anchor": link["source_anchor"],
            "raw_url": link["url"], "raw_open": raw,
            "revision_target_section_uids": [t["uid"] for t in targets],
            "route_id": corrected["route_id"] if corrected else None,
            "first_url": corrected["first_url"] if corrected else None,
            "first_page_id": corrected["first_page_id"] if corrected else None,
            "reachable_theme_uids": corrected["reachable_theme_uids"] if corrected else [],
            "issues": issues, "ok": not issues}


def audit_body_links(graph: ManualGraph, rpa: LandingPageRPA) -> list[dict]:
    """Open every captured in-body hyperlink from its actual source page.

    Each failure (redirect loop, dead anchor, HTTP error, cross-language mix)
    is reported with the page it was found on, not just a status code.
    """
    findings = []
    for link in graph.body_links:
        opened = rpa.open(link["url"], link["language"])
        findings.append({"link_id": link["link_id"], "edition": link["edition"],
                         "language": link["language"], "source_page_id": link["source_page_id"],
                         "source_anchor": link["source_anchor"], "url": link["url"],
                         "final_url": opened["final_url"], "ok": opened["ok"],
                         "theme_uid": opened["theme_uid"],
                         "issues": _enrich(opened["issues"],
                                           surface=f"body_link:{link['link_id']}",
                                           source_page_id=link["source_page_id"],
                                           language=link["language"])})
    return findings


def new_packaging_issues(graph: ManualGraph) -> list[dict]:
    """New packaging may only print current-edition theme-UID links."""
    issues = []
    for qr in graph.qrcodes:
        if qr["edition"] != graph.current_edition:
            continue
        if not qr.get("printed_ref", "").startswith("theme:"):
            issues.append(_issue(ISSUE_NEW_PACKAGING_NUMBER, surface=f"qr:{qr['qr_code']}",
                                 source_page_id=qr["source_page_id"], language=qr["language"],
                                 url=graph.section_url(qr["section_uid"]),
                                 detail="new packaging must print a stable theme UID link, not a bare number",
                                 printed_ref=qr.get("printed_ref", "")))
    return issues


def link_package(graph: ManualGraph, language: str) -> dict:
    """Complete current-edition link package for one language."""
    sections = graph.sections_for(graph.current_edition, language)
    items = []
    for section in sections:
        page_id = graph._page_id_for_section(section["uid"])
        items.append({"section_uid": section["uid"], "number": section["number"],
                      "title": section["title"], "theme_uid": section["theme_uid"],
                      "printed_pages": section.get("printed_pages", []),
                      "url": graph.section_url(section["uid"], page_id)})
    return {"product": graph.product["product"], "edition": graph.current_edition,
            "language": language, "links": items}


def old_entry_checklist(graph: ManualGraph) -> list[dict]:
    """Every printed old-code entry: both authorised deliveries and any forbidden one."""
    checklist = []
    for qr in graph.qrcodes:
        if qr["edition"] == graph.current_edition:
            continue
        section = graph.sections[qr["section_uid"]]
        for route in graph.routes_for(qr["qr_code"], qr["language"]):
            first_page = graph.page_for_url(route["first_url"])
            checklist.append({
                "qr_code": qr["qr_code"], "language": qr["language"],
                "source_page_id": qr["source_page_id"], "printed_page": qr["printed_page"],
                "original_section_uid": section["uid"],
                "original_theme_uid": section["theme_uid"],
                "required_theme_uids": sorted(graph.required_leaf_themes(section["uid"])),
                "route_id": route["route_id"], "strategy": route["strategy"],
                "status": route["status"], "first_url": route["first_url"],
                "first_page_id": first_page["page_id"] if first_page else None,
                "first_landing_kind": _landing_kind(graph, route),
                "entry_theme_uids": [e["theme_uid"] for e in route.get("entries", [])],
                "forbidden": route["strategy"] == "forbidden_silent_single",
            })
    return checklist


def _landing_kind(graph: ManualGraph, route: dict) -> str:
    if route["strategy"] == "archive":
        return "archived_v1_section"
    if route["strategy"] == "index":
        return "dual_theme_index"
    return "silent_single_target"


def body_link_naive_destination(graph: ManualGraph, link: dict) -> dict | None:
    """Where an old in-body numbered reference lands under the new numbering."""
    old_section = graph.sections[link["section_uid"]]
    base, anchor = split_url(link["url"])
    number = _anchor_number(anchor) if anchor.startswith("sec-") else old_section["number"]
    return naive_number_destination(graph, graph.current_edition, link["language"], number)


def verify_all(graph: ManualGraph) -> dict:
    """Main deliverable: destinations, failure list, link packages, old-entry list."""
    rpa = LandingPageRPA(graph)
    issues: list[dict] = []
    destinations = {"printed_qrcodes": [], "body_links": []}

    seen_qr = set()
    for qr in graph.qrcodes:
        key = (qr["qr_code"], qr["language"])
        if key in seen_qr:
            continue
        seen_qr.add(key)
        for naive in verify_qr_naive(graph, rpa, qr["qr_code"], qr["language"]):
            destinations["printed_qrcodes"].append(naive)
            required = graph.required_leaf_themes(qr["section_uid"])
            if qr["edition"] != graph.current_edition and naive["opened"]["theme_uid"] not in required:
                issues.append(_issue(
                    ISSUE_WRONG_THEME_NAIVE, surface=f"qr:{qr['qr_code']}",
                    source_page_id=qr["source_page_id"], language=qr["language"],
                    url=naive["naive"]["url"],
                    detail=f"printed number {graph.sections[qr['section_uid']]['number']} now resolves to "
                           f"{naive['opened']['theme_uid']}, not the original chapter themes",
                    required_theme_uids=sorted(required),
                    actual_theme_uid=naive["opened"]["theme_uid"]))

    route_reports = []
    for route in graph.routes:
        report = verify_route(graph, rpa, route)
        route_reports.append(report)
        issues.extend(report["issues"])

    for link in graph.body_links:
        corrected_archive = corrected_body_link(graph, rpa, link, "archive")
        corrected_index = corrected_body_link(graph, rpa, link, "index")
        naive = body_link_naive_destination(graph, link)
        raw = corrected_archive["raw_open"]
        destinations["body_links"].append({
            "link_id": link["link_id"], "language": link["language"],
            "source_page_id": link["source_page_id"], "source_anchor": link["source_anchor"],
            "raw_url": link["url"], "raw_open": raw,
            "naive_v2_destination": naive,
            "archive_first_url": corrected_archive["first_url"],
            "index_first_url": corrected_index["first_url"],
            "archive_reachable": corrected_archive["reachable_theme_uids"],
            "index_reachable": corrected_index["reachable_theme_uids"],
        })
        # The uncorrected v1 reference must not silently land on the reused
        # number's unrelated current chapter (Warranty in English, Cleaning in Chinese).
        if link["edition"] != graph.current_edition and naive is not None:
            old_required = graph.required_leaf_themes(link["section_uid"])
            if naive["theme_uid"] not in old_required:
                issues.append(_issue(
                    ISSUE_WRONG_THEME_NAIVE, surface=f"body_link:{link['link_id']}",
                    source_page_id=link["source_page_id"], language=link["language"],
                    url=naive["url"],
                    detail="in-body numbered reference resolves to a reused, unrelated chapter under the new edition",
                    required_theme_uids=sorted(old_required),
                    actual_theme_uid=naive["theme_uid"]))

    # Global in-body link audit: every captured hyperlink is actually opened
    # and its defect class is attached to the page it was scanned from.
    body_audit = audit_body_links(graph, rpa)
    for finding in body_audit:
        issues.extend(finding["issues"])

    issues.extend(new_packaging_issues(graph))
    return {
        "product": graph.product["product"],
        "current_edition": graph.current_edition,
        "destinations": destinations,
        "body_link_audit": body_audit,
        "route_reports": route_reports,
        "issues": _dedupe_issues(issues),
        "link_packages": {lang: link_package(graph, lang)
                          for lang in graph.product["languages"]},
        "old_entry_checklist": old_entry_checklist(graph),
    }


def _dedupe_issues(issues: list[dict]) -> list[dict]:
    unique = {}
    for issue in issues:
        unique[canonical(issue)] = issue
    return [unique[key] for key in sorted(unique)]


# --------------------------------------------------------------------------
# Immutable ledger integration (lazy import keeps this module standalone)
# --------------------------------------------------------------------------

def graph_to_events(graph: ManualGraph, captured_at: str = "2026-10-05") -> list[dict]:
    """Project the reference graph into append-only ``manual_*`` source events."""
    events: list[dict] = []

    def add(event: dict) -> None:
        event.setdefault("event_at", captured_at)
        events.append(event)

    for page in graph.pages.values():
        add({"event_id": f"manual-page-{page['page_id']}", "kind": "manual_page",
             "page_id": page["page_id"], "source": page["source"],
             "language": page["language"], "edition": page["edition"],
             "url": page["url"], "http_status": str(page["http_status"]),
             "digest": page.get("digest") or page_digest(page),
             "redirects_to": page.get("redirects_to", "")})
        for anchor in page.get("anchors", []):
            add({"event_id": f"manual-anchor-{page['page_id']}-{anchor['anchor']}",
                 "kind": "manual_anchor", "page_id": page["page_id"],
                 "anchor": anchor["anchor"], "theme_uid": anchor["theme_uid"],
                 "title": anchor["title"]})
    for revision in graph.revisions:
        sources = revision.get("from_section_uids") or [revision["from_section_uid"]]
        for index, source_uid in enumerate(sources):
            add({"event_id": f"manual-revision-{revision['revision_id']}-{index}",
                 "kind": "manual_revision", "revision_id": revision["revision_id"],
                 "product": graph.product["product"], "edition": revision["edition"],
                 "language": revision["language"], "relation": revision["relation"],
                 "from_theme_uid": revision["from_theme_uid"],
                 "to_theme_uid": revision["to_theme_uids"][0]
                 if len(revision["to_theme_uids"]) == 1 else "",
                 "to_theme_uids": canonical(revision["to_theme_uids"]),
                 "section_uid": source_uid, "note": revision.get("note", ""),
                 "signed_by": revision["signed_by"],
                 "digest": revision.get("digest", "")})
    for qr in graph.qrcodes:
        add({"event_id": f"manual-qrcode-{qr['qr_code'].replace(':', '-')}-{qr['language']}",
             "kind": "manual_qrcode", "qr_code": qr["qr_code"],
             "offer_id": qr.get("offer_id", ""), "edition": qr["edition"],
             "language": qr["language"], "section_uid": qr["section_uid"],
             "printed_page": qr["printed_page"], "source_page_id": qr["source_page_id"]})
    for link in graph.body_links:
        add({"event_id": f"manual-bodylink-{link['link_id']}", "kind": "manual_body_link",
             "link_id": link["link_id"], "language": link["language"],
             "edition": link["edition"], "section_uid": link["section_uid"],
             "target_ref": link["url"], "source_page_id": link["source_page_id"],
             "source_anchor": link["source_anchor"]})
    for route in graph.routes:
        add({"event_id": f"manual-route-{route['route_id']}", "kind": "manual_route",
             "qr_code": route["qr_code"], "strategy": route["strategy"],
             "language": route["language"], "first_url": route["first_url"],
             "status": route["status"], "supersedes": route.get("supersedes", "")})
        for ordinal, entry in enumerate(route.get("entries", []), start=1):
            add({"event_id": f"manual-routeentry-{route['route_id']}-{ordinal}",
                 "kind": "manual_route_entry", "route_event_id": f"manual-route-{route['route_id']}",
                 "theme_uid": entry["theme_uid"], "url": entry["url"], "ordinal": str(ordinal)})
    return events


def graph_from_conn(conn) -> dict:
    """Rebuild the captured graph pieces from the immutable ledger.

    Late scans are ordinary rows here; callers use :func:`late_pages` and
    :func:`route_journal` to see arrival order and the correction trail.
    """
    from .store import rows

    pages: dict[str, dict] = {}
    for row in rows(conn, "manual_pages"):
        page = {"page_id": row["page_id"], "source": row["source"],
                "language": row["language"], "edition": row["edition"],
                "url": row["url"], "http_status": row["http_status"],
                "digest": row["digest"], "anchors": [], "links": []}
        if row.get("redirects_to"):
            page["redirects_to"] = row["redirects_to"]
        pages[row["page_id"]] = page
    for row in rows(conn, "manual_anchors"):
        if row["page_id"] in pages:
            pages[row["page_id"]]["anchors"].append(
                {"anchor": row["anchor"], "theme_uid": row["theme_uid"], "title": row["title"]})
    anchor_index = {(p["page_id"], a["anchor"]): a["theme_uid"]
                    for p in pages.values() for a in p["anchors"]}
    for row in rows(conn, "manual_body_links"):
        page = pages.get(row["source_page_id"])
        if page is not None:
            base, anchor = split_url(row["target_ref"])
            theme_uid = anchor_index.get((page["page_id"], anchor), "")
            page["links"].append({"url": row["target_ref"], "theme_uid": theme_uid})
    qrcodes = [{"qr_code": r["qr_code"], "offer_id": r["offer_id"], "edition": r["edition"],
                "language": r["language"], "section_uid": r["section_uid"],
                "printed_page": r["printed_page"], "source_page_id": r["source_page_id"]}
               for r in rows(conn, "manual_qrcodes")]
    body_links = [{"link_id": r["link_id"], "language": r["language"], "edition": r["edition"],
                   "section_uid": r["section_uid"], "url": r["target_ref"],
                   "source_page_id": r["source_page_id"], "source_anchor": r["source_anchor"]}
                  for r in rows(conn, "manual_body_links")]
    entries: dict[str, list] = {}
    for row in rows(conn, "manual_route_entries"):
        entries.setdefault(row["route_event_id"], []).append(
            {"theme_uid": row["theme_uid"], "url": row["url"]})
    routes = [{"route_id": r["event_id"].removeprefix("manual-route-"),
               "qr_code": r["qr_code"], "strategy": r["strategy"], "language": r["language"],
               "first_url": r["first_url"], "status": r["status"],
               "supersedes": r["supersedes"],
               "entries": entries.get(r["event_id"], [])}
              for r in rows(conn, "manual_routes")]
    revisions: dict[str, dict] = {}
    for row in rows(conn, "manual_revisions"):
        stored_uids = json.loads(row["to_theme_uids"]) if row.get("to_theme_uids") else []
        item = revisions.setdefault(row["revision_id"], {
            "revision_id": row["revision_id"], "edition": row["edition"],
            "language": row["language"], "relation": row["relation"],
            "from_theme_uid": row["from_theme_uid"],
            "to_theme_uids": stored_uids,
            "from_section_uids": [], "signed_by": row["signed_by"],
            "digest": row["digest"], "note": row["note"]})
        item["from_section_uids"].append(row["section_uid"])
    return {"pages": sorted(pages.values(), key=lambda p: p["page_id"]),
            "qrcodes": qrcodes, "body_links": body_links, "routes": routes,
            "revisions": sorted(revisions.values(), key=lambda r: r["revision_id"])}


def route_journal(conn, qr_code: str | None = None) -> list[dict]:
    """Full route history per old code, including superseded deliveries.

    The delivered first landing and every later correction stay visible:
    late evidence never overwrites the earlier route decision.
    """
    from .store import rows

    journal = []
    for row in rows(conn, "manual_routes"):
        if qr_code is not None and row["qr_code"] != qr_code:
            continue
        journal.append({"event_id": row["event_id"], "event_at": row["event_at"],
                        "qr_code": row["qr_code"], "strategy": row["strategy"],
                        "language": row["language"], "first_url": row["first_url"],
                        "status": row["status"], "supersedes": row["supersedes"]})
    return sorted(journal, key=lambda r: (r["event_at"], r["event_id"]))


def late_pages(conn, as_of: str) -> list[dict]:
    """Scanned pages that arrived after ``as_of`` (late evidence)."""
    from .store import rows

    return [{"event_id": r["event_id"], "event_at": r["event_at"], "page_id": r["page_id"],
             "source": r["source"], "language": r["language"], "edition": r["edition"],
             "url": r["url"]}
            for r in rows(conn, "manual_pages") if r["event_at"] > as_of]
