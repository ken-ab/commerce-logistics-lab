"""Explain completed ranking errors without calling a model or changing any score."""
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean

from ranking_compare.experiment import ROOT, OUT, read, sha, score_order, validate_method


def main():
    validate_method()
    verified = read(OUT / "integrity_and_accounting.json")
    validation = read(OUT / "validation_summary.json")
    assert verified["status"] == "verified" and validation["status"] == "complete"
    model = validation["model"]
    records = sorted((OUT / "validation" / model).glob("*/result.json"))
    assert len(records) == 48
    rows, hashes = [], {}
    priority = {"E": 3, "S": 2, "C": 1, "I": 0}
    for path in records:
        relative = str(path.relative_to(ROOT))
        assert sha(path) == verified["sources_sha256"][relative]
        trial = read(path)
        source = ROOT / trial["source"]
        assert sha(source) == trial["source_sha256"]
        baseline = read(source)
        assert trial["status"] == "valid"
        assert score_order(baseline, trial["order"]) == trial["metrics"]
        mapping = {x["alias"]: x["product_index"] for x in baseline["presented_aliases"]}
        assert len(trial["order"]) == len(set(trial["order"])) == len(mapping)
        ranked = [mapping[a] for a in trial["order"]]
        oracle = sorted(mapping, key=lambda a: -priority[baseline["products"][mapping[a]]["label"]])
        ceiling = score_order(baseline, oracle)
        products = baseline["products"]

        def describe(index):
            product = products[index]
            return {"product_id": product["product_id"], "label": product["label"],
                    "document": product["document"],
                    "baseline_rank": baseline["baseline_order"].index(index) + 1,
                    "refined_rank": ranked.index(index) + 1 if index in ranked else None}

        rows.append({
            "query_id": trial["query_id"], "locale": trial["locale"], "query": baseline["query"],
            "source": trial["source"], "result": relative,
            "baseline_metrics": trial["baseline_metrics"], "refined_metrics": trial["metrics"],
            "label_oracle_top10_metrics": ceiling,
            "top10_has_exact": any(products[i]["label"] == "E" for i in baseline["shortlist_indices"]),
            "pool_has_exact": any(p["label"] == "E" for p in products),
            "ndcg_delta": trial["metrics"]["ndcg_at_10"] - trial["baseline_metrics"]["ndcg_at_10"],
            "refined_top3": [describe(i) for i in ranked[:3]],
            "exact_products_in_top10": [describe(i) for i in baseline["shortlist_indices"] if products[i]["label"] == "E"],
        })
        hashes[relative] = sha(path)
        hashes[trial["source"]] = sha(source)
    for name in ("ndcg_at_10", "hit_exact_at_1"):
        assert abs(mean(r["refined_metrics"][name] for r in rows) - validation["candidate"]["metrics"][name]) < 1e-12
    summary = {
        "queries": len(rows), "top10_with_exact": sum(r["top10_has_exact"] for r in rows),
        "pools_with_exact": sum(r["pool_has_exact"] for r in rows),
        "top1_misses": sum(r["refined_metrics"]["hit_exact_at_1"] == 0 for r in rows),
        "ndcg_better": sum(r["ndcg_delta"] > 1e-12 for r in rows),
        "ndcg_equal": sum(abs(r["ndcg_delta"]) <= 1e-12 for r in rows),
        "ndcg_worse": sum(r["ndcg_delta"] < -1e-12 for r in rows),
        "label_oracle_top10_ndcg": mean(r["label_oracle_top10_metrics"]["ndcg_at_10"] for r in rows),
        "label_oracle_top10_hit": mean(r["label_oracle_top10_metrics"]["hit_exact_at_1"] for r in rows),
    }
    result = {"status": "verified_descriptive_analysis", "created_at": datetime.now(timezone.utc).isoformat(),
              "scope": "Post-hoc explanation of the completed 48-query validation; labels are used only for diagnosis. No new model calls, tuning, relabeling or changes to the selection decision.",
              "summary": summary, "cases": rows, "sources_sha256": hashes}
    target = ROOT / "evidence" / "ranking_compare_posthoc"
    target.mkdir(exist_ok=True)
    (target / "validation_errors.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    misses = [{k: row[k] for k in ("query_id", "locale", "query", "top10_has_exact", "refined_top3", "exact_products_in_top10")}
              for row in rows if row["refined_metrics"]["hit_exact_at_1"] == 0]
    print(json.dumps({"summary": summary, "top1_misses": misses}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
