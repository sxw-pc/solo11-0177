"""Bank-attributable refund settlement certificates over a bipartite graph.

The :func:`provisional_matches` greedy is retained as the audited *baseline*
that this module replaces: it consumes receipts in input order and cannot see
the global augmenting path, so on the fixed family case it settles only one of
two claims.  Everything below works on a bipartite graph whose left vertices
are real bank flows (one vertex per bank transaction number, duplicate pages
merged) and whose right vertices are merchant refund claims.

Legal edges are *candidate* attribution only: same masked account, equal
amount, refund-code compatibility and a still-settled bank flow.  A batch or
account coincidence never proves attribution by itself.
"""
from __future__ import annotations

from .store import canonical, digest

RECORD_TYPES = frozenset({
    "merchant_claim", "bank_flow", "account_mapping", "refund_code",
    "transfer_authorization", "bank_revocation",
})

FLOW_STATUSES = frozenset({"settled", "revoked"})
PROTOCOL_TRANSFERABLE = "transferable"
PROTOCOL_NON_TRANSFERABLE = "non_transferable"


def provisional_matches(receipts, claims):
    """Assign each receipt to the first unclaimed compatible order in input order."""
    available = {claim["order_id"]: claim for claim in claims}
    matches = []
    unmatched = []
    for receipt in receipts:
        eligible = [order_id for order_id, claim in available.items()
                    if claim["account"] == receipt["account"]
                    and claim["amount"] == receipt["amount"]
                    and (not receipt.get("order_code") or receipt["order_code"] == order_id)]
        if not eligible:
            unmatched.append(receipt["txid"])
            continue
        order_id = eligible[0]
        matches.append({"txid": receipt["txid"], "order_id": order_id})
        del available[order_id]
    return {"matches": matches, "unmatched_receipts": unmatched,
            "unpaid_orders": list(available)}


# --------------------------------------------------------------------------- #
# Offline collection record validation and canonical normalisation
# --------------------------------------------------------------------------- #

def _require(value, kinds, label):
    if not isinstance(value, kinds) or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{label} has an invalid type or is empty")


def validate_refund_record(record: object) -> None:
    """Shape-check one offline-collected record. Conflicts stay data, shape is error."""
    _require(record, dict, "refund record")
    rtype = record.get("record_type")
    if rtype not in RECORD_TYPES:
        raise ValueError(f"unknown refund record_type: {rtype!r}")
    for key in ("record_id", "captured_at", "record_type"):
        _require(record.get(key), str, key)
    if rtype == "merchant_claim":
        for key in ("order_id", "account_id", "amount", "refund_due"):
            if key not in record:
                raise ValueError(f"merchant_claim missing field: {key}")
        _require(record["order_id"], str, "order_id")
        _require(record["account_id"], str, "account_id")
    elif rtype == "bank_flow":
        for key in ("txid", "account_id", "amount", "status", "doc_id", "page_no",
                    "page_bbox", "seal_digest"):
            if key not in record:
                raise ValueError(f"bank_flow missing field: {key}")
        _require(record["txid"], str, "txid")
        _require(record["account_id"], str, "account_id")
        _require(record["doc_id"], str, "doc_id")
        _require(record["page_bbox"], (list, tuple), "page_bbox")
        _require(record["seal_digest"], str, "seal_digest")
        if record["status"] not in FLOW_STATUSES:
            raise ValueError("bank_flow status must be 'settled' or 'revoked'")
        if "duplicate_of_txid" in record and record["duplicate_of_txid"] is not None:
            _require(record["duplicate_of_txid"], str, "duplicate_of_txid")
    elif rtype == "account_mapping":
        for key in ("account_id", "masked_account"):
            if key not in record:
                raise ValueError(f"account_mapping missing field: {key}")
            _require(record[key], str, key)
    elif rtype == "refund_code":
        for key in ("code", "txid", "order_id"):
            if key not in record:
                raise ValueError(f"refund_code missing field: {key}")
            _require(record[key], str, key)
    elif rtype == "transfer_authorization":
        for key in ("authorization_id", "protocol", "order_id"):
            if key not in record:
                raise ValueError(f"transfer_authorization missing field: {key}")
            _require(record[key], str, key)
        if record["protocol"] not in {PROTOCOL_TRANSFERABLE, PROTOCOL_NON_TRANSFERABLE}:
            raise ValueError("authorization protocol must be transferable or non_transferable")
        if "scope_orders" in record and record["scope_orders"] is not None:
            _require(record["scope_orders"], (list, tuple), "scope_orders")
    elif rtype == "bank_revocation":
        for key in ("txid", "doc_id", "page_no", "page_bbox", "seal_digest"):
            if key not in record:
                raise ValueError(f"bank_revocation missing field: {key}")
            _require(record[key], str if key in {"txid", "doc_id", "seal_digest"}
                     else (int, list, tuple), key)
    for key in ("amount", "refund_due", "page_no"):
        if key in record and record[key] is not None:
            if type(record[key]) is not int or record[key] < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
    if "amount" in record and record["amount"] <= 0:
        raise ValueError("amount must be positive")
    if "refund_due" in record and record["refund_due"] <= 0:
        raise ValueError("refund_due must be positive")


