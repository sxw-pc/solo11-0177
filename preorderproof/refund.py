"""Bank-attributable refund settlement certificates over a bipartite evidence graph.

The seed :func:`provisional_matches` is a first-fit greedy baseline.  It is kept
for replay/history compatibility but must not be used to certify attribution:
a global maximum matching (:func:`maximum_matching`) with an auditable
augmenting-path proof is the replacement.  Candidate edges only mean "the same
bank flow could legally settle this claim"; they are never a determination of
attribution, and same-account amounts are never summed into an account-level
total.
"""
from __future__ import annotations

import re
from collections import OrderedDict

from .store import canonical, digest

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SECTION_KINDS = {
    "claims": "merchant_refund_claim",
    "bank_flows": "bank_flow",
    "account_mappings": "account_mapping",
    "refund_codes": "refund_code",
    "authorizations": "transfer_authorization",
    "revocations": "bank_revocation",
}
_CLAIM_STATUSES = {"valid", "withdrawn"}
_PROTOCOLS = {"revocable_provisional", "non_transferable_bank_writeoff"}


# --------------------------------------------------------------------------- #
# Seed baseline (do not use for externally issued certificates)
# --------------------------------------------------------------------------- #
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
# Offline evidence validation
# --------------------------------------------------------------------------- #
def _require_page_ref(record):
    page = record.get("page_ref")
    if not isinstance(page, dict):
        raise ValueError(f"{record.get('record_id')}: page_ref required")
    if not isinstance(page.get("document"), str) or not page["document"].strip():
        raise ValueError("page_ref.document must be nonempty text")
    if type(page.get("page")) is not int or page["page"] < 1:
        raise ValueError("page_ref.page must be a positive integer")
    rect = page.get("rect")
    if not (isinstance(rect, list) and len(rect) == 4
            and all(type(x) is int and x >= 0 for x in rect)):
        raise ValueError("page_ref.rect must be four nonnegative integers")
    if not isinstance(page.get("seal_digest"), str) or not _HEX64.match(page["seal_digest"]):
        raise ValueError("page_ref.seal_digest must be a sha256 hex digest")
    return page


