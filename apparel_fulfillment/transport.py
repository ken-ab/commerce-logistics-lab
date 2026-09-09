"""Scheduled single-warehouse planning with explicit event revisions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import math

from apparel_fulfillment.data import ROOT, digest
from apparel_fulfillment.orders import OrderError
from logistics_lab.planning import _paths


def load_corridor():
    return json.loads((ROOT / "data/apparel_corridor_v1.json").read_text(encoding="utf-8"))


def instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise OrderError("Provide an ISO timestamp including its UTC offset")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OrderError("Invalid ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise OrderError("A timezone offset is required")
    return result.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def service_id(leg_id: str, nominal: datetime) -> str:
    return leg_id + "@" + iso(nominal)


def shipping_constraints(shipping, corridor, now: datetime) -> dict:
    if not isinstance(shipping, dict) or set(shipping) != {"destination", "ready_at", "deadline_at", "budget_cents"}:
        raise OrderError("Shipping needs destination, ready_at, deadline_at and budget_cents")
    if shipping["destination"] != corridor["destination"]:
        raise OrderError("This version supports the CN-SZ to DE-DC corridor only")
    if type(shipping["budget_cents"]) is not int or not 0 < shipping["budget_cents"] <= 100_000_000:
        raise OrderError("Shipping budget must be a positive integer number of USD cents")
    ready, deadline = instant(shipping["ready_at"]), instant(shipping["deadline_at"])
    if deadline <= ready or deadline <= now:
        raise OrderError("Delivery deadline must be after goods-ready and current time")
    if deadline - max(ready, now) > timedelta(days=corridor["maximum_horizon_days"]):
        raise OrderError("Deadline exceeds the registered research planning horizon")
    return {**shipping, "ready": ready, "deadline": deadline, "earliest": max(ready, now)}


def validate_event(event: dict, corridor: dict) -> dict:
    if not isinstance(event, dict) or set(event) - {"event_id", "kind", "leg_id", "nominal_departure", "delay_minutes", "published_at"}:
        raise OrderError("Unexpected transport event fields")
    if not isinstance(event.get("event_id"), str) or not 1 <= len(event["event_id"]) <= 100:
        raise OrderError("Event needs a stable event_id")
    leg = next((leg for leg in corridor["legs"] if leg["id"] == event.get("leg_id")), None)
    if not leg or event.get("kind") not in {"cancel", "delay"}:
        raise OrderError("Unknown transport leg or event kind")
    nominal = instant(event.get("nominal_departure"))
    anchor = instant(corridor["anchor_at"]) + timedelta(minutes=leg["offset_minutes"])
    seconds = (nominal - anchor).total_seconds()
    if seconds < 0 or seconds % (60 * leg["period_minutes"]) != 0:
        raise OrderError("The event does not identify a scheduled departure")
    delay = event.get("delay_minutes", 0)
    if type(delay) is not int or (event["kind"] == "delay" and not 1 <= delay <= 10080) or (event["kind"] == "cancel" and delay != 0):
        raise OrderError("Invalid event delay")
    published = instant(event.get("published_at"))
    return {"event_id": event["event_id"], "kind": event["kind"], "leg_id": leg["id"],
            "nominal_departure": iso(nominal), "delay_minutes": delay, "published_at": iso(published)}


def known_events(events: list[dict], corridor: dict, now: datetime) -> list[dict]:
    unique = {}
    for item in events:
        event = validate_event(item, corridor)
        if event["event_id"] in unique and unique[event["event_id"]] != event:
            raise OrderError("Conflicting duplicate event_id")
        if instant(event["published_at"]) <= now:
            unique[event["event_id"]] = event
    return sorted(unique.values(), key=lambda event: event["event_id"])


def departures(leg, earliest, latest, corridor, events):
    anchor = instant(corridor["anchor_at"]) + timedelta(minutes=leg["offset_minutes"])
    period = timedelta(minutes=leg["period_minutes"])
    # A previously scheduled but delayed departure can still be caught.
    max_delay = sum(e["delay_minutes"] for e in events if e["leg_id"] == leg["id"])
    first = max(0, math.ceil((earliest - timedelta(minutes=max_delay) - anchor) / period))
    count = max(0, math.floor((latest - anchor) / period) - first + 1)
    for index in range(first, first + count):
        nominal = anchor + index * period
        affected = [e for e in events if e["leg_id"] == leg["id"] and instant(e["nominal_departure"]) == nominal]
        if any(e["kind"] == "cancel" for e in affected):
            continue
        actual = nominal + timedelta(minutes=sum(e["delay_minutes"] for e in affected))
        if earliest <= actual <= latest:
            yield nominal, actual, [e["event_id"] for e in affected]


def route_candidates(checked, constraints, world, corridor, events, horizon):
    weight = 0
    for sku, units in checked["quantities_catalog_units"].items():
        grams = world["variants"][sku].get("weight_grams_per_catalog_unit")
        if type(grams) is not int or grams <= 0 or not world["variants"][sku]["provenance"].get("weight_grams_per_catalog_unit"):
            raise OrderError("Verified or explicitly simulated package weight is required")
        weight += grams * units
    results = []
    for legs in _paths(corridor["origin"], corridor["destination"], corridor):
        current = constraints["earliest"]
        segments, total = [], 0
        for index, leg in enumerate(legs):
            if weight > leg["capacity_grams"]:
                break
            transfer = corridor["transfer_minutes"].get(leg["origin"], 0) if index else 0
            available = current + timedelta(minutes=transfer)
            options = list(departures(leg, available, horizon, corridor, events))
            if not options:
                break
            nominal, departure, evidence = min(options, key=lambda item: (item[1], item[0]))
            arrival = departure + timedelta(minutes=leg["duration_minutes"])
            if arrival > horizon:
                break
            cost = int((Decimal(leg["fixed_cents"]) + Decimal(leg["per_kg_cents"]) * weight / 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            segments.append({"leg_id": leg["id"], "service_id": service_id(leg["id"], nominal),
                             "origin": leg["origin"], "destination": leg["destination"], "mode": leg["mode"],
                             "nominal_departure": iso(nominal), "departure_at": iso(departure), "arrival_at": iso(arrival),
                             "transfer_minutes": transfer, "wait_minutes": (departure - current).total_seconds() / 60,
                             "cost_cents": cost, "event_ids": evidence, "evidence_id": "sim-leg:" + leg["id"]})
            total += cost; current = arrival
        else:
            results.append({"segments": segments, "arrival_at": iso(current), "total_cost_cents": total, "weight_grams": weight})
    return results


def plan_transport(checked: dict, shipping: dict | None, world: dict, *, corridor=None, events=None, now=None) -> dict:
    if checked.get("status") != "ready":
        return {"status": "order_not_ready", "order_status": checked.get("status")}
    if not checked["needs_shipping"]:
        return {"status": "not_required", "segments": [], "total_cost_cents": 0}
    corridor = corridor or load_corridor()
    now = now or datetime.now(timezone.utc)
    if checked["warehouse"] != corridor["origin"]:
        raise OrderError("The selected single warehouse does not match this corridor")
    constraints = shipping_constraints(shipping, corridor, now)
    events = known_events(events or [], corridor, now)
    options = route_candidates(checked, constraints, world, corridor, events,
                               max(constraints["deadline"], constraints["earliest"] + timedelta(days=corridor["maximum_horizon_days"])))
    feasible = [option for option in options if option["total_cost_cents"] <= shipping["budget_cents"] and instant(option["arrival_at"]) <= constraints["deadline"]]
    common = {"corridor_id": corridor["dataset_id"], "corridor_digest": digest(corridor), "planning_at": iso(now),
              "events_digest": digest(events), "observed_event_ids": [event["event_id"] for event in events],
              "shipping_constraints": shipping, "currency": "USD", "provenance": corridor["provenance"]}
    if feasible:
        best = min(feasible, key=lambda option: (option["total_cost_cents"], option["arrival_at"], [s["service_id"] for s in option["segments"]]))
        return {**common, **best, "status": "planned"}
    adjustments = []
    for option in sorted(options, key=lambda option: (option["total_cost_cents"], option["arrival_at"])):
        change = {"requires_user_choice": True, "example_route": [s["service_id"] for s in option["segments"]]}
        if option["total_cost_cents"] > shipping["budget_cents"]:
            change["minimum_shipping_budget_cents"] = option["total_cost_cents"]
        if instant(option["arrival_at"]) > constraints["deadline"]:
            change["earliest_delivery_deadline_at"] = option["arrival_at"]
        adjustments.append(change)
    return {**common, "status": "infeasible", "adjustment_options": adjustments,
            "reason": "No route meets all current constraints within the defined corridor and horizon"}