def normalize_sources(records: list[dict]) -> dict:
    """Group raw records into a canonical, order-independent source snapshot.

    The same set of record bodies always normalises to the same JSON form even
    when pages arrive late, page numbers roll backwards, or the upload order
    changes; ingestion order is deliberately discarded.
    """
    if not isinstance(records, list) or not records:
        raise ValueError("refund sources must be a nonempty list")
    for record in records:
        validate_refund_record(record)
    seen_ids: set[str] = set()
    grouped: dict[str, list[dict]] = {rtype: [] for rtype in sorted(RECORD_TYPES)}
    for record in records:
        rid = record["record_id"]
        if rid in seen_ids:
            raise ValueError(f"duplicate record_id: {rid}")
        seen_ids.add(rid)
        grouped[record["record_type"]].append(record)
    for rtype in grouped:
        grouped[rtype] = sorted(grouped[rtype], key=lambda r: canonical(r))
    return {"source_set_hash": digest(grouped), "records": grouped}


# --------------------------------------------------------------------------- #
# Bipartite graph construction
# --------------------------------------------------------------------------- #

def _flow_vertex(record: dict) -> dict:
    return {
        "txid": record["txid"],
        "account_id": record["account_id"],
        "amount": record["amount"],
        "batch_id": record.get("batch_id"),
        "status": record["status"],
        "pages": [{
            "record_id": record["record_id"], "doc_id": record["doc_id"],
            "page_no": record["page_no"], "page_bbox": record["page_bbox"],
            "seal_digest": record["seal_digest"],
            "duplicate_of_txid": record.get("duplicate_of_txid"),
        }],
        "revocations": [],
    }


