"""Deterministic apparel checks. Models cannot authorize substitutions here."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from apparel_fulfillment.data import digest

ATTRIBUTES = ("requested_sku", "style_id", "brand", "category", "color", "size", "audience")


class OrderError(ValueError):
    pass


def normalize_request(request: dict) -> dict:
    if not isinstance(request, dict) or not isinstance(request.get("lines"), list) or not 1 <= len(request["lines"]) <= 20:
        raise OrderError("Provide 1-20 apparel order lines")
    if set(request) - {"lines", "sales_region", "wholesale", "needs_shipping", "shipping"}:
        raise OrderError("Unexpected order fields")
    result = deepcopy(request)
    for name in ("wholesale", "needs_shipping"):
        if type(result.get(name)) is not bool:
            raise OrderError(name + " must be explicitly true or false")
    if not isinstance(result.get("sales_region"), str) or len(result["sales_region"]) != 2:
        raise OrderError("Specify the two-letter sales region")
    result["sales_region"] = result["sales_region"].upper()
    seen = set()
    for line in result["lines"]:
        if not isinstance(line, dict) or set(line) - {*ATTRIBUTES, "line_id", "quantity", "unit"}:
            raise OrderError("Unexpected apparel line fields")
        ident = line.get("line_id")
        if not isinstance(ident, str) or not 1 <= len(ident) <= 40 or ident in seen:
            raise OrderError("Each line needs a unique nonempty line_id")
        seen.add(ident)
        if type(line.get("quantity")) is not int or not 1 <= line["quantity"] <= 10000:
            raise OrderError("Quantity must be a positive integer at most 10000")
        if line.get("unit") not in {"piece", "catalog_unit", None}:
            raise OrderError("Unit must be piece or catalog_unit, or left unresolved")
        for key in ATTRIBUTES:
            value = line.get(key)
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 200):
                raise OrderError("Attributes must be nonempty text or null")
    return result


def differences(line: dict, variant: dict) -> list[dict]:
    result = []
    for key in ATTRIBUTES:
        desired = line.get(key)
        actual = variant.get("sku" if key == "requested_sku" else key)
        if desired is not None and (actual is None or str(actual).casefold() != desired.casefold()):
            result.append({"field": key, "requested": desired, "candidate": actual})
    return result


def substitution(request: dict, line: dict, variant: dict) -> dict:
    change = {"request_digest": digest(normalize_request(request)), "line_id": line["line_id"],
              "sku": variant["sku"], "variant_digest": digest(variant), "differences": differences(line, variant)}
    return {**change, "approval_id": "SUB-" + digest(change)[:24]}


def check_order(request: dict, selections: list[dict], world: dict, *, approved_ids: frozenset[str] = frozenset()) -> dict:
    request = normalize_request(request)
    if not isinstance(selections, list) or any(not isinstance(s, dict) or set(s) != {"line_id", "sku"} for s in selections):
        raise OrderError("Selections must contain only line_id and sku")
    lines = {line["line_id"]: line for line in request["lines"]}
    if any(not isinstance(s["line_id"], str) or s["line_id"] not in lines or not isinstance(s["sku"], str) for s in selections):
        raise OrderError("Unknown line or invalid SKU")
    if len({s["line_id"] for s in selections}) != len(selections):
        raise OrderError("Only one selected SKU per line is supported")
    selected = {s["line_id"]: s["sku"] for s in selections}
    issues, checks, approvals = [], [], []
    quantities = defaultdict(int)
    pieces = defaultdict(int)
    for ident, line in lines.items():
        sku = selected.get(ident)
        variant = world["variants"].get(sku)
        if not variant:
            issues.append({"line_id": ident, "code": "select_known_variant", "severity": "clarify"})
            continue
        missing = [key for key in ("size", "color", "brand", "category") if not variant.get(key) or not variant.get("provenance", {}).get(key)]
        if missing:
            issues.append({"line_id": ident, "code": "missing_variant_evidence", "fields": missing, "severity": "clarify"})
        if not line.get("requested_sku") and not line.get("size"):
            issues.append({"line_id": ident, "code": "size_unspecified", "severity": "clarify"})
        change = substitution(request, line, variant)
        if change["differences"]:
            approvals.append(change)
            if change["approval_id"] not in approved_ids:
                issues.append({"line_id": ident, "code": "substitution_requires_confirmation", "approval_id": change["approval_id"], "severity": "clarify"})
        count = variant.get("pieces_per_catalog_unit")
        if type(count) is not int or count < 1 or not variant.get("provenance", {}).get("pieces_per_catalog_unit"):
            issues.append({"line_id": ident, "code": "unit_conversion_unverified", "severity": "clarify"})
            continue
        if line.get("unit") is None:
            issues.append({"line_id": ident, "code": "quantity_unit_unspecified", "severity": "clarify"})
            continue
        if line["unit"] == "piece" and line["quantity"] % count:
            issues.append({"line_id": ident, "code": "cannot_split_catalog_pack", "pieces_per_catalog_unit": count, "severity": "clarify"})
            continue
        units = line["quantity"] // count if line["unit"] == "piece" else line["quantity"]
        quantities[sku] += units
        pieces[sku] += units * count
        checks.append({"line_id": ident, "sku": sku, "catalog_units": units, "pieces": units * count,
                       "variant": {key: variant.get(key) for key in ("title", "style_id", "brand", "category", "color", "size", "audience")},
                       "evidence_ids": sorted({item["evidence_id"] for item in variant["provenance"].values()}),
                       "unit_basis": variant["provenance"]["pieces_per_catalog_unit"]})
    snapshots = {}
    for sku, quantity in quantities.items():
        variant = world["variants"][sku]
        rule = world["brand_rules"].get(variant.get("brand"))
        stock = world["stock"].get(sku)
        if not rule or not stock:
            issues.append({"sku": sku, "code": "missing_rule_or_inventory", "severity": "clarify"})
            continue
        snapshots[sku] = {"variant_version": variant["version"], "stock_version": stock["version"],
                          "rule_version": rule["version"], "available_catalog_units": stock["available_catalog_units"],
                          "evidence_ids": [rule["evidence_id"], stock["evidence_id"]]}
        if request["sales_region"] not in rule["allowed_sales_regions"]:
            issues.append({"sku": sku, "code": "sales_region_not_allowed", "region": request["sales_region"], "severity": "unsatisfied"})
        if request["wholesale"] and pieces[sku] < rule["wholesale_minimum_pieces_per_sku"]:
            issues.append({"sku": sku, "code": "wholesale_minimum_not_met", "minimum_pieces": rule["wholesale_minimum_pieces_per_sku"], "severity": "unsatisfied"})
        if stock["available_catalog_units"] < quantity:
            issues.append({"sku": sku, "code": "insufficient_stock", "requested_catalog_units": quantity,
                           "available_catalog_units": stock["available_catalog_units"], "severity": "unsatisfied"})
    status = "unfulfillable" if any(i["severity"] == "unsatisfied" for i in issues) else "needs_clarification" if issues else "ready"
    return {"status": status, "request_digest": digest(request), "dataset_id": world["dataset_id"],
            "warehouse": world["warehouse"], "issues": issues, "lines": checks,
            "quantities_catalog_units": dict(quantities), "quantities_pieces": dict(pieces),
            "source_versions": snapshots, "substitution_proposals": approvals,
            "needs_shipping": request["needs_shipping"],
            "notice": "Order verification in the simulated merchant environment; no inventory reserved and no order placed."}


def alternatives(request: dict, line_id: str, world: dict, *, limit: int = 8) -> list[dict]:
    request = normalize_request(request)
    line = next((line for line in request["lines"] if line["line_id"] == line_id), None)
    if not line:
        raise OrderError("Unknown line_id")
    results = []
    for variant in world["variants"].values():
        if line.get("category") and line["category"].casefold() != variant.get("category", "").casefold():
            continue
        one = {**request, "lines": [line]}
        checked = check_order(one, [{"line_id": line_id, "sku": variant["sku"]}], world)
        if any(issue["code"] != "substitution_requires_confirmation" for issue in checked["issues"]):
            continue
        change = substitution(request, line, variant)
        results.append({"sku": variant["sku"], "title": variant["title"], "style_id": variant["style_id"],
                        "differences": change["differences"], "requires_confirmation": bool(change["differences"]),
                        "approval_id": change["approval_id"], "quantity_conversion": checked["lines"][0],
                        "inventory": checked["source_versions"][variant["sku"]]})
    return sorted(results, key=lambda item: (len(item["differences"]), item["sku"]))[:max(1, min(limit, 50))]
