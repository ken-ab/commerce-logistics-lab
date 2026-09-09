"""Validate a submitted itinerary directly from schedules; never call the planner."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math

from apparel_fulfillment.data import digest


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp is not text")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp lacks timezone")
    return result.astimezone(timezone.utc)


def audit_route(checked, shipping, report, world, corridor, *, events=(), now=None):
    now = now or datetime.now(timezone.utc)
    errors = []
    if checked.get("status") != "ready":
        return {"assessed": True, "passed": False, "violations": ["order_not_ready"]}
    if not checked.get("needs_shipping"):
        ok = report.get("status") == "not_required" and report.get("segments") == [] and report.get("total_cost_cents") == 0
        return {"assessed": True, "passed": ok, "violations": [] if ok else ["shipping_not_requested"]}
    if report.get("status") != "planned":
        return {"assessed": False, "passed": False, "violations": ["non_plan_requires_case_oracle"]}
    try:
        if report.get("corridor_digest") != digest(corridor) or report.get("corridor_id") != corridor["dataset_id"]:
            errors.append("corridor_source_changed")
        if report.get("shipping_constraints") != shipping or report.get("currency") != "USD":
            errors.append("requested_constraints_changed")
        if checked["warehouse"] != corridor["origin"]:
            errors.append("wrong_single_warehouse")
        weight = sum(world["variants"][sku]["weight_grams_per_catalog_unit"] * qty
                     for sku, qty in checked["quantities_catalog_units"].items())
        if type(report.get("weight_grams")) is not int or report["weight_grams"] != weight:
            errors.append("wrong_consignment_weight")
        unique_events = {}
        for event in events:
            if _time(event["published_at"]) <= now:
                previous = unique_events.get(event["event_id"])
                if previous is not None and previous != event:
                    errors.append("conflicting_event_id")
                unique_events[event["event_id"]] = event
        by_id = {leg["id"]: leg for leg in corridor["legs"]}
        segments = report["segments"]
        if not isinstance(segments, list) or not segments:
            raise ValueError("No route segments")
        node, seen = checked["warehouse"], {checked["warehouse"]}
        current = max(_time(shipping["ready_at"]), now)
        total = 0
        for index, segment in enumerate(segments):
            leg = by_id[segment["leg_id"]]
            if leg["origin"] != node or leg["destination"] in seen:
                errors.append("disconnected_or_cyclic_route")
            for key in ("origin", "destination", "mode"):
                if segment.get(key) != leg[key]:
                    errors.append("wrong_segment_" + key)
            nominal, departure, arrival = (_time(segment[key]) for key in ("nominal_departure", "departure_at", "arrival_at"))
            anchor = _time(corridor["anchor_at"]) + timedelta(minutes=leg["offset_minutes"])
            seconds = (nominal - anchor).total_seconds()
            if seconds < 0 or seconds % (leg["period_minutes"] * 60) != 0:
                errors.append("nonexistent_departure")
            expected_id = leg["id"] + "@" + nominal.isoformat().replace("+00:00", "Z")
            if segment.get("service_id") != expected_id:
                errors.append("wrong_service_id")
            affecting = [event for event in unique_events.values() if event["leg_id"] == leg["id"] and _time(event["nominal_departure"]) == nominal]
            if any(event["kind"] == "cancel" for event in affecting):
                errors.append("cancelled_departure")
            delay = sum(event.get("delay_minutes", 0) for event in affecting if event["kind"] == "delay")
            if departure != nominal + timedelta(minutes=delay):
                errors.append("departure_does_not_include_events")
            if sorted(segment.get("event_ids", [])) != sorted(event["event_id"] for event in affecting):
                errors.append("segment_event_evidence_incomplete")
            if arrival != departure + timedelta(minutes=leg["duration_minutes"]):
                errors.append("wrong_segment_arrival")
            transfer = corridor["transfer_minutes"].get(node, 0) if index else 0
            if departure < current + timedelta(minutes=transfer):
                errors.append("missed_departure_or_transfer")
            if segment.get("transfer_minutes") != transfer:
                errors.append("wrong_transfer_allowance")
            # Wait was measured when the proposal was made; a later confirmation
            # may reduce first-leg waiting without changing the actual itinerary.
            wait_origin = max(_time(shipping["ready_at"]), _time(report["planning_at"])) if index == 0 else current
            wait = segment.get("wait_minutes")
            if type(wait) not in (float, int) or not math.isfinite(wait) or abs(wait - (departure - wait_origin).total_seconds() / 60) > 1e-6:
                errors.append("wrong_wait_time")
            if weight > leg["capacity_grams"]:
                errors.append("capacity_exceeded")
            cost = int((Decimal(leg["fixed_cents"]) + Decimal(leg["per_kg_cents"]) * Decimal(weight) / 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if type(segment.get("cost_cents")) is not int or segment["cost_cents"] != cost:
                errors.append("wrong_segment_cost")
            if segment.get("evidence_id") != "sim-leg:" + leg["id"]:
                errors.append("wrong_segment_source")
            total += cost; current = arrival; node = leg["destination"]; seen.add(node)
        if node != shipping["destination"] or node != corridor["destination"]:
            errors.append("wrong_destination")
        if _time(report["arrival_at"]) != current:
            errors.append("wrong_final_arrival")
        if current > _time(shipping["deadline_at"]):
            errors.append("deadline_violated")
        if total > shipping["budget_cents"]:
            errors.append("budget_violated")
        if type(report.get("total_cost_cents")) is not int or report["total_cost_cents"] != total:
            errors.append("wrong_total_cost")
    except (ValueError, KeyError, TypeError, OverflowError):
        errors.append("malformed_route_or_sources")
    return {"assessed": True, "passed": not errors, "violations": sorted(set(errors)),
            "scope": "Independent itinerary feasibility and arithmetic, not a proof of cost optimality or real carrier availability"}