def build_refund_graph(records: list[dict]) -> dict:
    """Build the auditable bipartite graph from offline collection records.

    One left vertex exists per bank transaction number: a repeated page carrying
    the same ``txid`` is merged as additional page evidence and can never be
    counted as a second flow.  Revocation pages flip the flow to ``revoked``,
    after which it carries no legal edges.
    """
    normalized = normalize_sources(records)
    groups = normalized["records"]

    mappings: dict[str, set[str]] = {}
    for rec in groups["account_mapping"]:
        mappings.setdefault(rec["account_id"], set()).add(rec["masked_account"])

    flows: dict[str, dict] = {}
    duplicate_pages: list[dict] = []
    for rec in groups["bank_flow"]:
        if rec["account_id"] not in mappings:
            raise ValueError(f"bank_flow {rec['txid']} has no account mapping evidence")
        existing = flows.get(rec["txid"])
        if rec.get("duplicate_of_txid"):
            if rec["duplicate_of_txid"] != rec["txid"]:
                raise ValueError(
                    f"duplicate page {rec['record_id']} must repeat its own txid")
            duplicate_pages.append(rec)
            if existing is None:
                # Late first sighting still proves only the single transaction.
                flows[rec["txid"]] = _flow_vertex(rec)
            else:
                existing["pages"].append(_flow_vertex(rec)["pages"][0])
            continue
        if existing is not None:
            raise ValueError(f"txid {rec['txid']} restated as a new bank flow")
        flows[rec["txid"]] = _flow_vertex(rec)

    claims: dict[str, dict] = {}
    for rec in groups["merchant_claim"]:
        if rec["account_id"] not in mappings:
            raise ValueError(f"claim {rec['order_id']} has no account mapping evidence")
        if rec["amount"] != rec["refund_due"]:
            raise ValueError(
                f"claim {rec['order_id']} amount {rec['amount']} differs from the "
                f"rights/delivery refund_due snapshot {rec['refund_due']}")
        claims[rec["order_id"]] = {
            "order_id": rec["order_id"], "account_id": rec["account_id"],
            "amount": rec["amount"], "refund_due": rec["refund_due"],
            "record_id": rec["record_id"], "seal_digest": rec.get("seal_digest"),
        }

    # Refund codes attach a claim-specific token to one bank transaction.
    code_by_txid: dict[str, str] = {}
    for rec in groups["refund_code"]:
        if rec["txid"] not in flows:
            raise ValueError(f"refund_code {rec['code']} references unknown txid {rec['txid']}")
        if rec["order_id"] not in claims:
            raise ValueError(f"refund_code {rec['code']} references unknown order {rec['order_id']}")
        if code_by_txid.setdefault(rec["txid"], rec["order_id"]) != rec["order_id"]:
            raise ValueError(f"txid {rec['txid']} carries conflicting refund codes")
        flows[rec["txid"]]["order_code"] = rec["code"]
        flows[rec["txid"]]["code_order_id"] = rec["order_id"]

    # Bank revocation pages are evidence attached to the flow they cancel.
    for rec in groups["bank_revocation"]:
        flow = flows.get(rec["txid"])
        if flow is None:
            raise ValueError(f"revocation {rec['record_id']} references unknown txid {rec['txid']}")
        flow["status"] = "revoked"
        flow["revocations"].append({
            "record_id": rec["record_id"], "doc_id": rec["doc_id"],
            "page_no": rec["page_no"], "page_bbox": rec["page_bbox"],
            "seal_digest": rec["seal_digest"],
        })

    authorizations = [{
        "authorization_id": rec["authorization_id"], "protocol": rec["protocol"],
        "order_id": rec["order_id"],
        "scope_orders": sorted(rec.get("scope_orders") or [rec["order_id"]]),
        "record_id": rec["record_id"],
    } for rec in groups["transfer_authorization"]]

    edges = []
    for txid in sorted(flows):
        flow = flows[txid]
        if flow["status"] != "settled":
            continue
        coded_order = code_by_txid.get(txid)
        for order_id in sorted(claims):
            claim = claims[order_id]
            if flow["account_id"] != claim["account_id"]:
                continue
            if flow["amount"] != claim["amount"]:
                continue
            if coded_order is not None and coded_order != order_id:
                continue
            reasons = ["same_masked_account", "equal_amount"]
            if flow.get("batch_id"):
                reasons.append("same_batch")
            if coded_order == order_id:
                reasons.append("refund_code_match")
            edges.append({
                "txid": txid, "order_id": order_id, "amount": flow["amount"],
                "candidate_basis": reasons,
                "deterministic": coded_order == order_id,
            })

    graph = {
        "source_set_hash": normalized["source_set_hash"],
        "account_mappings": [
            {"account_id": account_id, "masked_accounts": sorted(masked)}
            for account_id, masked in sorted(mappings.items())
        ],
        "bank_flows": [flows[txid] for txid in sorted(flows)],
        "claims": [claims[oid] for oid in sorted(claims)],
        "legal_edges": edges,
        "duplicate_pages": [{
            "record_id": rec["record_id"], "txid": rec["txid"],
            "doc_id": rec["doc_id"], "page_no": rec["page_no"],
            "page_bbox": rec["page_bbox"], "seal_digest": rec["seal_digest"],
        } for rec in sorted(duplicate_pages, key=lambda r: r["record_id"])],
        "authorizations": authorizations,
    }
    graph["graph_hash"] = digest({
        "flows": graph["bank_flows"], "claims": graph["claims"],
        "edges": graph["legal_edges"], "mappings": graph["account_mappings"],
    })
    return graph


# --------------------------------------------------------------------------- #
# Maximum bipartite matching with explicit augmenting-path evidence
# --------------------------------------------------------------------------- #

def _find_augmenting_path(txids, adjacency, match_claim, start_txid,
                          locked_edges=frozenset()):
    """BFS alternating search from one unmatched left vertex.

    Returns the alternating left/right vertex path when an augmenting path
    exists, otherwise the trail ending at the lock that blocks it.
    """
    parents = {start_txid: None}          # vertex -> (previous vertex, edge)
    seen_left = {start_txid}
    frontier = [start_txid]
    blocked_lock = None
    while frontier:
        next_frontier = []
        for left in frontier:
            for right in adjacency[left]:
                edge = (left, right)
                if edge in locked_edges:
                    if blocked_lock is None:
                        blocked_lock = edge
                    continue
                if right in parents:
                    continue
                parents[right] = (left, edge)
                if match_claim.get(right) is None:
                    path_right = [right]
                    node = right
                    while parents[node] is not None:
                        prev, used_edge = parents[node]
                        path_right.append(prev)
                        node = prev
                    return list(reversed(path_right)), None
                matched_left = match_claim[right]
                if matched_left not in seen_left:
                    seen_left.add(matched_left)
                    parents[matched_left] = (right, (matched_left, right))
                    next_frontier.append(matched_left)
        frontier = next_frontier
    return None, blocked_lock