def validate_bundle(bundle):
    """Structural validation of an offline collection record set.

    Business conflicts (withdrawn claims, revoked flows, duplicate pages) are
    represented on the graph, not raised; malformed or contradictory identity
    records are errors because the evidence set itself cannot be replayed.
    """
    if not isinstance(bundle, dict):
        raise ValueError("refund bundle must be a JSON object")
    if not isinstance(bundle.get("batch_id"), str) or not bundle["batch_id"].strip():
        raise ValueError("batch_id must be nonempty text")
    seen_ids = set()
    for section, kind in _SECTION_KINDS.items():
        records = bundle.get(section, [])
        if not isinstance(records, list):
            raise ValueError(f"{section} must be a list")
        for record in records:
            if not isinstance(record, dict):
                raise ValueError(f"{section} entries must be objects")
            record_id = record.get("record_id")
            if not isinstance(record_id, str) or not record_id.strip():
                raise ValueError(f"{section}: record_id must be nonempty text")
            if record_id in seen_ids:
                raise ValueError(f"duplicate record_id across evidence set: {record_id}")
            seen_ids.add(record_id)
            if record.get("kind") != kind:
                raise ValueError(f"{record_id}: kind must be {kind!r}")
            _require_page_ref(record)
    mappings = bundle.get("account_mappings", [])
    masks = {}
    for record in mappings:
        mask = record.get("account_mask")
        if not isinstance(mask, str) or not mask.strip():
            raise ValueError(f"{record['record_id']}: account_mask required")
        if mask in masks:
            raise ValueError(f"conflicting account mapping for {mask}")
        if not isinstance(record.get("holder"), str) or not record["holder"].strip():
            raise ValueError(f"{record['record_id']}: holder required")
        masks[mask] = record
    for record in bundle.get("claims", []):
        if not isinstance(record.get("order_id"), str) or not record["order_id"].strip():
            raise ValueError(f"{record['record_id']}: order_id required")
        if type(record.get("amount")) is not int or record["amount"] <= 0:
            raise ValueError(f"{record['record_id']}: amount must be a positive integer")
        if record.get("account_mask") not in masks:
            raise ValueError(f"{record['record_id']}: unknown account_mask {record.get('account_mask')}")
        if record.get("status") not in _CLAIM_STATUSES:
            raise ValueError(f"{record['record_id']}: invalid claim status")
        if not isinstance(record.get("snapshot"), dict):
            raise ValueError(f"{record['record_id']}: refund snapshot required")
    order_ids = [c["order_id"] for c in bundle.get("claims", [])]
    if len(order_ids) != len(set(order_ids)):
        raise ValueError("duplicate order_id across claims")
    orders = set(order_ids)
    codes = {}
    for record in bundle.get("refund_codes", []):
        code = record.get("code")
        order_id = record.get("order_id")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"{record['record_id']}: code required")
        if code in codes:
            raise ValueError(f"duplicate refund code {code}")
        if order_id not in orders:
            raise ValueError(f"{record['record_id']}: code references unknown order {order_id}")
        if record.get("exclusive") is not True:
            raise ValueError(f"{record['record_id']}: refund code must be exclusive to one order")
        codes[code] = record
    flows = bundle.get("bank_flows", [])
    flow_identity = {}
    for record in flows:
        txid = record.get("txid")
        if not isinstance(txid, str) or not txid.strip():
            raise ValueError(f"{record['record_id']}: txid required")
        if type(record.get("amount")) is not int or record["amount"] <= 0:
            raise ValueError(f"{record['record_id']}: amount must be a positive integer")
        if record.get("account_mask") not in masks:
            raise ValueError(f"{record['record_id']}: unknown account_mask {record.get('account_mask')}")
        if record.get("settled") is not True:
            raise ValueError(f"{record['record_id']}: only settled bank flows are accepted")
        code = record.get("order_code")
        if code is not None and code not in codes:
            raise ValueError(f"{record['record_id']}: order_code {code!r} has no refund code record")
        identity = (txid, record["account_mask"], record["amount"], code)
        if txid in flow_identity and flow_identity[txid] != identity:
            raise ValueError(f"bank flow identity conflict for repeated txid {txid}")
        flow_identity[txid] = identity
    for record in bundle.get("authorizations", []):
        if record.get("account_mask") not in masks:
            raise ValueError(f"{record['record_id']}: unknown account_mask")
        if not isinstance(record.get("transferable"), bool):
            raise ValueError(f"{record['record_id']}: transferable must be boolean")
        if record.get("protocol") not in _PROTOCOLS:
            raise ValueError(f"{record['record_id']}: unknown authorization protocol")
    for record in bundle.get("revocations", []):
        if not isinstance(record.get("txid"), str) or not record["txid"].strip():
            raise ValueError(f"{record['record_id']}: txid required")
    return True


def _record_pool(bundle):
    """All evidence records in one deterministic, content-addressable pool."""
    pool = []
    for section in _SECTION_KINDS:
        for record in bundle.get(section, []):
            pool.append(record)
    return sorted(pool, key=lambda item: item["record_id"])


def source_digest(bundle):
    """Fingerprint of the whole offline source set; stable under replay."""
    return digest(_record_pool(bundle))


