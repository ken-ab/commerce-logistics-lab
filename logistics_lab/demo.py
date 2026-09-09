from __future__ import annotations

import json

from .planning import ROOT, audit_report, plan_fulfilment, review_and_replan


def main() -> None:
    cases = json.loads((ROOT / "data/scenarios.json").read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        plan = plan_fulfilment(case["order"])
        audit = audit_report(case["order"], plan)
        matched = plan["status"] == case["expected_status"]
        if "expected_cost_usd" in case:
            matched = matched and plan.get("total_cost_usd") == case["expected_cost_usd"] and plan.get("transit_days") == case["expected_days"] and audit["passed"]
        rows.append({"case_id": case["id"], "expected_status": case["expected_status"], "plan": plan, "audit": audit, "matches_authored_expectation": matched})
    demo = review_and_replan(cases[0]["order"])
    output = ROOT / "evidence"
    output.mkdir(exist_ok=True)
    payload = {"scope": "12 authored synthetic regression cases; not held-out research evaluation or LLM performance", "case_count": len(rows), "matched_count": sum(x["matches_authored_expectation"] for x in rows), "cases": rows, "bounded_replan_demo": demo}
    (output / "simulation_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 服装订单与物流规划：离线原型演示", "", "数据：synthetic-apparel-v1-20260907。全部为合成记录，未连接模型或真实物流服务。", "", "| 场景 | 预期状态 | 实际状态 | 运输费 USD | 时效 / 天 |", "|---|---|---|---|---|"]
    for row in rows:
        p = row["plan"]
        lines.append(f"| {row['case_id']} | {row['expected_status']} | {p['status']} | {p.get('total_cost_usd', '—')} | {p.get('transit_days', '—')} |")
    lines += ["", "## 一次有界重规划", "", "订单：60 件 TEE-BLK-M，7 天内送达，运输预算 USD 300。", "", f"只比较价格的诊断方案：USD {demo['initial']['total_cost_usd']}，{demo['initial']['transit_days']} 天。审查发现超过时限。", "", f"按约束重规划后：USD {demo['final']['total_cost_usd']}，{demo['final']['transit_days']} 天，路线 {' → '.join(demo['final']['leg_ids'])}。", "", "这说明测试能够发现并修复预设的约束错误，不证明多 Agent 优于单 Agent，也不代表你的 Finance-Agent 已经获得质量提升。", "", "## 报告核验范围", "", "核验器从源记录复算所选 SKU 数量、库存、重量、运输费、时效、路线连续性、运力、封闭路段及证据 ID。它不对任意 Markdown 的语义作判断。本报告由经过核验的结构化结果渲染。", "", "非 planned 输出单独对照人工编写的预期状态，不因报告为空而判为事实完全正确。"]
    (output / "demo_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(rows), "matched": payload["matched_count"], "report": str(output / "demo_report.md")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