def maximum_matching(graph: dict, locked_edges=frozenset()):
    """Kuhn augmenting-path matching, returning the full audit trail.

    ``locked_edges`` are certificate-level constraints from previously issued
    non-transferable vouchers: they remain edges of the graph but the matcher
    may not traverse them, which is exactly how an issued certificate constrains
    later settlement rather than being silently overwritten.
    """
    adjacency: dict[str, list[str]] = {}
    legal = set()
    for edge in graph["legal_edges"]:
        adjacency.setdefault(edge["txid"], []).append(edge["order_id"])
        legal.add((edge["txid"], edge["order_id"]))
    for lock in locked_edges:
        if lock not in legal:
            raise ValueError(f"locked edge {lock} is not a legal edge of the graph")
    txids = sorted(adjacency)
    claims = sorted(c["order_id"] for c in graph["claims"])
    for txid in txids:
        adjacency[txid].sort()

    match_claim: dict[str, str] = {}
    match_flow: dict[str, str] = {}
    locked_flows = {edge[0] for edge in locked_edges}
    for edge in sorted(locked_edges):
        left, right = edge
        if right in match_claim or left in match_flow:
            raise ValueError("locked edges must be pairwise disjoint")
        match_claim[right] = left
        match_flow[left] = right

    iterations = []
    for txid in txids:
        if txid in match_flow:
            continue
        path, blocked_lock = _find_augmenting_path(
            txids, adjacency, match_claim, txid, frozenset(locked_edges))
        if path is None:
            iterations.append({
                "start": txid, "augmenting": False,
                "blocked_by_locked_edge": list(blocked_lock) if blocked_lock else None,
            })
            continue
        flipped = []
        for i in range(0, len(path) - 1, 2):
            left, right = path[i], path[i + 1]
            previous = match_claim.get(right)
            match_claim[right] = left
            match_flow[left] = right
            flipped.append({"edge": [left, right], "action": "add",
                            "displaced": previous})
            if previous is not None:
                match_flow.pop(previous, None)
        iterations.append({
            "start": txid, "augmenting": True,
            "alternating_path": path, "flipped_edges": flipped,
        })

    pairs = sorted([txid, order_id] for order_id, txid in match_claim.items())
    unmatched_flows = sorted(t for t in txids if t not in match_flow)
    # Revoked / duplicate-merged flows never participate in adjacency at all.
    unmatched_flows.extend(sorted(
        f["txid"] for f in graph["bank_flows"]
        if f["txid"] not in adjacency and f["txid"] not in match_flow))
    unmatched_claims = sorted(c for c in claims if c not in match_claim)
    return {
        "pairs": pairs, "size": len(pairs),
        "unmatched_flows": sorted(set(unmatched_flows)),
        "unmatched_claims": unmatched_claims,
        "iterations": iterations,
    }


def konig_min_vertex_cover(graph: dict, matching_pairs: list[list[str]]) -> dict:
    """Minimum vertex cover via König's theorem; it certifies maximality.

    Start BFS from unmatched left vertices along unmatched edges, alternating
    through matched edges.  The minimum cover is left vertices unreachable
    together with right vertices reachable.  Its cardinality equals the
    matching size, which proves the matching is maximum.
    """
    adjacency: dict[str, list[str]] = {}
    for edge in graph["legal_edges"]:
        adjacency.setdefault(edge["txid"], []).append(edge["order_id"])
    lefts = sorted(adjacency)
    for left in lefts:
        adjacency[left].sort()
    matched_right = {pair[0]: pair[1] for pair in matching_pairs}
    matched_left = {pair[1]: pair[0] for pair in matching_pairs}

    reachable_left, reachable_right = set(), set()
    frontier = [left for left in lefts if left not in matched_right]
    reachable_left.update(frontier)
    while frontier:
        nxt = []
        for left in frontier:
            for right in adjacency[left]:
                if right in reachable_right or [left, right] in matching_pairs:
                    continue
                reachable_right.add(right)
                back = matched_left.get(right)
                if back and back not in reachable_left:
                    reachable_left.add(back)
                    nxt.append(back)
        frontier = nxt
    cover_left = sorted(set(lefts) - reachable_left)
    cover_right = sorted(reachable_right)
    return {
        "bank_flow_vertices": cover_left, "claim_vertices": cover_right,
        "size": len(cover_left) + len(cover_right),
        "theorem": "Konig: cover size equals maximum matching size, proving maximality",
    }


