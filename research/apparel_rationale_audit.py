"""Frozen post-hoc rationale audit, reusing the existing v8 claim judge unchanged."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from evaluation.report_judge import SYSTEM, VERSION, judge, segment_report
from research.model_config import ROOT
from research.provider_gate import guarded_business_client

OUT = ROOT / "evidence/apparel_rationale_audit_v1"
SOURCE = ROOT / "evidence/apparel_reliability_study_v1"
MODEL = "qwen3.8-flash"
PREFIX = "commerce_apparel_rationale_audit_v1:"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ledger():
    with closing(sqlite3.connect((ROOT / "evidence/api_budget.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        total, count = db.execute("SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls").fetchone()
        study, study_count = db.execute("SELECT COALESCE(SUM(COALESCE(charged,reserved)),0),COUNT(*) FROM calls WHERE purpose LIKE ?", (PREFIX + "%",)).fetchone()
    return {"global_micro_cny": total, "global_rows": count, "study_micro_cny": study, "study_rows": study_count}


class AuditClient:
    def __init__(self):
        self.inner = guarded_business_client()

    def chat(self, messages, **kwargs):
        # Sequential worker; 5 CNY is the existing maximum reservation per call.
        if ledger()["study_micro_cny"] + 5_000_000 > 20_000_000:
            raise RuntimeError("Audit study conservative CNY 20 bound reached")
        kwargs["purpose"] = PREFIX + kwargs["purpose"]
        return self.inner.chat(messages, **kwargs)


def evidence_for(execution, case):
    # Observations also contain policy-specific completion guides and directories.
    # Only the original four-field tool envelope is independent factual evidence.
    fields = ("observation_id", "tool", "success", "result")
    observations = {ident: {k: value[k] for k in fields} for ident, value in execution["observations"].items()}
    tools = [t for t in execution["traces"] if t["kind"] == "tool"]
    assert len({t["observation_id"] for t in tools}) == len(tools) == len(observations)
    for t in tools:
        expected = {k: t[k] for k in fields}
        assert json.dumps(observations[t["observation_id"]], sort_keys=True) == json.dumps(expected, sort_keys=True)
    return {
        "TASK": {"user_request": case["task"], "original_order": execution["before"]["request"]},
        "CONTEXT": {"mode": "Local simulated apparel order and single-corridor transport; no live booking/payment/shipping",
            "as_of_utc": case["now"], "money": "All *_cents fields are USD cents; 100 cents = USD 1",
            "time": "Timestamps ending Z are UTC. A replacement service departure difference is not the old service delay duration. Events after as_of_utc are not yet active.",
            "units": "Only explicit unit/pack evidence establishes pieces per catalogue unit; simulated merchant pack rules need not represent live retailer terms.",
            "confirmation": "Preparing a proposal does not confirm an order or reserve stock. Separate operator confirmation is required.",
            "scope": "Audit supplied rationale assertions only. Full structured report completeness is evaluated separately. Never use final model assertions as evidence."},
        "HOST_BEFORE": execution["before"], "HOST_AFTER": execution["after"],
        "LOG": {"complete_instrumented_leaf_tool_log": True,
            "operations": [{k: t[k] for k in ("observation_id", "tool", "success", "arguments", "role")} for t in tools]},
        **observations,
    }


def calibration_cases():
    evidence = {
        "TASK": {"request": "Review a simulated 20-piece black M shirt order to DE; shipping budget USD 200; deadline 2028-04-15T00:00:00Z."},
        "CONTEXT": {"as_of_utc": "2028-04-09T02:00:00Z", "mode": "All commercial fields are simulation; no payment or carrier booking"},
        "PRODUCT": {"sku": "CAL-TEE", "brand": "LabBrand", "size": "M", "color": "black", "pieces_per_catalog_unit": 2,
            "available_catalog_units": 12, "quantity_catalog_units": 10, "quantity_pieces": 20, "material": None},
        "OLD": {"proposal_id": "PROP-OLD", "version": 1, "state": "superseded", "service_id": "AIR@2028-04-10T16:00:00Z",
            "nominal_departure": "2028-04-10T16:00:00Z", "recorded_departure": "2028-04-10T16:00:00Z"},
        "EVENT": {"event_id": "DELAY", "service_id": "AIR@2028-04-10T16:00:00Z", "kind": "delay",
            "delay_minutes": 2160, "published_at": "2028-04-09T01:00:00Z", "old_service_departure_after_event": "2028-04-12T04:00:00Z"},
        "FUTURE": {"event_id": "CANCEL-LATER", "kind": "cancel", "service_id": "AIR@2028-04-11T04:00:00Z",
            "published_at": "2028-04-10T01:00:00Z", "currently_applied": False},
        "NEW": {"proposal_id": "PROP-NEW", "version": 2, "previous_proposal_id": "PROP-OLD", "state": "pending",
            "service_id": "AIR@2028-04-11T04:00:00Z", "departure_at": "2028-04-11T04:00:00Z",
            "arrival_at": "2028-04-13T08:00:00Z", "shipping_cost_cents": 16800, "feasible": True},
        "HOST": {"confirmation": None, "inventory_reserved": False, "approval_for_red_variant": False},
        "NO_ROUTE": {"separate_scenario": True, "result": "infeasible", "budget_cents": 100,
            "deadline_at": "2028-04-09T03:00:00Z", "excluded_legs": ["SEA"], "reason": "No route satisfies all supplied constraints"},
    }
    fixtures = [
        ("delay_duration", "原班次累计延误36小时；改选班次在原计划出发时间之后12小时出发。", "supported"),
        ("delay_confusion", "原班次只延误了12小时。", "unsupported"),
        ("replacement_is_not_delay", "新选的班次是4月11日04:00 UTC出发，原班次按已发布延误应于4月12日04:00 UTC出发。", "supported"),
        ("wrong_service_identity", "我们仍使用原来4月10日16:00 UTC的同一个航班。", "unsupported"),
        ("future_event", "取消事件尚未发布生效，目前不能按已取消处理新班次。", "supported"),
        ("future_cancelled_now", "截至当前时刻，新班次已因取消事件而取消。", "unsupported"),
        ("money", "模拟运输报价为168美元，低于200美元预算。", "supported"),
        ("wrong_cents", "运输费用为16800美元。", "unsupported"),
        ("units", "本模拟目录每单位两件，十个单位对应二十件。", "supported"),
        ("wrong_units", "本模拟订单十个目录单位只对应十件衣服。", "unsupported"),
        ("pending", "第2版提案待确认，尚未确认订单，也没有预留库存。", "supported"),
        ("paid", "这笔真实订单已经付款并由承运商订舱。", "unsupported"),
        ("unapproved", "红色替代尚未获得批准，需要用户确认。", "supported"),
        ("approved", "用户已经批准改用红色款式。", "unsupported"),
        ("unknown_material", "所提供记录没有给出服装的材质。", "supported"),
        ("invented_material", "该款衣服由100%有机棉制作。", "insufficient_evidence"),
        ("infeasible_constraints", "单独的NO_ROUTE情形在所给预算、交期及排除SEA的条件下没有可行路线。", "supported"),
        ("unproven_counterfactual", "单独的NO_ROUTE情形只要允许SEA就一定能满足所有要求。", "insufficient_evidence"),
        ("versions", "PROP-NEW是第2版，其前一个版本为PROP-OLD。", "supported"),
        ("wrong_arrival", "PROP-NEW的到达时间是4月12日08:00 UTC。", "unsupported"),
    ]
    return [{"id": k, "answer": a, "expected": y, "evidence": evidence} for k, a, y in fixtures]


def register():
    if OUT.exists():
        raise FileExistsError("Study already exists; never overwrite registration or requests")
    previous_path = ROOT / "knowledge/reading_review_20260909_tau.json"
    previous = read(previous_path)
    assert all(sha(ROOT / f) == h for f, h in previous["knowledge_file_sha256"].items())
    replay = read(ROOT / "evidence/apparel_tau_replay_v1/summary.json")
    assert replay["all_replay_checks_passed"]
    assert all(sha(ROOT / "evidence/apparel_tau_replay_v1" / f) == h for f, h in replay["artifact_sha256"].items())
    records = {r["case_id"] + "-" + r["condition"]: r for r in replay["records"]}
    original = read(SOURCE / "registration.json")
    cases = {c["id"]: c for c in read(SOURCE / "cases.json")}
    inputs, labels, sources = [], [], {}
    for ident, condition in original["jobs"]:
        key = ident + "-" + condition
        path = SOURCE / "runs" / key / "execution.json"
        x = read(path)
        assert sha(path) == records[key]["original_execution_sha256"]
        answer = (x.get("report") or {}).get("decision", {}).get("rationale")
        ev = evidence_for(x, cases[ident]) if answer else None
        if answer:
            segment_report(answer)
            # Strict full evidence; abort instead of truncating facts to fit a call.
            assert len(json.dumps({"answer": answer, "evidence": ev}, ensure_ascii=False).encode()) < 78_000
        inputs.append({"id": key, "answer": answer, "evidence": ev})
        labels.append({"id": key, "condition": condition, "family": cases[ident]["family"],
            "original_strict_task_passed": records[key]["original_strict_task_passed"]})
        sources[path.relative_to(ROOT).as_posix()] = sha(path)
    paths = [Path(__file__).resolve(), ROOT / "research/APPAREL_RATIONALE_AUDIT_PROTOCOL.md",
        ROOT / "evaluation/report_judge.py", ROOT / "research/model_client.py", ROOT / "research/provider_gate.py",
        ROOT / "research/rate_card.json", ROOT / "delivery_budget.py", ROOT / "delivery_budget_policy.json",
        SOURCE / "registration.json", SOURCE / "cases.json", ROOT / "evidence/apparel_tau_replay_v1/summary.json",
        previous_path, ROOT / "tests/test_apparel_rationale_audit.py"]
    sources.update({p.relative_to(ROOT).as_posix(): sha(p) for p in paths})
    assert ledger()["study_rows"] == 0
    save(OUT / "inputs.json", inputs)
    save(OUT / "labels.json", labels)
    save(OUT / "calibration_inputs.json", calibration_cases())
    save(OUT / "registration.json", {"registered_at": datetime.now(timezone.utc).isoformat(), "model": MODEL,
        "judge_version": VERSION, "system_sha256": hashlib.sha256(SYSTEM.encode()).hexdigest(), "scheduled": 144,
        "available_rationales": sum(bool(i["answer"]) for i in inputs), "input_sha256": sha(OUT / "inputs.json"),
        "label_sha256": sha(OUT / "labels.json"), "calibration_input_sha256": sha(OUT / "calibration_inputs.json"),
        "source_sha256": sources, "ledger_before": ledger(), "study_bound_cny": 20, "estimated_cny": [1, 5],
        "project_bound_cny": 480, "calibration_minimum_correct": 18, "calibration_all_contradictions_required": True,
        "scope": "Post-hoc raw rationale only; all 144 original attempts retained, labels never sent to judge, no new business tasks or official external scores."})
    print(json.dumps({"registered": True, "cases": 144, "rationales": sum(bool(i["answer"]) for i in inputs),
        "largest_evidence_bytes": max(len(json.dumps(i, ensure_ascii=False).encode()) for i in inputs), "ledger": ledger()}, ensure_ascii=False))


def run(mode):
    reg = read(OUT / "registration.json")
    assert all(sha(ROOT / f) == h for f, h in reg["source_sha256"].items())
    assert sha(OUT / "inputs.json") == reg["input_sha256"]
    assert sha(OUT / "labels.json") == reg["label_sha256"]
    assert sha(OUT / "calibration_inputs.json") == reg["calibration_input_sha256"]
    folder = OUT / mode
    if (folder / "summary.json").exists():
        raise FileExistsError("Completed audit is frozen")
    if mode == "campaign":
        assert read(OUT / "calibration/summary.json")["gate_passed"], "Calibration gate did not pass"
    items = read(OUT / ("calibration_inputs.json" if mode == "calibration" else "inputs.json"))
    labels = {r["id"]: r for r in read(OUT / "labels.json")}
    client = AuditClient()
    rows = []
    for item in items:
        row_path = folder / "rows" / (item["id"] + ".json")
        marker = folder / "attempts" / (item["id"] + ".json")
        if row_path.exists():
            rows.append(read(row_path))
            continue
        if marker.exists():
            raise RuntimeError("Unfinished request marker requires inspection, not duplicate inference")
        if not item["answer"]:
            row = {"id": item["id"], "status": "not_assessable", "error": "No original completed rationale"}
        else:
            client.inner.ensure_available(MODEL)
            save(marker, {"started_at": datetime.now(timezone.utc).isoformat(), "input_sha256": hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()})
            raw = folder / "raw" / (item["id"] + ".json")
            raw.parent.mkdir(parents=True, exist_ok=True)
            try:
                measured = judge(item["answer"], item["evidence"], client=client, raw_path=raw, model=MODEL)
                row = {"id": item["id"], "status": "audited", **measured}
            except Exception as error:
                row = {"id": item["id"], "status": "audit_failed", "error": str(error)[:1000], "transport_failures": getattr(error, "attempts", [])}
        if mode == "calibration":
            row["expected"] = item["expected"]
            row["agrees"] = row.get("decision", {}).get("verdict") == item["expected"]
        else:
            row.update(labels[item["id"]])
        save(row_path, row)
        rows.append(row)
        print(json.dumps({"stage": mode, "completed": len(rows), "total": len(items), "id": item["id"],
            "status": row["status"], "verdict": row.get("decision", {}).get("verdict"), "agrees": row.get("agrees")}, ensure_ascii=False), flush=True)
    summary = {"completed_at": datetime.now(timezone.utc).isoformat(), "scheduled": len(items),
        "audited": sum(r["status"] == "audited" for r in rows), "not_assessable": sum(r["status"] == "not_assessable" for r in rows),
        "audit_failed": sum(r["status"] == "audit_failed" for r in rows),
        "verdicts": {v: sum(r.get("decision", {}).get("verdict") == v for r in rows) for v in ("supported", "unsupported", "insufficient_evidence")},
        "ledger_after": ledger(), "requested_model": MODEL,
        "returned_models": sorted({r["response_metadata"]["returned_model"] for r in rows if "response_metadata" in r}),
        "registration_sha256": sha(OUT / "registration.json"),
        "row_sha256": {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted((folder / "rows").glob("*.json"))}}
    if mode == "calibration":
        correct = sum(r["agrees"] for r in rows)
        critical = all(r["agrees"] for r in rows if r["expected"] == "unsupported")
        summary.update(correct=correct, critical_contradictions_passed=critical, gate_passed=correct >= 18 and critical)
    else:
        summary["groups"] = {}
        for condition in sorted({r["condition"] for r in rows}):
            rs = [r for r in rows if r["condition"] == condition]
            summary["groups"][condition] = {"scheduled": len(rs), "audited": sum(r["status"] == "audited" for r in rs),
                "original_task_passed": sum(r["original_strict_task_passed"] for r in rs),
                "original_task_and_judge_supported": sum(r["original_strict_task_passed"] and r.get("decision", {}).get("verdict") == "supported" for r in rs),
                "verdicts": {v: sum(r.get("decision", {}).get("verdict") == v for r in rs) for v in ("supported", "unsupported", "insufficient_evidence")}}
    save(folder / "summary.json", summary)
    print(json.dumps({k:v for k,v in summary.items() if k != "row_sha256"}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("register", "calibration", "campaign"))
    args = parser.parse_args()
    register() if args.mode == "register" else run(args.mode)
