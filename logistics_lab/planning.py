"""Small exhaustive planner over synthetic routes, with a separate report audit.

Single-warehouse fulfilment only. No stock mutation, bookings, external API calls,
tax calculation, or learned policy. Money is computed with Decimal.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_world() -> dict[str, Any]:
    return json.loads((ROOT / "data/world.json").read_text(encoding="utf-8"))


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def validate_order(order: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    """Normalize duplicate SKU lines before checking stock or minimum quantities."""
    if not isinstance(order, dict):
        return {"status": "needs_clarification", "reasons": ["Order must be an object."]}
    missing = [k for k in ("destination", "items", "deadline_days", "budget_usd") if not order.get(k)]
    if missing:
        return {"status": "needs_clarification", "reasons": ["Missing: " + ", ".join(missing)]}
    if not isinstance(order["destination"], str) or not isinstance(order["items"], list):
        return {"status": "needs_clarification", "reasons": ["Destination must be text and items a list."]}
    if not all(_number(order[k]) for k in ("deadline_days", "budget_usd")):
        return {"status": "needs_clarification", "reasons": ["Deadline and budget must be positive finite numbers."]}
    quantities: dict[str, int] = defaultdict(int)
    for item in order["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("sku"), str) or item["sku"] not in world["products"]:
            return {"status": "needs_clarification", "reasons": ["Unknown SKU; specify an exact catalog variant."]}
        if type(item.get("quantity")) is not int or item["quantity"] <= 0:
            return {"status": "needs_clarification", "reasons": ["Quantities must be positive integers."]}
        quantities[item["sku"]] += item["quantity"]
    blocked = order.get("blocked_legs", [])
    leg_ids = {leg["id"] for leg in world["legs"]}
    if not isinstance(blocked, list) or any(not isinstance(x, str) or x not in leg_ids for x in blocked):
        return {"status": "needs_clarification", "reasons": ["Unknown blocked transport leg."]}
    for sku, quantity in quantities.items():
        product = world["products"][sku]
        if order["destination"] not in product["allowed_destinations"]:
            return {"status": "rejected", "reasons": [f"Synthetic brand policy does not permit {sku} at this destination."]}
        if quantity < product["minimum_units"]:
            return {"status": "rejected", "reasons": [f"{sku} does not meet the synthetic wholesale minimum."]}
    return {"status": "valid", "quantities": dict(quantities)}


def _weight(quantities: dict[str, int], world: dict[str, Any]) -> Decimal:
    return sum((Decimal(str(world["products"][sku]["weight_kg"])) * qty for sku, qty in quantities.items()), Decimal(0))


def _cost(legs: list[dict[str, Any]], weight: Decimal) -> Decimal:
    return sum((Decimal(str(x["fixed_usd"])) + Decimal(str(x["per_kg_usd"])) * weight for x in legs), Decimal(0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _paths(origin: str, destination: str, world: dict[str, Any], visited: frozenset[str] = frozenset()) -> list[list[dict[str, Any]]]:
    if origin == destination:
        return [[]]
    paths = []
    for leg in world["legs"]:
        if leg["origin"] == origin and leg["destination"] not in visited | {origin}:
            for tail in _paths(leg["destination"], destination, world, visited | {origin}):
                paths.append([leg] + tail)
    return paths


def plan_fulfilment(order: dict[str, Any], policy: str = "constrained", world: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return minimum-cost feasible route, or an explicit unsatisfied outcome.

    The diagnostic cheapest_unchecked baseline intentionally ignores deadline,
    budget, capacity and blocked legs. It is not an LLM or a fair strong baseline.
    """
    if policy not in ("constrained", "cheapest_unchecked"):
        raise ValueError("Unknown policy")
    world = load_world() if world is None else world
    checked = validate_order(order, world)
    if checked["status"] != "valid":
        return {**checked, "dataset_id": world["dataset_id"]}
    quantities = checked["quantities"]
    weight = _weight(quantities, world)
    candidates = []
    for origin, stock in world["warehouses"].items():
        if any(stock.get(sku, 0) < qty for sku, qty in quantities.items()):
            continue
        for legs in _paths(origin, order["destination"], world):
            cost = _cost(legs, weight)
            days = sum(leg["days"] for leg in legs)
            if policy == "constrained" and (
                cost > Decimal(str(order["budget_usd"])) or days > order["deadline_days"]
                or any(weight > Decimal(str(x["capacity_kg"])) or x["id"] in order.get("blocked_legs", []) for x in legs)
            ):
                continue
            candidates.append({
                "status": "planned", "dataset_id": world["dataset_id"], "policy": policy,
                "warehouse": origin, "destination": order["destination"], "quantities": quantities,
                "weight_kg": float(weight), "total_cost_usd": float(cost), "transit_days": days,
                "leg_ids": [x["id"] for x in legs], "modes": [x["mode"] for x in legs],
                "evidence_ids": [f"product:{s}" for s in quantities] + [f"stock:{origin}:{s}" for s in quantities] + [f"leg:{x['id']}" for x in legs],
            })
    if not candidates:
        return {"status": "infeasible", "dataset_id": world["dataset_id"], "reasons": ["No single warehouse and route satisfy all specified constraints."]}
    return min(candidates, key=lambda x: (x["total_cost_usd"], x["transit_days"], x["warehouse"], x["leg_ids"]))