# --------------------------------------------------------------------------- #
# Certificate construction
# --------------------------------------------------------------------------- #

def _pair_evidence(graph: dict, txid: str, order_id: str) -> dict:
    flow = next(f for f in graph["bank_flows"] if f["txid"] == txid)
    edge = next(e for e in graph["legal_edges"]
                if e["txid"] == txid and e["order_id"] == order_id)
    return {
        "txid": txid, "order_id": order_id, "amount": flow["amount"],
        "candidate_basis": edge["candidate_basis"],
        "deterministic_edge": edge["deterministic"],
        "page_evidence": flow["pages"],
        "seal_digests": sorted({p["seal_digest"] for p in flow["pages"]}),
        "revocation_evidence": flow["revocations"],
    }


def _authorization_for(authorizations: list[dict], protocol: str, order_id: str):
    for auth in authorizations:
        if auth["protocol"] != protocol:
            continue
        if order_id in auth["scope_orders"]:
            return auth["authorization_id"]
    return None


def analyze_graph(graph: dict, locked_edges=frozenset()) -> dict:
    """Full settlement analysis: free matching, constrained matching, cover."""
    free = maximum_matching(graph)
    constrained = maximum_matching(graph, frozenset(locked_edges))
    cover = konig_min_vertex_cover(graph, free["pairs"])

    blocked_paths = []
    for step in constrained["iterations"]:
        if not step["augmenting"] and step.get("blocked_by_locked_edge"):
            blocked_paths.append({
                "start": step["start"],
                "blocked_by_locked_edge": step["blocked_by_locked_edge"],
                "reason": "augmenting path exists on the legal graph but the "
                          "issued non-transferable certificate locks its edge",
            })
    return {
        "matching_number": free["size"],
        "maximum_matching": free["pairs"],
        "augmenting_path_evidence": free["iterations"],
        "locked_edges": sorted([list(edge) for edge in locked_edges]),
        "constrained_matching": constrained["pairs"],
        "constrained_matching_size": constrained["size"],
        "blocked_augmenting_paths": blocked_paths,
        "unmatched_flows": constrained["unmatched_flows"],
        "unmatched_claims": constrained["unmatched_claims"],
        "min_obstacle_set": cover,
        "fully_settled": not constrained["unmatched_flows"]
        and not constrained["unmatched_claims"],
    }


def _certificate_id(payload: dict) -> str:
    return "RC-" + digest(payload)[:16]


def baseline_certificate(records: list[dict], *, protocol: str,
                         issued_at: str | None = None,
                         revision: int = 1) -> dict:
    """Reproduce the input-order greedy result as a version-1 certificate.

    For the transferable protocol this is a revocable internal provisional
    allocation; for the non-transferable protocol it is an issued bank
    write-off that becomes a locked edge for every later certificate.
    """
    if protocol not in {PROTOCOL_TRANSFERABLE, PROTOCOL_NON_TRANSFERABLE}:
        raise ValueError("unknown protocol")
    graph = build_refund_graph(records)
    active = [f for f in graph["bank_flows"] if f["status"] == "settled"]
    receipts = [{
        "txid": f["txid"], "account": f["account_id"], "amount": f["amount"],
        **({"order_code": f["code_order_id"]} if f.get("code_order_id") else {}),
    } for f in active]
    claim_rows = [{"order_id": c["order_id"], "account": c["account_id"],
                   "amount": c["amount"]} for c in graph["claims"]]
    greedy = provisional_matches(receipts, claim_rows)
    pairs = [[m["txid"], m["order_id"]] for m in greedy["matches"]]
    captured = [r["captured_at"] for recs in normalize_sources(records)["records"].values()
                for r in recs]
    payload = {
        "schema": "refund-settlement-certificate",
        "revision": revision, "protocol": protocol, "baseline_greedy": True,
        "graph_hash": graph["graph_hash"],
        "source_set_hash": graph["source_set_hash"],
        "issued_at": issued_at or max(captured),
        "pairs": pairs,
        "unmatched_flows": greedy["unmatched_receipts"],
        "unmatched_claims": greedy["unpaid_orders"],
        "note": "input-order greedy baseline, not a maximum matching",
    }
    cert_id = _certificate_id(payload)
    return {
        **payload, "certificate_id": cert_id,
        "pair_evidence": [_pair_evidence(graph, *pair) for pair in pairs],
        "graph": graph,
    }


