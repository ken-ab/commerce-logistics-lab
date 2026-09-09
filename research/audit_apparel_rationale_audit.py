"""Independent stdlib verification of frozen inputs, judge binding and paid accounting."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "commerce_apparel_rationale_audit_v1:"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def main():
    output = ROOT / "evidence/apparel_rationale_audit_checks_20260909.json"
    if output.exists():
        raise FileExistsError("Independent audit already recorded")
    cards = read(ROOT / "research/rate_card.json")
    original = ROOT / "evidence/apparel_reliability_study_v1"
    cases = {c["id"]: c for c in read(original / "cases.json")}
    verified, known_calls, phases, model_comparison = {}, {}, {}, []
    with closing(sqlite3.connect((ROOT / "evidence/api_budget.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        paid = {r["id"]: dict(r) for r in db.execute("SELECT * FROM calls WHERE substr(purpose,1,?)=?", (len(PREFIX), PREFIX))}
        total = dict(db.execute("SELECT SUM(COALESCE(charged,reserved)) AS micro_cny,COUNT(*) AS rows FROM calls").fetchone())
    previous_inputs = None
    for version in ("v1", "v2"):
        folder = ROOT / "evidence" / ("apparel_rationale_audit_" + version)
        if not (folder / "registration.json").exists():
            continue
        reg = read(folder / "registration.json")
        for name, digest in reg["source_sha256"].items():
            assert sha(ROOT / name) == digest, name
            verified[name] = digest
        for name, key in (("inputs.json", "input_sha256"), ("labels.json", "label_sha256"), ("calibration_inputs.json", "calibration_input_sha256")):
            assert sha(folder / name) == reg[key]
            verified[(folder / name).relative_to(ROOT).as_posix()] = reg[key]
        comparison = [reg[k] for k in ("input_sha256", "label_sha256", "calibration_input_sha256")]
        if previous_inputs:
            assert previous_inputs == comparison
        previous_inputs = comparison
        inputs = read(folder / "inputs.json")
        labels = {r["id"]: r for r in read(folder / "labels.json")}
        assert len(inputs) == len(labels) == 144
        for item in inputs:
            key = item["id"]
            case_id = key.split("-v", 1)[0]
            source = read(original / "runs" / key / "execution.json")
            original_result = read(original / "runs" / key / "result.json")
            assert labels[key]["original_strict_task_passed"] == original_result["evaluation"]["passed"]
            assert item["answer"] == (source.get("report") or {}).get("decision", {}).get("rationale")
            if not item["answer"]:
                assert item["evidence"] is None
                continue
            ev = item["evidence"]
            assert set(ev) == {"TASK", "CONTEXT", "HOST_BEFORE", "HOST_AFTER", "LOG"} | set(source["observations"])
            assert same(ev["HOST_BEFORE"], source["before"]) and same(ev["HOST_AFTER"], source["after"])
            assert ev["TASK"] == {"user_request": cases[case_id]["task"], "original_order": source["before"]["request"]}
            assert ev["CONTEXT"]["as_of_utc"] == cases[case_id]["now"]
            tools = [t for t in source["traces"] if t["kind"] == "tool"]
            assert len(tools) == len(source["observations"])
            for t in tools:
                assert same(ev[t["observation_id"]], {k:t[k] for k in ("observation_id", "tool", "success", "result")})
            assert same(ev["LOG"]["operations"], [{k:t[k] for k in ("observation_id", "tool", "success", "arguments", "role")} for t in tools])
        for phase in ("calibration", "campaign"):
            base = folder / phase
            if not (base / "summary.json").exists():
                if base.exists():
                    raise RuntimeError("Audit phase is unfinished; do not call incomplete data final")
                continue
            summary = read(base / "summary.json")
            input_list = read(folder / ("calibration_inputs.json" if phase == "calibration" else "inputs.json"))
            by_id = {i["id"]: i for i in input_list}
            rows = [read(p) for p in sorted((base / "rows").glob("*.json"))]
            assert len(rows) == len(by_id) == summary["scheduled"]
            assert {r["id"] for r in rows} == set(by_id)
            for name, digest in summary["row_sha256"].items():
                assert sha(ROOT / name) == digest
                verified[name] = digest
            audited, segments = 0, 0
            for row in rows:
                item = by_id[row["id"]]
                if not item["answer"]:
                    assert row["status"] == "not_assessable" and "decision" not in row
                elif row["status"] == "audited":
                    audited += 1
                    claims = row["decision"]["claims"]
                    assert len({c["segment_id"] for c in claims}) == len(claims)
                    assert "".join(ch for c in claims for ch in c["quote"] if not ch.isspace()) == "".join(c for c in item["answer"] if not c.isspace())
                    assert all(c["status"] in {"supported", "contradicted", "unverifiable"} for c in claims)
                    assert all(set(c["evidence_ids"]) <= set(item["evidence"]) for c in claims)
                    assert all(c["evidence_ids"] for c in claims if c["status"] == "supported")
                    derived = "unsupported" if any(c["status"] == "contradicted" for c in claims) else "insufficient_evidence" if any(c["status"] == "unverifiable" for c in claims) else "supported"
                    assert row["decision"]["verdict"] == derived
                    raw = read(base / "raw" / (row["id"] + ".json"))
                    tool = json.loads(raw["message"]["tool_calls"][0]["function"]["arguments"])
                    assert same(sorted(tool["claims"], key=lambda x:x["segment_id"]), [{k:v for k,v in c.items() if k != "quote"} for c in claims])
                    segments += len(claims)
                else:
                    assert row["status"] == "audit_failed" and "decision" not in row
                if phase == "calibration":
                    assert row["expected"] == item["expected"]
                    assert row["agrees"] == (row.get("decision", {}).get("verdict") == item["expected"])
                else:
                    assert all(row[k] == v for k,v in labels[row["id"]].items())
            assert audited == summary["audited"]
            assert sum(r['status'] == 'audit_failed' for r in rows) == summary['audit_failed']
            assert sum(r['status'] == 'not_assessable' for r in rows) == summary['not_assessable']
            verdicts = {v: sum(r.get("decision", {}).get("verdict") == v for r in rows) for v in ("supported", "unsupported", "insufficient_evidence")}
            assert verdicts == summary["verdicts"]
            if phase == "calibration":
                agree = sum(r["agrees"] for r in rows)
                critical = all(r["agrees"] for r in rows if r["expected"] == "unsupported")
                assert summary["gate_passed"] == (agree >= 18 and critical)
                assert agree == summary["correct"]
                model_comparison.append({"version": version, "model": reg["model"], "correct": agree, "cases": 20,
                    "completed_audits": audited, "gate_passed": summary["gate_passed"]})
            else:
                assert read(folder / "calibration/summary.json")["gate_passed"]
                for condition, group in summary["groups"].items():
                    chosen = [r for r in rows if r["condition"] == condition]
                    assert len(chosen) == group["scheduled"] == 24
                    assert sum(r['status'] == 'audited' for r in chosen) == group['audited']
                    assert sum(r['original_strict_task_passed'] for r in chosen) == group['original_task_passed']
                    assert {v:sum(r.get('decision', {}).get('verdict') == v for r in chosen) for v in verdicts} == group['verdicts']
                    assert group["original_task_and_judge_supported"] == sum(r["original_strict_task_passed"] and r.get("decision", {}).get("verdict") == "supported" for r in chosen)
            raw_cost = Decimal(0)
            for path in (base / "raw").glob("*.json"):
                raw = read(path)
                if "budget_call_id" not in raw:
                    continue
                call_id = raw["budget_call_id"]
                assert call_id not in known_calls and call_id in paid
                record = paid[call_id]
                assert record["model"] == raw["requested_model"] == reg["model"]
                assert record["status"] == "settled" and record["price_version"] == cards["version"]
                counts = raw["usage"]
                assert json.loads(record["usage"]) == counts
                rate = cards["models"][reg["model"]]
                amount = (Decimal(counts["prompt_tokens"]) * Decimal(rate["input_per_million"]) + Decimal(counts["completion_tokens"]) * Decimal(rate["output_per_million"])) / 1_000_000
                assert amount == Decimal(raw["estimated_cost_cny"])
                assert record["charged"] == int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
                raw_cost += amount
                known_calls[call_id] = {"phase": version + "/" + phase, "source": path.relative_to(ROOT).as_posix()}
            phases[version + "/" + phase] = {"scheduled": len(rows), "audited": audited, "audited_segments": segments,
                "verdicts": verdicts, "completed_response_cost_cny": str(raw_cost), "scope": "Includes saved model responses that later failed schema validation; not unresolved timeout reservations."}
        for path in folder.rglob("*.json"):
            verified[path.relative_to(ROOT).as_posix()] = sha(path)
    unmatched = {k:v for k,v in paid.items() if k not in known_calls}
    assert all(v["status"] == "uncertain" and v["charged"] is None for v in unmatched.values())
    cost = sum(v["charged"] if v["charged"] is not None else v["reserved"] for v in paid.values())
    assert cost <= 20_000_000 and total["micro_cny"] <= 480_000_000
    result = {"created_at": datetime.now(timezone.utc).isoformat(), "all_checks_passed": True, "phases": phases,
        "calibration_comparison": model_comparison, "input_identity_preserved": True, "known_model_responses": len(known_calls),
        "uncertain_request_count": len(unmatched), "uncertain_reservations_micro_cny": sum(v["reserved"] for v in unmatched.values()),
        "uncertain_scope": "Retained at study level; legacy transport error records lack call IDs, so not claimed as independently attributable to individual reports.",
        "study_micro_cny": cost, "study_calls": len(paid), "global_ledger": total, "new_business_agent_runs": 0,
        "new_real_users": 0, "verified_sha256": verified, "audit_source_sha256": sha(Path(__file__)),
        "scope": "Independent input, segment, raw model response and accounting audit. Does not certify judge factual truth or override original task results."}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k != "verified_sha256"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