def audit_report(order: dict[str, Any], report: dict[str, Any], world: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompute structured claims from source records, without invoking planner.

    Non-plan outcomes are not awarded a perfect score: their feasibility/rejection
    claims require a separate case oracle. Free-text Markdown semantics are not
    evaluated here. The demo renders Markdown from the audited structure.
    """
    world = load_world() if world is None else world
    if not isinstance(report, dict) or report.get("status") != "planned":
        return {"assessed": False, "passed": False, "violations": ["No positive plan to audit; validate the outcome against a case oracle."]}
    violations = []
    checked = validate_order(order, world)
    if checked["status"] != "valid":
        return {"assessed": True, "passed": False, "violations": ["Order is not valid for planning."]}
    quantities = checked["quantities"]
    origin = report.get("warehouse")
    if not isinstance(origin, str) or origin not in world["warehouses"]:
        return {"assessed": True, "passed": False, "violations": ["Unknown warehouse."]}
    if report.get("quantities") != quantities:
        violations.append("Reported SKU quantities differ from the order.")
    if any(world["warehouses"][origin].get(sku, 0) < qty for sku, qty in quantities.items()):
        violations.append("Insufficient SKU stock at selected warehouse.")
    ids = report.get("leg_ids")
    by_id = {x["id"]: x for x in world["legs"]}
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or x not in by_id for x in ids):
        return {"assessed": True, "passed": False, "violations": violations + ["Missing or unknown transport legs."]}
    legs = [by_id[x] for x in ids]
    node, seen = origin, {origin}
    for leg in legs:
        if leg["origin"] != node or leg["destination"] in seen:
            violations.append("Disconnected or cyclic transport route.")
        node = leg["destination"]
        seen.add(node)
    if node != order["destination"] or report.get("destination") != order["destination"]:
        violations.append("Route does not end at the requested destination.")
    weight = _weight(quantities, world)
    cost = _cost(legs, weight)
    days = sum(x["days"] for x in legs)
    for key, actual in (("weight_kg", float(weight)), ("total_cost_usd", float(cost)), ("transit_days", days)):
        claimed = report.get(key)
        if type(claimed) not in (int, float) or not math.isfinite(claimed) or abs(claimed - actual) > 0.00001:
            violations.append(f"{key} does not match the source records.")
    if days > order["deadline_days"]:
        violations.append("Delivery deadline violated.")
    if cost > Decimal(str(order["budget_usd"])):
        violations.append("Shipping budget violated.")
    if any(weight > Decimal(str(x["capacity_kg"])) for x in legs):
        violations.append("Transport capacity violated.")
    if any(x["id"] in order.get("blocked_legs", []) for x in legs):
        violations.append("A selected transport leg is blocked.")
    required = {f"product:{s}" for s in quantities} | {f"stock:{origin}:{s}" for s in quantities} | {f"leg:{x}" for x in ids}
    evidence = report.get("evidence_ids", [])
    if not isinstance(evidence, list) or any(not isinstance(x, str) for x in evidence) or set(evidence) != required:
        violations.append("Evidence IDs are missing or do not match the actual source records.")
    if report.get("dataset_id") != world["dataset_id"]:
        violations.append("Dataset version mismatch.")
    if report.get("modes") != [x["mode"] for x in legs]:
        violations.append("Transport modes do not match the source records.")
    return {"assessed": True, "passed": not violations, "violations": violations,
            "recomputed": {"weight_kg": float(weight), "total_cost_usd": float(cost), "transit_days": days}}


def review_and_replan(order: dict[str, Any], world: dict[str, Any] | None = None) -> dict[str, Any]:
    """Diagnostic demonstration of one bounded rerun, not LLM reflection."""
    first = plan_fulfilment(order, "cheapest_unchecked", world)
    review = audit_report(order, first, world)
    rerun = first["status"] == "planned" and not review["passed"]
    final = plan_fulfilment(order, "constrained", world) if rerun else first
    return {"initial": first, "initial_audit": review, "reruns": int(rerun), "final": final, "final_audit": audit_report(order, final, world)}
