from copy import deepcopy
import json
import pytest

from research.apparel_rationale_audit import SOURCE, calibration_cases, evidence_for
from evaluation.report_judge import segment_report


def fixture():
    cases = json.loads((SOURCE / "cases.json").read_text(encoding="utf-8"))
    case = next(c for c in cases if c["id"] == "RL-01-0")
    x = json.loads((SOURCE / "runs/RL-01-0-v6_single/execution.json").read_text(encoding="utf-8"))
    return x, case


def test_evidence_keeps_sources_but_not_gold_or_model_rationale():
    x, case = fixture()
    ev = evidence_for(x, case)
    assert not ({"expected", "report", "condition", "evaluation"} & ev.keys())
    assert x["report"]["decision"]["rationale"] not in json.dumps(ev, ensure_ascii=False)
    assert all(ev[k] == {name: v[name] for name in ("observation_id", "tool", "success", "result")} for k, v in x["observations"].items())
    assert all("completion_guide" not in ev[k] for k in x["observations"])
    assert ev["HOST_BEFORE"] == x["before"] and ev["HOST_AFTER"] == x["after"]
    assert ev["CONTEXT"]["as_of_utc"] == case["now"]


def test_changed_observation_cannot_silently_become_evidence():
    x, case = fixture()
    x = deepcopy(x)
    x["observations"]["O-1"]["result"]["confirmation"] = "fabricated"
    with pytest.raises(AssertionError):
        evidence_for(x, case)


def test_omitted_leaf_tool_is_rejected():
    x, case = fixture()
    x["traces"] = [t for t in x["traces"] if not (t["kind"] == "tool" and t["observation_id"] == "O-1")]
    with pytest.raises(AssertionError):
        evidence_for(x, case)


def test_segmentation_covers_every_non_whitespace_character():
    text = "延误36小时。\n改选12小时后的另一班。价格168.00美元；尚未付款！"
    segments = segment_report(text)
    covered = {i for s in segments for i in range(s["start"], s["end"])}
    assert all(i in covered for i, c in enumerate(text) if not c.isspace())
    assert all(text[s["start"]:s["end"]] == s["text"] for s in segments)


def test_calibration_preserves_failure_modes_and_distinct_labels():
    rows = calibration_cases()
    assert len(rows) == len({r["id"] for r in rows}) == 20
    assert {r["expected"] for r in rows} == {"supported", "unsupported", "insufficient_evidence"}
    assert sum(r["expected"] == "unsupported" for r in rows) == 8
    assert "delay_confusion" in {r["id"] for r in rows}