# --------------------------------------------------------------------------- #
# Bipartite graph construction
# --------------------------------------------------------------------------- #
def build_graph(bundle, revoked_txids=None):
    """Build the deterministic receipt->claim bipartite graph.

    Bank flows are one-to-one: a txid repeated on several export pages is a
    single graph node and can never be counted twice.  Revoked flows and
    withdrawn claims are excluded with explicit reasons; same-account /
    same-batch records only produce candidate edges and are never treated as
    proven attribution.
    """
    validate_bundle(bundle)
    if revoked_txids is None:
        revoked_txids = {r["txid"] for r in bundle.get("revocations", [])}
    revoked_txids = set(revoked_txids)

    claims_by_id = OrderedDict((c["order_id"], c)
                               for c in sorted(bundle.get("claims", []), key=lambda c: c["order_id"]))
    codes = {r["code"]: r for r in bundle.get("refund_codes", [])}
    mappings = {r["account_mask"]: r for r in bundle.get("account_mappings", [])}

    # Repeated txid pages collapse to one node; page order is preserved as evidence.
    flows = OrderedDict()
    duplicate_pages = []
    for record in sorted(bundle.get("bank_flows", []), key=lambda r: (r["txid"], r["record_id"])):
        txid = record["txid"]
        if txid not in flows:
            flows[txid] = {
                "txid": txid, "amount": record["amount"],
                "account_mask": record["account_mask"],
                "order_code": record.get("order_code"),
                "record_ids": [], "pages": [],
            }
        flows[txid]["record_ids"].append(record["record_id"])
        flows[txid]["pages"].append({"record_id": record["record_id"], "page_ref": record["page_ref"]})
    for txid, node in flows.items():
        if len(node["record_ids"]) > 1:
            duplicate_pages.append({"txid": txid, "kept_record_id": node["record_ids"][0],
                                    "ignored_record_ids": node["record_ids"][1:],
                                    "page_count": len(node["record_ids"])})

    claim_nodes, excluded_claims = [], []
    for order_id, claim in claims_by_id.items():
        entry = {"order_id": order_id, "record_id": claim["record_id"],
                 "amount": claim["amount"], "account_mask": claim["account_mask"],
                 "status": claim["status"],
                 "snapshot": claim["snapshot"],
                 "snapshot_hash": digest(claim["snapshot"]),
                 "page_ref": claim["page_ref"]}
        if claim["status"] != "valid":
            excluded_claims.append({**entry, "reason": f"claim_{claim['status']}"})
        else:
            claim_nodes.append(entry)

    receipt_nodes, excluded_receipts, edges, rejections = [], [], [], []
    for txid, flow in flows.items():
        base = {"txid": txid, "amount": flow["amount"], "account_mask": flow["account_mask"],
                "order_code": flow["order_code"], "pages": flow["pages"],
                "record_ids": flow["record_ids"]}
        if txid in revoked_txids:
            excluded_receipts.append({**base, "reason": "bank_revoked"})
            continue
        receipt_nodes.append(base)
        compatible = [c for c in claim_nodes
                      if c["account_mask"] == flow["account_mask"] and c["amount"] == flow["amount"]]
        if flow["order_code"] is not None:
            code_record = codes[flow["order_code"]]
            target = next((c for c in compatible if c["order_id"] == code_record["order_id"]), None)
            if target is None:
                rejections.append({"txid": txid, "reason": "exclusive_code_without_matching_claim",
                                   "order_code": flow["order_code"]})
                continue
            edges.append(_edge(flow, target, "exclusive_code", code_record, mappings))
            continue
        if not compatible:
            rejections.append({"txid": txid, "reason": "no_compatible_claim_same_account_and_amount"})
            continue
        for claim in compatible:
            edges.append(_edge(flow, claim, "account_amount", None, mappings))

    edges.sort(key=lambda e: (e["txid"], e["order_id"]))
    graph = {
        "batch_id": bundle["batch_id"],
        "source_digest": source_digest(bundle),
        "receipts": sorted(receipt_nodes, key=lambda r: r["txid"]),
        "claims": claim_nodes,
        "edges": edges,
        "excluded_receipts": sorted(excluded_receipts, key=lambda r: r["txid"]),
        "excluded_claims": sorted(excluded_claims, key=lambda c: c["order_id"]),
        "edge_rejections": sorted(rejections, key=lambda r: r["txid"]),
        "duplicate_pages": sorted(duplicate_pages, key=lambda d: d["txid"]),
        "revoked_txids": sorted(revoked_txids),
        "aggregation_guard": {
            "account_level_aggregation": False,
            "rule": "one bank txid binds to at most one claim; same-account amounts are never summed",
        },
    }
    graph["graph_version"] = digest({
        "source_digest": graph["source_digest"],
        "edges": [(e["txid"], e["order_id"], e["basis"]) for e in edges],
        "receipts": [r["txid"] for r in graph["receipts"]],
        "claims": [c["order_id"] for c in claim_nodes],
        "revoked_txids": graph["revoked_txids"],
    })
    return graph