def settle(records: list[dict], *, protocol: str,
           locked_edges=frozenset(), issued_at: str | None = None,
           revision: int = 1, supersedes: list[str] | None = None) -> dict:
    """Issue the reviewable bipartite-graph settlement certificate."""
    if protocol not in {PROTOCOL_TRANSFERABLE, PROTOCOL_NON_TRANSFERABLE}:
        raise ValueError("unknown protocol")
    graph = build_refund_graph(records)
    analysis = analyze_graph(graph, locked_edges)
    captured = [r["captured_at"] for recs in normalize_sources(records)["records"].values()
                for r in recs]
    when = issued_at or max(captured)

    pair_evidence = [_pair_evidence(graph, *pair)
                     for pair in analysis["constrained_matching"]]
    matched_flows = {pair[0] for pair in analysis["constrained_matching"]}
    settled_claims = {pair[1] for pair in analysis["constrained_matching"]}

    pending_funds = []
    for txid in analysis["unmatched_flows"]:
        flow = next(f for f in graph["bank_flows"] if f["txid"] == txid)
        if flow["status"] == "revoked":
            continue
        pending_funds.append({
            "txid": txid, "amount": flow["amount"],
            "disposition": "return_to_sender_via_bank",
            "reason": ("locked non-transferable edge blocks the augmenting "
                       "path that would attribute this flow"
                       if analysis["blocked_augmenting_paths"]
                       else "no legal edge to any unsettled claim"),
        })

    unsettled_claims = []
    for order_id in analysis["unmatched_claims"]:
        claim = next(c for c in graph["claims"] if c["order_id"] == order_id)
        unsettled_claims.append({
            "order_id": order_id, "amount": claim["amount"],
            "attribution_status": "unproven",
            "reason": "no bank flow can be exclusively attributed to this claim",
        })

    resign_targets = []
    if protocol == PROTOCOL_NON_TRANSFERABLE and locked_edges:
        for left, right in sorted(locked_edges):
            resign_targets.append({
                "edge": [left, right],
                "requires_customer_resignation": True,
                "reason": "reassigning an issued non-transferable bank write-off "
                          "needs a fresh customer authorization",
            })
        for item in unsettled_claims:
            resign_targets.append({
                "order_id": item["order_id"],
                "requires_customer_resignation": True,
                "reason": "a new transferable authorization is required before "
                          "any later flow can settle this claim",
            })

    reassignments = []
    if protocol == PROTOCOL_TRANSFERABLE and supersedes:
        prior_pairs = {(left, right) for left, right in locked_edges}
        for left, right in analysis["maximum_matching"]:
            if (left, right) not in prior_pairs:
                reassignments.append({
                    "txid": left, "now_order_id": right,
                    "authorization_id": _authorization_for(
                        graph["authorizations"], PROTOCOL_TRANSFERABLE, right),
                    "customer_resignation_required": False,
                    "reason": "pre-authorized transferable consent covers reassignment",
                })

    payload = {
        "schema": "refund-settlement-certificate",
        "revision": revision,
        "protocol": protocol,
        "baseline_greedy": False,
        "certificate_kind": ("locked_settlement" if locked_edges
                             else "maximum_settlement"),
        "graph_hash": graph["graph_hash"],
        "source_set_hash": graph["source_set_hash"],
        "issued_at": when,
        "supersedes": sorted(supersedes or []),
        "legal_edges": graph["legal_edges"],
        "locked_edges": analysis["locked_edges"],
        "matching_number": analysis["matching_number"],
        "matching": analysis["constrained_matching"],
        "matching_size": analysis["constrained_matching_size"],
        "augmenting_path_evidence": analysis["augmenting_path_evidence"],
        "blocked_augmenting_paths": analysis["blocked_augmenting_paths"],
        "min_obstacle_set": analysis["min_obstacle_set"],
        "fully_settled": analysis["fully_settled"],
        "duplicate_pages": graph["duplicate_pages"],
        "pending_funds": pending_funds,
        "unsettled_claims": unsettled_claims,
        "resignation_required": resign_targets,
        "reassigned_provisional_edges": reassignments,
        "authorizations": graph["authorizations"],
    }
    return {**payload, "certificate_id": _certificate_id(payload),
            "pair_evidence": pair_evidence, "graph": graph}