def _edge(flow, claim, basis, code_record, mappings):
    evidence = [{"role": "bank_flow", "record_id": rid, "page_ref": page["page_ref"]}
                for rid, page in zip(flow["record_ids"], flow["pages"])]
    evidence.append({"role": "refund_claim", "record_id": claim["record_id"],
                     "page_ref": claim["page_ref"]})
    if code_record is not None:
        evidence.append({"role": "refund_code", "record_id": code_record["record_id"],
                         "page_ref": code_record["page_ref"]})
    evidence.append({"role": "account_mapping", "record_id": mappings[flow["account_mask"]]["record_id"],
                     "page_ref": mappings[flow["account_mask"]]["page_ref"]})
    return {
        "edge_id": f"{flow['txid']}->{claim['order_id']}",
        "txid": flow["txid"], "order_id": claim["order_id"],
        "amount": flow["amount"], "account_mask": flow["account_mask"],
        "basis": basis, "candidate_only": True,
        "seals": sorted({item["page_ref"]["seal_digest"] for item in evidence}),
        "evidence": evidence,
    }


def adjacency(graph):
    adj = {r["txid"]: [] for r in graph["receipts"]}
    for edge in graph["edges"]:
        adj[edge["txid"]].append(edge["order_id"])
    for txid in adj:
        adj[txid].sort()
    return adj


# --------------------------------------------------------------------------- #
# Maximum bipartite matching with auditable augmenting paths
# --------------------------------------------------------------------------- #
def maximum_matching(graph, locked_edges=None):
    """Kuhn augmenting-path matching with a full replayable search log.

    Locked edges are immovable: an already-issued non-transferable bank
    write-off joins the graph as a locked edge whose endpoints cannot be
    reassigned, rather than as a reason to skip augmentation.
    """
    locked = sorted(tuple(edge) for edge in (locked_edges or []))
    edge_set = {(e["txid"], e["order_id"]) for e in graph["edges"]}
    for edge in locked:
        if edge not in edge_set:
            raise ValueError(f"locked edge {edge[0]}->{edge[1]} is not a legal edge of this graph")
    adj = adjacency(graph)
    match_receipt, match_claim = {}, {}
    log = []

    def lock(edge):
        txid, order_id = edge
        if txid in match_receipt or order_id in match_claim:
            raise ValueError(f"locked edges conflict at {txid}->{order_id}")
        match_receipt[txid] = order_id
        match_claim[order_id] = txid
        log.append({"attempt": len(log) + 1, "start": txid, "kind": "lock",
                    "explored": [{"receipt": txid, "claim": order_id, "locked": True}],
                    "augmenting_path": [txid, order_id],
                    "added_edges": [[txid, order_id]], "removed_edges": [],
                    "outcome": "locked", "size_after": len(match_receipt)})

    for edge in locked:
        lock(edge)

    for start in sorted(adj):
        if start in match_receipt:
            continue
        parent_claim, parent_receipt = {}, {start: None}
        explored, stack, free_claim = [], [start], None
        while stack:
            txid = stack.pop()
            for order_id in adj[txid]:
                if order_id in parent_claim:
                    continue
                parent_claim[order_id] = txid
                holder = match_claim.get(order_id)
                is_locked = holder is not None and (holder, order_id) in set(locked)
                explored.append({"receipt": txid, "claim": order_id,
                                 "matched_receipt": holder, "locked": is_locked})
                if is_locked:
                    continue
                if holder is None:
                    free_claim = order_id
                    break
                if holder not in parent_receipt:
                    parent_receipt[holder] = order_id
                    stack.append(holder)
            if free_claim is not None:
                break
        if free_claim is None:
            log.append({"attempt": len(log) + 1, "start": start, "kind": "augment",
                        "explored": explored, "augmenting_path": [],
                        "added_edges": [], "removed_edges": [],
                        "outcome": "no_augmenting_path", "size_after": len(match_receipt)})
            continue
        path_nodes, claim_cursor = [], free_claim
        while claim_cursor is not None:
            txid = parent_claim[claim_cursor]
            path_nodes.append((txid, claim_cursor))
            claim_cursor = parent_receipt[txid]
        path_nodes.reverse()
        added, removed, flat = [], [], []
        for txid, order_id in path_nodes:
            flat.extend((txid, order_id))
            added.append([txid, order_id])
            if order_id in match_claim and match_claim[order_id] != txid:
                removed.append([match_claim[order_id], order_id])
        for txid, order_id in path_nodes:
            match_claim[order_id] = txid
            match_receipt[txid] = order_id
        log.append({"attempt": len(log) + 1, "start": start, "kind": "augment",
                    "explored": explored,
                    "augmenting_path": flat,
                    "added_edges": sorted(added), "removed_edges": sorted(removed),
                    "outcome": "augmented", "size_after": len(match_receipt)})

    pairs = sorted(({"txid": txid, "order_id": order_id}
                    for txid, order_id in match_receipt.items()), key=lambda p: p["txid"])
    return {"pairs": pairs, "size": len(pairs), "locked_edges": [list(edge) for edge in locked],
            "log": log,
            "exchanges": [entry for entry in log if entry["removed_edges"]],
            "unmatched_receipts": sorted(set(adj) - set(match_receipt)),
            "unresolved_claims": sorted(set(c["order_id"] for c in graph["claims"])
                                        - set(match_claim))}


def min_barrier(graph, matching):
    """Minimum vertex cover (Kőnig): smallest node set blocking every edge.

    For a maximum matching this is a minimum barrier set proving that no
    settlement can simultaneously satisfy more claims than the matching size.
    Locked edges are preconditions outside the optimization: the cover is
    computed on the residual graph whose endpoints are not locked, and the
    locked edges are reported separately.
    """
    locked = {tuple(edge) for edge in matching.get("locked_edges", [])}
    locked_receipts = {txid for txid, _ in locked}
    locked_claims = {order_id for _, order_id in locked}
    full_adj = adjacency(graph)
    adj = {txid: [order_id for order_id in claims
                  if txid not in locked_receipts and order_id not in locked_claims]
           for txid, claims in full_adj.items()}
    residual_pairs = [p for p in matching["pairs"]
                      if (p["txid"], p["order_id"]) not in locked]
    match_claim = {p["order_id"]: p["txid"] for p in residual_pairs}
    residual_receipts = set(full_adj) - locked_receipts
    seed = sorted(residual_receipts - {p["txid"] for p in residual_pairs})
    reachable_receipts = set(seed)
    reachable_claims = set()
    stack = list(seed)
    while stack:
        txid = stack.pop()
        for order_id in adj.get(txid, []):
            if order_id not in reachable_claims:
                reachable_claims.add(order_id)
                holder = match_claim.get(order_id)
                if holder is not None and holder not in reachable_receipts:
                    reachable_receipts.add(holder)
                    stack.append(holder)
    cover_receipts = sorted(residual_receipts - reachable_receipts)
    cover_claims = sorted(reachable_claims)
    residual_edges = {e["edge_id"] for e in graph["edges"]
                      if e["txid"] not in locked_receipts and e["order_id"] not in locked_claims}
    blocked = sorted({f"{txid}->{order_id}" for txid in cover_receipts
                      for order_id in adj[txid]}
                     | {f"{txid}->{order_id}" for order_id in cover_claims
                        for txid in adj if order_id in adj[txid]})
    assert set(blocked) == residual_edges
    cover_size = len(cover_receipts) + len(cover_claims)
    return {
        "kind": "min_vertex_cover_konig",
        "receipts": cover_receipts, "claims": cover_claims,
        "size": cover_size,
        "proof": {
            "theorem": "Konig: alternating reach from unmatched receipts partitions all nodes; "
                       "(receipts outside Z) union (claims inside Z) is a minimum vertex cover",
            "unmatched_seed_receipts": seed,
            "alternating_reach": {"receipts": sorted(reachable_receipts),
                                  "claims": sorted(reachable_claims)},
            "blocks_all_residual_edges": True,
            "residual_cover_equals_residual_matching": cover_size == len(residual_pairs),
            "total_matching_size": matching["size"],
            "locked_edge_count": len(locked),
        },
        "locked_edges": sorted(list(edge) for edge in locked),
        "blocked_edge_ids": blocked,
    }


# --------------------------------------------------------------------------- #
# Certificates
# --------------------------------------------------------------------------- #
def _pair_evidence(graph, txid, order_id):
    edge = next(e for e in graph["edges"] if e["txid"] == txid and e["order_id"] == order_id)
    return {"txid": txid, "order_id": order_id, "amount": edge["amount"],
            "account_mask": edge["account_mask"], "edge_basis": edge["basis"],
            "candidate_only": False, "edge_id": edge["edge_id"],
            "seals": edge["seals"], "evidence": edge["evidence"]}


def _skeleton(graph, *, cert_id, cert_type, protocol, scope, issued_at, matching, barrier):
    return {
        "cert_id": cert_id,
        "cert_type": cert_type,
        "protocol": protocol,
        "scope": scope,
        "issued_at": issued_at,
        "batch_id": graph["batch_id"],
        "source_digest": graph["source_digest"],
        "graph_version": graph["graph_version"],
        "pairs": [_pair_evidence(graph, p["txid"], p["order_id"]) for p in matching["pairs"]],
        "matching_size": matching["size"],
        "unmatched_receipts": matching["unmatched_receipts"],
        "unresolved_claims": matching["unresolved_claims"],
        "augmenting_proof": {
            "log": matching["log"],
            "exchanges": matching["exchanges"],
            "locked_edges": matching["locked_edges"],
        },
        "barrier": barrier,
        "excluded_receipts": graph["excluded_receipts"],
        "excluded_claims": graph["excluded_claims"],
        "duplicate_pages": graph["duplicate_pages"],
        "aggregation_guard": graph["aggregation_guard"],
        "suspense": [],
        "resign_objects": [],
        "revocation_chain": [],
    }


def build_provisional_certificate(graph, *, cert_id, scope, issued_at, authorization_record_id=None):
    """The revocable internal provisional assignment (seed greedy, evidenced)."""
    seed_input_receipts = [{"txid": r["txid"], "account": r["account_mask"],
                            "amount": r["amount"], "order_code": r["order_code"]}
                           for r in graph["receipts"]]
    seed_input_claims = [{"order_id": c["order_id"], "account": c["account_mask"],
                          "amount": c["amount"]} for c in graph["claims"]]
    greedy = provisional_matches(seed_input_receipts, seed_input_claims)
    locked_like = [[m["txid"], m["order_id"]] for m in greedy["matches"]]
    matching = {
        "pairs": [{"txid": m["txid"], "order_id": m["order_id"]} for m in greedy["matches"]],
        "size": len(greedy["matches"]),
        "unmatched_receipts": greedy["unmatched_receipts"],
        "unresolved_claims": greedy["unpaid_orders"],
        "locked_edges": locked_like,
        "log": [{"attempt": 1, "kind": "greedy_provisional",
                 "rule": "first compatible claim in input order; revocable, not a bank write-off",
                 "pairs": greedy["matches"]}],
        "exchanges": [],
    }
    body = _skeleton(graph, cert_id=cert_id, cert_type="provisional",
                     protocol="revocable_provisional", scope=scope, issued_at=issued_at,
                     matching=matching, barrier=None)
    body["authorization_record_id"] = authorization_record_id
    body["revocable"] = True
    body["fingerprint_note"] = "internal provisional attribution; may be revoked and reassigned"
    return body


def build_revocable_settlement(graph, *, cert_id, scope, issued_at,
                               authorization_record_id, prior_cert=None):
    """Maximum-matching settlement under a transferable holder preauthorization."""
    matching = maximum_matching(graph)
    barrier = min_barrier(graph, matching)
    body = _skeleton(graph, cert_id=cert_id, cert_type="settlement_revocable",
                     protocol="revocable_provisional", scope=scope, issued_at=issued_at,
                     matching=matching, barrier=barrier)
    body["authorization_record_id"] = authorization_record_id
    body["resign_objects"] = []
    if prior_cert is not None:
        old_pairs = {(p["txid"], p["order_id"]) for p in prior_cert.get("pairs", [])}
        new_pairs = {(p["txid"], p["order_id"]) for p in matching["pairs"]}
        for txid, order_id in sorted(old_pairs - new_pairs):
            replacement = next((p for p in matching["pairs"] if p["txid"] == txid), None)
            body["revocation_chain"].append({
                "voided_cert_id": prior_cert["cert_id"],
                "revoked_pair": [txid, order_id],
                "action": "revoke_provisional_and_reassign",
                "new_pair": [replacement["txid"], replacement["order_id"]] if replacement else None,
                "customer_re_sign_required": False,
                "authorization_record_id": authorization_record_id,
            })
        body["voids_cert_id"] = prior_cert["cert_id"]
    body["suspense"] = [{"txid": txid, "reason": "no_legal_edge_after_global_matching",
                         "disposition": "merchant_internal_review",
                         "account_mask": next((r["account_mask"] for r in graph["receipts"]
                                               if r["txid"] == txid), None)}
                        for txid in matching["unmatched_receipts"]]
    return body


def build_locked_settlement(graph, locked_edges, *, cert_id, scope, issued_at,
                            authorization_record_id, writeoff_cert):
    """Settlement constrained by an already issued non-transferable bank voucher."""
    matching = maximum_matching(graph, locked_edges=locked_edges)
    barrier = min_barrier(graph, matching)
    body = _skeleton(graph, cert_id=cert_id, cert_type="settlement_locked",
                     protocol="non_transferable_bank_writeoff", scope=scope, issued_at=issued_at,
                     matching=matching, barrier=barrier)
    body["authorization_record_id"] = authorization_record_id
    body["bank_writeoff_cert_id"] = writeoff_cert["cert_id"]
    locked_claims = {edge[1] for edge in locked_edges}
    body["suspense"] = [{
        "txid": txid,
        "amount": next(r["amount"] for r in graph["receipts"] if r["txid"] == txid),
        "account_mask": next(r["account_mask"] for r in graph["receipts"] if r["txid"] == txid),
        "reason": "blocked_by_locked_non_transferable_writeoff",
        "disposition": "return_via_bank_original_route",
        "blocking_locked_edges": [list(edge) for edge in locked_edges],
    } for txid in matching["unmatched_receipts"]]
    body["resign_objects"] = [
        {"object": f"bank_flow:{txid}", "re_sign_required": True,
         "action": "holder_counter_sign_original_route_refund",
         "authorization_record_id": authorization_record_id}
        for txid in matching["unmatched_receipts"]
    ] + [
        {"object": f"claim:{order_id}", "re_sign_required": True,
         "action": "new_bank_attribution_evidence_or_customer_re_sign",
         "authorization_record_id": authorization_record_id}
        for order_id in matching["unresolved_claims"] if order_id not in locked_claims
    ]
    body["locked_attribution_note"] = (
        "locked edges come from an issued bank write-off and are not reassigned; "
        "the augmenting search runs around them and never silently swaps them")
    return body


def build_bank_writeoff_certificate(graph, pairs, *, cert_id, scope, issued_at,
                                   authorization_record_id):
    """The externally issued, non-transferable bank write-off voucher (locked)."""
    edge_set = {(e["txid"], e["order_id"]) for e in graph["edges"]}
    locked_edges = [[p[0], p[1]] if isinstance(p, (tuple, list)) else
                    [p["txid"], p["order_id"]] for p in pairs]
    for edge in locked_edges:
        if tuple(edge) not in edge_set:
            raise ValueError(f"write-off pair {edge[0]}->{edge[1]} is not a legal edge")
    matching = {"pairs": [{"txid": t, "order_id": o} for t, o in locked_edges],
                "size": len(locked_edges), "locked_edges": locked_edges,
                "log": [{"attempt": 1, "kind": "external_bank_writeoff",
                         "rule": "pair is sealed externally and becomes an immovable locked edge",
                         "pairs": locked_edges}],
                "exchanges": [],
                "unmatched_receipts": sorted(r["txid"] for r in graph["receipts"]
                                             if r["txid"] not in {t for t, _ in locked_edges}),
                "unresolved_claims": sorted(c["order_id"] for c in graph["claims"]
                                            if c["order_id"] not in {o for _, o in locked_edges})}
    body = _skeleton(graph, cert_id=cert_id, cert_type="bank_writeoff",
                     protocol="non_transferable_bank_writeoff", scope=scope, issued_at=issued_at,
                     matching=matching, barrier=None)
    body["authorization_record_id"] = authorization_record_id
    body["transferable"] = False
    return body


def certificate_fingerprint(body):
    """Hash of certificate content excluding the envelope cert_id label."""
    content = {key: value for key, value in body.items() if key != "cert_id"}
    return digest(content)


def render_certificate(body):
    """Canonical serialized certificate with its content fingerprint."""
    rendered = {**body, "sha256": certificate_fingerprint(body)}
    rendered["canonical"] = canonical({k: v for k, v in rendered.items() if k != "canonical"})
    return rendered
